"""Metrics unit tests on small synthetic inputs (FN-43) — fast,
deterministic, independent of the real archive fixtures.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from experiments import metrics
from experiments.baseline import BaselineResult, BaselineWindowVerdict
from experiments.donki_evidence import DonkiEvidenceEntry

UTC = timezone.utc


def _entry(message_type: str, record_id: str = "rec-1") -> DonkiEvidenceEntry:
    return DonkiEvidenceEntry(
        record_id=record_id,
        message_id="msg-1",
        message_type=message_type,
        published_at=datetime(2024, 5, 10, 12, 0, tzinfo=UTC),
        valid_from=datetime(2024, 5, 10, 12, 0, tzinfo=UTC),
        valid_to=datetime(2024, 5, 10, 12, 0, tzinfo=UTC),
    )


def _production_result(windows: list[dict[str, Any]]) -> dict[str, Any]:
    """Minimal synthetic production_result — only the shape
    ``production_flags_event`` reads (``windows[*].mechanisms[*]`` with
    ``mechanism``/``status``/``max_level``)."""
    return {"windows": windows}


def _space_weather_window(*, status: str, max_level: str | None = None) -> dict[str, Any]:
    return {
        "window_id": "win-a",
        "mechanisms": [{"mechanism": "space_weather", "status": status, "max_level": max_level}],
    }


def _baseline(verdict: str) -> BaselineResult:
    return BaselineResult(
        as_of=datetime(2024, 5, 10, 12, 0, tzinfo=UTC),
        verdict=verdict,  # type: ignore[arg-type]
        basis_message_id=None,
        basis_message_type=None,
        basis_published_at=None,
        lookback_note="synthetic",
        windows=(
            BaselineWindowVerdict(
                window_id="win-a",
                start_at=datetime(2024, 5, 10, 12, 0, tzinfo=UTC),
                end_at=datetime(2024, 5, 10, 18, 0, tzinfo=UTC),
                verdict=verdict,  # type: ignore[arg-type]
            ),
        ),
    )


# --- event_miss ---------------------------------------------------------


def test_event_miss_not_applicable_when_expected_event_is_not_true() -> None:
    result = metrics.compute_event_miss(
        expected_event=False,
        production_result=None,
        evidence_by_window={},
        baseline=_baseline("no_prior_observation"),
    )
    assert result.applicable is False
    assert result.production_missed is None
    assert result.evidence_missed is None
    assert result.baseline_missed is None


def test_event_miss_true_when_neither_method_flags_the_event() -> None:
    result = metrics.compute_event_miss(
        expected_event=True,
        production_result=None,
        evidence_by_window={"win-a": []},
        baseline=_baseline("no_prior_observation"),
    )
    assert result.applicable is True
    assert result.evidence_missed is True
    assert result.baseline_missed is True


def test_event_miss_false_when_evidence_flags_a_sep_notification() -> None:
    result = metrics.compute_event_miss(
        expected_event=True,
        production_result=None,
        evidence_by_window={"win-a": [_entry("SEP")]},
        baseline=_baseline("event_carried_forward"),
    )
    assert result.evidence_missed is False
    assert result.baseline_missed is False


def test_event_miss_evidence_and_baseline_can_disagree() -> None:
    """Evidence probe finds a SEP notification but the baseline's flat
    verdict happened to be quiet — both are independently computed and may
    disagree; the metric must report each side truthfully, not force
    agreement."""
    result = metrics.compute_event_miss(
        expected_event=True,
        production_result=None,
        evidence_by_window={"win-a": [_entry("SEP")]},
        baseline=_baseline("quiet_carried_forward"),
    )
    assert result.evidence_missed is False
    assert result.baseline_missed is True


def test_event_miss_ignores_non_sep_gst_evidence() -> None:
    """A CME/FLR notification overlapping the window does not count as
    "the event" this metric is about (see experiments/baseline.py and
    experiments/metrics.py docstrings on the SEP/GST restriction)."""
    result = metrics.compute_event_miss(
        expected_event=True,
        production_result=None,
        evidence_by_window={"win-a": [_entry("CME")]},
        baseline=_baseline("no_prior_observation"),
    )
    assert result.evidence_missed is True


def test_event_miss_production_is_na_while_space_weather_never_classifies() -> None:
    """FN-46 round 7 review, point 4: production_missed must be None (N/A),
    not a computed True/False derived from the informal evidence probe,
    while every window's space_weather status stays missing_data — the
    real, current state of experiments/production.py."""
    production_result = _production_result([_space_weather_window(status="missing_data")])
    result = metrics.compute_event_miss(
        expected_event=True,
        production_result=production_result,
        evidence_by_window={"win-a": [_entry("SEP")]},
        baseline=_baseline("event_carried_forward"),
    )
    assert result.production_missed is None
    # evidence/baseline stay real, independent signals even while production is N/A.
    assert result.evidence_missed is False
    assert result.baseline_missed is False


def test_event_miss_production_is_graded_once_space_weather_classifies() -> None:
    """Once a production build genuinely reaches status="ok" on some
    window (e.g. after FN-41/FN-42 land), production_missed is graded from
    THAT result, not from the DONKI evidence probe — even when the two
    disagree."""
    result = metrics.compute_event_miss(
        expected_event=True,
        production_result=_production_result([_space_weather_window(status="ok", max_level="S1")]),
        evidence_by_window={},  # evidence probe finds nothing — deliberately disagrees
        baseline=_baseline("no_prior_observation"),
    )
    assert result.production_missed is False  # production DID flag it (max_level=S1)
    assert result.evidence_missed is True  # evidence probe missed it — different, both honest

    missed_result = metrics.compute_event_miss(
        expected_event=True,
        production_result=_production_result(
            [_space_weather_window(status="ok", max_level="background")]
        ),
        evidence_by_window={"win-a": [_entry("SEP")]},
        baseline=_baseline("event_carried_forward"),
    )
    assert missed_result.production_missed is True  # production classified but stayed at background


# --- false_warning -------------------------------------------------------


def test_false_warning_not_applicable_when_expected_event_is_not_false() -> None:
    result = metrics.compute_false_warning(
        expected_event=True,
        production_result=None,
        evidence_by_window={},
        baseline=_baseline("no_prior_observation"),
    )
    assert result.applicable is False
    assert result.production_false_warning is None


def test_false_warning_true_when_evidence_or_baseline_flags_an_event() -> None:
    result = metrics.compute_false_warning(
        expected_event=False,
        production_result=None,
        evidence_by_window={"win-a": [_entry("GST")]},
        baseline=_baseline("event_carried_forward"),
    )
    assert result.evidence_false_warning is True
    assert result.baseline_false_warning is True


def test_false_warning_false_when_nothing_is_flagged() -> None:
    result = metrics.compute_false_warning(
        expected_event=False,
        production_result=None,
        evidence_by_window={"win-a": [], "win-b": [_entry("FLR")]},
        baseline=_baseline("quiet_carried_forward"),
    )
    assert result.evidence_false_warning is False
    assert result.baseline_false_warning is False


def test_false_warning_production_is_na_while_space_weather_never_classifies() -> None:
    production_result = _production_result([_space_weather_window(status="missing_data")])
    result = metrics.compute_false_warning(
        expected_event=False,
        production_result=production_result,
        evidence_by_window={},
        baseline=_baseline("no_prior_observation"),
    )
    assert result.production_false_warning is None


def test_false_warning_production_is_graded_once_space_weather_classifies() -> None:
    result = metrics.compute_false_warning(
        expected_event=False,
        production_result=_production_result([_space_weather_window(status="ok", max_level="S2")]),
        evidence_by_window={},
        baseline=_baseline("no_prior_observation"),
    )
    assert result.production_false_warning is True


# --- production_flags_event -------------------------------------------------


def test_production_flags_event_is_none_without_a_result() -> None:
    assert metrics.production_flags_event(None) is None


def test_production_flags_event_is_none_when_no_window_reaches_ok() -> None:
    result = _production_result(
        [
            _space_weather_window(status="missing_data"),
            {
                "window_id": "win-b",
                "mechanisms": [
                    {"mechanism": "space_weather", "status": "missing_data", "max_level": None}
                ],
            },
        ]
    )
    assert metrics.production_flags_event(result) is None


def test_production_flags_event_true_when_any_window_reaches_above_background() -> None:
    result = _production_result(
        [
            _space_weather_window(status="ok", max_level="background"),
            _space_weather_window(status="ok", max_level="S1"),
        ]
    )
    assert metrics.production_flags_event(result) is True


def test_production_flags_event_false_when_classified_windows_stay_background() -> None:
    result = _production_result([_space_weather_window(status="ok", max_level="background")])
    assert metrics.production_flags_event(result) is False


# --- coverage --------------------------------------------------------------


def test_coverage_reports_orbit_availability_and_mean_mmod_fraction() -> None:
    result = metrics.compute_coverage(
        orbit_release_available=True,
        mmod_coverage_fractions=[1.0, 0.5],
        evidence_by_window={"win-a": [_entry("SEP")], "win-b": []},
    )
    assert result.orbit_release_available is True
    assert result.mmod_mean_coverage_fraction == 0.75
    assert result.donki_evidence_window_fraction == 0.5


def test_coverage_handles_no_windows_and_no_mmod_fractions_without_dividing_by_zero() -> None:
    result = metrics.compute_coverage(
        orbit_release_available=False, mmod_coverage_fractions=[], evidence_by_window={}
    )
    assert result.mmod_mean_coverage_fraction is None
    assert result.donki_evidence_window_fraction == 0.0


# --- selected_window_change -------------------------------------------------


def test_selected_window_change_no_production_result() -> None:
    result = metrics.compute_selected_window_change(production_result=None)
    assert result.production_recommendation_status is None
    assert result.divergence is False
    assert result.baseline_has_window_preference is False


def test_selected_window_change_divergence_when_production_selects_a_window() -> None:
    result = metrics.compute_selected_window_change(
        production_result={
            "recommendation": {
                "status": "selected",
                "window_id": "win-a",
                "explanation": "synthetic",
            }
        }
    )
    assert result.divergence is True
    assert result.production_recommendation_window_id == "win-a"


def test_selected_window_change_no_divergence_when_production_excludes_all_windows() -> None:
    result = metrics.compute_selected_window_change(
        production_result={
            "recommendation": {
                "status": "all_windows_excluded",
                "window_id": None,
                "explanation": "synthetic",
            }
        }
    )
    assert result.divergence is False
