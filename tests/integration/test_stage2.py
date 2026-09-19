"""FN-37: детерминированная сквозная приёмка обязательного пути этапа 2.

Тест намеренно не помечен ``integration``: он использует настоящее
FastAPI-приложение, production service/store/export и бандловый документ
NASA MEO, но заменяет только три сетевые границы сохранёнными ответами.
Поэтому он воспроизводим в обычном CI и не может быть тихо исключён вместе
с live/docker-проверками.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.api.schemas import CalculationRequest
from src.api.service import run_calculation
from src.config import get_settings
from src.sources import noaa_3day_forecast as noaa_3day_source
from src.sources import orbit as orbit_source
from src.sources import swpc as swpc_source
from src.sources.http import HttpFetchResult
from src.store import connect as connect_store
from src.store import insert_record
from tests.api.conftest import noaa_3day_sample_bytes, orbit_tle_bytes, swpc_sample_bytes

UTC = timezone.utc


@pytest.fixture
def stage2_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FastAPI]:
    """Изолированный production stack без нестабильной внешней сети."""

    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("STORE_DB_PATH", str(tmp_path / "store.sqlite3"))
    monkeypatch.setenv("STORE_RAW_DIR", str(tmp_path / "raw"))
    get_settings.cache_clear()

    monkeypatch.setattr(orbit_source, "fetch_current_tle", lambda **_kwargs: orbit_tle_bytes())
    monkeypatch.setattr(
        swpc_source,
        "fetch",
        lambda url, **_kwargs: HttpFetchResult(
            status_code=200, body=swpc_sample_bytes(), url=url, elapsed_seconds=0.001
        ),
    )
    monkeypatch.setattr(
        noaa_3day_source,
        "fetch",
        lambda url, **_kwargs: HttpFetchResult(
            status_code=200, body=noaa_3day_sample_bytes(), url=url, elapsed_seconds=0.001
        ),
    )

    app = create_app()
    yield app
    get_settings.cache_clear()


def _seed_observed_window(app: FastAPI, *, start_at: datetime, hours: int, pfu: float) -> None:
    """Создаёт контрактные GOES records через production-нормализатор.

    Значения сценария управляемые (5/15/150 pfu), а не выдаются за новые
    архивные наблюдения; реальные HTTP parsing/fetch и live source отдельно
    покрыты source-тестами и ``test_stage1.py``.
    """

    conn = connect_store(app.state.settings.store_db_path)
    try:
        for offset_minutes in range(0, hours * 60, 5):
            observed_at = start_at + timedelta(minutes=offset_minutes)
            sample = swpc_source.SwpcSample(
                satellite="18",
                observed_at=observed_at,
                value=pfu,
                degraded=False,
                raw_entry={
                    "time_tag": observed_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "satellite": 18,
                    "flux": pfu,
                    "energy": ">=10 MeV",
                    "yaw_flip": 0,
                },
            )
            insert_record(
                conn,
                app.state.raw_store,
                swpc_source.to_record_input(
                    sample,
                    source_url=(
                        "https://services.swpc.noaa.gov/json/goes/primary/"
                        "integral-protons-1-day.json"
                    ),
                    fetched_at=observed_at,
                ),
            )
    finally:
        conn.close()


def _calculate(app: FastAPI, request: CalculationRequest, *, now: datetime) -> str:
    return run_calculation(
        request,
        settings=app.state.settings,
        raw_store=app.state.raw_store,
        registry=app.state.source_registry,
        now=now,
    )


def _mechanisms(window: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["mechanism"]: item for item in window["mechanisms"]}


def test_conflict_result_is_identical_in_store_json_and_html(stage2_app: FastAPI) -> None:
    """Оба механизма покрыты, но предпочитают разные равные окна.

    Времена 23:10 не зашиты в исходные HTTP fixtures. На первом окне GOES
    лучше (фон против S2), на втором — MMOD (background против elevated),
    поэтому production-правило обязано вернуть честный конфликт.
    """

    start_at = datetime(2024, 5, 5, 23, 10, tzinfo=UTC)
    late_start = start_at + timedelta(hours=24)
    _seed_observed_window(stage2_app, start_at=start_at, hours=1, pfu=5.0)
    _seed_observed_window(stage2_app, start_at=late_start, hours=1, pfu=150.0)

    request = CalculationRequest(
        mode="current", start_at=start_at, duration_hours=1, search_window_hours=24
    )
    result_id = _calculate(
        stage2_app, request, now=late_start + timedelta(hours=2)
    )

    with TestClient(stage2_app) as client:
        stored_response = client.get(f"/api/results/{result_id}")
        json_response = client.get(f"/api/results/{result_id}/export.json")
        html_response = client.get(f"/api/results/{result_id}/export.html")

    assert (
        stored_response.status_code
        == json_response.status_code
        == html_response.status_code
        == 200
    )
    stored = stored_response.json()
    assert json_response.json() == stored
    assert stored["result_id"] == result_id
    assert stored["recommendation"]["status"] == "insufficient_basis"
    assert stored["recommendation"]["window_id"] is None
    assert len(stored["windows"]) == 2

    win_a, win_b = stored["windows"][:2]
    assert win_a["duration_hours"] == win_b["duration_hours"] == 1
    for window in (win_a, win_b):
        mechanisms = _mechanisms(window)
        assert set(mechanisms) == {"space_weather", "mmod"}
        assert all(item["status"] == "ok" for item in mechanisms.values())
        assert all(item["critical_gap"] is False for item in mechanisms.values())
        assert window["excluded_from_comparison"] is False

    assert _mechanisms(win_a)["space_weather"]["max_level"] == "background"
    assert _mechanisms(win_b)["space_weather"]["max_level"] == "S2"
    assert _mechanisms(win_a)["mmod"]["max_level"] == "elevated"
    assert _mechanisms(win_b)["mmod"]["max_level"] == "background"

    html = html_response.text
    assert result_id in html
    assert "Оснований для рекомендации недостаточно" in html
    assert "Космическая погода (поток протонов)" in html
    assert "MMOD (метеороидная составляющая)" in html
    for window in (win_a, win_b):
        assert window["window_id"] in html
        for mechanism in window["mechanisms"]:
            for record_id in mechanism["record_ids"]:
                assert record_id in html


def test_changed_start_and_duration_recompute_equal_windows_and_keep_gap_visible(
    stage2_app: FastAPI,
) -> None:
    """Новый старт 10:10 и длительность 2ч дают новый immutable result.

    Раннее окно полностью покрыто обоими механизмами, позднее намеренно не
    имеет GOES-наблюдений. Оно остаётся в ответе с critical_gap и не может
    победить; оба окна после изменения плана по-прежнему равной длительности.
    """

    start_at = datetime(2024, 5, 10, 10, 10, tzinfo=UTC)
    _seed_observed_window(stage2_app, start_at=start_at, hours=2, pfu=15.0)
    request = CalculationRequest(
        mode="current", start_at=start_at, duration_hours=2, search_window_hours=6
    )

    first_id = _calculate(stage2_app, request, now=start_at + timedelta(hours=10))
    second_id = _calculate(stage2_app, request, now=start_at + timedelta(hours=10))
    assert first_id != second_id

    with TestClient(stage2_app) as client:
        first = client.get(f"/api/results/{first_id}").json()
        second = client.get(f"/api/results/{second_id}").json()

    assert first["request"] == second["request"]
    assert first["result_id"] == first_id
    assert second["result_id"] == second_id
    assert {window["duration_hours"] for window in first["windows"]} == {2}

    win_a, win_b = first["windows"][:2]
    assert all(item["status"] == "ok" for item in _mechanisms(win_a).values())
    assert win_a["excluded_from_comparison"] is False
    assert _mechanisms(win_b)["space_weather"]["critical_gap"] is True
    assert win_b["excluded_from_comparison"] is True
    assert win_b["exclusion_reason"]
    assert first["recommendation"]["status"] == "selected"
    assert first["recommendation"]["window_id"] == "win-a"
    assert first["recommendation"]["explanation"]
