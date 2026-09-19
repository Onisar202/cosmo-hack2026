"""Atomic scenario-directory replacement (FN-46 round 7 review, point 5):
re-running a scenario into the same ``out_dir`` must never leave stale
artifacts from a previous, differently-shaped run (e.g. a success run's
``production_result.json``/``.html`` sitting next to a later failure run's
``production_failure.json``) — the scenario directory is built in a
temporary location and only swapped into place after every artifact was
written successfully.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from experiments import production
from experiments.config import Scenario
from experiments.run import run_scenario

from .conftest import scenario_by_name

_RUN_STARTED_AT = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)


def test_rerunning_a_scenario_that_flips_from_success_to_failure_leaves_no_stale_files(
    tmp_path: Path, real_scenarios: list[Scenario], monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = scenario_by_name(real_scenarios, "event")

    # First run: the real, successful build — writes production_result.json/
    # production_result_export.json/production_result.html.
    run_scenario(scenario, out_dir=tmp_path, run_started_at=_RUN_STARTED_AT)
    scenario_dir = tmp_path / scenario.name
    assert (scenario_dir / "production_result.json").exists()
    assert (scenario_dir / "production_result_export.json").exists()
    assert (scenario_dir / "production_result.html").exists()
    assert not (scenario_dir / "production_failure.json").exists()

    # Second run into the SAME out_dir: force the failure path, simulating
    # e.g. a scenario config edit that turns a covered interval into a gap.
    real_build = production.build_production_result

    def _forced_failure(*args: object, **kwargs: object) -> production.ProductionBuild:
        real = real_build(*args, **kwargs)  # type: ignore[arg-type]
        assert real.result is not None
        return production.ProductionBuild(
            result=None,
            failure={"scenario": scenario.name, "error": {"type": "Forced", "message": "test"}},
            evidence_by_window={},
        )

    monkeypatch.setattr(production, "build_production_result", _forced_failure)
    run_scenario(scenario, out_dir=tmp_path, run_started_at=_RUN_STARTED_AT)

    assert (scenario_dir / "production_failure.json").exists()
    assert not (scenario_dir / "production_result.json").exists(), (
        "stale production_result.json from the previous success run survived "
        "a re-run that switched to the failure path"
    )
    assert not (scenario_dir / "production_result_export.json").exists()
    assert not (scenario_dir / "production_result.html").exists()
    # baseline_result.json/metrics.json are written on both paths — still present.
    assert (scenario_dir / "baseline_result.json").exists()
    assert (scenario_dir / "metrics.json").exists()


def test_rerunning_a_scenario_that_flips_from_failure_to_success_leaves_no_stale_files(
    tmp_path: Path, real_scenarios: list[Scenario], monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = scenario_by_name(real_scenarios, "event")
    real_build = production.build_production_result

    def _forced_failure(*args: object, **kwargs: object) -> production.ProductionBuild:
        return production.ProductionBuild(
            result=None,
            failure={"scenario": scenario.name, "error": {"type": "Forced", "message": "test"}},
            evidence_by_window={},
        )

    monkeypatch.setattr(production, "build_production_result", _forced_failure)
    run_scenario(scenario, out_dir=tmp_path, run_started_at=_RUN_STARTED_AT)
    scenario_dir = tmp_path / scenario.name
    assert (scenario_dir / "production_failure.json").exists()

    monkeypatch.setattr(production, "build_production_result", real_build)
    run_scenario(scenario, out_dir=tmp_path, run_started_at=_RUN_STARTED_AT)

    assert (scenario_dir / "production_result.json").exists()
    assert not (scenario_dir / "production_failure.json").exists(), (
        "stale production_failure.json from the previous failure run survived "
        "a re-run that switched to the success path"
    )
