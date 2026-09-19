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
        request_lookback_hours=24.0,
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
    # запрошенный интервал (запрос идёт по времени ВЫПУСКА, на сутки шире;
    # читаемый интервал события расширен назад на request_lookback_hours).
    assert outcome.report is not None
    interval = outcome.report.interval
    assert interval.start <= INTERVAL_START
    assert interval.end >= INTERVAL_END
    assert interval.fetched_at == FETCHED_AT
    # Отчёт — тот самый provider-agnostic контракт FN-42, который потребляет
    # оркестрация: шлюз не строит собственной карты покрытия рядом с ним.
    assert outcome.report.source_id == donki.SOURCE_ID
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
    assert outcome.report is None  # покрытия нет вовсе — не «событий не было»
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
    assert outcome.report is None


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
    assert outcome.report is None


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


def test_ambiguous_publication_time_is_reported_per_message_type(
    monkeypatch: pytest.MonkeyPatch,
    db_conn: sqlite3.Connection,
    raw_store: RawOriginalStore,
    registry: SourceStatusRegistry,
    config: donki.DonkiSourceConfig,
) -> None:
    """Реальный случай расхождения двух полей времени выпуска
    (``20240516-7D-001``, docs/method.md §6): запись сохраняется, но навсегда
    непригодна для строгого replay, и её ТИП виден вызывающей стороне —
    иначе строгая ветка не смогла бы отличить «непригодна запись о протонном
    событии» от «непригоден еженедельный отчёт» (FN-41,
    ``ArchiveIngestReport.has_unprovable_publication_of``)."""
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
    assert only_report.report is not None
    assert only_report.report.stored_not_replay_eligible == ("20240516-7D-001",)
    # Для механизма, которому важны только SEP, этот сбой не портит интервал.
    assert not only_report.report.has_unprovable_publication_of(frozenset({"SEP"}))

    monkeypatch.setattr(donki, "fetch", _fetch_returning([conflicting_report, conflicting_sep]))
    with_sep = _store(db_conn, raw_store, registry, config)
    assert with_sep.report is not None
    assert set(with_sep.report.stored_not_replay_eligible_message_types) == {"Report", "SEP"}
    assert with_sep.report.has_unprovable_publication_of(frozenset({"SEP"}))


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
    адресах (main-prompt.md §7): значения, на которых работает шлюз,
    действительно читаются из файла, а не зашиты в модуле."""
    loaded = donki.load_source_config()

    assert loaded.source_id == donki.SOURCE_ID
    assert loaded.url.startswith("https://api.nasa.gov/DONKI/notifications")
    assert loaded.request_lookback_hours > 0
    assert loaded.connect_timeout_seconds > 0 and loaded.read_timeout_seconds > 0


def test_interpretation_thresholds_live_in_the_shared_archive_registry_not_here() -> None:
    """Ключевое архитектурное требование постановки FN-41: источник
    космопогоды подключается через ИНТЕРФЕЙС, а спор «DONKI против NOAA» и
    спор о горизонте в коде не фиксируются.

    Поэтому событийные типы и фактический горизонт читает общий, не знающий
    про DONKI адаптер (``src/sources/archive_ingest.py``), а не этот
    коннектор: у :class:`donki.DonkiSourceConfig` таких полей нет вовсе, и
    двух расходящихся копий реестра существовать не может (main-prompt.md §7).
    """
    from src.sources import archive_ingest

    assert not hasattr(donki.load_source_config(), "event_message_types")
    assert not hasattr(donki.load_source_config(), "forecast_horizon_hours")

    product = archive_ingest.load_archive_product(donki.SOURCE_ID)
    assert product.event_message_types  # перечень объявлен в реестре
    # Горизонт объявлен ЯВНО (в том числе явным null = «не установлен») —
    # значение по умолчанию в коде запрещено (приёмка FN-42 п.5).
    assert product.forecast_horizon_hours is None or product.forecast_horizon_hours > 0
