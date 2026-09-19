"""Сравнение окон ВКД: правило доминирования v2 (FN-34, S2-04).

Заменяет прежнее лексикографическое правило предпочтения
(.ai/main-prompt.md §11 «Правило предпочтения окон» в его старой редакции)
на частичный порядок без каскада приоритетов между механизмами:

- окно A доминирует окно B, только если A не хуже B по всем пригодным
  сопоставимым показателям и устойчиво лучше хотя бы по одному;
- конфликт механизмов (один механизм за A, другой за B), равенство,
  несопоставимость (данные есть только у одной стороны), неопределённое
  различие (внутри одного механизма показатели расходятся разнонаправленно)
  или критический пробел данных — во всех этих случаях автоматического
  победителя нет;
- ни одно окно не скрывается: критический пробел выводит окно из
  СРАВНЕНИЯ (``excluded_from_comparison``), а не из списка окон результата.

Чистый расчётный модуль (.ai/main-prompt.md §8): принимает уже готовые
``mechanismAssessment``-подобные значения (contracts/result.schema.json) и
ничего не пересчитывает — реальные уровни/длительности превышения даёт
``src/domain/spaceweather``/``src/domain/mmod``, физическая дедупликация
связанных сигналов одного события (main-prompt.md §4) — их забота, не эта.
Здесь не читаются ни сеть, ни хранилище, ни системное «сейчас».

Ранжирование уровней внутри механизма не подбирается произвольно — это те
же названия, что и ``exceedance_hours_by_level``/``max_level`` контракта
(.ai/main-prompt.md §11: шкала S NOAA для space_weather, эвристика команды
Фон/Повышенный/Выраженный для mmod, здесь — ``background``/``elevated``/
``pronounced`` теми же словами, что и ключи ``exceedance_hours_by_level``
в contracts/result.schema.json).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

Mechanism = Literal["space_weather", "mmod"]
MechanismVerdict = Literal[
    "a_better", "b_better", "equal", "incomparable", "undetermined_difference", "no_information"
]
PairVerdict = Literal[
    "a_dominates_b",
    "b_dominates_a",
    "tie",
    "conflict",
    "incomparable",
    "undetermined_difference",
]
RecommendationStatus = Literal["selected", "tie", "insufficient_basis", "all_windows_excluded"]

#: Возрастающий порядок серьёзности уровня для каждого механизма — используется
#: только для сравнения "не хуже/лучше", не для арифметики над самим уровнем.
_LEVEL_ORDER: dict[Mechanism, tuple[str, ...]] = {
    "space_weather": ("background", "S1", "S2", "S3"),
    "mmod": ("background", "elevated", "pronounced"),
}

#: Ключи ``exceedance_hours_by_level``, обязательные контрактом для этого
#: механизма при ``status == "ok"`` (contracts/result.schema.json).
_EXCEEDANCE_KEYS: dict[Mechanism, tuple[str, ...]] = {
    "space_weather": ("S1", "S2", "S3"),
    "mmod": ("elevated", "pronounced"),
}

REQUIRED_MECHANISMS: tuple[Mechanism, ...] = ("space_weather", "mmod")


class WindowComparisonError(ValueError):
    """Входные данные нарушают предпосылку сравнения окон (разная
    длительность, отсутствующий/дублирующийся механизм) — это ошибка
    вызывающей стороны, не отсутствие данных о риске, поэтому не
    представляется как «оценить невозможно»."""


@dataclass(frozen=True)
class WindowMechanismInput:
    """Минимум, нужный правилу доминирования от одной ``mechanismAssessment``
    (contracts/result.schema.json) — без ``coverage_fraction``: бо́льшая
    полнота данных при тех же показателях риска не делает окно «лучше» (в
    отличие от старого лексикографического правила, где полнота была
    последним шагом каскада) — иначе окно с более полными, но одинаково
    тревожными данными искусственно проигрывало бы окну с меньшим
    покрытием, хотя различий в самом риске нет. Полнота остаётся видимой в
    результате (contracts), просто не участвует в этом решении."""

    mechanism: Mechanism
    status: str
    max_level: str | None
    exceedance_hours_by_level: Mapping[str, float] | None
    critical_gap: bool

    @classmethod
    def from_assessment(cls, assessment: Mapping[str, Any]) -> WindowMechanismInput:
        mechanism = assessment["mechanism"]
        if mechanism not in REQUIRED_MECHANISMS:
            raise WindowComparisonError(f"unknown mechanism {mechanism!r}")
        return cls(
            mechanism=mechanism,
            status=str(assessment["status"]),
            max_level=assessment.get("max_level"),
            exceedance_hours_by_level=assessment.get("exceedance_hours_by_level"),
            critical_gap=bool(assessment["critical_gap"]),
        )


@dataclass(frozen=True)
class WindowCandidate:
    """Одно окно ВКД, готовое к сравнению: ровно по одной оценке на каждый
    обязательный механизм (contracts/result.schema.json: ``window.mechanisms``,
    ровно 2 — по одной на ``space_weather`` и ``mmod``)."""

    window_id: str
    duration_hours: float
    mechanisms: Mapping[Mechanism, WindowMechanismInput]

    @classmethod
    def from_assessments(
        cls,
        window_id: str,
        duration_hours: float,
        mechanisms: Iterable[Mapping[str, Any]],
    ) -> WindowCandidate:
        parsed = [WindowMechanismInput.from_assessment(m) for m in mechanisms]
        by_mechanism = {m.mechanism: m for m in parsed}
        if len(by_mechanism) != len(parsed):
            raise WindowComparisonError(
                f"window {window_id!r} has duplicate mechanism assessments"
            )
        missing = set(REQUIRED_MECHANISMS) - set(by_mechanism)
        if missing:
            raise WindowComparisonError(
                f"window {window_id!r} is missing required mechanisms: {sorted(missing)}"
            )
        return cls(window_id=window_id, duration_hours=duration_hours, mechanisms=by_mechanism)


@dataclass(frozen=True)
class MechanismComparison:
    """Результат сравнения одного механизма между двумя окнами —
    промежуточный шаг, из которого строится итоговый вердикт пары и
    объяснение (О3/О4 «видно, что повлияло на выбор»)."""

    mechanism: Mechanism
    verdict: MechanismVerdict
    detail: str


@dataclass(frozen=True)
class PairwiseComparison:
    a_window_id: str
    b_window_id: str
    verdict: PairVerdict
    mechanism_comparisons: tuple[MechanismComparison, ...]
    explanation: str


@dataclass(frozen=True)
class DominanceRecommendation:
    status: RecommendationStatus
    window_id: str | None
    explanation: str


def _level_rank(mechanism: Mechanism, level: str | None) -> int | None:
    if level is None:
        return None
    order = _LEVEL_ORDER[mechanism]
    if level not in order:
        raise WindowComparisonError(
            f"unknown level {level!r} for mechanism {mechanism!r}, expected one of {order}"
        )
    return order.index(level)


def compare_mechanism(
    mechanism: Mechanism, a: WindowMechanismInput, b: WindowMechanismInput
) -> MechanismComparison:
    """Сравнивает одну ``mechanismAssessment`` между окнами A и B.

    ``coverage_fraction`` намеренно не участвует (см. docstring
    :class:`WindowMechanismInput`). Единственные сопоставимые показатели —
    ранг ``max_level`` и покомпонентно ``exceedance_hours_by_level`` — и
    только когда ОБА окна несут ``status == "ok"`` по этому механизму.
    """
    a_ok = a.status == "ok"
    b_ok = b.status == "ok"

    if not a_ok and not b_ok:
        return MechanismComparison(
            mechanism, "no_information",
            f"{mechanism}: ни у A ({a.status}), ни у B ({b.status}) нет готовой оценки — "
            "механизм не участвует в сравнении.",
        )
    if a_ok != b_ok:
        ok_side, gap_side = ("A", "B") if a_ok else ("B", "A")
        gap_status = b.status if a_ok else a.status
        return MechanismComparison(
            mechanism, "incomparable",
            f"{mechanism}: готовая оценка есть только у окна {ok_side}, у окна {gap_side} — "
            f"status={gap_status!r}; нельзя утверждать, что окно без оценки не хуже или не "
            "лучше — это несопоставимость, а не благоприятная оценка отсутствующей стороны.",
        )

    # Обе стороны status == "ok": контракт требует непустые max_level и
    # exceedance_hours_by_level (contracts/result.schema.json allOf).
    assert a.max_level is not None and b.max_level is not None
    assert a.exceedance_hours_by_level is not None and b.exceedance_hours_by_level is not None

    rank_a = _level_rank(mechanism, a.max_level)
    rank_b = _level_rank(mechanism, b.max_level)
    assert rank_a is not None and rank_b is not None

    keys = _EXCEEDANCE_KEYS[mechanism]
    hours_a = {k: float(a.exceedance_hours_by_level[k]) for k in keys}
    hours_b = {k: float(b.exceedance_hours_by_level[k]) for k in keys}

    a_not_worse = rank_a <= rank_b and all(hours_a[k] <= hours_b[k] for k in keys)
    b_not_worse = rank_b <= rank_a and all(hours_b[k] <= hours_a[k] for k in keys)
    a_strictly_better = a_not_worse and (
        rank_a < rank_b or any(hours_a[k] < hours_b[k] for k in keys)
    )
    b_strictly_better = b_not_worse and (
        rank_b < rank_a or any(hours_b[k] < hours_a[k] for k in keys)
    )

    detail_values = (
        f"{mechanism}: A(max_level={a.max_level}, exceedance={hours_a}) vs "
        f"B(max_level={b.max_level}, exceedance={hours_b})"
    )

    if a_not_worse and b_not_worse:
        return MechanismComparison(mechanism, "equal", f"{detail_values} — идентичны.")
    if a_strictly_better:
        return MechanismComparison(mechanism, "a_better", f"{detail_values} — A не хуже и лучше.")
    if b_strictly_better:
        return MechanismComparison(mechanism, "b_better", f"{detail_values} — B не хуже и лучше.")
    return MechanismComparison(
        mechanism, "undetermined_difference",
        f"{detail_values} — разнонаправленно (один показатель за A, другой за B), "
        "направление различия неопределённо.",
    )


def compare_window_pair(a: WindowCandidate, b: WindowCandidate) -> PairwiseComparison:
    """Полное попарное сравнение двух окон по правилу доминирования v2.

    Требует равной длительности (.ai/main-prompt.md, FN-34 «сокращение
    длительности — изменение плана и полный пересчёт РАВНЫХ по длительности
    окон» — сравнение окон разной длительности этой функцией запрещено, а
    не молча допускается с искажённым смыслом).
    """
    if a.duration_hours != b.duration_hours:
        raise WindowComparisonError(
            f"cannot compare windows of different duration_hours "
            f"({a.window_id}={a.duration_hours} vs {b.window_id}={b.duration_hours}) — "
            "изменение длительности требует полного пересчёта окон равной длительности"
        )

    comparisons = tuple(
        compare_mechanism(mechanism, a.mechanisms[mechanism], b.mechanisms[mechanism])
        for mechanism in REQUIRED_MECHANISMS
    )

    informative = [c for c in comparisons if c.verdict != "no_information"]
    detail_text = " ".join(c.detail for c in comparisons)

    if any(c.verdict == "incomparable" for c in informative):
        verdict: PairVerdict = "incomparable"
    elif any(c.verdict == "undetermined_difference" for c in informative):
        verdict = "undetermined_difference"
    elif not informative:
        # Ни один механизм не даёт готовой оценки ни для одного из окон —
        # не то же самое, что доказанное равенство: оснований нет вовсе.
        verdict = "incomparable"
    elif all(c.verdict == "equal" for c in informative):
        verdict = "tie"
    elif all(c.verdict in ("a_better", "equal") for c in informative):
        verdict = "a_dominates_b"
    elif all(c.verdict in ("b_better", "equal") for c in informative):
        verdict = "b_dominates_a"
    else:
        verdict = "conflict"

    return PairwiseComparison(
        a_window_id=a.window_id,
        b_window_id=b.window_id,
        verdict=verdict,
        mechanism_comparisons=comparisons,
        explanation=(
            f"{a.window_id} vs {b.window_id}: {verdict}. {detail_text}"
        ),
    )


def excluded_windows(candidates: Iterable[WindowCandidate]) -> dict[str, str]:
    """Окна с критическим пробелом данных по ЛЮБОМУ механизму — выводятся из
    сравнения (не из результата целиком), main-prompt.md §11 правило
    предпочтения окон, п.1, сохранено без изменений правилом v2."""
    reasons: dict[str, str] = {}
    for candidate in candidates:
        gapped = [
            mechanism
            for mechanism in REQUIRED_MECHANISMS
            if candidate.mechanisms[mechanism].critical_gap
        ]
        if gapped:
            reasons[candidate.window_id] = (
                "Критический пробел данных по механизму(ам) "
                f"{', '.join(gapped)} — окно выведено из сравнения (не проигрывает по "
                "баллам, main-prompt.md §11, правило предпочтения окон, п.1); окно "
                "остаётся видимым в результате."
            )
    return reasons


def recommend(candidates: Sequence[WindowCandidate]) -> DominanceRecommendation:
    """Правило доминирования v2 (FN-34) над произвольным числом окон РАВНОЙ
    длительности (минимум два, main-prompt.md §12).

    Не является тотальным порядком: возможен результат без победителя даже
    когда ни одно окно не исключено (конфликт/несопоставимость/
    неопределённое различие/равенство) — это осознанное отличие от старого
    лексикографического правила, которое всегда выбирало победителя через
    каскад приоритетов между механизмами.
    """
    if len(candidates) < 2:
        raise WindowComparisonError("at least two windows are required for comparison")
    durations = {c.duration_hours for c in candidates}
    if len(durations) > 1:
        raise WindowComparisonError(
            f"cannot recommend across windows of different duration_hours ({sorted(durations)}) "
            "— duration change requires a full recomputation of equal-duration windows"
        )

    exclusions = excluded_windows(candidates)
    survivors = [c for c in candidates if c.window_id not in exclusions]

    if not survivors:
        explanation = "Все окна исключены из сравнения: " + "; ".join(
            f"{window_id} — {reason}" for window_id, reason in exclusions.items()
        )
        return DominanceRecommendation("all_windows_excluded", None, explanation)

    if len(survivors) == 1:
        only = survivors[0]
        excluded_note = (
            " Остальные окна исключены: "
            + "; ".join(f"{wid} — {reason}" for wid, reason in exclusions.items())
            if exclusions
            else ""
        )
        return DominanceRecommendation(
            "selected",
            only.window_id,
            f"Окно {only.window_id} — единственное без критического пробела данных, "
            f"сравнивать не с чем.{excluded_note}",
        )

    pairwise: dict[tuple[str, str], PairwiseComparison] = {}
    for i, a in enumerate(survivors):
        for b in survivors[i + 1 :]:
            pairwise[(a.window_id, b.window_id)] = compare_window_pair(a, b)

    def _relative_verdict(subject_id: str, other_id: str) -> PairVerdict:
        if (subject_id, other_id) in pairwise:
            return pairwise[(subject_id, other_id)].verdict
        reversed_verdict = pairwise[(other_id, subject_id)].verdict
        flip: dict[PairVerdict, PairVerdict] = {
            "a_dominates_b": "b_dominates_a",
            "b_dominates_a": "a_dominates_b",
            "tie": "tie",
            "conflict": "conflict",
            "incomparable": "incomparable",
            "undetermined_difference": "undetermined_difference",
        }
        return flip[reversed_verdict]

    winners = [
        candidate
        for candidate in survivors
        if all(
            _relative_verdict(candidate.window_id, other.window_id) == "a_dominates_b"
            for other in survivors
            if other.window_id != candidate.window_id
        )
    ]

    all_pair_explanations = "; ".join(c.explanation for c in pairwise.values())
    exclusion_note = (
        " Также исключены: "
        + "; ".join(f"{wid} — {reason}" for wid, reason in exclusions.items())
        if exclusions
        else ""
    )

    if len(winners) == 1:
        return DominanceRecommendation(
            "selected",
            winners[0].window_id,
            f"Окно {winners[0].window_id} доминирует над остальными сравниваемыми окнами "
            f"по правилу v2 (не хуже по всем пригодным сопоставимым показателям, устойчиво "
            f"лучше хотя бы по одному). {all_pair_explanations}.{exclusion_note}",
        )

    if all(c.verdict == "tie" for c in pairwise.values()):
        return DominanceRecommendation(
            "tie",
            None,
            f"Сравниваемые окна равнозначны по правилу доминирования v2 — различий, "
            f"дающих основание предпочесть одно окно другому, нет. {all_pair_explanations}."
            f"{exclusion_note}",
        )

    return DominanceRecommendation(
        "insufficient_basis",
        None,
        "Автоматического победителя нет: конфликт механизмов, несопоставимость или "
        f"неопределённое различие хотя бы в одной паре окон. {all_pair_explanations}."
        f"{exclusion_note}",
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
