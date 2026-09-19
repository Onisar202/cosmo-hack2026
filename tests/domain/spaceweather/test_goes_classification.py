"""Тесты доменной классификации наблюдения GOES pfu по шкале S NOAA (FN-38).

Чистые тесты без сети и без хранилища (main-prompt.md §8) — вход этого
модуля — уже выбранные записи хранилища (словари в форме
``contracts/record.schema.json``) или готовые :class:`GoesPfuSample`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.domain.spaceweather.goes_classification import (
    EXCEEDANCE_LEVELS,
    LEVEL_ORDER,
    GoesPfuSample,
    UnsupportedRecordError,
    assess_goes_classification,
    classify_level,
    goes_samples_from_records,
    to_mechanism_assessment_dict,
)

UTC = timezone.utc
CRITICAL_STALENESS_SECONDS = 3600.0


def _record(
    *,
    record_id: str = "rec-1",
    source_id: str = "noaa-swpc-proton-flux",
    record_kind: str = "observation",
    unit: str | None = "pfu",
    value: float | None = 5.0,
    observed_at: str = "2024-05-10T12:00:00Z",
    quality: str = "nominal",
) -> dict:
    return {
        "record_id": record_id,
        "source_id": source_id,
        "record_kind": record_kind,
        "unit": unit,
        "value": value,
        "observed_at": observed_at,
        "quality": quality,
    }


def _sample(
    *, observed_at: datetime, value: float | None, record_id: str = "rec", degraded: bool = False
) -> GoesPfuSample:
    return GoesPfuSample(
        observed_at=observed_at, value=value, record_id=record_id, degraded=degraded
    )


# --------------------------------------------------------------------------
# classify_level — пороги шкалы S NOAA (main-prompt.md §11), границы
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value_pfu,expected_level",
    [
        (0.0, "background"),
        (9.999, "background"),
        (9.9999999, "background"),
        (10.0, "S1"),  # граница включительна снизу — main-prompt.md §11
        (10.0001, "S1"),
        (99.999, "S1"),
        (100.0, "S2"),
        (100.0001, "S2"),
        (999.999, "S2"),
        (1000.0, "S3"),
        (5000.0, "S3"),
    ],
)
def test_classify_level_boundaries(value_pfu: float, expected_level: str) -> None:
    assert classify_level(value_pfu) == expected_level


def test_level_order_and_exceedance_keys_match_dominance_module() -> None:
    """Слова уровней/ключей обязаны совпасть с
    ``src/domain/windows/dominance.py::_LEVEL_ORDER["space_weather"]`` — иначе
    ``WindowMechanismInput``/``compare_mechanism`` подняли бы
    ``WindowComparisonError`` на реальном результате."""
    assert LEVEL_ORDER == ("background", "S1", "S2", "S3")
    assert EXCEEDANCE_LEVELS == ("S1", "S2", "S3")


# --------------------------------------------------------------------------
# goes_samples_from_records — вторая граница между наблюдением и остальным
# --------------------------------------------------------------------------


def test_maps_well_formed_record_to_sample() -> None:
    samples = goes_samples_from_records([_record()])
    assert len(samples) == 1
    assert samples[0].value == 5.0
    assert samples[0].record_id == "rec-1"
    assert samples[0].degraded is False


def test_maps_degraded_quality_to_degraded_flag() -> None:
    samples = goes_samples_from_records([_record(quality="degraded")])
    assert samples[0].degraded is True


def test_maps_null_value_to_none_without_dropping_the_record() -> None:
    """Пропуск (value=None, например отрицательный отсчёт у источника) не
    отбрасывается на этой границе — main-prompt.md §2 «пропуск не
    заменяется ... значением», отдельно от «пропуск не выбрасывается
    молча»: вызывающая сторона (assess_goes_classification) должна САМА
    решить, что с ним делать (не засчитывать в покрытие)."""
    samples = goes_samples_from_records([_record(value=None, unit=None)])
    assert len(samples) == 1
    assert samples[0].value is None


def test_sorts_samples_by_observed_at() -> None:
    samples = goes_samples_from_records(
        [
            _record(record_id="later", observed_at="2024-05-10T12:10:00Z"),
            _record(record_id="earlier", observed_at="2024-05-10T12:00:00Z"),
        ]
    )
    assert [s.record_id for s in samples] == ["earlier", "later"]


def test_rejects_record_from_a_different_source() -> None:
    """Суточная вероятность внешнего прогноза (или любой другой источник) не
    должна быть тихо интерпретирована как наблюдение GOES pfu
    (main-prompt.md §4)."""
    with pytest.raises(UnsupportedRecordError):
        goes_samples_from_records([_record(source_id="noaa-swpc-3day-forecast")])


def test_rejects_forecast_record_kind() -> None:
    with pytest.raises(UnsupportedRecordError):
        goes_samples_from_records([_record(record_kind="forecast")])


def test_rejects_non_pfu_unit_for_a_non_null_value() -> None:
    with pytest.raises(UnsupportedRecordError):
        goes_samples_from_records([_record(unit="percent")])


# --------------------------------------------------------------------------
# assess_goes_classification — покрытие, устаревание, недоступность
# --------------------------------------------------------------------------


WINDOW_START = datetime(2024, 5, 10, 12, 0, tzinfo=UTC)
WINDOW_END = datetime(2024, 5, 10, 13, 0, tzinfo=UTC)  # 1 час — минимум постановки


def test_no_samples_and_source_healthy_is_missing_data() -> None:
    assessment = assess_goes_classification(
        [],
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        now=WINDOW_START,
        critical_staleness_seconds=CRITICAL_STALENESS_SECONDS,
        source_currently_unavailable=False,
    )
    assert assessment.status == "missing_data"
    assert assessment.max_level is None
    assert assessment.exceedance_hours_by_level is None
    assert assessment.critical_gap is True
    assert assessment.record_ids == ()


def test_no_samples_and_this_attempt_failed_is_source_error() -> None:
    """FN-38 приёмка п.3: таймаут/квота этой самой попытки, без ранее
    сохранённых отсчётов, — явный ``source_error``, не то же самое, что
    «источник просто ещё ни разу не получен»."""
    assessment = assess_goes_classification(
        [],
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        now=WINDOW_START,
        critical_staleness_seconds=CRITICAL_STALENESS_SECONDS,
        source_currently_unavailable=True,
    )
    assert assessment.status == "source_error"
    assert assessment.max_level is None
    assert assessment.critical_gap is True


def test_freshest_sample_older_than_critical_staleness_is_stale_data() -> None:
    samples = [_sample(observed_at=WINDOW_START, value=500.0, record_id="rec-1")]
    now = WINDOW_START + timedelta(seconds=CRITICAL_STALENESS_SECONDS)
    assessment = assess_goes_classification(
        samples,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        now=now,
        critical_staleness_seconds=CRITICAL_STALENESS_SECONDS,
    )
    assert assessment.status == "stale_data"
    assert assessment.max_level is None
    assert assessment.exceedance_hours_by_level is None
    assert assessment.critical_gap is True
    assert assessment.record_ids == ()


def test_freshest_sample_just_under_critical_staleness_is_ok() -> None:
    samples = [_sample(observed_at=WINDOW_START, value=5.0, record_id="rec-1")]
    now = WINDOW_START + timedelta(
        seconds=CRITICAL_STALENESS_SECONDS - 1
    )
    assessment = assess_goes_classification(
        samples,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        now=now,
        critical_staleness_seconds=CRITICAL_STALENESS_SECONDS,
    )
    assert assessment.status == "ok"
    assert assessment.max_level == "background"


def test_window_entirely_in_the_future_relative_to_now_is_missing_data() -> None:
    """Наблюдение не покрывает будущее (main-prompt.md §4, применено к
    наблюдению, а не к прогнозу) — окно, целиком после ``now``, не может
    получить status=ok просто потому что источник в целом свежий."""
    samples = [_sample(observed_at=WINDOW_START, value=5.0, record_id="rec-1")]
    now = WINDOW_START  # ровно на границе начала окна, окно всё ещё в будущем
    assessment = assess_goes_classification(
        samples,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        now=now,
        critical_staleness_seconds=CRITICAL_STALENESS_SECONDS,
    )
    assert assessment.status == "missing_data"
    assert assessment.max_level is None


def test_only_gap_samples_intersecting_window_is_missing_data() -> None:
    samples = [_sample(observed_at=WINDOW_START, value=None, record_id="rec-1")]
    now = WINDOW_START + timedelta(minutes=30)
    assessment = assess_goes_classification(
        samples,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        now=now,
        critical_staleness_seconds=CRITICAL_STALENESS_SECONDS,
    )
    assert assessment.status == "missing_data"
    assert assessment.max_level is None


def test_full_window_coverage_by_a_single_background_sample_has_no_critical_gap() -> None:
    samples = [_sample(observed_at=WINDOW_START, value=2.0, record_id="rec-1")]
    now = WINDOW_END  # отсчёт удерживается до конца окна включительно
    assessment = assess_goes_classification(
        samples,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        now=now,
        # Окно ровно 1 час — то же, что и критический порог устаревания в
        # проде (sources.yaml: 3600с); здесь порог намеренно больше, чтобы
        # тест проверял покрытие/exceedance, а не устаревание (у того —
        # свои тесты выше).
        critical_staleness_seconds=CRITICAL_STALENESS_SECONDS * 2,
    )
    assert assessment.status == "ok"
    assert assessment.max_level == "background"
    assert assessment.exceedance_hours_by_level == {"S1": 0.0, "S2": 0.0, "S3": 0.0}
    assert assessment.coverage_fraction == pytest.approx(1.0)
    assert assessment.critical_gap is False
    assert assessment.record_ids == ("rec-1",)


def test_rising_event_across_s1_boundary_with_a_gap_sample() -> None:
    """Воспроизводит фикстуру ``tests/fixtures/sources/swpc/integral-protons-1-day.sample.json``
    (реальная форма события 10 мая 2024 AR3664): 4.271 (фон), 9.845 (фон, у
    самой границы), 15.62 (S1), 22.18 (S1, degraded), пропуск, 31.44 (S1).
    """
    samples = [
        _sample(observed_at=datetime(2024, 5, 10, 12, 0, tzinfo=UTC), value=4.271, record_id="r0"),
        _sample(observed_at=datetime(2024, 5, 10, 12, 5, tzinfo=UTC), value=9.845, record_id="r1"),
        _sample(observed_at=datetime(2024, 5, 10, 12, 10, tzinfo=UTC), value=15.62, record_id="r2"),
        _sample(
            observed_at=datetime(2024, 5, 10, 12, 15, tzinfo=UTC),
            value=22.18,
            record_id="r3",
            degraded=True,
        ),
        _sample(observed_at=datetime(2024, 5, 10, 12, 20, tzinfo=UTC), value=None, record_id="r4"),
        _sample(observed_at=datetime(2024, 5, 10, 12, 25, tzinfo=UTC), value=31.44, record_id="r5"),
    ]
    now = datetime(2024, 5, 10, 12, 26, tzinfo=UTC)

    assessment = assess_goes_classification(
        samples,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        now=now,
        critical_staleness_seconds=CRITICAL_STALENESS_SECONDS,
    )

    assert assessment.status == "ok"
    assert assessment.max_level == "S1"
    # S1-или-выше: [12:10,12:15) + [12:15,12:20) + [12:25,12:26) = 11 минут.
    assert assessment.exceedance_hours_by_level["S1"] == pytest.approx(11 / 60)
    assert assessment.exceedance_hours_by_level["S2"] == 0.0
    assert assessment.exceedance_hours_by_level["S3"] == 0.0
    # Покрыто наблюдением: 5+5+5+5+1 = 21 минута из 60 (пропуск и будущее —
    # некрытые).
    assert assessment.coverage_fraction == pytest.approx(21 / 60)
    assert assessment.critical_gap is True
    assert set(assessment.record_ids) == {"r0", "r1", "r2", "r3", "r5"}  # r4 — пропуск, исключён
    assert any("degraded" in note or "yaw-flip" in note for note in assessment.notes)


@pytest.mark.parametrize(
    "value_pfu,expected_level",
    [(50.0, "S1"), (500.0, "S2"), (5000.0, "S3")],
)
def test_max_level_reflects_the_highest_covered_level(
    value_pfu: float, expected_level: str
) -> None:
    samples = [_sample(observed_at=WINDOW_START, value=value_pfu, record_id="rec-1")]
    now = WINDOW_END
    assessment = assess_goes_classification(
        samples,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        now=now,
        critical_staleness_seconds=CRITICAL_STALENESS_SECONDS * 2,  # см. тест покрытия выше
    )
    assert assessment.status == "ok"
    assert assessment.max_level == expected_level


def test_rejects_naive_datetimes() -> None:
    with pytest.raises(ValueError):
        assess_goes_classification(
            [],
            window_start=WINDOW_START.replace(tzinfo=None),
            window_end=WINDOW_END,
            now=WINDOW_START,
            critical_staleness_seconds=CRITICAL_STALENESS_SECONDS,
        )


def test_rejects_non_positive_window() -> None:
    with pytest.raises(ValueError):
        assess_goes_classification(
            [],
            window_start=WINDOW_END,
            window_end=WINDOW_START,
            now=WINDOW_START,
            critical_staleness_seconds=CRITICAL_STALENESS_SECONDS,
        )


# --------------------------------------------------------------------------
# to_mechanism_assessment_dict — форма contracts/result.schema.json
# --------------------------------------------------------------------------


def test_to_mechanism_assessment_dict_shape_when_ok() -> None:
    samples = [_sample(observed_at=WINDOW_START, value=15.0, record_id="rec-1")]
    assessment = assess_goes_classification(
        samples,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        now=WINDOW_END,
        critical_staleness_seconds=CRITICAL_STALENESS_SECONDS * 2,  # см. тест покрытия выше
    )
    payload = to_mechanism_assessment_dict(assessment)
    assert payload["mechanism"] == "space_weather"
    assert payload["status"] == "ok"
    assert payload["max_level"] == "S1"
    assert set(payload["exceedance_hours_by_level"]) == {"S1", "S2", "S3"}
    assert isinstance(payload["notes"], list)
    assert isinstance(payload["record_ids"], list)


def test_to_mechanism_assessment_dict_nulls_level_fields_when_not_ok() -> None:
    assessment = assess_goes_classification(
        [],
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        now=WINDOW_START,
        critical_staleness_seconds=CRITICAL_STALENESS_SECONDS,
    )
    payload = to_mechanism_assessment_dict(assessment)
    assert payload["status"] == "missing_data"
    assert payload["max_level"] is None
    assert payload["exceedance_hours_by_level"] is None
