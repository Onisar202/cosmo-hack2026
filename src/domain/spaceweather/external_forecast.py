"""Механизм 1: интерпретация внешнего суточного прогноза NOAA 3-Day (S1+).

Реализует доменную часть FN-31 (S2-01): представить сохранённые записи
NOAA «3-Day Forecast» (``src/sources/noaa_3day_forecast.py``,
``record_kind="forecast"``, ``unit="percent"``) для конкретного окна ВКД —
не считая физику источника (main-prompt.md §8 «получение не считает
физику», а обратное здесь — «расчёт не ходит в сеть»: этот модуль принимает
уже полученные записи как обычные словари/дата-классы, никакого HTTP или
доступа к ``src/store`` здесь нет) и не нарушая обязательную семантику
задачи:

- суточная вероятность НЕ делится по часам, НЕ умножается на длительность
  окна, НЕ суммируется через полночь;
- она нигде не называется «вероятностью ВКД» (main-prompt.md §4
  «эвристический уровень не называется вероятностью», здесь — сильнее:
  реальная вероятность внешнего явления не выдаётся за вероятность
  происшествия при ВКД);
- показываются исходные прогнозные дни и их пересечение с окном — не
  единое «покрыто/не покрыто» число;
- длительность фактического превышения из одной суточной вероятности
  неизвестна и не оценивается этим модулем (в отличие от
  ``exceedance_hours_by_level`` будущей интерпретации наблюдения GOES —
  contracts/result.schema.json резервирует это поле для механизма в целом,
  но источником него не может быть суточная вероятность внешнего прогноза).

Отсутствие пересечения окна ни с одним прогнозным днём — это «внешний
прогноз не покрывает окно» (см. :attr:`ExternalForecastAssessment.critical_gap`),
а не «спокойно» (main-prompt.md §2).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

UTC = timezone.utc

#: Источники, чьи записи эта интерпретация вправе читать — единственная
#: линия внешнего суточного прогноза S1+ этой задачи. Явный список (не
#: "любой forecast") — main-prompt.md §4 "различие должно быть невозможно
#: потерять при передаче": функция отказывается интерпретировать запись
#: наблюдения (GOES pfu) или чужого прогноза как эту вероятность.
_ACCEPTED_SOURCE_IDS = frozenset({"noaa-swpc-3day-forecast", "noaa-swpc-3day-forecast-archive"})


class UnsupportedRecordError(ValueError):
    """Запись — не суточная вероятность S1+ NOAA 3-Day (неверный source_id/
    record_kind/unit). Поднимается вместо тихого приведения типов: смешение
    наблюдения и прогноза на этом слое было бы прямым нарушением
    main-prompt.md §4."""


@dataclass(frozen=True)
class ExternalForecastDay:
    """Один исходный прогнозный день NOAA 3-Day (S1+), как опубликован — без
    интерпретации."""

    forecast_day: date
    probability_percent: float
    published_at: datetime
    record_id: str


@dataclass(frozen=True)
class ForecastDayWindowOverlap:
    """Пересечение одного прогнозного дня с окном ВКД.

    Намеренно не несёт ``overlap_hours`` или похожего числа: main-prompt.md
    §11 «Длительность фактического превышения из одной суточной вероятности
    неизвестна» — граница пересечения по времени показывается, но не
    выдаётся за длительность воздействия.
    """

    day: ExternalForecastDay
    overlaps_window: bool
    overlap_start: datetime | None
    overlap_end: datetime | None


@dataclass(frozen=True)
class ExternalForecastAssessment:
    """Представление внешнего суточного прогноза S1+ для одного окна ВКД."""

    window_start: datetime
    window_end: datetime
    overlaps: tuple[ForecastDayWindowOverlap, ...]
    max_probability_percent: float | None
    critical_gap: bool
    notes: tuple[str, ...]
    record_ids: tuple[str, ...]


def external_forecast_days_from_records(
    records: Iterable[Mapping[str, Any]],
) -> list[ExternalForecastDay]:
    """Преобразует уже выбранные (например через ``select_as_of``) записи
    хранилища (форма ``contracts/record.schema.json``) в
    :class:`ExternalForecastDay`.

    Отклоняет любую запись не из зарегистрированной линии NOAA 3-Day S1+
    (:class:`UnsupportedRecordError`) — вызывающая сторона обязана уже
    отфильтровать выборку по ``source_id``, эта проверка — вторая граница,
    не единственная, чтобы ошибка выборки выше по пайплайну не обернулась
    тихой интерпретацией наблюдения как прогноза.
    """
    days: list[ExternalForecastDay] = []
    for record in records:
        source_id = record.get("source_id")
        if source_id not in _ACCEPTED_SOURCE_IDS:
            raise UnsupportedRecordError(
                f"record {record.get('record_id')!r} has source_id={source_id!r}, "
                f"expected one of {sorted(_ACCEPTED_SOURCE_IDS)}"
            )
        if record.get("record_kind") != "forecast":
            raise UnsupportedRecordError(
                f"record {record.get('record_id')!r} has record_kind="
                f"{record.get('record_kind')!r}, expected 'forecast'"
            )
        if record.get("unit") != "percent":
            raise UnsupportedRecordError(
                f"record {record.get('record_id')!r} has unit={record.get('unit')!r}, "
                "expected 'percent' (main-prompt.md §2: unit travels with the value)"
            )
        value = record["value"]
        if value is None:
            # Контракт допускает value=null для record_kind=forecast (пропуск,
            # не заменяется нулём) — эта интерпретация пропускает такую запись,
            # а не подставляет 0% (main-prompt.md §2).
            continue
        valid_from = datetime.fromisoformat(str(record["valid_from"]).replace("Z", "+00:00"))
        published_at_raw = record.get("published_at")
        if published_at_raw is None:
            # published_at=null => replay_eligible=false по правилу хранилища;
            # запись не должна была пройти select_as_of для historical_forecast,
            # но эта функция также используется для current/historical_analysis
            # — там published_at всё равно обязателен для этой линии (парсер
            # источника требует ':Issued:'), null здесь означает искажённые
            # входные данные, не штатный случай.
            raise UnsupportedRecordError(
                f"record {record.get('record_id')!r} has published_at=null — "
                "the NOAA 3-Day connector always requires ':Issued:' (see "
                "src/sources/noaa_3day_forecast.py)"
            )
        published_at = datetime.fromisoformat(str(published_at_raw).replace("Z", "+00:00"))
        days.append(
            ExternalForecastDay(
                forecast_day=valid_from.astimezone(UTC).date(),
                probability_percent=float(value),
                published_at=published_at,
                record_id=str(record["record_id"]),
            )
        )
    return days


def assess_external_forecast(
    days: Iterable[ExternalForecastDay], *, window_start: datetime, window_end: datetime
) -> ExternalForecastAssessment:
    """Сопоставляет исходные прогнозные дни с окном ВКД, без агрегации в
    единый уровень или длительность.

    ``max_probability_percent`` — максимум ТОЛЬКО среди дней, реально
    пересекающихся с окном; ``None``, если ни один день окно не покрывает
    (критический пробел, не 0%). Сложение через полночь и деление по часам
    здесь структурно невозможны: функция ничего не делит и не суммирует,
    только строит попарные пересечения интервалов.
    """
    if window_start.tzinfo is None or window_end.tzinfo is None:
        raise ValueError("window_start/window_end must be timezone-aware UTC datetimes")
    if window_end <= window_start:
        raise ValueError("window_end must be after window_start")

    overlaps: list[ForecastDayWindowOverlap] = []
    for day in days:
        fd = day.forecast_day
        day_start = datetime(fd.year, fd.month, fd.day, tzinfo=UTC)
        day_end = day_start + timedelta(days=1)
        overlap_start = max(day_start, window_start)
        overlap_end = min(day_end, window_end)
        overlaps_window = overlap_start < overlap_end
        overlaps.append(
            ForecastDayWindowOverlap(
                day=day,
                overlaps_window=overlaps_window,
                overlap_start=overlap_start if overlaps_window else None,
                overlap_end=overlap_end if overlaps_window else None,
            )
        )

    intersecting = [o for o in overlaps if o.overlaps_window]
    max_probability = max((o.day.probability_percent for o in intersecting), default=None)
    critical_gap = len(intersecting) == 0

    notes: list[str] = []
    for overlap in overlaps:
        day = overlap.day
        if overlap.overlaps_window:
            notes.append(
                f"NOAA 3-Day Forecast: суточная вероятность S1+ на {day.forecast_day.isoformat()} "
                f"= {day.probability_percent:g}% (опубликовано {day.published_at.isoformat()}); "
                f"пересечение с окном: {overlap.overlap_start.isoformat()} .. "  # type: ignore[union-attr]
                f"{overlap.overlap_end.isoformat()}. Это суточная вероятность внешнего прогноза, "  # type: ignore[union-attr]
                "не вероятность ВКД; длительность фактического превышения внутри окна по ней не "
                "определяется."
            )
        else:
            notes.append(
                f"NOAA 3-Day Forecast: день {day.forecast_day.isoformat()} "
                f"(вероятность S1+ {day.probability_percent:g}%, опубликовано "
                f"{day.published_at.isoformat()}) не пересекается с этим окном."
            )
    if critical_gap:
        notes.append(
            "Окно не покрыто ни одним днём NOAA 3-Day Forecast — это отсутствие внешнего "
            "прогноза на этот интервал, а не спокойная обстановка (main-prompt.md §2)."
        )

    return ExternalForecastAssessment(
        window_start=window_start,
        window_end=window_end,
        overlaps=tuple(overlaps),
        max_probability_percent=max_probability,
        critical_gap=critical_gap,
        notes=tuple(notes),
        record_ids=tuple(o.day.record_id for o in intersecting),
    )


__all__ = [
    "ExternalForecastAssessment",
    "ExternalForecastDay",
    "ForecastDayWindowOverlap",
    "UnsupportedRecordError",
    "assess_external_forecast",
    "external_forecast_days_from_records",
]
