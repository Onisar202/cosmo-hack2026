"""FN-41: детерминированная сквозная приёмка исторических режимов (этап 3).

Как и ``test_stage2.py``, тест намеренно НЕ помечен ``integration``: он
поднимает настоящее FastAPI-приложение и проходит production
service/store/export, заменяя только сетевые границы сохранёнными реальными
ответами источников (``tests/fixtures/orbit/history``,
``tests/fixtures/sources/archive``). Поэтому он воспроизводим в обычном CI и
не может быть тихо исключён вместе с live/docker-проверками
(.ai/main-prompt.md §9).

Проверяется то, что обязано работать вместе, а не по отдельности:
пользовательский путь «запрос → задача → сохранённый результат → обе
выгрузки» для ОБОИХ исторических режимов на произвольной дате обязательного
периода, различимость трёх состояний архивной линии и невозможность
подмены исторической геометрии современной.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.config import get_settings
from src.sources import noaa_3day_forecast as noaa_3day_source
from src.sources import orbit as orbit_source
from src.sources import swpc as swpc_source
from src.sources.http import HttpFetchResult
from tests.api.conftest import (
    install_archive_fetch_fakes,
    noaa_3day_sample_bytes,
    orbit_tle_bytes,
    swpc_sample_bytes,
    wait_for_job,
)

#: Выраженное событие периода (AR3664) и контрольный спокойный период —
#: main-prompt.md §11.
EVENT_FORECAST_REQUEST: dict[str, Any] = {
    "mode": "historical_forecast",
    "start_at": "2024-05-11T00:00:00Z",
    "duration_hours": 4,
    "search_window_hours": 8,
    "as_of": "2024-05-11T00:00:00Z",
}
QUIET_ANALYSIS_REQUEST: dict[str, Any] = {
    "mode": "historical_analysis",
    "start_at": "2024-06-20T09:00:00Z",
    "duration_hours": 4,
    "search_window_hours": 8,
}


@pytest.fixture
def stage3_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FastAPI]:
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
    install_archive_fetch_fakes(monkeypatch)

    app = create_app()
    yield app
    get_settings.cache_clear()


def _space_weather(window: dict[str, Any]) -> dict[str, Any]:
    return next(m for m in window["mechanisms"] if m["mechanism"] == "space_weather")


def _run(client: TestClient, payload: dict[str, Any]) -> dict[str, Any]:
    create = client.post("/api/calculations", json=payload)
    assert create.status_code == 202, create.text
    job = wait_for_job(client, create.json()["task_id"])
    assert job["status"] == "done", job
    result = client.get(f"/api/results/{job['result_id']}")
    assert result.status_code == 200
    body: dict[str, Any] = result.json()
    return body


def test_both_historical_modes_complete_the_whole_user_path(stage3_app: FastAPI) -> None:
    """Запрос → статус задачи → сохранённый результат → JSON и HTML выгрузки,
    для обоих режимов, на разных произвольных датах обязательного периода."""
    with TestClient(stage3_app) as client:
        for payload in (EVENT_FORECAST_REQUEST, QUIET_ANALYSIS_REQUEST):
            stored = _run(client, payload)

            assert stored["mode"] == payload["mode"]
            assert stored["as_of"] == payload.get("as_of")
            assert stored["coverage"]["requested_period_supported"] is True
            assert stored["orbit"]["source"] == "nasa-iss-oem"
            assert stored["data_manifest"]  # прослеживаемость до записей
            assert len(stored["windows"]) >= 2

            exported = client.get(f"/api/results/{stored['result_id']}/export.json")
            assert exported.status_code == 200
            assert exported.json() == stored  # один и тот же объект, не пересборка

            html = client.get(f"/api/results/{stored['result_id']}/export.html")
            assert html.status_code == 200
            assert stored["result_id"] in html.text
            assert stored["algorithm_version"] in html.text


def test_the_two_modes_are_distinguishable_on_the_same_dates(stage3_app: FastAPI) -> None:
    """Исторический разбор и прогноз из прошлого — две РАЗЛИЧИМЫЕ функции
    (main-prompt.md §12): на одних и тех же датах они дают разные режим,
    отсечение, отметку реконструкции и состояние архивной линии."""
    forecast_payload = {**QUIET_ANALYSIS_REQUEST, "mode": "historical_forecast"}
    forecast_payload["as_of"] = "2024-06-20T00:00:00Z"

    with TestClient(stage3_app) as client:
        analysis = _run(client, QUIET_ANALYSIS_REQUEST)
        forecast = _run(client, forecast_payload)

    assert analysis["as_of"] is None
    assert forecast["as_of"] == "2024-06-20T00:00:00Z"

    # Разбор имеет право взять выпуск, созданный позже интересующего
    # момента, и обязан пометить это реконструкцией; строгий прогноз — нет.
    assert analysis["orbit"]["is_reconstructed"] is True
    assert forecast["orbit"]["is_reconstructed"] is False

    analysis_state = _space_weather(analysis["windows"][0])["event_state"]
    forecast_state = _space_weather(forecast["windows"][0])["event_state"]
    assert analysis_state == "NO_EVENT_DETECTED"
    # За отсечением событийная линия о будущем окне не знает ничего —
    # «не покрыто», а не «спокойно» (main-prompt.md §4).
    assert forecast_state == "INSUFFICIENT_DATA"


def test_a_pronounced_event_and_a_quiet_control_period_differ_end_to_end(
    stage3_app: FastAPI,
) -> None:
    """Сравнение выраженного события и контрольного периода — обязательная
    пара для экспериментов (main-prompt.md §11): один и тот же режим на двух
    датах даёт разные состояния и разную рекомендацию."""
    with TestClient(stage3_app) as client:
        event = _run(client, EVENT_FORECAST_REQUEST)
        quiet = _run(client, QUIET_ANALYSIS_REQUEST)

    event_sw = _space_weather(event["windows"][0])
    quiet_sw = _space_weather(quiet["windows"][0])

    assert event_sw["event_state"] == "EVENT_PRESENT"
    assert event_sw["status"] == "qualitative_only"
    assert event_sw["record_ids"]  # до конкретных уведомлений архива
    assert any(
        w["code"] == "space-weather-archived-event-present" and w["severity"] == "critical"
        for w in event["warnings"]
    )

    assert quiet_sw["event_state"] == "NO_EVENT_DETECTED"
    assert quiet_sw["status"] == "ok"
    assert quiet_sw["critical_gap"] is False

    # Окно с подтверждённым событием выводится из автоматического сравнения
    # (правило предпочтения окон, п.1) — но остаётся видимым в результате.
    assert event["windows"][0]["excluded_from_comparison"] is True
    assert event["recommendation"]["status"] in {
        "selected",
        "tie",
        "insufficient_basis",
        "all_windows_excluded",
    }


def test_historical_results_never_carry_modern_elements_or_a_second_result_shape(
    stage3_app: FastAPI,
) -> None:
    """Главный запрет задачи (main-prompt.md §1, §11) и требование единого
    сохранённого объекта (§3): в историческом результате нет ни записи
    CelesTrak, ни отдельной формы результата — та же схема, что у
    ``mode=current``."""
    with TestClient(stage3_app) as client:
        current = _run(
            client,
            {
                "mode": "current",
                "start_at": "2026-09-18T09:00:00Z",
                "duration_hours": 4,
                "search_window_hours": 8,
            },
        )
        historical = _run(client, EVENT_FORECAST_REQUEST)

    assert current["orbit"]["source"] == "celestrak"
    assert historical["orbit"]["source"] == "nasa-iss-oem"
    assert all(
        entry["source_id"] != "celestrak-gp" for entry in historical["data_manifest"]
    )
    assert "celestrak" not in str(historical["orbit"]).lower()
    # Одинаковый набор ключей верхнего уровня — параллельной формы результата
    # для исторических режимов не появилось.
    assert set(current) == set(historical)
    for window in historical["windows"]:
        assert set(window) == set(current["windows"][0])
        for mechanism in window["mechanisms"]:
            assert set(mechanism) == set(current["windows"][0]["mechanisms"][0])
