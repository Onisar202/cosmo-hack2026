"""Слой хранения: оригиналы, версии записей, выборка по времени и as_of.

Публичный интерфейс собран здесь из ``schema.py`` (SQLite-схема, ``connect``),
``records.py`` (записи источников, ``select_as_of``, оригиналы с checksum) и
``results.py`` (неизменяемые результаты расчёта). Ни один из этих модулей не
предоставляет обновления или удаления сохранённых записей/результатов
(.ai/main-prompt.md §2–3, .ai/backend-prompt.md §1–2).
"""

from src.store.records import (
    ChecksumMismatchError,
    DuplicateKeyConflictError,
    RawOriginalStore,
    RecordInput,
    get_original,
    get_record,
    insert_record,
    select_as_of,
)
from src.store.results import (
    ManifestVerificationError,
    get_result,
    list_results,
    store_result,
)
from src.store.schema import connect

__all__ = [
    "ChecksumMismatchError",
    "DuplicateKeyConflictError",
    "ManifestVerificationError",
    "RawOriginalStore",
    "RecordInput",
    "connect",
    "get_original",
    "get_record",
    "get_result",
    "insert_record",
    "list_results",
    "select_as_of",
    "store_result",
]
