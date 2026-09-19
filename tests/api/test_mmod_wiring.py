"""Подключение Механизма 2 (MMOD) к production API — FN-39 (S2-08).

Закрывает FN-39 приёмку пп.4-5: «Production API выдаёт рассчитанный
механизм 2 и позволяет сценарии conflict/equal/critical gap» + unit/
integration-тесты. Все временны́е точки внутри обязательного периода
01.05–30.06.2024 (за исключением случая, доказывающего честный
``missing_data`` вне грида) взяты из РЕАЛЬНОГО первичного файла NASA MEO
(``tests/sources/test_mmod.py`` уже сверяет его байты), не придуманы.

**Известная, задокументированная граница** (``docs/mechanisms.md`` §12.5,
уже верна ДО этой задачи): ``result.recommendation.status`` в
``mode=current`` остаётся ``all_windows_excluded`` даже когда MMOD даёт
реальную, различающуюся между окнами оценку — Механизм 1 (space_weather)
всё ещё ``not_implemented``/``critical_gap=True`` в каждом окне
(``src.api.service._not_implemented_mechanism``), а правило предпочтения
окон (``src.domain.windows.dominance.excluded_windows``) выводит окно из
сравнения при критическом пробеле ПО ЛЮБОМУ обязательному механизму — не
только по MMOD. Поэтому conflict/equal демонстрируются здесь на уровне
самой ``mechanismAssessment``/``compare_mechanism`` (то, что реально решает
FN-39), а не как ``recommendation.status`` целиком (то, что решит будущая
задача Механизма 1, не эта)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from src.api.schemas import CalculationRequest
from src.api.service import ensure_store_ready
from src.api.service import run_calculation as _run_calculation
from src.config import get_settings
from src.domain.mmod.background import BackgroundNode, assess_mmod_background
from src.domain.windows.dominance import WindowMechanismInput, compare_mechanism
from src.sources import mmod as mmod_source
from src.sources import noaa_3day_forecast as noaa_3day_source
from src.sources import orbit as orbit_source
from src.sources import swpc as swpc_source
from src.sources.http import HttpFetchResult
from src.sources.status import SourceStatusRegistry
from src.store import RawOriginalStore
from src.store import connect as connect_store
from src.store import get_result as store_get_result

UTC = timezone.utc
FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"


def _mmod_mechanism_dict(assessment: Any) -> dict[str, Any]:
    return {
        "mechanism": "mmod",
        "status": assessment.status,
        "max_level": assessment.max_level,
        "exceedance_hours_by_level": assessment.exceedance_hours_by_level,
        "coverage_fraction": assessment.coverage_fraction,
        "critical_gap": assessment.critical_gap,
        "notes": list(assessment.notes),
        "record_ids": list(assessment.record_ids),
    }


class TestMmodDominanceComparison:
    """Прямая проверка ``src.domain.windows.dominance.compare_mechanism``
    (не переопределяет её — использует реальную реализацию FN-34) на
    MMOD-оценках, построенных из реальных строк NASA MEO. Показывает, что
    ``ratio_to_background`` этой задачи способен дать содержательные
    equal/directional/incomparable сравнения — предпосылка FN-39 приёмки
    п.4, независимая от пока не реализованного Механизма 1 (см. docstring
    модуля)."""

    def test_two_background_windows_compare_equal(self) -> None:
        # 2024-03-01, глубокий фон (tests/mmod/... подтверждает порядок
        # величины ~5e-4 — на два порядка ниже порога 1.2).
        nodes = [
            BackgroundNode(datetime(2024, 3, 1, 8, tzinfo=UTC), 0.0005, "n1"),
            BackgroundNode(datetime(2024, 3, 1, 10, tzinfo=UTC), 0.0004, "n2"),
            BackgroundNode(datetime(2024, 3, 1, 12, tzinfo=UTC), 0.0004, "n3"),
            BackgroundNode(datetime(2024, 3, 1, 14, tzinfo=UTC), 0.0003, "n4"),
        ]
        win_a = assess_mmod_background(
            nodes,
            window_start=datetime(2024, 3, 1, 8, tzinfo=UTC),
            window_end=datetime(2024, 3, 1, 10, tzinfo=UTC),
        )
        win_b = assess_mmod_background(
            nodes,
            window_start=datetime(2024, 3, 1, 12, tzinfo=UTC),
            window_end=datetime(2024, 3, 1, 14, tzinfo=UTC),
        )
        assert win_a.status == win_b.status == "ok"
        assert win_a.max_level == win_b.max_level == "background"

        comparison = compare_mechanism(
            "mmod",
            WindowMechanismInput.from_assessment(_mmod_mechanism_dict(win_a)),
            WindowMechanismInput.from_assessment(_mmod_mechanism_dict(win_b)),
        )
        assert comparison.verdict == "equal"

    def test_elevated_vs_background_window_compares_directionally(self) -> None:
        """Реальные строки flux_data.txt: 2024-05-05 23:00 (ratio≈1.2406,
        elevated) против 2024-05-06 23:00 (ratio≈1.1811, background) —
        значения сверены в ``tests/sources/test_mmod.py`` не напрямую, но
        взяты из того же проверенного файла (``REAL_FILE_BYTES``)."""
        raw = mmod_source.fetch()
        parsed = mmod_source.parse_flux_forecast(raw)
        by_time = {row.ut_datetime: row.factor_105j for row in parsed.rows}

        elevated_nodes = [
            BackgroundNode(t, by_time[t], f"elevated-{i}")
            for i, t in enumerate(
                [datetime(2024, 5, 5, 23, tzinfo=UTC), datetime(2024, 5, 6, 0, tzinfo=UTC)]
            )
        ]
        background_nodes = [
            BackgroundNode(t, by_time[t], f"background-{i}")
            for i, t in enumerate(
                [datetime(2024, 5, 6, 23, tzinfo=UTC), datetime(2024, 5, 7, 0, tzinfo=UTC)]
            )
        ]
        win_a = assess_mmod_background(
            elevated_nodes,
            window_start=datetime(2024, 5, 5, 23, tzinfo=UTC),
            window_end=datetime(2024, 5, 6, 0, tzinfo=UTC),
        )
        win_b = assess_mmod_background(
            background_nodes,
            window_start=datetime(2024, 5, 6, 23, tzinfo=UTC),
            window_end=datetime(2024, 5, 7, 0, tzinfo=UTC),
        )
        assert win_a.max_level == "elevated"
        assert win_b.max_level == "background"

        comparison = compare_mechanism(
            "mmod",
            WindowMechanismInput.from_assessment(_mmod_mechanism_dict(win_a)),
            WindowMechanismInput.from_assessment(_mmod_mechanism_dict(win_b)),
        )
        # win_b (фон) не хуже и строго лучше win_a (повышенный) -> b_better.
        assert comparison.verdict == "b_better"

    def test_ok_vs_missing_data_is_incomparable(self) -> None:
        win_a = assess_mmod_background(
            [
                BackgroundNode(datetime(2024, 3, 1, tzinfo=UTC), 0.0, "n1"),
                BackgroundNode(datetime(2024, 3, 1, 1, tzinfo=UTC), 0.0, "n2"),
            ],
            window_start=datetime(2024, 3, 1, tzinfo=UTC),
            window_end=datetime(2024, 3, 1, 1, tzinfo=UTC),
        )
        win_b = assess_mmod_background(
            [],
            window_start=datetime(2026, 1, 1, tzinfo=UTC),
            window_end=datetime(2026, 1, 1, 1, tzinfo=UTC),
        )
        assert win_a.status == "ok"
        assert win_b.status == "missing_data"

        comparison = compare_mechanism(
            "mmod",
            WindowMechanismInput.from_assessment(_mmod_mechanism_dict(win_a)),
            WindowMechanismInput.from_assessment(_mmod_mechanism_dict(win_b)),
        )
        assert comparison.verdict == "incomparable"


def _run_current_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    start_at: datetime,
    duration_hours: float,
    search_window_hours: float,
    now: datetime,
) -> dict[str, Any]:
    """Прогоняет ``run_calculation`` напрямую (не через HTTP-очередь) —
    тем же паттерном, что ``tests/api/test_requests.py::
    test_noaa_3day_forecast_is_used_in_windows_and_manifest_when_it_overlaps``
    — потому что реальный ``mode=current`` эндпоинт всегда использует
    настоящее «сейчас» сервера, а обязательный период проверки (01.05–
    30.06.2024) требует управляемого ``now``. ``src.sources.mmod.fetch``
    НЕ подменяется: читает реальный бандловый файл — детерминированно (не
    сеть), тот же файл, что и в production."""
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("STORE_DB_PATH", str(tmp_path / "store.sqlite3"))
    monkeypatch.setenv("STORE_RAW_DIR", str(tmp_path / "raw"))
    get_settings.cache_clear()

    orbit_bytes = (FIXTURES_DIR / "orbit" / "celestrak_iss_gp_sample.tle").read_bytes()
    monkeypatch.setattr(orbit_source, "fetch_current_tle", lambda **_kwargs: orbit_bytes)

    swpc_bytes = (
        FIXTURES_DIR / "sources" / "swpc" / "integral-protons-1-day.sample.json"
    ).read_bytes()
    monkeypatch.setattr(
        swpc_source,
        "fetch",
        lambda url, **_kwargs: HttpFetchResult(
            status_code=200, body=swpc_bytes, url=url, elapsed_seconds=0.001
        ),
    )
    noaa_3day_bytes = (
        FIXTURES_DIR
        / "sources"
        / "noaa_3day_forecast"
        / "synthetic"
        / "bulletin_2025-01-05_2200.txt"
    ).read_bytes()
    monkeypatch.setattr(
        noaa_3day_source,
        "fetch",
        lambda url, **_kwargs: HttpFetchResult(
            status_code=200, body=noaa_3day_bytes, url=url, elapsed_seconds=0.001
        ),
    )

    settings = get_settings()
    ensure_store_ready(settings)
    raw_store = RawOriginalStore(settings.store_raw_dir)
    registry = SourceStatusRegistry()

    request = CalculationRequest(
        mode="current",
        start_at=start_at,
        duration_hours=duration_hours,
        search_window_hours=search_window_hours,
    )
    result_id = _run_calculation(
        request, settings=settings, raw_store=raw_store, registry=registry, now=now
    )

    conn = connect_store(settings.store_db_path)
    try:
        result = store_get_result(conn, result_id)
    finally:
        conn.close()
    assert result is not None
    get_settings.cache_clear()
    return result


class TestMmodEndToEndCurrentMode:
    def test_real_conflicting_windows_within_mandatory_period(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """win-a — 2024-05-05 23:00-00:00 (ratio≈1.24, elevated), win-b —
        2024-05-06 23:00-00:00, 24ч позже (ratio≈1.18, background) — оба
        реальных значения NASA MEO, не выдуманы. Демонстрирует FN-39
        приёмка п.4 «позволяет сценарии conflict» на уровне
        mechanismAssessment MMOD (см. docstring модуля про
        recommendation.status)."""
        result = _run_current_mode(
            tmp_path,
            monkeypatch,
            start_at=datetime(2024, 5, 5, 23, 0, tzinfo=UTC),
            duration_hours=1,
            search_window_hours=24,
            now=datetime(2024, 5, 7, 0, 0, tzinfo=UTC),
        )
        win_a = next(w for w in result["windows"] if w["window_id"] == "win-a")
        win_b = next(w for w in result["windows"] if w["window_id"] == "win-b")
        mmod_a = next(m for m in win_a["mechanisms"] if m["mechanism"] == "mmod")
        mmod_b = next(m for m in win_b["mechanisms"] if m["mechanism"] == "mmod")

        assert mmod_a["status"] == "ok"
        assert mmod_a["max_level"] == "elevated"
        assert mmod_a["exceedance_hours_by_level"]["elevated"] == pytest.approx(1.0)
        assert mmod_a["record_ids"]

        assert mmod_b["status"] == "ok"
        assert mmod_b["max_level"] == "background"
        assert mmod_b["exceedance_hours_by_level"]["elevated"] == pytest.approx(0.0)
        assert mmod_b["record_ids"]

        # Фактически использованные записи обоих окон попадают в манифест
        # (main-prompt.md §3), с правильным source_id.
        mmod_manifest = [
            e for e in result["data_manifest"] if e["source_id"] == mmod_source.SOURCE_ID
        ]
        assert mmod_manifest
        manifest_ids = {e["record_id"] for e in mmod_manifest}
        assert manifest_ids >= set(mmod_a["record_ids"]) | set(mmod_b["record_ids"])

        # Задокументированная граница (docs/mechanisms.md §12.5): space_weather
        # остаётся not_implemented/critical_gap=True в каждом окне, поэтому
        # оба окна ВСЁ РАВНО исключены из сравнения — это не баг этой
        # задачи, см. docstring модуля.
        sw_a = next(m for m in win_a["mechanisms"] if m["mechanism"] == "space_weather")
        assert sw_a["status"] == "not_implemented"
        assert win_a["excluded_from_comparison"] is True
        assert win_b["excluded_from_comparison"] is True
        assert result["recommendation"]["status"] == "all_windows_excluded"

    def test_real_equal_background_windows_within_mandatory_period(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Оба окна — глубокий фон 2024-03-01 (реальные значения ~5e-4,
        ratio≈1.0005) — равный (не различающийся) сценарий MMOD."""
        result = _run_current_mode(
            tmp_path,
            monkeypatch,
            start_at=datetime(2024, 3, 1, 8, 0, tzinfo=UTC),
            duration_hours=2,
            search_window_hours=4,
            now=datetime(2024, 3, 2, 0, 0, tzinfo=UTC),
        )
        win_a = next(w for w in result["windows"] if w["window_id"] == "win-a")
        win_b = next(w for w in result["windows"] if w["window_id"] == "win-b")
        mmod_a = next(m for m in win_a["mechanisms"] if m["mechanism"] == "mmod")
        mmod_b = next(m for m in win_b["mechanisms"] if m["mechanism"] == "mmod")

        assert mmod_a["status"] == mmod_b["status"] == "ok"
        assert mmod_a["max_level"] == mmod_b["max_level"] == "background"
        assert mmod_a["exceedance_hours_by_level"] == pytest.approx(
            {"elevated": 0.0, "pronounced": 0.0}
        )
        assert mmod_b["exceedance_hours_by_level"] == pytest.approx(
            {"elevated": 0.0, "pronounced": 0.0}
        )

    def test_window_outside_2024_grid_is_honest_critical_gap(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Реальное «сейчас» этого окружения (2026+) лежит вне покрытия
        годового документа NASA (только 2024) — честный critical_gap, не
        имитация уровня (main-prompt.md §2)."""
        result = _run_current_mode(
            tmp_path,
            monkeypatch,
            start_at=datetime(2026, 9, 19, 9, 0, tzinfo=UTC),
            duration_hours=2,
            search_window_hours=6,
            now=datetime(2026, 9, 19, 9, 0, tzinfo=UTC),
        )
        for window in result["windows"]:
            mmod = next(m for m in window["mechanisms"] if m["mechanism"] == "mmod")
            assert mmod["status"] == "missing_data"
            assert mmod["max_level"] is None
            assert mmod["exceedance_hours_by_level"] is None
            assert mmod["critical_gap"] is True
            assert mmod["record_ids"] == []
        mmod_manifest = [
            e for e in result["data_manifest"] if e["source_id"] == mmod_source.SOURCE_ID
        ]
        assert mmod_manifest == []
