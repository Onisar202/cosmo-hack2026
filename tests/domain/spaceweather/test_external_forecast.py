"""Тесты доменной интерпретации внешнего прогноза NOAA 3-Day S1+ (FN-31).

Чистые тесты без сети и без хранилища — main-prompt.md §8 «расчёт не ходит
в сеть»: вход этого модуля — уже полученные записи (словари в форме
``contracts/record.schema.json``) или готовые дата-классы.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.domain.spaceweather.external_forecast import (
    ExternalForecastDay,
    UnsupportedRecordError,
    assess_external_forecast,
    external_forecast_days_from_records,
)

UTC = timezone.utc


def _record(
    *,
    record_id: str = "rec-1",
    source_id: str = "noaa-swpc-3day-forecast",
    record_kind: str = "forecast",
    unit: str | None = "percent",
    value: float | None = 20.0,
    valid_from: str = "2025-01-05T00:00:00Z",
    published_at: str | None = "2025-01-05T22:00:00Z",
) -> dict:
    return {
        "record_id": record_id,
        "source_id": source_id,
        "record_kind": record_kind,
        "unit": unit,
        "value": value,
        "valid_from": valid_from,
        "published_at": published_at,
    }


# --------------------------------------------------------------------------
# external_forecast_days_from_records — только эта линия, никакая другая
# --------------------------------------------------------------------------


def test_maps_well_formed_record_to_external_forecast_day() -> None:
    days = external_forecast_days_from_records([_record()])
    assert len(days) == 1
    assert days[0].forecast_day.isoformat() == "2025-01-05"
    assert days[0].probability_percent == 20.0
    assert days[0].record_id == "rec-1"


def test_rejects_record_from_a_different_source() -> None:
    """Наблюдение GOES (или любой другой источник) не должно быть тихо
    интерпретировано как суточная вероятность S1+ (main-prompt.md §4)."""
    with pytest.raises(UnsupportedRecordError):
        external_forecast_days_from_records([_record(source_id="noaa-swpc-proton-flux")])


def test_rejects_non_forecast_record_kind() -> None:
    with pytest.raises(UnsupportedRecordError):
        external_forecast_days_from_records([_record(record_kind="observation")])


def test_rejects_wrong_unit() -> None:
    with pytest.raises(UnsupportedRecordError):
        external_forecast_days_from_records([_record(unit="pfu")])


def test_missing_value_is_skipped_not_treated_as_zero() -> None:
    """main-prompt.md §2: пропуск не заменяется нулём."""
    days = external_forecast_days_from_records([_record(value=None)])
    assert days == []


def test_null_published_at_is_rejected_as_malformed_input() -> None:
    with pytest.raises(UnsupportedRecordError):
        external_forecast_days_from_records([_record(published_at=None)])


def test_accepts_archive_source_id_too() -> None:
    days = external_forecast_days_from_records(
        [_record(source_id="noaa-swpc-3day-forecast-archive")]
    )
    assert len(days) == 1


# --------------------------------------------------------------------------
# assess_external_forecast — пересечение с окном, без агрегации в часы
# --------------------------------------------------------------------------


def _day(
    forecast_day: str, probability: float, published_at: str, record_id: str
) -> ExternalForecastDay:
    from datetime import date

    y, m, d = (int(part) for part in forecast_day.split("-"))
    return ExternalForecastDay(
        forecast_day=date(y, m, d),
        probability_percent=probability,
        published_at=datetime.fromisoformat(published_at),
        record_id=record_id,
    )


def test_overlapping_day_is_reported_with_intersection_bounds() -> None:
    day = _day("2025-01-05", 20.0, "2025-01-05T00:00:00+00:00", "rec-1")
    window_start = datetime(2025, 1, 5, 10, 0, tzinfo=UTC)
    window_end = datetime(2025, 1, 5, 14, 0, tzinfo=UTC)  # 4-часовое окно ВКД

    assessment = assess_external_forecast([day], window_start=window_start, window_end=window_end)

    assert assessment.critical_gap is False
    assert assessment.max_probability_percent == 20.0
    assert assessment.record_ids == ("rec-1",)
    overlap = assessment.overlaps[0]
    assert overlap.overlaps_window is True
    assert overlap.overlap_start == window_start
    assert overlap.overlap_end == window_end


def test_non_overlapping_day_does_not_contribute_to_max_probability() -> None:
    day = _day("2025-01-07", 90.0, "2025-01-05T22:00:00+00:00", "rec-2")
    window_start = datetime(2025, 1, 5, 10, 0, tzinfo=UTC)
    window_end = datetime(2025, 1, 5, 14, 0, tzinfo=UTC)

    assessment = assess_external_forecast([day], window_start=window_start, window_end=window_end)

    assert assessment.critical_gap is True  # нет ни одного пересекающегося дня
    assert assessment.max_probability_percent is None  # не 0%
    assert assessment.record_ids == ()
    assert assessment.overlaps[0].overlaps_window is False


def test_window_spanning_midnight_keeps_both_days_separate_not_summed() -> None:
    """main-prompt.md/FN-31: суточная вероятность не суммируется через
    полночь — окно, пересекающее два дня, даёт ДВЕ отдельные записи о
    пересечении, не одно суммарное число."""
    day1 = _day("2025-01-05", 20.0, "2025-01-05T22:00:00+00:00", "rec-1")
    day2 = _day("2025-01-06", 45.0, "2025-01-06T22:00:00+00:00", "rec-2")
    window_start = datetime(2025, 1, 5, 22, 0, tzinfo=UTC)
    window_end = datetime(2025, 1, 6, 4, 0, tzinfo=UTC)  # пересекает полночь

    assessment = assess_external_forecast(
        [day1, day2], window_start=window_start, window_end=window_end
    )

    assert len(assessment.overlaps) == 2
    assert all(o.overlaps_window for o in assessment.overlaps)
    # Максимум — это максимум ДНЕВНЫХ значений, не их сумма (20+45=65 было бы багом).
    assert assessment.max_probability_percent == 45.0
    assert set(assessment.record_ids) == {"rec-1", "rec-2"}


def test_no_forecast_days_at_all_is_a_critical_gap() -> None:
    window_start = datetime(2025, 1, 5, 10, 0, tzinfo=UTC)
    window_end = datetime(2025, 1, 5, 14, 0, tzinfo=UTC)
    assessment = assess_external_forecast([], window_start=window_start, window_end=window_end)
    assert assessment.critical_gap is True
    assert assessment.max_probability_percent is None
    assert "не покрыто" in assessment.notes[-1] or len(assessment.notes) == 0


def test_naive_window_bounds_are_rejected() -> None:
    day = _day("2025-01-05", 20.0, "2025-01-05T22:00:00+00:00", "rec-1")
    with pytest.raises(ValueError):
        assess_external_forecast(
            [day],
            window_start=datetime(2025, 1, 5, 10, 0),  # naive — запрещено §1
            window_end=datetime(2025, 1, 5, 14, 0, tzinfo=UTC),
        )


def test_notes_never_claim_this_is_an_eva_probability() -> None:
    """FN-31 «Обязательная семантика»: нигде не называется вероятностью ВКД."""
    day = _day("2025-01-05", 20.0, "2025-01-05T22:00:00+00:00", "rec-1")
    assessment = assess_external_forecast(
        [day],
        window_start=datetime(2025, 1, 5, 10, 0, tzinfo=UTC),
        window_end=datetime(2025, 1, 5, 14, 0, tzinfo=UTC),
    )
    joined = " ".join(assessment.notes).lower()
    # Разрешена только явная оговорка «не вероятность ВКД»; утвердительной
    # формы («это вероятность ВКД») быть не должно.
    assert "не вероятность вкд" in joined
    assert "это вероятность вкд" not in joined
