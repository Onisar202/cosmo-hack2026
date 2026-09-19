"""Тесты коннектора NOAA SWPC «3-Day Forecast» (FN-31, S2-01).

Полностью детерминированы и без сети (.ai/main-prompt.md §9): HTTP заменяется
``httpx.MockTransport``. Фикстуры — ``tests/fixtures/sources/noaa_3day_forecast/synthetic/``,
явно синтетические (см. её README про то, почему в этой сессии нет реальных
архивных фикстур этого продукта — заблокированный сетевой доступ).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from src.sources.noaa_3day_forecast import (
    ARCHIVE_SOURCE_ID,
    SOURCE_ID,
    FetchOutcome,
    Noaa3DayForecastFormatError,
    Noaa3DayForecastSourceConfig,
    fetch_and_store,
    fetch_and_store_archived_bulletin,
    noaa_3day_forecast_to_record_inputs,
    parse_noaa_3day_forecast,
)
from src.sources.status import SourceStatusRegistry
from src.store import RawOriginalStore, get_record, select_as_of

UTC = timezone.utc
FIXTURES_DIR = (
    Path(__file__).resolve().parent.parent
    / "fixtures" / "sources" / "noaa_3day_forecast" / "synthetic"
)

BULLETIN_JAN05 = (FIXTURES_DIR / "bulletin_2025-01-05_2200.txt").read_bytes()
BULLETIN_JAN06 = (FIXTURES_DIR / "bulletin_2025-01-06_2200.txt").read_bytes()
BULLETIN_YEAR_ROLLOVER = (FIXTURES_DIR / "bulletin_2024-12-30_2200.txt").read_bytes()
MISSING_ISSUED = (FIXTURES_DIR / "missing_issued_line.txt").read_bytes()
MISSING_S1_TABLE = (FIXTURES_DIR / "missing_s1_table.txt").read_bytes()
MALFORMED_PERCENT = (FIXTURES_DIR / "malformed_percent.txt").read_bytes()
EMPTY = (FIXTURES_DIR / "empty.txt").read_bytes()


def _mock_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture
def config() -> Noaa3DayForecastSourceConfig:
    return Noaa3DayForecastSourceConfig(
        source_id=SOURCE_ID,
        url="https://services.swpc.noaa.gov/text/3-day-forecast.txt",
        connect_timeout_seconds=5.0,
        read_timeout_seconds=10.0,
        max_retries=2,
        backoff_base_seconds=0.0,
        ttl_seconds=3600.0,
        critical_staleness_seconds=7200.0,
        enabled=True,
    )


@pytest.fixture
def registry() -> SourceStatusRegistry:
    return SourceStatusRegistry()


@pytest.fixture
def db_conn(tmp_path: Path) -> sqlite3.Connection:
    from src.store import connect

    conn = connect(tmp_path / "store.sqlite3")
    yield conn
    conn.close()


@pytest.fixture
def raw_store(tmp_path: Path) -> RawOriginalStore:
    return RawOriginalStore(tmp_path / "raw")


# --------------------------------------------------------------------------
# parse_noaa_3day_forecast — на синтетических, но формат-точных фикстурах
# --------------------------------------------------------------------------


def test_parse_extracts_issued_at_and_three_days() -> None:
    forecast = parse_noaa_3day_forecast(BULLETIN_JAN05)
    assert forecast.issued_at == datetime(2025, 1, 5, 22, 0, tzinfo=UTC)
    assert [p.forecast_day.isoformat() for p in forecast.probabilities] == [
        "2025-01-05", "2025-01-06", "2025-01-07",
    ]
    assert [p.probability_percent for p in forecast.probabilities] == [20.0, 10.0, 5.0]
    assert [p.day_index for p in forecast.probabilities] == [1, 2, 3]


def test_parse_ignores_kp_and_radio_blackout_tables() -> None:
    """Раздел A (Kp) и C (радиошумовые блэкауты) не должны попадать в
    результат этого коннектора — main-prompt.md §11 Kp/шкала R — отдельный
    контекст, не эта задача."""
    forecast = parse_noaa_3day_forecast(BULLETIN_JAN05)
    assert len(forecast.probabilities) == 3  # не 6 (S1 + R1-R2 + R3) и не 9


def test_parse_resolves_year_rollover_across_new_year() -> None:
    """Бюллетень выпущен 30 декабря 2024, третья колонка таблицы —
    '01 января' без года: обязана быть распознана как 2025-01-01, не
    2024-01-01 (главная ловушка отсутствия года в заголовке таблицы)."""
    forecast = parse_noaa_3day_forecast(BULLETIN_YEAR_ROLLOVER)
    days = [p.forecast_day.isoformat() for p in forecast.probabilities]
    assert days == ["2024-12-30", "2024-12-31", "2025-01-01"]


@pytest.mark.parametrize(
    "payload",
    [MISSING_ISSUED, MISSING_S1_TABLE, MALFORMED_PERCENT, EMPTY, b"not a bulletin at all"],
)
def test_parse_rejects_malformed_input_explicitly(payload: bytes) -> None:
    """main-prompt.md §2: смена формата / неразбираемое значение — явная
    ошибка, не пустой результат и не придуманное число."""
    with pytest.raises(Noaa3DayForecastFormatError):
        parse_noaa_3day_forecast(payload)


# --------------------------------------------------------------------------
# noaa_3day_forecast_to_record_inputs — нормализация в RecordInput
# --------------------------------------------------------------------------


def test_to_record_inputs_produce_one_record_per_day_with_full_day_interval() -> None:
    forecast = parse_noaa_3day_forecast(BULLETIN_JAN05)
    fetched_at = datetime(2025, 1, 5, 22, 5, tzinfo=UTC)
    records = noaa_3day_forecast_to_record_inputs(
        forecast, source_id=SOURCE_ID, source_url="https://x.invalid", fetched_at=fetched_at
    )
    assert len(records) == 3
    first = records[0]
    assert first.record_kind == "forecast"
    assert first.unit == "percent"
    assert first.value == 20.0
    assert first.quality == "nominal"
    assert first.published_at == datetime(2025, 1, 5, 22, 0, tzinfo=UTC)
    assert first.fetched_at == fetched_at
    assert first.valid_from == datetime(2025, 1, 5, tzinfo=UTC)
    assert first.valid_to == datetime(2025, 1, 6, tzinfo=UTC)  # полные сутки, не точка
    assert first.observed_at == first.valid_from
    assert first.provider_record_id == "noaa-3day-s1-plus:2025-01-05"
    # fetched_at не подменяет published_at, published_at не подменяет valid_from
    assert first.fetched_at != first.published_at
    assert first.published_at != first.valid_from


def test_to_record_inputs_never_mixes_units_with_goes_observation() -> None:
    """Наблюдение GOES несёт unit='pfu' (src/sources/swpc.py); эта линия —
    unit='percent'. Разные единицы структурно исключают смешение
    (main-prompt.md §4)."""
    forecast = parse_noaa_3day_forecast(BULLETIN_JAN05)
    records = noaa_3day_forecast_to_record_inputs(
        forecast, source_id=SOURCE_ID, source_url="https://x.invalid",
        fetched_at=datetime(2025, 1, 5, 22, 5, tzinfo=UTC),
    )
    assert all(r.unit == "percent" for r in records)
    assert all(r.unit != "pfu" for r in records)


# --------------------------------------------------------------------------
# Обязательный тест на утечку времени (main-prompt.md §9.1, приёмка FN-31 п.3):
# позднее уточнение того же дня не должно быть видно до его собственного as_of.
# --------------------------------------------------------------------------


def test_later_bulletin_revision_of_the_same_day_is_excluded_by_as_of(
    db_conn: sqlite3.Connection, raw_store: RawOriginalStore
) -> None:
    from src.store import insert_record

    first_bulletin = parse_noaa_3day_forecast(BULLETIN_JAN05)  # 06 Jan -> 10%
    second_bulletin = parse_noaa_3day_forecast(BULLETIN_JAN06)  # 06 Jan revised -> 45%

    for record in noaa_3day_forecast_to_record_inputs(
        first_bulletin, source_id=SOURCE_ID, source_url="https://x.invalid",
        fetched_at=first_bulletin.issued_at,
    ):
        insert_record(db_conn, raw_store, record)
    for record in noaa_3day_forecast_to_record_inputs(
        second_bulletin, source_id=SOURCE_ID, source_url="https://x.invalid",
        fetched_at=second_bulletin.issued_at,
    ):
        insert_record(db_conn, raw_store, record)

    # До выпуска второго бюллетеня (06 Jan 22:00) — только первая версия (10%).
    as_of_before = datetime(2025, 1, 6, 12, 0, tzinfo=UTC)
    before = select_as_of(db_conn, as_of_before, source_id=SOURCE_ID, record_kind="forecast")
    jan06_before = next(r for r in before if r["provider_record_id"].endswith("2025-01-06"))
    assert jan06_before["value"] == 10.0
    assert not any(r["value"] == 45.0 for r in before)

    # После выпуска второго бюллетеня — видна пересмотренная версия (45%),
    # прежняя (10%) остаётся в хранилище рядом, не перезаписана.
    as_of_after = datetime(2025, 1, 6, 23, 0, tzinfo=UTC)
    after = select_as_of(db_conn, as_of_after, source_id=SOURCE_ID, record_kind="forecast")
    jan06_after = next(r for r in after if r["provider_record_id"].endswith("2025-01-06"))
    assert jan06_after["value"] == 45.0


# --------------------------------------------------------------------------
# fetch_and_store — шлюз: TTL, force, freeze, disable, квота, таймаут, формат
# --------------------------------------------------------------------------


def test_fetch_and_store_success_stores_three_records_and_updates_status(
    db_conn: sqlite3.Connection,
    raw_store: RawOriginalStore,
    registry: SourceStatusRegistry,
    config: Noaa3DayForecastSourceConfig,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=BULLETIN_JAN05)

    now = datetime(2025, 1, 5, 22, 5, tzinfo=UTC)
    outcome = fetch_and_store(
        db_conn, raw_store, config=config, registry=registry, now=now,
        http_client=_mock_client(handler),
    )
    assert outcome.outcome == "stored"
    assert len(outcome.stored_record_ids) == 3
    assert outcome.status.last_success_at == now
    for record_id in outcome.stored_record_ids:
        assert get_record(db_conn, record_id) is not None


def test_fetch_and_store_skips_network_within_ttl_then_refreshes_after(
    db_conn: sqlite3.Connection,
    raw_store: RawOriginalStore,
    registry: SourceStatusRegistry,
    config: Noaa3DayForecastSourceConfig,
) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, content=BULLETIN_JAN05)

    t0 = datetime(2025, 1, 5, 22, 5, tzinfo=UTC)
    fetch_and_store(
        db_conn, raw_store, config=config, registry=registry, now=t0,
        http_client=_mock_client(handler),
    )
    assert calls["n"] == 1

    t1 = t0 + timedelta(seconds=60)
    skipped = fetch_and_store(
        db_conn, raw_store, config=config, registry=registry, now=t1,
        http_client=_mock_client(handler),
    )
    assert skipped.outcome == "skipped_fresh"
    assert calls["n"] == 1

    t2 = t0 + timedelta(seconds=4000)
    refreshed = fetch_and_store(
        db_conn, raw_store, config=config, registry=registry, now=t2,
        http_client=_mock_client(handler),
    )
    assert refreshed.outcome == "stored"
    assert calls["n"] == 2


def test_fetch_and_store_disabled_source_never_touches_network(
    db_conn: sqlite3.Connection,
    raw_store: RawOriginalStore,
    registry: SourceStatusRegistry,
    config: Noaa3DayForecastSourceConfig,
) -> None:
    from dataclasses import replace

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("disabled source must not be fetched")

    disabled = replace(config, enabled=False)
    outcome = fetch_and_store(
        db_conn, raw_store, config=disabled, registry=registry,
        now=datetime(2025, 1, 5, 22, 5, tzinfo=UTC), http_client=_mock_client(handler),
    )
    assert outcome.outcome == "skipped_disabled"
    assert outcome.status.frozen is True


def test_fetch_and_store_frozen_never_touches_network_even_when_forced(
    db_conn: sqlite3.Connection,
    raw_store: RawOriginalStore,
    registry: SourceStatusRegistry,
    config: Noaa3DayForecastSourceConfig,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("frozen source must not be fetched")

    registry.freeze(config.source_id)
    outcome = fetch_and_store(
        db_conn, raw_store, config=config, registry=registry,
        now=datetime(2025, 1, 5, 22, 5, tzinfo=UTC), force=True, http_client=_mock_client(handler),
    )
    assert outcome.outcome == "skipped_frozen"


def test_fetch_and_store_quota_429_gives_explicit_status_not_a_favorable_one(
    db_conn: sqlite3.Connection,
    raw_store: RawOriginalStore,
    registry: SourceStatusRegistry,
    config: Noaa3DayForecastSourceConfig,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "60"})

    outcome = fetch_and_store(
        db_conn, raw_store, config=config, registry=registry,
        now=datetime(2025, 1, 5, 22, 5, tzinfo=UTC), http_client=_mock_client(handler),
    )
    assert outcome.outcome == "error_quota"
    assert outcome.status.quota_limited is True
    assert outcome.stored_record_ids == ()


def test_fetch_and_store_timeout_gives_explicit_status(
    db_conn: sqlite3.Connection,
    raw_store: RawOriginalStore,
    registry: SourceStatusRegistry,
    config: Noaa3DayForecastSourceConfig,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated timeout", request=request)

    outcome = fetch_and_store(
        db_conn, raw_store, config=config, registry=registry,
        now=datetime(2025, 1, 5, 22, 5, tzinfo=UTC), http_client=_mock_client(handler),
    )
    assert outcome.outcome == "error_timeout"
    assert outcome.stored_record_ids == ()


def test_fetch_and_store_empty_200_gives_explicit_status_not_favorable(
    db_conn: sqlite3.Connection,
    raw_store: RawOriginalStore,
    registry: SourceStatusRegistry,
    config: Noaa3DayForecastSourceConfig,
) -> None:
    """main-prompt.md §5: пустой 200 — ошибка источника, не «фон»."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"")

    outcome = fetch_and_store(
        db_conn, raw_store, config=config, registry=registry,
        now=datetime(2025, 1, 5, 22, 5, tzinfo=UTC), http_client=_mock_client(handler),
    )
    assert outcome.outcome == "error_format"
    assert outcome.stored_record_ids == ()


def test_fetch_and_store_unexpected_format_gives_explicit_status(
    db_conn: sqlite3.Connection,
    raw_store: RawOriginalStore,
    registry: SourceStatusRegistry,
    config: Noaa3DayForecastSourceConfig,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=MISSING_S1_TABLE)

    outcome = fetch_and_store(
        db_conn, raw_store, config=config, registry=registry,
        now=datetime(2025, 1, 5, 22, 5, tzinfo=UTC), http_client=_mock_client(handler),
    )
    assert outcome.outcome == "error_format"


def test_fetch_and_store_repeated_fetch_of_same_bulletin_is_idempotent(
    db_conn: sqlite3.Connection,
    raw_store: RawOriginalStore,
    registry: SourceStatusRegistry,
    config: Noaa3DayForecastSourceConfig,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=BULLETIN_JAN05)

    t0 = datetime(2025, 1, 5, 22, 5, tzinfo=UTC)
    first = fetch_and_store(
        db_conn, raw_store, config=config, registry=registry, now=t0, force=True,
        http_client=_mock_client(handler),
    )
    t1 = t0 + timedelta(seconds=1)
    second = fetch_and_store(
        db_conn, raw_store, config=config, registry=registry, now=t1, force=True,
        http_client=_mock_client(handler),
    )
    assert first.stored_record_ids == second.stored_record_ids


def test_load_source_config_reads_real_sources_yaml() -> None:
    from src.sources.noaa_3day_forecast import load_source_config

    cfg = load_source_config()
    assert cfg.source_id == SOURCE_ID
    assert cfg.url.startswith("https://")
    assert cfg.max_retries >= 0


def test_load_source_config_raises_for_unknown_source_id() -> None:
    from src.sources.noaa_3day_forecast import load_source_config

    with pytest.raises(KeyError):
        load_source_config(source_id="does-not-exist")


# --------------------------------------------------------------------------
# fetch_and_store_archived_bulletin — исторический выпуск по прямому URL
# --------------------------------------------------------------------------


def test_fetch_and_store_archived_bulletin_success(
    db_conn: sqlite3.Connection, raw_store: RawOriginalStore, registry: SourceStatusRegistry
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=BULLETIN_JAN05)

    outcome: FetchOutcome = fetch_and_store_archived_bulletin(
        db_conn, raw_store,
        "https://www.ngdc.noaa.gov/.../202501052200_3-day-forecast.txt",
        connect_timeout_seconds=5.0, read_timeout_seconds=15.0, max_retries=2,
        backoff_base_seconds=0.0, registry=registry,
        now=datetime(2025, 1, 6, 0, 0, tzinfo=UTC), http_client=_mock_client(handler),
    )
    assert outcome.outcome == "stored"
    assert len(outcome.stored_record_ids) == 3
    for record_id in outcome.stored_record_ids:
        stored = get_record(db_conn, record_id)
        assert stored is not None
        assert stored["source_id"] == ARCHIVE_SOURCE_ID


def test_fetch_and_store_archived_bulletin_format_error_is_explicit(
    db_conn: sqlite3.Connection, raw_store: RawOriginalStore, registry: SourceStatusRegistry
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=MISSING_ISSUED)

    outcome = fetch_and_store_archived_bulletin(
        db_conn, raw_store, "https://www.ngdc.noaa.gov/.../broken.txt",
        connect_timeout_seconds=5.0, read_timeout_seconds=15.0, max_retries=1,
        backoff_base_seconds=0.0, registry=registry,
        now=datetime(2025, 1, 6, 0, 0, tzinfo=UTC), http_client=_mock_client(handler),
    )
    assert outcome.outcome == "error_format"
    assert outcome.stored_record_ids == ()
