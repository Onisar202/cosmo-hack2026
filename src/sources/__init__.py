"""Слой получения: коннекторы источников, парсеры, нормализация в запись.

Не интерпретирует данные и не считает физику — только получает и
нормализует (см. .ai/main-prompt.md, §8). Коннекторы добавляются
последующими задачами по зоне 1.
"""

from src.sources.status import (
    SourceStatus,
    SourceStatusRegistry,
    effective_status,
    is_critically_stale,
    staleness_seconds,
)
from src.sources.swpc import (
    SOURCE_ID as SWPC_SOURCE_ID,
)
from src.sources.swpc import (
    FetchOutcome,
    SwpcFormatError,
    SwpcSourceConfig,
    fetch_and_store,
    load_source_config,
    parse_response,
)

__all__ = [
    "SWPC_SOURCE_ID",
    "FetchOutcome",
    "SourceStatus",
    "SourceStatusRegistry",
    "SwpcFormatError",
    "SwpcSourceConfig",
    "effective_status",
    "fetch_and_store",
    "is_critically_stale",
    "load_source_config",
    "parse_response",
    "staleness_seconds",
]
