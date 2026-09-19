"""Provider-agnostic адаптер загрузки архивной космопогоды в хранилище (FN-42).

Закрывает разрыв, который ``src/sources/archive_probe.py`` оставлял явно: тот
модуль — **зонд**, доказательство пригодности архивов для строгого replay
(«Это доказательство доступности, не реализованный replay всего сервиса»,
его модульный докстринг и docs/method.md §5). Здесь — реальная загрузка уже
сохранённых архивных исходников в хранилище (``src/store``) через общий
контракт, который production orchestration потребляет **одинаково** для
любого продукта, не зная, какой именно архив за ним стоит.

**Почему адаптер provider-agnostic, а не «DONKI как ответ»** (main-prompt.md
§11, абзац о DONKI против NOAA SWPC Forecast Discussion). Вопрос о том,
какой продукт является допустимой единственной линией строгого прогноза из
прошлого — DONKI (событийные уведомления с ``messageIssueTime``) или
датированный численный продукт NOAA — **остаётся открытым у кейсодержателя**
и этой задачей не решается. Поэтому:

- продукт выбирается :class:`HistoricalArchiveStrategy` из ``sources.yaml``,
  а не условием в коде: ни в этом модуле, ни в ``src/domain/spaceweather``
  нет ветки вида ``if source_id == "nasa-donki-notifications"``, решающей,
  что считать правильным ответом;
- фактический горизонт прогноза продукта — значение конфигурации
  (``forecast_horizon_hours`` в ``sources.yaml``), а не константа в расчёте.
  Пока ответа эксперта нет, оно равно ``null`` для обоих архивных продуктов,
  и всё, что выходит за горизонт, становится ``beyond_horizon``/
  ``INSUFFICIENT_DATA`` — «не покрыто», а не «спокойно» (main-prompt.md §4).
  Никакие 6 или 24 часа здесь не зашиты (приёмка FN-42 п.5);
- этот модуль нигде не утверждает, что какой-либо один продукт «уже
  окончательно достаточен».

**Разделение слоёв (main-prompt.md §8).** Модуль нормализует и сохраняет,
не интерпретирует: он не решает, что считать событием, какой уровень S
присвоить и покрыто ли окно ВКД — это делает
``src/domain/spaceweather/archive_assessment.py`` по политике, полученной из
той же конфигурации. Здесь же остаётся то, что домен не вправе делать:
обращение к хранилищу и построение карты фактически загруженных интервалов.

**Ключевое различие «нет события» против «не можем сказать»** (main-prompt.md
§2, три состояния, а не два). Отсутствие записи само по себе не значит
ничего. Значение имеет только пара «интервал архива фактически загружен» +
«в нём нет записи о событии»: поэтому каждая загрузка возвращает
:class:`IngestedInterval` — какой именно интервал архива доказанно прочитан.
Без этого «мы не запрашивали эти сутки» было бы неотличимо от «в эти сутки
ничего не происходило».

Публичный контракт для production orchestration — docs/method.md §9.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from src.sources.archive_probe import (
    DONKI_SOURCE_ID,
    SWPC_ARCHIVE_SOURCE_ID,
    CoverageReport,
    build_coverage_report,
    donki_notification_to_record_input,
    parse_donki_notifications,
    parse_swpc_forecast_discussion,
    swpc_forecast_discussion_to_record_input,
)
from src.store.forecast_snapshot import seal_forecast_input_snapshot
from src.store.records import (
    DuplicateKeyConflictError,
    RawOriginalStore,
    RecordInput,
    get_record,
    insert_record,
    select_as_of,
)

UTC = timezone.utc

_DEFAULT_SOURCES_YAML = Path(__file__).resolve().parent.parent.parent / "sources.yaml"


class ArchiveConfigError(RuntimeError):
    """``sources.yaml`` не описывает продукт так, как требует этот адаптер.

    Отдельный отказ вместо значений по умолчанию: молчаливый дефолт для
    горизонта прогноза или для перечня событийных типов означал бы ровно то,
    что приёмка FN-42 п.5 запрещает — подставленное в коде число вместо
    ответа кейсодержателя (main-prompt.md §7 «пороги, горизонты… в конфиге,
    а не в коде»).
    """


# ---------------------------------------------------------------------------
# Конфигурация продукта и стратегия выбора (main-prompt.md §7)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArchiveProductConfig:
    """Один архивный продукт космопогоды, как он описан в ``sources.yaml``.

    ``forecast_horizon_hours`` — **фактический** горизонт продукта, на
    который распространяется его прогноз, в часах от момента публикации.
    ``None`` означает «горизонт продукта не установлен» — не «бесконечный» и
    не «шесть часов»: открытый вопрос к кейсодержателю (main-prompt.md §11),
    при котором любая точка за пределами собственного интервала действия
    записи честно становится ``beyond_horizon``. Значение приходит из
    реестра источников, поэтому ответ эксперта закрывается правкой
    ``sources.yaml``, без изменения расчётных функций.

    ``event_message_types`` — какие типы сообщений этого продукта вообще
    являются сообщением о событии рассматриваемого механизма. Тоже
    конфигурация, а не условие в домене: домен применяет перечень, но не
    знает, какие типы бывают у конкретного поставщика.
    """

    source_id: str
    record_kind: str
    forecast_horizon_hours: float | None
    forecast_horizon_note: str
    event_message_types: frozenset[str]
    enabled: bool

    def with_forecast_horizon_hours(self, hours: float | None) -> ArchiveProductConfig:
        """Возвращает копию с другим горизонтом — точка расширения для
        ответа кейсодержателя и для экспериментов стенда Т5.

        Существует ровно затем, чтобы горизонт оставался **данными**: стенд
        может прогнать один и тот же расчёт при нескольких горизонтах, не
        трогая ни одной расчётной функции (main-prompt.md §7).
        """
        if hours is not None and hours <= 0:
            raise ValueError("forecast_horizon_hours must be positive when it is known")
        return ArchiveProductConfig(
            source_id=self.source_id,
            record_kind=self.record_kind,
            forecast_horizon_hours=hours,
            forecast_horizon_note=self.forecast_horizon_note,
            event_message_types=self.event_message_types,
            enabled=self.enabled,
        )


@dataclass(frozen=True)
class HistoricalArchiveStrategy:
    """Какие архивные продукты и с каким горизонтом участвуют в строгом replay.

    Это и есть требуемый «выбор продукта и горизонта конфигурацией/
    стратегией, а не условием, зашитым в domain»: orchestration собирает
    стратегию один раз (обычно :meth:`from_sources_yaml`) и передаёт её
    дальше; ни выборка, ни оценка не знают названий конкретных продуктов.

    Несколько продуктов в стратегии — норма, а не исключение: постановка
    требует строгий прогноз из прошлого «хотя бы по одной линии», но не
    запрещает вести их параллельно, и выбор единственной допустимой линии
    этой задачей сознательно не делается (main-prompt.md §11).
    """

    products: tuple[ArchiveProductConfig, ...]

    def __post_init__(self) -> None:
        if not self.products:
            raise ValueError("strategy must select at least one archive product")
        seen = [p.source_id for p in self.products]
        if len(seen) != len(set(seen)):
            raise ValueError(f"duplicate source_id in strategy: {seen}")

    @property
    def source_ids(self) -> tuple[str, ...]:
        return tuple(p.source_id for p in self.products)

    def product_for(self, source_id: str) -> ArchiveProductConfig:
        for product in self.products:
            if product.source_id == source_id:
                return product
        raise KeyError(f"source_id {source_id!r} is not part of this strategy")

    @property
    def enabled_products(self) -> tuple[ArchiveProductConfig, ...]:
        """Продукты, не отключённые переключателем ``enabled`` реестра.

        Отключение источника (main-prompt.md §5, критерий Т6) обязано
        действовать и на исторический путь: отключённый продукт не
        загружается и не участвует в выборке. Последствие для оценки —
        «данных нет», а не «спокойно»: пустая выборка даёт
        ``INSUFFICIENT_DATA`` в ``archive_assessment``, не благоприятный
        результат.
        """
        return tuple(p for p in self.products if p.enabled)

    @classmethod
    def from_sources_yaml(
        cls, source_ids: Sequence[str], *, path: str | Path = _DEFAULT_SOURCES_YAML
    ) -> HistoricalArchiveStrategy:
        return cls(products=tuple(load_archive_product(sid, path=path) for sid in source_ids))


def load_archive_product(
    source_id: str, *, path: str | Path = _DEFAULT_SOURCES_YAML
) -> ArchiveProductConfig:
    """Читает описание архивного продукта из реестра источников.

    Читается заново при каждом вызове (не кешируется) — тот же принцип, что
    и у ``src/sources/noaa_3day_forecast.py::load_source_config``: реестр
    отдельным текстом расходится с кодом за сутки (main-prompt.md §7).

    Отсутствие ключа ``forecast_horizon_hours`` — :class:`ArchiveConfigError`,
    а не ``None`` по умолчанию: «горизонт не объявлен в реестре» и «реестр
    прямо говорит, что горизонт не установлен» — разные утверждения, и
    второе должно быть записано явно (main-prompt.md §2 о трёх состояниях,
    применённое к самой конфигурации).
    """
    doc = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    for entry in doc.get("space_weather") or []:
        if entry.get("id") != source_id:
            continue
        if "forecast_horizon_hours" not in entry:
            raise ArchiveConfigError(
                f"source {source_id!r} has no 'forecast_horizon_hours' key in {path} — "
                "the actual product horizon must be declared in the registry (even as "
                "null = not established), never defaulted in code (main-prompt.md §7)"
            )
        raw_horizon = entry["forecast_horizon_hours"]
        horizon = None if raw_horizon is None else float(raw_horizon)
        if horizon is not None and horizon <= 0:
            raise ArchiveConfigError(
                f"source {source_id!r} declares a non-positive forecast_horizon_hours"
            )
        raw_types = entry.get("event_message_types")
        if raw_types is None:
            raise ArchiveConfigError(
                f"source {source_id!r} has no 'event_message_types' key in {path} — "
                "which message types report an event is a property of the product, "
                "not of the domain code that applies them"
            )
        return ArchiveProductConfig(
            source_id=source_id,
            record_kind=str(entry["record_kind"]),
            forecast_horizon_hours=horizon,
            forecast_horizon_note=str(entry.get("forecast_horizon_note", "")),
            event_message_types=frozenset(str(t) for t in raw_types),
            enabled=bool(entry.get("enabled", True)),
        )
    raise ArchiveConfigError(f"source_id {source_id!r} is not registered in {path}")


# ---------------------------------------------------------------------------
# Результат загрузки
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IngestedInterval:
    """Интервал архива, который доказанно прочитан этой загрузкой.

    Не «интервал, в котором что-то нашлось», а «интервал, который мы
    действительно запросили и разобрали». Ровно это превращает отсутствие
    записей в утверждение «события не зафиксировано» вместо «оценить
    невозможно» (main-prompt.md §2) — см. модульный докстринг.

    ``fetched_at`` — когда этот интервал был реально прочитан (тот же момент,
    что передан в ``ingest_donki_notifications``/``ingest_swpc_forecast_discussion``).
    Прокидывается в доменный двойник ``archive_assessment.CoverageInterval``
    как факт со своим временем получения (round 3 ревью PR #37), а не
    анонимная пара границ.
    """

    source_id: str
    start: datetime
    end: datetime
    fetched_at: datetime

    def __post_init__(self) -> None:
        for name, value in (
            ("start", self.start),
            ("end", self.end),
            ("fetched_at", self.fetched_at),
        ):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{name} must be timezone-aware UTC (.ai/main-prompt.md §1)")
        if self.end <= self.start:
            raise ValueError("end must be after start")


@dataclass(frozen=True)
class ArchiveIngestReport:
    """Итог одной загрузки архивного ответа в хранилище.

    Каждая категория перечислена отдельно, а не свёрнута в «загружено N»:
    запись, не попавшая в строгий replay из-за неизвестного времени
    публикации, и запись, не созданная вовсе из-за неизвлекаемого времени
    события, — разные ситуации с разными последствиями, и обе обязаны быть
    видны вызывающей стороне (main-prompt.md §2, §3).
    """

    source_id: str
    interval: IngestedInterval
    stored_record_ids: tuple[str, ...]
    replay_eligible_record_ids: tuple[str, ...]
    #: Провайдерские id, для которых время события не извлекается — запись не
    #: создавалась вовсе (``archive_probe`` вернул ``None``), а не создавалась
    #: с подставленным временем.
    skipped_without_event_time: tuple[str, ...]
    #: ``messageType`` каждой пропущенной записи выше, тот же порядок и та же
    #: длина, что у ``skipped_without_event_time`` (round 3 ревью PR #37) —
    #: нужно, чтобы вызывающая сторона могла решить, пропущена ли запись
    #: РЕЛЕВАНТНОГО для конкретного механизма типа, не переизобретая парсинг.
    skipped_message_types: tuple[str, ...]
    #: Провайдерские id, сохранённые, но навсегда непригодные для строгого
    #: replay: ``published_at`` неизвестен (main-prompt.md §1).
    stored_not_replay_eligible: tuple[str, ...]
    #: ``messageType`` каждой непригодной записи выше, тот же порядок и та же
    #: длина, что у ``stored_not_replay_eligible`` (FN-41) — нужно строгой
    #: ветке ``historical_forecast``: такая запись НЕ попадает в
    #: ``select_as_of``, поэтому её отсутствие в строгой выборке нельзя
    #: читать как «события не было», если она РЕЛЕВАНТНОГО типа
    #: (:meth:`has_unprovable_publication_of`).
    stored_not_replay_eligible_message_types: tuple[str, ...]
    #: Сообщения о конфликте дедуп-ключа с другим содержимым — не «дубликат».
    conflicts: tuple[str, ...]
    #: ``messageType`` записи, вызвавшей каждый конфликт выше — тот же
    #: порядок и та же длина, что у ``conflicts`` (round 3 ревью PR #37).
    conflict_message_types: tuple[str, ...]
    #: Дни публикаций внутри интервала — сырьё для карты наличия/пробелов.
    publication_days: tuple[date, ...]

    @property
    def stored_count(self) -> int:
        return len(self.stored_record_ids)

    def has_unresolved_notifications_of(self, relevant_message_types: frozenset[str]) -> bool:
        """True, если РЕЛЕВАНТНОЕ (для оцениваемого механизма) уведомление в
        этом ответе не нормализовалось (``skipped_message_types``) либо
        конфликтовало по дедуп-ключу (``conflict_message_types``).

        Round 3 ревью PR #37: ошибка нормализации именно релевантного
        уведомления не должна тихо превращаться в «интервал полностью
        прочитан и чист» — см. :func:`merged_ingested_intervals`. Нерелевантный
        сбой (например нераспознанный ``FLR`` без ``Activity ID`` — реальные
        ~18% архива, docs/method.md §6) не портит доверие к интервалу для
        механизма, который эти типы не использует.
        """
        return bool(
            (set(self.skipped_message_types) | set(self.conflict_message_types))
            & relevant_message_types
        )

    def has_unprovable_publication_of(self, relevant_message_types: frozenset[str]) -> bool:
        """True, если в этом ответе есть РЕЛЕВАНТНОЕ уведомление, сохранённое
        без доказуемого времени публикации (``published_at is None``).

        FN-41. Такая запись существует в хранилище, но ``select_as_of`` её
        никогда не вернёт (``replay_eligible = false``, main-prompt.md §1) —
        поэтому для СТРОГОЙ ветки ``historical_forecast`` её невидимость
        означает «подтвердить нечем», а не «события не было». Реальный
        случай — ``20240516-7D-001``, у которого два независимых указания
        времени выпуска расходятся (docs/method.md §4).

        Отдельный предикат от :meth:`has_unresolved_notifications_of`, а не
        добавленный в него флаг: для ``historical_analysis`` такая запись
        полностью пригодна (она выбирается по времени события, а не по
        публикации), и портить ей доверие к интервалу было бы неверно
        (main-prompt.md §1 — две ветки, не один флаг).
        """
        return bool(set(self.stored_not_replay_eligible_message_types) & relevant_message_types)


def _insert_all(
    conn: sqlite3.Connection,
    raw_store: RawOriginalStore,
    record_inputs: Iterable[RecordInput],
) -> tuple[list[tuple[str, RecordInput]], list[tuple[str, str]], list[tuple[str, str]]]:
    """Вставляет записи, разделяя пригодные/непригодные/конфликтные.

    Возвращает пары ``(record_id, RecordInput)``, а не два независимых
    списка: при конфликте дедуп-ключа запись пропускается, и позиционное
    сопоставление «i-й id — к i-й записи» разъехалось бы именно на том
    входе, где это труднее всего заметить.

    Повторная вставка того же ``(source_id, provider_record_id,
    source_version)`` идемпотентна на уровне ``insert_record`` и возвращает
    тот же ``record_id`` — дубликат не создаёт второго воздействия
    (main-prompt.md §2). Поздняя версия того же продукта приходит с другим
    ``source_version`` и поэтому ложится **рядом**, не поверх.

    Конфликты возвращаются парами ``(сообщение, message_type)`` — тип берётся
    из ``spatial_context`` записи, которая конфликтовала (доступна в момент
    перехвата исключения, до того как запись потеряна), чтобы вызывающая
    сторона могла отличить конфликт релевантного типа от нерелевантного
    (round 3 ревью PR #37, :meth:`ArchiveIngestReport.has_unresolved_notifications_of`).
    """
    stored: list[tuple[str, RecordInput]] = []
    not_eligible: list[tuple[str, str]] = []
    conflicts: list[tuple[str, str]] = []
    for record_input in record_inputs:
        message_type = str(record_input.spatial_context.get("message_type", ""))
        try:
            record_id = insert_record(conn, raw_store, record_input)
        except DuplicateKeyConflictError as exc:
            conflicts.append((str(exc), message_type))
            continue
        except sqlite3.IntegrityError:
            # Гонка двух конкурентных исторических расчётов за одну и ту же
            # запись архива: insert_record делает SELECT-затем-INSERT без
            # транзакционной защиты, поэтому проигравший получает UNIQUE
            # constraint failed вместо идемпотентного возврата id. Повтор
            # застаёт уже закоммиченную строку — содержимое то же самое (тот
            # же messageID того же архива), это не конфликт версии. Тот же
            # приём, что и в src/api/service.py::fetch_and_store_orbit (FN-41).
            record_id = insert_record(conn, raw_store, record_input)
        stored.append((record_id, record_input))
        if record_input.published_at is None:
            not_eligible.append((record_input.provider_record_id, message_type))
    return stored, not_eligible, conflicts


def ingest_donki_notifications(
    conn: sqlite3.Connection,
    raw_store: RawOriginalStore,
    raw_bytes: bytes,
    *,
    source_url: str,
    fetched_at: datetime,
    interval_start: datetime,
    interval_end: datetime,
) -> ArchiveIngestReport:
    """Загружает сохранённый ответ ``DONKI/notifications`` в хранилище.

    ``interval_start``/``interval_end`` — окно запроса к архиву (для DONKI
    это ``startDate``/``endDate``), а не диапазон найденных уведомлений:
    именно запрошенный интервал доказывает, что «пусто» означает «событий не
    зафиксировано», а не «мы туда не смотрели».

    Детерминированность (приёмка FN-42 п.1): каждая запись несёт
    ``raw_ref``/``checksum`` (вычисляются ``insert_record`` по
    каноническому представлению конкретного уведомления),
    ``source_version``, ``published_at`` и интервал действия — всё из
    ``archive_probe``, без единого значения, придуманного здесь. Повторный
    прогон на том же файле возвращает те же ``record_id``.
    """
    notifications = parse_donki_notifications(raw_bytes)
    interval = IngestedInterval(
        source_id=DONKI_SOURCE_ID,
        start=interval_start,
        end=interval_end,
        fetched_at=fetched_at,
    )

    record_inputs: list[RecordInput] = []
    skipped: list[str] = []
    skipped_types: list[str] = []
    publication_days: list[date] = []
    for notification in notifications:
        record_input = donki_notification_to_record_input(
            notification, source_url=source_url, fetched_at=fetched_at
        )
        if record_input is None:
            # Время события неизвлекаемо — запись не создаётся вовсе, а не
            # создаётся с временем публикации вместо времени события
            # (archive_probe, round 1 ревью PR #18).
            skipped.append(notification.message_id)
            skipped_types.append(notification.message_type)
            continue
        record_inputs.append(record_input)
        if record_input.published_at is not None:
            publication_days.append(record_input.published_at.astimezone(UTC).date())

    stored, not_eligible, conflict_pairs = _insert_all(conn, raw_store, record_inputs)
    eligible = [
        record_id
        for record_id, record_input in stored
        if record_input.published_at is not None
    ]
    return ArchiveIngestReport(
        source_id=DONKI_SOURCE_ID,
        interval=interval,
        stored_record_ids=tuple(record_id for record_id, _ in stored),
        replay_eligible_record_ids=tuple(eligible),
        skipped_without_event_time=tuple(skipped),
        skipped_message_types=tuple(skipped_types),
        stored_not_replay_eligible=tuple(pid for pid, _ in not_eligible),
        stored_not_replay_eligible_message_types=tuple(mt for _, mt in not_eligible),
        conflicts=tuple(c for c, _ in conflict_pairs),
        conflict_message_types=tuple(t for _, t in conflict_pairs),
        publication_days=tuple(publication_days),
    )


def ingest_swpc_forecast_discussion(
    conn: sqlite3.Connection,
    raw_store: RawOriginalStore,
    raw_bytes: bytes,
    *,
    source_url: str,
    fetched_at: datetime,
    interval_start: datetime,
    interval_end: datetime,
) -> ArchiveIngestReport:
    """Загружает один сохранённый выпуск NOAA SWPC Forecast Discussion.

    Тот же публичный контракт, что и у :func:`ingest_donki_notifications` —
    в этом и смысл адаптера: orchestration вызывает обе функции одинаково и
    складывает их отчёты в одну карту покрытия, не разбираясь, что один
    продукт событийный, а другой периодический.

    Интервал здесь — слот архива, который был запрошен (например сутки
    каталога NCEI), а не придуманный интервал действия бюллетеня: сама
    запись по-прежнему честно утверждает только момент выпуска
    (``archive_probe.swpc_forecast_discussion_to_record_input``).
    """
    discussion = parse_swpc_forecast_discussion(raw_bytes)
    interval = IngestedInterval(
        source_id=SWPC_ARCHIVE_SOURCE_ID,
        start=interval_start,
        end=interval_end,
        fetched_at=fetched_at,
    )
    record_input = swpc_forecast_discussion_to_record_input(
        discussion, source_url=source_url, fetched_at=fetched_at
    )
    stored, not_eligible, conflict_pairs = _insert_all(conn, raw_store, [record_input])
    stored_ids = tuple(record_id for record_id, _ in stored)
    eligible = stored_ids if record_input.published_at is not None else ()
    return ArchiveIngestReport(
        source_id=SWPC_ARCHIVE_SOURCE_ID,
        interval=interval,
        stored_record_ids=stored_ids,
        replay_eligible_record_ids=eligible,
        skipped_without_event_time=(),
        skipped_message_types=(),
        stored_not_replay_eligible=tuple(pid for pid, _ in not_eligible),
        stored_not_replay_eligible_message_types=tuple(mt for _, mt in not_eligible),
        conflicts=tuple(c for c, _ in conflict_pairs),
        conflict_message_types=tuple(t for _, t in conflict_pairs),
        publication_days=(discussion.issued_at.astimezone(UTC).date(),),
    )


# ---------------------------------------------------------------------------
# Карта покрытия и выборка
# ---------------------------------------------------------------------------


def coverage_report_for(
    reports: Iterable[ArchiveIngestReport], *, window_start: date, window_end: date
) -> CoverageReport:
    """Строит явную карту наличия/пробелов по дням для набора загрузок.

    Переиспользует ``archive_probe.build_coverage_report`` без изменения
    правила: этот модуль лишь собирает дни публикаций из отчётов загрузки.
    Карта отвечает на вопрос «в какие дни продукт вообще публиковался», и
    её нулевой день сам по себе не означает «спокойно» — что именно означает
    ноль, решает ``archive_assessment`` вместе с картой фактически
    загруженных интервалов (см. модульный докстринг).
    """
    days: list[date] = []
    for report in reports:
        days.extend(report.publication_days)
    return build_coverage_report(days, window_start=window_start, window_end=window_end)


def merged_ingested_intervals(
    reports: Iterable[ArchiveIngestReport],
    *,
    source_id: str,
    relevant_message_types: frozenset[str],
) -> tuple[IngestedInterval, ...]:
    """Склеивает пересекающиеся/смежные загруженные интервалы одного продукта.

    Нужна, потому что архив загружается окнами (DONKI — по 15 суток,
    main-prompt.md §6 «стенд ходит в архив пакетно»), и окно ВКД может
    приходиться на стык двух окон загрузки. Без склейки стык выглядел бы
    пробелом покрытия, то есть ложным ``INSUFFICIENT_DATA`` — ошибка в
    безопасную сторону, но всё же ошибка.

    ``relevant_message_types`` — обычно ``policy.event_message_types`` того
    же продукта. Отчёт о загрузке, в котором есть непронормализованное
    (``skipped_message_types``) или конфликтное (``conflict_message_types``)
    уведомление хотя бы одного из этих типов, **не** участвует в склейке —
    его интервал целиком выбывает из «доказанно прочитанного» покрытия
    (round 3 ревью PR #37: `archive_ingest.py`, было — интервал объявлялся
    покрытым независимо от таких сбоев, и ошибка нормализации релевантного
    уведомления могла тихо превратиться в ``NO_EVENT_DETECTED``). Сбой
    нерелевантного типа (например нераспознанный ``FLR`` без ``Activity ID``
    — реальные ~18% архива, docs/method.md §6) интервал не портит: механизм,
    не использующий этот тип, не теряет доверие к покрытию из-за него.

    Параметр обязателен, а не по умолчанию пуст: пустое множество молча
    отключило бы всю эту проверку для любого вызова, который забыл его
    передать (main-prompt.md §7 — не молчаливый дефолт для того, что решает
    корректность вывода).

    **Не закреплена по времени загрузки — не использовать напрямую для сборки
    входа ``historical_forecast``** (round 6/7 ревью PR #37). Эта функция
    честно склеивает то, что ей передали, СЕЙЧАС — без всякой памяти о том,
    что было передано при прошлом вызове для того же расчёта. Для строгого
    прогноза из прошлого нужен :func:`assemble_historical_forecast_input`,
    который закрепляет её результат ВМЕСТЕ с выбранными записями одним
    снимком (``src/store/forecast_snapshot.py::seal_forecast_input_snapshot``)
    за конкретным расчётом и не даёт последующей догрузке архива или новым
    подходящим записям задним числом изменить уже вычисленный вход. Прямые
    вызовы этой функции в тестах ниже намеренно проверяют склейку саму по
    себе (стык окон загрузки, исключение непронормализованных интервалов) —
    это не то же самое, что сборка входа расчёта.
    """
    intervals = sorted(
        (
            r.interval
            for r in reports
            if r.source_id == source_id
            and not r.has_unresolved_notifications_of(relevant_message_types)
        ),
        key=lambda i: i.start,
    )
    merged: list[IngestedInterval] = []
    for interval in intervals:
        if merged and interval.start <= merged[-1].end:
            previous = merged[-1]
            if interval.end > previous.end:
                merged[-1] = IngestedInterval(
                    source_id=source_id,
                    start=previous.start,
                    end=interval.end,
                    fetched_at=max(previous.fetched_at, interval.fetched_at),
                )
            elif interval.fetched_at > previous.fetched_at:
                merged[-1] = IngestedInterval(
                    source_id=source_id,
                    start=previous.start,
                    end=previous.end,
                    fetched_at=interval.fetched_at,
                )
            continue
        merged.append(interval)
    return tuple(merged)


def strict_replay_intervals(
    reports: Iterable[ArchiveIngestReport],
    *,
    source_id: str,
    relevant_message_types: frozenset[str],
) -> tuple[IngestedInterval, ...]:
    """Покрытие для СТРОГОЙ ветки ``historical_forecast`` (FN-41).

    То же, что :func:`merged_ingested_intervals`, плюс одно дополнительное
    исключение: отчёт, в котором есть релевантное уведомление, сохранённое
    БЕЗ доказуемого времени публикации
    (:meth:`ArchiveIngestReport.has_unprovable_publication_of`), не участвует
    в склейке. Такая запись не попадает в ``select_as_of``, поэтому для
    строгого прогноза её невидимость означает «подтвердить нечем», а объявить
    интервал полностью прочитанным значило бы превратить неизвестную
    публикацию в вывод «события не было» (main-prompt.md §1, §2).

    Отдельная функция, а не параметр-флаг у :func:`merged_ingested_intervals`:
    для ``historical_analysis`` это исключение неверно (там записи выбираются
    по времени события, а не по публикации), и «флаг рано или поздно окажется
    не в том значении» (main-prompt.md §1).
    """
    return merged_ingested_intervals(
        (
            report
            for report in reports
            if not report.has_unprovable_publication_of(relevant_message_types)
        ),
        source_id=source_id,
        relevant_message_types=relevant_message_types,
    )


def select_forecast_inputs(
    conn: sqlite3.Connection,
    *,
    as_of: datetime,
    strategy: HistoricalArchiveStrategy,
) -> dict[str, list[dict[str, Any]]]:
    """Вход строгого прогноза из прошлого: по продукту стратегии — записи,
    пригодные к моменту ``as_of``.

    **Переиспользует** ``src/store/records.py::select_as_of`` и не
    переизобретает его правило (``published_at <= as_of`` и
    ``replay_eligible = true``, уже покрыто ``tests/store/test_as_of.py``):
    здесь добавлен ровно один слой — перебор продуктов, выбранных
    конфигурацией. Отключённый в реестре продукт не выбирается вовсе.

    Только записи — без карты покрытия, и без закрепления снимком: КАЖДЫЙ
    вызов честно выбирает записи заново по текущему состоянию хранилища.
    Для входа конкретного, воспроизводимого расчёта ``historical_forecast``
    (записи **и** покрытие, закреплённые ВМЕСТЕ одним снимком) используйте
    :func:`assemble_historical_forecast_input` — раздельный вызов этой
    функции и отдельная сборка покрытия рядом друг с другом может незаметно
    разойтись по времени между двумя вызовами одного и того же расчёта
    (round 7 ревью PR #37, finding 2).

    Симметричная функция для апостериорной проверки прогноза — **другой
    модуль**: ``src/store/verification.py::select_verification_records``
    (main-prompt.md §1: проверка и вход прогноза не могут быть одной
    функцией с булевым флагом). Ни одного параметра, переключающего эту
    функцию в режим проверки, здесь нет и быть не должно.
    """
    return {
        product.source_id: select_as_of(conn, as_of, source_id=product.source_id)
        for product in strategy.enabled_products
    }


@dataclass(frozen=True)
class HistoricalForecastInput:
    """Полный, неизменяемый вход строгого ``historical_forecast`` для одного
    продукта: записи, пригодные к ``as_of``, и карта покрытия, из которой
    они извлечены — оба закреплены ОДНИМ снимком (round 7 ревью PR #37,
    finding 2: не двумя раздельными закреплениями, которые могли бы разойтись
    между собой во времени).

    ``snapshot_id`` — собственный, сервером сгенерированный идентификатор
    именно этого снимка (round 7 ревью PR #37, finding 3, ⚠️) — не совпадает
    и не обязан совпадать с ``computation_id``, который его породил; годится
    для сохранения в ``data_manifest`` результата, чтобы восстановить
    использованный вход из выгрузки независимо от того, что впоследствии
    станет с ``computation_id``.
    """

    source_id: str
    snapshot_id: str
    records: tuple[dict[str, Any], ...]
    ingested_intervals: tuple[IngestedInterval, ...]


def assemble_historical_forecast_input(
    conn: sqlite3.Connection,
    reports: Iterable[ArchiveIngestReport],
    *,
    computation_id: str,
    as_of: datetime,
    strategy: HistoricalArchiveStrategy,
) -> dict[str, HistoricalForecastInput]:
    """Единственная задокументированная точка сборки входа
    ``historical_forecast`` для всех продуктов стратегии сразу — round
    6/7 ревью PR #37. Для каждого включённого продукта:

    1. вычисляет ТЕКУЩИЕ (не обязательно окончательные) кандидаты — записи
       через :func:`select_forecast_inputs` и карту покрытия через
       :func:`merged_ingested_intervals` (с ``event_message_types`` ИМЕННО
       этого продукта);
    2. закрепляет их ОБА ВМЕСТЕ одним вызовом
       ``src/store/forecast_snapshot.py::seal_forecast_input_snapshot`` —
       первый вызов для (``computation_id``, ``source_id``, ``as_of``)
       сохраняет эти кандидаты дословно под новым ``snapshot_id`` и
       возвращает их; любой последующий вызов с тем же ключом читает уже
       сохранённое содержимое обратно и полностью игнорирует новые
       кандидаты — даже если с тех пор в хранилище появились записи,
       пригодные к тому же ``as_of`` (round 7 ревью PR #37, finding 2: это
       раньше было отдельным, незакреплённым источником расхождения —
       только карта покрытия закреплялась, набор ЗАПИСЕЙ выбирался заново
       при каждом вызове), или покрытие честно выросло (round 4/6 ревью);
    3. при необходимости перечитывает полные записи закреплённого снимка
       через ``get_record`` (когда снимок был закреплён РАНЬШЕ этого
       вызова — свежевыбранные кандидаты в этом случае не совпадают с тем,
       что сохранено, и не используются).

    ``computation_id`` — обязательный идентификатор КОНКРЕТНОГО расчёта
    (будущий ``result_id`` production orchestration, либо любой другой
    идентификатор, который вызывающая сторона генерирует заново для каждого
    независимого расчёта — main-prompt.md §3, «Пересчёт создаёт новый
    result_id»; round 6 ревью PR #37, finding 3). Повторный вызов с тем же
    ``computation_id`` — идемпотентный повтор/ретрай того же расчёта и
    обязан вернуть тот же результат, включая тот же ``snapshot_id``.

    Результат — по ``source_id``: готовый :class:`HistoricalForecastInput`.
    Преобразование ``IngestedInterval`` (``sources/``) в ``CoverageInterval``
    (``domain/``) остаётся на стороне вызывающего (домен не импортирует
    ``sources/``, main-prompt.md §8) — см. docs/method.md §9.1, пример.
    """
    selected = select_forecast_inputs(conn, as_of=as_of, strategy=strategy)
    result: dict[str, HistoricalForecastInput] = {}
    for product in strategy.enabled_products:
        candidate_records = selected[product.source_id]
        candidate_intervals = strict_replay_intervals(
            reports,
            source_id=product.source_id,
            relevant_message_types=product.event_message_types,
        )
        sealed = seal_forecast_input_snapshot(
            conn,
            computation_id=computation_id,
            source_id=product.source_id,
            as_of=as_of,
            candidate_record_ids=tuple(r["record_id"] for r in candidate_records),
            candidate_intervals=tuple(
                (i.start, i.end, i.fetched_at) for i in candidate_intervals
            ),
        )
        if sealed.record_ids == tuple(r["record_id"] for r in candidate_records):
            records = tuple(candidate_records)
        else:
            records = tuple(_record_or_raise(conn, record_id) for record_id in sealed.record_ids)
        result[product.source_id] = HistoricalForecastInput(
            source_id=product.source_id,
            snapshot_id=sealed.snapshot_id,
            records=records,
            ingested_intervals=tuple(
                IngestedInterval(
                    source_id=product.source_id, start=start, end=end, fetched_at=fetched_at
                )
                for start, end, fetched_at in sealed.intervals
            ),
        )
    return result


def _record_or_raise(conn: sqlite3.Connection, record_id: str) -> dict[str, Any]:
    record = get_record(conn, record_id)
    assert record is not None, (
        f"record_id {record_id!r} was sealed into a snapshot but is no longer in the "
        "store — the store never deletes rows, so this indicates a bug, not a valid state"
    )
    return record


__all__ = [
    "ArchiveConfigError",
    "ArchiveIngestReport",
    "ArchiveProductConfig",
    "HistoricalArchiveStrategy",
    "HistoricalForecastInput",
    "IngestedInterval",
    "assemble_historical_forecast_input",
    "coverage_report_for",
    "ingest_donki_notifications",
    "ingest_swpc_forecast_discussion",
    "load_archive_product",
    "merged_ingested_intervals",
    "select_forecast_inputs",
    "strict_replay_intervals",
]
