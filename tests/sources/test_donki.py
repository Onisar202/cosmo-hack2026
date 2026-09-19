"""Тесты production-шлюза архивной линии DONKI (FN-41, этап 3).

Сети нет (.ai/main-prompt.md §9): подменяется ``src.sources.donki.fetch`` —
та же точка подмены, что у ``src.sources.swpc.fetch``. Каждый тест закрывает
конкретный способ получить правдоподобный, но неверный результат: ключ API в
сохранённой записи или в тексте ошибки, отказ источника, выданный за
подтверждённое покрытие, и повторная загрузка, задваивающая воздействие.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from src.sources import donki
from src.sources.http import (
    HttpFetchResult,
    SourceHttpError,
    SourceQuotaLimitedError,
    SourceTimeoutError,
)
from src.sources.status import SourceStatusRegistry
from src.store import RawOriginalStore, get_record

UTC = timezone.utc
ARCHIVE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "sources" / "archive"
FETCHED_AT = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)

#: Интервал внутри выраженного события 10–11 мая 2024 (main-prompt.md §11).
INTERVAL_START = datetime(2024, 5, 10, 12, 0, tzinfo=UTC)
INTERVAL_END = datetime(2024, 5, 10, 20, 0, tzinfo=UTC)


@pytest.fixture
def config() -> donki.DonkiSourceConfig:
    """Конфигурация без чтения ``sources.yaml`` — тест не зависит от правок
    файла в репозитории (тот же приём, что и ``swpc_config``)."""
    return donki.DonkiSourceConfig(
        source_id=donki.SOURCE_ID,
        url="https://api.nasa.gov/DONKI/notifications",
        connect_timeout_seconds=5.0,
        read_timeout_seconds=15.0,
        max_retries=1,
        backoff_base_seconds=0.0,
        enabled=True,
        qualifying_message_types=("SEP", "GST"),
        event_persistence_lookback_hours=24.0,
    )


def _may_notifications() -> list[dict[str, Any]]:
    return list(
        json.loads(
            (ARCHIVE_DIR / "donki_2024-05-01_2024-05-15.json").read_text(encoding="utf-8")
        )
    )


def _fetch_returning(entries: list[dict[str, Any]]) -> Any:
    def _fetch(url: str, **_kwargs: Any) -> HttpFetchResult:
        return HttpFetchResult(
            status_code=200,
            body=json.dumps(entries, ensure_ascii=False).encode("utf-8"),
            url=url,
            elapsed_seconds=0.001,
        )

    return _fetch


def _store(
    conn: sqlite3.Connection,
    raw_store: RawOriginalStore,
    registry: SourceStatusRegistry,
    config: donki.DonkiSourceConfig,
    *,
    api_key: str = "test-secret-key",
) -> donki.DonkiFetchOutcome:
    return donki.fetch_and_store_window(
        conn,
        raw_store,
        config=config,
        registry=registry,
        interval_start=INTERVAL_START,
        interval_end=INTERVAL_END,
        fetched_at=FETCHED_AT,
        api_key=api_key,
    )


def test_successful_fetch_stores_records_and_reports_event_time_coverage(
    monkeypatch: pytest.MonkeyPatch,
    db_conn: sqlite3.Connection,
    raw_store: RawOriginalStore,
    registry: SourceStatusRegistry,
    config: donki.DonkiSourceConfig,
) -> None:
    monkeypatch.setattr(donki, "fetch", _fetch_returning(_may_notifications()))

    outcome = _store(db_conn, raw_store, registry, config)

    assert outcome.outcome == "stored"
    assert outcome.stored_record_ids
    # Покрытие заявляется по ВРЕМЕНИ СОБЫТИЯ и с запасом закрывает
    # запрошенный интервал (запрос идёт по времени ВЫПУСКА, на сутки шире).
    assert outcome.covered_start is not None and outcome.covered_end is not None
    assert outcome.covered_start <= INTERVAL_START
    assert outcome.covered_end >= INTERVAL_END
    assert registry.get(donki.SOURCE_ID).last_success_at == FETCHED_AT


def test_stored_record_never_carries_the_api_key(
    monkeypatch: pytest.MonkeyPatch,
    db_conn: sqlite3.Connection,
    raw_store: RawOriginalStore,
    registry: SourceStatusRegistry,
    config: donki.DonkiSourceConfig,
) -> None:
    """main-prompt.md §7: ключ не попадает ни в хранимую запись, ни в её
    ``source_url`` — при том что сам запрос без ключа не работает."""
    seen_urls: list[str] = []

    def _fetch(url: str, **_kwargs: Any) -> HttpFetchResult:
        seen_urls.append(url)
        return HttpFetchResult(
            status_code=200,
            body=json.dumps(_may_notifications(), ensure_ascii=False).encode("utf-8"),
            url=url,
            elapsed_seconds=0.001,
        )

    monkeypatch.setattr(donki, "fetch", _fetch)
    outcome = _store(db_conn, raw_store, registry, config, api_key="super-secret")

    assert seen_urls and "api_key=super-secret" in seen_urls[0]  # ключ реально отправлен
    for record_id in outcome.stored_record_ids:
        record = get_record(db_conn, record_id)
        assert record is not None
        assert "super-secret" not in json.dumps(record, ensure_ascii=False)


@pytest.mark.parametrize(
    ("exception", "expected_outcome", "expected_quota"),
    [
        (SourceTimeoutError("timeout fetching https://api.nasa.gov/...?api_key=super-secret"),
         "error_timeout", False),
        (SourceQuotaLimitedError(
            "https://api.nasa.gov/...?api_key=super-secret responded 429",
            retry_after_seconds=60.0,
         ), "error_quota", True),
        (SourceHttpError("https://api.nasa.gov/...?api_key=super-secret responded 500"),
         "error_http", False),
    ],
)
def test_fetch_failures_never_become_confirmed_coverage_and_never_leak_the_key(
    monkeypatch: pytest.MonkeyPatch,
    db_conn: sqlite3.Connection,
    raw_store: RawOriginalStore,
    registry: SourceStatusRegistry,
    config: donki.DonkiSourceConfig,
    exception: Exception,
    expected_outcome: str,
    expected_quota: bool,
) -> None:
    """main-prompt.md §2/§9 п.3: отказ, квота и ошибка источника дают явный
    отказ и НУЛЕВОЕ покрытие — «событий не было» отсюда не выводится. И
    §7: текст ошибки источника содержит полный URL, значит обязан быть
    замаскирован прежде, чем попасть в реестр статусов."""

    def _failing(url: str, **_kwargs: Any) -> HttpFetchResult:
        raise exception

    monkeypatch.setattr(donki, "fetch", _failing)
    outcome = _store(db_conn, raw_store, registry, config, api_key="super-secret")

    assert outcome.outcome == expected_outcome
    assert outcome.covered_start is None and outcome.covered_end is None
    assert outcome.stored_record_ids == ()
    assert outcome.message is not None and "super-secret" not in outcome.message
    status = registry.get(donki.SOURCE_ID)
    assert status.last_error_message is not None
    assert "super-secret" not in status.last_error_message
    assert status.quota_limited is expected_quota


def test_malformed_response_is_an_error_not_an_empty_archive(
    monkeypatch: pytest.MonkeyPatch,
    db_conn: sqlite3.Connection,
    raw_store: RawOriginalStore,
    registry: SourceStatusRegistry,
    config: donki.DonkiSourceConfig,
) -> None:
    def _fetch(url: str, **_kwargs: Any) -> HttpFetchResult:
        return HttpFetchResult(status_code=200, body=b"", url=url, elapsed_seconds=0.001)

    monkeypatch.setattr(donki, "fetch", _fetch)
    outcome = _store(db_conn, raw_store, registry, config)

    assert outcome.outcome == "error_format"
    assert outcome.covered_start is None


def test_disabled_source_does_not_touch_the_network(
    monkeypatch: pytest.MonkeyPatch,
    db_conn: sqlite3.Connection,
    raw_store: RawOriginalStore,
    registry: SourceStatusRegistry,
    config: donki.DonkiSourceConfig,
) -> None:
    """Документированный переключатель отключения (main-prompt.md §5,
    критерий Т6) — и он тоже не превращается в подтверждённое покрытие."""

    def _forbidden(url: str, **_kwargs: Any) -> HttpFetchResult:
        raise AssertionError("disabled source must not be fetched")

    monkeypatch.setattr(donki, "fetch", _forbidden)
    disabled = donki.DonkiSourceConfig(**{**config.__dict__, "enabled": False})

    outcome = _store(db_conn, raw_store, registry, disabled)

    assert outcome.outcome == "skipped_disabled"
    assert outcome.covered_start is None


def test_repeated_fetch_of_the_same_window_does_not_duplicate_records(
    monkeypatch: pytest.MonkeyPatch,
    db_conn: sqlite3.Connection,
    raw_store: RawOriginalStore,
    registry: SourceStatusRegistry,
    config: donki.DonkiSourceConfig,
) -> None:
    """main-prompt.md §2/§9 п.2: повторное сообщение с тем же
    идентификатором не удваивает воздействие — кеш архивных ответов
    бессрочен, но защита от дублей всё равно проверяется явно."""
    monkeypatch.setattr(donki, "fetch", _fetch_returning(_may_notifications()))

    first = _store(db_conn, raw_store, registry, config)
    second = _store(db_conn, raw_store, registry, config)

    assert first.outcome == "stored" and second.outcome == "stored"
    assert set(first.stored_record_ids) == set(second.stored_record_ids)
    stored_rows = db_conn.execute(
        "SELECT COUNT(*) FROM source_records WHERE source_id = ?", (donki.SOURCE_ID,)
    ).fetchone()[0]
    assert stored_rows == len(set(first.stored_record_ids))


def test_ambiguous_publication_time_is_counted_only_for_qualifying_types(
    monkeypatch: pytest.MonkeyPatch,
    db_conn: sqlite3.Connection,
    raw_store: RawOriginalStore,
    registry: SourceStatusRegistry,
    config: donki.DonkiSourceConfig,
) -> None:
    """Реальный случай расхождения двух полей времени выпуска
    (``20240516-7D-001``, docs/method.md §6): у уведомления
    неквалифицирующего типа он не мешает судить о протонных событиях, у
    квалифицирующего — мешает и обязан быть посчитан."""
    conflicting_report = {
        "messageType": "Report",
        "messageID": "20240516-7D-001",
        "messageURL": "https://example.invalid/report",
        "messageIssueTime": "2024-05-10T03:44Z",
        "messageBody": (
            "## Message Issue Date: 2024-05-10T17:40:05Z\n"
            "Report Coverage Begin Date: 2024-05-09T00:00Z\n"
            "Report Coverage End Date: 2024-05-10T00:00Z\n"
        ),
    }
    conflicting_sep = {
        "messageType": "SEP",
        "messageID": "20240510-AL-777",
        "messageURL": "https://example.invalid/sep",
        "messageIssueTime": "2024-05-10T03:44Z",
        "messageBody": (
            "## Message Issue Date: 2024-05-10T17:40:05Z\n"
            "Activity ID: 2024-05-10T13:35:00-SEP-009.\n"
        ),
    }

    monkeypatch.setattr(donki, "fetch", _fetch_returning([conflicting_report]))
    only_report = _store(db_conn, raw_store, registry, config)
    assert only_report.ambiguous_publication_count == 0

    monkeypatch.setattr(donki, "fetch", _fetch_returning([conflicting_report, conflicting_sep]))
    with_sep = _store(db_conn, raw_store, registry, config)
    assert with_sep.ambiguous_publication_count == 1


def test_request_url_used_for_records_carries_no_key_and_the_declared_range(
    config: donki.DonkiSourceConfig,
) -> None:
    url = donki.build_request_url(config, start_day="2024-05-09", end_day="2024-05-11")

    assert url.startswith("https://api.nasa.gov/DONKI/notifications?")
    assert "startDate=2024-05-09" in url and "endDate=2024-05-11" in url
    assert "api_key" not in url


def test_api_key_defaults_to_the_public_demo_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ключ — из окружения; значение по умолчанию (публичный DEMO_KEY NASA)
    не секрет и лежит в коде осознанно (.env.example, sources.yaml)."""
    monkeypatch.delenv(donki.API_KEY_ENV, raising=False)
    assert donki.resolve_api_key() == donki.DEFAULT_API_KEY

    monkeypatch.setenv(donki.API_KEY_ENV, "from-env")
    assert donki.resolve_api_key() == "from-env"


def test_registered_config_is_readable_from_sources_yaml() -> None:
    """Реестр источников — единственный источник истины о порогах и
    адресах (main-prompt.md §7): значения, на которых работает оценка,
    действительно читаются из файла, а не зашиты в модуле."""
    loaded = donki.load_source_config()

    assert loaded.source_id == donki.SOURCE_ID
    assert loaded.url.startswith("https://api.nasa.gov/DONKI/notifications")
    assert set(loaded.qualifying_message_types) == {"SEP", "GST"}
    assert loaded.event_persistence_lookback_hours > 0
