"""Artifact shape (FN-43): ``production_result.json`` validates against
``contracts/result.schema.json``, and ``production_result_export.json``/
``production_result.html`` correspond to it (round-trip through the real
exporters, acceptance criterion 5).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from experiments.config import Scenario
from experiments.run import run_scenario
from src.export._validate import validate_result_or_raise
from src.export.html_export import export_result_html
from src.export.json_export import export_result_json

from .conftest import scenario_by_name

#: These tests do not assert on computed_at — any fixed value works.
_RUN_STARTED_AT = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)


def test_production_result_validates_against_the_result_schema(
    tmp_path: Path, real_scenarios: list[Scenario]
) -> None:
    scenario = scenario_by_name(real_scenarios, "event")
    run_scenario(scenario, out_dir=tmp_path, run_started_at=_RUN_STARTED_AT)

    result_path = tmp_path / scenario.name / "production_result.json"
    assert result_path.exists()
    result = json.loads(result_path.read_text(encoding="utf-8"))

    # Raises ExportError (via validate_result_or_raise) if this does not
    # conform to contracts/result.schema.json — this call is the assertion.
    validate_result_or_raise(result)

    assert result["mode"] == "historical_forecast"
    assert len(result["windows"]) >= 2


def test_export_json_and_html_correspond_to_the_saved_production_result(
    tmp_path: Path, real_scenarios: list[Scenario]
) -> None:
    scenario = scenario_by_name(real_scenarios, "event")
    run_scenario(scenario, out_dir=tmp_path, run_started_at=_RUN_STARTED_AT)

    scenario_dir = tmp_path / scenario.name
    result = json.loads((scenario_dir / "production_result.json").read_text(encoding="utf-8"))
    saved_export = json.loads(
        (scenario_dir / "production_result_export.json").read_text(encoding="utf-8")
    )
    saved_html = (scenario_dir / "production_result.html").read_text(encoding="utf-8")

    # Round-trip through the REAL exporters again on the same saved result —
    # must reproduce exactly what run_scenario already wrote to disk.
    assert export_result_json(result) == saved_export
    assert export_result_json(result) == result
    assert export_result_html(result) == saved_html

    assert result["result_id"] in saved_html
    for window in result["windows"]:
        assert window["window_id"] in saved_html


def test_missing_data_scenario_produces_no_production_result_json(
    tmp_path: Path, real_scenarios: list[Scenario]
) -> None:
    """The missing_data scenario must NOT produce a production_result.json
    (that would mean a fabricated result slipped through) — only a
    production_failure.json."""
    scenario = scenario_by_name(real_scenarios, "missing_data")
    run_scenario(scenario, out_dir=tmp_path, run_started_at=_RUN_STARTED_AT)

    scenario_dir = tmp_path / scenario.name
    assert not (scenario_dir / "production_result.json").exists()
    assert not (scenario_dir / "production_result_export.json").exists()
    assert not (scenario_dir / "production_result.html").exists()
    assert (scenario_dir / "production_failure.json").exists()
    assert (scenario_dir / "baseline_result.json").exists()
    assert (scenario_dir / "metrics.json").exists()
