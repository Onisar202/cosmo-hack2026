"""Тесты исторических орбитальных элементов МКС — NASA TOPO CCSDS OEM (FN-33, S2-03).

Полностью детерминированы и без сети (.ai/backend-prompt.md §8): парсер OEM
и S3-листинга, отбор выпуска и интерполяция гоняются на 4 реальных
сохранённых выпусках из ``tests/fixtures/orbit/history/`` (см. её README о
происхождении и уже задокументированных вручную фактах — здесь эти факты
становятся исполняемыми проверками).

Обязательный тест точности интерполяции (.ai/main-prompt.md §9, п.5;
.ai/backend-prompt.md §8) сверяет интерполянт с held-out реальным вектором,
который НЕ передан интерполятору как узел — не с повторным расчётом той же
формулой.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.domain.orbit.interpolate import (
    InterpolationError,
    OemNode,
    interpolate_state,
)
from src.sources import orbit as sources_orbit
from src.sources import orbit_history
from src.store import RawOriginalStore, connect, get_record, insert_record

UTC = timezone.utc
HISTORY_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "orbit" / "history"


# ---------------------------------------------------------------------------
# Загрузка реальных фикстур (без сети) — общий помощник для всех тестов ниже
# ---------------------------------------------------------------------------


def _load_release(release_date: str) -> tuple[orbit_history.OemRelease, bytes]:
    txt_path = HISTORY_DIR / f"nasa_iss_oem_{release_date}.txt"
    listing_path = HISTORY_DIR / f"nasa_iss_oem_listing_{release_date}.xml"
    raw_bytes = txt_path.read_bytes()
    parsed = orbit_history.parse_oem(raw_bytes)
    listing_entries = orbit_history.parse_s3_listing(listing_path.read_bytes())
    release = orbit_history.build_oem_release(
        parsed,
        listing_entries=listing_entries,
        release_date=release_date,
        source_url=orbit_history.OEM_URL_TEMPLATE.format(release_date=release_date),
    )
    return release, raw_bytes


def _load_meta_provenance(release_date: str) -> orbit_history.MetaJsonProvenance:
    meta = json.loads((HISTORY_DIR / f"nasa_iss_oem_{release_date}.meta.json").read_text())
    headers = meta["response_headers"]
    return orbit_history.MetaJsonProvenance(
        last_modified_header=headers["last-modified"],
        etag_header=headers["etag"],
        body_sha256=meta["body_sha256"],
    )


RELEASE_DATES = ["2024-05-08", "2024-05-12", "2024-06-14", "2024-06-18"]


@pytest.fixture(scope="module")
def all_releases() -> dict[str, tuple[orbit_history.OemRelease, bytes]]:
    return {date: _load_release(date) for date in RELEASE_DATES}


# ---------------------------------------------------------------------------
# Парсер: 4 реальных выпуска (main-prompt §9 п.5 стиль — числа сверены с
# литеральными значениями, которые можно прочитать в самих фикстурах)
# ---------------------------------------------------------------------------


def test_parse_oem_reads_meta_fields_for_all_four_real_releases(
    all_releases: dict[str, tuple[orbit_history.OemRelease, bytes]],
) -> None:
    expected = {
        "2024-05-08": {
            "creation_date": datetime(2024, 5, 8, 16, 49, 32, 385000, tzinfo=UTC),
            "useable_start": datetime(2024, 5, 8, 12, 0, tzinfo=UTC),
            "useable_stop": datetime(2024, 5, 23, 12, 0, tzinfo=UTC),
            "vector_count": 5403,
        },
        "2024-05-12": {
            "creation_date": datetime(2024, 5, 10, 18, 51, 53, 202000, tzinfo=UTC),
            "useable_start": datetime(2024, 5, 10, 12, 0, tzinfo=UTC),
            "useable_stop": datetime(2024, 5, 25, 12, 0, tzinfo=UTC),
            "vector_count": 6565,
        },
        "2024-06-14": {
            "creation_date": datetime(2024, 6, 14, 19, 20, 58, 186000, tzinfo=UTC),
            "useable_start": datetime(2024, 6, 14, 12, 0, tzinfo=UTC),
            "useable_stop": datetime(2024, 6, 29, 12, 0, tzinfo=UTC),
            "vector_count": 6093,
        },
        "2024-06-18": {
            "creation_date": datetime(2024, 6, 18, 3, 23, 22, 72000, tzinfo=UTC),
            "useable_start": datetime(2024, 6, 17, 12, 0, tzinfo=UTC),
            "useable_stop": datetime(2024, 7, 2, 12, 0, tzinfo=UTC),
            "vector_count": 5629,
        },
    }
    for release_date, exp in expected.items():
        release, _ = all_releases[release_date]
        parsed = release.parsed
        assert parsed.object_name == "ISS"
        assert parsed.object_id == "1998-067-A"
        assert parsed.norad_id == "25544"
        assert parsed.center_name == "Earth"
        assert parsed.ref_frame == "EME2000"
        assert parsed.time_system == "UTC"
        assert parsed.originator == "NASA/JSC/FOD/TOPO"
        assert parsed.creation_date == exp["creation_date"]
        assert parsed.useable_start_time == exp["useable_start"]
        assert parsed.useable_stop_time == exp["useable_stop"]
        assert len(parsed.state_vectors) == exp["vector_count"]


def test_parse_oem_first_and_last_vector_match_literal_fixture_values() -> None:
    """Значения сверены буквально с первой/последней строкой векторов
    состояния каждого файла (main-prompt §9 п.5)."""
    release, _ = _load_release("2024-05-08")
    vectors = release.parsed.state_vectors

    first = vectors[0]
    assert first.time == datetime(2024, 5, 8, 12, 0, tzinfo=UTC)
    assert first.position_km == pytest.approx(
        (-3795.621020729360, 4756.193798233340, -3029.799396350430)
    )
    assert first.velocity_km_s == pytest.approx(
        (-5.67629881414535, -1.38306343695366, 4.94745274531810)
    )

    last = vectors[-1]
    assert last.time == datetime(2024, 5, 23, 12, 0, tzinfo=UTC)
    assert last.position_km == pytest.approx(
        (4027.077214414510, -1243.657353271850, -5328.994527204180)
    )
    assert last.velocity_km_s == pytest.approx(
        (1.89493730175473, 7.40997478327160, -0.29999927731357)
    )


def test_parse_oem_skips_comment_lines_including_maneuver_table() -> None:
    """COMMENT-блок (масса/сопротивление, узлы, таблица манёвров) не
    попадает в state_vectors — main-prompt.md §11 «не парсить их содержание»."""
    release, _ = _load_release("2024-06-14")
    for vector in release.parsed.state_vectors[:5]:
        assert vector.time.year == 2024  # ни один COMMENT не прошёл как вектор


def test_parse_oem_rejects_empty_body() -> None:
    with pytest.raises(orbit_history.OemFormatError, match="empty"):
        orbit_history.parse_oem(b"   ")


def test_parse_oem_rejects_missing_meta_stop() -> None:
    body = (
        b"CCSDS_OEM_VERS = 2.0\nCREATION_DATE = 2024-05-08T00:00:00\nORIGINATOR = X\n\n"
        b"META_START\nOBJECT_NAME = ISS\n"
    )
    with pytest.raises(orbit_history.OemFormatError, match="META_STOP"):
        orbit_history.parse_oem(body)


def test_parse_oem_rejects_wrong_object_id() -> None:
    raw_bytes = (HISTORY_DIR / "nasa_iss_oem_2024-05-08.txt").read_bytes()
    corrupted = raw_bytes.replace(b"1998-067-A", b"1998-067-B")
    with pytest.raises(orbit_history.OemFormatError, match="OBJECT_ID"):
        orbit_history.parse_oem(corrupted)


def test_parse_oem_rejects_malformed_state_vector_line() -> None:
    raw_bytes = (HISTORY_DIR / "nasa_iss_oem_2024-05-08.txt").read_bytes()
    corrupted = raw_bytes.replace(
        b"2024-05-08T12:00:00.000 -3795.621020729360 4756.193798233340 -3029.799396350430 "
        b"-5.67629881414535 -1.38306343695366 4.94745274531810",
        b"2024-05-08T12:00:00.000 not-a-number 4756.193798233340",
    )
    with pytest.raises(orbit_history.OemFormatError):
        orbit_history.parse_oem(corrupted)


# ---------------------------------------------------------------------------
# S3-листинг + перекрёстная проверка целостности (meta.json vs листинг vs байты)
# ---------------------------------------------------------------------------


def test_parse_s3_listing_extracts_txt_entry_with_last_modified_and_etag() -> None:
    entries = orbit_history.parse_s3_listing(
        (HISTORY_DIR / "nasa_iss_oem_listing_2024-05-12.xml").read_bytes()
    )
    txt = [e for e in entries if e.key.endswith(".txt")][0]
    assert txt.key == "iss-coords/2024-05-12/ISS_OEM/ISS.OEM_J2K_EPH.txt"
    assert txt.last_modified == datetime(2024, 5, 13, 3, 6, 46, tzinfo=UTC)
    assert txt.etag == "f27a98420ac59ac94d0925c123f03c79"
    assert txt.size == 872949


def test_build_oem_release_published_at_is_s3_last_modified_not_creation_date() -> None:
    """Главный факт задачи: published_at != CREATION_DATE. Выпуск 2024-05-12
    (README фикстур): CREATION_DATE=2024-05-10T18:51:53Z, но
    LastModified=2024-05-13T03:06:46Z — почти 2.5 суток разницы."""
    release, _ = _load_release("2024-05-12")
    assert release.parsed.creation_date == datetime(2024, 5, 10, 18, 51, 53, 202000, tzinfo=UTC)
    assert release.published_at == datetime(2024, 5, 13, 3, 6, 46, tzinfo=UTC)
    assert release.published_at != release.parsed.creation_date
    assert release.published_at > release.parsed.creation_date


@pytest.mark.parametrize("release_date", RELEASE_DATES)
def test_verify_fetch_integrity_passes_for_real_fixtures(release_date: str) -> None:
    """Кросс-проверка meta.json (заголовки исходного HTTP-ответа) против
    S3-листинга выпуска и фактических байтов — README фикстур документирует
    это как проверенное вручную при подготовке; здесь это исполняемый тест."""
    release, raw_bytes = _load_release(release_date)
    meta = _load_meta_provenance(release_date)
    listing_entries = orbit_history.parse_s3_listing(
        (HISTORY_DIR / f"nasa_iss_oem_listing_{release_date}.xml").read_bytes()
    )
    txt_entry = [e for e in listing_entries if e.key.endswith(".txt")][0]

    orbit_history.verify_fetch_integrity(raw_bytes=raw_bytes, meta=meta, listing_entry=txt_entry)
    # Проверка не No-op — сравнивает то, что реально вычислено.
    assert release.etag == txt_entry.etag


def test_verify_fetch_integrity_rejects_tampered_bytes() -> None:
    release_date = "2024-05-08"
    raw_bytes = (HISTORY_DIR / f"nasa_iss_oem_{release_date}.txt").read_bytes()
    tampered = raw_bytes.replace(b"ISS", b"XXX", 1)
    meta = _load_meta_provenance(release_date)
    listing_entries = orbit_history.parse_s3_listing(
        (HISTORY_DIR / f"nasa_iss_oem_listing_{release_date}.xml").read_bytes()
    )
    txt_entry = [e for e in listing_entries if e.key.endswith(".txt")][0]

    with pytest.raises(orbit_history.OemIntegrityError, match="sha256"):
        orbit_history.verify_fetch_integrity(raw_bytes=tampered, meta=meta, listing_entry=txt_entry)


# ---------------------------------------------------------------------------
# source_version: различается по S3-ключу/CREATION_DATE/ETag, идемпотентен
# для одинакового входа (src/store/records.py дедуп-ключ)
# ---------------------------------------------------------------------------


def test_source_version_differs_across_real_releases(
    all_releases: dict[str, tuple[orbit_history.OemRelease, bytes]],
) -> None:
    fetched_at = datetime(2026, 9, 19, 9, 0, tzinfo=UTC)
    versions = set()
    for release_date in RELEASE_DATES:
        release, raw_bytes = all_releases[release_date]
        record = orbit_history.build_oem_orbital_elements_record(
            release, raw_bytes=raw_bytes, fetched_at=fetched_at
        )
        versions.add(record.source_version)
    assert len(versions) == len(RELEASE_DATES)  # все четыре выпуска — разные версии


def test_source_version_is_idempotent_for_the_same_release() -> None:
    release, raw_bytes = _load_release("2024-05-08")
    fetched_at_1 = datetime(2026, 9, 19, 9, 0, tzinfo=UTC)
    fetched_at_2 = datetime(2026, 9, 19, 10, 0, tzinfo=UTC)  # разное время получения

    record_1 = orbit_history.build_oem_orbital_elements_record(
        release, raw_bytes=raw_bytes, fetched_at=fetched_at_1
    )
    record_2 = orbit_history.build_oem_orbital_elements_record(
        release, raw_bytes=raw_bytes, fetched_at=fetched_at_2
    )
    # Повторное получение того же выпуска — идемпотентный дубль (тот же
    # дедуп-ключ), разница только в fetched_at (src/store/records.py: это
    # не содержательное поле, main-prompt §2 «дубликат не создаёт повторное
    # воздействие»).
    assert record_1.source_version == record_2.source_version
    assert record_1.provider_record_id == record_2.provider_record_id


# ---------------------------------------------------------------------------
# Хранилище: два реальных выпуска сохраняются рядом (не конфликт дедупа)
# ---------------------------------------------------------------------------


@pytest.fixture
def db_conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = connect(tmp_path / "store.sqlite3")
    yield conn
    conn.close()


@pytest.fixture
def raw_store(tmp_path: Path) -> RawOriginalStore:
    return RawOriginalStore(tmp_path / "raw")


def test_build_oem_record_stores_oem_meta_and_publication_evidence(
    db_conn: sqlite3.Connection, raw_store: RawOriginalStore
) -> None:
    release, raw_bytes = _load_release("2024-05-12")
    record_input = orbit_history.build_oem_orbital_elements_record(
        release, raw_bytes=raw_bytes, fetched_at=datetime(2026, 9, 19, 9, 0, tzinfo=UTC)
    )
    record_id = insert_record(db_conn, raw_store, record_input)
    stored = get_record(db_conn, record_id)

    assert stored is not None
    assert stored["record_kind"] == "orbital_elements"
    assert stored["orbital_elements_meta"]["format"] == "OEM"
    assert stored["orbital_elements_meta"]["coordinate_system"] == "EME2000"
    assert stored["orbital_elements_meta"]["epoch"] == "2024-05-10T18:51:53.202000Z"
    assert stored["published_at"] == "2024-05-13T03:06:46.000000Z"
    assert stored["replay_eligible"] is True  # published_at и source_version известны
    assert stored["spatial_context"]["publication_evidence"] == "s3_last_modified"
    # Оговорка ограничения доказательства сохранена в самой записи, не только в докстринге.
    assert "не" in stored["spatial_context"]["publication_evidence_note"]
    assert "value" in stored and stored["value"]["raw"]
    assert len(stored["value"]["state_vectors"]) == len(release.parsed.state_vectors)


def test_two_real_releases_are_stored_side_by_side_not_overwritten(
    db_conn: sqlite3.Connection, raw_store: RawOriginalStore
) -> None:
    fetched_at = datetime(2026, 9, 19, 9, 0, tzinfo=UTC)
    release_a, raw_a = _load_release("2024-05-08")
    release_b, raw_b = _load_release("2024-05-12")
    record_a = orbit_history.build_oem_orbital_elements_record(
        release_a, raw_bytes=raw_a, fetched_at=fetched_at
    )
    record_b = orbit_history.build_oem_orbital_elements_record(
        release_b, raw_bytes=raw_b, fetched_at=fetched_at
    )
    id_a = insert_record(db_conn, raw_store, record_a)
    id_b = insert_record(db_conn, raw_store, record_b)
    assert id_a != id_b
    stored_a = get_record(db_conn, id_a)
    stored_b = get_record(db_conn, id_b)
    assert stored_a is not None
    assert stored_b is not None
    assert stored_a["record_id"] != stored_b["record_id"]


# ---------------------------------------------------------------------------
# Отбор historical_forecast: таблица as_of -> выпуск из README фикстур
# (все строки таблицы покрыты реальными фикстурами — все 4 выпуска в репозитории)
# ---------------------------------------------------------------------------


_AS_OF_TABLE: list[tuple[str, str, tuple[str, str]]] = [
    # (as_of, expected release_date, (interval_start, interval_end) — внутри USEABLE выпуска)
    ("2024-05-10T12:00:00Z", "2024-05-08", ("2024-05-10T13:00:00Z", "2024-05-10T14:00:00Z")),
    # 2024-05-12 ещё не появился в архиве (LastModified 2024-05-13T03:06:46Z)
    ("2024-05-13T00:00:00Z", "2024-05-08", ("2024-05-10T13:00:00Z", "2024-05-10T14:00:00Z")),
    ("2024-05-13T06:00:00Z", "2024-05-12", ("2024-05-13T01:00:00Z", "2024-05-13T02:00:00Z")),
    ("2024-06-17T00:00:00Z", "2024-06-14", ("2024-06-17T01:00:00Z", "2024-06-17T02:00:00Z")),
    ("2024-06-20T00:00:00Z", "2024-06-18", ("2024-06-20T01:00:00Z", "2024-06-20T02:00:00Z")),
]


def _iso(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


@pytest.mark.parametrize("as_of_text,expected_release_date,interval", _AS_OF_TABLE)
def test_select_release_for_forecast_matches_readme_worked_table(
    as_of_text: str,
    expected_release_date: str,
    interval: tuple[str, str],
    all_releases: dict[str, tuple[orbit_history.OemRelease, bytes]],
) -> None:
    releases = [all_releases[date][0] for date in RELEASE_DATES]
    selected = orbit_history.select_release_for_forecast(
        releases,
        as_of=_iso(as_of_text),
        interval_start=_iso(interval[0]),
        interval_end=_iso(interval[1]),
    )
    assert selected is not None
    assert selected.release_date == expected_release_date


# ---------------------------------------------------------------------------
# Покрытие: as_of проходит, но USEABLE не покрывает интервал целиком -> None
# ---------------------------------------------------------------------------


def test_select_release_for_forecast_rejects_release_that_does_not_cover_interval(
    all_releases: dict[str, tuple[orbit_history.OemRelease, bytes]],
) -> None:
    releases = [all_releases["2024-05-08"][0]]  # единственный кандидат, published_at подходит
    as_of = _iso("2024-05-10T12:00:00Z")
    # Интервал начинается ДО USEABLE_START_TIME выпуска (2024-05-08T12:00Z) —
    # published_at <= as_of прошёл бы, но покрытие интервала — нет.
    selected = orbit_history.select_release_for_forecast(
        releases,
        as_of=as_of,
        interval_start=_iso("2024-05-08T00:00:00Z"),
        interval_end=_iso("2024-05-08T06:00:00Z"),
    )
    assert selected is None


# ---------------------------------------------------------------------------
# Обязательный тест на утечку времени (main-prompt §1, §9 п.1)
# ---------------------------------------------------------------------------


def test_adding_a_later_published_release_does_not_change_forecast_selection(
    all_releases: dict[str, tuple[orbit_history.OemRelease, bytes]],
) -> None:
    """Добавление записи с published_at > as_of не меняет результат
    historical_forecast ни в одном поле (.ai/main-prompt.md §9 п.1)."""
    as_of = _iso("2024-05-10T12:00:00Z")
    interval_start = _iso("2024-05-10T13:00:00Z")
    interval_end = _iso("2024-05-10T14:00:00Z")

    release_08, _ = all_releases["2024-05-08"]
    release_12, _ = all_releases["2024-05-12"]  # published_at > as_of здесь
    assert release_12.published_at > as_of

    without_future = orbit_history.select_release_for_forecast(
        [release_08], as_of=as_of, interval_start=interval_start, interval_end=interval_end
    )
    with_future = orbit_history.select_release_for_forecast(
        [release_08, release_12],
        as_of=as_of,
        interval_start=interval_start,
        interval_end=interval_end,
    )

    assert without_future is not None
    assert with_future is not None
    assert without_future == with_future  # ни одно поле не изменилось


# ---------------------------------------------------------------------------
# Интерполяция: точность против held-out реального вектора (не самоповтор)
# ---------------------------------------------------------------------------


def _dense_segment_nodes(
    release: orbit_history.OemRelease, *, exclude_time: datetime
) -> list[OemNode]:
    return [
        OemNode(time=v.time, position_km=v.position_km, velocity_km_s=v.velocity_km_s)
        for v in release.parsed.state_vectors
        if v.time != exclude_time
    ]


def test_interpolate_state_matches_held_out_real_vector_in_dense_maneuver_segment() -> None:
    """Держим вне входа реальный узел из плотного ~2с-участка манёвра
    (2024-05-24T14:16, nasa_iss_oem_2024-05-12.txt) и проверяем, что
    интерполянт его воспроизводит — не сверка с самой же реализацией
    (main-prompt §9 п.5)."""
    release, _ = _load_release("2024-05-12")
    held_out_time = datetime(2024, 5, 24, 14, 16, 30, tzinfo=UTC)
    held_out = next(v for v in release.parsed.state_vectors if v.time == held_out_time)

    nodes = _dense_segment_nodes(release, exclude_time=held_out_time)
    result = interpolate_state(nodes, held_out_time)

    # Допуск на 4 порядка шире фактического расхождения (~5e-9 км / ~1e-12
    # км/с на этом отрезке, dt=4с) — достаточно узко, чтобы поймать
    # перепутанные единицы/оси, но не требовать точности сильнее реальной
    # (main-prompt §9 п.5 «обоснованный допуск»).
    for actual, expected in zip(result.position_km, held_out.position_km, strict=True):
        assert actual == pytest.approx(expected, abs=1e-5)
    for actual, expected in zip(result.velocity_km_s, held_out.velocity_km_s, strict=True):
        assert actual == pytest.approx(expected, abs=1e-8)
    assert result.coordinate_system == "EME2000"


def test_interpolate_state_exact_at_nominal_240s_nodes() -> None:
    """На узле (s=0 или s=1) интерполянт обязан совпасть с узлом точно —
    свойство кубического Эрмита, а не приближение."""
    release, _ = _load_release("2024-05-08")
    vectors = release.parsed.state_vectors
    nodes = [
        OemNode(time=v.time, position_km=v.position_km, velocity_km_s=v.velocity_km_s)
        for v in vectors[:5]
    ]
    result = interpolate_state(nodes, vectors[2].time)
    assert result.position_km == pytest.approx(vectors[2].position_km, abs=1e-9)
    assert result.velocity_km_s == pytest.approx(vectors[2].velocity_km_s, abs=1e-9)


# ---------------------------------------------------------------------------
# Запрет экстраполяции
# ---------------------------------------------------------------------------


def test_interpolate_state_rejects_time_outside_covered_interval() -> None:
    release, _ = _load_release("2024-05-08")
    vectors = release.parsed.state_vectors
    nodes = [
        OemNode(time=v.time, position_km=v.position_km, velocity_km_s=v.velocity_km_s)
        for v in vectors[:5]
    ]
    before = nodes[0].time - timedelta(minutes=1)
    after = nodes[-1].time + timedelta(minutes=1)

    with pytest.raises(InterpolationError, match="no extrapolation|outside"):
        interpolate_state(nodes, before)
    with pytest.raises(InterpolationError, match="no extrapolation|outside"):
        interpolate_state(nodes, after)


def test_interpolate_state_rejects_unsorted_nodes() -> None:
    node_a = OemNode(
        time=datetime(2024, 5, 8, 12, 4, tzinfo=UTC),
        position_km=(0.0, 0.0, 0.0),
        velocity_km_s=(0.0, 0.0, 0.0),
    )
    node_b = OemNode(
        time=datetime(2024, 5, 8, 12, 0, tzinfo=UTC),  # раньше node_a — нарушает порядок
        position_km=(0.0, 0.0, 0.0),
        velocity_km_s=(0.0, 0.0, 0.0),
    )
    with pytest.raises(InterpolationError, match="increasing"):
        interpolate_state([node_a, node_b], node_a.time)


def test_interpolate_state_rejects_naive_datetime() -> None:
    node_a = OemNode(
        time=datetime(2024, 5, 8, 12, 0, tzinfo=UTC),
        position_km=(0.0, 0.0, 0.0),
        velocity_km_s=(0.0, 0.0, 0.0),
    )
    node_b = OemNode(
        time=datetime(2024, 5, 8, 12, 4, tzinfo=UTC),
        position_km=(1.0, 1.0, 1.0),
        velocity_km_s=(0.0, 0.0, 0.0),
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        interpolate_state([node_a, node_b], datetime(2024, 5, 8, 12, 2))


# ---------------------------------------------------------------------------
# historical_analysis vs historical_forecast — изоляция (main-prompt §1)
# ---------------------------------------------------------------------------


def test_analysis_and_forecast_can_select_different_releases_and_analysis_is_marked_reconstruction(
    all_releases: dict[str, tuple[orbit_history.OemRelease, bytes]],
) -> None:
    releases = [all_releases[date][0] for date in RELEASE_DATES]

    # Момент внутри перекрытия USEABLE окон 2024-05-08 (..05-23T12:00) и
    # 2024-05-12 (05-10T12:00..) — оба выпуска покрывают этот момент.
    moment = datetime(2024, 5, 15, 0, 0, tzinfo=UTC)

    analysis = sources_orbit.select_oem_elements_for_request(
        "historical_analysis", releases, moment=moment
    )
    # Ни один из двух покрывающих выпусков не создан ПОСЛЕ момента (оба
    # CREATION_DATE — начало мая) — правило берёт наиболее свежий из
    # покрывающих: 2024-05-12 (создан 05-10, позже чем 05-08 — 05-08).
    assert analysis.release.release_date == "2024-05-12"
    assert analysis.is_reconstruction is True

    # historical_forecast на as_of ДО публикации 2024-05-12 (LastModified
    # 2024-05-13T03:06:46Z) — единственный пригодный выпуск, покрывающий тот
    # же момент, — 2024-05-08.
    forecast = sources_orbit.select_oem_elements_for_request(
        "historical_forecast",
        releases,
        as_of=datetime(2024, 5, 11, 0, 0, tzinfo=UTC),
        interval_start=moment,
        interval_end=moment + timedelta(hours=1),
    )
    assert forecast.release.release_date == "2024-05-08"
    assert forecast.is_reconstruction is False

    # Разные выпуски для родственных запросов на один и тот же момент — не
    # смешаны в одну ветку кода.
    assert analysis.release.release_date != forecast.release.release_date

    analysis_record = orbit_history.build_oem_orbital_elements_record(
        analysis.release,
        raw_bytes=all_releases[analysis.release.release_date][1],
        fetched_at=datetime(2026, 9, 19, 9, 0, tzinfo=UTC),
        quality="reconstructed" if analysis.is_reconstruction else "nominal",
    )
    forecast_record = orbit_history.build_oem_orbital_elements_record(
        forecast.release,
        raw_bytes=all_releases[forecast.release.release_date][1],
        fetched_at=datetime(2026, 9, 19, 9, 0, tzinfo=UTC),
        quality="reconstructed" if forecast.is_reconstruction else "nominal",
    )
    assert analysis_record.quality == "reconstructed"
    assert forecast_record.quality == "nominal"


def test_select_oem_elements_for_request_raises_when_no_release_is_eligible(
    all_releases: dict[str, tuple[orbit_history.OemRelease, bytes]],
) -> None:
    releases = [all_releases["2024-05-12"][0]]  # published 2024-05-13T03:06:46Z
    with pytest.raises(sources_orbit.HistoricalElementsUnsupportedError):
        sources_orbit.select_oem_elements_for_request(
            "historical_forecast",
            releases,
            as_of=datetime(2024, 5, 1, 0, 0, tzinfo=UTC),  # задолго до публикации
            interval_start=datetime(2024, 5, 11, 0, 0, tzinfo=UTC),
            interval_end=datetime(2024, 5, 11, 1, 0, tzinfo=UTC),
        )


def test_select_oem_elements_for_request_rejects_current_mode() -> None:
    with pytest.raises(ValueError, match="current"):
        sources_orbit.select_oem_elements_for_request("current", [])
