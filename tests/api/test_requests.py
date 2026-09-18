"""Тесты API S1-07: форма запроса/ответа, сбои источников, «сбои» (§1–2,
«Интерфейс и форма результата», «Сбои» — постановка, main-prompt.md §9,
приёмка FN-26).
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from src.sources import orbit as orbit_source
from src.sources import swpc as swpc_source
from src.sources.http import SourceTimeoutError
from tests.api.conftest import wait_for_job

CURRENT_REQUEST: dict[str, Any] = {
    "mode": "current",
    "start_at": "2026-09-18T09:00:00Z",
    "duration_hours": 4,
    "search_window_hours": 8,
}


def test_create_calculation_returns_202_pending(app_client: TestClient) -> None:
    response = app_client.post("/api/calculations", json=CURRENT_REQUEST)

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "pending"
    assert body["task_id"]


def test_get_calculation_status_unknown_task_is_404(app_client: TestClient) -> None:
    response = app_client.get("/api/calculations/does-not-exist")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "task_not_found"


def test_get_result_unknown_id_is_404(app_client: TestClient) -> None:
    response = app_client.get("/api/results/does-not-exist")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "result_not_found"


def test_current_mode_full_flow_returns_real_orbit_and_not_implemented_mechanisms(
    app_client: TestClient,
) -> None:
    create = app_client.post("/api/calculations", json=CURRENT_REQUEST)
    assert create.status_code == 202
    task_id = create.json()["task_id"]

    job = wait_for_job(app_client, task_id)
    assert job["status"] == "done", job
    assert job["error"] is None
    result_id = job["result_id"]
    assert result_id

    result = app_client.get(f"/api/results/{result_id}")
    assert result.status_code == 200
    body = result.json()

    assert body["result_id"] == result_id
    assert body["mode"] == "current"
    assert body["as_of"] is None
    assert body["request"]["duration_hours"] == CURRENT_REQUEST["duration_hours"]
    assert body["request"]["start_at"] == CURRENT_REQUEST["start_at"]
    assert "as_of" not in body["request"]  # current: as_of отсутствует как ключ

    # Орбита реальная: эпоха фикстуры tests/fixtures/orbit/README.md
    assert body["orbit"]["source"] == "celestrak"
    assert body["orbit"]["norad_id"] == "25544"
    assert body["orbit"]["elements_epoch"].startswith("2020-01-01")
    assert body["orbit"]["record_id"]
    assert body["orbit"]["is_reconstructed"] is True  # фикстура давно устарела

    # Manifest — только фактически использованная запись (орбита); поток
    # протонов получен, но не использован (интерпретация not_implemented).
    assert len(body["data_manifest"]) == 1
    assert body["data_manifest"][0]["record_id"] == body["orbit"]["record_id"]
    assert body["data_manifest"][0]["record_kind"] == "orbital_elements"

    assert len(body["windows"]) >= 2
    for window in body["windows"]:
        mechanisms = {m["mechanism"]: m for m in window["mechanisms"]}
        assert set(mechanisms) == {"space_weather", "mmod"}
        for assessment in mechanisms.values():
            assert assessment["status"] == "not_implemented"
            assert assessment["max_level"] is None
            assert assessment["exceedance_hours_by_level"] is None
            assert assessment["record_ids"] == []
        assert window["excluded_from_comparison"] is True
        assert window["exclusion_reason"]

    assert body["recommendation"]["status"] == "all_windows_excluded"
    assert body["recommendation"]["window_id"] is None

    source_ids = {s["source_id"] for s in body["source_status"]}
    assert source_ids == {"celestrak-gp", "noaa-swpc-proton-flux"}
    swpc_status = next(
        s for s in body["source_status"] if s["source_id"] == "noaa-swpc-proton-flux"
    )
    assert swpc_status["last_success_at"] is not None  # реальная фикстура успешно получена


@pytest.mark.parametrize("mode", ["historical_analysis", "historical_forecast"])
def test_historical_modes_fail_with_clear_not_implemented(
    app_client: TestClient, mode: str
) -> None:
    payload: dict[str, Any] = {
        "mode": mode,
        "start_at": "2024-05-10T09:00:00Z",
        "duration_hours": 4,
        "search_window_hours": 8,
    }
    if mode == "historical_forecast":
        payload["as_of"] = "2024-05-09T00:00:00Z"

    create = app_client.post("/api/calculations", json=payload)
    assert create.status_code == 202
    task_id = create.json()["task_id"]

    job = wait_for_job(app_client, task_id)

    assert job["status"] == "failed"
    assert job["result_id"] is None
    assert job["error"]["code"] == "historical_mode_not_implemented"
    assert job["error"]["message"]  # не пустая, понятная ошибка, не фиктивный успех


def test_orbit_source_failure_fails_the_job_not_a_fake_success(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing_fetch(**_kwargs: Any) -> bytes:
        raise orbit_source.OrbitSourceError("simulated CelesTrak outage")

    monkeypatch.setattr(orbit_source, "fetch_current_tle", failing_fetch)

    create = app_client.post("/api/calculations", json=CURRENT_REQUEST)
    task_id = create.json()["task_id"]

    job = wait_for_job(app_client, task_id)

    assert job["status"] == "failed"
    assert job["error"]["code"] == "orbit_error_source"


def test_swpc_source_failure_does_not_fail_the_job(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """При отказе одного источника (поток протонов) остальные — орбита,
    результат в целом — остаются доступны (приёмка FN-26)."""

    def failing_fetch(url: str, **_kwargs: Any) -> None:
        raise SourceTimeoutError(f"simulated timeout for {url}")

    monkeypatch.setattr(swpc_source, "fetch", failing_fetch)

    create = app_client.post("/api/calculations", json=CURRENT_REQUEST)
    task_id = create.json()["task_id"]

    job = wait_for_job(app_client, task_id)
    assert job["status"] == "done", job

    result = app_client.get(f"/api/results/{job['result_id']}").json()
    assert result["orbit"]["record_id"]  # орбита всё равно есть

    swpc_status = next(
        s for s in result["source_status"] if s["source_id"] == "noaa-swpc-proton-flux"
    )
    assert swpc_status["last_error_message"] is not None
    assert any(w["mechanism"] == "space_weather" for w in result["warnings"])


@pytest.mark.parametrize(
    "overrides",
    [
        {"start_at": "2026-09-18T09:00:00"},  # naive datetime, no offset
        {"duration_hours": 0.5},  # below minimum
        {"duration_hours": 9},  # above maximum
        {"search_window_hours": 25},  # above maximum
        {"mode": "current", "as_of": "2026-09-18T00:00:00Z"},  # as_of forbidden for current
        {"mode": "historical_forecast"},  # as_of missing
        {
            "mode": "historical_forecast",
            "as_of": "2026-09-18T10:00:00Z",  # as_of after start_at
        },
        {
            "mode": "historical_analysis",
            "start_at": "2024-04-30T23:59:59Z",  # before archive start
        },
        {
            "mode": "historical_analysis",
            "start_at": "2024-07-01T00:00:00Z",  # after archive end
        },
        {"lighting_constraint": {"requires_sunlight": True}},  # not implemented yet
        {"unexpected_field": "nope"},  # additionalProperties: false
    ],
)
def test_invalid_requests_are_rejected_with_uniform_error_format(
    app_client: TestClient, overrides: dict[str, Any]
) -> None:
    payload = {**CURRENT_REQUEST, **overrides}

    response = app_client.post("/api/calculations", json=payload)

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "invalid_request"
    assert body["error"]["message"]


def test_list_results_and_sources_status(app_client: TestClient) -> None:
    create = app_client.post("/api/calculations", json=CURRENT_REQUEST)
    task_id = create.json()["task_id"]
    job = wait_for_job(app_client, task_id)
    assert job["status"] == "done"

    listing = app_client.get("/api/results")
    assert listing.status_code == 200
    items = listing.json()
    assert any(item["result_id"] == job["result_id"] for item in items)

    statuses = app_client.get("/api/sources/status")
    assert statuses.status_code == 200
    ids = {s["source_id"] for s in statuses.json()}
    assert ids == {"celestrak-gp", "noaa-swpc-proton-flux"}


def test_refresh_sources_forces_a_fetch(app_client: TestClient) -> None:
    response = app_client.post("/api/sources/refresh")

    assert response.status_code == 200
    statuses = {s["source_id"]: s for s in response.json()}
    assert statuses["celestrak-gp"]["last_success_at"] is not None
    assert statuses["noaa-swpc-proton-flux"]["last_success_at"] is not None
