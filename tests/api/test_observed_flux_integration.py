"""FN-38 (S2-08): классификатор наблюдаемого потока GOES, подключённый к
``src/api/service.py`` — production-путь (``run_calculation``, то же, что
вызывает HTTP-роутер), не только доменный unit-тест
(tests/domain/spaceweather/test_observed_classifier.py — там же чистая
классификация без хранилища).

Приёмка FN-38 п.2 («реальные current-наблюдения проходят через production
API, а не только доменный unit-тест») и п.3 («missing/stale/429/timeout дают
явный source_status/critical gap без подстановки нуля»).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from src.api import service as service_module
from src.api.schemas import CalculationRequest
from src.api.service import ensure_store_ready
from src.api.service import run_calculation as _run_calculation
from src.config import Settings, get_settings
from src.sources import noaa_3day_forecast as noaa_3day_source
from src.sources import orbit as orbit_source
from src.sources import swpc as swpc_source
from src.sources.http import HttpFetchResult, SourceTimeoutError
from src.sources.status import SourceStatusRegistry
from src.store import RawOriginalStore, insert_record
from src.store import connect as connect_store
from src.store import get_result as store_get_result
from tests.api.conftest import noaa_3day_sample_bytes, orbit_tle_bytes, swpc_sample_bytes

UTC = timezone.utc

_Env = tuple[Settings, RawOriginalStore]


@pytest.fixture
def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Env]:
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("STORE_DB_PATH", str(tmp_path / "store.sqlite3"))
    monkeypatch.setenv("STORE_RAW_DIR", str(tmp_path / "raw"))
    get_settings.cache_clear()

    monkeypatch.setattr(orbit_source, "fetch_current_tle", lambda **_kwargs: orbit_tle_bytes())
    monkeypatch.setattr(
        noaa_3day_source,
        "fetch",
        lambda url, **_kwargs: HttpFetchResult(
            status_code=200, body=noaa_3day_sample_bytes(), url=url, elapsed_seconds=0.001
        ),
    )

    settings = get_settings()
    ensure_store_ready(settings)
    raw_store = RawOriginalStore(settings.store_raw_dir)
    yield settings, raw_store
    get_settings.cache_clear()


def _seed_dense_hour_of_observations(
    *, settings: Settings, raw_store: RawOriginalStore, start_at: datetime, spike_minute: int
) -> None:
    """Заполняет хранилище через РЕАЛЬНЫЕ ``src.sources.swpc.SwpcSample`` +
    ``to_record_input`` + ``insert_record`` (те же функции, что использует
    ``fetch_and_store`` при живом получении) — двенадцать отсчётов каждые
    5 минут на весь час, все ``>=10 МэВ`` (S1), один — ``>=100`` (S2) в
    ``spike_minute``. Не байты фиктивного HTTP-ответа: сама эта функция не
    заменяет проверку парсера (уже покрыта tests/sources/test_swpc.py), а
    даёт production-нормализованные записи достаточной плотности для полного
    покрытия часового окна — часовой реальный HTTP-фикстуры в репозитории
    нет (см. tests/fixtures/sources/swpc/README.md: сетевой доступ к NOAA
    заблокирован политикой этого окружения)."""
    conn = connect_store(settings.store_db_path)
    try:
        for minute in range(0, 60, 5):
            observed_at = start_at + timedelta(minutes=minute)
            value = 150.0 if minute == spike_minute else 15.0
            sample = swpc_source.SwpcSample(
                satellite="18",
                observed_at=observed_at,
                value=value,
                degraded=False,
                raw_entry={
                    "time_tag": observed_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "satellite": 18,
                    "flux": value,
                    "energy": ">=10 MeV",
                    "yaw_flip": 0,
                },
            )
            record_input = swpc_source.to_record_input(
                sample,
                source_url="https://services.swpc.noaa.gov/json/goes/primary/"
                "integral-protons-1-day.json",
                fetched_at=start_at,
            )
            insert_record(conn, raw_store, record_input)
    finally:
        conn.close()


def test_seeded_full_hour_of_observations_yields_ok_status_via_production_path(
    _env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, raw_store = _env
    registry = SourceStatusRegistry()
    window_start = datetime(2024, 5, 10, 10, 0, tzinfo=UTC)
    _seed_dense_hour_of_observations(
        settings=settings, raw_store=raw_store, start_at=window_start, spike_minute=15
    )

    def swpc_fetch(url: str, **_kwargs: Any) -> HttpFetchResult:
        return HttpFetchResult(
            status_code=200, body=swpc_sample_bytes(), url=url, elapsed_seconds=0.001
        )

    monkeypatch.setattr(swpc_source, "fetch", swpc_fetch)

    request = CalculationRequest(
        mode="current", start_at=window_start, duration_hours=1, search_window_hours=4,
    )
    result_id = _run_calculation(
        request,
        settings=settings,
        raw_store=raw_store,
        registry=registry,
        now=window_start + timedelta(hours=10),  # давно после окна — не beyond_horizon
    )

    conn = connect_store(settings.store_db_path)
    try:
        result = store_get_result(conn, result_id)
    finally:
        conn.close()
    assert result is not None

    win_a = next(w for w in result["windows"] if w["window_id"] == "win-a")
    space_weather = next(m for m in win_a["mechanisms"] if m["mechanism"] == "space_weather")

    assert space_weather["status"] == "ok"
    assert space_weather["critical_gap"] is False
    assert space_weather["coverage_fraction"] == pytest.approx(1.0)
    assert space_weather["max_level"] == "S2"
    assert space_weather["exceedance_hours_by_level"]["S1"] == pytest.approx(1.0)
    assert space_weather["exceedance_hours_by_level"]["S2"] == pytest.approx(5 / 60)
    assert space_weather["exceedance_hours_by_level"]["S3"] == pytest.approx(0.0)
    assert len(space_weather["record_ids"]) == 12

    observed_manifest = [
        m for m in result["data_manifest"] if m["record_kind"] == "observation"
    ]
    assert len(observed_manifest) == 12
    assert {m["record_id"] for m in observed_manifest} == set(space_weather["record_ids"])
    assert all(m["source_id"] == swpc_source.SOURCE_ID for m in observed_manifest)

    # mmod (FN-39 не сделано) по-прежнему держит окно исключённым из
    # автоматического сравнения — правило доминирования не меняется здесь.
    assert win_a["excluded_from_comparison"] is True
    assert result["recommendation"]["status"] == "all_windows_excluded"


def test_frozen_source_with_stale_last_success_yields_stale_data_status(
    _env: _Env,
) -> None:
    """FN-38 приёмка п.3 («stale... дают явный source_status/critical gap»):
    источник заморожен (документированный переключатель, main-prompt.md §5),
    последний реальный успех — за пределами
    ``critical_staleness_seconds`` (sources.yaml → space_weather[0], 3600с).
    Живая попытка эту заморозку не может обойти — ``fetch`` не подменяется,
    сеть не должна быть тронута."""
    settings, raw_store = _env
    registry = SourceStatusRegistry()

    long_ago = datetime(2024, 5, 10, 6, 0, tzinfo=UTC)
    registry.record_success(swpc_source.SOURCE_ID, at=long_ago)
    registry.freeze(swpc_source.SOURCE_ID)

    now = long_ago + timedelta(hours=6)  # << 3600с (1ч) критического порога
    request = CalculationRequest(
        mode="current",
        start_at=now - timedelta(hours=2),
        duration_hours=1,
        search_window_hours=4,
    )
    result_id = _run_calculation(
        request, settings=settings, raw_store=raw_store, registry=registry, now=now,
    )

    conn = connect_store(settings.store_db_path)
    try:
        result = store_get_result(conn, result_id)
    finally:
        conn.close()
    assert result is not None

    win_a = next(w for w in result["windows"] if w["window_id"] == "win-a")
    space_weather = next(m for m in win_a["mechanisms"] if m["mechanism"] == "space_weather")
    assert space_weather["status"] == "stale_data"
    assert space_weather["max_level"] is None
    assert space_weather["critical_gap"] is True

    swpc_status = next(
        s for s in result["source_status"] if s["source_id"] == swpc_source.SOURCE_ID
    )
    assert swpc_status["frozen"] is True


def test_window_entirely_in_the_future_yields_beyond_horizon_status(
    _env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GOES — мгновенное наблюдение без собственного прогноза вперёд
    (main-prompt.md §11/§4): окно ВКД для планирования ВКД обычно лежит в
    будущем относительно момента расчёта — это честно ``beyond_horizon``,
    не ``not_implemented`` и не благоприятная оценка."""
    settings, raw_store = _env
    registry = SourceStatusRegistry()

    def swpc_fetch(url: str, **_kwargs: Any) -> HttpFetchResult:
        return HttpFetchResult(
            status_code=200, body=swpc_sample_bytes(), url=url, elapsed_seconds=0.001
        )

    monkeypatch.setattr(swpc_source, "fetch", swpc_fetch)

    now = datetime(2024, 5, 10, 8, 0, tzinfo=UTC)
    request = CalculationRequest(
        mode="current",
        start_at=now + timedelta(hours=1),
        duration_hours=2,
        search_window_hours=4,
    )
    result_id = _run_calculation(
        request, settings=settings, raw_store=raw_store, registry=registry, now=now,
    )

    conn = connect_store(settings.store_db_path)
    try:
        result = store_get_result(conn, result_id)
    finally:
        conn.close()
    assert result is not None

    for window in result["windows"]:
        space_weather = next(
            m for m in window["mechanisms"] if m["mechanism"] == "space_weather"
        )
        assert space_weather["status"] == "beyond_horizon"
        assert space_weather["max_level"] is None
        assert space_weather["critical_gap"] is True


def test_timeout_with_no_stored_observations_yields_source_error_status(
    _env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FN-38 приёмка п.3 («429/timeout дают явный source_status/critical gap
    без подстановки нуля»): первая попытка сразу отказывает, ни одной
    записи наблюдения в хранилище нет вовсе — причина пробела обязана быть
    видна как отказ источника, а не безликое «нет данных»."""
    settings, raw_store = _env
    registry = SourceStatusRegistry()

    def failing_fetch(url: str, **_kwargs: Any) -> HttpFetchResult:
        raise SourceTimeoutError(f"simulated timeout for {url}")

    monkeypatch.setattr(swpc_source, "fetch", failing_fetch)

    now = datetime(2024, 5, 10, 14, 0, tzinfo=UTC)
    request = CalculationRequest(
        mode="current",
        start_at=now - timedelta(hours=2),
        duration_hours=1,
        search_window_hours=4,
    )
    result_id = _run_calculation(
        request, settings=settings, raw_store=raw_store, registry=registry, now=now,
    )

    conn = connect_store(settings.store_db_path)
    try:
        result = store_get_result(conn, result_id)
    finally:
        conn.close()
    assert result is not None

    win_a = next(w for w in result["windows"] if w["window_id"] == "win-a")
    space_weather = next(m for m in win_a["mechanisms"] if m["mechanism"] == "space_weather")
    assert space_weather["status"] == "source_error"
    assert space_weather["max_level"] is None
    assert space_weather["record_ids"] == []


def test_unreadable_source_config_yields_source_error_not_a_guessed_ok(
    _env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """round 1 ревью PR #29 (🚨 src/api/service.py): раньше при ошибке чтения
    ``sources.yaml`` расчёт молча считал на запасных константах
    (``hold_seconds=300``, ``critical_staleness_seconds=3600``) — со
    сплошь покрытым часом в хранилище это давало ``status="ok"``, хотя сама
    конфигурация источника была недоступна. Сейчас — явный ``source_error``,
    без какого-либо расчёта на угаданных числах."""
    settings, raw_store = _env
    registry = SourceStatusRegistry()
    window_start = datetime(2024, 5, 10, 10, 0, tzinfo=UTC)
    # Ровно те же насеянные плотные наблюдения, что дают "ok" в
    # test_seeded_full_hour_of_observations_yields_ok_status_via_production_path
    # — разница только в том, что конфигурация источника недоступна.
    _seed_dense_hour_of_observations(
        settings=settings, raw_store=raw_store, start_at=window_start, spike_minute=15
    )

    def broken_config(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("simulated sources.yaml read failure")

    monkeypatch.setattr(swpc_source, "load_source_config", broken_config)

    request = CalculationRequest(
        mode="current", start_at=window_start, duration_hours=1, search_window_hours=4,
    )
    result_id = _run_calculation(
        request,
        settings=settings,
        raw_store=raw_store,
        registry=registry,
        now=window_start + timedelta(hours=10),
    )

    conn = connect_store(settings.store_db_path)
    try:
        result = store_get_result(conn, result_id)
    finally:
        conn.close()
    assert result is not None

    win_a = next(w for w in result["windows"] if w["window_id"] == "win-a")
    space_weather = next(m for m in win_a["mechanisms"] if m["mechanism"] == "space_weather")
    assert space_weather["status"] == "source_error"
    assert space_weather["max_level"] is None
    assert space_weather["exceedance_hours_by_level"] is None
    assert space_weather["record_ids"] == []
    assert any("конфигурация" in note for note in space_weather["notes"])

    # Плотные наблюдения при этом реально были насеяны — окно не пустое,
    # просто не может быть классифицировано без известной конфигурации.
    observed_manifest = [
        m for m in result["data_manifest"] if m["record_kind"] == "observation"
    ]
    assert observed_manifest == []


def test_conflicting_satellite_records_yield_source_error_not_missing_data(
    _env: _Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """round 1 ревью PR #29 (⚠️ src/api/service.py): ошибка обработки
    сохранённых наблюдений (здесь — ``ConflictingObservationsError`` из двух
    разных provider-записей на один ``observed_at``, main-prompt.md §2)
    обязана остаться видимой как ``source_error``, а не тихо схлопнуться в
    обычный ``missing_data`` (который выглядел бы как «данных для окна и
    правда нет», хотя причина — повреждённая/неразрешимая выборка)."""
    settings, raw_store = _env
    registry = SourceStatusRegistry()
    window_start = datetime(2024, 5, 10, 10, 0, tzinfo=UTC)

    conn = connect_store(settings.store_db_path)
    conflicting_record_ids: list[str] = []
    try:
        for satellite in ("18", "99"):
            sample = swpc_source.SwpcSample(
                satellite=satellite,
                observed_at=window_start,
                value=15.0,
                degraded=False,
                raw_entry={
                    "time_tag": window_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "satellite": int(satellite),
                    "flux": 15.0,
                    "energy": ">=10 MeV",
                    "yaw_flip": 0,
                },
            )
            record_input = swpc_source.to_record_input(
                sample,
                source_url="https://services.swpc.noaa.gov/json/goes/primary/"
                "integral-protons-1-day.json",
                fetched_at=window_start,
            )
            conflicting_record_ids.append(insert_record(conn, raw_store, record_input))
    finally:
        conn.close()

    def swpc_fetch(url: str, **_kwargs: Any) -> HttpFetchResult:
        return HttpFetchResult(
            status_code=200, body=swpc_sample_bytes(), url=url, elapsed_seconds=0.001
        )

    monkeypatch.setattr(swpc_source, "fetch", swpc_fetch)

    request = CalculationRequest(
        mode="current", start_at=window_start, duration_hours=1, search_window_hours=4,
    )
    result_id = _run_calculation(
        request,
        settings=settings,
        raw_store=raw_store,
        registry=registry,
        now=window_start + timedelta(hours=10),
    )

    conn = connect_store(settings.store_db_path)
    try:
        result = store_get_result(conn, result_id)
    finally:
        conn.close()
    assert result is not None

    win_a = next(w for w in result["windows"] if w["window_id"] == "win-a")
    space_weather = next(m for m in win_a["mechanisms"] if m["mechanism"] == "space_weather")
    assert space_weather["status"] == "source_error"
    assert space_weather["max_level"] is None

    # round 2 ревью PR #29 (⚠️ «id конфликтующих наблюдений скрыты»): сама
    # ошибка обработки и id конкретных конфликтующих записей должны быть
    # прослеживаемы — не только в логе, но и в warnings/mechanism/manifest
    # результата.
    assert set(conflicting_record_ids) <= set(space_weather["record_ids"])
    processing_warning = next(
        w for w in result["warnings"]
        if w["code"] == "space-weather-observation-processing-error"
    )
    assert processing_warning["window_id"] == "win-a"
    assert set(conflicting_record_ids) <= set(processing_warning["record_ids"])

    # round 4 ревью PR #29 (⚠️ «событие с этим fetch_attempt_id не
    # записывается в лог»): предупреждение обязано доказуемо вести к своей
    # же строке структурного лога, не к несуществующей попытке.
    attempt_id = processing_warning["fetch_attempt_id"]
    assert attempt_id
    logs = capsys.readouterr().err
    processing_log_lines = [
        line for line in logs.splitlines() if "swpc_observation_processing_failed" in line
    ]
    assert any(
        attempt_id in line and "win-a" in line for line in processing_log_lines
    ), processing_log_lines

    observed_manifest_ids = {
        m["record_id"] for m in result["data_manifest"] if m["record_kind"] == "observation"
    }
    assert set(conflicting_record_ids) <= observed_manifest_ids


def test_selection_failure_warning_is_traceable_to_its_own_log_entry(
    _env: _Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """round 3 ревью PR #29 (⚠️ src/api/service.py:951): предупреждение об
    ошибке ``select_observed_range`` раньше ссылалось на ``swpc_attempt_id``
    — идентификатор попытки СЕТЕВОГО получения потока протонов, которая к
    этому моменту вполне могла уже успешно завершиться (как здесь: получение
    намеренно замокано успешным). Единственное доказательство предупреждения
    указывало бы на постороннее, успешное событие. Теперь у попытки
    выборки/обработки свой отдельный идентификатор, залогированный вместе с
    ``window_id`` — warning доказуемо ведёт именно к своей записи лога
    (contracts/README.md «каждое предупреждение доказуемо»), не к чужой."""
    settings, raw_store = _env
    registry = SourceStatusRegistry()

    def swpc_fetch(url: str, **_kwargs: Any) -> HttpFetchResult:
        return HttpFetchResult(
            status_code=200, body=swpc_sample_bytes(), url=url, elapsed_seconds=0.001
        )

    monkeypatch.setattr(swpc_source, "fetch", swpc_fetch)  # получение — успешно

    def failing_select(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        raise RuntimeError("simulated storage failure")

    monkeypatch.setattr(service_module, "select_observed_range", failing_select)

    now = datetime(2024, 5, 10, 14, 0, tzinfo=UTC)
    request = CalculationRequest(
        mode="current",
        start_at=now - timedelta(hours=2),
        duration_hours=1,
        search_window_hours=4,
    )
    result_id = _run_calculation(
        request, settings=settings, raw_store=raw_store, registry=registry, now=now,
    )

    conn = connect_store(settings.store_db_path)
    try:
        result = store_get_result(conn, result_id)
    finally:
        conn.close()
    assert result is not None

    # Получение потока протонов реально успешно — swpc_attempt_id из него
    # НЕ должен быть тем, на что ссылается предупреждение об ошибке выборки.
    swpc_status = next(
        s for s in result["source_status"] if s["source_id"] == swpc_source.SOURCE_ID
    )
    assert swpc_status["last_success_at"] is not None

    win_a = next(w for w in result["windows"] if w["window_id"] == "win-a")
    space_weather = next(m for m in win_a["mechanisms"] if m["mechanism"] == "space_weather")
    assert space_weather["status"] == "source_error"

    processing_warning = next(
        w for w in result["warnings"]
        if w["code"] == "space-weather-observation-processing-error" and w["window_id"] == "win-a"
    )
    attempt_id = processing_warning["fetch_attempt_id"]
    assert attempt_id

    logs = capsys.readouterr().err
    assert "swpc_observation_selection_failed" in logs
    # Идентификатор и window_id обязаны стоять в ОДНОЙ и той же строке лога
    # (одно событие), не просто где-то в общем выводе.
    selection_failure_lines = [
        line for line in logs.splitlines() if "swpc_observation_selection_failed" in line
    ]
    assert any(
        attempt_id in line and "win-a" in line for line in selection_failure_lines
    ), selection_failure_lines
