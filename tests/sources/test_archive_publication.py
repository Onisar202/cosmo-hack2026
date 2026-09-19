"""Тесты зонда пригодности архивов космопогоды (FN-23): парсинг, нормализация,
исключение по времени публикации, карта наличия/пробелов.

Полностью детерминированы и без сети (.ai/main-prompt.md §9): парсер
гоняется на реальных сохранённых архивных ответах
(``tests/fixtures/sources/archive/``, см. её README о происхождении и
задокументированных фактах) и на явно синтетических фикстурах
(``tests/fixtures/sources/archive/synthetic/``) для случая, который на
реальных ответах не встречается.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.sources.archive_probe import (
    DONKI_SOURCE_ID,
    SWPC_ARCHIVE_SOURCE_ID,
    ArchiveFormatError,
    DonkiNotification,
    GapRun,
    build_coverage_report,
    donki_notification_to_record_input,
    parse_donki_notifications,
    parse_swpc_forecast_discussion,
    parse_swpc_forecast_discussion_listing,
    swpc_forecast_discussion_to_record_input,
)
from src.store import RawOriginalStore, insert_record, select_as_of

UTC = timezone.utc
ARCHIVE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "sources" / "archive"
SYNTHETIC_DIR = ARCHIVE_DIR / "synthetic"

DONKI_FILES = [
    "donki_2024-05-01_2024-05-15.json",
    "donki_2024-05-16_2024-05-31.json",
    "donki_2024-06-01_2024-06-15.json",
    "donki_2024-06-16_2024-06-30.json",
]
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


def _load_all_donki_notifications() -> list[DonkiNotification]:
    notifications: list[DonkiNotification] = []
    for name in DONKI_FILES:
        raw = (ARCHIVE_DIR / name).read_bytes()
        notifications.extend(parse_donki_notifications(raw))
    return notifications


# ---------------------------------------------------------------------------
# DONKI — парсинг на реальных ответах
# ---------------------------------------------------------------------------


def test_donki_archive_covers_the_whole_mandatory_period_with_245_notifications() -> None:
    """README фикстур: «У всех 245 уведомлений есть messageIssueTime», без
    повторов messageID между файлами — это то, что делает DONKI основной
    линией строгого replay по §11."""
    notifications = _load_all_donki_notifications()
    assert len(notifications) == 245
    message_ids = [n.message_id for n in notifications]
    assert len(message_ids) == len(set(message_ids)), "messageID must not repeat across windows"


def test_donki_notification_issue_times_are_utc_aware() -> None:
    for notification in _load_all_donki_notifications():
        assert notification.reported_issue_time.tzinfo is not None
        reported_offset = notification.reported_issue_time.utcoffset()
        assert reported_offset is not None
        assert reported_offset.total_seconds() == 0
        if notification.resolved_issue_time is not None:
            assert notification.resolved_issue_time.tzinfo is not None
            resolved_offset = notification.resolved_issue_time.utcoffset()
            assert resolved_offset is not None
            assert resolved_offset.total_seconds() == 0


def test_donki_resolved_issue_time_matches_reported_for_almost_all_notifications() -> None:
    """Round 1 ревью PR #18: 244 из 245 реальных уведомлений имеют
    согласованные ``messageIssueTime`` и тело («## Message Issue Date:») в
    пределах округления до минуты; единственное реальное расхождение —
    `20240516-7D-001` (см. отдельный тест ниже) — обязано остаться
    единственным."""
    notifications = _load_all_donki_notifications()
    unresolved = [n for n in notifications if n.resolved_issue_time is None]
    assert [n.message_id for n in unresolved] == ["20240516-7D-001"]


def test_donki_conflicting_issue_time_fields_are_not_silently_trusted(
    db_conn: sqlite3.Connection, raw_store: RawOriginalStore
) -> None:
    """Реальный случай (round 1 ревью PR #18): `20240516-7D-001` — верхнее
    поле `messageIssueTime` даёт `2024-05-16T03:44Z`, тело письма («##
    Message Issue Date:») — `2024-05-16T17:40:05Z`, расхождение ~14 часов.
    Нельзя угадывать, какое из двух верно — published_at обязан стать
    неизвестным (main-prompt.md §1), а не одним из двух правдоподобных, но
    непроверяемых кандидатов."""
    raw = (ARCHIVE_DIR / "donki_2024-05-16_2024-05-31.json").read_bytes()
    notifications = parse_donki_notifications(raw)
    by_id = {n.message_id: n for n in notifications}
    notification = by_id["20240516-7D-001"]

    assert notification.reported_issue_time == datetime(2024, 5, 16, 3, 44, tzinfo=UTC)
    assert notification.resolved_issue_time is None

    record_input = donki_notification_to_record_input(
        notification,
        source_url="https://api.nasa.gov/DONKI/notifications",
        fetched_at=datetime(2026, 9, 18, 23, 3, 15, tzinfo=UTC),
    )
    assert record_input is not None  # Report Coverage Begin/End Date даёт настоящий observed_at
    assert record_input.published_at is None
    insert_record(db_conn, raw_store, record_input)

    # Ни отсечение сразу после верхнего поля, ни отсечение сразу после тела,
    # ни отсечение спустя годы не должны дать эту запись строгому replay —
    # published_at=None делает её непригодной навсегда (src/store/records.py),
    # а не «доступной с такого-то момента».
    for as_of in (
        datetime(2024, 5, 16, 4, 0, tzinfo=UTC),
        datetime(2024, 5, 16, 18, 0, tzinfo=UTC),
        datetime(2026, 1, 1, tzinfo=UTC),
    ):
        eligible = select_as_of(db_conn, as_of, source_id=DONKI_SOURCE_ID)
        assert not any(row["provider_record_id"] == "20240516-7D-001" for row in eligible)


def test_donki_parser_rejects_missing_required_field() -> None:
    payload = b'[{"messageType": "CME", "messageIssueTime": "2024-05-10T00:00Z"}]'
    with pytest.raises(ArchiveFormatError):
        parse_donki_notifications(payload)


def test_donki_parser_rejects_non_array_body() -> None:
    with pytest.raises(ArchiveFormatError):
        parse_donki_notifications(b'{"not": "an array"}')


def test_donki_parser_rejects_empty_body() -> None:
    with pytest.raises(ArchiveFormatError):
        parse_donki_notifications(b"")


# ---------------------------------------------------------------------------
# Обязательный тест на утечку времени (.ai/main-prompt.md §9.1):
# позднее уточнение (published_at > as_of) не должно повлиять на выборку.
# ---------------------------------------------------------------------------


def test_late_update_notification_is_excluded_by_as_of(
    db_conn: sqlite3.Connection, raw_store: RawOriginalStore
) -> None:
    """README фикстур, «Готовые случаи для тестов»: AL-009 (04:50Z) — первичное
    уведомление о CME; AL-012 (14:23Z) — обновление того же события. При
    ``as_of = 2024-05-11T12:00Z`` в строгий replay обязано попасть только
    AL-009 — AL-012 опубликован позже отсечения (main-prompt.md §1: «Четыре
    времени различаются» — дата *события* в прошлом не значит, что
    уточнение было доступно к отсечению)."""
    raw = (ARCHIVE_DIR / "donki_2024-05-01_2024-05-15.json").read_bytes()
    notifications = parse_donki_notifications(raw)
    by_id = {n.message_id: n for n in notifications}
    assert "20240511-AL-009" in by_id
    assert "20240511-AL-012" in by_id

    fetched_at = datetime(2026, 9, 18, 23, 3, 15, tzinfo=UTC)
    for message_id in ("20240511-AL-009", "20240511-AL-012"):
        record_input = donki_notification_to_record_input(
            by_id[message_id],
            source_url="https://api.nasa.gov/DONKI/notifications",
            fetched_at=fetched_at,
        )
        assert record_input is not None, f"{message_id} has an Activity ID and must convert"
        insert_record(db_conn, raw_store, record_input)

    as_of = datetime(2024, 5, 11, 12, 0, tzinfo=UTC)
    eligible = select_as_of(db_conn, as_of, source_id=DONKI_SOURCE_ID)
    provider_ids = {row["provider_record_id"] for row in eligible}

    assert "20240511-AL-009" in provider_ids
    assert "20240511-AL-012" not in provider_ids, (
        "later update published after as_of leaked into the strict replay selection"
    )


def test_earlier_as_of_excludes_both_notifications(
    db_conn: sqlite3.Connection, raw_store: RawOriginalStore
) -> None:
    """Симметричный случай: до 04:50Z ни одно из двух уведомлений ещё не
    выпущено — обе должны отсутствовать, а не «одна из двух наугад»."""
    raw = (ARCHIVE_DIR / "donki_2024-05-01_2024-05-15.json").read_bytes()
    notifications = parse_donki_notifications(raw)
    by_id = {n.message_id: n for n in notifications}
    fetched_at = datetime(2026, 9, 18, 23, 3, 15, tzinfo=UTC)
    for message_id in ("20240511-AL-009", "20240511-AL-012"):
        record_input = donki_notification_to_record_input(
            by_id[message_id],
            source_url="https://api.nasa.gov/DONKI/notifications",
            fetched_at=fetched_at,
        )
        assert record_input is not None
        insert_record(db_conn, raw_store, record_input)

    as_of = datetime(2024, 5, 11, 0, 0, tzinfo=UTC)
    eligible = select_as_of(db_conn, as_of, source_id=DONKI_SOURCE_ID)
    assert eligible == []


# ---------------------------------------------------------------------------
# Извлечение настоящего времени события, а не подмена временем публикации
# (main-prompt.md §1: «четыре времени различаются»).
# ---------------------------------------------------------------------------


def test_donki_record_observed_at_is_the_activity_time_not_the_publish_time() -> None:
    """AL-009 опубликован в 04:50:13Z, но описывает CME с Activity ID
    `2024-05-11T01:36:00-CME-001` — начавшийся более чем тремя часами раньше.
    ``observed_at`` обязан быть временем активности, не временем выпуска."""
    raw = (ARCHIVE_DIR / "donki_2024-05-01_2024-05-15.json").read_bytes()
    notifications = parse_donki_notifications(raw)
    by_id = {n.message_id: n for n in notifications}
    notification = by_id["20240511-AL-009"]

    record_input = donki_notification_to_record_input(
        notification,
        source_url="https://api.nasa.gov/DONKI/notifications",
        fetched_at=datetime(2026, 9, 18, 23, 3, 15, tzinfo=UTC),
    )
    assert record_input is not None
    activity_time = datetime(2024, 5, 11, 1, 36, 0, tzinfo=UTC)
    assert record_input.observed_at == activity_time
    assert record_input.valid_from == activity_time
    assert record_input.valid_to == activity_time
    assert record_input.observed_at != record_input.published_at
    assert record_input.quality == "reconstructed"


def test_donki_notification_without_extractable_event_time_yields_no_record_input() -> None:
    """Часть уведомлений FLR не содержит структурированного `Activity ID`
    (главная тема ревью round 1, PR #18: не подставлять время публикации
    заглушкой) — для них зонд обязан вернуть ``None``, не запись с
    придуманным ``observed_at``."""
    raw = (ARCHIVE_DIR / "donki_2024-05-01_2024-05-15.json").read_bytes()
    notifications = parse_donki_notifications(raw)
    by_id = {n.message_id: n for n in notifications}
    notification = by_id["20240515-AL-006"]  # "Flare M5.0 crossing time: …", без Activity ID
    assert "Activity ID" not in notification.raw_entry["messageBody"]

    record_input = donki_notification_to_record_input(
        notification,
        source_url="https://api.nasa.gov/DONKI/notifications",
        fetched_at=datetime(2026, 9, 18, 23, 3, 15, tzinfo=UTC),
    )
    assert record_input is None


def test_donki_report_notification_uses_coverage_window_not_a_point() -> None:
    """Тип `Report` (еженедельная сводка) несёт `Report Coverage Begin/End
    Date` — интервал, а не точку; ``valid_from``/``valid_to`` обязаны его
    отражать, не схлопываться в момент публикации."""
    raw = (ARCHIVE_DIR / "donki_2024-05-16_2024-05-31.json").read_bytes()
    notifications = parse_donki_notifications(raw)
    by_id = {n.message_id: n for n in notifications}
    notification = by_id["20240516-7D-001"]

    record_input = donki_notification_to_record_input(
        notification,
        source_url="https://api.nasa.gov/DONKI/notifications",
        fetched_at=datetime(2026, 9, 18, 23, 3, 15, tzinfo=UTC),
    )
    assert record_input is not None
    assert record_input.valid_from == datetime(2024, 5, 8, 0, 0, tzinfo=UTC)
    assert record_input.valid_to == datetime(2024, 5, 14, 23, 59, tzinfo=UTC)
    assert record_input.valid_from < record_input.valid_to


def test_swpc_record_input_no_longer_fabricates_a_forecast_horizon() -> None:
    """Round 1 ревью PR #18: прежняя версия задавала ``valid_to = issued_at
    + 12 часов`` — число, не подтверждённое содержимым бюллетеня (реальный
    горизонт — 2-3 суток, main-prompt.md §11). Запись обязана утверждать
    только точку (момент выпуска), не придуманный интервал."""
    raw = (ARCHIVE_DIR / "swpc_forecast_discussion_20240510_0030.txt").read_bytes()
    discussion = parse_swpc_forecast_discussion(raw)
    record_input = swpc_forecast_discussion_to_record_input(
        discussion,
        source_url="https://www.ngdc.noaa.gov/x",
        fetched_at=datetime(2026, 9, 18, 23, 6, 8, tzinfo=UTC),
    )
    assert record_input.observed_at == discussion.issued_at
    assert record_input.valid_from == discussion.issued_at
    assert record_input.valid_to == discussion.issued_at
    assert record_input.quality == "reconstructed"


# ---------------------------------------------------------------------------
# NOAA SWPC Forecast Discussion — парсинг на реальных ответах
# ---------------------------------------------------------------------------


def test_swpc_issued_time_is_read_from_body_not_filename() -> None:
    """README фикстур: файл «...0030» за 10 мая фактически выпущен в 00:35
    UTC — время публикации обязано браться из тела, а не из слота в имени
    файла/запросе."""
    raw = (ARCHIVE_DIR / "swpc_forecast_discussion_20240510_0030.txt").read_bytes()
    discussion = parse_swpc_forecast_discussion(raw)
    assert discussion.issued_at == datetime(2024, 5, 10, 0, 35, tzinfo=UTC)


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("swpc_forecast_discussion_20240509_1230.txt", datetime(2024, 5, 9, 12, 30, tzinfo=UTC)),
        ("swpc_forecast_discussion_20240511_0030.txt", datetime(2024, 5, 11, 0, 30, tzinfo=UTC)),
        ("swpc_forecast_discussion_20240628_1230.txt", datetime(2024, 6, 28, 12, 30, tzinfo=UTC)),
    ],
)
def test_swpc_issued_time_matches_documented_value(filename: str, expected: datetime) -> None:
    raw = (ARCHIVE_DIR / filename).read_bytes()
    discussion = parse_swpc_forecast_discussion(raw)
    assert discussion.issued_at == expected


def test_swpc_parser_rejects_empty_body() -> None:
    with pytest.raises(ArchiveFormatError):
        parse_swpc_forecast_discussion(b"")


# ---------------------------------------------------------------------------
# Неизвестная публикация исключена — синтетический случай (не встречается на
# реальных архивных ответах, см. tests/fixtures/sources/archive/README.md).
# ---------------------------------------------------------------------------


def test_swpc_bulletin_without_issued_line_is_rejected_not_silently_accepted() -> None:
    raw = (SYNTHETIC_DIR / "swpc_forecast_discussion_missing_issued.txt").read_bytes()
    with pytest.raises(ArchiveFormatError):
        parse_swpc_forecast_discussion(raw)


def test_donki_notification_without_issue_time_is_rejected() -> None:
    """Симметричный синтетический случай для DONKI (тоже не встречается на
    реальных ответах — README фикстур)."""
    payload = (
        b'[{"messageID": "made-up-1", "messageType": "CME", '
        b'"messageIssueTime": "not-a-timestamp", '
        b'"messageURL": "https://example.invalid/x", "messageBody": ""}]'
    )
    with pytest.raises(ArchiveFormatError):
        parse_donki_notifications(payload)


# ---------------------------------------------------------------------------
# Карта наличия/пробелов
# ---------------------------------------------------------------------------


def test_coverage_report_flags_the_documented_swpc_gap() -> None:
    """README фикстур: «пробел в архиве: с 15 мая по 16 июня 2024 выпусков
    нет совсем». Обязательный тест приёмки FN-23: пробел должен быть виден
    зонду, а не замаскирован под покрытый период."""
    issue_times = [
        parse_swpc_forecast_discussion((ARCHIVE_DIR / name).read_bytes()).issued_at
        for name in SWPC_FILES
    ]
    report = build_coverage_report(
        issue_times, window_start=date(2024, 5, 1), window_end=date(2024, 6, 30)
    )

    # Пробел должен включать весь диапазон 15 мая — 16 июня и не должен быть
    # искусственно сужен: это то самое доказательство, ради которого задача
    # существует (main-prompt.md §11 «в архиве SWPC Forecast Discussion нет
    # выпусков 15.05–16.06.2024»).
    covering_gap = [
        gap
        for gap in report.gap_runs
        if gap.first_day <= date(2024, 5, 20) and gap.last_day >= date(2024, 6, 10)
    ]
    assert covering_gap, f"expected a gap run covering mid-May..mid-June, got {report.gap_runs}"
    gap = covering_gap[0]
    assert gap.first_day <= date(2024, 5, 15)
    assert gap.last_day >= date(2024, 6, 16) - timedelta(days=1)

    # 1-14 мая и 17-30 июня покрыты хотя бы частично (реальные выпуски есть).
    assert report.daily_counts[date(2024, 5, 9)] > 0
    assert report.daily_counts[date(2024, 6, 20)] > 0


def test_coverage_report_shows_donki_notification_activity_has_no_multi_week_silence() -> None:
    """DONKI — основная линия §11: в отличие от SWPC, здесь нет сравнимого
    многонедельного молчания. Это НЕ значит «весь период покрыт прогнозом» —
    день без уведомлений может означать «порог не пересечён», а не
    «состояние подтверждено спокойным» (round 1 ревью PR #18, см. docstring
    build_coverage_report). Проверяется только отсутствие *длинных*
    провалов активности, а не полное отсутствие нулевых дней и не
    заменяемость периодического прогноза SWPC."""
    issue_times = [n.reported_issue_time for n in _load_all_donki_notifications()]
    report = build_coverage_report(
        issue_times, window_start=date(2024, 5, 1), window_end=date(2024, 6, 30)
    )
    longest_gap = max((gap.length_days for gap in report.gap_runs), default=0)
    assert longest_gap < 7, f"unexpectedly long DONKI silence: {report.gap_runs}"


def test_coverage_report_rejects_inverted_window() -> None:
    with pytest.raises(ValueError):
        build_coverage_report([], window_start=date(2024, 6, 1), window_end=date(2024, 5, 1))


# ---------------------------------------------------------------------------
# Листинг каталога NCEI — независимое от скачанных тел доказательство пробела
# ---------------------------------------------------------------------------


def test_listing_parses_all_slots_present_in_the_directory() -> None:
    """Май: 28 слотов (1-14 мая, дважды в сутки) — полностью совпадает с
    задокументированным покрытием (README фикстур)."""
    html = (ARCHIVE_DIR / "swpc_forecast_discussion_listing_2024-05.html").read_bytes()
    slots = parse_swpc_forecast_discussion_listing(html)
    assert len(slots) == 28
    assert min(slots) == date(2024, 5, 1)
    assert max(slots) == date(2024, 5, 14)


def test_listing_confirms_june_archive_resumes_only_on_the_17th() -> None:
    html = (ARCHIVE_DIR / "swpc_forecast_discussion_listing_2024-06.html").read_bytes()
    slots = parse_swpc_forecast_discussion_listing(html)
    assert len(slots) == 28
    assert min(slots) == date(2024, 6, 17)
    assert max(slots) == date(2024, 6, 30)


def test_listing_based_coverage_confirms_the_gap_is_a_real_archive_property() -> None:
    """Сильнее, чем test_coverage_report_flags_the_documented_swpc_gap: там
    карта строится по 11 выборочно скачанным телам, здесь — по полному
    перечню файлов каталога за оба месяца. Пробел обязан совпасть: это
    доказывает, что 15.05–16.06.2024 — свойство архива, а не артефакт
    выборки этой задачи."""
    may_html = (ARCHIVE_DIR / "swpc_forecast_discussion_listing_2024-05.html").read_bytes()
    june_html = (ARCHIVE_DIR / "swpc_forecast_discussion_listing_2024-06.html").read_bytes()
    slots = parse_swpc_forecast_discussion_listing(
        may_html
    ) + parse_swpc_forecast_discussion_listing(june_html)
    report = build_coverage_report(
        slots, window_start=date(2024, 5, 1), window_end=date(2024, 6, 30)
    )
    assert report.gap_runs == (GapRun(first_day=date(2024, 5, 15), last_day=date(2024, 6, 16)),)
    assert report.covered_days == 28  # 14 дней в мае + 14 дней в июне, каждый со слотом


# ---------------------------------------------------------------------------
# swpc_forecast_discussion_to_record_input — форма записи
# ---------------------------------------------------------------------------


def test_swpc_record_input_is_replay_eligible_once_stored(
    db_conn: sqlite3.Connection, raw_store: RawOriginalStore
) -> None:
    raw = (ARCHIVE_DIR / "swpc_forecast_discussion_20240510_0030.txt").read_bytes()
    discussion = parse_swpc_forecast_discussion(raw)
    record_input = swpc_forecast_discussion_to_record_input(
        discussion,
        source_url=(
            "https://www.ngdc.noaa.gov/stp/space-weather/swpc-products/"
            "daily_reports/discussion/2024/05/202405100030forecast_discussion.txt"
        ),
        fetched_at=datetime(2026, 9, 18, 23, 6, 8, tzinfo=UTC),
    )
    record_id = insert_record(db_conn, raw_store, record_input)
    stored = select_as_of(
        db_conn, datetime(2024, 5, 10, 1, 0, tzinfo=UTC), source_id=SWPC_ARCHIVE_SOURCE_ID
    )
    assert any(row["record_id"] == record_id for row in stored)
