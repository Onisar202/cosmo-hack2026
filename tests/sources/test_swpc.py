"""Тесты коннектора NOAA SWPC (FN-22): парсер, нормализация, шлюз fetch_and_store.

Полностью детерминированы и без сети (.ai/main-prompt.md §9): HTTP заменяется
``httpx.MockTransport``, парсер гоняется на сохранённых реальных фикстурах
(``tests/fixtures/sources/swpc/``, см. её README про их происхождение).
Единственное исключение — ``test_live_smoke`` внизу файла, помеченный
``skip`` по умолчанию.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from src.sources.http import (
    SourceHttpError,
    SourceQuotaLimitedError,
    SourceTimeoutError,
    fetch,
)
from src.sources.status import (
    SourceStatusRegistry,
    effective_status,
    is_critically_stale,
    staleness_seconds,
)
from src.sources.swpc import (
    SOURCE_ID,
    SwpcFormatError,
    SwpcSourceConfig,
    fetch_and_store,
    load_source_config,
    parse_response,
    to_record_input,
)
from src.store import RawOriginalStore, get_record, select_as_of

from .conftest import FIXTURES_DIR

UTC = timezone.utc
SAMPLE = (FIXTURES_DIR / "integral-protons-1-day.sample.json").read_bytes()
EMPTY = (FIXTURES_DIR / "empty-array.json").read_bytes()
GATEWAY_ERROR_HTML = (FIXTURES_DIR / "gateway-error.html").read_bytes()


# --------------------------------------------------------------------------
# parse_response — на реальных сохранённых ответах
# --------------------------------------------------------------------------


def test_parse_response_filters_to_target_energy_channel() -> None:
    samples = parse_response(SAMPLE)
    # Фикстура несёт 8 каналов энергии на каждый time_tag; только >=10 MeV
    # должен пройти фильтр.
    assert len(samples) == 6
    assert {s.observed_at.minute for s in samples} == {0, 5, 10, 15, 20, 25}


def test_parse_response_keeps_plausible_flux_value() -> None:
    samples = parse_response(SAMPLE)
    by_minute = {s.observed_at.minute: s for s in samples}
    assert by_minute[0].value == pytest.approx(4.271)
    assert by_minute[25].value == pytest.approx(31.44)


def test_parse_response_maps_negative_sentinel_to_none_not_zero() -> None:
    """main-prompt.md §2: отсутствующее значение не заменяется нулём."""
    samples = parse_response(SAMPLE)
    by_minute = {s.observed_at.minute: s for s in samples}
    assert by_minute[20].value is None
    assert by_minute[20].value != 0


def test_parse_response_flags_yaw_flip_period_as_degraded() -> None:
    samples = parse_response(SAMPLE)
    by_minute = {s.observed_at.minute: s for s in samples}
    assert by_minute[15].degraded is True
    assert by_minute[0].degraded is False


def test_parse_response_rejects_empty_array() -> None:
    with pytest.raises(SwpcFormatError):
        parse_response(EMPTY)


def test_parse_response_rejects_empty_body() -> None:
    with pytest.raises(SwpcFormatError):
        parse_response(b"")


def test_parse_response_rejects_non_json_body() -> None:
    """Инфраструктурный отказ (502/504 отдают HTML) — не «нет данных»."""
    with pytest.raises(SwpcFormatError):
        parse_response(GATEWAY_ERROR_HTML)


def test_parse_response_rejects_missing_target_channel() -> None:
    """Смена формата источника (канал исчез/переименован) обнаруживается
    тестом, а не читается как «нет активности» (.ai/backend-prompt.md §3)."""
    payload = (
        b'[{"time_tag": "2024-05-10T12:00:00Z", "satellite": 18, "flux": 1.0, '
        b'"energy": ">=1 MeV", "yaw_flip": 0}]'
    )
    with pytest.raises(SwpcFormatError):
        parse_response(payload)


def test_parse_response_rejects_non_list_payload() -> None:
    with pytest.raises(SwpcFormatError):
        parse_response(b'{"error": "not an array"}')


def test_parse_response_accepts_time_tag_without_z_suffix_as_utc() -> None:
    payload = (
        b'[{"time_tag": "2024-05-10T12:00:00", "satellite": 18, "flux": 5.0, '
        b'"energy": ">=10 MeV", "yaw_flip": 0}]'
    )
    samples = parse_response(payload)
    assert samples[0].observed_at == datetime(2024, 5, 10, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# to_record_input — нормализация в форму contracts/record.schema.json
# --------------------------------------------------------------------------


def test_to_record_input_has_no_published_at_and_is_not_replay_eligible(
    db_conn: sqlite3.Connection, raw_store: RawOriginalStore
) -> None:
    """Продукт не несёт времени публикации — записи этого коннектора
    навсегда непригодны для строгого replay, и это ожидаемо (main-prompt.md
    §11, приёмка FN-22 «replay_eligible=false»)."""
    from src.store import insert_record

    samples = parse_response(SAMPLE)
    fetched_at = datetime(2024, 5, 10, 12, 30, tzinfo=UTC)
    record_input = to_record_input(
        samples[0],
        source_url="https://services.swpc.noaa.gov/json/goes/primary/x.json",
        fetched_at=fetched_at,
        raw_bytes=SAMPLE,
    )
    assert record_input.published_at is None

    record_id = insert_record(db_conn, raw_store, record_input)
    stored = get_record(db_conn, record_id)
    assert stored is not None
    assert stored["published_at"] is None
    assert stored["replay_eligible"] is False
    # fetched_at не подменяет observed_at
    assert stored["fetched_at"] != stored["observed_at"]


def test_to_record_input_missing_value_has_no_unit(
    db_conn: sqlite3.Connection, raw_store: RawOriginalStore
) -> None:
    from src.store import insert_record

    samples = parse_response(SAMPLE)
    missing = next(s for s in samples if s.value is None)
    record_input = to_record_input(
        missing,
        source_url="https://services.swpc.noaa.gov/json/goes/primary/x.json",
        fetched_at=datetime(2024, 5, 10, 12, 30, tzinfo=UTC),
        raw_bytes=SAMPLE,
    )
    assert record_input.unit is None
    assert record_input.quality == "unknown"

    record_id = insert_record(db_conn, raw_store, record_input)
    stored = get_record(db_conn, record_id)
    assert stored is not None
    assert stored["value"] is None
    assert stored["unit"] is None


def test_to_record_input_degraded_quality_for_yaw_flip(
    db_conn: sqlite3.Connection, raw_store: RawOriginalStore
) -> None:
    samples = parse_response(SAMPLE)
    degraded_sample = next(s for s in samples if s.degraded)
    record_input = to_record_input(
        degraded_sample,
        source_url="https://services.swpc.noaa.gov/json/goes/primary/x.json",
        fetched_at=datetime(2024, 5, 10, 12, 30, tzinfo=UTC),
        raw_bytes=SAMPLE,
    )
    assert record_input.quality == "degraded"


def test_content_derived_source_version_allows_later_correction_as_new_row(
    db_conn: sqlite3.Connection, raw_store: RawOriginalStore
) -> None:
    """Если один и тот же time_tag переиздан с другим значением (despiking),
    это должно стать новой записью рядом со старой, а не конфликтом
    дедупликации (main-prompt.md §2 «поздние уточнения — рядом со старыми»)."""
    from src.sources.swpc import SwpcSample
    from src.store import insert_record

    observed_at = datetime(2024, 5, 10, 12, 0, tzinfo=UTC)
    original = SwpcSample(satellite="18", observed_at=observed_at, value=9.0, degraded=False)
    corrected = SwpcSample(satellite="18", observed_at=observed_at, value=11.0, degraded=False)
    id_1 = insert_record(
        db_conn,
        raw_store,
        to_record_input(
            original,
            source_url="https://x.invalid",
            fetched_at=datetime(2024, 5, 10, 12, 1, tzinfo=UTC),
            raw_bytes=b"a",
        ),
    )
    id_2 = insert_record(
        db_conn,
        raw_store,
        to_record_input(
            corrected,
            source_url="https://x.invalid",
            fetched_at=datetime(2024, 5, 10, 13, 0, tzinfo=UTC),
            raw_bytes=b"b",
        ),
    )
    assert id_1 != id_2
    assert get_record(db_conn, id_1) is not None
    assert get_record(db_conn, id_2) is not None


def test_repeated_fetch_of_same_value_is_idempotent(
    db_conn: sqlite3.Connection, raw_store: RawOriginalStore
) -> None:
    from src.store import insert_record

    samples = parse_response(SAMPLE)
    record_input = to_record_input(
        samples[0], source_url="https://x.invalid",
        fetched_at=datetime(2024, 5, 10, 12, 30, tzinfo=UTC), raw_bytes=SAMPLE,
    )
    id_1 = insert_record(db_conn, raw_store, record_input)
    # Повторное получение того же значения позже — тот же дедуп-ключ, тот же
    # record_id, второй раз не создаёт воздействия.
    record_input_again = to_record_input(
        samples[0], source_url="https://x.invalid",
        fetched_at=datetime(2024, 5, 10, 12, 35, tzinfo=UTC), raw_bytes=SAMPLE,
    )
    id_2 = insert_record(db_conn, raw_store, record_input_again)
    assert id_1 == id_2


# --------------------------------------------------------------------------
# src/sources/http.py — таймауты, повторы, 429
# --------------------------------------------------------------------------


def _mock_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_http_fetch_retries_transient_5xx_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, content=b"try later")
        return httpx.Response(200, content=SAMPLE)

    sleeps: list[float] = []
    result = fetch(
        "https://services.swpc.noaa.gov/x.json",
        connect_timeout_seconds=1.0,
        read_timeout_seconds=1.0,
        max_retries=3,
        backoff_base_seconds=0.01,
        client=_mock_client(handler),
        sleep=sleeps.append,
    )
    assert result.status_code == 200
    assert result.body == SAMPLE
    assert calls["n"] == 3
    assert sleeps == [0.01, 0.02]  # экспоненциальный рост между двумя повторами


def test_http_fetch_gives_up_after_max_retries() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503)

    with pytest.raises(SourceHttpError):
        fetch(
            "https://services.swpc.noaa.gov/x.json",
            connect_timeout_seconds=1.0,
            read_timeout_seconds=1.0,
            max_retries=2,
            backoff_base_seconds=0.0,
            client=_mock_client(handler),
            sleep=lambda _seconds: None,
        )
    assert calls["n"] == 3  # первая попытка + 2 повтора


def test_http_fetch_429_is_not_retried_and_raises_quota_error() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, headers={"Retry-After": "30"})

    with pytest.raises(SourceQuotaLimitedError) as excinfo:
        fetch(
            "https://services.swpc.noaa.gov/x.json",
            connect_timeout_seconds=1.0,
            read_timeout_seconds=1.0,
            max_retries=3,
            backoff_base_seconds=1.0,
            client=_mock_client(handler),
            sleep=lambda _seconds: (_ for _ in ()).throw(AssertionError("must not sleep on 429")),
        )
    assert calls["n"] == 1
    assert excinfo.value.retry_after_seconds == 30.0


def test_http_fetch_timeout_after_retries_raises_timeout_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated read timeout", request=request)

    with pytest.raises(SourceTimeoutError):
        fetch(
            "https://services.swpc.noaa.gov/x.json",
            connect_timeout_seconds=1.0,
            read_timeout_seconds=1.0,
            max_retries=1,
            backoff_base_seconds=0.0,
            client=_mock_client(handler),
            sleep=lambda _seconds: None,
        )


def test_http_fetch_does_not_retry_permanent_4xx() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404)

    with pytest.raises(SourceHttpError):
        fetch(
            "https://services.swpc.noaa.gov/x.json",
            connect_timeout_seconds=1.0,
            read_timeout_seconds=1.0,
            max_retries=3,
            backoff_base_seconds=0.0,
            client=_mock_client(handler),
            sleep=lambda _seconds: (_ for _ in ()).throw(AssertionError("must not retry 404")),
        )
    assert calls["n"] == 1


# --------------------------------------------------------------------------
# fetch_and_store — шлюз: TTL/periodic refresh, force, freeze, disable, статус
# --------------------------------------------------------------------------


def test_fetch_and_store_success_stores_records_and_updates_status(
    db_conn: sqlite3.Connection,
    raw_store: RawOriginalStore,
    registry: SourceStatusRegistry,
    swpc_config: SwpcSourceConfig,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=SAMPLE)

    now = datetime(2024, 5, 10, 12, 30, tzinfo=UTC)
    outcome = fetch_and_store(
        db_conn,
        raw_store,
        config=swpc_config,
        registry=registry,
        now=now,
        http_client=_mock_client(handler),
    )
    assert outcome.outcome == "stored"
    assert len(outcome.stored_record_ids) == 6
    assert outcome.status.last_success_at == now
    assert outcome.status.quota_limited is False

    # Реально отражено в store: строго исторический replay по этим записям
    # невозможен (published_at всегда null), но сами записи там есть.
    for record_id in outcome.stored_record_ids:
        assert get_record(db_conn, record_id) is not None
    assert select_as_of(db_conn, now, source_id=SOURCE_ID) == []


def test_fetch_and_store_skips_network_within_ttl_then_refreshes_after(
    db_conn: sqlite3.Connection,
    raw_store: RawOriginalStore,
    registry: SourceStatusRegistry,
    swpc_config: SwpcSourceConfig,
) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, content=SAMPLE)

    t0 = datetime(2024, 5, 10, 12, 0, tzinfo=UTC)
    first = fetch_and_store(
        db_conn, raw_store, config=swpc_config, registry=registry, now=t0,
        http_client=_mock_client(handler),
    )
    assert first.outcome == "stored"
    assert calls["n"] == 1

    # Внутри TTL (300s) — вызов не должен трогать сеть вовсе.
    t1 = t0 + timedelta(seconds=60)
    second = fetch_and_store(
        db_conn, raw_store, config=swpc_config, registry=registry, now=t1,
        http_client=_mock_client(handler),
    )
    assert second.outcome == "skipped_fresh"
    assert calls["n"] == 1

    # После истечения TTL — периодический refresh реально идёт в сеть.
    t2 = t0 + timedelta(seconds=400)
    third = fetch_and_store(
        db_conn, raw_store, config=swpc_config, registry=registry, now=t2,
        http_client=_mock_client(handler),
    )
    assert third.outcome == "stored"


def test_fetch_and_store_force_bypasses_ttl(
    db_conn: sqlite3.Connection,
    raw_store: RawOriginalStore,
    registry: SourceStatusRegistry,
    swpc_config: SwpcSourceConfig,
) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, content=SAMPLE)

    t0 = datetime(2024, 5, 10, 12, 0, tzinfo=UTC)
    fetch_and_store(
        db_conn, raw_store, config=swpc_config, registry=registry, now=t0,
        http_client=_mock_client(handler),
    )
    assert calls["n"] == 1

    t1 = t0 + timedelta(seconds=5)  # далеко внутри TTL
    forced = fetch_and_store(
        db_conn, raw_store, config=swpc_config, registry=registry, now=t1, force=True,
        http_client=_mock_client(handler),
    )
    assert forced.outcome == "stored"
    assert calls["n"] == 2


def test_fetch_and_store_frozen_never_touches_network_even_when_forced(
    db_conn: sqlite3.Connection,
    raw_store: RawOriginalStore,
    registry: SourceStatusRegistry,
    swpc_config: SwpcSourceConfig,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("frozen source must not be fetched")

    registry.freeze(swpc_config.source_id)
    now = datetime(2024, 5, 10, 12, 0, tzinfo=UTC)
    outcome = fetch_and_store(
        db_conn, raw_store, config=swpc_config, registry=registry, now=now, force=True,
        http_client=_mock_client(handler),
    )
    assert outcome.outcome == "skipped_frozen"
    assert outcome.status.frozen is True

    registry.unfreeze(swpc_config.source_id)
    unfrozen_status = effective_status(registry.get(swpc_config.source_id), config_enabled=True)
    assert unfrozen_status.frozen is False


def test_fetch_and_store_disabled_source_never_touches_network(
    db_conn: sqlite3.Connection,
    raw_store: RawOriginalStore,
    registry: SourceStatusRegistry,
    swpc_config: SwpcSourceConfig,
) -> None:
    from dataclasses import replace

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("disabled source must not be fetched")

    disabled_config = replace(swpc_config, enabled=False)
    outcome = fetch_and_store(
        db_conn, raw_store, config=disabled_config, registry=registry,
        now=datetime(2024, 5, 10, 12, 0, tzinfo=UTC), force=True,
        http_client=_mock_client(handler),
    )
    assert outcome.outcome == "skipped_disabled"
    # main-prompt.md: отключение видно через то же поле контракта, что и заморозка
    assert outcome.status.frozen is True


def test_fetch_and_store_quota_429_gives_explicit_status_not_a_favorable_one(
    db_conn: sqlite3.Connection,
    raw_store: RawOriginalStore,
    registry: SourceStatusRegistry,
    swpc_config: SwpcSourceConfig,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "60"})

    now = datetime(2024, 5, 10, 12, 0, tzinfo=UTC)
    outcome = fetch_and_store(
        db_conn, raw_store, config=swpc_config, registry=registry, now=now,
        http_client=_mock_client(handler),
    )
    assert outcome.outcome == "error_quota"
    assert outcome.stored_record_ids == ()
    assert outcome.status.quota_limited is True
    assert outcome.status.last_error_at == now
    assert outcome.status.last_success_at is None  # ни разу не было успеха — не выдумываем его


def test_fetch_and_store_timeout_gives_explicit_status(
    db_conn: sqlite3.Connection,
    raw_store: RawOriginalStore,
    registry: SourceStatusRegistry,
    swpc_config: SwpcSourceConfig,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("simulated connect timeout", request=request)

    now = datetime(2024, 5, 10, 12, 0, tzinfo=UTC)
    outcome = fetch_and_store(
        db_conn, raw_store, config=swpc_config, registry=registry, now=now,
        http_client=_mock_client(handler),
    )
    assert outcome.outcome == "error_timeout"
    assert outcome.status.quota_limited is False
    assert outcome.status.last_success_at is None


def test_fetch_and_store_unexpected_format_gives_explicit_status_not_favorable(
    db_conn: sqlite3.Connection,
    raw_store: RawOriginalStore,
    registry: SourceStatusRegistry,
    swpc_config: SwpcSourceConfig,
) -> None:
    """Пустой 200 не должен читаться как «данных нет, всё спокойно»
    (main-prompt.md §2 приёмка FN-22)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=EMPTY)

    now = datetime(2024, 5, 10, 12, 0, tzinfo=UTC)
    outcome = fetch_and_store(
        db_conn, raw_store, config=swpc_config, registry=registry, now=now,
        http_client=_mock_client(handler),
    )
    assert outcome.outcome == "error_format"
    assert outcome.stored_record_ids == ()
    assert "empty" in (outcome.message or "").lower()


def test_fetch_and_store_error_preserved_after_later_recovery(
    db_conn: sqlite3.Connection,
    raw_store: RawOriginalStore,
    registry: SourceStatusRegistry,
    swpc_config: SwpcSourceConfig,
) -> None:
    """Статус источника — самостоятельная наблюдаемая сущность: последняя
    ошибка остаётся видна даже после того, как источник восстановился
    (.ai/backend-prompt.md §3)."""
    fail_then_succeed = {"failed": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if not fail_then_succeed["failed"]:
            fail_then_succeed["failed"] = True
            return httpx.Response(429)
        return httpx.Response(200, content=SAMPLE)

    t0 = datetime(2024, 5, 10, 12, 0, tzinfo=UTC)
    first = fetch_and_store(
        db_conn, raw_store, config=swpc_config, registry=registry, now=t0,
        http_client=_mock_client(handler),
    )
    assert first.outcome == "error_quota"

    t1 = t0 + timedelta(seconds=400)  # за TTL, реальный повтор
    second = fetch_and_store(
        db_conn, raw_store, config=swpc_config, registry=registry, now=t1,
        http_client=_mock_client(handler),
    )
    assert second.outcome == "stored"
    assert second.status.last_success_at == t1
    assert second.status.last_error_at == t0  # ошибка не стёрлась успехом
    assert second.status.quota_limited is True  # тоже описывает последнюю ошибку, не текущий момент


# --------------------------------------------------------------------------
# src/sources/status.py — устаревание и критическая недоступность
# --------------------------------------------------------------------------


def test_staleness_seconds_is_none_without_any_success(registry: SourceStatusRegistry) -> None:
    status = registry.get("some-source")
    assert staleness_seconds(status, now=datetime(2024, 1, 1, tzinfo=UTC)) is None


def test_never_succeeded_source_counts_as_critically_stale(
    registry: SourceStatusRegistry,
) -> None:
    status = registry.get("some-source")
    now = datetime(2024, 1, 1, tzinfo=UTC)
    assert is_critically_stale(status, now=now, critical_staleness_seconds=3600) is True


def test_is_critically_stale_true_past_threshold_false_before(
    registry: SourceStatusRegistry,
) -> None:
    t0 = datetime(2024, 1, 1, tzinfo=UTC)
    registry.record_success("s", at=t0)
    status = registry.get("s")
    just_before = t0 + timedelta(seconds=3599)
    just_after = t0 + timedelta(seconds=3601)
    assert is_critically_stale(status, now=just_before, critical_staleness_seconds=3600) is False
    assert is_critically_stale(status, now=just_after, critical_staleness_seconds=3600) is True


def test_effective_status_disabled_config_forces_frozen_true(
    registry: SourceStatusRegistry,
) -> None:
    status = registry.get("s")
    assert effective_status(status, config_enabled=True).frozen is False
    assert effective_status(status, config_enabled=False).frozen is True


# --------------------------------------------------------------------------
# sources.yaml — конфигурация читается из реестра, не из кода
# --------------------------------------------------------------------------


def test_load_source_config_reads_real_sources_yaml() -> None:
    config = load_source_config()
    assert config.source_id == SOURCE_ID
    assert config.url.startswith("https://services.swpc.noaa.gov/")
    assert config.max_retries >= 1
    assert config.enabled is True


def test_load_source_config_raises_for_unknown_source_id(tmp_path: Path) -> None:
    empty_registry = tmp_path / "sources.yaml"
    empty_registry.write_text("space_weather: []\n", encoding="utf-8")
    with pytest.raises(KeyError):
        load_source_config(empty_registry)


# --------------------------------------------------------------------------
# Живой smoke-тест — НЕ часть детерминированного набора (.ai/main-prompt.md §9)
# --------------------------------------------------------------------------


@pytest.mark.skip(
    reason=(
        "live-smoke: реальное обращение к services.swpc.noaa.gov, отдельно от "
        "детерминированных тестов (.ai/main-prompt.md §9). Запускать вручную "
        "с сетевым доступом: снять @pytest.mark.skip или вызвать "
        "pytest tests/sources/test_swpc.py::test_live_smoke -m '' --no-skip "
        "(либо временно удалить декоратор), не в CI."
    )
)
def test_live_smoke(db_conn: sqlite3.Connection, raw_store: RawOriginalStore) -> None:
    from src.sources.status import SourceStatusRegistry as _Registry

    outcome = fetch_and_store(
        db_conn, raw_store, registry=_Registry(), force=True,
    )
    assert outcome.outcome == "stored"
    assert outcome.stored_record_ids
