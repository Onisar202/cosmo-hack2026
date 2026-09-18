"""``select_as_of`` — строгое отсечение по времени публикации (src/store).

Это тест хранилища, не полное закрытие критерия Т4: здесь проверяется только
выборка записей, без интерпретации космической погоды или MMOD.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from src.store import RawOriginalStore, insert_record, select_as_of
from tests.store.conftest import make_record

UTC = timezone.utc


def test_publication_after_cutoff_is_excluded(
    db_conn: sqlite3.Connection, raw_store: RawOriginalStore
) -> None:
    early = make_record(
        provider_record_id="early",
        source_version="1",
        published_at=datetime(2024, 5, 9, 12, 0, tzinfo=UTC),
    )
    late = make_record(
        provider_record_id="late",
        source_version="1",
        published_at=datetime(2024, 5, 10, 15, 0, tzinfo=UTC),
    )
    insert_record(db_conn, raw_store, early)
    insert_record(db_conn, raw_store, late)

    as_of = datetime(2024, 5, 10, 0, 0, tzinfo=UTC)
    selected = select_as_of(db_conn, as_of)

    provider_ids = {row["provider_record_id"] for row in selected}
    assert provider_ids == {"early"}


def test_records_without_published_at_never_selected(
    db_conn: sqlite3.Connection, raw_store: RawOriginalStore
) -> None:
    unpublished = make_record(provider_record_id="unpublished", published_at=None)
    insert_record(db_conn, raw_store, unpublished)

    far_future = datetime(2030, 1, 1, tzinfo=UTC)
    selected = select_as_of(db_conn, far_future)

    assert selected == []


def test_newer_publication_after_cutoff_does_not_leak_even_as_a_refinement(
    db_conn: sqlite3.Connection, raw_store: RawOriginalStore
) -> None:
    """Позднее уточнение того же продукта не подменяет выбор, если оно
    опубликовано после ``as_of`` — иначе строгий прогноз из прошлого
    незаметно превращается в ретроспективный разбор по факту."""
    v1 = make_record(
        provider_record_id="p-1",
        source_version="1",
        published_at=datetime(2024, 5, 9, 12, 0, tzinfo=UTC),
        value=10.0,
    )
    v2_after_cutoff = make_record(
        provider_record_id="p-1",
        source_version="2",
        published_at=datetime(2024, 5, 11, 0, 0, tzinfo=UTC),
        value=999.0,
    )
    insert_record(db_conn, raw_store, v1)
    insert_record(db_conn, raw_store, v2_after_cutoff)

    as_of = datetime(2024, 5, 10, 0, 0, tzinfo=UTC)
    selected = select_as_of(db_conn, as_of)

    assert len(selected) == 1
    assert selected[0]["source_version"] == "1"
    assert selected[0]["value"] == 10.0


def test_select_as_of_returns_latest_eligible_version_of_the_same_product(
    db_conn: sqlite3.Connection, raw_store: RawOriginalStore
) -> None:
    v1 = make_record(
        provider_record_id="p-2",
        source_version="1",
        published_at=datetime(2024, 5, 9, 12, 0, tzinfo=UTC),
        value=10.0,
    )
    v2 = make_record(
        provider_record_id="p-2",
        source_version="2",
        published_at=datetime(2024, 5, 9, 20, 0, tzinfo=UTC),
        value=15.0,
    )
    insert_record(db_conn, raw_store, v1)
    insert_record(db_conn, raw_store, v2)

    as_of = datetime(2024, 5, 10, 0, 0, tzinfo=UTC)
    selected = select_as_of(db_conn, as_of)

    assert len(selected) == 1
    assert selected[0]["source_version"] == "2"
    assert selected[0]["value"] == 15.0


def test_select_as_of_filters_by_source_id_and_record_kind(
    db_conn: sqlite3.Connection, raw_store: RawOriginalStore
) -> None:
    donki = make_record(
        provider_record_id="donki-1",
        source_id="nasa-ccmc-donki",
        record_kind="forecast",
        published_at=datetime(2024, 5, 9, 12, 0, tzinfo=UTC),
    )
    noaa = make_record(
        provider_record_id="noaa-1",
        source_id="noaa-swpc-geomag-watch",
        record_kind="warning",
        published_at=datetime(2024, 5, 9, 12, 0, tzinfo=UTC),
    )
    insert_record(db_conn, raw_store, donki)
    insert_record(db_conn, raw_store, noaa)

    as_of = datetime(2024, 5, 10, 0, 0, tzinfo=UTC)

    only_donki = select_as_of(db_conn, as_of, source_id="nasa-ccmc-donki")
    assert {row["source_id"] for row in only_donki} == {"nasa-ccmc-donki"}

    only_warnings = select_as_of(db_conn, as_of, record_kind="warning")
    assert {row["record_kind"] for row in only_warnings} == {"warning"}


def test_select_as_of_at_exact_cutoff_is_inclusive(
    db_conn: sqlite3.Connection, raw_store: RawOriginalStore
) -> None:
    published_at = datetime(2024, 5, 10, 9, 0, tzinfo=UTC)
    record = make_record(published_at=published_at)
    insert_record(db_conn, raw_store, record)

    selected = select_as_of(db_conn, published_at)

    assert len(selected) == 1
