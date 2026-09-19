"""Config-driven, not hardcoded (acceptance criterion 7): changing a
scenario's dates in ``experiments/scenarios.yaml`` changes the computed
output accordingly — proves the calculation reads from config/records, not
from a hardcoded answer for a fixed scenario name.
"""

from __future__ import annotations

import sqlite3
import textwrap
from datetime import datetime, timezone
from pathlib import Path

from experiments import production
from experiments.config import load_scenarios
from experiments.determinism import deterministic_record_ids
from src.store import RawOriginalStore

#: This test does not assert on computed_at — any fixed value works.
_RUN_STARTED_AT = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)

_TEMPLATE = """
scenarios:
  - name: shiftable
    description: "test scenario for config-driven behaviour"
    mode: historical_forecast
    as_of: "{as_of}"
    duration_hours: 6
    window_a_start: "{window_a_start}"
    window_b_start: "{window_b_start}"
    expected_event: null
"""


def _write_config(path: Path, *, as_of: str, window_a_start: str, window_b_start: str) -> Path:
    content = _TEMPLATE.format(
        as_of=as_of, window_a_start=window_a_start, window_b_start=window_b_start
    )
    path.write_text(textwrap.dedent(content), encoding="utf-8")
    return path


def test_shifting_scenario_dates_in_config_changes_the_evidence_found(
    tmp_path: Path, db_conn: sqlite3.Connection, raw_store: RawOriginalStore
) -> None:
    """Same scenario NAME, two different configs: one whose window sits in
    the real, documented AR3664 event period (2024-05-10..11) and one whose
    window sits in the documented quiet period (2024-06-20..21) — the DONKI
    evidence this stand finds must differ accordingly, proving the
    calculation is driven by the config's dates, not by anything hardcoded
    for the scenario name.

    Both configs honor ``as_of <= window_a_start`` (FN-46 round 7 review) —
    same dates as the real ``experiments/scenarios.yaml`` event/control
    scenarios: the event window opens right after the real magnetopause-
    crossing forecast notices 20240510-AL-011/AL-012 (published
    2024-05-10T17:33:04Z/17:39:02Z, predicting a crossing at
    2024-05-10T18:01Z), which is real archived evidence genuinely visible
    BEFORE the window starts.
    """
    event_config = _write_config(
        tmp_path / "event.yaml",
        as_of="2024-05-10T17:40:00Z",
        window_a_start="2024-05-10T17:40:00Z",
        window_b_start="2024-05-11T00:00:00Z",
    )
    quiet_config = _write_config(
        tmp_path / "quiet.yaml",
        as_of="2024-06-20T00:00:00Z",
        window_a_start="2024-06-20T06:00:00Z",
        window_b_start="2024-06-21T06:00:00Z",
    )

    event_scenario = load_scenarios(event_config)[0]
    quiet_scenario = load_scenarios(quiet_config)[0]
    assert event_scenario.name == quiet_scenario.name == "shiftable"

    with deterministic_record_ids("config-driven-event"):
        event_build = production.build_production_result(
            event_scenario, conn=db_conn, raw_store=raw_store, run_started_at=_RUN_STARTED_AT
        )
    with deterministic_record_ids("config-driven-quiet"):
        quiet_build = production.build_production_result(
            quiet_scenario, conn=db_conn, raw_store=raw_store, run_started_at=_RUN_STARTED_AT
        )

    assert event_build.result is not None
    assert quiet_build.result is not None

    event_evidence_ids = {
        entry.record_id
        for entries in event_build.evidence_by_window.values()
        for entry in entries
    }
    quiet_evidence_ids = {
        entry.record_id
        for entries in quiet_build.evidence_by_window.values()
        for entry in entries
    }
    assert event_evidence_ids, "expected real DONKI evidence for the 2024-05-10..11 window"
    assert not quiet_evidence_ids, "expected no DONKI evidence for the documented quiet window"
    assert event_evidence_ids != quiet_evidence_ids

    # And the orbit block genuinely differs too (different OEM release selected).
    event_epoch = event_build.result["orbit"]["elements_epoch"]
    quiet_epoch = quiet_build.result["orbit"]["elements_epoch"]
    assert event_epoch != quiet_epoch
