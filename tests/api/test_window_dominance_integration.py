"""FN-34 (S2-04): проверяет, что ``src.api.service._apply_window_dominance``
действительно делегирует ``excluded_from_comparison``/``exclusion_reason`` и
``recommendation`` правилу доминирования v2 (``src.domain.windows``), а не
хранит собственную копию логики — в отличие от прежней захардкоженной
константы ``all_windows_excluded``.

Не гоняет полный HTTP-пайплайн (сеть/хранилище не нужны, main-prompt.md §8):
строит ``window``-словари той же формы, что ``_window()``/
``_not_implemented_mechanism()`` в ``src/api/service.py``, напрямую — так
доступны сценарии (доминирование, конфликт, равенство), которые пока
недостижимы через реальный ``mode=current`` (space_weather/mmod там всегда
``not_implemented`` до задач зон 2/3).
"""

from __future__ import annotations

from typing import Any

from src.api.schemas import ALGORITHM_VERSION
from src.api.service import _apply_window_dominance

_OK_SW_BACKGROUND = {"S1": 1.0, "S2": 0.0, "S3": 0.0}
_OK_MMOD_BACKGROUND = {"elevated": 0.0, "pronounced": 0.0}


def _mechanism(
    mechanism: str,
    *,
    status: str = "ok",
    max_level: str | None,
    exceedance: dict[str, float] | None,
    critical_gap: bool = False,
) -> dict[str, Any]:
    return {
        "mechanism": mechanism,
        "status": status,
        "max_level": max_level,
        "exceedance_hours_by_level": exceedance,
        "coverage_fraction": 1.0,
        "critical_gap": critical_gap,
        "notes": [],
        "record_ids": [],
    }


def _window(
    window_id: str, *, duration_hours: float, sw: dict[str, Any], mmod: dict[str, Any]
) -> dict[str, Any]:
    return {
        "window_id": window_id,
        "start_at": "2026-09-18T09:00:00+00:00",
        "end_at": "2026-09-18T13:00:00+00:00",
        "duration_hours": duration_hours,
        "mechanisms": [sw, mmod],
        "lighting": {"requested": False, "status": "not_requested", "note": None},
    }


def test_algorithm_version_was_bumped_for_the_dominance_rule_change() -> None:
    assert ALGORITHM_VERSION == "0.2.0"


def test_apply_window_dominance_selects_the_dominating_window() -> None:
    windows = [
        _window(
            "win-a",
            duration_hours=4.0,
            sw=_mechanism("space_weather", max_level="S1", exceedance=_OK_SW_BACKGROUND),
            mmod=_mechanism("mmod", max_level="background", exceedance=_OK_MMOD_BACKGROUND),
        ),
        _window(
            "win-b",
            duration_hours=4.0,
            sw=_mechanism(
                "space_weather", max_level="S3", exceedance={"S1": 4.0, "S2": 2.0, "S3": 1.0}
            ),
            mmod=_mechanism("mmod", max_level="background", exceedance=_OK_MMOD_BACKGROUND),
        ),
    ]

    recommendation = _apply_window_dominance(windows)

    assert recommendation["status"] == "selected"
    assert recommendation["window_id"] == "win-a"
    win_a, win_b = windows
    assert win_a["excluded_from_comparison"] is False
    assert win_a["exclusion_reason"] is None
    assert win_b["excluded_from_comparison"] is False
    assert win_b["exclusion_reason"] is None


def test_apply_window_dominance_reports_conflict_as_insufficient_basis() -> None:
    windows = [
        _window(
            "win-a",
            duration_hours=4.0,
            sw=_mechanism("space_weather", max_level="S1", exceedance=_OK_SW_BACKGROUND),
            mmod=_mechanism(
                "mmod", max_level="pronounced", exceedance={"elevated": 3.0, "pronounced": 1.0}
            ),
        ),
        _window(
            "win-b",
            duration_hours=4.0,
            sw=_mechanism(
                "space_weather", max_level="S3", exceedance={"S1": 4.0, "S2": 2.0, "S3": 1.0}
            ),
            mmod=_mechanism("mmod", max_level="background", exceedance=_OK_MMOD_BACKGROUND),
        ),
    ]

    recommendation = _apply_window_dominance(windows)

    assert recommendation["status"] == "insufficient_basis"
    assert recommendation["window_id"] is None
    for window in windows:
        assert window["excluded_from_comparison"] is False


def test_apply_window_dominance_excludes_window_with_critical_gap_but_keeps_it_visible() -> None:
    windows = [
        _window(
            "win-a",
            duration_hours=4.0,
            sw=_mechanism(
                "space_weather",
                status="source_error",
                max_level=None,
                exceedance=None,
                critical_gap=True,
            ),
            mmod=_mechanism("mmod", max_level="background", exceedance=_OK_MMOD_BACKGROUND),
        ),
        _window(
            "win-b",
            duration_hours=4.0,
            sw=_mechanism("space_weather", max_level="S1", exceedance=_OK_SW_BACKGROUND),
            mmod=_mechanism("mmod", max_level="background", exceedance=_OK_MMOD_BACKGROUND),
        ),
    ]

    recommendation = _apply_window_dominance(windows)

    assert recommendation["status"] == "selected"
    assert recommendation["window_id"] == "win-b"
    win_a, win_b = windows
    assert win_a["excluded_from_comparison"] is True
    assert win_a["exclusion_reason"]
    assert win_b["excluded_from_comparison"] is False
    # окно с пробелом остаётся в списке окон результата, а не удаляется:
    assert [w["window_id"] for w in windows] == ["win-a", "win-b"]


def test_apply_window_dominance_all_windows_excluded_when_not_implemented() -> None:
    """Реальное сегодняшнее состояние сервиса: оба механизма
    ``not_implemented``/``critical_gap=True`` — то же наблюдаемое поведение,
    что и раньше захардкоженная константа, но теперь выведенное правилом."""
    not_implemented_sw = _mechanism(
        "space_weather", status="not_implemented", max_level=None, exceedance=None,
        critical_gap=True,
    )
    not_implemented_mmod = _mechanism(
        "mmod", status="not_implemented", max_level=None, exceedance=None, critical_gap=True
    )
    windows = [
        _window("win-a", duration_hours=4.0, sw=not_implemented_sw, mmod=not_implemented_mmod),
        _window("win-b", duration_hours=4.0, sw=not_implemented_sw, mmod=not_implemented_mmod),
    ]

    recommendation = _apply_window_dominance(windows)

    assert recommendation["status"] == "all_windows_excluded"
    assert recommendation["window_id"] is None
    for window in windows:
        assert window["excluded_from_comparison"] is True
        assert window["exclusion_reason"]
