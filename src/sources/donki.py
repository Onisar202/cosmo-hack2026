"""Сетевой шлюз архивной линии космопогоды — NASA CCMC DONKI (FN-41, этап 3).

Слой ``sources``, и только его сетевая часть. Разделение обязанностей после
слияния FN-42 в ``main`` (см. README, раздел «Историческая оркестрация»):

- **разбор ответа и нормализация** — ``src/sources/archive_probe.py``
  (FN-23: ``parse_donki_notifications``, ``donki_notification_to_record_input``);
- **запись в хранилище, карта фактически прочитанных интервалов и строгая
  выборка по ``as_of``** — provider-agnostic адаптер
  ``src/sources/archive_ingest.py`` (FN-42:
  :func:`~src.sources.archive_ingest.ingest_donki_notifications`,
  :func:`~src.sources.archive_ingest.assemble_historical_forecast_input`);
- **интерпретация** — ``src/domain/spaceweather/archive_assessment.py``
  (FN-42, три состояния EVENT_PRESENT/NO_EVENT_DETECTED/INSUFFICIENT_DATA);
- **этот модуль** — ровно то, чего адаптеру не хватало до production: реальный
  HTTP через общий клиент (``src/sources/http.py`` — раздельные таймауты,
  ограниченные повторы, отдельная обработка 429), ключ API и его маскирование,
  реестр статусов источника (``/api/sources/status``, ``/api/sources/refresh``)
  и честное поднятие отказа наверх. Отказ получения НИКОГДА не выглядит как
  «уведомлений не было» (main-prompt.md §2): при любом исходе, кроме
  ``stored``, :class:`DonkiFetchOutcome` не несёт отчёта о загрузке, а значит
  домен не получает ни одного интервала подтверждённого покрытия и честно
  отвечает ``INSUFFICIENT_DATA``.

Здесь нет ни одной ветки вида «какой messageType считать событием» и ни
одного числа горизонта: и то и другое — данные ``sources.yaml``, которые
читает ``archive_ingest.load_archive_product`` (требование постановки FN-41:
источник космопогоды подключается через интерфейс/вход-записи, а спор
«DONKI против NOAA» и спор о горизонте в коде не фиксируются).

**Объём FN-41 — загрузка по требованию для окон конкретного расчёта**, не
фоновая постраничная выкачка всего обязательного периода и не планировщик:
один запрос на расчёт покрывает оба сравниваемых окна вместе с запасом по
времени выпуска. Полная предзагрузка периода (docs/method.md §5, 4 запроса по
15 суток) этим шлюзом не делается и остаётся следующей задачей; повторный
расчёт по тому же периоду перезапрашивает архив (дублей записей при этом не
возникает — дедупликация по ``(source_id, provider_record_id,
source_version)`` в ``src/store/records.py``).

**Ключ API.** ``api.nasa.gov`` требует ``api_key`` в query-строке. Значение
берётся из переменной окружения :data:`API_KEY_ENV` (``NASA_API_KEY``), по
умолчанию — публичный демонстрационный ``DEMO_KEY`` NASA (не секрет, лимит
~10 запросов в час, ``sources.yaml`` →
``nasa-donki-notifications.access_restrictions``). Ключ не попадает ни в одну
строку, которая может быть сохранена или показана: ``source_url`` записи и все
сообщения об ошибках строятся из URL БЕЗ ключа (:func:`build_request_url`), а
текст исключений общего HTTP-клиента (который получает URL с ключом) проходит
через :func:`_mask_api_key` прежде, чем попасть в реестр статусов,
предупреждения результата или лог (main-prompt.md §7).
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
from src.sources.archive_ingest import ArchiveIngestReport, ingest_donki_notifications
from src.sources.archive_probe import ArchiveFormatError
from src.sources.status import SourceStatus, SourceStatusRegistry, effective_status
from src.store.records import RawOriginalStore

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
    """Сетевая конфигурация коннектора из ``sources.yaml`` (.ai/main-prompt.md
    §7 — адреса и таймауты в конфиге, не в коде).

    Пороги и перечни интерпретации СЮДА НЕ ВХОДЯТ намеренно: их читает
    ``src/sources/archive_ingest.py::load_archive_product`` (те же
    ``sources.yaml`` → ``space_weather[*].forecast_horizon_hours`` и
    ``event_message_types``), общий для любого архивного продукта. Две копии
    одного и того же реестра разошлись бы за сутки (main-prompt.md §7).
    """

    source_id: str
    url: str
    connect_timeout_seconds: float
    read_timeout_seconds: float
    max_retries: int
    backoff_base_seconds: float
    enabled: bool
    #: ``archive_request_lookback_hours`` реестра — на сколько часов назад от
    #: начала запрошенного интервала расширяется читаемый (и доказанно
    #: прочитанный) интервал ВРЕМЕНИ СОБЫТИЯ. См. подробное обоснование в
    #: ``sources.yaml`` и :func:`fetch_and_store_window`.
    request_lookback_hours: float


@dataclass(frozen=True)
class DonkiFetchOutcome:
    """Итог одного вызова :func:`fetch_and_store_window`.

    ``report`` — отчёт адаптера FN-42 о фактически загруженном интервале
    (:class:`~src.sources.archive_ingest.ArchiveIngestReport`); заполняется
    ТОЛЬКО при ``outcome == "stored"``. При любом другом исходе он ``None`` —
    отказ, квота или неожиданный формат не дают права сказать «событий не
    было» (main-prompt.md §2); домен получает пустой список интервалов
    покрытия и честно отвечает ``INSUFFICIENT_DATA``.
    """

    outcome: Outcome
    message: str | None
    report: ArchiveIngestReport | None
    status: SourceStatus

    @property
    def stored_record_ids(self) -> tuple[str, ...]:
        return () if self.report is None else self.report.stored_record_ids


def load_source_config(
    path: str | Path = _DEFAULT_SOURCES_YAML, *, source_id: str = SOURCE_ID
) -> DonkiSourceConfig:
    """Читает запись источника из ``sources.yaml`` заново при каждом вызове
    (то же правило, что и ``src/sources/swpc.py::load_source_config``)."""
    doc = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    for entry in doc.get("space_weather") or []:
        if entry.get("id") == source_id:
            network = entry.get("network") or {}
            return DonkiSourceConfig(
                source_id=source_id,
                url=entry["url"],
                connect_timeout_seconds=float(network["connect_timeout_seconds"]),
                read_timeout_seconds=float(network["read_timeout_seconds"]),
                max_retries=int(network["max_retries"]),
                backoff_base_seconds=float(network["retry_backoff_base_seconds"]),
                enabled=bool(entry.get("enabled", True)),
                # Обязательный ключ, без значения по умолчанию: молчаливый
                # ноль означал бы, что уже объявленное явление перед окном
                # тихо выпадает из прочитанного интервала — то есть пробел
                # знания превращается в NO_EVENT_DETECTED (main-prompt.md §2,
                # §7 «пороги и горизонты в конфиге, а не в коде»).
                request_lookback_hours=float(entry["archive_request_lookback_hours"]),
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
    """Получает уведомления DONKI, покрывающие ``[interval_start,
    interval_end]`` по ВРЕМЕНИ СОБЫТИЯ, и передаёт ответ адаптеру FN-42.

    Никогда не поднимает исключение сама (как
    ``src/sources/swpc.py::fetch_and_store``) — отказ выражается полем
    ``outcome`` и отсутствием отчёта, потому что решение «что это значит для
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
            None,
            status,
        )
    if status.frozen:
        return DonkiFetchOutcome(
            "skipped_frozen", f"source {config.source_id!r} is frozen", None, status
        )

    # Читаемый интервал ВРЕМЕНИ СОБЫТИЯ расширяется назад на значение
    # реестра: событийный продукт публикует только начало явления, и уже
    # объявленное перед окном явление обязано попасть в прочитанный интервал,
    # иначе домен его не увидит и пробел знания станет «событий не было»
    # (sources.yaml → archive_request_lookback_hours, main-prompt.md §2).
    event_interval_start = interval_start - timedelta(hours=config.request_lookback_hours)
    margin = timedelta(days=_ISSUE_TIME_MARGIN_DAYS)
    start_day = (event_interval_start - margin).date()
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
        return DonkiFetchOutcome("error_quota", message, None, updated)
    except source_http.SourceTimeoutError as exc:
        message = _mask_api_key(str(exc), key)
        updated = registry.record_error(
            config.source_id, at=fetched_at, message=message, quota_limited=False
        )
        return DonkiFetchOutcome("error_timeout", message, None, updated)
    except source_http.SourceHttpError as exc:
        message = _mask_api_key(str(exc), key)
        updated = registry.record_error(
            config.source_id, at=fetched_at, message=message, quota_limited=False
        )
        return DonkiFetchOutcome("error_http", message, None, updated)

    # Покрытие по ВРЕМЕНИ СОБЫТИЯ уже запаса по времени выпуска: сутки,
    # добавленные назад к запросу, закрывают задержку выпуска, но сами по себе
    # прочитанным интервалом событий не являются.
    covered_start = datetime.combine(start_day + margin, datetime.min.time(), tzinfo=UTC)
    covered_end = datetime.combine(end_day, datetime.max.time(), tzinfo=UTC)
    try:
        report = ingest_donki_notifications(
            conn,
            raw_store,
            result.body,
            # КАНОНИЧЕСКИЙ адрес продукта, а не URL этого конкретного запроса
            # с его датами. Загрузка по требованию перекрывает окна (расчёт на
            # 10 мая и расчёт на 11 мая читают один и тот же диапазон выпуска),
            # и одно и то же неизменяемое уведомление приходит дважды под
            # разными query-строками. Если бы диапазон попадал в source_url
            # записи, дедупликация по (source_id, provider_record_id,
            # source_version) считала бы это КОНФЛИКТОМ содержания и роняла
            # загрузку — хотя уведомление байт в байт то же самое. Конкретная
            # ссылка на само уведомление не теряется: она сохраняется в
            # spatial_context.message_url каждой записи (archive_probe).
            source_url=config.url,
            fetched_at=fetched_at,
            interval_start=covered_start,
            interval_end=covered_end,
        )
    except ArchiveFormatError as exc:
        message = _mask_api_key(str(exc), key)
        updated = registry.record_error(
            config.source_id, at=fetched_at, message=message, quota_limited=False
        )
        return DonkiFetchOutcome("error_format", message, None, updated)

    if report.conflicts:
        message = (
            f"{len(report.conflicts)} notification(s) conflicted "
            f"({report.stored_count} stored anyway): {report.conflicts[0]}"
        )
        updated = registry.record_error(
            config.source_id, at=fetched_at, message=message, quota_limited=False
        )
        # Частичная потеря ответа не выдаётся за успех и НЕ даёт покрытия:
        # утверждать полноту архива по неполностью сохранённому ответу нельзя
        # (main-prompt.md §2), тот же принцип, что и в
        # src/sources/swpc.py::fetch_and_store.
        return DonkiFetchOutcome("error_conflict", message, None, updated)

    updated = registry.record_success(config.source_id, at=fetched_at)
    message = (
        f"stored {report.stored_count} notification(s) issued "
        f"{start_day.isoformat()}..{end_day.isoformat()}"
    )
    return DonkiFetchOutcome("stored", message, report, updated)


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
