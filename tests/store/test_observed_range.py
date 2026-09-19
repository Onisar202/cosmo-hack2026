"""``select_observed_range`` — выборка по диапазону ``observed_at``, не
зависящая от ``published_at``/``replay_eligible`` (src/store, FN-38).

Это тест хранилища, не закрытие приёмки FN-38 целиком — здесь проверяется
только выборка записей, без интерпретации космической погоды (см.
tests/domain/spaceweather/test_observed_classifier.py и tests/api для
production-пути).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from src.store import RawOriginalStore, insert_record, select_observed_range
from tests.store.conftest import make_record

UTC = timezone.utc


def test_selects_records_within_half_open_range(
    db_conn: sqlite3.Connection, raw_store: RawOriginalStore
) -> None:
    before = make_record(
        provider_record_id="before",
        source_id="noaa-swpc-proton-flux",
        record_kind="observation",
        observed_at=datetime(2024, 5, 10, 11, 55, tzinfo=UTC),
        published_at=None,
    )
    at_start = make_record(
        provider_record_id="at-start",
        source_id="noaa-swpc-proton-flux",
        record_kind="observation",
        observed_at=datetime(2024, 5, 10, 12, 0, tzinfo=UTC),
        published_at=None,
    )
    inside = make_record(
        provider_record_id="inside",
        source_id="noaa-swpc-proton-flux",
        record_kind="observation",
        observed_at=datetime(2024, 5, 10, 12, 10, tzinfo=UTC),
        published_at=None,
    )
    at_end = make_record(
        provider_record_id="at-end",
        source_id="noaa-swpc-proton-flux",
        record_kind="observation",
        observed_at=datetime(2024, 5, 10, 12, 30, tzinfo=UTC),
        published_at=None,
    )
    for record in (before, at_start, inside, at_end):
        insert_record(db_conn, raw_store, record)

    selected = select_observed_range(
        db_conn,
        source_id="noaa-swpc-proton-flux",
        record_kind="observation",
        start_at=datetime(2024, 5, 10, 12, 0, tzinfo=UTC),
        end_at=datetime(2024, 5, 10, 12, 30, tzinfo=UTC),
    )

    provider_ids = [row["provider_record_id"] for row in selected]
    # [start, end) — начало включено, конец нет.
    assert provider_ids == ["at-start", "inside"]


def test_ignores_published_at_and_replay_eligible(
    db_conn: sqlite3.Connection, raw_store: RawOriginalStore
) -> None:
    """``noaa-swpc-proton-flux`` никогда не несёт ``published_at``
    (sources.yaml) — у каждой такой записи ``replay_eligible=False``, и
    ``select_as_of`` НИКОГДА бы её не вернул. ``select_observed_range``
    обслуживает ровно этот случай."""
    record = make_record(
        provider_record_id="no-published-at",
        source_id="noaa-swpc-proton-flux",
        record_kind="observation",
        observed_at=datetime(2024, 5, 10, 12, 5, tzinfo=UTC),
        published_at=None,
    )
    insert_record(db_conn, raw_store, record)

    selected = select_observed_range(
        db_conn,
        source_id="noaa-swpc-proton-flux",
        record_kind="observation",
        start_at=datetime(2024, 5, 10, 12, 0, tzinfo=UTC),
        end_at=datetime(2024, 5, 10, 12, 30, tzinfo=UTC),
    )

    assert len(selected) == 1
    assert selected[0]["published_at"] is None
    assert selected[0]["replay_eligible"] is False


def test_filters_by_source_id_and_record_kind(
    db_conn: sqlite3.Connection, raw_store: RawOriginalStore
) -> None:
    same_window = datetime(2024, 5, 10, 12, 0, tzinfo=UTC)
    proton = make_record(
        provider_record_id="proton",
        source_id="noaa-swpc-proton-flux",
        record_kind="observation",
        observed_at=same_window,
        published_at=None,
    )
    other_source = make_record(
        provider_record_id="other-source",
        source_id="nasa-ccmc-donki",
        record_kind="observation",
        observed_at=same_window,
        published_at=None,
    )
    other_kind = make_record(
        provider_record_id="other-kind",
        source_id="noaa-swpc-proton-flux",
        record_kind="forecast",
        observed_at=same_window,
        published_at=datetime(2024, 5, 9, tzinfo=UTC),
    )
    for record in (proton, other_source, other_kind):
        insert_record(db_conn, raw_store, record)

    selected = select_observed_range(
        db_conn,
        source_id="noaa-swpc-proton-flux",
        record_kind="observation",
        start_at=datetime(2024, 5, 10, 11, 0, tzinfo=UTC),
        end_at=datetime(2024, 5, 10, 13, 0, tzinfo=UTC),
    )

    assert [row["provider_record_id"] for row in selected] == ["proton"]


def test_orders_results_by_observed_at_ascending(
    db_conn: sqlite3.Connection, raw_store: RawOriginalStore
) -> None:
    later = make_record(
        provider_record_id="later",
        source_id="noaa-swpc-proton-flux",
        record_kind="observation",
        observed_at=datetime(2024, 5, 10, 12, 10, tzinfo=UTC),
        published_at=None,
    )
    earlier = make_record(
        provider_record_id="earlier",
        source_id="noaa-swpc-proton-flux",
        record_kind="observation",
        observed_at=datetime(2024, 5, 10, 12, 0, tzinfo=UTC),
        published_at=None,
    )
    insert_record(db_conn, raw_store, later)
    insert_record(db_conn, raw_store, earlier)

    selected = select_observed_range(
        db_conn,
        source_id="noaa-swpc-proton-flux",
        record_kind="observation",
        start_at=datetime(2024, 5, 10, 11, 0, tzinfo=UTC),
        end_at=datetime(2024, 5, 10, 13, 0, tzinfo=UTC),
    )

    assert [row["provider_record_id"] for row in selected] == ["earlier", "later"]
