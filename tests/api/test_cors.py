"""FN-45: CORSMiddleware подключается только когда оператор явно задал
CORS_ALLOWED_ORIGINS — reverse proxy на одном origin (docker/nginx.conf.template)
не требует CORS вовсе, и middleware не должен появляться "просто на всякий
случай" (main-prompt.md §7: настройки — из конфига, поведение по умолчанию
предсказуемо).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.config import get_settings


@pytest.fixture
def isolated_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("STORE_DB_PATH", str(tmp_path / "store.sqlite3"))
    monkeypatch.setenv("STORE_RAW_DIR", str(tmp_path / "raw"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_no_cors_headers_when_origins_not_configured(
    isolated_app: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CORS_ALLOWED_ORIGINS", raising=False)
    with TestClient(create_app()) as client:
        response = client.get("/health", headers={"Origin": "https://someone-else.example.org"})
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


def test_cors_headers_present_for_configured_origin(
    isolated_app: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://vkd-risk.example.org")
    with TestClient(create_app()) as client:
        response = client.get("/health", headers={"Origin": "https://vkd-risk.example.org"})
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "https://vkd-risk.example.org"


def test_cors_headers_absent_for_unlisted_origin(
    isolated_app: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://vkd-risk.example.org")
    with TestClient(create_app()) as client:
        response = client.get("/health", headers={"Origin": "https://attacker.example.org"})
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers
