"""Тесты правила доминирования v2 для сравнения окон ВКД (FN-34, S2-04).

Чистые тесты без сети/хранилища (main-prompt.md §8) — вход строится вручную
как ``mechanismAssessment``-подобные словари (contracts/result.schema.json),
как их видел бы ``src/domain/windows`` после того, как
``src/domain/spaceweather``/``src/domain/mmod`` уже оценили каждый механизм.

Соответствие приёмке FN-34, п.2 (обязательные тестовые сценарии):
A доминирует B / конфликт факторов / равенство / оба окна неполны / один
критический пробел / unknown-missing / перенос начала / изменение
длительности / горизонт меньше конца окна — каждому посвящён один или
несколько тестов ниже, с явной пометкой в имени/докстринге.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.domain.windows import (
    DominanceRecommendation,
    WindowCandidate,
    WindowComparisonError,
    compare_mechanism,
    compare_window_pair,
    excluded_windows,
    recommend,
)
from src.domain.windows.dominance import WindowMechanismInput


def _sw(
    *,
    status: str = "ok",
    max_level: str | None = "S1",
    exceedance: dict[str, float] | None = None,
    critical_gap: bool = False,
) -> dict[str, Any]:
    return {
        "mechanism": "space_weather",
        "status": status,
        "max_level": max_level,
        "exceedance_hours_by_level": (
            exceedance if exceedance is not None else {"S1": 1.0, "S2": 0.0, "S3": 0.0}
        )
        if status == "ok"
        else None,
        "coverage_fraction": 1.0,
        "critical_gap": critical_gap,
        "notes": [],
        "record_ids": ["rec-sw-1"],
    }


def _mmod(
    *,
    status: str = "ok",
    max_level: str | None = "background",
    exceedance: dict[str, float] | None = None,
    critical_gap: bool = False,
) -> dict[str, Any]:
    return {
        "mechanism": "mmod",
        "status": status,
        "max_level": max_level,
        "exceedance_hours_by_level": (
            exceedance if exceedance is not None else {"elevated": 0.0, "pronounced": 0.0}
        )
        if status == "ok"
        else None,
        "coverage_fraction": 1.0,
        "critical_gap": critical_gap,
        "notes": [],
        "record_ids": ["rec-mmod-1"],
    }


def _window(
    window_id: str, *, duration_hours: float = 6.0, sw: dict[str, Any], mmod: dict[str, Any]
) -> WindowCandidate:
    return WindowCandidate.from_assessments(window_id, duration_hours, [sw, mmod])


# ---------------------------------------------------------------------------
# 1. A доминирует B
# ---------------------------------------------------------------------------


def test_a_dominates_b_strictly_better_on_one_mechanism_equal_on_the_other() -> None:
    a = _window(
        "win-a",
        sw=_sw(max_level="S1", exceedance={"S1": 1.0, "S2": 0.0, "S3": 0.0}),
        mmod=_mmod(max_level="background"),
    )
    b = _window(
        "win-b",
        sw=_sw(max_level="S2", exceedance={"S1": 3.0, "S2": 1.0, "S3": 0.0}),
        mmod=_mmod(max_level="background"),
    )
    pair = compare_window_pair(a, b)
    assert pair.verdict == "a_dominates_b"

    result = recommend([a, b])
    assert result == DominanceRecommendation("selected", "win-a", result.explanation)


def test_dominance_requires_not_worse_on_every_comparable_metric_not_just_max_level() -> None:
    """A имеет меньший max_level, но БОЛЬШУЮ длительность превышения S1 —
    A не доминирует B несмотря на "меньший максимальный уровень", в отличие
    от старого лексикографического правила, где max_level имел приоритет
    над длительностью."""
    a = _window(
        "win-a",
        sw=_sw(max_level="S1", exceedance={"S1": 5.0, "S2": 0.0, "S3": 0.0}),
        mmod=_mmod(),
    )
    b = _window(
        "win-b",
        sw=_sw(max_level="S2", exceedance={"S1": 1.0, "S2": 0.1, "S3": 0.0}),
        mmod=_mmod(),
    )
    pair = compare_window_pair(a, b)
    assert pair.verdict == "undetermined_difference"

    result = recommend([a, b])
    assert result.status == "insufficient_basis"
    assert result.window_id is None


# ---------------------------------------------------------------------------
# 2. Конфликт механизмов
# ---------------------------------------------------------------------------


def test_conflict_when_each_window_wins_a_different_mechanism() -> None:
    a = _window(
        "win-a",
        sw=_sw(max_level="S1"),  # A лучше по space_weather
        mmod=_mmod(max_level="pronounced", exceedance={"elevated": 4.0, "pronounced": 2.0}),
    )
    b = _window(
        "win-b",
        sw=_sw(max_level="S3", exceedance={"S1": 6.0, "S2": 4.0, "S3": 2.0}),
        mmod=_mmod(max_level="background"),  # B лучше по mmod
    )
    pair = compare_window_pair(a, b)
    assert pair.verdict == "conflict"

    result = recommend([a, b])
    assert result.status == "insufficient_basis"
    assert result.window_id is None
    assert "конфликт" in result.explanation.lower()


# ---------------------------------------------------------------------------
# 3. Равенство
# ---------------------------------------------------------------------------


def test_tie_when_both_windows_are_identical_on_every_mechanism() -> None:
    sw = _sw(max_level="S2", exceedance={"S1": 2.0, "S2": 1.0, "S3": 0.0})
    a = _window("win-a", sw=sw, mmod=_mmod())
    b = _window("win-b", sw=sw, mmod=_mmod())

    pair = compare_window_pair(a, b)
    assert pair.verdict == "tie"

    result = recommend([a, b])
    assert result == DominanceRecommendation("tie", None, result.explanation)


def test_tie_is_invariant_to_window_start_shift() -> None:
    """Перенос начала окна (другой window_id/иной физический интервал, та же
    оценка риска) не должен ничего "улучшать" сам по себе — правило смотрит
    только на переданные показатели, не на идентичность/порядок окон
    (main-prompt.md §12 «перенос не улучшает»)."""
    shared_sw = _sw(max_level="S1", exceedance={"S1": 1.5, "S2": 0.0, "S3": 0.0})
    shared_mmod = _mmod(max_level="elevated", exceedance={"elevated": 1.0, "pronounced": 0.0})
    a = _window("win-a-shifted-by-2h", sw=shared_sw, mmod=shared_mmod)
    b = _window("win-b-original-start", sw=shared_sw, mmod=shared_mmod)

    result = recommend([a, b])
    assert result.status == "tie"


# ---------------------------------------------------------------------------
# 4. Оба окна неполны (оба с критическим пробелом)
# ---------------------------------------------------------------------------


def test_all_windows_excluded_when_both_have_a_critical_gap() -> None:
    a = _window(
        "win-a", sw=_sw(status="missing_data", max_level=None, critical_gap=True), mmod=_mmod()
    )
    b = _window(
        "win-b",
        sw=_sw(),
        mmod=_mmod(status="not_implemented", max_level=None, critical_gap=True),
    )

    result = recommend([a, b])
    assert result == DominanceRecommendation("all_windows_excluded", None, result.explanation)
    assert set(excluded_windows([a, b])) == {"win-a", "win-b"}


# ---------------------------------------------------------------------------
# 5. Один критический пробел
# ---------------------------------------------------------------------------


def test_window_with_critical_gap_is_excluded_but_the_other_is_still_recommended() -> None:
    a = _window(
        "win-a", sw=_sw(status="source_error", max_level=None, critical_gap=True), mmod=_mmod()
    )
    b = _window("win-b", sw=_sw(max_level="S1"), mmod=_mmod())

    exclusions = excluded_windows([a, b])
    assert set(exclusions) == {"win-a"}

    result = recommend([a, b])
    assert result == DominanceRecommendation("selected", "win-b", result.explanation)
    assert "win-a" in result.explanation


def test_excluded_window_remains_visible_not_hidden_from_the_window_list() -> None:
    """Исключение из автоматической рекомендации не скрывает окно
    (main-prompt.md, FN-34 «все окна остаются видимыми») — эта гарантия
    относится к слою API/контракту (окно остаётся элементом
    ``result.windows``); на уровне чистого домена она проявляется как
    отдельная, явная функция ``excluded_windows`` вместо удаления окна
    из входного списка кем-либо в этом модуле."""
    a = _window(
        "win-a", sw=_sw(status="stale_data", max_level=None, critical_gap=True), mmod=_mmod()
    )
    b = _window("win-b", sw=_sw(), mmod=_mmod())
    candidates = [a, b]
    recommend(candidates)
    assert [c.window_id for c in candidates] == ["win-a", "win-b"]


# ---------------------------------------------------------------------------
# 6. unknown/missing (несопоставимость: данные есть только у одной стороны)
# ---------------------------------------------------------------------------


def test_incomparable_when_only_one_window_has_a_ready_assessment_for_a_mechanism() -> None:
    """missing_data БЕЗ критического пробела (частичное, но не критичное
    отсутствие данных) — окно не исключается целиком, но по этому механизму
    сравнение невозможно: неизвестное не считается ни благоприятным, ни
    неблагоприятным (main-prompt.md §2)."""
    a = _window(
        "win-a", sw=_sw(status="missing_data", max_level=None, critical_gap=False), mmod=_mmod()
    )
    b = _window("win-b", sw=_sw(max_level="S1"), mmod=_mmod())

    mechanism_result = compare_mechanism(
        "space_weather", a.mechanisms["space_weather"], b.mechanisms["space_weather"]
    )
    assert mechanism_result.verdict == "incomparable"

    pair = compare_window_pair(a, b)
    assert pair.verdict == "incomparable"

    result = recommend([a, b])
    assert result.status == "insufficient_basis"
    assert result.window_id is None


def test_no_information_when_both_windows_lack_a_ready_assessment_for_a_mechanism() -> None:
    """Оба not_implemented по mmod (сегодняшнее реальное состояние сервиса,
    FN-32) — механизм просто не участвует в решении, это не то же самое,
    что "несопоставимость" (ни одна сторона ничего не утверждает)."""
    a = _window(
        "win-a",
        sw=_sw(max_level="S1"),
        mmod=_mmod(status="not_implemented", max_level=None, critical_gap=False),
    )
    b = _window(
        "win-b",
        sw=_sw(max_level="S2", exceedance={"S1": 3.0, "S2": 1.0, "S3": 0.0}),
        mmod=_mmod(status="not_implemented", max_level=None, critical_gap=False),
    )
    mechanism_result = compare_mechanism("mmod", a.mechanisms["mmod"], b.mechanisms["mmod"])
    assert mechanism_result.verdict == "no_information"

    result = recommend([a, b])
    assert result == DominanceRecommendation("selected", "win-a", result.explanation)


def test_insufficient_basis_when_no_mechanism_has_any_information_at_all() -> None:
    a = _window(
        "win-a",
        sw=_sw(status="not_implemented", max_level=None, critical_gap=False),
        mmod=_mmod(status="not_implemented", max_level=None, critical_gap=False),
    )
    b = _window(
        "win-b",
        sw=_sw(status="not_implemented", max_level=None, critical_gap=False),
        mmod=_mmod(status="not_implemented", max_level=None, critical_gap=False),
    )
    result = recommend([a, b])
    assert result.status == "insufficient_basis"
    assert result.window_id is None


# ---------------------------------------------------------------------------
# 7. Горизонт меньше конца окна (beyond_horizon)
# ---------------------------------------------------------------------------


def test_beyond_horizon_is_treated_as_not_ok_not_as_calm() -> None:
    """Окно частично за горизонтом прогноза — "не покрыто", не "спокойно"
    (main-prompt.md §4). Немаркированный критическим пробелом beyond_horizon
    делает механизм несопоставимым с окном, у которого есть готовая оценка —
    ту же семантику, что и missing_data."""
    a = _window(
        "win-a",
        sw=_sw(status="beyond_horizon", max_level=None, critical_gap=False),
        mmod=_mmod(),
    )
    b = _window("win-b", sw=_sw(max_level="S1"), mmod=_mmod())

    mechanism_result = compare_mechanism(
        "space_weather", a.mechanisms["space_weather"], b.mechanisms["space_weather"]
    )
    assert mechanism_result.verdict == "incomparable"
    assert compare_window_pair(a, b).verdict == "incomparable"


def test_beyond_horizon_marked_as_critical_gap_excludes_the_window() -> None:
    a = _window(
        "win-a",
        sw=_sw(status="beyond_horizon", max_level=None, critical_gap=True),
        mmod=_mmod(),
    )
    b = _window("win-b", sw=_sw(max_level="S1"), mmod=_mmod())
    result = recommend([a, b])
    assert result == DominanceRecommendation("selected", "win-b", result.explanation)


# ---------------------------------------------------------------------------
# 8. Изменение длительности — окна разной длительности сравнивать нельзя
# ---------------------------------------------------------------------------


def test_duration_mismatch_is_rejected_not_silently_compared() -> None:
    a = _window("win-a", duration_hours=4.0, sw=_sw(), mmod=_mmod())
    b = _window("win-b", duration_hours=6.0, sw=_sw(), mmod=_mmod())

    with pytest.raises(WindowComparisonError):
        compare_window_pair(a, b)
    with pytest.raises(WindowComparisonError):
        recommend([a, b])


# ---------------------------------------------------------------------------
# Прочие структурные гарантии
# ---------------------------------------------------------------------------


def test_from_assessments_rejects_missing_mechanism() -> None:
    with pytest.raises(WindowComparisonError):
        WindowCandidate.from_assessments("win-a", 6.0, [_sw()])


def test_from_assessments_rejects_duplicate_mechanism() -> None:
    with pytest.raises(WindowComparisonError):
        WindowCandidate.from_assessments("win-a", 6.0, [_sw(), _sw()])


def test_recommend_requires_at_least_two_windows() -> None:
    a = _window("win-a", sw=_sw(), mmod=_mmod())
    with pytest.raises(WindowComparisonError):
        recommend([a])


def test_three_windows_one_dominates_others_despite_them_conflicting_with_each_other() -> None:
    a = _window("win-a", sw=_sw(max_level="S1"), mmod=_mmod(max_level="background"))
    b = _window(
        "win-b",
        sw=_sw(max_level="S2", exceedance={"S1": 4.0, "S2": 1.0, "S3": 0.0}),
        mmod=_mmod(max_level="pronounced", exceedance={"elevated": 3.0, "pronounced": 1.0}),
    )
    c = _window(
        "win-c",
        sw=_sw(max_level="S3", exceedance={"S1": 6.0, "S2": 4.0, "S3": 1.0}),
        mmod=_mmod(max_level="background"),
    )
    # b против c: b хуже по space_weather, лучше по mmod -> конфликт между ними,
    # но a не хуже и строго лучше обоих по обоим механизмам.
    assert compare_window_pair(b, c).verdict == "conflict"
    result = recommend([a, b, c])
    assert result == DominanceRecommendation("selected", "win-a", result.explanation)


def test_mechanism_comparison_rejects_unknown_level() -> None:
    a = WindowMechanismInput("space_weather", "ok", "S9", {"S1": 0.0, "S2": 0.0, "S3": 0.0}, False)
    b = WindowMechanismInput(
        "space_weather", "ok", "S1", {"S1": 0.0, "S2": 0.0, "S3": 0.0}, False
    )
    with pytest.raises(WindowComparisonError):
        compare_mechanism("space_weather", a, b)
