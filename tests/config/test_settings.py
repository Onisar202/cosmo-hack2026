"""FN-45: `Settings.cors_origins()` — CORS_ALLOWED_ORIGINS из env parse в список.

Пустая настройка по умолчанию не должна превращаться в "разрешить всё"
(main-prompt.md §7 «пороги, адреса — в конфиге»): src/api/app.py читает
именно этот список, чтобы решить, подключать ли CORSMiddleware вообще.
"""

from __future__ import annotations

import pytest

from src.config import Settings


def _settings(cors_allowed_origins: str = "", **overrides: object) -> Settings:
    return Settings(app_env="development", cors_allowed_origins=cors_allowed_origins, **overrides)  # type: ignore[arg-type]


def test_cors_origins_default_is_empty() -> None:
    assert _settings().cors_origins() == []


def test_cors_origins_parses_comma_separated_list() -> None:
    settings = _settings("https://a.example.org,https://b.example.org")
    assert settings.cors_origins() == ["https://a.example.org", "https://b.example.org"]


@pytest.mark.parametrize(
    "raw",
    [
        " https://a.example.org , https://b.example.org ",
        "https://a.example.org,,https://b.example.org,",
    ],
)
def test_cors_origins_strips_whitespace_and_drops_empty_entries(raw: str) -> None:
    assert _settings(raw).cors_origins() == ["https://a.example.org", "https://b.example.org"]
