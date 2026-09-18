"""Общие фикстуры для тестов коннекторов источников (src/sources).

Своё SQLite-соединение и свой каталог оригиналов на тест, как в
``tests/store/conftest.py`` — тесты источников тоже не должны делить
состояние хранилища друг с другом.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from src.sources.status import SourceStatusRegistry
from src.sources.swpc import SwpcSourceConfig
from src.store import RawOriginalStore, connect

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "sources" / "swpc"


@pytest.fixture
def db_conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = connect(tmp_path / "store.sqlite3")
    yield conn
    conn.close()


@pytest.fixture
def raw_store(tmp_path: Path) -> RawOriginalStore:
    return RawOriginalStore(tmp_path / "raw")


@pytest.fixture
def registry() -> SourceStatusRegistry:
    return SourceStatusRegistry()


@pytest.fixture
def swpc_config() -> SwpcSourceConfig:
    """Конфигурация коннектора без чтения ``sources.yaml`` — тесты фиксируют
    свои значения таймаутов/TTL напрямую, чтобы не зависеть от содержимого
    файла в репозитории и не делать тест недетерминированным при его правке.
    """
    return SwpcSourceConfig(
        source_id="noaa-swpc-proton-flux",
        url="https://services.swpc.noaa.gov/json/goes/primary/integral-protons-1-day.json",
        connect_timeout_seconds=5.0,
        read_timeout_seconds=10.0,
        max_retries=2,
        backoff_base_seconds=0.0,  # без реального ожидания в тестах
        ttl_seconds=300.0,
        critical_staleness_seconds=3600.0,
        enabled=True,
    )
