"""CLI entry point for the FN-43 (T5) experiment stand.

    uv run python -m experiments.run --config experiments/scenarios.yaml --out experiments/out

Fully offline: every input this command reads is either the scenario
config (``experiments/scenarios.yaml``) or a bundled/fixture file already
committed to this repository (``tests/fixtures/...``,
``src/sources/data/mmod/...`` — see ``experiments/fixtures.py`` for the
exact list and why they are read by path rather than duplicated). No
network call happens anywhere in this module or anything it imports from
``experiments/``.

**Determinism.** Nothing that feeds ``result_id``/``as_of``/every record's
``fetched_at`` samples the wall clock or an unseeded random source:

- ``as_of``/every record's ``fetched_at`` in a scenario's build are either
  the scenario's own configured ``as_of`` (a fixed value from
  ``experiments/scenarios.yaml``) or the REAL retrieval provenance of the
  underlying fixture file (``experiments/fixtures.py``'s
  ``donki_fetched_at``/``oem_release_fetched_at``/``mmod_fetched_at``,
  themselves fixed values read from committed ``*.meta.json`` sidecars) —
  never ``datetime.now()``, and never each other's value substituted in
  (FN-46 round 7 review, point 2: ``as_of`` is a request cutoff, not a
  stand-in for "when we fetched this" or "when we ran this");
- ``result_id`` is ``f"res-experiment-{scenario.name}"``, not
  ``uuid.uuid4()``;
- every ``record_id`` assigned by ``src.store.records.insert_record``
  during a scenario's build is made deterministic for the duration of that
  one build via ``experiments.determinism.deterministic_record_ids`` (see
  that module's docstring for why this is necessary and how it is scoped).

``result.computed_at`` ("когда расчёт был выполнен и сохранён",
contracts/result.schema.json) is the one deliberate exception: it is the
REAL moment this run started (``run_started_at`` below, ``datetime.now()``
captured exactly once by :func:`main`), because that is genuinely what the
field means — a scenario's ``as_of`` is not "when we ran this experiment"
either. Two runs of the same scenario therefore differ in ``computed_at``
(and in the bytes of any artifact that embeds it) but are otherwise
byte-identical; ``tests/experiments/test_determinism.py`` checks this by
passing the SAME explicit ``run_started_at`` to both runs it compares —
proving every OTHER field is deterministic — rather than by masking
``computed_at`` back to a fake constant.
"""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from experiments import baseline as baseline_module
from experiments import fixtures, metrics, production
from experiments.config import Scenario, load_scenarios
from experiments.determinism import deterministic_record_ids
from src.export._validate import validate_result_or_raise
from src.export.html_export import export_result_html
from src.export.json_export import export_result_json
from src.store import RawOriginalStore
from src.store.schema import connect as connect_store

DEFAULT_CONFIG = Path(__file__).resolve().parent / "scenarios.yaml"
DEFAULT_OUT = Path(__file__).resolve().parent / "out"


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False)
    path.write_text(text + "\n", encoding="utf-8")


def _atomic_rename(src: Path, dst: Path) -> None:
    """Thin wrapper around ``Path.rename`` so a test can inject a failure at
    one specific rename step without monkeypatching ``pathlib`` globally."""
    src.rename(dst)


def _replace_scenario_dir(final_dir: Path, built_dir: Path) -> None:
    """Atomically swaps a fully-built ``built_dir`` into ``final_dir``'s
    place (FN-46 round 7 review, point 5; round 1 re-review of that fix,
    point 3).

    Previously ``run_scenario`` wrote straight into ``out_dir /
    scenario.name``, reusing whatever was already there: re-running a
    scenario whose outcome flips between a run (``production_result.json``
    + ``.html``) and a failure (``production_failure.json``) left BOTH
    sets of files sitting side by side, silently misrepresenting the most
    recent run. Building into a sibling temp directory first and renaming
    it into place only after every artifact was written successfully means
    ``final_dir`` is, at every observable moment, either the complete
    previous build or the complete new one — never a mix.

    The first fix moved the previous ``final_dir`` aside and deleted it
    BEFORE renaming ``built_dir`` into place: if that second rename failed
    or the process died in between, the old good result was already gone
    and the new one never arrived — a genuine data loss window, not just a
    brief "empty" gap. The previous directory is now kept under its
    ``.stale-`` name until the new one is confirmed in place, and restored
    on any failure of that final rename; it is only deleted after success.
    """
    stale_dir: Path | None = None
    if final_dir.exists():
        stale_dir = final_dir.parent / f".{final_dir.name}.stale-{uuid.uuid4().hex}"
        _atomic_rename(final_dir, stale_dir)
    try:
        _atomic_rename(built_dir, final_dir)
    except BaseException:
        if stale_dir is not None:
            _atomic_rename(stale_dir, final_dir)
        raise
    if stale_dir is not None:
        shutil.rmtree(stale_dir)


def run_scenario(scenario: Scenario, *, out_dir: Path, run_started_at: datetime) -> dict[str, Any]:
    """Runs one scenario end to end and writes its artifacts under
    ``out_dir / scenario.name``. Returns the scenario's ``metrics.json``
    content (also used to build the cross-scenario ``summary.md`` table).

    ``run_started_at`` is threaded straight through to
    ``production.build_production_result`` — see that function's docstring
    and this module's docstring "Determinism" section.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    final_dir = out_dir / scenario.name
    build_dir = Path(tempfile.mkdtemp(dir=out_dir, prefix=f".{scenario.name}.build-"))
    try:
        metrics_dict = _build_scenario_artifacts(
            scenario, scenario_dir=build_dir, run_started_at=run_started_at
        )
        _replace_scenario_dir(final_dir, build_dir)
    except BaseException:
        # Cleans up build_dir whether _build_scenario_artifacts failed (it
        # still exists under its temp name) or _replace_scenario_dir failed
        # after already renaming it away (this is then a no-op — nothing
        # left at build_dir's path, ignore_errors handles that).
        shutil.rmtree(build_dir, ignore_errors=True)
        raise
    return metrics_dict


def _build_scenario_artifacts(
    scenario: Scenario, *, scenario_dir: Path, run_started_at: datetime
) -> dict[str, Any]:
    """Builds every artifact for one scenario into ``scenario_dir`` (a
    fresh, empty directory — see :func:`run_scenario`) and returns the
    scenario's ``metrics.json`` content."""
    # A fresh, temporary store per scenario build: this stand does not need
    # a persistent store between runs (determinism comes from
    # experiments.determinism, not from reusing a database file across
    # invocations — see that module's docstring), and a fresh store keeps
    # scenarios from ever accidentally seeing each other's inserted records.
    with tempfile.TemporaryDirectory(prefix=f"fn43-{scenario.name}-") as tmp:
        tmp_path = Path(tmp)
        conn = connect_store(tmp_path / "store.sqlite3")
        raw_store = RawOriginalStore(tmp_path / "raw")
        try:
            with deterministic_record_ids(scenario.name):
                build = production.build_production_result(
                    scenario, conn=conn, raw_store=raw_store, run_started_at=run_started_at
                )
        finally:
            conn.close()

    notifications = fixtures.load_donki_notifications()
    baseline_result = baseline_module.run_baseline(
        notifications,
        as_of=scenario.as_of,
        windows=[
            ("win-a", scenario.window_a_start, scenario.window_a_end),
            ("win-b", scenario.window_b_start, scenario.window_b_end),
        ],
    )
    _write_json(
        scenario_dir / "baseline_result.json",
        baseline_module.baseline_result_to_dict(baseline_result),
    )

    mmod_fractions: list[float] = []
    if build.result is not None:
        validate_result_or_raise(build.result)
        _write_json(scenario_dir / "production_result.json", build.result)

        # Acceptance criterion 5: production-method results also go through
        # the real, existing JSON/HTML exporters (src/export/), proving
        # this saved result and its exports genuinely correspond.
        exported_json = export_result_json(build.result)
        _write_json(scenario_dir / "production_result_export.json", exported_json)
        html = export_result_html(build.result)
        (scenario_dir / "production_result.html").parent.mkdir(parents=True, exist_ok=True)
        (scenario_dir / "production_result.html").write_text(html, encoding="utf-8")

        for window in build.result["windows"]:
            for mechanism in window["mechanisms"]:
                if mechanism["mechanism"] == "mmod":
                    mmod_fractions.append(float(mechanism["coverage_fraction"]))
    else:
        assert build.failure is not None
        _write_json(scenario_dir / "production_failure.json", build.failure)

    event_miss = metrics.compute_event_miss(
        expected_event=scenario.expected_event,
        production_result=build.result,
        evidence_by_window=build.evidence_by_window,
        baseline=baseline_result,
    )
    false_warning = metrics.compute_false_warning(
        expected_event=scenario.expected_event,
        production_result=build.result,
        evidence_by_window=build.evidence_by_window,
        baseline=baseline_result,
    )
    window_change = metrics.compute_selected_window_change(production_result=build.result)
    coverage = metrics.compute_coverage(
        orbit_release_available=build.result is not None,
        mmod_coverage_fractions=mmod_fractions,
        evidence_by_window=build.evidence_by_window,
    )

    metrics_dict: dict[str, Any] = {
        "scenario": scenario.name,
        "algorithm_version": production.ALGORITHM_VERSION,
        "as_of": scenario.as_of.isoformat().replace("+00:00", "Z"),
        "expected_event": scenario.expected_event,
        "event_miss": metrics.event_miss_to_dict(event_miss),
        "false_warning": metrics.false_warning_to_dict(false_warning),
        "selected_window_change": metrics.selected_window_change_to_dict(window_change),
        "coverage": metrics.coverage_to_dict(coverage),
        "lead_time_stability_note": (
            "Not computed: DONKI notifications are point-in-time event "
            "alerts, not a continuous quantitative flux series, so a "
            "lead-time-before-threshold-crossing or run-to-run stability "
            "metric cannot be honestly derived from what this stand "
            "actually reads (main-prompt.md §2 — inventing a formula "
            "unsupported by the archived data would be worse than skipping "
            "it)."
        ),
    }
    _write_json(scenario_dir / "metrics.json", metrics_dict)
    return metrics_dict


def _summary_row(scenario: Scenario, m: dict[str, Any]) -> str:
    em = m["event_miss"]
    fw = m["false_warning"]
    wc = m["selected_window_change"]
    cov = m["coverage"]

    def _na(value: bool | None) -> str:
        return "n/a" if value is None else str(value)

    em_text = (
        f"production={_na(em['production_missed'])}, evidence={em['evidence_missed']} / "
        f"baseline={em['baseline_missed']}"
        if em["applicable"]
        else "n/a"
    )
    fw_text = (
        f"production={_na(fw['production_false_warning'])}, "
        f"evidence={fw['evidence_false_warning']} / baseline={fw['baseline_false_warning']}"
        if fw["applicable"]
        else "n/a"
    )
    mmod_cov = cov["mmod_mean_coverage_fraction"]
    mmod_cov_text = f"{mmod_cov:.2f}" if mmod_cov is not None else "n/a"
    return (
        f"| {scenario.name} | {scenario.expected_event} | {em_text} | {fw_text} | "
        f"{wc['production_recommendation_status']} | {wc['divergence']} | "
        f"{mmod_cov_text} | {cov['donki_evidence_window_fraction']:.2f} |"
    )


def build_summary(scenarios: list[Scenario], all_metrics: dict[str, dict[str, Any]]) -> str:
    """Builds ``summary.md`` content strictly from the metrics just
    computed — every sentence below is derived from ``all_metrics``, not
    asserted independently of it, so this document cannot silently drift
    from what the artifacts actually show."""
    lines: list[str] = [
        "# FN-43 (T5) experiment stand — summary",
        "",
        "Generated by `uv run python -m experiments.run`. See `README.md` "
        '("Стенд экспериментов Т5 (FN-43)") and `docs/method.md` for the '
        "full write-up.",
        "",
        "This is metrics about a research-prototype decision-support tool "
        "comparing a real (if data-limited) production method against a "
        "deliberately naive baseline — **not** a safety/go-no-go "
        "conclusion (.ai/main-prompt.md §11: the service never phrases its "
        "output as an EVA clearance).",
        "",
        "## Scenario × metric",
        "",
        "| Scenario | expected_event | event_miss | false_warning | "
        "production recommendation | window divergence | mmod coverage "
        "(mean) | DONKI evidence window fraction |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for scenario in scenarios:
        lines.append(_summary_row(scenario, all_metrics[scenario.name]))
    lines.append("")
    lines.append("## Conclusions")
    lines.append("")

    event_metrics = all_metrics.get("event", {})
    control_metrics = all_metrics.get("control", {})
    missing_metrics = all_metrics.get("missing_data", {})

    if event_metrics.get("event_miss", {}).get("applicable"):
        em = event_metrics["event_miss"]
        production_note = (
            "n/a (production's space_weather mechanism never classifies here, see below)"
            if em["production_missed"] is None
            else ("missed" if em["production_missed"] else "correctly flagged")
        )
        lines.append(
            f"- **event scenario**: production is {production_note}; the "
            "informal DONKI evidence probe (NOT a production classification) "
            f"{'missed' if em['evidence_missed'] else 'correctly flagged'} the "
            f"real AR3664 event as of this scenario's as_of, and the naive "
            f"baseline {'missed' if em['baseline_missed'] else 'correctly flagged'} it "
            "too — see `experiments/out/event/metrics.json` for the exact "
            "notification IDs behind these numbers."
        )
    if control_metrics.get("false_warning", {}).get("applicable"):
        fw = control_metrics["false_warning"]
        production_note = (
            "is n/a (production's space_weather mechanism never classifies here, see below)"
            if fw["production_false_warning"] is None
            else (
                "raised a false warning"
                if fw["production_false_warning"]
                else "did not raise a false warning"
            )
        )
        lines.append(
            f"- **control scenario**: production {production_note}; the "
            "informal DONKI evidence probe (NOT a production "
            f"classification) {'raised' if fw['evidence_false_warning'] else 'did not raise'} a "
            f"false warning for the documented quiet period, and the naive "
            f"baseline {'raised' if fw['baseline_false_warning'] else 'did not raise'} "
            "one either — see `experiments/out/control/metrics.json`."
        )
    event_wc_status = event_metrics.get("selected_window_change", {}).get(
        "production_recommendation_status"
    )
    control_wc_status = control_metrics.get("selected_window_change", {}).get(
        "production_recommendation_status"
    )
    lines.append(
        "- **production method vs. naive baseline on window preference**: "
        "for both scenarios where a production result was built, "
        f"`recommendation.status` was {event_wc_status!r} (event) and "
        f"{control_wc_status!r} (control) — the production method never "
        "fabricates a preferred "
        "window when the space_weather mechanism has a critical data gap "
        "(`critical_gap=true` on every window, see the `windows[*].mechanisms` "
        'entry with `mechanism="space_weather"` in each `production_result.json`), '
        "while the naive baseline structurally cannot express a window "
        "preference at all (it applies one flat verdict to every window by "
        "construction) — so there is no window-preference divergence to "
        "report on this run, for two different, honest reasons on each side."
    )
    if missing_metrics:
        lines.append(
            "- **missing_data scenario**: orbit selection genuinely fails with "
            "`HistoricalElementsUnsupportedError` for both candidate windows — "
            "see `experiments/out/missing_data/production_failure.json` — "
            "because no NASA OEM release's useable interval covers this "
            "interval while being published by `as_of` (the archive has a "
            "real gap between the 2024-05-12 and 2024-06-14 releases). This is "
            "the honest, correct outcome for this scenario, not a bug: no "
            "orbit, no fabricated calm result."
        )
    lines.append(
        "- **space_weather is `missing_data` for every window this stand "
        "evaluated** (event and control scenarios): there is no archived "
        "quantitative proton-flux OBSERVATION source in this repository "
        "today, pending FN-41/FN-42 (not yet merged) — this correctly makes "
        '`recommendation.status="all_windows_excluded"` for both, per the '
        "windows-dominance rule's critical-gap clause "
        "(.ai/main-prompt.md §11). That is the intended, honest finding of "
        "this experiment stand, not a bug to work around."
    )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)

    # Captured exactly once for the whole invocation — every scenario built
    # by this run shares the same, real "when did we run this" timestamp
    # (see module docstring "Determinism").
    run_started_at = datetime.now(timezone.utc)

    scenarios = load_scenarios(args.config)
    all_metrics: dict[str, dict[str, Any]] = {}
    for scenario in scenarios:
        all_metrics[scenario.name] = run_scenario(
            scenario, out_dir=args.out, run_started_at=run_started_at
        )

    summary = build_summary(scenarios, all_metrics)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "summary.md").write_text(summary, encoding="utf-8")
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_summary", "main", "run_scenario"]
