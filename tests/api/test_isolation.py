"""Тесты изоляции конкурентных запросов (приёмка FN-26, .ai/main-prompt.md §9,
п.7 «Изоляция: два одновременных запроса с разными параметрами не смешивают
результаты», .ai/backend-prompt.md §2 «одновременные запросы изолированы:
параметры, статусы и результаты разных расчётов не смешиваются»).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from src.api.service import fetch_and_store_orbit
from src.sources import orbit as orbit_source
from src.sources.status import SourceStatusRegistry
from src.store import RawOriginalStore, connect
from tests.api.conftest import orbit_tle_bytes, wait_for_job

# Разные duration_hours/search_window_hours на каждый конкурентный запрос —
# если параметры где-то перепутаются между потоками (например через модульную
# переменную вместо явно переданного контекста), окна одного результата
# получат длительность другого запроса.
CONCURRENT_REQUESTS: list[dict[str, Any]] = [
    {
        "mode": "current",
        "start_at": "2026-09-18T09:00:00Z",
        "duration_hours": 2,
        "search_window_hours": 4,
    },
    {
        "mode": "current",
        "start_at": "2026-09-18T10:00:00Z",
        "duration_hours": 5,
        "search_window_hours": 12,
    },
    {
        "mode": "current",
        "start_at": "2026-09-18T11:00:00Z",
        "duration_hours": 8,
        "search_window_hours": 0,
    },
    {
        "mode": "current",
        "start_at": "2026-09-18T12:30:00Z",
        "duration_hours": 3,
        "search_window_hours": 6,
    },
]


def test_concurrent_calculations_do_not_mix_parameters_statuses_or_data(
    app_client: TestClient,
) -> None:
    # Все запросы создаются до того, как какой-либо из них успевает
    # завершиться (fire-and-forget 202), чтобы фоновые задачи реально
    # выполнялись параллельно, а не последовательно одна за другой.
    task_ids = []
    for payload in CONCURRENT_REQUESTS:
        response = app_client.post("/api/calculations", json=payload)
        assert response.status_code == 202
        task_ids.append(response.json()["task_id"])

    assert len(set(task_ids)) == len(task_ids)  # разные task_id на разные запросы

    jobs = [wait_for_job(app_client, task_id) for task_id in task_ids]
    for job, payload in zip(jobs, CONCURRENT_REQUESTS, strict=True):
        assert job["status"] == "done", (job, payload)

    result_ids = [job["result_id"] for job in jobs]
    assert len(set(result_ids)) == len(result_ids)  # разные result_id — пересчёта нет

    results = [app_client.get(f"/api/results/{rid}").json() for rid in result_ids]

    for result, payload in zip(results, CONCURRENT_REQUESTS, strict=True):
        # Каждый результат несёт ровно свои собственные параметры запроса —
        # не параметры другого конкурентного запроса.
        assert result["request"]["start_at"] == payload["start_at"]
        assert result["request"]["duration_hours"] == payload["duration_hours"]
        assert result["request"]["search_window_hours"] == payload["search_window_hours"]

        first_window = result["windows"][0]
        assert first_window["start_at"] == payload["start_at"]
        assert first_window["duration_hours"] == payload["duration_hours"]

    # Ни один результат не совпадает с другим по параметрам окна — прямое
    # доказательство, что окна не перепутались между конкурентными расчётами.
    window_starts = [r["windows"][0]["start_at"] for r in results]
    assert len(set(window_starts)) == len(window_starts)


def test_concurrent_calculations_have_independent_task_status(
    app_client: TestClient,
) -> None:
    """Статус одной задачи не протекает в статус другой, даже когда они
    выполняются в одном пуле потоков одновременно: одна задача — валидный
    запрос (даёт ``done``), вторая — заведомо неподдерживаемый исторический
    режим (даёт ``failed``)."""
    ok_payload = CONCURRENT_REQUESTS[0]
    failing_payload = {
        "mode": "historical_analysis",
        "start_at": "2024-05-15T00:00:00Z",
        "duration_hours": 2,
        "search_window_hours": 4,
    }

    with ThreadPoolExecutor(max_workers=2) as pool:
        ok_future = pool.submit(app_client.post, "/api/calculations", json=ok_payload)
        failing_future = pool.submit(
            app_client.post, "/api/calculations", json=failing_payload
        )
        ok_response = ok_future.result()
        failing_response = failing_future.result()

    assert ok_response.status_code == 202
    assert failing_response.status_code == 202
    ok_task_id = ok_response.json()["task_id"]
    failing_task_id = failing_response.json()["task_id"]
    assert ok_task_id != failing_task_id

    ok_job = wait_for_job(app_client, ok_task_id)
    failing_job = wait_for_job(app_client, failing_task_id)

    assert ok_job["status"] == "done"
    assert ok_job["error"] is None
    assert ok_job["result_id"]

    assert failing_job["status"] == "failed"
    assert failing_job["result_id"] is None
    assert failing_job["error"]["code"] == "historical_mode_not_implemented"

    # Повторное чтение обоих статусов позже подтверждает, что они не
    # перезаписали друг друга задним числом (не общая модульная переменная).
    assert app_client.get(f"/api/calculations/{ok_task_id}").json()["status"] == "done"
    assert (
        app_client.get(f"/api/calculations/{failing_task_id}").json()["status"] == "failed"
    )


def test_recompute_with_same_parameters_creates_a_new_immutable_result(
    app_client: TestClient,
) -> None:
    """Пересчёт с теми же параметрами — новый ``result_id``, не правка
    старого (.ai/main-prompt.md §3, приёмка FN-26 «результат неизменяем,
    пересчёт новый ID»)."""
    payload = CONCURRENT_REQUESTS[0]

    first = app_client.post("/api/calculations", json=payload)
    second = app_client.post("/api/calculations", json=payload)

    first_job = wait_for_job(app_client, first.json()["task_id"])
    second_job = wait_for_job(app_client, second.json()["task_id"])

    assert first_job["status"] == "done"
    assert second_job["status"] == "done"
    assert first_job["result_id"] != second_job["result_id"]

    first_result = app_client.get(f"/api/results/{first_job['result_id']}").json()
    second_result = app_client.get(f"/api/results/{second_job['result_id']}").json()
    assert first_result["result_id"] != second_result["result_id"]
    assert first_result["computed_at"] <= second_result["computed_at"]


def test_orbit_fetch_status_snapshot_is_not_mutated_by_a_later_concurrent_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``OrbitFetchResult.status`` — снимок, сделанный ИМЕННО этим вызовом
    (round 1 ревью PR #19): результат.source_status собирается из него, а не
    отдельным более поздним ``registry.get(source_id)``, который на общем
    (потокобезопасном, но именно поэтому и разделяемом между конкурентными
    расчётами) реестре мог бы уже отразить состояние другого, позже
    завершившегося запроса — тогда успешный расчёт мог бы сохранить в своём
    результате чужую ошибку вместо собственного исхода."""
    monkeypatch.setattr(orbit_source, "fetch_current_tle", lambda **_kwargs: orbit_tle_bytes())

    conn = connect(tmp_path / "store.sqlite3")
    raw_store = RawOriginalStore(tmp_path / "raw")
    registry = SourceStatusRegistry()

    t1 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    result = fetch_and_store_orbit(conn, raw_store, registry, now=t1)
    assert result.outcome == "stored"
    assert result.status.last_success_at == t1
    assert result.status.last_error_at is None

    # Имитирует более позднюю, никак не связанную ошибку на ТОМ ЖЕ реестре —
    # ровно то, что произвёл бы второй конкурентный запрос к тому же
    # source_id.
    t2 = t1 + timedelta(seconds=5)
    registry.record_error(
        orbit_source.SOURCE_ID_CURRENT, at=t2, message="unrelated concurrent failure",
        quota_limited=False,
    )

    # Снимок, захваченный ПЕРВЫМ вызовом, остаётся ровно тем, что увидел он
    # сам — не задет более поздней мутацией общего реестра.
    assert result.status.last_success_at == t1
    assert result.status.last_error_at is None
    assert result.status.last_error_message is None

    conn.close()
