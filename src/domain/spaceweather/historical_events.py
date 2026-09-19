"""Механизм 1 для исторических окон: архивная событийная линия (FN-41, этап 3).

Третья линия Механизма 1 рядом с уже существующими двумя —
``observed_classifier.py`` (НАБЛЮДЕНИЕ потока протонов GOES, FN-38) и
``external_forecast.py`` (внешний СУТОЧНЫЙ прогноз NOAA 3-Day, FN-31).
Отличие принципиальное и не должно теряться при передаче через API
(main-prompt.md §4): здесь нет ни измеренной величины, ни вероятности —
есть только факт наличия/отсутствия ПУБЛИКАЦИИ уведомления о событии в
архиве и подтверждённость покрытия архива на этот интервал. Обе первые
линии для обязательного периода 01.05–30.06.2024 неприменимы: продукт
GOES ``integral-protons-1-day`` архива не хранит вовсе, а архив NOAA 3-Day
покрывает период не полностью (``sources.yaml``).

**Три состояния, не два** (main-prompt.md §2 «воздействие не выявлено /
данные устарели / оценить невозможно»; подтверждено доменным экспертом
2026-09-19 как обязательное различие именно для исторических окон):

- :data:`EVENT_PRESENT` — внутри окна (с учётом обратного запаса, см. ниже)
  есть пригодное уведомление квалифицирующего типа;
- :data:`NO_EVENT_DETECTED` — покрытие архива на нужный интервал
  ПОДТВЕРЖДЕНО, и ни одного квалифицирующего уведомления в нём нет;
- :data:`INSUFFICIENT_DATA` — покрытие подтвердить нельзя: пробел архива,
  отказ получения, или в интервале есть квалифицирующее уведомление с
  неразрешимым временем публикации (``DonkiNotification.resolved_issue_time
  is None``). Это критический пробел, а не благоприятный вывод: отказ
  источника, не превратившийся в «события не было», — прямое требование
  main-prompt.md §2 и критерия Т6.

**Чистая функция** (main-prompt.md §8): ни сети, ни хранилища, ни
системного «сейчас». Все пороги и списки типов приходят аргументами из
``sources.yaml`` (``src/sources/donki.py::DonkiSourceConfig``), интервалы
подтверждённого покрытия — от шлюза получения, который один знает, что
именно было успешно скачано. Какие записи попали во вход — решают ДВЕ
РАЗНЫЕ ветки вызывающей стороны (строгий ``published_at <= as_of`` +
``replay_eligible`` для ``historical_forecast`` против полного архивного
диапазона для ``historical_analysis``), а не флаг внутри этой функции
(main-prompt.md §1 «последующие наблюдения — отдельная ветка кода»; тот же
раздельный вид, что и ``select_release_for_forecast`` /
``select_release_for_analysis`` для орбиты).

**Обратный запас (``persistence_lookback_hours``) — не горизонт прогноза.**
DONKI выпускает уведомление в момент НАЧАЛА события (например «flux of
> 10 MeV protons exceeds 10 pfu starting at 2024-05-10T13:35Z») и не
выпускает сообщения об окончании. Окно, начинающееся через час после
такого уведомления, поэтому нельзя считать спокойным только потому, что
внутри него самого уведомлений нет. Событие консервативно считается
продолжающимся ``persistence_lookback_hours`` от момента события —
консервативно в сторону тревоги, не в сторону благополучия, и явно
отмечено в ``notes``. Это предположение о продолжительности УЖЕ
ОБЪЯВЛЕННОГО события назад по времени; вопрос о горизонте собственного
прогноза ВПЕРЁД (6 ч против 24 ч) им не решается и остаётся открытым (вне
объёма FN-41).

**Связанные сигналы одного события дают один вклад** (main-prompt.md §4):
уведомления схлопываются по паре (тип сообщения, момент события) — момент
события извлечён из структурированного ``Activity ID`` самого уведомления
(``src/sources/archive_probe.py``), поэтому пара уведомлений об одной и той
же активности (реальный случай ``20240510-AL-013``/``AL-014`` о Kp 7.67,
активность ``2024-05-10T15:00:00-GST-001``) даёт одно событие, а не два.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

UTC = timezone.utc

EventState = Literal["EVENT_PRESENT", "NO_EVENT_DETECTED", "INSUFFICIENT_DATA"]

EVENT_PRESENT: EventState = "EVENT_PRESENT"
NO_EVENT_DETECTED: EventState = "NO_EVENT_DETECTED"
INSUFFICIENT_DATA: EventState = "INSUFFICIENT_DATA"

#: Единственный зарегистрированный источник этой линии (``sources.yaml`` →
#: ``space_weather`` → ``nasa-donki-notifications``). Строка продублирована
#: буквально, а не импортом из ``src.sources`` — тем же правилом, что и
#: ``_ACCEPTED_SOURCE_ID`` в ``observed_classifier.py`` (domain не знает про
#: слой получения, только про форму уже полученной записи).
_ACCEPTED_SOURCE_ID = "nasa-donki-notifications"
_ACCEPTED_RECORD_KIND = "warning"


class UnsupportedRecordError(ValueError):
    """Запись — не уведомление архивной линии DONKI (чужой ``source_id`` или
    ``record_kind``). Поднимается вместо тихого приведения: смешение
    предупреждения с наблюдением или прогнозом — прямое нарушение
    main-prompt.md §4."""


@dataclass(frozen=True)
class ArchivedNotification:
    """Одно уведомление архива в форме, нужной этой оценке.

    ``event_start``/``event_end`` — времена СОБЫТИЯ (``valid_from``/
    ``valid_to`` записи, извлечённые источником из ``Activity ID`` либо
    ``Report Coverage …``), не время публикации: подмена одного другим —
    главная ловушка main-prompt.md §1.
    """

    record_id: str
    provider_record_id: str
    message_type: str
    event_start: datetime
    event_end: datetime
    published_at: datetime | None

    @property
    def activity_key(self) -> tuple[str, datetime]:
        """Ключ схлопывания связанных сигналов одного события (см. модульный
        docstring): тип сообщения + момент активности."""
        return (self.message_type, self.event_start)


@dataclass(frozen=True)
class HistoricalEventAssessment:
    """Результат оценки архивной событийной линии для одного окна."""

    state: EventState
    window_start: datetime
    window_end: datetime
    required_start: datetime
    coverage_fraction: float
    critical_gap: bool
    event_count: int
    notes: tuple[str, ...]
    record_ids: tuple[str, ...]


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(
            f"{field_name} must be a timezone-aware UTC datetime (.ai/main-prompt.md §1)"
        )


def _parse_iso(value: str, *, field_name: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise UnsupportedRecordError(f"{field_name} has no UTC offset: {value!r}")
    return parsed.astimezone(UTC)


def archived_notifications_from_records(
    records: Iterable[Mapping[str, Any]],
) -> list[ArchivedNotification]:
    """Приводит записи хранилища (форма ``contracts/record.schema.json``) к
    :class:`ArchivedNotification`.

    Чужой источник/вид записи — :class:`UnsupportedRecordError`, а не тихий
    пропуск: вызывающая сторона обязана передавать выборку именно этой
    линии, и ошибка выборки не должна выглядеть как «уведомлений нет».
    """
    result: list[ArchivedNotification] = []
    for record in records:
        source_id = record.get("source_id")
        record_kind = record.get("record_kind")
        if source_id != _ACCEPTED_SOURCE_ID or record_kind != _ACCEPTED_RECORD_KIND:
            raise UnsupportedRecordError(
                f"record {record.get('record_id')!r} is source_id={source_id!r}/"
                f"record_kind={record_kind!r}, expected {_ACCEPTED_SOURCE_ID!r}/"
                f"{_ACCEPTED_RECORD_KIND!r}"
            )
        spatial_context = record.get("spatial_context") or {}
        message_type = str(spatial_context.get("message_type", "")).strip()
        if not message_type:
            raise UnsupportedRecordError(
                f"record {record.get('record_id')!r} has no spatial_context.message_type"
            )
        published_at_raw = record.get("published_at")
        result.append(
            ArchivedNotification(
                record_id=str(record["record_id"]),
                provider_record_id=str(record["provider_record_id"]),
                message_type=message_type,
                event_start=_parse_iso(str(record["valid_from"]), field_name="valid_from"),
                event_end=_parse_iso(str(record["valid_to"]), field_name="valid_to"),
                published_at=(
                    _parse_iso(str(published_at_raw), field_name="published_at")
                    if published_at_raw
                    else None
                ),
            )
        )
    return result


def _covered_fraction(
    coverage_intervals: Sequence[tuple[datetime, datetime]],
    *,
    start: datetime,
    end: datetime,
) -> float:
    """Доля ``[start, end]``, покрытая объединением интервалов (они могут
    пересекаться и идти в любом порядке)."""
    total = (end - start).total_seconds()
    if total <= 0:
        raise ValueError("end must be after start")
    clipped = sorted(
        (max(lo, start), min(hi, end))
        for lo, hi in coverage_intervals
        if min(hi, end) > max(lo, start)
    )
    covered = timedelta(0)
    cursor: datetime | None = None
    for lo, hi in clipped:
        if cursor is None or lo > cursor:
            covered += hi - lo
            cursor = hi
        elif hi > cursor:
            covered += hi - cursor
            cursor = hi
    return min(1.0, covered.total_seconds() / total)


def assess_archived_events(
    notifications: Sequence[ArchivedNotification],
    *,
    window_start: datetime,
    window_end: datetime,
    coverage_intervals: Sequence[tuple[datetime, datetime]],
    qualifying_message_types: Sequence[str],
    persistence_lookback_hours: float,
    ambiguous_publication_count: int = 0,
) -> HistoricalEventAssessment:
    """Оценивает архивную событийную линию для одного окна ВКД.

    ``notifications`` — УЖЕ отобранные вызывающей стороной записи (строгая
    ветка ``historical_forecast`` передаёт только ``published_at <= as_of``
    и ``replay_eligible``; ветка ``historical_analysis`` — весь архивный
    диапазон); эта функция не фильтрует по времени публикации и не знает
    ``as_of`` — иначе правило пригодности оказалось бы продублированным в
    двух местах, что main-prompt.md §1 прямо запрещает.

    ``coverage_intervals`` — интервалы ВРЕМЕНИ СОБЫТИЯ, на которых архив
    реально получен этой попыткой (пусто при отказе получения). Строгая
    ветка дополнительно обрезает их моментом ``as_of``: то, что было
    опубликовано позже отсечения, не может подтверждать покрытие.

    ``ambiguous_publication_count`` — число уведомлений интервала, у
    которых время публикации неразрешимо (``resolved_issue_time is None``,
    ``src/sources/archive_probe.py``): их отсутствие в строгой выборке —
    следствие неизвестной публикации, а не доказанного отсутствия события.
    """
    _require_aware(window_start, "window_start")
    _require_aware(window_end, "window_end")
    if window_end <= window_start:
        raise ValueError("window_end must be after window_start")
    if persistence_lookback_hours < 0:
        raise ValueError("persistence_lookback_hours must not be negative")

    required_start = window_start - timedelta(hours=persistence_lookback_hours)
    qualifying_types = {t.strip().upper() for t in qualifying_message_types}

    qualifying = [
        notification
        for notification in notifications
        if notification.message_type.strip().upper() in qualifying_types
        and notification.event_end >= required_start
        and notification.event_start <= window_end
    ]
    distinct_events = {notification.activity_key for notification in qualifying}
    record_ids = tuple(sorted({notification.record_id for notification in qualifying}))

    coverage_fraction = _covered_fraction(
        coverage_intervals, start=required_start, end=window_end
    )
    window_coverage_fraction = _covered_fraction(
        coverage_intervals, start=window_start, end=window_end
    )
    coverage_confirmed = coverage_fraction >= 1.0

    notes: list[str] = [
        "Архивная событийная линия Механизма 1 — уведомления NASA CCMC DONKI "
        f"типов {sorted(qualifying_types)} (протонные события и геомагнитные бури, "
        "main-prompt.md §11; sources.yaml → nasa-donki-notifications). Это факт "
        "ПУБЛИКАЦИИ предупреждения, а не измеренное значение потока и не "
        "вероятность: собственного уровня шкалы S эта линия не даёт.",
        f"Учтён обратный запас {persistence_lookback_hours:g} ч от начала окна: DONKI "
        "выпускает уведомление в момент начала события и не выпускает сообщения "
        "об окончании, поэтому уже объявленное событие консервативно считается "
        "продолжающимся (в сторону тревоги, не благополучия). Это не горизонт "
        "прогноза вперёд — вопрос горизонта здесь не решается.",
    ]

    if qualifying:
        state: EventState = EVENT_PRESENT
        critical_gap = True
        notes.append(
            f"В интервале [{required_start.isoformat()}, {window_end.isoformat()}] "
            f"найдено {len(distinct_events)} событие(й) по {len(qualifying)} "
            "уведомлению(ям) — связанные уведомления одной активности схлопнуты в "
            "один вклад (main-prompt.md §4). Количественный уровень и длительность "
            "превышения порогов по этой линии не восстанавливаются (уведомление "
            "фиксирует пересечение порога, не профиль потока), поэтому окно "
            "выводится из автоматического сравнения — но это «оценить невозможно "
            "количественно» при ПОДТВЕРЖДЁННОМ событии, а не благоприятная оценка."
        )
    elif not coverage_confirmed:
        state = INSUFFICIENT_DATA
        critical_gap = True
        notes.append(
            f"Покрытие архива на интервал [{required_start.isoformat()}, "
            f"{window_end.isoformat()}] подтверждено только на "
            f"{coverage_fraction:.0%} — отсутствие уведомлений в непокрытой части "
            "не означает отсутствия события (main-prompt.md §2: отказ/пробел не "
            "превращается в благоприятную оценку)."
        )
    elif ambiguous_publication_count > 0:
        state = INSUFFICIENT_DATA
        critical_gap = True
        notes.append(
            f"{ambiguous_publication_count} уведомление(й) этого интервала имеют "
            "неразрешимое время публикации (два независимых поля расходятся, "
            "src/sources/archive_probe.py) — они непригодны для строгого отбора, "
            "и потому отсутствие события подтвердить нечем (main-prompt.md §1: "
            "неизвестное время публикации — «непригодно», а не «вероятно было "
            "доступно»)."
        )
    else:
        state = NO_EVENT_DETECTED
        critical_gap = False
        notes.append(
            f"Покрытие архива на интервал [{required_start.isoformat()}, "
            f"{window_end.isoformat()}] подтверждено полностью, квалифицирующих "
            "уведомлений в нём нет — это подтверждённое отсутствие события этой "
            "линии, отличимое от «нет данных» (main-prompt.md §2)."
        )

    return HistoricalEventAssessment(
        state=state,
        window_start=window_start,
        window_end=window_end,
        required_start=required_start,
        coverage_fraction=window_coverage_fraction,
        critical_gap=critical_gap,
        event_count=len(distinct_events),
        notes=tuple(notes),
        record_ids=record_ids,
    )


__all__ = [
    "EVENT_PRESENT",
    "INSUFFICIENT_DATA",
    "NO_EVENT_DETECTED",
    "ArchivedNotification",
    "EventState",
    "HistoricalEventAssessment",
    "UnsupportedRecordError",
    "archived_notifications_from_records",
    "assess_archived_events",
]
