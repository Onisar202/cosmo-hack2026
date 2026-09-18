"""Версионирование, дедупликация и неизменяемость (src/store).

Каждый тест закрывает конкретный способ получить правдоподобный, но неверный
результат хранения (.ai/main-prompt.md §9): перезапись версии, двойной учёт
дубликата, подмену пропуска нулём, обновление или удаление через публичный
интерфейс, потерю данных при перезапуске.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from src.store import (
    ChecksumMismatchError,
    DuplicateKeyConflictError,
    RawOriginalStore,
    connect,
    get_original,
    get_record,
    get_result,
    insert_record,
    select_as_of,
    store_result,
)
from src.store.results import ManifestVerificationError
from tests.store.conftest import make_record

UTC = timezone.utc


def test_later_refinement_does_not_overwrite_old(
    db_conn: sqlite3.Connection, raw_store: RawOriginalStore
) -> None:
    v1 = make_record(source_version="1", raw_bytes=b"v1-body")
    v1_id = insert_record(db_conn, raw_store, v1)

    v2 = make_record(
        source_version="2",
        raw_bytes=b"v2-body",
        published_at=datetime(2024, 5, 9, 20, 0, tzinfo=UTC),
    )
    v2_id = insert_record(db_conn, raw_store, v2)

    assert v1_id != v2_id

    original_v1 = get_record(db_conn, v1_id)
    assert original_v1 is not None
    assert original_v1["source_version"] == "1"
    assert v1.published_at is not None
    stored_published_at = datetime.strptime(
        original_v1["published_at"], "%Y-%m-%dT%H:%M:%S.%fZ"
    ).replace(tzinfo=UTC)
    assert stored_published_at == v1.published_at.astimezone(UTC)

    row_count = db_conn.execute("SELECT COUNT(*) FROM source_records").fetchone()[0]
    assert row_count == 2


def test_duplicate_insert_is_idempotent_and_no_double_impact(
    db_conn: sqlite3.Connection, raw_store: RawOriginalStore
) -> None:
    record = make_record()

    first_id = insert_record(db_conn, raw_store, record)
    second_id = insert_record(db_conn, raw_store, record)

    assert first_id == second_id

    row_count = db_conn.execute("SELECT COUNT(*) FROM source_records").fetchone()[0]
    assert row_count == 1

    selected = select_as_of(db_conn, datetime(2024, 6, 1, tzinfo=UTC))
    assert len(selected) == 1


def test_missing_published_at_is_not_replay_eligible(
    db_conn: sqlite3.Connection, raw_store: RawOriginalStore
) -> None:
    record = make_record(published_at=None)

    record_id = insert_record(db_conn, raw_store, record)

    stored = get_record(db_conn, record_id)
    assert stored is not None
    assert stored["published_at"] is None
    assert stored["replay_eligible"] is False


def test_null_value_is_preserved_not_replaced_with_zero(
    db_conn: sqlite3.Connection, raw_store: RawOriginalStore
) -> None:
    record = make_record(value=None, unit=None)

    record_id = insert_record(db_conn, raw_store, record)

    stored = get_record(db_conn, record_id)
    assert stored is not None
    assert stored["value"] is None
    assert stored["unit"] is None


def test_original_is_recoverable_by_record_id_with_matching_checksum(
    db_conn: sqlite3.Connection, raw_store: RawOriginalStore
) -> None:
    raw_bytes = b"the exact original bytes from the source"
    record = make_record(raw_bytes=raw_bytes)

    record_id = insert_record(db_conn, raw_store, record)

    recovered = get_original(db_conn, raw_store, record_id)
    assert recovered == raw_bytes

    stored = get_record(db_conn, record_id)
    assert stored is not None
    assert stored["checksum"] == hashlib.sha256(raw_bytes).hexdigest()


def test_checksum_mismatch_is_detected(
    db_conn: sqlite3.Connection, raw_store: RawOriginalStore
) -> None:
    record = make_record(raw_bytes=b"original bytes")
    record_id = insert_record(db_conn, raw_store, record)

    stored = get_record(db_conn, record_id)
    assert stored is not None
    corrupted_path = raw_store.root / stored["raw_ref"]
    corrupted_path.write_bytes(b"tampered bytes")

    with pytest.raises(ChecksumMismatchError):
        get_original(db_conn, raw_store, record_id)


def test_store_module_exposes_no_update_or_delete() -> None:
    import src.store as store_module
    import src.store.records as records_module
    import src.store.results as results_module

    forbidden_prefixes = ("update", "delete", "remove", "overwrite")
    for module in (store_module, records_module, results_module):
        public_names = [name for name in dir(module) if not name.startswith("_")]
        for name in public_names:
            lowered = name.lower()
            assert not lowered.startswith(forbidden_prefixes), (
                f"{module.__name__}.{name} looks like an update/delete operation, "
                "the store must be append-only"
            )


def _base_result(record_id: str, *, duration_hours: int = 6) -> dict[str, Any]:
    return {
        "result_id": "res-0001",
        "computed_at": "2024-05-10T00:00:00Z",
        "request": {
            "mode": "historical_analysis",
            "start_at": "2024-05-10T12:00:00Z",
            "duration_hours": duration_hours,
            "search_window_hours": 0,
        },
        "mode": "historical_analysis",
        "as_of": None,
        "algorithm_version": "0.1.0",
        "data_manifest": [
            {
                "record_id": record_id,
                "source_id": "nasa-ccmc-donki",
                "source_version": "1",
                "record_kind": "forecast",
            }
        ],
    }


def test_result_is_immutable_recompute_creates_new_result_id(
    db_conn: sqlite3.Connection, raw_store: RawOriginalStore
) -> None:
    record_id = insert_record(db_conn, raw_store, make_record())

    result = _base_result(record_id)
    store_result(db_conn, result)

    with pytest.raises(ValueError):
        store_result(db_conn, result)

    recomputed = dict(result)
    recomputed["result_id"] = "res-0002"
    stored_id = store_result(db_conn, recomputed)
    assert stored_id == "res-0002"

    row_count = db_conn.execute("SELECT COUNT(*) FROM calculation_results").fetchone()[0]
    assert row_count == 2


def test_store_result_rejects_manifest_referencing_unknown_record(
    db_conn: sqlite3.Connection,
) -> None:
    result = _base_result("does-not-exist")

    with pytest.raises(ManifestVerificationError):
        store_result(db_conn, result)


def test_different_results_parameters_are_isolated(
    db_conn: sqlite3.Connection, raw_store: RawOriginalStore
) -> None:
    record_id = insert_record(db_conn, raw_store, make_record())

    result_a = _base_result(record_id, duration_hours=4)
    result_a["result_id"] = "res-a"

    result_b = _base_result(record_id, duration_hours=8)
    result_b["result_id"] = "res-b"

    store_result(db_conn, result_a)
    store_result(db_conn, result_b)

    stored_a = get_result(db_conn, "res-a")
    stored_b = get_result(db_conn, "res-b")
    assert stored_a is not None and stored_b is not None
    assert stored_a["request"]["duration_hours"] == 4
    assert stored_b["request"]["duration_hours"] == 8


def test_records_and_results_survive_restart(tmp_path: Path, raw_store: RawOriginalStore) -> None:
    db_path = tmp_path / "restart.sqlite3"

    conn = connect(db_path)
    record_id = insert_record(conn, raw_store, make_record())
    result = _base_result(record_id)
    store_result(conn, result)
    conn.close()

    reopened = connect(db_path)
    try:
        stored_record = get_record(reopened, record_id)
        stored_result = get_result(reopened, "res-0001")

        assert stored_record is not None
        assert stored_record["record_id"] == record_id
        assert stored_result is not None
        assert stored_result["result_id"] == "res-0001"
    finally:
        reopened.close()


def test_record_input_rejects_naive_datetime(
    raw_store: RawOriginalStore, db_conn: sqlite3.Connection
) -> None:
    naive_record = replace(make_record(), observed_at=datetime(2024, 5, 10, 12, 0))

    with pytest.raises(ValueError):
        insert_record(db_conn, raw_store, naive_record)


def test_record_input_rejects_empty_source_version(
    raw_store: RawOriginalStore, db_conn: sqlite3.Connection
) -> None:
    """Пустая/неизвестная версия непригодна для replay так же, как
    неизвестная публикация (.ai/main-prompt.md §1) — запись отклоняется
    целиком, а не тихо сохраняется с replay_eligible = false (round 1 ревью)."""
    empty_version = replace(make_record(), source_version="   ")

    with pytest.raises(ValueError):
        insert_record(db_conn, raw_store, empty_version)

    assert db_conn.execute("SELECT COUNT(*) FROM source_records").fetchone()[0] == 0


def test_duplicate_key_with_different_content_is_a_conflict_not_a_duplicate(
    db_conn: sqlite3.Connection, raw_store: RawOriginalStore
) -> None:
    """Совпадение (source_id, provider_record_id, source_version) само по
    себе не значит «тот же дубль»: если оригинал отличается, это конфликт
    версии у поставщика, а не сообщение с тем же содержимым, и тихо
    подтверждать первую попавшуюся запись нельзя (round 1 ревью)."""
    first = make_record(source_version="1", raw_bytes=b"first submission")
    insert_record(db_conn, raw_store, first)

    conflicting = make_record(source_version="1", raw_bytes=b"different submission")

    with pytest.raises(DuplicateKeyConflictError):
        insert_record(db_conn, raw_store, conflicting)

    assert db_conn.execute("SELECT COUNT(*) FROM source_records").fetchone()[0] == 1


def test_duplicate_key_with_identical_content_is_idempotent(
    db_conn: sqlite3.Connection, raw_store: RawOriginalStore
) -> None:
    record = make_record(source_version="1", raw_bytes=b"same bytes twice")

    first_id = insert_record(db_conn, raw_store, record)
    second_id = insert_record(db_conn, raw_store, replace(record))

    assert first_id == second_id
    assert db_conn.execute("SELECT COUNT(*) FROM source_records").fetchone()[0] == 1


def test_published_at_offset_is_normalized_to_utc_before_comparison(
    db_conn: sqlite3.Connection, raw_store: RawOriginalStore
) -> None:
    """``00:30-05:00`` — это ``05:30Z``: если хранилище сравнивало бы ISO-строки
    без приведения к UTC, эта запись прошла бы отсечение ``as_of = 03:00Z``
    лексикографически, хотя фактически опубликована позже (round 1 ревью,
    утечка будущих данных)."""
    published_at_with_offset = datetime(
        2024, 5, 10, 0, 30, tzinfo=timezone(timedelta(hours=-5))
    )
    assert published_at_with_offset.astimezone(UTC) == datetime(2024, 5, 10, 5, 30, tzinfo=UTC)

    record = make_record(published_at=published_at_with_offset)
    insert_record(db_conn, raw_store, record)

    as_of_before_actual_utc_instant = datetime(2024, 5, 10, 3, 0, tzinfo=UTC)
    selected = select_as_of(db_conn, as_of_before_actual_utc_instant)
    assert selected == []

    as_of_after_actual_utc_instant = datetime(2024, 5, 10, 6, 0, tzinfo=UTC)
    selected_after = select_as_of(db_conn, as_of_after_actual_utc_instant)
    assert len(selected_after) == 1
    assert selected_after[0]["published_at"] == "2024-05-10T05:30:00.000000Z"
