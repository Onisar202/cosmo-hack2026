"""Determinism (FN-43): running the same scenario twice yields identical
artifacts — full equality, no modulo, since result_id/computed_at/record_id
are all made deterministic (experiments/determinism.py,
experiments/production.py, experiments/run.py module docstrings).
"""

from __future__ import annotations

from pathlib import Path

from experiments.config import Scenario
from experiments.run import run_scenario

from .conftest import scenario_by_name


def test_running_the_same_scenario_twice_yields_byte_identical_artifacts(
    tmp_path: Path, real_scenarios: list[Scenario]
) -> None:
    scenario = scenario_by_name(real_scenarios, "event")
    out_a = tmp_path / "run_a"
    out_b = tmp_path / "run_b"

    metrics_a = run_scenario(scenario, out_dir=out_a)
    metrics_b = run_scenario(scenario, out_dir=out_b)

    assert metrics_a == metrics_b

    dir_a = out_a / scenario.name
    dir_b = out_b / scenario.name
    files_a = sorted(p.name for p in dir_a.iterdir())
    files_b = sorted(p.name for p in dir_b.iterdir())
    assert files_a == files_b
    assert files_a, "expected at least one artifact file"

    for name in files_a:
        content_a = (dir_a / name).read_bytes()
        content_b = (dir_b / name).read_bytes()
        assert content_a == content_b, f"{name} differs between two runs of the same scenario"


def test_running_the_missing_data_scenario_twice_yields_byte_identical_artifacts(
    tmp_path: Path, real_scenarios: list[Scenario]
) -> None:
    """Same determinism guarantee on the failure path (production_failure.json)."""
    scenario = scenario_by_name(real_scenarios, "missing_data")
    out_a = tmp_path / "run_a"
    out_b = tmp_path / "run_b"

    run_scenario(scenario, out_dir=out_a)
    run_scenario(scenario, out_dir=out_b)

    dir_a = out_a / scenario.name
    dir_b = out_b / scenario.name
    for name in ("baseline_result.json", "metrics.json", "production_failure.json"):
        assert (dir_a / name).read_bytes() == (dir_b / name).read_bytes()
