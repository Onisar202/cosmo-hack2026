"""Общие фикстуры для тестов API (src/api).

Сеть в тестах отсутствует (.ai/main-prompt.md §9 «тесты детерминированы»):
``src.sources.orbit.fetch_current_tle`` и ``src.sources.swpc.fetch``
подменяются на сохранённые реальные фикстуры (``tests/fixtures/orbit``,
``tests/fixtures/sources/swpc``), как это уже делают
``tests/orbit/test_propagation.py`` и ``tests/sources/test_swpc.py``.

Каждый тест получает свой временный каталог хранилища (``STORE_DB_PATH``/
``STORE_RAW_DIR``), поэтому тесты не делят состояние SQLite друг с другом —
как и ``tests/store/conftest.py`` для более низкого уровня.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.config import get_settings
from src.sources import orbit as orbit_source
from src.sources import swpc as swpc_source
from src.sources.http import HttpFetchResult

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"
ORBIT_TLE_SAMPLE = FIXTURES_DIR / "orbit" / "celestrak_iss_gp_sample.tle"
SWPC_SAMPLE = FIXTURES_DIR / "sources" / "swpc" / "integral-protons-1-day.sample.json"


def orbit_tle_bytes() -> bytes:
    return ORBIT_TLE_SAMPLE.read_bytes()


def swpc_sample_bytes() -> bytes:
    return SWPC_SAMPLE.read_bytes()


@pytest.fixture
def app_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """``TestClient`` за свежим, изолированным ``create_app()``.

    Орбита и поток протонов подменены сохранёнными реальными фикстурами по
    умолчанию (стандартный «счастливый путь»); отдельные тесты
    переопределяют ``src.sources.orbit.fetch_current_tle``/
    ``src.sources.swpc.fetch`` заново через ``monkeypatch``, чтобы
    смоделировать отказ конкретного источника.
    """
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("STORE_DB_PATH", str(tmp_path / "store.sqlite3"))
    monkeypatch.setenv("STORE_RAW_DIR", str(tmp_path / "raw"))
    get_settings.cache_clear()

    orbit_bytes = orbit_tle_bytes()

    def fake_fetch_current_tle(
        *,
        norad_id: str = orbit_source.ISS_NORAD_ID,
        client: httpx.Client | None = None,
        timeout: httpx.Timeout | None = None,
    ) -> bytes:
        return orbit_bytes

    monkeypatch.setattr(orbit_source, "fetch_current_tle", fake_fetch_current_tle)

    swpc_bytes = swpc_sample_bytes()

    def fake_http_fetch(url: str, **_kwargs: Any) -> HttpFetchResult:
        return HttpFetchResult(status_code=200, body=swpc_bytes, url=url, elapsed_seconds=0.001)

    monkeypatch.setattr(swpc_source, "fetch", fake_http_fetch)

    app = create_app()
    with TestClient(app) as client:
        yield client

    get_settings.cache_clear()


def wait_for_job(
    client: TestClient, task_id: str, *, timeout_seconds: float = 5.0
) -> dict[str, Any]:
    """Опрашивает ``GET /api/calculations/{task_id}`` до терминального статуса.

    Тестовый эквивалент клиента, ожидающего длительную операцию — реальный
    расчёт здесь занимает миллисекунды (нет сети), но выполняется в фоновом
    потоке (``loop.run_in_executor``), поэтому статус не готов сразу после
    202.
    """
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        response = client.get(f"/api/calculations/{task_id}")
        assert response.status_code == 200
        body = response.json()
        if body["status"] in ("done", "failed"):
            return body
        time.sleep(0.01)
    raise AssertionError(
        f"task {task_id} did not reach a terminal status within {timeout_seconds}s"
    )
