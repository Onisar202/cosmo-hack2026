"""Слой хранения: оригиналы, версии записей, выборка по времени и as_of.

Публичный интерфейс собран здесь из ``schema.py`` (SQLite-схема, ``connect``),
``records.py`` (записи источников, ``select_as_of``, оригиналы с checksum),
``forecast_snapshot.py`` (неизменяемый снимок входа ``historical_forecast``,
записи и карта покрытия вместе — FN-42) и ``results.py`` (неизменяемые
результаты расчёта). Ни один из этих модулей не предоставляет обновления или
удаления сохранённых записей/результатов
(.ai/main-prompt.md §2–3, .ai/backend-prompt.md §1–2).
"""

from src.store.forecast_snapshot import (
    SealedForecastSnapshot,
    read_sealed_snapshot,
    seal_forecast_input_snapshot,
)
from src.store.records import (
    ChecksumMismatchError,
    DuplicateKeyConflictError,
    RawOriginalStore,
    RecordInput,
    get_latest_record,
    get_original,
    get_record,
    insert_record,
    select_as_of,
    select_observed_range,
)
from src.store.results import (
    ManifestVerificationError,
    get_result,
    list_results,
    store_result,
)
from src.store.schema import connect
from src.store.verification import select_verification_records

__all__ = [
    "ChecksumMismatchError",
    "DuplicateKeyConflictError",
    "ManifestVerificationError",
    "RawOriginalStore",
    "RecordInput",
    "SealedForecastSnapshot",
    "connect",
    "get_latest_record",
    "get_original",
    "get_record",
    "get_result",
    "insert_record",
    "list_results",
    "read_sealed_snapshot",
    "seal_forecast_input_snapshot",
    "select_as_of",
    "select_observed_range",
    "select_verification_records",
    "store_result",
]
