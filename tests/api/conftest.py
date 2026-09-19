"""Общие фикстуры для тестов API (src/api).

Сеть в тестах отсутствует (.ai/main-prompt.md §9 «тесты детерминированы»):
``src.sources.orbit.fetch_current_tle``, ``src.sources.swpc.fetch``,
``src.sources.noaa_3day_forecast.fetch`` и — с FN-41 — ``src.sources.
orbit_history.fetch``/``src.sources.donki.fetch`` подменяются на
сохранённые/явно синтетические фикстуры (``tests/fixtures/orbit``,
``tests/fixtures/orbit/history``, ``tests/fixtures/sources/swpc``,
``tests/fixtures/sources/archive``, ``tests/fixtures/sources/noaa_3day_forecast``),
как это уже делают ``tests/orbit/test_propagation.py`` и
``tests/sources/test_swpc.py``. Без этой подмены (FN-31, round 3 ревью PR #24
— подключение коннектора к оркестрации) любой тест, идущий через
``app_client``, реально обращался бы к ``services.swpc.noaa.gov`` — не
только медленно, но и не детерминированно вне сети с прямым доступом.

Архивные подмены FN-41 отвечают ровно то, что отвечает реальный источник на
тот же URL (сохранённые байт-в-байт ответы, см. README обеих папок фикстур),
и честно отдают 404 для датированных выпусков, которых в фикстурах нет —
так тесты проходят тот же путь частичного получения кандидатов, что и
production.

Каждый тест получает свой временный каталог хранилища (``STORE_DB_PATH``/
``STORE_RAW_DIR``), поэтому тесты не делят состояние SQLite друг с другом —
как и ``tests/store/conftest.py`` для более низкого уровня.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterator
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.config import get_settings
from src.sources import donki as donki_source
from src.sources import noaa_3day_forecast as noaa_3day_source
from src.sources import orbit as orbit_source
from src.sources import orbit_history as orbit_history_source
from src.sources import swpc as swpc_source
from src.sources.http import HttpFetchResult, SourceHttpError

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"
ORBIT_TLE_SAMPLE = FIXTURES_DIR / "orbit" / "celestrak_iss_gp_sample.tle"
SWPC_SAMPLE = FIXTURES_DIR / "sources" / "swpc" / "integral-protons-1-day.sample.json"
NOAA_3DAY_SAMPLE = (
    FIXTURES_DIR / "sources" / "noaa_3day_forecast" / "synthetic" / "bulletin_2025-01-05_2200.txt"
)
OEM_HISTORY_DIR = FIXTURES_DIR / "orbit" / "history"
ARCHIVE_DIR = FIXTURES_DIR / "sources" / "archive"

#: Датированные выпуски OEM, реально сохранённые в фикстурах (README этой
#: папки). Индекс архива при этом настоящий и содержит все 736 выпусков —
#: кандидат, которого нет среди этих четырёх, получает честный 404, как и
#: любой недоступный объект бакета.
OEM_FIXTURE_RELEASES = ("2024-05-08", "2024-05-12", "2024-06-14", "2024-06-18")

_OEM_RELEASE_DATE_RE = re.compile(r"iss-coords/(?P<date>\d{4}-\d{2}-\d{2})/")
_DONKI_RANGE_RE = re.compile(r"startDate=(?P<start>[\d-]+)&endDate=(?P<end>[\d-]+)")


def orbit_tle_bytes() -> bytes:
    return ORBIT_TLE_SAMPLE.read_bytes()


def swpc_sample_bytes() -> bytes:
    return SWPC_SAMPLE.read_bytes()


def noaa_3day_sample_bytes() -> bytes:
    return NOAA_3DAY_SAMPLE.read_bytes()


def fake_oem_fetch(url: str, **_kwargs: Any) -> HttpFetchResult:
    """Ответ архива NASA TOPO OEM из сохранённых фикстур.

    Маршрутизация — по тому же URL, что строит production-шлюз: индекс
    архива, листинг папки выпуска, тело выпуска. Выпуск, которого нет среди
    :data:`OEM_FIXTURE_RELEASES`, даёт ``SourceHttpError`` (как 404 бакета),
    а не тихо пустой ответ.
    """
    if url == orbit_history_source.ARCHIVE_INDEX_URL:
        return HttpFetchResult(
            status_code=200,
            body=(OEM_HISTORY_DIR / "nasa_iss_oem_archive_index.xml").read_bytes(),
            url=url,
            elapsed_seconds=0.001,
        )
    match = _OEM_RELEASE_DATE_RE.search(url)
    if match is None:
        raise SourceHttpError(f"unexpected OEM url in test: {url}")
    release_date = match.group("date")
    if release_date not in OEM_FIXTURE_RELEASES:
        raise SourceHttpError(f"{url} responded 404 (not retried)")
    name = (
        f"nasa_iss_oem_{release_date}.txt"
        if url.endswith(".txt")
        else f"nasa_iss_oem_listing_{release_date}.xml"
    )
    return HttpFetchResult(
        status_code=200,
        body=(OEM_HISTORY_DIR / name).read_bytes(),
        url=url,
        elapsed_seconds=0.001,
    )


def donki_notifications_for_range(start_day: date, end_day: date) -> list[dict[str, Any]]:
    """Уведомления реального архива DONKI с ``messageIssueTime`` в
    ``[start_day, end_day]`` — та же фильтрация по времени ВЫПУСКА, которую
    делает сам сервер (см. tests/fixtures/sources/archive/README.md)."""
    selected: list[dict[str, Any]] = []
    for path in sorted(ARCHIVE_DIR.glob("donki_2024-*-*_2024-*-*.json")):
        if path.name.endswith(".meta.json"):
            continue
        for entry in json.loads(path.read_text(encoding="utf-8")):
            issued = datetime.fromisoformat(
                str(entry["messageIssueTime"]).replace("Z", "+00:00")
            ).astimezone(timezone.utc)
            if start_day <= issued.date() <= end_day:
                selected.append(entry)
    return selected


def fake_donki_fetch(url: str, **_kwargs: Any) -> HttpFetchResult:
    """Ответ ``DONKI/notifications`` из сохранённого реального архива."""
    match = _DONKI_RANGE_RE.search(url)
    if match is None:
        raise SourceHttpError(f"unexpected DONKI url in test: {url}")
    entries = donki_notifications_for_range(
        date.fromisoformat(match.group("start")), date.fromisoformat(match.group("end"))
    )
    return HttpFetchResult(
        status_code=200,
        body=json.dumps(entries, ensure_ascii=False).encode("utf-8"),
        url=url,
        elapsed_seconds=0.001,
    )


def install_archive_fetch_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Подменяет оба архивных сетевых вызова исторических режимов (FN-41)."""
    monkeypatch.setattr(orbit_history_source, "fetch", fake_oem_fetch)
    monkeypatch.setattr(donki_source, "fetch", fake_donki_fetch)


def utc(
    year: int, month: int, day: int, hour: int = 0, minute: int = 0
) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


def days(count: int) -> timedelta:
    return timedelta(days=count)


@pytest.fixture
def app_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """``TestClient`` за свежим, изолированным ``create_app()``.

    Орбита и поток протонов подменены сохранёнными реальными фикстурами по
    умолчанию (стандартный «счастливый путь»); отдельные тесты
    переопределяют ``src.sources.orbit.fetch_current_tle``/
    ``src.sources.swpc.fetch`` заново через ``monkeypatch``, чтобы
    смоделировать отказ конкретного источника.
    """
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("STORE_DB_PATH", str(tmp_path / "store.sqlite3"))
    monkeypatch.setenv("STORE_RAW_DIR", str(tmp_path / "raw"))
    get_settings.cache_clear()

    orbit_bytes = orbit_tle_bytes()

    def fake_fetch_current_tle(
        *,
        norad_id: str = orbit_source.ISS_NORAD_ID,
        client: httpx.Client | None = None,
        timeout: httpx.Timeout | None = None,
    ) -> bytes:
        return orbit_bytes

    monkeypatch.setattr(orbit_source, "fetch_current_tle", fake_fetch_current_tle)

    swpc_bytes = swpc_sample_bytes()

    def fake_http_fetch(url: str, **_kwargs: Any) -> HttpFetchResult:
        return HttpFetchResult(status_code=200, body=swpc_bytes, url=url, elapsed_seconds=0.001)

    monkeypatch.setattr(swpc_source, "fetch", fake_http_fetch)

    noaa_3day_bytes = noaa_3day_sample_bytes()

    def fake_noaa_3day_fetch(url: str, **_kwargs: Any) -> HttpFetchResult:
        return HttpFetchResult(
            status_code=200, body=noaa_3day_bytes, url=url, elapsed_seconds=0.001
        )

    monkeypatch.setattr(noaa_3day_source, "fetch", fake_noaa_3day_fetch)
    install_archive_fetch_fakes(monkeypatch)

    app = create_app()
    with TestClient(app) as client:
        yield client

    get_settings.cache_clear()


def wait_for_job(
    client: TestClient, task_id: str, *, timeout_seconds: float = 5.0
) -> dict[str, Any]:
    """Опрашивает ``GET /api/calculations/{task_id}`` до терминального статуса.

    Тестовый эквивалент клиента, ожидающего длительную операцию — реальный
    расчёт здесь занимает миллисекунды (нет сети), но выполняется в фоновом
    потоке (``loop.run_in_executor``), поэтому статус не готов сразу после
    202.
    """
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        response = client.get(f"/api/calculations/{task_id}")
        assert response.status_code == 200
        body = response.json()
        if body["status"] in ("done", "failed"):
            return body
        time.sleep(0.01)
    raise AssertionError(
        f"task {task_id} did not reach a terminal status within {timeout_seconds}s"
    )
