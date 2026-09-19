"""Тесты коннектора NASA MEO 2024 LEO forecast (MMOD, FN-39, S2-08).

Реальный первичный файл (не синтетика) — обоснование и проверка байтов
(SHA-256, контрольные строки) в этой сессии описаны в
``tests/fixtures/mmod/nasa_meo_leo_forecast_2024/README.md`` и
``sources.yaml#nasa-meo-leo-forecast-2024``. Детерминированность
(.ai/main-prompt.md §9) обеспечена тем, что файл — бандловая копия под
``src/sources/data/`` (не сеть); ``fetch()`` монкипатчится там, где тест
хочет подменить содержимое, тем же паттерном, что у остальных источников.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.domain.mmod.background import ratio_to_background
from src.sources import mmod as mmod_module
from src.sources.mmod import (
    PUBLISHED_AT,
    SOURCE_ID,
    VALUE_UNIT,
    MmodFluxForecastFormatError,
    build_mmod_background_records,
    ensure_mmod_records_for_window,
    fetch,
    parse_flux_forecast,
)
from src.store import RawOriginalStore, get_record, select_as_of
from src.store.schema import connect

UTC = timezone.utc

REAL_FILE_BYTES = mmod_module.DEFAULT_DATA_PATH.read_bytes()

# SHA-256 проверена в этой сессии дважды (Jira-вложение и локальная копия) —
# tests/fixtures/mmod/nasa_meo_leo_forecast_2024/README.md.
REAL_FILE_SHA256 = "c7b1abc031c04f04255f9c7f40ec2f1612ce2f921e06c5d34be5b39149700a1f"


@pytest.fixture
def db_conn(tmp_path: Path) -> sqlite3.Connection:
    conn = connect(tmp_path / "store.sqlite3")
    yield conn
    conn.close()


@pytest.fixture
def raw_store(tmp_path: Path) -> RawOriginalStore:
    return RawOriginalStore(tmp_path / "raw")


def test_bundled_file_matches_verified_checksum() -> None:
    """Единственная копия файла в репозитории (``src/sources/data/`` — не
    ``tests/``/``data/``, оба исключены ``.dockerignore``, см. docstring
    ``src/sources/mmod.py::DEFAULT_DATA_PATH``) обязана оставаться
    байт-в-байт тем же файлом, что проверен и задокументирован в README
    fixture (``tests/fixtures/mmod/nasa_meo_leo_forecast_2024/README.md``,
    хранящем только происхождение/контрольную сумму, не второй экземпляр
    файла — round 1 ревью PR #31, «diff слишком большой» из-за дублирования
    строки в двух копиях)."""
    import hashlib

    assert hashlib.sha256(REAL_FILE_BYTES).hexdigest() == REAL_FILE_SHA256


def test_fetch_reads_the_bundled_file() -> None:
    assert fetch() == REAL_FILE_BYTES


class TestParseFluxForecast:
    def test_parses_full_real_file(self) -> None:
        parsed = parse_flux_forecast(REAL_FILE_BYTES)
        assert len(parsed.rows) == 8791
        assert parsed.grid_start == datetime(2024, 1, 1, tzinfo=UTC)
        assert parsed.grid_end == datetime(2025, 1, 1, 6, 0, tzinfo=UTC)

    def test_matches_owner_reported_control_rows(self) -> None:
        """Контрольные строки, названные владельцем задачи FN-39 (Jira),
        сверены посимвольно с сохранённым файлом — не пересчитаны заново
        этим тестом (main-prompt.md §9 «численная часть проверяется
        независимым примером»: независимый пример здесь — сам первичный
        документ NASA, не эта кодовая база)."""
        parsed = parse_flux_forecast(REAL_FILE_BYTES)
        by_time = {row.ut_datetime: row.factor_105j for row in parsed.rows}
        assert by_time[datetime(2024, 5, 5, 14, 0, tzinfo=UTC)] == pytest.approx(0.3165592)
        assert by_time[datetime(2024, 6, 9, 23, 0, tzinfo=UTC)] == pytest.approx(0.5820643)

    def test_rejects_wrong_field_count(self) -> None:
        with pytest.raises(MmodFluxForecastFormatError, match="expected 13"):
            parse_flux_forecast(b"# header\n 2024-01-01 00:00:00 only-two-fields\n")

    def test_rejects_unparseable_datetime(self) -> None:
        # 11 числовых полей после даты/времени (julian, solarlon, zhr,
        # flux x4, factor x4) — итого 13 whitespace-разделённых полей.
        bad = b"# h\n bad-date 00:00:00 1 1 1 1 1 1 1 1 1 1 1\n"
        with pytest.raises(MmodFluxForecastFormatError, match="cannot parse"):
            parse_flux_forecast(bad)

    # Строка данных — 13 whitespace-разделённых полей: date, time, julian,
    # solar_lon, zhr, flux[6.7J], flux[105J], flux[2.83kJ], flux[105kJ],
    # factor[6.7J], factor[105J] (используемая колонка, индекс 10), factor
    # [2.83kJ], factor[105kJ]. ``_ROW`` — шаблон с 11 числовыми полями,
    # {factor105j} подставляется в позицию factor[105J].
    _ROW = " 2024-01-01 00:00:00  1.0  2.0  3.0  4.0  5.0  6.0  7.0  8.0  {factor105j}  9.0  10.0\n"

    def test_rejects_non_numeric_factor(self) -> None:
        row = self._ROW.format(factor105j="NaN-ish")
        with pytest.raises(MmodFluxForecastFormatError):
            parse_flux_forecast(("# h\n" + row).encode())

    def test_rejects_negative_factor(self) -> None:
        row = self._ROW.format(factor105j="-0.5")
        with pytest.raises(MmodFluxForecastFormatError, match="non-negative"):
            parse_flux_forecast(("# h\n" + row).encode())

    def test_rejects_non_increasing_timestamps(self) -> None:
        row_01 = self._ROW.format(factor105j="0.1").replace("00:00:00", "01:00:00")
        row_00 = self._ROW.format(factor105j="0.1")
        rows = "# h\n" + row_01 + row_00
        with pytest.raises(MmodFluxForecastFormatError, match="strictly increasing"):
            parse_flux_forecast(rows.encode())

    def test_rejects_empty_input(self) -> None:
        with pytest.raises(MmodFluxForecastFormatError, match="no data rows"):
            parse_flux_forecast(b"# only a header\n# and another\n")


class TestBuildMmodBackgroundRecords:
    def test_builds_one_record_per_row_with_raw_value_not_ratio(self) -> None:
        """RecordInput.value — ``factor_105j`` КАК ОПУБЛИКОВАНО, не
        ``1 + factor`` (main-prompt.md §8 «получение не считает физику»,
        см. docstring src/sources/mmod.py)."""
        parsed = parse_flux_forecast(REAL_FILE_BYTES)
        subset = parsed.rows[:3]
        from src.sources.mmod import ParsedFluxForecast

        windowed = ParsedFluxForecast(
            rows=subset, grid_start=parsed.grid_start, grid_end=parsed.grid_end
        )
        fetched_at = datetime(2026, 1, 1, tzinfo=UTC)
        records = build_mmod_background_records(
            windowed, raw_bytes=REAL_FILE_BYTES, fetched_at=fetched_at
        )
        assert len(records) == 3
        first = records[0]
        assert first.source_id == SOURCE_ID
        assert first.record_kind == "forecast"
        assert first.unit == VALUE_UNIT
        assert first.value == subset[0].factor_105j
        assert first.value != ratio_to_background(subset[0].factor_105j)
        assert first.published_at == PUBLISHED_AT
        assert first.valid_from == subset[0].ut_datetime
        assert first.valid_to == subset[0].ut_datetime + timedelta(hours=1)
        assert first.spatial_context["worst_case_unshielded_leo"] is True
        assert first.spatial_context["not_spacecraft_surface_specific"] is True

    def test_provider_record_id_is_unique_per_hour(self) -> None:
        parsed = parse_flux_forecast(REAL_FILE_BYTES)
        from src.sources.mmod import ParsedFluxForecast

        windowed = ParsedFluxForecast(
            rows=parsed.rows[:5], grid_start=parsed.grid_start, grid_end=parsed.grid_end
        )
        records = build_mmod_background_records(
            windowed, raw_bytes=REAL_FILE_BYTES, fetched_at=datetime(2026, 1, 1, tzinfo=UTC)
        )
        assert len({r.provider_record_id for r in records}) == 5


class TestEnsureMmodRecordsForWindow:
    def test_inserts_only_nodes_bracketing_the_window(
        self, db_conn: sqlite3.Connection, raw_store: RawOriginalStore
    ) -> None:
        """Не весь год — только узлы, нужные этому окну (main-prompt.md §8
        «не строй инфраструктуру там, где её не нужно»; ~25с холодной вставки
        всех ~8791 записей измерено и недопустимо, см. docs/mechanisms.md §12.4)."""
        window_start = datetime(2024, 5, 5, 13, 0, tzinfo=UTC)
        window_end = datetime(2024, 5, 5, 15, 0, tzinfo=UTC)
        record_ids, doc_start, doc_end = ensure_mmod_records_for_window(
            db_conn,
            raw_store,
            window_start=window_start,
            window_end=window_end,
            fetched_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        # Окно 2 часа + запас по 1 часу с каждой стороны -> не более ~5 узлов,
        # точно не тысячи.
        assert 2 <= len(record_ids) <= 6
        assert doc_start == datetime(2024, 1, 1, tzinfo=UTC)
        assert doc_end == datetime(2025, 1, 1, 6, 0, tzinfo=UTC)

        stored = select_as_of(
            db_conn,
            datetime(2026, 1, 1, tzinfo=UTC),
            source_id=SOURCE_ID,
            record_kind="forecast",
        )
        assert len(stored) == len(record_ids)
        for record in stored:
            assert record["source_id"] == SOURCE_ID
            assert record["unit"] == VALUE_UNIT
            assert record["published_at"] is not None
            assert record["replay_eligible"] is True

    def test_idempotent_reinsertion_does_not_duplicate(
        self, db_conn: sqlite3.Connection, raw_store: RawOriginalStore
    ) -> None:
        window_start = datetime(2024, 5, 5, 13, 0, tzinfo=UTC)
        window_end = datetime(2024, 5, 5, 15, 0, tzinfo=UTC)
        ids_1, _, _ = ensure_mmod_records_for_window(
            db_conn,
            raw_store,
            window_start=window_start,
            window_end=window_end,
            fetched_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        ids_2, _, _ = ensure_mmod_records_for_window(
            db_conn,
            raw_store,
            window_start=window_start,
            window_end=window_end,
            fetched_at=datetime(2026, 1, 2, tzinfo=UTC),  # другой fetched_at
        )
        assert sorted(ids_1) == sorted(ids_2)

    def test_window_entirely_outside_grid_inserts_nothing(
        self, db_conn: sqlite3.Connection, raw_store: RawOriginalStore
    ) -> None:
        record_ids, doc_start, doc_end = ensure_mmod_records_for_window(
            db_conn,
            raw_store,
            window_start=datetime(2026, 9, 19, tzinfo=UTC),
            window_end=datetime(2026, 9, 19, 6, 0, tzinfo=UTC),
            fetched_at=datetime(2026, 9, 19, tzinfo=UTC),
        )
        assert record_ids == []
        assert doc_start == datetime(2024, 1, 1, tzinfo=UTC)
        assert doc_end == datetime(2025, 1, 1, 6, 0, tzinfo=UTC)

    def test_stored_record_matches_contract_shape(
        self, db_conn: sqlite3.Connection, raw_store: RawOriginalStore
    ) -> None:
        record_ids, _, _ = ensure_mmod_records_for_window(
            db_conn,
            raw_store,
            window_start=datetime(2024, 5, 5, 14, 0, tzinfo=UTC),
            window_end=datetime(2024, 5, 5, 15, 0, tzinfo=UTC),
            fetched_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        record = get_record(db_conn, record_ids[0])
        assert record is not None
        assert len(record["checksum"]) == 64
        assert record["raw_ref"]
