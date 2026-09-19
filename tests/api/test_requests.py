"""Тесты API S1-07: форма запроса/ответа, сбои источников, «сбои» (§1–2,
«Интерфейс и форма результата», «Сбои» — постановка, main-prompt.md §9,
приёмка FN-26).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from src.api import routes as routes_module
from src.api.schemas import CalculationRequest
from src.api.service import ensure_store_ready
from src.api.service import run_calculation as _run_calculation
from src.config import get_settings
from src.sources import noaa_3day_forecast as noaa_3day_source
from src.sources import orbit as orbit_source
from src.sources import swpc as swpc_source
from src.sources.http import HttpFetchResult, SourceTimeoutError
from src.sources.status import SourceStatusRegistry
from src.store import RawOriginalStore
from src.store import connect as connect_store
from src.store import get_result as store_get_result
from tests.api.conftest import orbit_tle_bytes, swpc_sample_bytes, wait_for_job

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


def test_current_mode_full_flow_returns_real_orbit_and_honest_mechanism_gaps(
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

    # Manifest — только фактически использованная запись (орбита); фикстура
    # потока протонов (10 мая 2024) не пересекается с окном этого запроса
    # (18 сентября 2026), поэтому наблюдение получено, но не использовано —
    # ни одна запись не попадает в покрытый сегмент ни одного окна. MMOD
    # (FN-39) не добавляет запись в манифест здесь по той же причине:
    # CURRENT_REQUEST лежит в 2026 г., вне годового покрытия NASA MEO 2024
    # (2024-01-01..2025-01-01) — ни одной записи для этого окна не
    # существует, см. ниже.
    assert len(body["data_manifest"]) == 1
    assert body["data_manifest"][0]["record_id"] == body["orbit"]["record_id"]
    assert body["data_manifest"][0]["record_kind"] == "orbital_elements"

    assert len(body["windows"]) >= 2
    for window in body["windows"]:
        mechanisms = {m["mechanism"]: m for m in window["mechanisms"]}
        assert set(mechanisms) == {"space_weather", "mmod"}

        # space_weather (FN-38) теперь реально классифицируется — для ЭТОГО
        # окна пригодных отсчётов наблюдения нет (см. комментарий про
        # manifest выше), но источник исправен и свеж (фикстура только что
        # «получена»), поэтому причина — честный пробел покрытия
        # (missing_data), не отказ.
        space_weather = mechanisms["space_weather"]
        assert space_weather["status"] == "missing_data"
        assert space_weather["max_level"] is None
        assert space_weather["exceedance_hours_by_level"] is None
        assert space_weather["record_ids"] == []
        assert space_weather["critical_gap"] is True

        # FN-39: MMOD больше не заглушка not_implemented — реальная оценка
        # NASA MEO 2024 LEO forecast. CURRENT_REQUEST лежит в 2026 г., вне
        # годового покрытия документа (только 2024) — честный
        # critical_gap/missing_data, не имитация уровня и не
        # not_implemented (main-prompt.md §2 «за пределами данных — не
        # спокойная обстановка»).
        mmod = mechanisms["mmod"]
        assert mmod["status"] == "missing_data"
        assert mmod["max_level"] is None
        assert mmod["exceedance_hours_by_level"] is None
        assert mmod["critical_gap"] is True
        assert mmod["record_ids"] == []
        assert any("2024" in note for note in mmod["notes"])

        assert window["excluded_from_comparison"] is True
        assert window["exclusion_reason"]

    assert body["recommendation"]["status"] == "all_windows_excluded"
    assert body["recommendation"]["window_id"] is None

    source_ids = {s["source_id"] for s in body["source_status"]}
    # FN-31: NOAA 3-Day Forecast (S1+) — отдельная от потока протонов линия
    # того же Механизма 1, тоже получается и сохраняется каждым расчётом.
    assert source_ids == {"celestrak-gp", "noaa-swpc-proton-flux", "noaa-swpc-3day-forecast"}
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


def test_orbit_source_failure_falls_back_to_last_stored_record(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Отказ CelesTrak не роняет расчёт, если есть ранее сохранённый набор
    элементов: main-prompt.md §5 «последний пригодный ответ сохраняется и
    отдаётся с явной давностью, когда источник недоступен» (round 1 ревью
    PR #19)."""
    first = app_client.post("/api/calculations", json=CURRENT_REQUEST)
    first_job = wait_for_job(app_client, first.json()["task_id"])
    assert first_job["status"] == "done", first_job
    first_result = app_client.get(f"/api/results/{first_job['result_id']}").json()
    cached_record_id = first_result["orbit"]["record_id"]

    def failing_fetch(**_kwargs: Any) -> bytes:
        raise orbit_source.OrbitSourceError("simulated CelesTrak outage")

    monkeypatch.setattr(orbit_source, "fetch_current_tle", failing_fetch)

    second = app_client.post("/api/calculations", json=CURRENT_REQUEST)
    second_job = wait_for_job(app_client, second.json()["task_id"])

    assert second_job["status"] == "done", second_job  # not failed: fallback used
    second_result = app_client.get(f"/api/results/{second_job['result_id']}").json()
    assert second_result["result_id"] != first_result["result_id"]
    assert second_result["orbit"]["record_id"] == cached_record_id

    orbit_status = next(
        s for s in second_result["source_status"] if s["source_id"] == "celestrak-gp"
    )
    # Эта, вторая, попытка сама завершилась ошибкой — это видно, даже
    # несмотря на то, что расчёт в целом успешен благодаря fallback.
    assert orbit_status["last_error_message"] is not None

    fallback_warning = next(
        w for w in second_result["warnings"] if w["code"] == "orbit-source-stale-fallback"
    )
    assert fallback_warning["record_ids"] == [cached_record_id]
    assert fallback_warning["fetch_attempt_id"]


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

    # FN-38 приёмка п.3: timeout источника даёт явный status="source_error"
    # для space_weather — не благоприятную оценку и не 0 pfu (main-prompt.md
    # §2). Ни одной записи наблюдения нет вовсе (первая попытка сразу
    # завершилась ошибкой) — record_ids/max_level пусты.
    for window in result["windows"]:
        space_weather = next(
            m for m in window["mechanisms"] if m["mechanism"] == "space_weather"
        )
        assert space_weather["status"] == "source_error"
        assert space_weather["max_level"] is None
        assert space_weather["record_ids"] == []
        assert space_weather["critical_gap"] is True


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
    assert ids == {"celestrak-gp", "noaa-swpc-proton-flux", "noaa-swpc-3day-forecast"}


def test_refresh_sources_forces_a_fetch(app_client: TestClient) -> None:
    response = app_client.post("/api/sources/refresh")

    assert response.status_code == 200
    statuses = {s["source_id"]: s for s in response.json()}
    assert statuses["celestrak-gp"]["last_success_at"] is not None
    assert statuses["noaa-swpc-proton-flux"]["last_success_at"] is not None


def test_swpc_failure_warning_fetch_attempt_id_is_traceable_in_the_log(
    app_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``warning.fetch_attempt_id`` доказуемо ссылается на залогированную
    попытку (contracts/README.md «Каждое предупреждение доказуемо») — не
    просто отформатированная строка без следа (round 1 ревью PR #19)."""

    def failing_fetch(url: str, **_kwargs: Any) -> None:
        raise SourceTimeoutError(f"simulated timeout for {url}")

    monkeypatch.setattr(swpc_source, "fetch", failing_fetch)

    create = app_client.post("/api/calculations", json=CURRENT_REQUEST)
    task_id = create.json()["task_id"]
    job = wait_for_job(app_client, task_id)
    assert job["status"] == "done", job

    result = app_client.get(f"/api/results/{job['result_id']}").json()
    warning = next(w for w in result["warnings"] if w["mechanism"] == "space_weather")
    fetch_attempt_id = warning["fetch_attempt_id"]
    assert fetch_attempt_id

    logs = capsys.readouterr().err
    assert fetch_attempt_id in logs
    assert task_id in logs
    assert job["result_id"] in logs


def test_negative_elements_age_is_clamped_to_non_negative_in_the_stored_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``orbit.elements_age_hours`` — минимум 0 по контракту
    (contracts/result.schema.json); эпоха элементов «из будущего»
    относительно момента расчёта не должна проходить в хранилище
    отрицательным значением (round 1 ревью PR #19). API не даёт
    подставить произвольный ``now``, поэтому сценарий воспроизведён
    напрямую через ``service.run_calculation``."""
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("STORE_DB_PATH", str(tmp_path / "store.sqlite3"))
    monkeypatch.setenv("STORE_RAW_DIR", str(tmp_path / "raw"))
    get_settings.cache_clear()

    monkeypatch.setattr(
        orbit_source, "fetch_current_tle", lambda **_kwargs: orbit_tle_bytes()
    )
    monkeypatch.setattr(
        swpc_source,
        "fetch",
        lambda url, **_kwargs: HttpFetchResult(
            status_code=200, body=swpc_sample_bytes(), url=url, elapsed_seconds=0.001
        ),
    )

    settings = get_settings()
    ensure_store_ready(settings)
    raw_store = RawOriginalStore(settings.store_raw_dir)
    registry = SourceStatusRegistry()

    request = CalculationRequest(
        mode="current",
        start_at=datetime(2019, 1, 2, tzinfo=timezone.utc),
        duration_hours=2,
        search_window_hours=4,
    )
    # tests/fixtures/orbit/celestrak_iss_gp_sample.tle epoch is
    # 2020-01-01T19:42:47Z; "now" below is earlier than that epoch, so the
    # raw (signed) age would be negative without the fix.
    result_id = _run_calculation(
        request,
        settings=settings,
        raw_store=raw_store,
        registry=registry,
        now=datetime(2019, 1, 1, tzinfo=timezone.utc),
    )

    conn = connect_store(settings.store_db_path)
    try:
        result = store_get_result(conn, result_id)
    finally:
        conn.close()
    assert result is not None
    assert result["orbit"]["elements_age_hours"] >= 0
    assert any("рассинхронизация" in note for note in result["limitations"])

    get_settings.cache_clear()


def test_noaa_3day_forecast_is_used_in_windows_and_manifest_when_it_overlaps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FN-31 (round 3 ревью PR #24 — «линия не подключена к расчёту»):
    суточная вероятность S1+, реально пересекающаяся с окном, обязана быть
    видна в notes/record_ids окна и в data_manifest — не только получена и
    сохранена (это уже проверяет test_current_mode_full_flow_…), но и
    использована результатом (main-prompt.md §3 «манифест собирается
    фактически использованными записями»). Она по-прежнему НЕ создаёт
    собственного уровня механизма (FN-38 не меняет это — суточная
    вероятность и наблюдение GOES остаются разными величинами, см.
    src/domain/spaceweather/external_forecast.py). ``mechanisms[*].status``
    здесь — ``missing_data`` (не ``not_implemented``, с FN-38): наблюдение
    GOES теперь классифицируется по-настоящему (FN-38), но фикстура потока
    протонов датирована маем 2024, а это окно — январём 2025, поэтому для
    НЕГО пригодных отсчётов наблюдения нет — честный пробел покрытия, а не
    отсутствие реализации."""
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
    noaa_3day_bytes = (
        Path(__file__).resolve().parent.parent
        / "fixtures" / "sources" / "noaa_3day_forecast" / "synthetic"
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

    # Бюллетень (fixture) несёт S1+ на 05/06/07 января 2025: 20%/10%/5%,
    # выпущен 2025-01-05 22:00 UTC. Окно win-a (10:00-14:00 того же 05
    # января) целиком лежит внутри первого прогнозного дня.
    request = CalculationRequest(
        mode="current",
        start_at=datetime(2025, 1, 5, 10, 0, tzinfo=timezone.utc),
        duration_hours=4,
        search_window_hours=8,
    )
    result_id = _run_calculation(
        request,
        settings=settings,
        raw_store=raw_store,
        registry=registry,
        now=datetime(2025, 1, 6, 0, 0, tzinfo=timezone.utc),  # после выпуска бюллетеня
    )

    conn = connect_store(settings.store_db_path)
    try:
        result = store_get_result(conn, result_id)
    finally:
        conn.close()
    assert result is not None

    win_a = next(w for w in result["windows"] if w["window_id"] == "win-a")
    space_weather = next(m for m in win_a["mechanisms"] if m["mechanism"] == "space_weather")
    assert space_weather["status"] == "missing_data"
    assert space_weather["max_level"] is None
    assert space_weather["record_ids"]  # реально использованная запись прогноза
    assert any("20%" in note for note in space_weather["notes"])
    assert any("не вероятность ВКД" in note for note in space_weather["notes"])

    manifest_forecast_entries = [
        m for m in result["data_manifest"] if m["record_kind"] == "forecast"
    ]
    assert manifest_forecast_entries
    assert manifest_forecast_entries[0]["record_id"] in space_weather["record_ids"]
    assert manifest_forecast_entries[0]["source_id"] == noaa_3day_source.SOURCE_ID

    forecast_status = next(
        s for s in result["source_status"] if s["source_id"] == noaa_3day_source.SOURCE_ID
    )
    assert forecast_status["last_success_at"] is not None

    get_settings.cache_clear()


def test_unexpected_internal_error_returns_sanitized_message_not_raw_exception_text(
    app_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Текст непредвиденного исключения не должен уходить клиенту как есть —
    он может раскрыть внутренние детали (путь к БД, URL с ключом в
    query-строке и т.п.). round 1 ревью закрыл утечку в ответ клиенту;
    round 2 указал, что немаскированный текст всё ещё уходил в лог сервера
    (main-prompt.md §4 «маскирование на уровне логгера, а не на уровне
    дисциплины») — секрет обязан отсутствовать И в ответе, И в логе, лог
    несёт только тип исключения и task_id для сопоставления."""

    def failing_run_calculation(*_args: Any, **_kwargs: Any) -> str:
        raise RuntimeError("boom: leaking /var/secret/db-credentials.txt")

    monkeypatch.setattr(routes_module, "_run_calculation", failing_run_calculation)

    create = app_client.post("/api/calculations", json=CURRENT_REQUEST)
    task_id = create.json()["task_id"]
    job = wait_for_job(app_client, task_id)

    assert job["status"] == "failed"
    assert job["error"]["code"] == "internal_error"
    assert "secret" not in job["error"]["message"]
    assert "db-credentials" not in job["error"]["message"]
    assert task_id in job["error"]["message"]

    logs = capsys.readouterr().err
    assert "secret" not in logs
    assert "db-credentials" not in logs
    assert "RuntimeError" in logs  # тип исключения виден для разбора инцидента
    assert task_id in logs
