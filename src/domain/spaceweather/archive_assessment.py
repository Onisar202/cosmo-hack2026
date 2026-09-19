"""Механизм 1: три состояния архивной оценки окна ВКД (FN-42).

Отвечает на вопрос, который нельзя выразить одним ``bool`` и одним уровнем
(.ai/main-prompt.md §2 — «различаются три состояния, а не два»):

``EVENT_PRESENT``
    В фактически загруженном интервале архива есть сообщение о событии,
    пересекающее окно ВКД.

``NO_EVENT_DETECTED``
    Интервал архива, покрывающий окно, **доказанно прочитан**, продукт умеет
    сообщать о событиях этого механизма, и сообщения о событии в нём нет.
    Это утверждение «порог не пересечён в прочитанном интервале», а **не**
    «обстановка подтверждена спокойной»: непрерывного подтверждения
    состояния событийный архив не даёт (docs/method.md §6 п.5,
    ``archive_probe.build_coverage_report``).

``INSUFFICIENT_DATA``
    Оценить невозможно: окно не покрыто загруженными интервалами, либо
    выходит за фактический горизонт продукта, либо продукт вовсе не умеет
    машиночитаемо сообщать о событии, либо в прочитанном интервале есть
    подтверждённое **начало** явления без задокументированного конца
    (``open_ended`` — round 3 ревью PR #37, см. ``assess_archive_window``),
    чья истинная продолжительность на момент окна неизвестна. Отказ, пробел
    и «за горизонтом» никогда не превращаются в благоприятную оценку
    (main-prompt.md §2, §4).

**Продукт и горизонт — конфигурация, а не условие в домене.** В этом модуле
нет ни одной ветки вида ``if source_id == "..."``: и перечень событийных
типов сообщений, и фактический горизонт прогноза приходят в
:class:`ArchiveProductPolicy` из реестра источников
(``src/sources/archive_ingest.py::load_archive_product`` → ``sources.yaml``).
Вопрос «DONKI или датированный численный продукт NOAA» остаётся открытым у
кейсодержателя (main-prompt.md §11), и этот модуль его не решает: он
одинаково обслуживает любой продукт, описанный политикой.

**Горизонт нигде не зашит числом.** Пока ``forecast_horizon_hours``
продукта не установлен (``null`` в реестре), вперёд от ``as_of`` не
засчитывается никакое покрытие: часть окна после отсечения становится
``beyond_horizon`` и даёт ``INSUFFICIENT_DATA``. Ни 6, ни 24 часов в коде
нет (приёмка FN-42 п.5) — ответ эксперта закрывается правкой ``sources.yaml``.

Слой соблюдает main-prompt.md §8: модуль не ходит в сеть и не обращается к
хранилищу — принимает уже выбранные записи (форма
``contracts/record.schema.json``, как их отдаёт ``select_as_of``) и карту
фактически загруженных интервалов.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Protocol

from src.domain.spaceweather.source_agreement import SourceAssessment

UTC = timezone.utc

#: Три состояния оценки — ровно те, что перечислены в постановке FN-42.
AssessmentStatus = Literal["EVENT_PRESENT", "NO_EVENT_DETECTED", "INSUFFICIENT_DATA"]


class UnsupportedRecordError(ValueError):
    """Запись не принадлежит продукту, описанному политикой, либо не имеет
    времени публикации.

    Та же роль, что у одноимённых ошибок в ``external_forecast.py`` и
    ``observed_classifier.py``: вторая граница после фильтра выборки, чтобы
    ошибка выше по пайплайну не обернулась тихой интерпретацией чужой записи
    (main-prompt.md §4).
    """


class TemporalLeakError(ValueError):
    """Во вход строгого прогноза попала запись, опубликованная после ``as_of``.

    Структурный предохранитель, а не проверка на всякий случай
    (main-prompt.md §1 — временная честность как высший приоритет проекта).
    ``select_as_of`` такие записи не возвращает; если запись всё же пришла,
    значит вызывающая сторона собрала вход мимо строгой выборки — тихо
    отбросить её означало бы скрыть уже случившуюся ошибку пайплайна, а
    учесть — реализовать ровно ту утечку, против которой написан §1.
    """


class _ProductConfigLike(Protocol):
    """Структурный вид конфигурации продукта, достаточный для политики.

    Протокол, а не импорт ``src.sources.archive_ingest.ArchiveProductConfig``:
    ``domain/`` не зависит от ``sources/`` (main-prompt.md §8) — тот же
    приём, которым ``external_forecast.py`` дублирует строку ``source_id``
    вместо импорта коннектора.
    """

    @property
    def source_id(self) -> str: ...

    @property
    def forecast_horizon_hours(self) -> float | None: ...

    @property
    def forecast_horizon_note(self) -> str: ...

    @property
    def event_message_types(self) -> frozenset[str]: ...


@dataclass(frozen=True)
class ArchiveProductPolicy:
    """Что домену разрешено утверждать об этом архивном продукте.

    ``forecast_horizon_hours = None`` — «горизонт продукта не установлен»
    (открытый вопрос к кейсодержателю), а не «бесконечный»: см. модульный
    докстринг.

    Пустой ``event_message_types`` — продукт не даёт машиночитаемого
    признака события (реальный случай: NOAA SWPC Forecast Discussion —
    свободный текст, из которого событие извлекается только разбором прозы,
    docs/method.md §6 п.6). Такой продукт структурно не может дать
    ``NO_EVENT_DETECTED``: «в тексте, который мы не разбираем, ничего не
    нашлось» — это «оценить невозможно», а не доказанное отсутствие события.
    """

    source_id: str
    forecast_horizon_hours: float | None
    forecast_horizon_note: str = ""
    event_message_types: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if self.forecast_horizon_hours is not None and self.forecast_horizon_hours <= 0:
            raise ValueError("forecast_horizon_hours must be positive when it is known")

    @classmethod
    def from_config(cls, config: _ProductConfigLike) -> ArchiveProductPolicy:
        """Строит политику из записи реестра источников.

        Единственный переход «конфигурация → домен»; расчётные функции ниже
        читают только политику и потому не могут зависеть от того, какой
        продукт за ней стоит.
        """
        return cls(
            source_id=config.source_id,
            forecast_horizon_hours=config.forecast_horizon_hours,
            forecast_horizon_note=config.forecast_horizon_note,
            event_message_types=config.event_message_types,
        )


@dataclass(frozen=True)
class CoverageInterval:
    """Интервал архива, доказанно прочитанный загрузкой.

    Доменный двойник ``src/sources/archive_ingest.py::IngestedInterval``
    (домен не импортирует ``sources/``). Смысл тот же и он критичен:
    отсутствие записей значит «события не зафиксировано» только внутри
    интервала, который действительно был прочитан.

    ``fetched_at`` — когда именно этот интервал архива был реально прочитан
    (round 3 ревью PR #37: факт «мы прочитали этот интервал» — это тоже
    заявление источника со своим временем получения, а не голая пара
    границ без происхождения). Обязателен, а не опционален: у факта
    покрытия, как и у любой записи (main-prompt.md §1, §3), должно быть
    видно, когда он установлен — это то, что делает карту покрытия
    версионируемой, а не анонимной парой чисел. Используется в
    объяснениях (``notes``) для прослеживаемости (критерий О4); не
    участвует в отборе по ``as_of`` — покрытие устанавливается
    исследовательским конвейером сегодня для периода в прошлом, и это не
    временная утечка (утечкой было бы использование записи с
    ``published_at > as_of``, не факта о том, когда МЫ прочитали архив).
    """

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
class ArchiveEvent:
    """Одно сообщение о событии, пересекающее окно ВКД, либо (в
    ``ArchiveWindowAssessment.unresolved_open_events``) начало явления без
    задокументированного конца, чья продолжительность на момент окна
    неизвестна (round 3 ревью PR #37)."""

    record_id: str
    provider_record_id: str
    message_type: str
    valid_from: datetime
    valid_to: datetime
    published_at: datetime


@dataclass(frozen=True)
class ArchiveWindowAssessment:
    """Оценка одного окна ВКД по одному архивному продукту."""

    status: AssessmentStatus
    source_id: str
    window_start: datetime
    window_end: datetime
    as_of: datetime
    #: Момент, до которого продукт вообще что-либо утверждает о будущем
    #: относительно ``as_of``; ``None`` — горизонт продукта не установлен.
    horizon_end: datetime | None
    beyond_horizon: bool
    critical_gap: bool
    coverage_fraction: float
    events: tuple[ArchiveEvent, ...]
    #: Начала явлений без задокументированного конца (``open_ended`` —
    #: ``src/sources/archive_probe.py``), чей момент строго предшествует
    #: окну, но чья истинная продолжительность на момент окна неизвестна —
    #: round 3 ревью PR #37. Непустой список сам по себе не даёт
    #: ``EVENT_PRESENT`` (мы не утверждаем, что явление ещё продолжалось),
    #: но и не даёт ``NO_EVENT_DETECTED`` (мы не утверждаем, что оно уже
    #: закончилось) — только ``INSUFFICIENT_DATA``.
    unresolved_open_events: tuple[ArchiveEvent, ...]
    record_ids: tuple[str, ...]
    notes: tuple[str, ...]


def _parse(value: Any, field_name: str, record_id: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise UnsupportedRecordError(
            f"record {record_id!r} has a naive {field_name} (.ai/main-prompt.md §1)"
        )
    return parsed.astimezone(UTC)


def _merge(intervals: Iterable[CoverageInterval]) -> list[CoverageInterval]:
    """Склеивает пересекающиеся/смежные интервалы покрытия одного продукта.

    ``fetched_at`` склеенного интервала — более поздний из двух: покрытие
    объединённого диапазона доказано только с того момента, когда была
    прочитана ВТОРАЯ (более поздно полученная) из его составляющих частей,
    не раньше (round 3 ревью PR #37).
    """
    ordered = sorted(intervals, key=lambda i: i.start)
    merged: list[CoverageInterval] = []
    for interval in ordered:
        if merged and interval.start <= merged[-1].end:
            if interval.end > merged[-1].end:
                merged[-1] = CoverageInterval(
                    start=merged[-1].start,
                    end=interval.end,
                    fetched_at=max(merged[-1].fetched_at, interval.fetched_at),
                )
            elif interval.fetched_at > merged[-1].fetched_at:
                merged[-1] = CoverageInterval(
                    start=merged[-1].start,
                    end=merged[-1].end,
                    fetched_at=interval.fetched_at,
                )
            continue
        merged.append(interval)
    return merged


def assess_archive_window(
    records: Iterable[Mapping[str, Any]],
    *,
    policy: ArchiveProductPolicy,
    ingested_intervals: Sequence[CoverageInterval],
    window_start: datetime,
    window_end: datetime,
    as_of: datetime,
) -> ArchiveWindowAssessment:
    """Оценивает окно ВКД по одному архивному продукту в трёх состояниях.

    ``records`` — уже отобранные строгой выборкой записи (``select_as_of``
    через ``archive_ingest.select_forecast_inputs``). Запись, опубликованная
    позже ``as_of``, здесь не игнорируется, а поднимает
    :class:`TemporalLeakError`: это делает обязательный тест на утечку
    (main-prompt.md §9.1) проверкой поведения, а не проверкой того, что
    кто-то не забыл отфильтровать вход.

    Покрытие считается как пересечение окна с объединением **фактически
    загруженных** интервалов, дополнительно ограниченное горизонтом
    продукта: вперёд от ``as_of`` продукт говорит не дальше, чем
    ``as_of + forecast_horizon_hours``, а если горизонт не установлен — не
    дальше самого ``as_of``. Непокрытая часть окна даёт ``critical_gap``, а
    часть за горизонтом — ``beyond_horizon``; ни то, ни другое не может
    превратиться в благоприятную оценку.
    """
    for name, value in (
        ("window_start", window_start),
        ("window_end", window_end),
        ("as_of", as_of),
    ):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{name} must be a timezone-aware UTC datetime (main-prompt.md §1)")
    if window_end <= window_start:
        raise ValueError("window_end must be after window_start")

    window_duration = window_end - window_start
    merged_intervals = _merge(ingested_intervals)

    events: list[ArchiveEvent] = []
    unresolved_open_events: list[ArchiveEvent] = []
    considered_record_ids: list[str] = []
    for record in records:
        record_id = record.get("record_id")
        if record.get("source_id") != policy.source_id:
            raise UnsupportedRecordError(
                f"record {record_id!r} has source_id={record.get('source_id')!r}, "
                f"expected {policy.source_id!r}"
            )
        published_raw = record.get("published_at")
        if published_raw is None:
            raise UnsupportedRecordError(
                f"record {record_id!r} has published_at=null — such a record is "
                "permanently unfit for strict replay and must never reach this "
                "assessment (.ai/main-prompt.md §1)"
            )
        published_at = _parse(published_raw, "published_at", record_id)
        if published_at > as_of:
            raise TemporalLeakError(
                f"record {record_id!r} was published at {published_at.isoformat()}, "
                f"after as_of={as_of.isoformat()} — it was not available at the cutoff "
                "(.ai/main-prompt.md §1)"
            )
        considered_record_ids.append(str(record_id))

        valid_from = _parse(record["valid_from"], "valid_from", record_id)
        valid_to = _parse(record["valid_to"], "valid_to", record_id)
        spatial_context = record.get("spatial_context") or {}
        message_type = str(spatial_context.get("message_type", ""))
        if message_type not in policy.event_message_types:
            continue
        # Границы закрыты с обеих сторон: точечное событие ровно на краю окна
        # — это событие внутри окна, и пропустить его было бы ошибкой в
        # опасную сторону.
        if valid_from <= window_end and valid_to >= window_start:
            events.append(
                ArchiveEvent(
                    record_id=str(record_id),
                    provider_record_id=str(record["provider_record_id"]),
                    message_type=message_type,
                    valid_from=valid_from,
                    valid_to=valid_to,
                    published_at=published_at,
                )
            )
            continue
        # round 3 ревью PR #37: точечное начало явления без
        # задокументированного конца (``open_ended`` — archive_probe.py),
        # предшествующее окну. Отсутствие прямого пересечения здесь НЕ
        # означает «явление закончилось раньше окна» — мы этого не знаем.
        # Ограничиваем предположение о неопределённости тем же прочитанным
        # интервалом архива, в котором зафиксировано начало: дальше этого
        # интервала у нас нет вообще никакой информации об этом продукте,
        # и это уже покрыто critical_gap/beyond_horizon отдельно.
        open_ended = bool(spatial_context.get("open_ended", False))
        if open_ended and valid_from <= window_end:
            containing = _find_containing_interval(valid_from, merged_intervals)
            if containing is not None and containing.end >= window_start:
                unresolved_open_events.append(
                    ArchiveEvent(
                        record_id=str(record_id),
                        provider_record_id=str(record["provider_record_id"]),
                        message_type=message_type,
                        valid_from=valid_from,
                        valid_to=valid_to,
                        published_at=published_at,
                    )
                )

    horizon_end: datetime | None = None
    if policy.forecast_horizon_hours is not None:
        horizon_end = as_of + timedelta(hours=policy.forecast_horizon_hours)
    # Горизонт не установлен => вперёд от отсечения продукт не утверждает
    # ничего. Это не «ноль часов как значение по умолчанию», а отказ
    # засчитывать покрытие там, где его никто не подтвердил.
    coverage_limit = horizon_end if horizon_end is not None else as_of

    covered = timedelta(0)
    covering_fetched_at: datetime | None = None
    for interval in merged_intervals:
        start = max(interval.start, window_start)
        end = min(min(interval.end, window_end), coverage_limit)
        if end > start:
            covered += end - start
            if covering_fetched_at is None or interval.fetched_at > covering_fetched_at:
                covering_fetched_at = interval.fetched_at

    coverage_fraction = covered / window_duration
    critical_gap = covered < window_duration
    beyond_horizon = window_end > coverage_limit

    status: AssessmentStatus
    if events:
        status = "EVENT_PRESENT"
    elif not policy.event_message_types:
        status = "INSUFFICIENT_DATA"
    elif beyond_horizon or critical_gap:
        status = "INSUFFICIENT_DATA"
    elif unresolved_open_events:
        status = "INSUFFICIENT_DATA"
    else:
        status = "NO_EVENT_DETECTED"

    notes = _build_notes(
        policy=policy,
        status=status,
        events=events,
        unresolved_open_events=unresolved_open_events,
        horizon_end=horizon_end,
        beyond_horizon=beyond_horizon,
        critical_gap=critical_gap,
        coverage_fraction=coverage_fraction,
        covering_fetched_at=covering_fetched_at,
        as_of=as_of,
    )

    return ArchiveWindowAssessment(
        status=status,
        source_id=policy.source_id,
        window_start=window_start,
        window_end=window_end,
        as_of=as_of,
        horizon_end=horizon_end,
        beyond_horizon=beyond_horizon,
        critical_gap=critical_gap,
        coverage_fraction=coverage_fraction,
        events=tuple(events),
        unresolved_open_events=tuple(unresolved_open_events),
        record_ids=tuple(considered_record_ids),
        notes=notes,
    )


def _find_containing_interval(
    moment: datetime, merged_intervals: Sequence[CoverageInterval]
) -> CoverageInterval | None:
    """Интервал из уже склеенного (``_merge``) списка, содержащий ``moment``.

    ``merged_intervals`` не пересекаются и отсортированы по построению
    (``_merge``), поэтому линейный поиск здесь достаточен: списки интервалов
    покрытия одного продукта на практике коротки (единицы-десятки окон
    загрузки), это не путь, чувствительный к производительности."""
    for interval in merged_intervals:
        if interval.start <= moment <= interval.end:
            return interval
    return None


def _build_notes(
    *,
    policy: ArchiveProductPolicy,
    status: AssessmentStatus,
    events: Sequence[ArchiveEvent],
    unresolved_open_events: Sequence[ArchiveEvent],
    horizon_end: datetime | None,
    beyond_horizon: bool,
    critical_gap: bool,
    coverage_fraction: float,
    covering_fetched_at: datetime | None,
    as_of: datetime,
) -> tuple[str, ...]:
    """Объяснения к оценке (критерий О4: от вывода — к записи и правилу)."""
    notes: list[str] = []
    if status == "EVENT_PRESENT":
        listed = ", ".join(f"{e.provider_record_id} ({e.message_type})" for e in events)
        notes.append(
            f"Архив {policy.source_id}: окно пересекают сообщения о событии — {listed}. "
            "Это внешнее предупреждение источника, не расчёт команды и не доза "
            "космонавта (main-prompt.md §4)."
        )
    elif status == "NO_EVENT_DETECTED":
        fetched_note = (
            f" Покрытие установлено загрузкой архива по {covering_fetched_at.isoformat()}."
            if covering_fetched_at is not None
            else ""
        )
        notes.append(
            f"Архив {policy.source_id}: интервал, покрывающий окно, прочитан полностью, "
            "и сообщений о событии в нём нет. Это означает «порог не пересечён в "
            "прочитанном интервале», а не «обстановка подтверждена спокойной»: "
            "событийный архив не даёт непрерывного подтверждения состояния "
            f"(docs/method.md §6).{fetched_note}"
        )
    else:
        notes.append(
            f"Архив {policy.source_id}: оценить невозможно — это отсутствие данных, "
            "а не благоприятная обстановка (main-prompt.md §2)."
        )

    if unresolved_open_events:
        listed = ", ".join(
            f"{e.provider_record_id} (начало {e.valid_from.isoformat()})"
            for e in unresolved_open_events
        )
        notes.append(
            f"Архив {policy.source_id}: в прочитанном интервале зафиксировано начало "
            f"явления без задокументированного конца — {listed}. Источник не публикует "
            "структурированный момент завершения (round 3 ревью PR #37), поэтому "
            "продолжалось ли явление на момент этого окна — неизвестно: это «оценить "
            "невозможно», а не подтверждённое отсутствие и не подтверждённое "
            "продолжение (main-prompt.md §2)."
        )

    if not policy.event_message_types:
        notes.append(
            f"Продукт {policy.source_id} не даёт машиночитаемого признака события "
            "(event_message_types пуст в sources.yaml), поэтому доказанное отсутствие "
            "события по нему структурно недостижимо — только EVENT_PRESENT по другому "
            "продукту или INSUFFICIENT_DATA."
        )
    if beyond_horizon:
        if horizon_end is None:
            notes.append(
                "Фактический горизонт прогноза этого продукта не установлен "
                "(forecast_horizon_hours = null в sources.yaml), поэтому часть окна "
                f"после отсечения {as_of.isoformat()} считается непокрытой — "
                "«не покрыто», а не «спокойно» (main-prompt.md §4). "
                + (policy.forecast_horizon_note or "")
            )
        else:
            notes.append(
                f"Часть окна после {horizon_end.isoformat()} — за фактическим горизонтом "
                f"продукта ({policy.forecast_horizon_hours:g} ч от отсечения, значение из "
                "sources.yaml, не из кода): «не покрыто», а не «спокойно» "
                "(main-prompt.md §4)."
            )
    if critical_gap:
        notes.append(
            f"Окно покрыто фактически загруженными интервалами архива на "
            f"{coverage_fraction:.0%} длительности — непокрытая часть является "
            "критическим пробелом данных (main-prompt.md §2)."
        )
    return tuple(notes)


def source_assessment_for_agreement(assessment: ArchiveWindowAssessment) -> SourceAssessment:
    """Адаптирует оценку одного архивного продукта в провайдер-агностичный
    вид, которым оперирует
    ``src/domain/spaceweather/source_agreement.py::compute_source_agreement``
    (FN-47).

    ``AssessmentStatus.INSUFFICIENT_DATA`` этого продукта — не decision-relevant
    классификация, а «оценить невозможно» (main-prompt.md §2), поэтому здесь
    она превращается в ``classification=None`` и не войдёт в подсчёт
    ПРИМЕНИМЫХ независимых оценок FN-47 — но ``record_ids``/``notes`` этого
    продукта сохраняются целиком, а не отбрасываются (main-prompt.md §2
    «сохранить оценки и provenance каждого источника»).

    ``EVENT_PRESENT``/``NO_EVENT_DETECTED`` переносятся как есть: это и есть
    decision-relevant классификация двух независимых архивных продуктов
    одного механизма (DONKI, SWPC Forecast Discussion — оба
    задокументированы как независимые друг от друга в ``sources.yaml``
    → ``independence_note``)."""
    classification = None if assessment.status == "INSUFFICIENT_DATA" else assessment.status
    return SourceAssessment(
        source_id=assessment.source_id,
        classification=classification,
        record_ids=assessment.record_ids,
        notes=assessment.notes,
    )


__all__ = [
    "ArchiveEvent",
    "ArchiveProductPolicy",
    "ArchiveWindowAssessment",
    "AssessmentStatus",
    "CoverageInterval",
    "TemporalLeakError",
    "UnsupportedRecordError",
    "assess_archive_window",
    "source_assessment_for_agreement",
]
