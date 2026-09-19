"""FN-34 (S2-04): проверяет, что ``src.api.service._apply_window_dominance``
действительно делегирует ``excluded_from_comparison``/``exclusion_reason`` и
``recommendation`` правилу доминирования v2 (``src.domain.windows``), а не
хранит собственную копию логики — в отличие от прежней захардкоженной
константы ``all_windows_excluded``.

Не гоняет полный HTTP-пайплайн (сеть/хранилище не нужны, main-prompt.md §8):
строит ``window``-словари той же формы, что ``_window()`` в
``src/api/service.py``, напрямую — так доступны все сценарии
(доминирование, конфликт, равенство) на контролируемых
``mechanismAssessment``, не завися от конкретных фикстур реального
``mode=current`` (оба механизма там уже реализованы, FN-38/FN-39, но какой
именно сценарий доминирования получится — зависит от дат запроса и
покрытия источников).
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


def test_algorithm_version_was_bumped_for_the_mmod_wiring_change() -> None:
    # FN-39 подключил реальный mmod поверх уже реального space_weather
    # (FN-38) — main-prompt.md §3 требует поднять ALGORITHM_VERSION при
    # каждом таком изменении алгоритма, даже когда наблюдаемый результат
    # части запросов не меняется (например окно вне грида NASA MEO 2024
    # оставалось и остаётся missing_data и до, и после этого бампа).
    # FN-41 (этап 3) — следующее такое изменение: два реальных исторических
    # режима, новое обязательное поле контракта event_state (видно в КАЖДОМ
    # сохранённом результате, включая mode=current) и новый статус механизма
    # qualitative_only.
    assert ALGORITHM_VERSION == "0.5.0"


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
    """Защитный сценарий для legacy/неполного producer payload.

    Production current-путь после FN-38/FN-39 больше не создаёт
    ``not_implemented`` для этих механизмов, однако доменное правило не
    должно случайно рекомендовать окно, если такой неполный payload пришёл
    от старой версии producer или сохранённой фикстуры.
    """
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
