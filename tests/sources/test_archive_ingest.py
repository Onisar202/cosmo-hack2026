"""Загрузка реальных сохранённых архивных ответов в хранилище (FN-42).

Полностью детерминированы и без сети (main-prompt.md §9): всё гоняется на
**реальных** сохранённых ответах (``tests/fixtures/sources/archive/``, см.
README этой папки о происхождении и задокументированных фактах). Ни один
архивный ответ здесь не выдуман: случаи события, доказанного отсутствия
события, пробела, дубликата и позднего уточнения взяты из уже сохранённых
файлов, а не смоделированы.

Все интервалы загрузки ниже — реальные окна запроса из ``*.meta.json``
соответствующего файла (``startDate``/``endDate`` в ``request_url``).
"""

from __future__ import annotations

import dataclasses
import hashlib
import sqlite3
from collections.abc import Iterable
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from src.domain.spaceweather.archive_assessment import (
    ArchiveProductPolicy,
    CoverageInterval,
    assess_archive_window,
)
from src.sources.archive_ingest import (
    ArchiveConfigError,
    ArchiveIngestReport,
    HistoricalArchiveStrategy,
    IngestedInterval,
    coverage_report_for,
    ingest_donki_notifications,
    ingest_swpc_forecast_discussion,
    load_archive_product,
    merged_ingested_intervals,
    select_forecast_inputs,
)
from src.store import RawOriginalStore, get_original, get_record

UTC = timezone.utc
ARCHIVE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "sources" / "archive"

DONKI_SOURCE_ID = "nasa-donki-notifications"
SWPC_SOURCE_ID = "noaa-swpc-forecast-discussion-archive"
DONKI_URL = "https://api.nasa.gov/DONKI/notifications"
FETCHED_AT = datetime(2026, 9, 18, 23, 3, 31, tzinfo=UTC)

#: Реальные окна запроса DONKI (из request_url соответствующих *.meta.json).
DONKI_WINDOWS = {
    "donki_2024-05-01_2024-05-15.json": (
        datetime(2024, 5, 1, tzinfo=UTC),
        datetime(2024, 5, 16, tzinfo=UTC),
    ),
    "donki_2024-05-16_2024-05-31.json": (
        datetime(2024, 5, 16, tzinfo=UTC),
        datetime(2024, 6, 1, tzinfo=UTC),
    ),
    "donki_2024-06-01_2024-06-15.json": (
        datetime(2024, 6, 1, tzinfo=UTC),
        datetime(2024, 6, 16, tzinfo=UTC),
    ),
    "donki_2024-06-16_2024-06-30.json": (
        datetime(2024, 6, 16, tzinfo=UTC),
        datetime(2024, 7, 1, tzinfo=UTC),
    ),
}

SWPC_FILES = [
    "swpc_forecast_discussion_20240509_1230.txt",
    "swpc_forecast_discussion_20240510_0030.txt",
    "swpc_forecast_discussion_20240510_1230.txt",
    "swpc_forecast_discussion_20240511_0030.txt",
    "swpc_forecast_discussion_20240511_1230.txt",
    "swpc_forecast_discussion_20240512_0030.txt",
    "swpc_forecast_discussion_20240620_0030.txt",
    "swpc_forecast_discussion_20240620_1230.txt",
    "swpc_forecast_discussion_20240624_1230.txt",
    "swpc_forecast_discussion_20240628_0030.txt",
    "swpc_forecast_discussion_20240628_1230.txt",
]


def _ingest_donki(
    conn: sqlite3.Connection, raw_store: RawOriginalStore, filename: str
) -> ArchiveIngestReport:
    start, end = DONKI_WINDOWS[filename]
    return ingest_donki_notifications(
        conn,
        raw_store,
        (ARCHIVE_DIR / filename).read_bytes(),
        source_url=DONKI_URL,
        fetched_at=FETCHED_AT,
        interval_start=start,
        interval_end=end,
    )


def _as_coverage_intervals(intervals: Iterable[IngestedInterval]) -> list[CoverageInterval]:
    """``IngestedInterval`` (``sources/``) -> ``CoverageInterval`` (``domain/``).

    Ручное преобразование, а не общий тип: домен не импортирует ``sources/``
    (main-prompt.md §8) — то же самое делает production-код (docs/method.md
    §9.1), здесь просто без orchestration-обёртки.
    """
    return [
        CoverageInterval(start=i.start, end=i.end, fetched_at=i.fetched_at) for i in intervals
    ]


def _ingest_all_donki(
    conn: sqlite3.Connection, raw_store: RawOriginalStore
) -> list[ArchiveIngestReport]:
    return [_ingest_donki(conn, raw_store, name) for name in DONKI_WINDOWS]


def _donki_strategy(horizon_hours: float | None = None) -> HistoricalArchiveStrategy:
    """Стратегия из реального реестра; горизонт подставляется только тем
    тестам, которым нужно показать САМ механизм горизонта.

    Значение в ``sources.yaml`` остаётся ``null`` до ответа кейсодержателя
    (приёмка FN-42 п.5) — тесты ниже проверяют, что механизм работает от
    конфигурации, а не что горизонт равен какому-то числу.
    """
    product = load_archive_product(DONKI_SOURCE_ID)
    if horizon_hours is not None:
        product = product.with_forecast_horizon_hours(horizon_hours)
    return HistoricalArchiveStrategy(products=(product,))


# ---------------------------------------------------------------------------
# Приёмка п.1 — детерминированный импорт с полным происхождением
# ---------------------------------------------------------------------------


def test_ingest_stores_full_provenance_for_every_record(
    db_conn: sqlite3.Connection, raw_store: RawOriginalStore
) -> None:
    """Каждая загруженная запись несёт ``raw_ref``/``checksum``/
    ``source_version``/``published_at`` и интервал действия, а оригинал
    восстанавливается по ``record_id`` с совпадающей контрольной суммой
    (main-prompt.md §3)."""
    report = _ingest_donki(db_conn, raw_store, "donki_2024-06-16_2024-06-30.json")
    assert report.stored_count > 0

    for record_id in report.stored_record_ids:
        payload = get_record(db_conn, record_id)
        assert payload is not None
        for field in ("raw_ref", "checksum", "source_version", "valid_from", "valid_to"):
            assert payload[field], f"{field} must be present and non-empty"
        assert payload["source_id"] == DONKI_SOURCE_ID
        # Оригинал именно этой записи (не всего ответа) восстановим и сходится
        # по контрольной сумме.
        original = get_original(db_conn, raw_store, record_id)
        assert hashlib.sha256(original).hexdigest() == payload["checksum"]

    eligible_payloads = [get_record(db_conn, rid) for rid in report.replay_eligible_record_ids]
    assert eligible_payloads
    for payload in eligible_payloads:
        assert payload is not None
        assert payload["published_at"] is not None
        assert payload["replay_eligible"] is True


def test_reingesting_the_same_response_is_idempotent(
    db_conn: sqlite3.Connection, raw_store: RawOriginalStore
) -> None:
    """Приёмка п.3, дубликат: повторный прогон того же файла не удваивает
    воздействие и возвращает те же ``record_id`` (main-prompt.md §2 —
    дедупликация по устойчивому идентификатору поставщика, не по времени
    получения)."""
    first = _ingest_donki(db_conn, raw_store, "donki_2024-06-16_2024-06-30.json")
    rows_after_first = db_conn.execute("SELECT COUNT(*) FROM source_records").fetchone()[0]

    second = _ingest_donki(db_conn, raw_store, "donki_2024-06-16_2024-06-30.json")
    rows_after_second = db_conn.execute("SELECT COUNT(*) FROM source_records").fetchone()[0]

    assert second.stored_record_ids == first.stored_record_ids
    assert rows_after_second == rows_after_first
    assert second.conflicts == ()


def test_notifications_without_event_time_are_skipped_not_faked(
    db_conn: sqlite3.Connection, raw_store: RawOriginalStore
) -> None:
    """Уведомления без структурированного времени события не превращаются в
    записи с подставленным временем публикации — они перечислены отдельно
    (docs/method.md §6 п.1)."""
    reports = _ingest_all_donki(db_conn, raw_store)
    skipped = [mid for r in reports for mid in r.skipped_without_event_time]
    stored = sum(r.stored_count for r in reports)
    assert skipped, "the real archive contains notifications without an extractable event time"
    assert stored + len(skipped) == 245  # README фикстур: всего 245 уведомлений


# ---------------------------------------------------------------------------
# Приёмка п.4 — неизвестный published_at навсегда вне строгого replay
# ---------------------------------------------------------------------------


def test_unknown_publication_time_is_stored_but_never_selected(
    db_conn: sqlite3.Connection, raw_store: RawOriginalStore
) -> None:
    """Реальный случай ``20240516-7D-001`` (расхождение двух указаний времени
    выпуска на ~14 часов): запись сохраняется, но не попадает в строгий
    replay ни при каком ``as_of`` — ни между двумя кандидатами, ни годы
    спустя (main-prompt.md §1)."""
    report = _ingest_donki(db_conn, raw_store, "donki_2024-05-16_2024-05-31.json")
    assert "20240516-7D-001" in report.stored_not_replay_eligible

    strategy = _donki_strategy()
    for as_of in (
        datetime(2024, 5, 16, 4, 0, tzinfo=UTC),
        datetime(2024, 5, 16, 18, 0, tzinfo=UTC),
        datetime(2026, 1, 1, tzinfo=UTC),
    ):
        selected = select_forecast_inputs(db_conn, as_of=as_of, strategy=strategy)
        assert not any(
            row["provider_record_id"] == "20240516-7D-001"
            for row in selected[DONKI_SOURCE_ID]
        )


# ---------------------------------------------------------------------------
# Приёмка п.3 — позднее уточнение остаётся отдельной версией
# ---------------------------------------------------------------------------


def test_late_sep_update_is_stored_beside_the_earlier_one_not_over_it(
    db_conn: sqlite3.Connection, raw_store: RawOriginalStore
) -> None:
    """Реальная пара уведомлений об ОДНОЙ активности ``2024-05-11T04:07:00-SEP-001``:
    ``20240511-AL-008`` выпущено в 04:13Z, ``20240511-AL-013`` — в 15:03Z.

    DONKI выпускает уточнение новым ``messageID``, поэтому «поздние уточнения
    остаются отдельными версиями» здесь означает буквально: обе записи лежат
    в хранилище рядом, ни одна не перезаписана (хранилище вообще не
    предоставляет UPDATE — ``src/store/schema.py``; версионирование одного и
    того же ``provider_record_id`` отдельно покрыто
    ``tests/store/test_versions.py::test_later_refinement_does_not_overwrite_old``).
    К отсечению 12:00Z доступно только раннее уведомление.
    """
    _ingest_donki(db_conn, raw_store, "donki_2024-05-01_2024-05-15.json")

    stored_ids = {
        row[0]
        for row in db_conn.execute(
            "SELECT provider_record_id FROM source_records WHERE source_id = ?",
            (DONKI_SOURCE_ID,),
        ).fetchall()
    }
    assert {"20240511-AL-008", "20240511-AL-013"} <= stored_ids

    selected = select_forecast_inputs(
        db_conn, as_of=datetime(2024, 5, 11, 12, 0, tzinfo=UTC), strategy=_donki_strategy()
    )
    provider_ids = {row["provider_record_id"] for row in selected[DONKI_SOURCE_ID]}
    assert "20240511-AL-008" in provider_ids
    assert "20240511-AL-013" not in provider_ids


# ---------------------------------------------------------------------------
# Приёмка п.2 — тест на утечку времени (main-prompt.md §9.1)
# ---------------------------------------------------------------------------


def test_adding_a_record_published_after_as_of_changes_neither_selection_nor_assessment(
    db_conn: sqlite3.Connection, raw_store: RawOriginalStore
) -> None:
    """Обязательный тест §9.1 на реальных данных и на полном пути
    «загрузка → выборка → оценка».

    Сначала в хранилище только окно 01–15 мая; затем добавляются ещё три
    реальных архивных окна, все публикации которых позже отсечения
    ``2024-05-10T14:00Z``. Ни выбранный набор, ни оценка не меняются ни в
    одном поле.
    """
    as_of = datetime(2024, 5, 10, 14, 0, tzinfo=UTC)
    window_start = datetime(2024, 5, 10, 13, 0, tzinfo=UTC)
    window_end = as_of

    first_report = _ingest_donki(db_conn, raw_store, "donki_2024-05-01_2024-05-15.json")
    strategy = _donki_strategy()
    policy = ArchiveProductPolicy.from_config(strategy.product_for(DONKI_SOURCE_ID))
    intervals = [
        CoverageInterval(
            start=first_report.interval.start,
            end=first_report.interval.end,
            fetched_at=first_report.interval.fetched_at,
        )
    ]

    before_selected = select_forecast_inputs(db_conn, as_of=as_of, strategy=strategy)
    before = assess_archive_window(
        before_selected[DONKI_SOURCE_ID],
        policy=policy,
        ingested_intervals=intervals,
        window_start=window_start,
        window_end=window_end,
        as_of=as_of,
    )

    # Всё, что публикуется позже отсечения: три следующих архивных окна.
    for name in (
        "donki_2024-05-16_2024-05-31.json",
        "donki_2024-06-01_2024-06-15.json",
        "donki_2024-06-16_2024-06-30.json",
    ):
        _ingest_donki(db_conn, raw_store, name)

    after_selected = select_forecast_inputs(db_conn, as_of=as_of, strategy=strategy)
    after = assess_archive_window(
        after_selected[DONKI_SOURCE_ID],
        policy=policy,
        ingested_intervals=intervals,
        window_start=window_start,
        window_end=window_end,
        as_of=as_of,
    )

    assert {r["record_id"] for r in after_selected[DONKI_SOURCE_ID]} == {
        r["record_id"] for r in before_selected[DONKI_SOURCE_ID]
    }
    assert after == before


def test_a_later_ingested_coverage_interval_does_not_change_an_unaffected_historical_assessment(
    db_conn: sqlite3.Connection, raw_store: RawOriginalStore
) -> None:
    """Round 3 ревью PR #37: предыдущий тест выше переиспользует ОДИН и тот
    же список ``intervals`` для «до» и «после», поэтому не проверяет путь,
    в котором сама КАРТА ПОКРЫТИЯ (не только набор записей) растёт между
    двумя вычислениями — как это происходит на практике, когда
    ``merged_ingested_intervals`` пересчитывается заново из всех отчётов о
    загрузке, накопленных к текущему моменту.

    Здесь интервалы покрытия для «после» **пересчитываются заново**
    (``merged_ingested_intervals`` по ВСЕМ четырём загруженным окнам, а не
    список из одного окна, зафиксированный до дополнительной загрузки) — и
    всё равно не меняют оценку окна 10 мая, потому что дополнительно
    прочитанные интервалы (16 мая — 30 июня) не затрагивают ни это окно, ни
    его горизонт.
    """
    as_of = datetime(2024, 5, 10, 14, 0, tzinfo=UTC)
    window_start = datetime(2024, 5, 10, 13, 0, tzinfo=UTC)
    window_end = as_of
    strategy = _donki_strategy()
    policy = ArchiveProductPolicy.from_config(strategy.product_for(DONKI_SOURCE_ID))
    relevant_types = strategy.product_for(DONKI_SOURCE_ID).event_message_types

    first_report = _ingest_donki(db_conn, raw_store, "donki_2024-05-01_2024-05-15.json")
    # Записи фиксируются один раз здесь: этот тест изолированно проверяет
    # рост КАРТЫ ПОКРЫТИЯ, рост набора записей — предмет теста выше.
    selected = select_forecast_inputs(db_conn, as_of=as_of, strategy=strategy)
    before_intervals = merged_ingested_intervals(
        [first_report], source_id=DONKI_SOURCE_ID, relevant_message_types=relevant_types
    )
    before = assess_archive_window(
        selected[DONKI_SOURCE_ID],
        policy=policy,
        ingested_intervals=_as_coverage_intervals(before_intervals),
        window_start=window_start,
        window_end=window_end,
        as_of=as_of,
    )

    later_reports = [first_report]
    for name in (
        "donki_2024-05-16_2024-05-31.json",
        "donki_2024-06-01_2024-06-15.json",
        "donki_2024-06-16_2024-06-30.json",
    ):
        later_reports.append(_ingest_donki(db_conn, raw_store, name))
    after_intervals = merged_ingested_intervals(
        later_reports, source_id=DONKI_SOURCE_ID, relevant_message_types=relevant_types
    )
    assert after_intervals != before_intervals, (
        "the coverage map must actually have grown for this test to exercise anything"
    )

    after = assess_archive_window(
        selected[DONKI_SOURCE_ID],
        policy=policy,
        ingested_intervals=_as_coverage_intervals(after_intervals),
        window_start=window_start,
        window_end=window_end,
        as_of=as_of,
    )

    assert after == before


# ---------------------------------------------------------------------------
# Приёмка п.3 — событие и доказанное отсутствие события, на реальных данных
# ---------------------------------------------------------------------------


def test_real_sep_event_window_is_event_present(
    db_conn: sqlite3.Connection, raw_store: RawOriginalStore
) -> None:
    """10 мая 2024: ``20240510-AL-004`` (выпущено 13:46Z) сообщает о протонном
    событии ``2024-05-10T13:35:00-SEP-001`` — превышение 10 pfu, уровень S1
    (README фикстур, «События в периоде»)."""
    report = _ingest_donki(db_conn, raw_store, "donki_2024-05-01_2024-05-15.json")
    as_of = datetime(2024, 5, 10, 14, 0, tzinfo=UTC)
    selected = select_forecast_inputs(db_conn, as_of=as_of, strategy=_donki_strategy())

    assessment = assess_archive_window(
        selected[DONKI_SOURCE_ID],
        policy=ArchiveProductPolicy.from_config(load_archive_product(DONKI_SOURCE_ID)),
        ingested_intervals=[
            CoverageInterval(
                start=report.interval.start,
                end=report.interval.end,
                fetched_at=report.interval.fetched_at,
            )
        ],
        window_start=datetime(2024, 5, 10, 13, 0, tzinfo=UTC),
        window_end=as_of,
        as_of=as_of,
    )

    assert assessment.status == "EVENT_PRESENT"
    assert "20240510-AL-004" in {e.provider_record_id for e in assessment.events}
    assert all(e.message_type == "SEP" for e in assessment.events)


def test_control_quiet_period_is_no_event_detected_not_insufficient(
    db_conn: sqlite3.Connection, raw_store: RawOriginalStore
) -> None:
    """Контрольный спокойный период 16–27 июня 2024 (main-prompt.md §11): в
    реально загруженном окне архива нет ни одного уведомления SEP, и окно
    целиком покрыто загруженным интервалом — это доказанное отсутствие
    события, а не «оценить невозможно».

    Горизонт задаётся тестом через конфигурацию (6 ч) — именно чтобы
    показать, что механизм работает от значения реестра; само значение в
    ``sources.yaml`` остаётся ``null`` до ответа кейсодержателя.
    """
    report = _ingest_donki(db_conn, raw_store, "donki_2024-06-16_2024-06-30.json")
    as_of = datetime(2024, 6, 20, 0, 0, tzinfo=UTC)
    strategy = _donki_strategy(horizon_hours=6.0)
    selected = select_forecast_inputs(db_conn, as_of=as_of, strategy=strategy)

    assessment = assess_archive_window(
        selected[DONKI_SOURCE_ID],
        policy=ArchiveProductPolicy.from_config(strategy.product_for(DONKI_SOURCE_ID)),
        ingested_intervals=[
            CoverageInterval(
                start=report.interval.start,
                end=report.interval.end,
                fetched_at=report.interval.fetched_at,
            )
        ],
        window_start=as_of,
        window_end=datetime(2024, 6, 20, 6, 0, tzinfo=UTC),
        as_of=as_of,
    )

    assert assessment.status == "NO_EVENT_DETECTED"
    assert assessment.events == ()
    assert assessment.critical_gap is False
    assert assessment.beyond_horizon is False


def test_same_quiet_window_is_insufficient_data_when_horizon_is_not_established(
    db_conn: sqlite3.Connection, raw_store: RawOriginalStore
) -> None:
    """Тот же интервал, те же записи, но горизонт продукта не установлен
    (реальное состояние ``sources.yaml``: ``forecast_horizon_hours: null``) —
    окно вперёд от отсечения становится ``beyond_horizon`` и даёт
    ``INSUFFICIENT_DATA``, а не «спокойно» (main-prompt.md §4, приёмка п.5).
    """
    report = _ingest_donki(db_conn, raw_store, "donki_2024-06-16_2024-06-30.json")
    as_of = datetime(2024, 6, 20, 0, 0, tzinfo=UTC)
    strategy = _donki_strategy()  # горизонт как в реестре — null
    assert strategy.product_for(DONKI_SOURCE_ID).forecast_horizon_hours is None
    selected = select_forecast_inputs(db_conn, as_of=as_of, strategy=strategy)

    assessment = assess_archive_window(
        selected[DONKI_SOURCE_ID],
        policy=ArchiveProductPolicy.from_config(strategy.product_for(DONKI_SOURCE_ID)),
        ingested_intervals=[
            CoverageInterval(
                start=report.interval.start,
                end=report.interval.end,
                fetched_at=report.interval.fetched_at,
            )
        ],
        window_start=as_of,
        window_end=datetime(2024, 6, 20, 6, 0, tzinfo=UTC),
        as_of=as_of,
    )

    assert assessment.status == "INSUFFICIENT_DATA"
    assert assessment.beyond_horizon is True
    assert assessment.horizon_end is None


# ---------------------------------------------------------------------------
# Приёмка п.3 — пробел, на реальном архиве SWPC
# ---------------------------------------------------------------------------


def test_swpc_ingest_coverage_map_shows_the_real_archive_gap(
    db_conn: sqlite3.Connection, raw_store: RawOriginalStore
) -> None:
    """Загрузка 11 реальных выпусков Forecast Discussion даёт карту, в
    которой виден задокументированный пробел 15.05–16.06.2024 (README
    фикстур) — он не маскируется под покрытый период (main-prompt.md §2)."""
    reports = []
    for name in SWPC_FILES:
        slot_day = datetime.strptime(name.split("_")[3], "%Y%m%d").replace(tzinfo=UTC)
        reports.append(
            ingest_swpc_forecast_discussion(
                db_conn,
                raw_store,
                (ARCHIVE_DIR / name).read_bytes(),
                source_url=f"https://www.ngdc.noaa.gov/archive/{name}",
                fetched_at=FETCHED_AT,
                interval_start=slot_day,
                interval_end=slot_day.replace(hour=23, minute=59),
            )
        )

    assert all(r.stored_count == 1 for r in reports)
    report = coverage_report_for(
        reports, window_start=date(2024, 5, 1), window_end=date(2024, 6, 30)
    )
    covering = [
        gap
        for gap in report.gap_runs
        if gap.first_day <= date(2024, 5, 15) and gap.last_day >= date(2024, 6, 16)
    ]
    assert covering, f"documented 15.05-16.06 gap must be visible, got {report.gap_runs}"
    assert report.daily_counts[date(2024, 5, 9)] == 1
    assert report.daily_counts[date(2024, 5, 20)] == 0


def test_window_inside_the_swpc_gap_is_insufficient_data(
    db_conn: sqlite3.Connection, raw_store: RawOriginalStore
) -> None:
    """Окно внутри пробела архива: нет ни одного загруженного интервала,
    покрывающего его, — «данных нет», не «спокойно»."""
    name = "swpc_forecast_discussion_20240512_0030.txt"
    report = ingest_swpc_forecast_discussion(
        db_conn,
        raw_store,
        (ARCHIVE_DIR / name).read_bytes(),
        source_url=f"https://www.ngdc.noaa.gov/archive/{name}",
        fetched_at=FETCHED_AT,
        interval_start=datetime(2024, 5, 12, tzinfo=UTC),
        interval_end=datetime(2024, 5, 13, tzinfo=UTC),
    )
    as_of = datetime(2024, 5, 20, 12, 0, tzinfo=UTC)
    product = load_archive_product(SWPC_SOURCE_ID)
    strategy = HistoricalArchiveStrategy(products=(product,))
    selected = select_forecast_inputs(db_conn, as_of=as_of, strategy=strategy)

    assessment = assess_archive_window(
        selected[SWPC_SOURCE_ID],
        policy=ArchiveProductPolicy.from_config(product),
        ingested_intervals=[
            CoverageInterval(
                start=report.interval.start,
                end=report.interval.end,
                fetched_at=report.interval.fetched_at,
            )
        ],
        window_start=datetime(2024, 5, 20, 6, 0, tzinfo=UTC),
        window_end=as_of,
        as_of=as_of,
    )
    assert assessment.status == "INSUFFICIENT_DATA"
    assert assessment.critical_gap is True
    assert assessment.coverage_fraction == 0.0


# ---------------------------------------------------------------------------
# Стратегия и конфигурация (приёмка п.5, п.6)
# ---------------------------------------------------------------------------


def test_registry_declares_no_hardcoded_horizon_for_archive_products() -> None:
    """Приёмка п.5: до ответа кейсодержателя ни один архивный продукт не
    объявляет горизонт — значение прочитано из реестра и равно ``null``,
    а не подставлено кодом."""
    for source_id in (DONKI_SOURCE_ID, SWPC_SOURCE_ID, "noaa-swpc-3day-forecast-archive"):
        product = load_archive_product(source_id)
        assert product.forecast_horizon_hours is None
        assert product.forecast_horizon_note.strip(), "the null must be explained in the registry"


def test_horizon_override_is_configuration_not_code() -> None:
    product = load_archive_product(DONKI_SOURCE_ID).with_forecast_horizon_hours(24.0)
    assert product.forecast_horizon_hours == 24.0
    assert product.event_message_types == frozenset({"SEP"})
    with pytest.raises(ValueError):
        product.with_forecast_horizon_hours(0.0)


def test_missing_horizon_key_is_an_error_not_a_silent_default(tmp_path: Path) -> None:
    """«Горизонт не объявлен в реестре» и «реестр говорит, что горизонт не
    установлен» — разные утверждения; первое обязано быть отказом, а не
    молчаливым ``None`` (main-prompt.md §7)."""
    registry = tmp_path / "sources.yaml"
    registry.write_text(
        "space_weather:\n"
        "  - id: some-product\n"
        "    record_kind: warning\n"
        "    event_message_types: []\n",
        encoding="utf-8",
    )
    with pytest.raises(ArchiveConfigError):
        load_archive_product("some-product", path=registry)


def test_disabled_product_is_not_selected(
    db_conn: sqlite3.Connection, raw_store: RawOriginalStore
) -> None:
    """Переключатель отключения источника (main-prompt.md §5, Т6) действует и
    на исторический путь: отключённый продукт не выбирается вовсе — что
    даёт «оценить невозможно», а не благоприятную оценку."""
    _ingest_donki(db_conn, raw_store, "donki_2024-05-01_2024-05-15.json")
    product = load_archive_product(DONKI_SOURCE_ID)
    disabled = HistoricalArchiveStrategy(
        products=(
            type(product)(
                source_id=product.source_id,
                record_kind=product.record_kind,
                forecast_horizon_hours=product.forecast_horizon_hours,
                forecast_horizon_note=product.forecast_horizon_note,
                event_message_types=product.event_message_types,
                enabled=False,
            ),
        )
    )
    selected = select_forecast_inputs(
        db_conn, as_of=datetime(2024, 5, 15, tzinfo=UTC), strategy=disabled
    )
    assert selected == {}


def test_strategy_rejects_duplicate_products() -> None:
    product = load_archive_product(DONKI_SOURCE_ID)
    with pytest.raises(ValueError):
        HistoricalArchiveStrategy(products=(product, product))


def test_merged_intervals_join_adjacent_archive_windows(
    db_conn: sqlite3.Connection, raw_store: RawOriginalStore
) -> None:
    """Стык двух окон загрузки (15/16 мая) не должен выглядеть пробелом."""
    reports = _ingest_all_donki(db_conn, raw_store)
    merged = merged_ingested_intervals(
        reports, source_id=DONKI_SOURCE_ID, relevant_message_types=frozenset({"SEP"})
    )
    assert merged == (
        IngestedInterval(
            source_id=DONKI_SOURCE_ID,
            start=datetime(2024, 5, 1, tzinfo=UTC),
            end=datetime(2024, 7, 1, tzinfo=UTC),
            fetched_at=FETCHED_AT,
        ),
    )


def test_unresolved_relevant_notification_drops_the_whole_interval_from_coverage(
    db_conn: sqlite3.Connection, raw_store: RawOriginalStore
) -> None:
    """Round 3 ревью PR #37: в реальных фикстурах ни один ``SEP`` никогда не
    пропускается и не конфликтует (единственный пропускаемый тип — ``FLR``,
    ``test_notifications_without_event_time_are_skipped_not_faked``), поэтому
    этот сценарий целенаправленно смоделирован через ``dataclasses.replace``
    поверх РЕАЛЬНОГО отчёта о загрузке — не выдуманы ни архивный ответ, ни
    хранимые записи, только сам факт «в этом ответе не нормализовалось
    релевантное уведомление».

    Раньше (до этого раунда) ``IngestedInterval`` отчёта объявлял весь
    запрошенный интервал прочитанным независимо от ``skipped_message_types``/
    ``conflict_message_types`` — ошибка нормализации именно SEP-уведомления
    могла тихо превратиться в ``NO_EVENT_DETECTED``. Теперь такой отчёт
    целиком выбывает из ``merged_ingested_intervals`` для релевantных типов,
    и окно внутри него получает ``INSUFFICIENT_DATA`` через обычный
    ``critical_gap`` — тот же путь, что и для любого непрочитанного
    интервала, а не специальный код на эту ситуацию.
    """
    clean_report = _ingest_donki(db_conn, raw_store, "donki_2024-06-16_2024-06-30.json")
    tainted_report = dataclasses.replace(
        clean_report,
        skipped_without_event_time=(*clean_report.skipped_without_event_time, "fake-sep-001"),
        skipped_message_types=(*clean_report.skipped_message_types, "SEP"),
    )

    relevant = frozenset({"SEP"})
    trusted = merged_ingested_intervals(
        [tainted_report], source_id=DONKI_SOURCE_ID, relevant_message_types=relevant
    )
    assert trusted == (), "a report with an unresolved SEP notification must not be trusted"

    # Для нерелевантного типа (сконфигурирован только GST) тот же самый
    # отчёт остаётся доверенным — испорчена не запись, а конкретно доверие
    # к покрытию SEP.
    trusted_for_gst = merged_ingested_intervals(
        [tainted_report], source_id=DONKI_SOURCE_ID, relevant_message_types=frozenset({"GST"})
    )
    assert len(trusted_for_gst) == 1

    as_of = datetime(2024, 6, 20, 0, 0, tzinfo=UTC)
    strategy = _donki_strategy(horizon_hours=6.0)
    selected = select_forecast_inputs(db_conn, as_of=as_of, strategy=strategy)
    assessment = assess_archive_window(
        selected[DONKI_SOURCE_ID],
        policy=ArchiveProductPolicy.from_config(strategy.product_for(DONKI_SOURCE_ID)),
        ingested_intervals=_as_coverage_intervals(trusted),
        window_start=as_of,
        window_end=datetime(2024, 6, 20, 6, 0, tzinfo=UTC),
        as_of=as_of,
    )
    assert assessment.status == "INSUFFICIENT_DATA"
    assert assessment.critical_gap is True
    assert assessment.coverage_fraction == 0.0
