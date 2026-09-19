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
from src.store.records import (
    DuplicateKeyConflictError,
    RawOriginalStore,
    RecordInput,
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
    """

    source_id: str
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        for name, value in (("start", self.start), ("end", self.end)):
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
    #: Провайдерские id, сохранённые, но навсегда непригодные для строгого
    #: replay: ``published_at`` неизвестен (main-prompt.md §1).
    stored_not_replay_eligible: tuple[str, ...]
    #: Сообщения о конфликте дедуп-ключа с другим содержимым — не «дубликат».
    conflicts: tuple[str, ...]
    #: Дни публикаций внутри интервала — сырьё для карты наличия/пробелов.
    publication_days: tuple[date, ...]

    @property
    def stored_count(self) -> int:
        return len(self.stored_record_ids)


def _insert_all(
    conn: sqlite3.Connection,
    raw_store: RawOriginalStore,
    record_inputs: Iterable[RecordInput],
) -> tuple[list[tuple[str, RecordInput]], list[str], list[str]]:
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
    """
    stored: list[tuple[str, RecordInput]] = []
    not_eligible: list[str] = []
    conflicts: list[str] = []
    for record_input in record_inputs:
        try:
            record_id = insert_record(conn, raw_store, record_input)
        except DuplicateKeyConflictError as exc:
            conflicts.append(str(exc))
            continue
        stored.append((record_id, record_input))
        if record_input.published_at is None:
            not_eligible.append(record_input.provider_record_id)
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
        source_id=DONKI_SOURCE_ID, start=interval_start, end=interval_end
    )

    record_inputs: list[RecordInput] = []
    skipped: list[str] = []
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
            continue
        record_inputs.append(record_input)
        if record_input.published_at is not None:
            publication_days.append(record_input.published_at.astimezone(UTC).date())

    stored, not_eligible, conflicts = _insert_all(conn, raw_store, record_inputs)
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
        stored_not_replay_eligible=tuple(not_eligible),
        conflicts=tuple(conflicts),
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
        source_id=SWPC_ARCHIVE_SOURCE_ID, start=interval_start, end=interval_end
    )
    record_input = swpc_forecast_discussion_to_record_input(
        discussion, source_url=source_url, fetched_at=fetched_at
    )
    stored, not_eligible, conflicts = _insert_all(conn, raw_store, [record_input])
    stored_ids = tuple(record_id for record_id, _ in stored)
    eligible = stored_ids if record_input.published_at is not None else ()
    return ArchiveIngestReport(
        source_id=SWPC_ARCHIVE_SOURCE_ID,
        interval=interval,
        stored_record_ids=stored_ids,
        replay_eligible_record_ids=eligible,
        skipped_without_event_time=(),
        stored_not_replay_eligible=tuple(not_eligible),
        conflicts=tuple(conflicts),
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
    reports: Iterable[ArchiveIngestReport], *, source_id: str
) -> tuple[IngestedInterval, ...]:
    """Склеивает пересекающиеся/смежные загруженные интервалы одного продукта.

    Нужна, потому что архив загружается окнами (DONKI — по 15 суток,
    main-prompt.md §6 «стенд ходит в архив пакетно»), и окно ВКД может
    приходиться на стык двух окон загрузки. Без склейки стык выглядел бы
    пробелом покрытия, то есть ложным ``INSUFFICIENT_DATA`` — ошибка в
    безопасную сторону, но всё же ошибка.
    """
    intervals = sorted(
        (r.interval for r in reports if r.source_id == source_id),
        key=lambda i: i.start,
    )
    merged: list[IngestedInterval] = []
    for interval in intervals:
        if merged and interval.start <= merged[-1].end:
            previous = merged[-1]
            if interval.end > previous.end:
                merged[-1] = IngestedInterval(
                    source_id=source_id, start=previous.start, end=interval.end
                )
            continue
        merged.append(interval)
    return tuple(merged)


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


__all__ = [
    "ArchiveConfigError",
    "ArchiveIngestReport",
    "ArchiveProductConfig",
    "HistoricalArchiveStrategy",
    "IngestedInterval",
    "coverage_report_for",
    "ingest_donki_notifications",
    "ingest_swpc_forecast_discussion",
    "load_archive_product",
    "merged_ingested_intervals",
    "select_forecast_inputs",
]
