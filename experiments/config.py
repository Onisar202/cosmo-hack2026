"""Typed loader for ``experiments/scenarios.yaml`` (FN-43).

Scenario dates and parameters are config-driven, not hardcoded in Python
(acceptance criterion 7): this module only parses and validates the YAML
into timezone-aware, typed :class:`Scenario` objects. It does not decide
anything about the calculation itself — ``experiments/production.py`` and
``experiments/baseline.py`` read archived records to derive their answers;
``expected_event`` here is carried through only for
``experiments/metrics.py`` grading, never fed into either method's
computation (main-prompt.md §9 — that would leak the answer into the thing
under test).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

UTC = timezone.utc


class ScenarioConfigError(ValueError):
    """``experiments/scenarios.yaml`` is missing a field or has an invalid value."""


def _parse_dt(raw: Any, *, field: str, scenario: str) -> datetime:
    if not isinstance(raw, str):
        raise ScenarioConfigError(f"scenario {scenario!r}: {field} must be an ISO 8601 string")
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ScenarioConfigError(
            f"scenario {scenario!r}: {field}={raw!r} is not a valid ISO 8601 datetime"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ScenarioConfigError(
            f"scenario {scenario!r}: {field}={raw!r} has no explicit UTC offset "
            "(.ai/main-prompt.md §1 — naive datetimes are rejected)"
        )
    return parsed.astimezone(UTC)


@dataclass(frozen=True)
class Scenario:
    """One fully-parsed scenario from ``experiments/scenarios.yaml``."""

    name: str
    description: str
    mode: str
    as_of: datetime
    duration_hours: float
    window_a_start: datetime
    window_b_start: datetime
    expected_event: bool | None

    @property
    def window_a_end(self) -> datetime:
        return self.window_a_start + timedelta(hours=self.duration_hours)

    @property
    def window_b_end(self) -> datetime:
        return self.window_b_start + timedelta(hours=self.duration_hours)

    @property
    def search_window_hours(self) -> float:
        """``search_end_at = start_at + search_window_hours``
        (contracts/request.schema.json) — derived from the two configured
        window starts, not a separate config field, so the two can never
        silently disagree."""
        return (self.window_b_start - self.window_a_start).total_seconds() / 3600.0


def load_scenarios(path: Path) -> list[Scenario]:
    """Parses and validates every scenario in ``path``.

    Raises :class:`ScenarioConfigError` for anything that would make the
    scenario unusable (missing field, non-UTC-aware datetime, duration/
    search-window out of the contract's bounds) — a bad config fails loudly
    here, not deep inside the production/baseline builders.
    """
    raw_data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw_data, dict) or "scenarios" not in raw_data:
        raise ScenarioConfigError(f"{path}: expected a top-level 'scenarios' list")
    entries = raw_data["scenarios"]
    if not isinstance(entries, list) or not entries:
        raise ScenarioConfigError(f"{path}: 'scenarios' must be a non-empty list")

    scenarios: list[Scenario] = []
    seen_names: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ScenarioConfigError(f"{path}: each scenario entry must be a mapping")
        try:
            name = str(entry["name"])
        except KeyError as exc:
            raise ScenarioConfigError(f"{path}: scenario entry is missing 'name'") from exc
        if name in seen_names:
            raise ScenarioConfigError(f"{path}: duplicate scenario name {name!r}")
        seen_names.add(name)

        mode = str(entry.get("mode", "historical_forecast"))
        if mode != "historical_forecast":
            raise ScenarioConfigError(
                f"scenario {name!r}: mode={mode!r} is not supported by this stand "
                "(only historical_forecast — the strict-replay mode this ticket "
                "exercises)"
            )

        try:
            duration_hours = float(entry["duration_hours"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ScenarioConfigError(
                f"scenario {name!r}: duration_hours must be a number"
            ) from exc
        if not (1.0 <= duration_hours <= 8.0):
            raise ScenarioConfigError(
                f"scenario {name!r}: duration_hours={duration_hours} outside [1, 8] "
                "(contracts/request.schema.json)"
            )

        as_of = _parse_dt(entry.get("as_of"), field="as_of", scenario=name)
        window_a_start = _parse_dt(
            entry.get("window_a_start"), field="window_a_start", scenario=name
        )
        window_b_start = _parse_dt(
            entry.get("window_b_start"), field="window_b_start", scenario=name
        )

        search_window_hours = (window_b_start - window_a_start).total_seconds() / 3600.0
        if not (0.0 <= search_window_hours <= 24.0):
            raise ScenarioConfigError(
                f"scenario {name!r}: window_b_start - window_a_start = "
                f"{search_window_hours}h outside [0, 24] "
                "(contracts/request.schema.json search_window_hours)"
            )

        if as_of > window_a_start:
            raise ScenarioConfigError(
                f"scenario {name!r}: as_of={as_of.isoformat()} is after "
                f"window_a_start={window_a_start.isoformat()} — a cutoff after the "
                "earliest window's start turns a strict forecast into a retrospective "
                "analysis mislabeled as historical_forecast (same rule as "
                "src.api.schemas.CalculationRequest, contracts/README.md "
                "«Три режима и as_of»)"
            )

        expected_event_raw = entry.get("expected_event", None)
        if expected_event_raw is not None and not isinstance(expected_event_raw, bool):
            raise ScenarioConfigError(
                f"scenario {name!r}: expected_event must be true, false, or omitted/null"
            )

        scenarios.append(
            Scenario(
                name=name,
                description=str(entry.get("description", "")).strip(),
                mode=mode,
                as_of=as_of,
                duration_hours=duration_hours,
                window_a_start=window_a_start,
                window_b_start=window_b_start,
                expected_event=expected_event_raw,
            )
        )
    return scenarios


__all__ = ["Scenario", "ScenarioConfigError", "load_scenarios"]
