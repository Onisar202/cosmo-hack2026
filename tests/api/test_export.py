"""Эндпойнты выгрузки: ``GET /api/results/{result_id}/export.json`` и
``.../export.html`` (FN-36, S2-06).

Приёмка FN-36 проверяется здесь на уровне HTTP (тот же ``app_client``, что
``tests/api/test_requests.py``): единая форма 404 для неизвестного
``result_id`` (п.3) и семантическое равенство JSON-выгрузки с
``GET /api/results/{result_id}`` (п.1) — оба читают один и тот же реально
посчитанный и сохранённый результат, не фикстуру.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from tests.api.conftest import wait_for_job

CURRENT_REQUEST: dict[str, Any] = {
    "mode": "current",
    "start_at": "2026-09-18T09:00:00Z",
    "duration_hours": 4,
    "search_window_hours": 8,
}


def _create_and_wait(app_client: TestClient) -> str:
    create = app_client.post("/api/calculations", json=CURRENT_REQUEST)
    assert create.status_code == 202
    job = wait_for_job(app_client, create.json()["task_id"])
    assert job["status"] == "done", job
    result_id: str = job["result_id"]
    return result_id


def test_export_json_unknown_result_id_is_the_same_404_as_get_result(
    app_client: TestClient,
) -> None:
    get_response = app_client.get("/api/results/does-not-exist")
    export_response = app_client.get("/api/results/does-not-exist/export.json")

    assert export_response.status_code == 404
    assert export_response.json() == get_response.json()
    assert export_response.json()["error"]["code"] == "result_not_found"


def test_export_html_unknown_result_id_is_the_same_404_as_get_result(
    app_client: TestClient,
) -> None:
    get_response = app_client.get("/api/results/does-not-exist")
    export_response = app_client.get("/api/results/does-not-exist/export.html")

    assert export_response.status_code == 404
    assert export_response.json() == get_response.json()
    assert export_response.json()["error"]["code"] == "result_not_found"


def test_export_json_is_semantically_equal_to_get_result(app_client: TestClient) -> None:
    result_id = _create_and_wait(app_client)

    get_response = app_client.get(f"/api/results/{result_id}")
    export_response = app_client.get(f"/api/results/{result_id}/export.json")

    assert export_response.status_code == 200
    assert export_response.headers["content-type"].startswith("application/json")
    assert export_response.json() == get_response.json()


def test_export_html_contains_key_fields_of_the_stored_result(app_client: TestClient) -> None:
    result_id = _create_and_wait(app_client)

    get_response = app_client.get(f"/api/results/{result_id}")
    stored = get_response.json()

    export_response = app_client.get(f"/api/results/{result_id}/export.html")

    assert export_response.status_code == 200
    assert export_response.headers["content-type"].startswith("text/html")
    html = export_response.text

    assert stored["result_id"] in html
    assert stored["algorithm_version"] in html
    for window in stored["windows"]:
        assert window["window_id"] in html
    for entry in stored["data_manifest"]:
        assert entry["record_id"] in html


def test_repeated_export_is_deterministic_and_does_not_change_stored_data(
    app_client: TestClient,
) -> None:
    """Приёмка п.4: повторный экспорт детерминирован и не меняет result_id/данные."""
    result_id = _create_and_wait(app_client)

    first_json = app_client.get(f"/api/results/{result_id}/export.json").json()
    second_json = app_client.get(f"/api/results/{result_id}/export.json").json()
    assert first_json == second_json

    first_html = app_client.get(f"/api/results/{result_id}/export.html").text
    second_html = app_client.get(f"/api/results/{result_id}/export.html").text
    assert first_html == second_html

    still_stored = app_client.get(f"/api/results/{result_id}").json()
    assert still_stored["result_id"] == result_id
    assert still_stored == first_json
