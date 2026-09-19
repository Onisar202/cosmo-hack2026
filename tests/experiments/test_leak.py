"""Mandatory temporal leak test (.ai/main-prompt.md §1/§9 п.1) for the FN-43
experiment stand: a record published AFTER a scenario's ``as_of`` must not
change that scenario's production result or evidence-probe output in any
field. This is the single most important test in this stand — see the
FN-43 ticket ("do not skip this").
"""

from __future__ import annotations

import sqlite3
import tempfile
from datetime import timedelta
from pathlib import Path

import pytest

from experiments import donki_evidence, fixtures, production
from experiments.config import Scenario
from experiments.determinism import deterministic_record_ids
from src.sources import archive_probe
from src.store import RawOriginalStore
from src.store.schema import connect as connect_store

from .conftest import scenario_by_name


def _future_leaking_notification(scenario: Scenario) -> archive_probe.DonkiNotification:
    """A synthetic DONKI SEP notification whose EVENT time falls squarely
    inside ``scenario.window_a_start``..``window_a_end`` (so it WOULD show
    up in DONKI evidence for window A if the leak protection failed), but
    whose publication time is safely after ``scenario.as_of`` (so a correct
    implementation must exclude it).
    """
    event_at = scenario.window_a_start + (scenario.window_a_end - scenario.window_a_start) / 2
    published_at = scenario.as_of + timedelta(days=3650)  # unambiguously "the future" of as_of
    activity_ts = event_at.strftime("%Y-%m-%dT%H:%M:%S")
    body = (
        f"## Message Issue Date: {published_at.strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
        f"Activity ID: {activity_ts}-SEP-999\n"
        "Synthetic leak-test notification — must never be visible at the "
        "scenario's as_of."
    )
    return archive_probe.DonkiNotification(
        message_id="leak-test-SEP-999",
        message_type="SEP",
        reported_issue_time=published_at,
        resolved_issue_time=published_at,
        url="https://example.invalid/leak-test",
        raw_entry={
            "messageID": "leak-test-SEP-999",
            "messageType": "SEP",
            "messageIssueTime": published_at.strftime("%Y-%m-%dT%H:%MZ"),
            "messageURL": "https://example.invalid/leak-test",
            "messageBody": body,
        },
    )


def test_late_publication_does_not_change_evidence_for_window(
    real_scenarios: list[Scenario], db_conn: sqlite3.Connection, raw_store: RawOriginalStore
) -> None:
    scenario = scenario_by_name(real_scenarios, "event")
    leaking = _future_leaking_notification(scenario)

    before = donki_evidence.evidence_for_window(
        db_conn,
        as_of=scenario.as_of,
        window_start=scenario.window_a_start,
        window_end=scenario.window_a_end,
    )
    assert before == []  # sanity: nothing inserted yet

    donki_evidence.ensure_donki_records(
        db_conn,
        raw_store,
        notifications=[leaking],
        source_url="https://example.invalid/leak-test",
        fetched_at=scenario.as_of,
    )
    after = donki_evidence.evidence_for_window(
        db_conn,
        as_of=scenario.as_of,
        window_start=scenario.window_a_start,
        window_end=scenario.window_a_end,
    )
    assert after == [], (
        "a DONKI notification published after as_of leaked into strict "
        "as_of replay evidence"
    )

    # And an as_of that finally reaches the leaking notification's own
    # publication time DOES see it — proving the exclusion above is really
    # about the as_of cutoff, not e.g. a broken query that excludes
    # everything.
    reachable = donki_evidence.evidence_for_window(
        db_conn,
        as_of=leaking.resolved_issue_time,  # type: ignore[arg-type]
        window_start=scenario.window_a_start,
        window_end=scenario.window_a_end,
    )
    assert any(entry.message_id == "leak-test-SEP-999" for entry in reachable)


def test_late_publication_does_not_change_the_production_result(
    real_scenarios: list[Scenario], monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end version of the same test through
    ``experiments.production.build_production_result``: inserting the same
    leaking notification alongside the real archive must not change a
    single field of the built result (main-prompt.md §1 mandatory leak
    test)."""
    scenario = scenario_by_name(real_scenarios, "event")

    def _build() -> dict[str, object]:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            conn = connect_store(tmp_path / "store.sqlite3")
            store = RawOriginalStore(tmp_path / "raw")
            try:
                with deterministic_record_ids("leak-test"):
                    build = production.build_production_result(scenario, conn=conn, raw_store=store)
            finally:
                conn.close()
        assert build.result is not None
        return build.result

    baseline_result = _build()

    real_load = fixtures.load_donki_notifications
    leaking = _future_leaking_notification(scenario)

    def _load_with_leak() -> list[archive_probe.DonkiNotification]:
        return [*real_load(), leaking]

    monkeypatch.setattr(fixtures, "load_donki_notifications", _load_with_leak)
    leaked_result = _build()

    assert leaked_result == baseline_result
