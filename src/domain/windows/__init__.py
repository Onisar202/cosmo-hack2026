"""Сравнение окон ВКД и правило доминирования (v2, FN-34)."""

from src.domain.windows.dominance import (
    REQUIRED_MECHANISMS,
    DominanceRecommendation,
    Mechanism,
    MechanismComparison,
    MechanismVerdict,
    PairVerdict,
    PairwiseComparison,
    RecommendationStatus,
    WindowCandidate,
    WindowComparisonError,
    WindowMechanismInput,
    compare_mechanism,
    compare_window_pair,
    excluded_windows,
    recommend,
)

__all__ = [
    "REQUIRED_MECHANISMS",
    "DominanceRecommendation",
    "Mechanism",
    "MechanismComparison",
    "MechanismVerdict",
    "PairVerdict",
    "PairwiseComparison",
    "RecommendationStatus",
    "WindowCandidate",
    "WindowComparisonError",
    "WindowMechanismInput",
    "compare_mechanism",
    "compare_window_pair",
    "excluded_windows",
    "recommend",
]
