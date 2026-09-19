"""Production-шлюз архивной линии космопогоды — NASA CCMC DONKI (FN-41, этап 3).

Слой ``sources``: получает уведомления за запрошенный период и сохраняет их
записями хранилища — не интерпретирует (какой ``messageType`` считается
событием Механизма 1, что значит их отсутствие и как это превращается в
оценку окна, решает ``src/domain/spaceweather/historical_events.py``,
main-prompt.md §8).

Разбор ответа и нормализация в ``RecordInput`` уже реализованы и проверены
на реальных сохранённых ответах зондом FN-23
(``src/sources/archive_probe.py``: ``parse_donki_notifications``,
``donki_notification_to_record_input``) — этот модуль их переиспользует, а не
переписывает. Новое здесь ровно то, чего зонду не хватало до production
(его собственный docstring: «постраничная загрузка всего архива — задача
следующего этапа»): реальный HTTP через общий клиент
(``src/sources/http.py`` — раздельные таймауты, ограниченные повторы,
отдельная обработка 429), реестр статусов источника (``/api/sources/status``,
``/api/sources/refresh``) и честное поднятие отказа наверх — отказ получения
НИКОГДА не выглядит как «уведомлений не было» (main-prompt.md §2; ради
этого :class:`DonkiFetchOutcome` несёт и ``covered_start``/``covered_end``,
которые заполняются ТОЛЬКО при реальном успехе).

**Объём FN-41 — загрузка по требованию для окон конкретного расчёта**, не
фоновая постраничная выкачка всего обязательного периода и не планировщик:
один запрос на расчёт покрывает оба сравниваемых окна вместе с обратным
запасом (:func:`fetch_and_store_window` вызывается один раз на расчёт с
объединённым интервалом). Полная предзагрузка периода
(docs/method.md §5 «Способ загрузки в этапе 3», 4 запроса по 15 суток) этим
шлюзом не делается и остаётся следующей задачей; повторный расчёт по тому
же периоду перезапрашивает архив (дублей записей при этом не возникает —
дедупликация по ``(source_id, provider_record_id, source_version)`` в
``src/store/records.py``), журнала покрытия запросов пока нет.

**Ключ API.** ``api.nasa.gov`` требует ``api_key`` в query-строке.
Значение берётся из переменной окружения :data:`API_KEY_ENV`
(``NASA_API_KEY``), по умолчанию — публичный демонстрационный ``DEMO_KEY``
NASA (не секрет, лимит ~10 запросов в час, ``sources.yaml`` →
``nasa-donki-notifications.access_restrictions``). Ключ не попадает ни в
одну строку, которая может быть сохранена или показана: ``source_url``
записи и все сообщения об ошибках строятся из URL БЕЗ ключа
(:func:`build_request_url`), а текст исключений общего HTTP-клиента
(который получает URL с ключом) проходит через :func:`_mask_api_key`
прежде, чем попасть в реестр статусов, предупреждения результата или лог
(main-prompt.md §7 «в логи не попадают ... полные URL с ключом в
query-строке»).
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

import httpx
import yaml

from src.sources import http as source_http
from src.sources.archive_probe import (
    ArchiveFormatError,
    donki_notification_to_record_input,
    parse_donki_notifications,
)
from src.sources.status import SourceStatus, SourceStatusRegistry, effective_status
from src.store.records import (
    DuplicateKeyConflictError,
    RawOriginalStore,
    insert_record,
)

SOURCE_ID = "nasa-donki-notifications"

API_KEY_ENV = "NASA_API_KEY"
DEFAULT_API_KEY = "DEMO_KEY"

UTC = timezone.utc

_DEFAULT_SOURCES_YAML = Path(__file__).resolve().parent.parent.parent / "sources.yaml"

#: Запас по обе стороны запрашиваемого периода в СУТКАХ. DONKI фильтрует по
#: времени ВЫПУСКА уведомления (``messageIssueTime``), а не по времени
#: события: уведомление о событии, случившемся в начале первых запрошенных
#: суток, может быть выпущено накануне. Сутки запаса назад закрывают этот
#: разрыв с запасом (реально наблюдаемая задержка выпуска — минуты-часы:
#: SEP 2024-05-10 13:35Z выпущен в 13:46Z, GST за синоптический период
#: 15:00–18:00Z выпущен в 18:44Z, tests/fixtures/sources/archive/README.md).
#: Поэтому и ПОКРЫТИЕ по времени события заявляется на сутки уже
#: запрошенного окна выпуска — см. :func:`fetch_and_store_window`.
_ISSUE_TIME_MARGIN_DAYS = 1

Outcome = Literal[
    "stored",
    "skipped_disabled",
    "skipped_frozen",
    "error_timeout",
    "error_quota",
    "error_format",
    "error_http",
    "error_conflict",
]


@dataclass(frozen=True)
class DonkiSourceConfig:
    """Конфигурация коннектора из ``sources.yaml`` (.ai/main-prompt.md §7 —
    адреса, таймауты и пороги в конфиге, не в коде).

    ``qualifying_message_types``/``event_persistence_lookback_hours``
    относятся к интерпретации (домен), но их ЗНАЧЕНИЯ, как и любые пороги,
    живут в реестре источников и передаются в чистую функцию расчёта
    вызывающей стороной — ``src/domain/spaceweather/historical_events.py``
    не читает ни файл, ни сеть.
    """

    source_id: str
    url: str
    connect_timeout_seconds: float
    read_timeout_seconds: float
    max_retries: int
    backoff_base_seconds: float
    enabled: bool
    qualifying_message_types: tuple[str, ...]
    event_persistence_lookback_hours: float


@dataclass(frozen=True)
class DonkiFetchOutcome:
    """Итог одного вызова :func:`fetch_and_store_window`.

    ``covered_start``/``covered_end`` — интервал ВРЕМЕНИ СОБЫТИЯ, для
    которого этот вызов может утверждать полноту архива; заполняется только
    при ``outcome == "stored"``. При любом другом исходе оба поля ``None`` —
    отказ, квота или неожиданный формат не дают права сказать «событий не
    было» (main-prompt.md §2); домен получает пустой список интервалов
    покрытия и честно отвечает ``INSUFFICIENT_DATA``.

    ``skipped_without_event_time`` — число уведомлений, для которых зонд не
    смог извлечь структурированное время события (~18% реального архива,
    docs/method.md §6): они НЕ сохраняются (``donki_notification_to_record_input``
    возвращает ``None``) и потому остаются потенциально неучтёнными —
    вызывающая сторона показывает это число, а не прячет.

    ``ambiguous_publication_count`` — сколько уведомлений КВАЛИФИЦИРУЮЩЕГО
    типа (``config.qualifying_message_types``) в полученном ответе имеют
    неразрешимое время публикации (``resolved_issue_time is None``: два
    независимых поля разошлись, реальный случай ``20240516-7D-001``). Такие
    записи сохраняются, но непригодны для строгого отбора
    (``replay_eligible = false``) — и их отсутствие в строгой выборке нельзя
    читать как «события не было» (main-prompt.md §1). Считаются только
    квалифицирующие типы: расхождение в уведомлении о вспышке не мешает
    судить о протонных событиях и бурях.
    """

    outcome: Outcome
    message: str | None
    stored_record_ids: tuple[str, ...]
    covered_start: datetime | None
    covered_end: datetime | None
    skipped_without_event_time: int
    ambiguous_publication_count: int
    status: SourceStatus


def load_source_config(
    path: str | Path = _DEFAULT_SOURCES_YAML, *, source_id: str = SOURCE_ID
) -> DonkiSourceConfig:
    """Читает запись источника из ``sources.yaml`` заново при каждом вызове
    (то же правило, что и ``src/sources/swpc.py::load_source_config``)."""
    doc = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    for entry in doc.get("space_weather") or []:
        if entry.get("id") == source_id:
            network = entry.get("network") or {}
            assessment = entry.get("historical_assessment") or {}
            return DonkiSourceConfig(
                source_id=source_id,
                url=entry["url"],
                connect_timeout_seconds=float(network["connect_timeout_seconds"]),
                read_timeout_seconds=float(network["read_timeout_seconds"]),
                max_retries=int(network["max_retries"]),
                backoff_base_seconds=float(network["retry_backoff_base_seconds"]),
                enabled=bool(entry.get("enabled", True)),
                qualifying_message_types=tuple(assessment["qualifying_message_types"]),
                event_persistence_lookback_hours=float(
                    assessment["event_persistence_lookback_hours"]
                ),
            )
    raise KeyError(f"source_id {source_id!r} is not registered in {path}")


def resolve_api_key() -> str:
    """Ключ NASA API из окружения; по умолчанию — публичный ``DEMO_KEY``.

    Секрет никогда не лежит в репозитории (main-prompt.md §7): в
    ``.env.example`` только имя переменной с комментарием.
    """
    return os.environ.get(API_KEY_ENV, "").strip() or DEFAULT_API_KEY


def build_request_url(config: DonkiSourceConfig, *, start_day: str, end_day: str) -> str:
    """URL запроса БЕЗ ``api_key`` — именно он сохраняется в ``record.source_url``
    и показывается в сообщениях/логах (main-prompt.md §7)."""
    return f"{config.url}?startDate={start_day}&endDate={end_day}&type=all"


def _authenticated_url(public_url: str, api_key: str) -> str:
    return f"{public_url}&api_key={api_key}"


def _mask_api_key(text: str, api_key: str) -> str:
    """Убирает значение ключа из произвольного текста (сообщение исключения
    общего HTTP-клиента содержит полный URL, включая query-строку)."""
    return text.replace(api_key, "***") if api_key else text


def fetch(
    url: str,
    *,
    config: DonkiSourceConfig,
    client: httpx.Client | None = None,
    now: datetime | None = None,
) -> source_http.HttpFetchResult:
    """Один сетевой вызов архива DONKI — точка подмены в тестах
    (``monkeypatch.setattr``), тем же паттерном, что ``src.sources.swpc.fetch``."""
    return source_http.fetch(
        url,
        connect_timeout_seconds=config.connect_timeout_seconds,
        read_timeout_seconds=config.read_timeout_seconds,
        max_retries=config.max_retries,
        backoff_base_seconds=config.backoff_base_seconds,
        client=client,
        now=now,
    )


def fetch_and_store_window(
    conn: sqlite3.Connection,
    raw_store: RawOriginalStore,
    *,
    config: DonkiSourceConfig,
    registry: SourceStatusRegistry,
    interval_start: datetime,
    interval_end: datetime,
    fetched_at: datetime,
    api_key: str | None = None,
    http_client: httpx.Client | None = None,
) -> DonkiFetchOutcome:
    """Получает и сохраняет уведомления DONKI, покрывающие
    ``[interval_start, interval_end]`` по ВРЕМЕНИ СОБЫТИЯ.

    Никогда не поднимает исключение сама (как
    ``src/sources/swpc.py::fetch_and_store``) — отказ выражается полем
    ``outcome`` и пустым покрытием, потому что решение «что это значит для
    оценки окна» принимает вызывающая сторона, а не шлюз.

    TTL здесь нет осознанно: архивный ответ не меняется (main-prompt.md §5
    «кеш архивных ответов бессрочен»), а TTL-пропуск по свежести последнего
    успеха был бы прямо вредным — он отдал бы «свежий» статус источника
    расчёту за СОВСЕМ ДРУГОЙ период, для которого ничего не загружено.
    Единственные пропуски — документированные переключатели отключения и
    заморозки (main-prompt.md §5, критерий Т6).
    """
    if fetched_at.tzinfo is None:
        raise ValueError("fetched_at must be a timezone-aware UTC datetime (main-prompt.md §1)")

    raw_status = registry.get(config.source_id)
    status = effective_status(raw_status, config_enabled=config.enabled)
    if not config.enabled:
        return DonkiFetchOutcome(
            "skipped_disabled",
            f"source {config.source_id!r} is disabled in sources.yaml",
            (), None, None, 0, 0, status,
        )
    if status.frozen:
        return DonkiFetchOutcome(
            "skipped_frozen", f"source {config.source_id!r} is frozen", (), None, None, 0, 0, status
        )

    margin = timedelta(days=_ISSUE_TIME_MARGIN_DAYS)
    start_day = (interval_start - margin).date()
    end_day = interval_end.date()
    public_url = build_request_url(
        config, start_day=start_day.isoformat(), end_day=end_day.isoformat()
    )
    key = api_key if api_key is not None else resolve_api_key()

    try:
        result = fetch(
            _authenticated_url(public_url, key),
            config=config,
            client=http_client,
            now=fetched_at,
        )
    except source_http.SourceQuotaLimitedError as exc:
        message = _mask_api_key(str(exc), key)
        updated = registry.record_error(
            config.source_id,
            at=fetched_at,
            message=message,
            quota_limited=True,
            retry_after_seconds=exc.retry_after_seconds,
        )
        return DonkiFetchOutcome("error_quota", message, (), None, None, 0, 0, updated)
    except source_http.SourceTimeoutError as exc:
        message = _mask_api_key(str(exc), key)
        updated = registry.record_error(
            config.source_id, at=fetched_at, message=message, quota_limited=False
        )
        return DonkiFetchOutcome("error_timeout", message, (), None, None, 0, 0, updated)
    except source_http.SourceHttpError as exc:
        message = _mask_api_key(str(exc), key)
        updated = registry.record_error(
            config.source_id, at=fetched_at, message=message, quota_limited=False
        )
        return DonkiFetchOutcome("error_http", message, (), None, None, 0, 0, updated)

    try:
        notifications = parse_donki_notifications(result.body)
    except ArchiveFormatError as exc:
        message = _mask_api_key(str(exc), key)
        updated = registry.record_error(
            config.source_id, at=fetched_at, message=message, quota_limited=False
        )
        return DonkiFetchOutcome("error_format", message, (), None, None, 0, 0, updated)

    qualifying_types = {t.strip().upper() for t in config.qualifying_message_types}
    stored_ids: list[str] = []
    conflicts: list[str] = []
    skipped = 0
    ambiguous = 0
    for notification in notifications:
        if (
            notification.resolved_issue_time is None
            and notification.message_type.strip().upper() in qualifying_types
        ):
            # Два независимых указания времени выпуска разошлись — время
            # публикации неизвестно целиком (main-prompt.md §1). Запись всё
            # равно сохраняется (она пригодна для historical_analysis), но
            # считается отдельно: для строгого прогноза это не «события не
            # было», а «подтвердить нечем».
            ambiguous += 1
        record_input = donki_notification_to_record_input(
            notification, source_url=public_url, fetched_at=fetched_at
        )
        if record_input is None:
            skipped += 1
            continue
        try:
            stored_ids.append(insert_record(conn, raw_store, record_input))
        except sqlite3.IntegrityError:
            # Гонка двух конкурентных исторических расчётов за одну и ту же
            # запись архива: insert_record делает SELECT-затем-INSERT без
            # транзакционной защиты, поэтому проигравший получает UNIQUE
            # constraint failed вместо идемпотентного возврата id. Повтор
            # застаёт уже закоммиченную строку — содержимое то же самое (тот
            # же messageID того же архива), это не конфликт версии. Тот же
            # приём, что и в src/api/service.py::fetch_and_store_orbit.
            stored_ids.append(insert_record(conn, raw_store, record_input))
        except DuplicateKeyConflictError as exc:
            conflicts.append(str(exc))

    if conflicts:
        message = (
            f"{len(conflicts)} of {len(notifications)} notification(s) conflicted "
            f"({len(stored_ids)} stored anyway): {conflicts[0]}"
        )
        updated = registry.record_error(
            config.source_id, at=fetched_at, message=message, quota_limited=False
        )
        # Частичная потеря ответа не выдаётся за успех и НЕ даёт покрытия:
        # утверждать полноту архива по неполностью сохранённому ответу
        # нельзя (main-prompt.md §2), тот же принцип, что и в
        # src/sources/swpc.py::fetch_and_store.
        return DonkiFetchOutcome(
            "error_conflict", message, tuple(stored_ids), None, None, skipped, ambiguous, updated
        )

    updated = registry.record_success(config.source_id, at=fetched_at)
    covered_start = datetime.combine(
        start_day + margin, datetime.min.time(), tzinfo=UTC
    )
    covered_end = datetime.combine(end_day, datetime.max.time(), tzinfo=UTC)
    message = (
        f"stored {len(stored_ids)} notification(s) issued {start_day.isoformat()}"
        f"..{end_day.isoformat()}"
    )
    return DonkiFetchOutcome(
        "stored",
        message,
        tuple(stored_ids),
        covered_start,
        covered_end,
        skipped,
        ambiguous,
        updated,
    )


__all__ = [
    "API_KEY_ENV",
    "DEFAULT_API_KEY",
    "SOURCE_ID",
    "DonkiFetchOutcome",
    "DonkiSourceConfig",
    "build_request_url",
    "fetch",
    "fetch_and_store_window",
    "load_source_config",
    "resolve_api_key",
]
