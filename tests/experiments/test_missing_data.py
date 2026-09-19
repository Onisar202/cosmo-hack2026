"""The missing_data scenario genuinely fails at orbit selection (FN-43) —
pinned to the real fixture data, not mocked: no NASA OEM release covers the
requested interval, so ``src.sources.orbit.select_oem_elements_for_request``
must raise ``HistoricalElementsUnsupportedError``, and this stand must
surface that as a structured failure rather than silently producing a
plausible-looking calm result.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from experiments import fixtures, production
from experiments.config import Scenario
from experiments.determinism import deterministic_record_ids
from src.sources import orbit
from src.store import RawOriginalStore

from .conftest import scenario_by_name

#: The orbit-failure path never reaches computed_at — any fixed value works.
_RUN_STARTED_AT = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)


def test_missing_data_scenario_raises_historical_elements_unsupported(
    real_scenarios: list[Scenario], db_conn: sqlite3.Connection, raw_store: RawOriginalStore
) -> None:
    scenario = scenario_by_name(real_scenarios, "missing_data")

    with deterministic_record_ids(scenario.name):
        build = production.build_production_result(
            scenario, conn=db_conn, raw_store=raw_store, run_started_at=_RUN_STARTED_AT
        )

    assert build.result is None
    assert build.failure is not None
    assert build.failure["error"]["type"] == orbit.HistoricalElementsUnsupportedError.__name__
    assert build.failure["scenario"] == "missing_data"
    assert "gap" in build.failure["why_this_is_correct_not_a_bug"].lower()


def test_missing_data_scenario_windows_are_genuinely_uncovered_by_any_real_release(
    real_scenarios: list[Scenario],
) -> None:
    """Pins the scenario's own dates to the real, documented OEM release
    table (tests/fixtures/orbit/history/README.md), so this test would fail
    loudly if someone edited scenarios.yaml to no longer describe a real
    gap."""
    scenario = scenario_by_name(real_scenarios, "missing_data")
    releases_with_bytes = fixtures.load_all_oem_releases()

    interval_start = min(scenario.window_a_start, scenario.window_b_start)
    interval_end = max(scenario.window_a_end, scenario.window_b_end)

    for release, _raw in releases_with_bytes:
        covers = (
            release.parsed.useable_start_time <= interval_start
            and release.parsed.useable_stop_time >= interval_end
        )
        assert not covers, (
            f"release {release.release_date} unexpectedly covers the "
            "missing_data scenario's interval — scenarios.yaml no longer "
            "describes a real archival gap"
        )
