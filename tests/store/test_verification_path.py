"""Последующие наблюдения — отдельная ветка кода, не вход прогноза (FN-42).

.ai/main-prompt.md §1: «Проверка качества прогноза и вход прогноза не должны
быть одной функцией с булевым флагом». Эти тесты закрепляют именно
структурное разделение, а не только поведение одной выборки: две функции
живут в разных модулях, и ни у одной нет параметра, переключающего её в
режим другой.

Данные — реальные сохранённые уведомления DONKI
(``tests/fixtures/sources/archive/``), та же пара «первичное уведомление /
позднее уточнение», что и в остальных тестах FN-42.
"""

from __future__ import annotations

import inspect
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from src.sources.archive_ingest import ingest_donki_notifications, select_forecast_inputs
from src.store import RawOriginalStore, select_as_of, select_verification_records
from src.store.records import select_as_of as select_as_of_direct

UTC = timezone.utc
ARCHIVE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "sources" / "archive"
DONKI_SOURCE_ID = "nasa-donki-notifications"
MAY_FILE = "donki_2024-05-01_2024-05-15.json"


def _ingest_may(conn: sqlite3.Connection, raw_store: RawOriginalStore) -> None:
    ingest_donki_notifications(
        conn,
        raw_store,
        (ARCHIVE_DIR / MAY_FILE).read_bytes(),
        source_url="https://api.nasa.gov/DONKI/notifications",
        fetched_at=datetime(2026, 9, 18, 23, 3, 31, tzinfo=UTC),
        interval_start=datetime(2024, 5, 1, tzinfo=UTC),
        interval_end=datetime(2024, 5, 16, tzinfo=UTC),
    )


def test_forecast_input_and_verification_sets_are_disjoint(
    db_conn: sqlite3.Connection, raw_store: RawOriginalStore
) -> None:
    """Ни одна запись не может оказаться одновременно входом прогноза и
    последующим наблюдением: условия двух функций взаимно исключающи
    (``published_at <= as_of`` против ``published_at > as_of``)."""
    _ingest_may(db_conn, raw_store)
    as_of = datetime(2024, 5, 11, 12, 0, tzinfo=UTC)

    forecast_input = select_as_of(db_conn, as_of, source_id=DONKI_SOURCE_ID)
    verification = select_verification_records(db_conn, as_of, source_id=DONKI_SOURCE_ID)

    assert forecast_input, "the real archive must yield forecast inputs before this cutoff"
    assert verification, "the real archive must yield later-arriving records after it"
    assert not (
        {r["record_id"] for r in forecast_input} & {r["record_id"] for r in verification}
    )


def test_late_update_is_available_only_to_the_verification_path(
    db_conn: sqlite3.Connection, raw_store: RawOriginalStore
) -> None:
    """Реальная пара об одной активности ``2024-05-11T04:07:00-SEP-001``:
    ``20240511-AL-008`` (04:13Z) — вход прогноза; ``20240511-AL-013``
    (15:03Z) — только материал проверки (Т4: «последующая информация —
    только для проверки»)."""
    _ingest_may(db_conn, raw_store)
    as_of = datetime(2024, 5, 11, 12, 0, tzinfo=UTC)

    forecast_ids = {
        r["provider_record_id"] for r in select_as_of(db_conn, as_of, source_id=DONKI_SOURCE_ID)
    }
    verification_ids = {
        r["provider_record_id"]
        for r in select_verification_records(db_conn, as_of, source_id=DONKI_SOURCE_ID)
    }

    assert "20240511-AL-008" in forecast_ids
    assert "20240511-AL-008" not in verification_ids
    assert "20240511-AL-013" in verification_ids
    assert "20240511-AL-013" not in forecast_ids


def test_verification_keeps_every_late_version_not_just_the_latest(
    db_conn: sqlite3.Connection, raw_store: RawOriginalStore
) -> None:
    """В отличие от ``select_as_of``, разбор качества не схлопывает поздние
    уточнения до одного сводного состояния — иначе было бы не видно, что
    уточнение приходило несколько раз."""
    _ingest_may(db_conn, raw_store)
    as_of = datetime(2024, 5, 10, 12, 0, tzinfo=UTC)
    verification = select_verification_records(db_conn, as_of, source_id=DONKI_SOURCE_ID)

    published = [r["published_at"] for r in verification]
    assert published == sorted(published), "verification records must arrive in publication order"
    assert len(verification) > 1


def test_verification_window_limits_the_observed_period(
    db_conn: sqlite3.Connection, raw_store: RawOriginalStore
) -> None:
    """Проверка ограничивается тем же периодом, на который делался прогноз."""
    _ingest_may(db_conn, raw_store)
    as_of = datetime(2024, 5, 10, 12, 0, tzinfo=UTC)
    narrow = select_verification_records(
        db_conn,
        as_of,
        source_id=DONKI_SOURCE_ID,
        observed_from=datetime(2024, 5, 10, tzinfo=UTC),
        observed_to=datetime(2024, 5, 11, tzinfo=UTC),
    )
    assert narrow
    for record in narrow:
        observed = datetime.fromisoformat(record["observed_at"].replace("Z", "+00:00"))
        assert datetime(2024, 5, 10, tzinfo=UTC) <= observed < datetime(2024, 5, 11, tzinfo=UTC)


def test_records_with_unknown_publication_time_appear_in_neither_path(
    db_conn: sqlite3.Connection, raw_store: RawOriginalStore
) -> None:
    """``published_at = None`` не даёт утверждать ни «было доступно до
    отсечения», ни «пришло после» — запись не участвует ни в одной из двух
    веток (main-prompt.md §1)."""
    ingest_donki_notifications(
        db_conn,
        raw_store,
        (ARCHIVE_DIR / "donki_2024-05-16_2024-05-31.json").read_bytes(),
        source_url="https://api.nasa.gov/DONKI/notifications",
        fetched_at=datetime(2026, 9, 18, 23, 3, 31, tzinfo=UTC),
        interval_start=datetime(2024, 5, 16, tzinfo=UTC),
        interval_end=datetime(2024, 6, 1, tzinfo=UTC),
    )
    as_of = datetime(2024, 5, 16, 12, 0, tzinfo=UTC)

    in_forecast = {r["provider_record_id"] for r in select_as_of(db_conn, as_of)}
    in_verification = {r["provider_record_id"] for r in select_verification_records(db_conn, as_of)}
    assert "20240516-7D-001" not in in_forecast
    assert "20240516-7D-001" not in in_verification


def test_neither_selection_function_has_a_mode_flag() -> None:
    """Структурная проверка §1: ни у входа прогноза, ни у пути проверки нет
    булева параметра, переключающего одну ветку в другую — они разведены
    разными функциями в разных модулях, а не режимом одного вызова.

    Тест смотрит на сигнатуры, потому что именно подмена «двух функций» на
    «одну функцию с флагом» является тем регрессом, который §1 запрещает.
    """
    for function in (select_as_of_direct, select_verification_records, select_forecast_inputs):
        for name, parameter in inspect.signature(function).parameters.items():
            assert not isinstance(parameter.default, bool), (
                f"{function.__module__}.{function.__name__} has boolean parameter {name!r} — "
                "forecast input and verification must stay separate functions (main-prompt.md §1)"
            )

    assert select_verification_records.__module__ == "src.store.verification"
    assert select_as_of_direct.__module__ == "src.store.records"
