"""Тесты траектории МКС (S1-06): распространение орбиты и получение элементов.

Обязательный тест точности (.ai/main-prompt.md §9, п.5; .ai/backend-prompt.md
§8): положение станции сверяется с независимым, ранее опубликованным
эталоном (Vallado/Kelso SGP4 verification, см.
``tests/fixtures/orbit/README.md``), а не с повторным расчётом той же
формулой (main-prompt §9: "тест, повторяющий собственную реализацию,
доказательством не является").

Сеть в этих тестах не используется (.ai/backend-prompt.md §8): HTTP-вызовы
CelesTrak подменяются ``httpx.MockTransport``, парсер и распространение
работают на зафиксированных фикстурах.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from src.domain.orbit import propagate as domain
from src.sources import orbit as sources_orbit
from src.store import RawOriginalStore, connect, get_record, insert_record

UTC = timezone.utc
FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "orbit"

# Оправданный допуск (main-prompt §9, п.5 «обоснованный допуск»): реальные
# расхождения между этой реализацией и опубликованным эталоном — порядка
# 1e-8 км (см. обоснование tolerance в docstring теста ниже). 1 метр по
# положению и 1 мм/с по скорости — заведомо шире реальной ошибки, но
# достаточно узки, чтобы поймать перепутанные единицы, оси или системы
# координат.
POSITION_TOLERANCE_KM = 1e-3
VELOCITY_TOLERANCE_KM_S = 1e-6


# ---------------------------------------------------------------------------
# domain/orbit/propagate.py — точность распространения (независимый эталон)
# ---------------------------------------------------------------------------


def _load_vallado_case() -> tuple[domain.OrbitalElements, dict]:
    tle_lines = (FIXTURES_DIR / "vallado_sgp4_verification_sat5.tle").read_text().splitlines()
    line1, line2 = tle_lines[0], tle_lines[1]
    expected = json.loads(
        (FIXTURES_DIR / "vallado_sgp4_verification_sat5_expected.json").read_text()
    )
    elements = domain.load_elements(line1, line2, norad_id=expected["norad_id"])
    return elements, expected


def test_propagate_matches_independently_published_reference() -> None:
    """Положение/скорость совпадают с официальным эталоном SGP4 verification
    (Vallado et al., см. tests/fixtures/orbit/README.md) в одной системе
    координат (TEME) и одних единицах (км, км/с) — не с расчётом,
    повторяющим собственную реализацию.
    """
    elements, expected = _load_vallado_case()
    times = [
        elements.epoch + timedelta(minutes=case["tsince_minutes"]) for case in expected["cases"]
    ]

    states = domain.propagate(elements, times)

    assert len(states) == len(expected["cases"])
    for state, case in zip(states, expected["cases"], strict=True):
        assert state.coordinate_system == expected["coordinate_system"]
        for actual, ref in zip(state.position_km, case["position_km"], strict=True):
            assert actual == pytest.approx(ref, abs=POSITION_TOLERANCE_KM)
        for actual, ref in zip(state.velocity_km_s, case["velocity_km_s"], strict=True):
            assert actual == pytest.approx(ref, abs=VELOCITY_TOLERANCE_KM_S)


def test_load_elements_epoch_matches_independently_parsed_epoch() -> None:
    """Эпоха, которую domain извлекает через SGP4, совпадает с эпохой,
    которую sources извлекает вручную по колонкам TLE (два независимых
    способа разбора одного и того же поля — main-prompt §9, п.5).
    """
    tle_path = FIXTURES_DIR / "celestrak_iss_gp_sample.tle"
    _, line1, line2 = tle_path.read_text().splitlines()

    domain_elements = domain.load_elements(line1, line2, norad_id="25544")
    source_epoch = sources_orbit._tle_epoch(line1)  # noqa: SLF001 — независимая перепроверка

    assert domain_elements.epoch == source_epoch
    # Значение подтверждено независимо: sgp4/tests.py::test_december_32
    # (python-sgp4, MIT) явно утверждает эту же эпоху для тех же строк.
    assert domain_elements.epoch == datetime(2020, 1, 1, 19, 42, 47, 134368, tzinfo=UTC)


def test_propagate_rejects_naive_datetime() -> None:
    elements, _ = _load_vallado_case()
    naive = datetime(2000, 6, 27, 18, 50, 19)  # без tzinfo
    with pytest.raises(ValueError, match="timezone-aware"):
        domain.propagate(elements, [naive])


def test_propagate_surfaces_sgp4_error_as_status_not_silence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SGP4 может вернуть код ошибки (например для распавшегося объекта);
    он обязан дойти до вызывающей стороны как статус, а не быть проглочен
    (.ai/backend-prompt.md §5 «не глотать исключения»).
    """

    class _FailingSatrec:
        @classmethod
        def twoline2rv(cls, line1: str, line2: str) -> _FailingSatrec:
            return cls()

        def sgp4(self, jd: float, fr: float) -> tuple[int, list[float], list[float]]:
            return 6, [0.0, 0.0, 0.0], [0.0, 0.0, 0.0]

    monkeypatch.setattr(domain, "Satrec", _FailingSatrec)
    elements = domain.OrbitalElements(
        line1="irrelevant",
        line2="irrelevant",
        epoch=datetime(2024, 1, 1, tzinfo=UTC),
        norad_id="00000",
    )
    with pytest.raises(domain.PropagationError, match="error code 6"):
        domain.propagate(elements, [datetime(2024, 1, 1, tzinfo=UTC)])


# ---------------------------------------------------------------------------
# domain/orbit/propagate.py — конфигурируемая сетка и давность/реконструкция
# ---------------------------------------------------------------------------


def test_time_grid_is_configurable_and_respects_step() -> None:
    start = datetime(2024, 5, 10, 0, 0, tzinfo=UTC)
    grid = domain.time_grid(start, hours=2, step_minutes=30)
    assert grid == [
        start,
        start + timedelta(minutes=30),
        start + timedelta(minutes=60),
        start + timedelta(minutes=90),
        start + timedelta(minutes=120),
    ]


def test_time_grid_default_horizon_is_32_hours() -> None:
    start = datetime(2024, 5, 10, 0, 0, tzinfo=UTC)
    domain.time_grid(start, hours=32, step_minutes=60)  # не должно поднимать исключение
    with pytest.raises(ValueError, match="exceeds max_hours"):
        domain.time_grid(start, hours=32.01, step_minutes=60)


def test_time_grid_max_hours_is_a_caller_parameter_not_a_constant() -> None:
    """Горизонт — параметр конфигурации вызывающей стороны, не константа
    модуля (.ai/main-prompt.md §6): другой вызывающий код может передать
    иной предел.
    """
    start = datetime(2024, 5, 10, 0, 0, tzinfo=UTC)
    with pytest.raises(ValueError, match="exceeds max_hours"):
        domain.time_grid(start, hours=10, step_minutes=60, max_hours=8)
    domain.time_grid(start, hours=10, step_minutes=60, max_hours=48)


def test_time_grid_requires_aware_start_and_positive_step() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        domain.time_grid(datetime(2024, 5, 10), hours=1, step_minutes=10)
    with pytest.raises(ValueError, match="step_minutes"):
        domain.time_grid(datetime(2024, 5, 10, tzinfo=UTC), hours=1, step_minutes=0)


def test_time_grid_always_covers_the_full_window_when_step_does_not_divide_it() -> None:
    """round 1 ревью PR #17: hours=1, step_minutes=40 раньше давал только
    [00:00, 00:40] — последние 20 минут окна оставались без расчёта, что
    могло скрыть максимальный уровень воздействия ближе к концу ВКД.
    """
    start = datetime(2024, 5, 10, 0, 0, tzinfo=UTC)
    grid = domain.time_grid(start, hours=1, step_minutes=40)
    assert grid == [start, start + timedelta(minutes=40), start + timedelta(hours=1)]
    assert grid[-1] == start + timedelta(hours=1)


def test_time_grid_covers_window_when_step_is_longer_than_the_window() -> None:
    start = datetime(2024, 5, 10, 0, 0, tzinfo=UTC)
    grid = domain.time_grid(start, hours=1, step_minutes=120)
    assert grid == [start, start + timedelta(hours=1)]


def test_elements_age_hours_and_reconstruction_flag() -> None:
    epoch = datetime(2024, 5, 10, 0, 0, tzinfo=UTC)
    fresh_as_of = epoch + timedelta(hours=2)
    stale_as_of = epoch + timedelta(hours=100)

    assert domain.elements_age_hours(epoch, fresh_as_of) == pytest.approx(2.0)
    assert domain.elements_age_hours(epoch, stale_as_of) == pytest.approx(100.0)

    # Элементы на момент, близкий к эпохе, считаются подтверждёнными.
    assert not domain.is_reconstructed_geometry(epoch, fresh_as_of, max_confirmed_age_hours=24.0)
    # Слишком давние для запрошенного as_of — геометрия помечается реконструкцией
    # (.ai/main-prompt.md §11 «Траектория»), а не выдаётся как подтверждённая.
    assert domain.is_reconstructed_geometry(epoch, stale_as_of, max_confirmed_age_hours=24.0)


# ---------------------------------------------------------------------------
# sources/orbit.py — разбор реального ответа
# ---------------------------------------------------------------------------


def test_parse_tle_response_accepts_real_recorded_response() -> None:
    raw_bytes = (FIXTURES_DIR / "celestrak_iss_gp_sample.tle").read_bytes()

    parsed = sources_orbit.parse_tle_response(raw_bytes)

    assert parsed.norad_id == "25544"
    assert parsed.object_name == "ISS (ZARYA)"
    assert parsed.epoch == datetime(2020, 1, 1, 19, 42, 47, 134368, tzinfo=UTC)


def test_parse_tle_response_rejects_corrupted_checksum() -> None:
    raw_bytes = (FIXTURES_DIR / "celestrak_iss_gp_sample.tle").read_bytes()
    corrupted = raw_bytes.replace(b"9129", b"9120")  # портит контрольную сумму строки 1

    with pytest.raises(sources_orbit.CorruptedElementsError, match="checksum mismatch"):
        sources_orbit.parse_tle_response(corrupted)


def test_parse_tle_response_rejects_unexpected_norad_id() -> None:
    raw_bytes = (FIXTURES_DIR / "celestrak_iss_gp_sample.tle").read_bytes()

    with pytest.raises(sources_orbit.CorruptedElementsError, match="does not match expected"):
        sources_orbit.parse_tle_response(raw_bytes, expected_norad_id="00005")


def test_parse_tle_response_rejects_wrong_line_count() -> None:
    with pytest.raises(sources_orbit.CorruptedElementsError, match="2 or 3 non-empty lines"):
        sources_orbit.parse_tle_response(b"just one line\n")


def test_parse_tle_response_rejects_mismatched_line1_line2_norad_id() -> None:
    """round 1 ревью PR #17: строка 1 МКС (checksum валиден) + строка 2
    другого спутника (тоже checksum-валидна, тот же satellite 5, что и в
    vallado_sgp4_verification_sat5.tle) раньше молча принималась бы как МКС,
    хотя орбита определяется параметрами строки 2, а не строки 1.
    """
    iss_line1 = (FIXTURES_DIR / "celestrak_iss_gp_sample.tle").read_text().splitlines()[1]
    other_line2 = (
        FIXTURES_DIR / "vallado_sgp4_verification_sat5.tle"
    ).read_text().splitlines()[1]
    mixed = f"{iss_line1}\n{other_line2}\n".encode()

    with pytest.raises(sources_orbit.CorruptedElementsError, match="NORAD ids disagree"):
        sources_orbit.parse_tle_response(mixed)


# ---------------------------------------------------------------------------
# sources/orbit.py — сетевой слой (без живой сети, httpx.MockTransport)
# ---------------------------------------------------------------------------


def _client_with_transport(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_fetch_current_tle_returns_body_on_200() -> None:
    body = (FIXTURES_DIR / "celestrak_iss_gp_sample.tle").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        assert "CATNR=25544" in str(request.url)
        return httpx.Response(200, content=body)

    client = _client_with_transport(handler)
    try:
        result = sources_orbit.fetch_current_tle(client=client)
    finally:
        client.close()

    assert result == body


def test_fetch_current_tle_raises_quota_error_on_429() -> None:
    client = _client_with_transport(lambda request: httpx.Response(429, content=b""))
    try:
        with pytest.raises(sources_orbit.OrbitSourceQuotaError):
            sources_orbit.fetch_current_tle(client=client)
    finally:
        client.close()


def test_fetch_current_tle_raises_source_error_on_non_200() -> None:
    client = _client_with_transport(lambda request: httpx.Response(503, content=b""))
    try:
        with pytest.raises(sources_orbit.OrbitSourceError):
            sources_orbit.fetch_current_tle(client=client)
    finally:
        client.close()


def test_fetch_current_tle_raises_source_error_on_empty_200_body() -> None:
    """Код 200 с пустым телом — ошибка источника, а не набор нулей
    (.ai/backend-prompt.md §3)."""
    client = _client_with_transport(lambda request: httpx.Response(200, content=b"   "))
    try:
        with pytest.raises(sources_orbit.OrbitSourceError, match="empty body"):
            sources_orbit.fetch_current_tle(client=client)
    finally:
        client.close()


def test_fetch_current_tle_raises_source_error_on_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    client = _client_with_transport(handler)
    try:
        with pytest.raises(sources_orbit.OrbitSourceError, match="timed out"):
            sources_orbit.fetch_current_tle(client=client)
    finally:
        client.close()


# ---------------------------------------------------------------------------
# Исторический режим: честный отказ, а не подстановка текущих элементов
# ---------------------------------------------------------------------------


def test_require_supported_mode_allows_current() -> None:
    sources_orbit.require_supported_mode("current")  # не поднимает исключение


@pytest.mark.parametrize("mode", ["historical_analysis", "historical_forecast"])
def test_require_supported_mode_rejects_historical_modes(mode: str) -> None:
    with pytest.raises(sources_orbit.HistoricalElementsUnsupportedError):
        sources_orbit.require_supported_mode(mode)


@pytest.mark.parametrize("mode", ["historical_analysis", "historical_forecast"])
def test_fetch_elements_for_request_rejects_historical_without_network_call(mode: str) -> None:
    """Исторический запрос не берёт современные TLE: отказ происходит до
    любого сетевого вызова, а не после тихой подстановки CelesTrak
    (.ai/main-prompt.md §11 «Траектория»).
    """

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        pytest.fail("historical request must not reach the network")

    client = _client_with_transport(handler)
    try:
        with pytest.raises(sources_orbit.HistoricalElementsUnsupportedError):
            sources_orbit.fetch_elements_for_request(mode, client=client)  # type: ignore[arg-type]
    finally:
        client.close()


def test_fetch_elements_for_request_current_mode_returns_parsed_elements() -> None:
    body = (FIXTURES_DIR / "celestrak_iss_gp_sample.tle").read_bytes()
    client = _client_with_transport(lambda request: httpx.Response(200, content=body))
    try:
        parsed, raw_bytes, url = sources_orbit.fetch_elements_for_request("current", client=client)
    finally:
        client.close()

    assert parsed.norad_id == "25544"
    assert raw_bytes == body
    assert "25544" in url


# ---------------------------------------------------------------------------
# Интеграция с хранилищем (FN-21): запись, эпоха и давность прослеживаются
# ---------------------------------------------------------------------------


@pytest.fixture
def db_conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = connect(tmp_path / "store.sqlite3")
    yield conn
    conn.close()


@pytest.fixture
def raw_store(tmp_path: Path) -> RawOriginalStore:
    return RawOriginalStore(tmp_path / "raw")


def test_build_orbital_elements_record_is_never_replay_eligible(
    db_conn: sqlite3.Connection, raw_store: RawOriginalStore
) -> None:
    """CelesTrak не сообщает published_at (sources.yaml) => хранилище
    (src/store/records.py) всегда вычисляет replay_eligible=false для этих
    записей — современные элементы не могут быть тихо использованы для
    строгого historical_forecast (.ai/main-prompt.md §1).
    """
    raw_bytes = (FIXTURES_DIR / "celestrak_iss_gp_sample.tle").read_bytes()
    parsed = sources_orbit.parse_tle_response(raw_bytes)
    record_input = sources_orbit.build_orbital_elements_record(
        parsed,
        raw_bytes=raw_bytes,
        fetched_at=datetime(2024, 5, 10, 12, 0, tzinfo=UTC),
        source_url="https://celestrak.org/NORAD/elements/gp.php?CATNR=25544&FORMAT=TLE",
    )

    record_id = insert_record(db_conn, raw_store, record_input)
    stored = get_record(db_conn, record_id)

    assert stored is not None
    assert stored["record_kind"] == "orbital_elements"
    assert stored["published_at"] is None
    assert stored["replay_eligible"] is False
    assert stored["orbital_elements_meta"]["format"] == "TLE"
    assert stored["orbital_elements_meta"]["coordinate_system"] == "TEME"
    assert stored["orbital_elements_meta"]["epoch"] == "2020-01-01T19:42:47.134368Z"
    # Эпоха и давность прослеживаются от сохранённой записи (main-prompt §11
    # «видны источник, эпоха элементов и их давность»): davnost — разница
    # между произвольным моментом расчёта и эпохой из этой же записи.
    at = datetime(2024, 5, 10, 12, 0, tzinfo=UTC)
    age_hours = domain.elements_age_hours(parsed.epoch, at)
    assert age_hours > 0


def test_refined_elements_at_the_same_epoch_get_a_new_version_not_a_conflict(
    db_conn: sqlite3.Connection, raw_store: RawOriginalStore
) -> None:
    """round 1 ревью PR #17: source_version раньше был равен только эпохе
    TLE. Уточнённый поставщиком набор элементов с той же эпохой получал бы
    тот же дедуп-ключ (source_id, provider_record_id, source_version), что и
    прежняя запись, но другое содержимое — src/store/records.py отклонил бы
    его как DuplicateKeyConflictError вместо того, чтобы сохранить рядом со
    старой версией (.ai/backend-prompt.md §1 «поздние уточнения хранятся
    рядом с прежними версиями»).
    """
    raw_bytes = (FIXTURES_DIR / "celestrak_iss_gp_sample.tle").read_bytes()
    parsed = sources_orbit.parse_tle_response(raw_bytes)

    # Меняем эксцентриситет (значимое поле орбиты), сохраняя эпоху и длину
    # строки, и пересчитываем контрольную сумму — получаем валидный, но
    # содержательно другой набор элементов той же эпохи.
    refined_body = parsed.line2[:-1].replace("0005156", "0009999")
    assert refined_body != parsed.line2[:-1]
    refined_checksum = sources_orbit._tle_checksum(refined_body + "0")  # noqa: SLF001
    refined_line2 = refined_body + str(refined_checksum)
    assert len(refined_line2) == len(parsed.line2)

    refined_parsed = sources_orbit.ParsedTle(
        object_name=parsed.object_name,
        norad_id=parsed.norad_id,
        line1=parsed.line1,
        line2=refined_line2,
        epoch=parsed.epoch,  # та же эпоха, что и у исходного набора
    )
    refined_raw_bytes = f"{parsed.object_name}\n{parsed.line1}\n{refined_line2}\n".encode()

    fetched_at = datetime(2024, 5, 10, 12, 0, tzinfo=UTC)
    source_url = "https://celestrak.org/NORAD/elements/gp.php?CATNR=25544&FORMAT=TLE"
    original_record = sources_orbit.build_orbital_elements_record(
        parsed, raw_bytes=raw_bytes, fetched_at=fetched_at, source_url=source_url
    )
    refined_record = sources_orbit.build_orbital_elements_record(
        refined_parsed, raw_bytes=refined_raw_bytes, fetched_at=fetched_at, source_url=source_url
    )

    assert original_record.source_version != refined_record.source_version

    original_id = insert_record(db_conn, raw_store, original_record)
    # Не должно поднимать DuplicateKeyConflictError: разное содержимое той же
    # эпохи обязано получить собственный record_id рядом со старым.
    refined_id = insert_record(db_conn, raw_store, refined_record)

    assert original_id != refined_id
    assert get_record(db_conn, original_id)["value"] != get_record(db_conn, refined_id)["value"]
