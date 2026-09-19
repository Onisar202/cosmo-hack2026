"""Shared fixtures for ``tests/experiments`` (FN-43).

Follows the same pattern as ``tests/store/conftest.py``/
``tests/sources/conftest.py``: each test gets its own tmp_path-scoped
SQLite connection and raw-original-store directory, so tests never share
state or depend on run order.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from experiments.config import Scenario, load_scenarios
from src.store import RawOriginalStore, connect

SCENARIOS_PATH = Path(__file__).resolve().parent.parent.parent / "experiments" / "scenarios.yaml"


@pytest.fixture
def db_conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = connect(tmp_path / "store.sqlite3")
    yield conn
    conn.close()


@pytest.fixture
def raw_store(tmp_path: Path) -> RawOriginalStore:
    return RawOriginalStore(tmp_path / "raw")


@pytest.fixture(scope="module")
def real_scenarios() -> list[Scenario]:
    """The real, committed ``experiments/scenarios.yaml`` — used by tests
    that must run against the actual event/control/missing_data scenarios
    this stand ships with."""
    return load_scenarios(SCENARIOS_PATH)


def scenario_by_name(scenarios: list[Scenario], name: str) -> Scenario:
    for scenario in scenarios:
        if scenario.name == name:
            return scenario
    raise KeyError(f"no scenario named {name!r} in {[s.name for s in scenarios]}")
