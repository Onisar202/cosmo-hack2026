"""Metrics for the FN-43 experiment stand: production-method evidence vs.
the naive baseline, graded against each scenario's ``expected_event``.

``expected_event`` is read ONLY here (never by
``experiments/production.py``/``experiments/baseline.py``, which must
derive their answers from the archived records alone — main-prompt.md §9,
feeding the answer into the thing under test would be a leak, not a
feature).

Every function here is a pure, small computation on already-built inputs —
no I/O, no store, no fixtures — so it can be unit-tested on synthetic data
(``tests/experiments/test_metrics.py``) independently of the real archive.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from experiments.baseline import BaselineResult
from experiments.donki_evidence import DonkiEvidenceEntry

#: Same restriction as experiments/production.py/baseline.py: only these two
#: DONKI message types are treated as "the event" this stand's Mechanism 1
#: scenarios are about (proton events / geomagnetic storms) — see
#: experiments/baseline.py module docstring "Event-type classification" for
#: the full justification.
EVENT_MESSAGE_TYPES: frozenset[str] = frozenset({"SEP", "GST"})


def evidence_flags_event(evidence_by_window: dict[str, list[DonkiEvidenceEntry]]) -> bool:
    """True if ANY window's DONKI evidence includes a SEP/GST notification."""
    return any(
        entry.message_type in EVENT_MESSAGE_TYPES
        for entries in evidence_by_window.values()
        for entry in entries
    )


def baseline_flags_event(baseline: BaselineResult) -> bool:
    return baseline.verdict == "event_carried_forward"


def production_flags_event(production_result: dict[str, Any] | None) -> bool | None:
    """Whether the production method's OWN ``space_weather`` mechanism
    genuinely classified the event — ``None`` (not applicable/N/A) unless
    at least one window's ``space_weather`` mechanism reaches
    ``status="ok"`` for this scenario (FN-46 round 7 review, point 4).

    Today ``experiments/production.py`` always reports
    ``space_weather.status="missing_data"`` for every window (no archived
    quantitative pfu observation in this repository, pending FN-41/FN-42 —
    see that module's docstring): there is no production classification to
    grade here, so this returns ``None`` rather than fabricating a
    True/False verdict out of the informal DONKI evidence probe. When a
    future production build DOES reach ``status="ok"`` on some window (once
    FN-41/FN-42 land), this reports whether ANY such window's ``max_level``
    is above background — the real production-vs-baseline comparison the
    ticket asks for, computed from ``build.result`` itself rather than from
    ``experiments/donki_evidence.py``'s separate, informal probe.

    **Partial coverage is NOT a negative result** (FN-46 round 1 re-review
    of round 7's fix, point 2): a scenario where one window is
    ``status="ok"``/background and the OTHER is ``status="missing_data"``
    must not read as a confident ``False`` — the second window was never
    actually checked. ``True`` as soon as ANY window flags the event
    (regardless of the other windows' coverage); ``False`` only when EVERY
    window's ``space_weather`` mechanism reached ``status="ok"`` and none
    flagged it; ``None`` (N/A) for every other case, including a mix of
    ``ok`` and uncovered windows.
    """
    if production_result is None:
        return None
    statuses: list[str] = []
    flagged = False
    for window in production_result["windows"]:
        for mechanism in window["mechanisms"]:
            if mechanism["mechanism"] != "space_weather":
                continue
            statuses.append(mechanism["status"])
            if mechanism["status"] == "ok" and mechanism.get("max_level") not in (
                None,
                "background",
            ):
                flagged = True
    if flagged:
        return True
    if statuses and all(status == "ok" for status in statuses):
        return False
    return None


@dataclass(frozen=True)
class EventMissResult:
    applicable: bool
    production_missed: bool | None
    evidence_missed: bool | None
    baseline_missed: bool | None
    note: str


def compute_event_miss(
    *,
    expected_event: bool | None,
    production_result: dict[str, Any] | None,
    evidence_by_window: dict[str, list[DonkiEvidenceEntry]],
    baseline: BaselineResult,
) -> EventMissResult:
    """Meaningful only for a scenario with ``expected_event=True`` — did the
    production method / evidence probe / baseline fail to flag ANY
    event-overlap at all for this scenario?

    ``production_missed`` is graded from ``production_result`` itself (the
    real production-method output), never from the informal DONKI evidence
    probe — see :func:`production_flags_event`. It is ``None`` (N/A) rather
    than a computed miss while production's own ``space_weather`` mechanism
    never reaches ``status="ok"`` here (FN-46 round 7 review, point 4: the
    ticket's summary previously read as a production-vs-baseline
    comparison while actually comparing the evidence probe, which
    production explicitly forbids treating as its own classification).
    """
    if expected_event is not True:
        return EventMissResult(
            applicable=False,
            production_missed=None,
            evidence_missed=None,
            baseline_missed=None,
            note="expected_event is not True for this scenario — event_miss does not apply.",
        )
    production_flag = production_flags_event(production_result)
    evidence_missed = not evidence_flags_event(evidence_by_window)
    baseline_missed = not baseline_flags_event(baseline)
    return EventMissResult(
        applicable=True,
        production_missed=None if production_flag is None else not production_flag,
        evidence_missed=evidence_missed,
        baseline_missed=baseline_missed,
        note=(
            "production_missed: N/A (null) while the production method's own "
            'space_weather mechanism never reaches status="ok" here (no '
            "archived quantitative pfu observation, pending FN-41/FN-42) — "
            "there is nothing for production to have missed or caught. "
            "evidence_missed: the DONKI evidence probe (archival, informal, "
            "NOT a production classification) found no SEP/GST notification "
            "overlapping any window. baseline_missed: the naive baseline's "
            "flat verdict was not event_carried_forward."
        ),
    )


@dataclass(frozen=True)
class FalseWarningResult:
    applicable: bool
    production_false_warning: bool | None
    evidence_false_warning: bool | None
    baseline_false_warning: bool | None
    note: str


def compute_false_warning(
    *,
    expected_event: bool | None,
    production_result: dict[str, Any] | None,
    evidence_by_window: dict[str, list[DonkiEvidenceEntry]],
    baseline: BaselineResult,
) -> FalseWarningResult:
    """Meaningful only for a scenario with ``expected_event=False`` — did the
    production method / evidence probe / baseline flag an event when none
    is expected? ``production_false_warning`` follows the same N/A rule as
    ``EventMissResult.production_missed`` — see
    :func:`production_flags_event` and :func:`compute_event_miss`."""
    if expected_event is not False:
        return FalseWarningResult(
            applicable=False,
            production_false_warning=None,
            evidence_false_warning=None,
            baseline_false_warning=None,
            note="expected_event is not False for this scenario — false_warning does not apply.",
        )
    production_flag = production_flags_event(production_result)
    return FalseWarningResult(
        applicable=True,
        production_false_warning=production_flag,
        evidence_false_warning=evidence_flags_event(evidence_by_window),
        baseline_false_warning=baseline_flags_event(baseline),
        note=(
            "production_false_warning: N/A (null) while the production "
            "method's own space_weather mechanism never reaches "
            'status="ok" here (no archived quantitative pfu observation, '
            "pending FN-41/FN-42) — there is nothing for production to have "
            "flagged. evidence_false_warning: the DONKI evidence probe "
            "(archival, informal, NOT a production classification) found a "
            "SEP/GST notification overlapping some window despite "
            "expected_event=false. baseline_false_warning: the naive "
            "baseline's flat verdict was event_carried_forward despite "
            "expected_event=false."
        ),
    )


@dataclass(frozen=True)
class SelectedWindowChangeResult:
    production_recommendation_status: str | None
    production_recommendation_window_id: str | None
    baseline_has_window_preference: bool
    divergence: bool
    explanation: str


def compute_selected_window_change(
    *, production_result: dict[str, Any] | None
) -> SelectedWindowChangeResult:
    """Compares the production method's window recommendation against what
    the naive baseline can express.

    The naive baseline (``experiments/baseline.py``) applies the SAME flat
    verdict to every candidate window BY CONSTRUCTION (it never computes
    window intersections) — it therefore can never express a preference for
    one window over another; ``baseline_has_window_preference`` is always
    ``False`` here, not computed from the baseline result, because that is
    a structural property of the method, not an accident of this run's
    numbers. ``divergence`` is True exactly when production DOES express a
    preference (``status="selected"``) that the baseline structurally
    cannot — this is a meaningful, honest comparison per the ticket
    ("baseline commits to window X / no-preference, production correctly
    declines to recommend — divergence: yes/no, and why"), rather than
    forcing a same-shape comparison that does not fit when production is
    ``all_windows_excluded`` (the typical case here, see
    experiments/production.py module docstring on space_weather).
    """
    if production_result is None:
        return SelectedWindowChangeResult(
            production_recommendation_status=None,
            production_recommendation_window_id=None,
            baseline_has_window_preference=False,
            divergence=False,
            explanation=(
                "No production result was built for this scenario (orbit "
                "selection failed — see production_failure.json), so there is "
                "nothing to compare a window preference against."
            ),
        )
    recommendation = production_result["recommendation"]
    status = str(recommendation["status"])
    window_id = recommendation["window_id"]
    divergence = status == "selected"
    if divergence:
        explanation = (
            f"Production selected window {window_id!r} ({status}); the naive "
            "baseline cannot express any window preference at all (it applies "
            "the same flat verdict to every window by construction) — this is a "
            "genuine divergence in what each method can conclude."
        )
    else:
        explanation = (
            f"Production status={status!r} does not select a single window either "
            "(critical gap / tie / insufficient basis); the naive baseline also "
            "cannot express a window preference (same flat verdict for every "
            "window by construction). No divergence to report on this axis for "
            "this run — both methods end up without a differentiated pick, for "
            "different reasons (production: honestly declines due to the "
            "space_weather data gap; baseline: structurally incapable of "
            "comparing windows at all)."
        )
    return SelectedWindowChangeResult(
        production_recommendation_status=status,
        production_recommendation_window_id=window_id,
        baseline_has_window_preference=False,
        divergence=divergence,
        explanation=explanation,
    )


@dataclass(frozen=True)
class CoverageResult:
    orbit_release_available: bool
    mmod_mean_coverage_fraction: float | None
    donki_evidence_window_fraction: float
    note: str


def compute_coverage(
    *,
    orbit_release_available: bool,
    mmod_coverage_fractions: list[float],
    evidence_by_window: dict[str, list[DonkiEvidenceEntry]],
) -> CoverageResult:
    """Fraction of the scenario's real archived-record backing for each
    line actually used while building the result — computed directly from
    the counts the caller already has (production.py's own
    ``mmod_mechanism["coverage_fraction"]`` per window, and how many of the
    scenario's windows carry at least one DONKI evidence entry), not from a
    formula unrelated to what was actually built."""
    mean_mmod = (
        sum(mmod_coverage_fractions) / len(mmod_coverage_fractions)
        if mmod_coverage_fractions
        else None
    )
    total_windows = len(evidence_by_window)
    windows_with_evidence = sum(1 for entries in evidence_by_window.values() if entries)
    donki_fraction = windows_with_evidence / total_windows if total_windows else 0.0
    return CoverageResult(
        orbit_release_available=orbit_release_available,
        mmod_mean_coverage_fraction=mean_mmod,
        donki_evidence_window_fraction=donki_fraction,
        note=(
            "orbit_release_available: whether an OEM release covering the full "
            "requested interval, published by as_of, was found at all. "
            "mmod_mean_coverage_fraction: mean of "
            "mechanismAssessment.coverage_fraction (mmod) across this "
            "scenario's windows, real output of "
            "src.domain.mmod.background.assess_mmod_background — null when no "
            "production result was built. donki_evidence_window_fraction: "
            "fraction of this scenario's windows for which the DONKI evidence "
            "probe found at least one overlapping notification known by as_of "
            "(any type, not filtered to SEP/GST — a broader signal-presence "
            "measure than event_miss/false_warning above)."
        ),
    )


def event_miss_to_dict(result: EventMissResult) -> dict[str, Any]:
    return {
        "applicable": result.applicable,
        "production_missed": result.production_missed,
        "evidence_missed": result.evidence_missed,
        "baseline_missed": result.baseline_missed,
        "note": result.note,
    }


def false_warning_to_dict(result: FalseWarningResult) -> dict[str, Any]:
    return {
        "applicable": result.applicable,
        "production_false_warning": result.production_false_warning,
        "evidence_false_warning": result.evidence_false_warning,
        "baseline_false_warning": result.baseline_false_warning,
        "note": result.note,
    }


def selected_window_change_to_dict(result: SelectedWindowChangeResult) -> dict[str, Any]:
    return {
        "production_recommendation_status": result.production_recommendation_status,
        "production_recommendation_window_id": result.production_recommendation_window_id,
        "baseline_has_window_preference": result.baseline_has_window_preference,
        "divergence": result.divergence,
        "explanation": result.explanation,
    }


def coverage_to_dict(result: CoverageResult) -> dict[str, Any]:
    return {
        "orbit_release_available": result.orbit_release_available,
        "mmod_mean_coverage_fraction": result.mmod_mean_coverage_fraction,
        "donki_evidence_window_fraction": result.donki_evidence_window_fraction,
        "note": result.note,
    }


__all__ = [
    "EVENT_MESSAGE_TYPES",
    "CoverageResult",
    "EventMissResult",
    "FalseWarningResult",
    "SelectedWindowChangeResult",
    "baseline_flags_event",
    "compute_coverage",
    "compute_event_miss",
    "compute_false_warning",
    "compute_selected_window_change",
    "coverage_to_dict",
    "evidence_flags_event",
    "event_miss_to_dict",
    "false_warning_to_dict",
    "production_flags_event",
    "selected_window_change_to_dict",
]
