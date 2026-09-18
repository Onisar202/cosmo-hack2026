"""Получение орбитальных элементов МКС (CelesTrak GP/TLE, NORAD 25544).

Слой ``sources``: получает и нормализует ответ источника в
``src.store.records.RecordInput`` — не интерпретирует физику и не считает
траекторию (.ai/main-prompt.md §8 «получение не считает физику»). Расчёт —
``src/domain/orbit/propagate.py``.

Текущие элементы — CelesTrak (эта задача, продукт ``celestrak-gp`` в
``sources.yaml``). Исторические — Space-Track ``GP_HISTORY`` с
``CREATION_DATE`` как ``published_at`` (.ai/main-prompt.md §11
«Траектория»); соответствующий коннектор — задача следующего этапа зоны 1
(см. ``sources.yaml``, продукт ``space-track-gp-history``). До его
появления исторический запрос честно поднимает
:class:`HistoricalElementsUnsupportedError`, а не подставляет текущие
элементы CelesTrak — современные элементы никогда не заменяют исторические
(.ai/main-prompt.md §1, §11).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

import httpx

from src.store.records import RecordInput

ISS_NORAD_ID = "25544"

SOURCE_ID_CURRENT = "celestrak-gp"
# Ещё не реализован — см. sources.yaml, продукт space-track-gp-history.
SOURCE_ID_HISTORICAL = "space-track-gp-history"

CELESTRAK_GP_URL = "https://celestrak.org/NORAD/elements/gp.php?CATNR={norad_id}&FORMAT=TLE"

# Жёсткий таймаут на соединение и на чтение отдельно (.ai/backend-prompt.md
# §3) — вызов без таймаута в этом слое не допускается.
DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)

RequestMode = Literal["current", "historical_analysis", "historical_forecast"]
Quality = Literal["nominal", "degraded", "reconstructed", "unknown"]


class OrbitSourceError(RuntimeError):
    """Источник орбитальных элементов недоступен или вернул ошибку.

    Отказ источника — статус, а не благоприятная оценка (.ai/main-prompt.md
    §2): исключение поднимается наверх, а не превращается в пустой или
    придуманный набор элементов.
    """


class OrbitSourceQuotaError(OrbitSourceError):
    """Источник ответил кодом квоты (429).

    Обрабатывается отдельно от прочих ошибок источника: квота — не сбой
    сети, немедленный повтор только ухудшит ситуацию
    (.ai/backend-prompt.md §3). Решение о паузе и общем лимите попыток
    принимает вызывающая сторона (планировщик обновления).
    """


class CorruptedElementsError(ValueError):
    """Ответ источника не проходит контроль целостности TLE.

    Несовпадение длины строки, контрольной суммы или NORAD ID — повреждённые
    элементы дают явный статус, а не тихо принимаются как рабочие
    (.ai/main-prompt.md §11 «Отказ/старые/повреждённые элементы дают
    статус»).
    """


class HistoricalElementsUnsupportedError(NotImplementedError):
    """Исторический режим запрошен, а Space-Track GP_HISTORY ещё не реализован.

    Текущие элементы CelesTrak не подставляются вместо исторических ни при
    каких обстоятельствах (.ai/main-prompt.md §11 «Траектория»): до задачи,
    добавляющей коннектор ``space-track-gp-history`` (см. ``sources.yaml``),
    запрос на историческую геометрию честно отказывает, а не имитирует
    результат современными данными.
    """


@dataclass(frozen=True)
class ParsedTle:
    """Разобранный и провалидированный набор орбитальных элементов TLE."""

    object_name: str | None
    norad_id: str
    line1: str
    line2: str
    epoch: datetime  # UTC — эпоха элементов


def fetch_current_tle(
    *,
    norad_id: str = ISS_NORAD_ID,
    client: httpx.Client | None = None,
    timeout: httpx.Timeout = DEFAULT_TIMEOUT,
) -> bytes:
    """Получает текущий набор GP/TLE с CelesTrak.

    Жёсткий таймаут на каждый вызов (.ai/backend-prompt.md §3); повторов
    здесь нет намеренно — решение о повторе с экспоненциальной задержкой и
    общем лимите попыток принимает вызывающая сторона (планировщик
    обновления источников, вне объёма этой задачи).
    """
    url = CELESTRAK_GP_URL.format(norad_id=norad_id)
    owns_client = client is None
    http_client = client if client is not None else httpx.Client()
    try:
        response = http_client.get(url, timeout=timeout)
    except httpx.TimeoutException as exc:
        raise OrbitSourceError(f"CelesTrak GP request timed out: {url}") from exc
    except httpx.HTTPError as exc:
        raise OrbitSourceError(f"CelesTrak GP request failed: {url}: {exc}") from exc
    finally:
        if owns_client:
            http_client.close()

    if response.status_code == 429:
        # Квота — отдельная ветка ошибки, не общий сбой сети
        # (.ai/backend-prompt.md §3).
        raise OrbitSourceQuotaError(f"CelesTrak GP quota exceeded (429): {url}")
    if response.status_code != 200:
        raise OrbitSourceError(f"CelesTrak GP returned HTTP {response.status_code}: {url}")
    if not response.content.strip():
        # Код 200 с пустым телом — ошибка источника, а не набор нулей
        # (.ai/backend-prompt.md §3).
        raise OrbitSourceError(f"CelesTrak GP returned an empty body: {url}")
    return response.content


def _tle_checksum(line: str) -> int:
    """Контрольная сумма строки TLE: сумма цифр по модулю 10; ``-`` считается
    за 1, прочие символы игнорируются."""
    total = 0
    for ch in line[:-1]:
        if ch.isdigit():
            total += int(ch)
        elif ch == "-":
            total += 1
    return total % 10


def _validate_tle_line(line: str, *, line_number: int) -> None:
    if len(line) != 69:
        raise CorruptedElementsError(
            f"TLE line {line_number} has length {len(line)}, expected 69: {line!r}"
        )
    if not line.startswith(str(line_number)):
        raise CorruptedElementsError(
            f"TLE line {line_number} does not start with {line_number!r}: {line!r}"
        )
    try:
        actual_checksum = int(line[-1])
    except ValueError as exc:
        raise CorruptedElementsError(
            f"TLE line {line_number} checksum digit is not numeric: {line!r}"
        ) from exc
    expected_checksum = _tle_checksum(line)
    if actual_checksum != expected_checksum:
        raise CorruptedElementsError(
            f"TLE line {line_number} checksum mismatch: expected {expected_checksum}, "
            f"got {actual_checksum}: {line!r}"
        )


def _tle_epoch(line1: str) -> datetime:
    """Эпоха элементов из колонок 19-32 строки 1 (``YYDDD.DDDDDDDD``).

    Независимо перепроверено в tests/orbit/test_propagation.py против
    эпохи, которую для тех же строк вычисляет сам SGP4
    (``src.domain.orbit.propagate.load_elements``) — оба пути должны
    совпасть до микросекунды.
    """
    epoch_field = line1[18:32]
    year_2digit = int(epoch_field[0:2])
    day_of_year_frac = float(epoch_field[2:])
    # Правило TLE (Spacetrack Report #3): 57-99 -> 1957-1999, 00-56 -> 2000-2056.
    year = 2000 + year_2digit if year_2digit < 57 else 1900 + year_2digit
    return datetime(year, 1, 1, tzinfo=timezone.utc) + timedelta(days=day_of_year_frac - 1)


def parse_tle_response(raw_bytes: bytes, *, expected_norad_id: str = ISS_NORAD_ID) -> ParsedTle:
    """Разбирает и проверяет ответ источника в формате TLE (2 или 3 строки).

    Парсер пишется под сохранённый реальный ответ
    (.ai/backend-prompt.md §3): ``tests/fixtures/orbit/`` содержит образец,
    ``tests/orbit/test_propagation.py`` гоняется на нём.
    """
    try:
        text = raw_bytes.decode("ascii", errors="strict")
    except UnicodeDecodeError as exc:
        raise CorruptedElementsError(f"TLE response is not ASCII: {exc}") from exc

    lines = [ln.rstrip("\r") for ln in text.splitlines() if ln.strip()]
    object_name: str | None
    if len(lines) == 2:
        object_name = None
        line1, line2 = lines
    elif len(lines) == 3:
        object_name, line1, line2 = lines
        object_name = object_name.strip()
    else:
        raise CorruptedElementsError(
            f"expected 2 or 3 non-empty lines in TLE response, got {len(lines)}"
        )

    _validate_tle_line(line1, line_number=1)
    _validate_tle_line(line2, line_number=2)

    norad_id = line1[2:7].strip()
    if norad_id != expected_norad_id:
        raise CorruptedElementsError(
            f"TLE NORAD id {norad_id!r} does not match expected {expected_norad_id!r}"
        )

    epoch = _tle_epoch(line1)
    return ParsedTle(
        object_name=object_name, norad_id=norad_id, line1=line1, line2=line2, epoch=epoch
    )


def build_orbital_elements_record(
    parsed: ParsedTle,
    *,
    raw_bytes: bytes,
    fetched_at: datetime,
    source_url: str,
    quality: Quality = "nominal",
) -> RecordInput:
    """Строит запись источника (``record_kind=orbital_elements``) для сохранения в ``store``.

    ``published_at`` — всегда ``None``: CelesTrak не публикует отдельное
    время выпуска набора элементов (см. ``sources.yaml``, продукт
    ``celestrak-gp``), в отличие от Space-Track ``GP_HISTORY``
    (``CREATION_DATE``). Хранилище поэтому всегда вычислит
    ``replay_eligible = false`` для этих записей — так текущие элементы не
    могут быть тихо использованы вместо исторических в строгом
    ``historical_forecast`` (.ai/main-prompt.md §1, §11).
    """
    tle_text = f"{parsed.line1}\n{parsed.line2}"
    spatial_context: dict[str, object] = {"norad_id": parsed.norad_id}
    if parsed.object_name:
        spatial_context["object_name"] = parsed.object_name

    return RecordInput(
        provider_record_id=f"iss-gp-{parsed.norad_id}",
        source_id=SOURCE_ID_CURRENT,
        source_url=source_url,
        record_kind="orbital_elements",
        observed_at=parsed.epoch,
        valid_from=parsed.epoch,
        valid_to=parsed.epoch,
        published_at=None,
        fetched_at=fetched_at,
        value={"raw": tle_text},
        unit=None,
        spatial_context=spatial_context,
        source_version=parsed.epoch.isoformat(),
        quality=quality,
        raw_bytes=raw_bytes,
        orbital_elements_meta={
            "epoch": parsed.epoch.isoformat().replace("+00:00", "Z"),
            "format": "TLE",
            "coordinate_system": "TEME",
        },
    )


def require_supported_mode(mode: RequestMode) -> None:
    """Поднимает :class:`HistoricalElementsUnsupportedError` для любого режима, кроме ``current``.

    Space-Track ``GP_HISTORY`` ещё не реализован (см. ``sources.yaml``), и
    текущие элементы CelesTrak не подставляются вместо исторических
    (.ai/main-prompt.md §11).
    """
    if mode != "current":
        raise HistoricalElementsUnsupportedError(
            f"historical orbital elements are not supported yet (mode={mode!r}); "
            "the Space-Track GP_HISTORY connector is a future task (see sources.yaml, "
            "product space-track-gp-history) — current CelesTrak elements are not "
            "substituted for historical ones (.ai/main-prompt.md §11)"
        )


def fetch_elements_for_request(
    mode: RequestMode,
    *,
    norad_id: str = ISS_NORAD_ID,
    client: httpx.Client | None = None,
    timeout: httpx.Timeout = DEFAULT_TIMEOUT,
) -> tuple[ParsedTle, bytes, str]:
    """Получает орбитальные элементы для запроса заданного режима.

    Возвращает ``(parsed, raw_bytes, source_url)``. Поддерживается только
    ``mode="current"`` — для остальных режимов честно поднимается
    :class:`HistoricalElementsUnsupportedError` (см.
    :func:`require_supported_mode`), геометрия при этом до этапа с
    Space-Track остаётся официально неподдерживаемой, а не приблизительной.
    """
    require_supported_mode(mode)
    url = CELESTRAK_GP_URL.format(norad_id=norad_id)
    raw_bytes = fetch_current_tle(norad_id=norad_id, client=client, timeout=timeout)
    parsed = parse_tle_response(raw_bytes, expected_norad_id=norad_id)
    return parsed, raw_bytes, url
