"""Общие фикстуры для тестов хранилища (src/store).

Каждый тест получает своё SQLite-соединение и свой каталог оригиналов —
на отдельном временном пути, поэтому тесты не делят состояние друг с другом
и не зависят от порядка запуска.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from src.store import RawOriginalStore, RecordInput, connect

UTC = timezone.utc


@pytest.fixture
def db_conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = connect(tmp_path / "store.sqlite3")
    yield conn
    conn.close()


@pytest.fixture
def raw_store(tmp_path: Path) -> RawOriginalStore:
    return RawOriginalStore(tmp_path / "raw")


def make_record(
    *,
    provider_record_id: str = "donki-fx-0001",
    source_id: str = "nasa-ccmc-donki",
    source_version: str = "1",
    record_kind: str = "forecast",
    observed_at: datetime = datetime(2024, 5, 10, 12, 0, tzinfo=UTC),
    valid_from: datetime = datetime(2024, 5, 10, 12, 0, tzinfo=UTC),
    valid_to: datetime = datetime(2024, 5, 10, 18, 0, tzinfo=UTC),
    published_at: datetime | None = datetime(2024, 5, 9, 18, 0, tzinfo=UTC),
    fetched_at: datetime = datetime(2024, 5, 9, 18, 5, tzinfo=UTC),
    value: Any = 42.0,
    unit: str | None = "pfu",
    spatial_context: dict[str, Any] | None = None,
    quality: str = "nominal",
    raw_bytes: bytes = b'{"raw": "example"}',
) -> RecordInput:
    return RecordInput(
        provider_record_id=provider_record_id,
        source_id=source_id,
        source_url="https://example.invalid/donki/fx-0001",
        record_kind=record_kind,  # type: ignore[arg-type]
        observed_at=observed_at,
        valid_from=valid_from,
        valid_to=valid_to,
        published_at=published_at,
        fetched_at=fetched_at,
        value=value,
        unit=unit,
        spatial_context=spatial_context if spatial_context is not None else {},
        source_version=source_version,
        quality=quality,  # type: ignore[arg-type]
        raw_bytes=raw_bytes,
    )


def hours(n: float) -> timedelta:
    return timedelta(hours=n)
