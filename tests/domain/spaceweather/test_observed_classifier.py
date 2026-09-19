"""FN-38 (S2-08): классификация НАБЛЮДАЕМОГО потока протонов GOES по шкале
S NOAA (main-prompt.md §11, Механизм 1).

Чистые доменные тесты (.ai/main-prompt.md §9): без сети и без хранилища,
записи строятся вручную в форме ``contracts/record.schema.json``, как их
отдаёт ``src.store.select_observed_range`` (см.
tests/store/test_observed_range.py для самой выборки и tests/api для
production-пути через ``src/api/service.py``).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.domain.spaceweather.observed_classifier import (
    ConflictingObservationsError,
    ObservedProtonSample,
    UnsupportedRecordError,
    assess_observed_flux,
    classify_level,
    observed_proton_samples_from_records,
)

UTC = timezone.utc


def _record(
    *,
    record_id: str,
    observed_at: datetime,
    value: float | None,
    quality: str = "nominal",
    source_id: str = "noaa-swpc-proton-flux",
    record_kind: str = "observation",
    unit: str | None = "pfu",
    provider_record_id: str | None = None,
    fetched_at: datetime | None = None,
) -> dict[str, object]:
    return {
        "record_id": record_id,
        "source_id": source_id,
        "record_kind": record_kind,
        "observed_at": observed_at.isoformat(),
        "value": value,
        "unit": unit if value is not None else None,
        "quality": quality,
        # По умолчанию — свой собственный provider_record_id/fetched_at (как
        # будто это единственная известная версия этой записи): большинство
        # тестов этого файла не проверяют версионирование само по себе, см.
        # test_later_version_of_the_same_provider_record_replaces_the_earlier_one
        # и test_conflicting_provider_records_at_the_same_observed_at_raise.
        "provider_record_id": provider_record_id if provider_record_id is not None else record_id,
        "fetched_at": (fetched_at if fetched_at is not None else observed_at).isoformat(),
    }


def _sample(
    *,
    observed_at: datetime,
    value: float | None,
    quality: str = "nominal",
    record_id: str = "r",
    provider_record_id: str | None = None,
    fetched_at: datetime | None = None,
) -> ObservedProtonSample:
    return ObservedProtonSample(
        observed_at=observed_at,
        value=value,
        quality=quality,
        record_id=record_id,
        provider_record_id=provider_record_id if provider_record_id is not None else record_id,
        fetched_at=fetched_at if fetched_at is not None else observed_at,
    )


# --------------------------------------------------------------------------
# classify_level — пороги main-prompt.md §11 (фон/S1/S2/S3), включительно снизу
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0.0, "background"),
        (9.999, "background"),
        (10.0, "S1"),
        (10.001, "S1"),
        (99.999, "S1"),
        (100.0, "S2"),
        (100.001, "S2"),
        (999.999, "S2"),
        (1000.0, "S3"),
        (50_000.0, "S3"),
    ],
)
def test_classify_level_boundaries(value: float, expected: str) -> None:
    assert classify_level(value) == expected


# --------------------------------------------------------------------------
# observed_proton_samples_from_records — форма записи
# --------------------------------------------------------------------------


def test_samples_from_records_keeps_missing_reading_as_none_not_zero() -> None:
    """main-prompt.md §2: отсутствующее значение не заменяется нулём."""
    records = [
        _record(record_id="r1", observed_at=datetime(2024, 5, 10, 12, 0, tzinfo=UTC), value=None)
    ]

    samples = observed_proton_samples_from_records(records)

    assert samples[0].value is None
    assert samples[0].value != 0


def test_samples_from_records_rejects_wrong_source_id() -> None:
    records = [
        _record(
            record_id="r1",
            observed_at=datetime(2024, 5, 10, 12, 0, tzinfo=UTC),
            value=15.0,
            source_id="nasa-ccmc-donki",
        )
    ]
    with pytest.raises(UnsupportedRecordError):
        observed_proton_samples_from_records(records)


def test_samples_from_records_rejects_forecast_record_kind() -> None:
    """Наблюдение GOES pfu и суточный прогноз NOAA 3-Day — разные величины
    одного механизма (main-prompt.md §4); эта функция не должна тихо принять
    чужую запись."""
    records = [
        _record(
            record_id="r1",
            observed_at=datetime(2024, 5, 10, 12, 0, tzinfo=UTC),
            value=15.0,
            record_kind="forecast",
        )
    ]
    with pytest.raises(UnsupportedRecordError):
        observed_proton_samples_from_records(records)


def test_samples_from_records_rejects_wrong_unit() -> None:
    records = [
        _record(
            record_id="r1", observed_at=datetime(2024, 5, 10, 12, 0, tzinfo=UTC), value=15.0
        )
    ]
    records[0]["unit"] = "percent"
    with pytest.raises(UnsupportedRecordError):
        observed_proton_samples_from_records(records)


# --------------------------------------------------------------------------
# assess_observed_flux — покрытие, уровни, exceedance
# --------------------------------------------------------------------------

WINDOW_START = datetime(2024, 5, 10, 12, 0, tzinfo=UTC)
WINDOW_END = datetime(2024, 5, 10, 13, 0, tzinfo=UTC)  # 1 час
LONG_AFTER_WINDOW = datetime(2024, 5, 10, 18, 0, tzinfo=UTC)  # now — окно давно в прошлом


def test_full_coverage_with_dense_samples_is_not_a_critical_gap() -> None:
    """12 отсчётов каждые 5 минут (hold_seconds=300с) на весь час окна —
    соседние реальные отсчёты образуют СПЛОШНОЕ покрытие без экстраполяции
    за пределы последнего реального отсчёта (см. следующий тест на разрыв)."""
    samples = [
        _sample(
            observed_at=WINDOW_START.replace(minute=minute),
            value=150.0 if minute == 15 else 15.0,
            record_id=f"s{minute}",
        )
        for minute in range(0, 60, 5)
    ]

    assessment = assess_observed_flux(
        samples, window_start=WINDOW_START, window_end=WINDOW_END,
        now=LONG_AFTER_WINDOW, hold_seconds=300.0,
    )

    assert assessment.critical_gap is False
    assert assessment.coverage_fraction == pytest.approx(1.0)
    assert assessment.beyond_horizon is False
    assert assessment.max_level == "S2"  # 150 pfu в 12:15-12:20
    # Весь час >= 10 pfu (S1); только сегмент 12:15-12:20 (5 мин = 1/12 ч) >= 100 pfu (S2).
    assert assessment.exceedance_hours_by_level["S1"] == pytest.approx(1.0)
    assert assessment.exceedance_hours_by_level["S2"] == pytest.approx(5 / 60)
    assert assessment.exceedance_hours_by_level["S3"] == pytest.approx(0.0)
    assert set(assessment.record_ids) == {f"s{minute}" for minute in range(0, 60, 5)}


def test_gap_between_samples_longer_than_hold_is_not_bridged() -> None:
    """main-prompt.md §11: «сохранение последнего наблюдения на весь
    горизонт» — явно названный наивный БАЗОВЫЙ метод для сравнения, не то,
    что делает эта реализация. Разрыв больше hold_seconds не заполняется."""
    samples = [
        _sample(observed_at=WINDOW_START, value=500.0, record_id="s0"),
        # Следующий реальный отсчёт — только через 40 минут, hold=300с=5мин:
        # покрытие первого отсчёта обрывается в 12:05, задолго до второго.
        _sample(
            observed_at=WINDOW_START.replace(minute=40), value=500.0, record_id="s40"
        ),
    ]

    assessment = assess_observed_flux(
        samples, window_start=WINDOW_START, window_end=WINDOW_END,
        now=LONG_AFTER_WINDOW, hold_seconds=300.0,
    )

    assert assessment.critical_gap is True
    assert assessment.coverage_fraction < 1.0
    # Каждый из двух отсчётов покрывает только hold_seconds=5 мин вперёд от
    # своего observed_at (второй — не имеет следующего отсчёта, до конца окна
    # не продлевается): 5 + 5 = 10 мин из 60.
    assert assessment.coverage_fraction == pytest.approx(10 / 60)


def test_missing_reading_creates_a_gap_not_a_favorable_value() -> None:
    samples = [
        _sample(observed_at=WINDOW_START, value=9.0, record_id="s0"),
        _sample(observed_at=WINDOW_START.replace(minute=5), value=None, record_id="s5"),
        _sample(observed_at=WINDOW_START.replace(minute=10), value=9.0, record_id="s10"),
    ]

    assessment = assess_observed_flux(
        samples, window_start=WINDOW_START, window_end=WINDOW_END,
        now=LONG_AFTER_WINDOW, hold_seconds=300.0,
    )

    assert assessment.critical_gap is True
    # Пропущенный отсчёт не создаёт сегмент вовсе — ни фон, ни S1, ничего.
    assert "s5" not in assessment.record_ids


def test_window_entirely_in_the_future_is_beyond_horizon() -> None:
    """GOES — мгновенное наблюдение, не прогноз: часть окна после ``now`` —
    «не покрыто», main-prompt.md §4."""
    now = WINDOW_START  # now ровно в начале окна — всё окно ещё не наступило
    samples: list[ObservedProtonSample] = []

    assessment = assess_observed_flux(
        samples, window_start=WINDOW_START, window_end=WINDOW_END, now=now, hold_seconds=300.0,
    )

    assert assessment.beyond_horizon is True
    assert assessment.critical_gap is True
    assert assessment.coverage_fraction == 0.0
    assert assessment.max_level is None
    assert any("горизонт" in note for note in assessment.notes)


def test_segment_never_extends_past_now_even_with_hold() -> None:
    """Отсчёт незадолго до ``now`` не «утекает» покрытием в будущее — даже
    если ``hold_seconds`` формально позволил бы продлить его дальше."""
    now = WINDOW_START.replace(minute=10)
    samples = [_sample(observed_at=WINDOW_START, value=5.0, record_id="s0")]

    assessment = assess_observed_flux(
        samples, window_start=WINDOW_START, window_end=WINDOW_END, now=now, hold_seconds=3600.0,
    )

    # Покрытие: 12:00 (observed_at) .. 12:10 (now) = 10 минут, не hold_seconds=1ч.
    assert assessment.coverage_fraction == pytest.approx(10 / 60)
    assert assessment.beyond_horizon is True  # окно продолжается за 12:10


def test_degraded_quality_is_used_and_noted_not_excluded() -> None:
    """sources.yaml: yaw-flip помечает отсчёт degraded — менее надёжен, но
    не отсутствует (main-prompt.md §2 «заполненный пропуск помечается как
    заполненный», здесь — обратный случай: НЕ-пропуск не маскируется под
    пропуск)."""
    samples = [
        _sample(observed_at=WINDOW_START, value=15.0, quality="degraded", record_id="s0"),
    ]

    assessment = assess_observed_flux(
        samples, window_start=WINDOW_START, window_end=WINDOW_START.replace(minute=5),
        now=LONG_AFTER_WINDOW, hold_seconds=300.0,
    )

    assert assessment.max_level == "S1"
    assert "s0" in assessment.record_ids
    assert any("degraded" in note or "yaw-flip" in note for note in assessment.notes)


def test_later_version_of_the_same_provider_record_replaces_the_earlier_one() -> None:
    """round 1 ревью PR #29 (🚨): ``select_observed_range`` (в отличие от
    ``select_as_of``) возвращает ВСЕ версии одного ``provider_record_id`` —
    источник документирует возможность пересчёта отсчёта (sources.yaml →
    space_weather[0].quality_notes). Позднее исправление ошибочного 1000 pfu
    на настоящие 5 pfu обязано ЗАМЕНИТЬ старую версию, не усредниться и не
    проиграть «эвристике максимума» — иначе расчёт продолжал бы показывать
    ложный S3 после того, как источник сам его исправил."""
    erroneous = _sample(
        observed_at=WINDOW_START, value=1000.0, record_id="v1",
        provider_record_id="18:2024-05-10T12:00:00+00:00",
        fetched_at=WINDOW_START,
    )
    corrected = _sample(
        observed_at=WINDOW_START, value=5.0, record_id="v2",
        provider_record_id="18:2024-05-10T12:00:00+00:00",
        fetched_at=WINDOW_START.replace(minute=30),  # получено позже — новая версия
    )

    assessment = assess_observed_flux(
        [erroneous, corrected], window_start=WINDOW_START,
        window_end=WINDOW_START.replace(minute=5),
        now=LONG_AFTER_WINDOW, hold_seconds=300.0,
    )

    assert assessment.max_level == "background"  # 5 pfu, не 1000 (ложный S3)
    assert assessment.record_ids == ("v2",)

    # Порядок появления в выборке не должен влиять на исход.
    assessment_reversed = assess_observed_flux(
        [corrected, erroneous], window_start=WINDOW_START,
        window_end=WINDOW_START.replace(minute=5),
        now=LONG_AFTER_WINDOW, hold_seconds=300.0,
    )
    assert assessment_reversed.max_level == "background"
    assert assessment_reversed.record_ids == ("v2",)


def test_conflicting_provider_records_at_the_same_observed_at_raise() -> None:
    """После выбора последней версии КАЖДОЙ записи поставщика (тест выше)
    две РАЗНЫЕ записи поставщика на один и тот же момент — не то же самое,
    что версии одной записи: источник задокументирован как единственный
    «первичный» спутник (sources.yaml → space_weather[0].independence_note),
    задокументированного правила выбора между двумя разными спутниками нет
    — main-prompt.md §2 требует делать эту неоднозначность видимой, не
    решать её эвристикой «взять большее значение» (round 1 ревью PR #29)."""
    same_moment = WINDOW_START
    samples = [
        _sample(
            observed_at=same_moment, value=50.0, record_id="low",
            provider_record_id="18:2024-05-10T12:00:00+00:00",
        ),
        _sample(
            observed_at=same_moment, value=500.0, record_id="high",
            provider_record_id="99:2024-05-10T12:00:00+00:00",
        ),
    ]

    with pytest.raises(ConflictingObservationsError):
        assess_observed_flux(
            samples, window_start=WINDOW_START, window_end=WINDOW_START.replace(minute=5),
            now=LONG_AFTER_WINDOW, hold_seconds=300.0,
        )


def test_rejects_naive_window_or_now() -> None:
    with pytest.raises(ValueError):
        assess_observed_flux(
            [], window_start=WINDOW_START.replace(tzinfo=None), window_end=WINDOW_END,
            now=LONG_AFTER_WINDOW, hold_seconds=300.0,
        )
