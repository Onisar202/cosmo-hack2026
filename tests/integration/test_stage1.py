"""Сквозная (end-to-end) проверка этапа 1 (FN-28, S1-10) против уже
развёрнутого стека — реального работающего сервиса, а не `TestClient` с
подменёнными источниками (в отличие от ``tests/api/``).

Не входит в состав тестов CI по умолчанию (`uv run pytest`): требует
запущенного сервиса. `pyproject.toml` регистрирует эти тесты под маркером
`integration` и исключает маркер из `addopts` по умолчанию.

Запуск (после `docker compose up --build -d`, см. README.md «Развёртывание»,
либо после `uv run python -m src.api.app` для локальной проверки без Docker):

    STAGE1_BASE_URL=http://127.0.0.1:8000 uv run pytest \\
        tests/integration/test_stage1.py -m integration -v

Что проверяется (приёмка FN-28, «сквозная проверка этапа»):

- health-проверка сервиса (`test_health`);
- полный путь запроса: API создаёт задание → получает реальный источник →
  считает орбиту → сохраняет результат (`test_current_mode_end_to_end_shape`,
  через фикстуру `completed_calculation`);
- перезапуск контейнера `api` не теряет сохранённый результат — постоянный
  том `compose.yaml` (`test_restart_persists_saved_result`);
- два одновременных запроса с разными параметрами не смешивают статусы и
  результаты (`test_two_concurrent_calculations_are_isolated`);
- статусы источников отдаются в реальной форме, отсутствие успешного
  получения — не подменяется нулём/выдумкой (`test_sources_status_shape`).

«Получение реального источника» (main-prompt.md §11) — часть приёмки этого
этапа, поэтому `mode=current` здесь намеренно не подменяет
`src.sources.orbit`/`src.sources.swpc`, как это делает `tests/api/`. Если
сеть, в которой выполняется проверка, не пропускает CelesTrak/NOAA SWPC (см.
README.md «Известное ограничение окружения сборки»), фикстура
`completed_calculation` останавливает зависящие от неё тесты через
`pytest.skip` с точной причиной вместо того, чтобы маскировать сетевой отказ
мок-источником — так тест не может случайно выдать честный отказ источника
за пройденную проверку.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
BASE_URL = os.environ.get("STAGE1_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
# Команда перезапуска api-контейнера для проверки персистентности (пусто —
# пропустить конкретно эту проверку без docker compose под рукой, например
# при проверке напрямую против `uv run python -m src.api.app`).
COMPOSE_RESTART_CMD = os.environ.get("STAGE1_COMPOSE_CMD", "docker compose")

JOB_POLL_TIMEOUT_SECONDS = 60.0
JOB_POLL_INTERVAL_SECONDS = 0.5
HEALTH_POLL_TIMEOUT_SECONDS = 60.0

# Единственные коды ошибок job.error.code, которые эта проверка принимает
# как «реальный источник недоступен из текущей сети» (src/api/service.py:
# `f"orbit_{orbit_fetch.outcome}"`, outcome ∈ {"error_source", "error_quota"}
# — см. src/sources/orbit.py). Любой другой код (result_schema_violation,
# orbit_propagation_failed, internal_error и т.п.) — это дефект развёртывания
# или сервиса, а не отсутствие сети, и обязан ронять проверку, а не
# маскироваться под неё (round 1 ревью PR #21).
SOURCE_UNAVAILABLE_ERROR_CODES = frozenset({"orbit_error_source", "orbit_error_quota"})


def _client() -> httpx.Client:
    return httpx.Client(base_url=BASE_URL, timeout=10.0)


def _current_mode_request(*, duration_hours: int, search_window_hours: int) -> dict[str, Any]:
    # "Пользователь сам задаёт... время показано с часовым поясом" — здесь
    # берётся реальное текущее время запуска проверки, а не зафиксированная
    # дата (main-prompt.md §12), как и полагается для mode=current.
    start_at = datetime.now(timezone.utc) + timedelta(minutes=1)
    return {
        "mode": "current",
        "start_at": start_at.isoformat().replace("+00:00", "Z"),
        "duration_hours": duration_hours,
        "search_window_hours": search_window_hours,
    }


def _wait_for_job(client: httpx.Client, task_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + JOB_POLL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        response = client.get(f"/api/calculations/{task_id}")
        assert response.status_code == 200, response.text
        body: dict[str, Any] = response.json()
        if body["status"] in ("done", "failed"):
            return body
        time.sleep(JOB_POLL_INTERVAL_SECONDS)
    raise AssertionError(
        f"task {task_id} did not reach a terminal status within "
        f"{JOB_POLL_TIMEOUT_SECONDS}s (last status unknown — timed out polling)"
    )


def _wait_for_health(client: httpx.Client, *, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            response = client.get("/health", timeout=3.0)
            if response.status_code == 200 and response.json().get("status") == "ok":
                return
        except httpx.HTTPError as exc:  # сервис ещё перезапускается
            last_error = exc
        time.sleep(1.0)
    raise AssertionError(
        f"/health did not report ok within {timeout_seconds}s after restart: {last_error}"
    )


@pytest.fixture(scope="module")
def base_client() -> Iterator[httpx.Client]:
    with _client() as client:
        yield client


def test_health(base_client: httpx.Client) -> None:
    response = base_client.get("/health")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"]
    # /health отдаёт tz-aware время (main-prompt.md §1) — валидная ISO-строка
    # со смещением, а не naive datetime.
    datetime.fromisoformat(body["time"])


@pytest.fixture(scope="module")
def completed_calculation(base_client: httpx.Client) -> dict[str, Any]:
    """Полный путь `mode=current` против реальных источников.

    Пропускает зависящие тесты (не проваливает), если реальный источник
    недоступен из этой сети, — см. docstring модуля.
    """
    payload = _current_mode_request(duration_hours=4, search_window_hours=8)
    create = base_client.post("/api/calculations", json=payload)
    assert create.status_code == 202, create.text
    task_id = create.json()["task_id"]

    job = _wait_for_job(base_client, task_id)
    if job["status"] == "failed":
        error = job.get("error") or {}
        code = error.get("code")
        if code not in SOURCE_UNAVAILABLE_ERROR_CODES:
            # НЕ пропускаем: этот код не входит в известный список признаков
            # сетевой недоступности источника — это похоже на реальный дефект
            # развёртывания/сервиса (например повреждённый ответ, ошибка
            # валидации контракта, внутренняя ошибка), и его обязана поймать
            # именно эта сквозная проверка, а не потеряться под видом
            # «источник недоступен» (round 1 ревью PR #21).
            pytest.fail(
                "mode=current завершился ошибкой с кодом, который НЕ входит в "
                f"список ожидаемых признаков недоступности источника "
                f"{sorted(SOURCE_UNAVAILABLE_ERROR_CODES)!r} — это дефект "
                f"развёртывания или сервиса, а не отсутствие сети. Полный "
                f"статус задания: {job!r}"
            )
        pytest.skip(
            "mode=current завершился отказом источника (ожидаемо, если сеть, в "
            "которой запущена проверка, не пропускает CelesTrak/NOAA SWPC — "
            "см. README.md «Известное ограничение окружения сборки»): "
            f"code={code!r} message={error.get('message')!r}. "
            "Это НЕ означает, что сервис вернул благоприятную оценку при "
            "отказе — job.status == 'failed', не 'done' (main-prompt.md §2)."
        )

    assert job["status"] == "done", job
    assert job["error"] is None
    result_id = job["result_id"]
    assert result_id

    result = base_client.get(f"/api/results/{result_id}")
    assert result.status_code == 200, result.text
    body: dict[str, Any] = result.json()
    body["_request_payload"] = payload
    return body


def test_current_mode_end_to_end_shape(completed_calculation: dict[str, Any]) -> None:
    body = completed_calculation

    assert body["mode"] == "current"
    assert body["as_of"] is None

    # Орбита реальная (не мок): источник и запись видны, современные
    # элементы не подставлены задним числом иначе, чем помечено
    # is_reconstructed (main-prompt.md §11 «Траектория»).
    orbit = body["orbit"]
    assert orbit["source"] == "celestrak"
    assert orbit["norad_id"] == "25544"
    assert orbit["record_id"]
    assert isinstance(orbit["is_reconstructed"], bool)

    # Manifest прослеживает фактически использованные записи.
    assert body["data_manifest"]
    assert any(m["record_id"] == orbit["record_id"] for m in body["data_manifest"])

    # Оба обязательных механизма (space_weather — FN-38, mmod — FN-39) теперь
    # реально интерпретируются, но это окно — целиком в будущем относительно
    # реального времени запуска проверки (start_at = сегодняшнее "сейчас" +
    # 1 минута, см. _current_mode_request): наблюдение GOES не имеет
    # собственного горизонта прогноза вперёд — честно "beyond_horizon", не
    # "not_implemented" (main-prompt.md §4); NASA MEO 2024 LEO forecast
    # покрывает только 2024-01-01..2025-01-01 — любое реальное "сегодня"
    # после этого лежит вне грида, честный "missing_data", а не имитация
    # спокойной обстановки (main-prompt.md §2).
    assert body["windows"]
    for window in body["windows"]:
        mechanisms = {m["mechanism"]: m for m in window["mechanisms"]}
        assert set(mechanisms) == {"space_weather", "mmod"}
        assert mechanisms["mmod"]["status"] == "missing_data"
        assert mechanisms["mmod"]["critical_gap"] is True
        assert mechanisms["space_weather"]["status"] == "beyond_horizon"
        for assessment in mechanisms.values():
            assert assessment["max_level"] is None

    source_ids = {s["source_id"] for s in body["source_status"]}
    assert {"celestrak-gp", "noaa-swpc-proton-flux"} <= source_ids


def test_restart_persists_saved_result(
    completed_calculation: dict[str, Any], base_client: httpx.Client
) -> None:
    """Перезапуск api-контейнера не теряет сохранённый результат (том compose.yaml)."""
    if not COMPOSE_RESTART_CMD:
        pytest.skip("STAGE1_COMPOSE_CMD пуст — перезапуск не выполняется по запросу")

    result_id = completed_calculation["result_id"]
    cmd = [*shlex.split(COMPOSE_RESTART_CMD), "restart", "api"]
    try:
        restart = subprocess.run(
            cmd, cwd=REPO_ROOT, capture_output=True, text=True, timeout=120
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        pytest.skip(f"не удалось выполнить {cmd!r}: {exc}")

    if restart.returncode != 0:
        pytest.skip(
            f"{cmd!r} завершился кодом {restart.returncode} — сервис, похоже, "
            f"запущен не через docker compose: stderr={restart.stderr.strip()!r}"
        )

    _wait_for_health(base_client, timeout_seconds=HEALTH_POLL_TIMEOUT_SECONDS)

    after_restart = base_client.get(f"/api/results/{result_id}")
    assert after_restart.status_code == 200, after_restart.text
    body = after_restart.json()
    # Сохранённый результат неизменяем (main-prompt.md §3) — после
    # перезапуска он должен быть побайтово тем же объектом, не пересчитан.
    assert body == {k: v for k, v in completed_calculation.items() if k != "_request_payload"}


def test_two_concurrent_calculations_are_isolated(base_client: httpx.Client) -> None:
    payload_a = _current_mode_request(duration_hours=3, search_window_hours=6)
    payload_b = _current_mode_request(duration_hours=5, search_window_hours=10)

    def submit(payload: dict[str, Any]) -> str:
        response = base_client.post("/api/calculations", json=payload)
        assert response.status_code == 202, response.text
        task_id: str = response.json()["task_id"]
        return task_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        task_id_a, task_id_b = pool.map(submit, (payload_a, payload_b))

    assert task_id_a != task_id_b

    job_a = _wait_for_job(base_client, task_id_a)
    job_b = _wait_for_job(base_client, task_id_b)

    # Изоляция — не «оба успешны», а «не смешались»: у каждого задания свой
    # статус и, если он завершился успехом, свой результат с собственными
    # параметрами запроса (main-prompt.md §9, тест 7 «изоляция»).
    for job, payload in ((job_a, payload_a), (job_b, payload_b)):
        if job["status"] != "done":
            continue
        result = base_client.get(f"/api/results/{job['result_id']}")
        assert result.status_code == 200, result.text
        body = result.json()
        assert body["request"]["duration_hours"] == payload["duration_hours"]
        assert body["request"]["search_window_hours"] == payload["search_window_hours"]

    if job_a["status"] == "done" and job_b["status"] == "done":
        assert job_a["result_id"] != job_b["result_id"]


def test_sources_status_shape(base_client: httpx.Client) -> None:
    response = base_client.get("/api/sources/status")
    assert response.status_code == 200, response.text
    statuses = response.json()
    assert isinstance(statuses, list)
    assert statuses

    for status in statuses:
        assert status["source_id"]
        assert isinstance(status["frozen"], bool)
        assert isinstance(status["quota_limited"], bool)
        # Отсутствие успешного получения — None, а не «0 секунд назад»
        # (main-prompt.md §2 «пропуск не заменяется нулём»).
        if status["last_success_at"] is not None:
            datetime.fromisoformat(status["last_success_at"].replace("Z", "+00:00"))
