from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from src.api.app import app
from src.config import Settings

client = TestClient(app)


def test_health_returns_200_ok() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"]
    assert body["app_env"] in {"development", "staging", "production"}


def test_health_time_is_utc_aware() -> None:
    response = client.get("/health")

    reported_at = datetime.fromisoformat(response.json()["time"])
    assert reported_at.tzinfo is not None
    assert reported_at.utcoffset().total_seconds() == 0


def test_settings_requires_app_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APP_ENV", raising=False)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_settings_rejects_unknown_app_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "not-a-real-environment")

    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]
