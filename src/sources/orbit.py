"""Получение орбитальных элементов МКС (CelesTrak GP/TLE, NORAD 25544).

Слой ``sources``: получает и нормализует ответ источника в
``src.store.records.RecordInput`` — не интерпретирует физику и не считает
траекторию (.ai/main-prompt.md §8 «получение не считает физику»). Расчёт —
``src/domain/orbit/propagate.py``.

Текущие элементы — CelesTrak (эта задача, продукт ``celestrak-gp`` в
``sources.yaml``); ``fetch_elements_for_request``/``require_supported_mode``
ниже обслуживают ИМЕННО этот путь (живая сеть, только ``mode="current"``).

**Исторические элементы (FN-33, S2-03) — NASA TOPO CCSDS OEM,**
``src/sources/orbit_history.py`` (продукт ``nasa-iss-oem-history`` в
``sources.yaml``): готовые векторы состояния + интерполяция
(``src/domain/orbit/interpolate.py``), не GP/TLE и не SGP4.
:func:`select_oem_elements_for_request` ниже — актуальная точка входа для
``historical_analysis``/``historical_forecast``: она отбирает пригодный
выпуск OEM среди уже полученных (``orbit_history.OemRelease``) и поднимает
:class:`HistoricalElementsUnsupportedError`, только если пригодного выпуска
нет (главный случай — критический пробел покрытия/публикации, а не «модуль
исторических элементов вообще не поддерживает»). Space-Track ``GP_HISTORY``
остаётся задокументированным, но не реализованным источником (нет учётной
записи команды, см. ``sources.yaml``, продукт ``space-track-gp-history``) —
``require_supported_mode``/``fetch_elements_for_request`` продолжают
поднимать то же исключение для исторического запроса на путь CelesTrak, не
подставляя текущие элементы вместо исторических ни при каких обстоятельствах
(.ai/main-prompt.md §1, §11).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

import httpx

from src.sources import orbit_history
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
    """Исторический режим запрошен, а элементы для него недоступны.

    Два разных, но одинаково честных случая поднимают это исключение:

    - запрос идёт по пути CelesTrak (:func:`require_supported_mode`,
      :func:`fetch_elements_for_request`) — Space-Track ``GP_HISTORY`` ещё
      не реализован (см. ``sources.yaml``, продукт
      ``space-track-gp-history``), и текущие элементы CelesTrak не
      подставляются вместо исторических ни при каких обстоятельствах
      (.ai/main-prompt.md §11 «Траектория»);
    - запрос идёт по актуальному пути OEM
      (:func:`select_oem_elements_for_request`) — среди уже полученных
      выпусков ``src/sources/orbit_history.py`` нет ни одного, пригодного по
      правилу отбора (нет выпуска, опубликованного (S3 ``LastModified``) до
      ``as_of`` и покрывающего запрошенный интервал целиком) — критический
      пробел архива, а не повод взять более поздний или непокрывающий
      выпуск.

    В обоих случаях запрос на историческую геометрию честно отказывает, а не
    имитирует результат современными данными.
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
    # Обе строки TLE несут номер спутника (колонки 3-7) независимо друг от
    # друга; проверка одной только первой строки допустила бы пару "строка 1
    # МКС + валидная по checksum строка 2 другого спутника" — орбита
    # определяется параметрами строки 2, так что такая пара тихо считала бы
    # чужую орбиту орбитой МКС (round 1 ревью PR #17).
    norad_id_line2 = line2[2:7].strip()
    if norad_id_line2 != norad_id:
        raise CorruptedElementsError(
            f"TLE line 1 and line 2 NORAD ids disagree: {norad_id!r} vs {norad_id_line2!r}"
        )
    if norad_id != expected_norad_id:
        raise CorruptedElementsError(
            f"TLE NORAD id {norad_id!r} does not match expected {expected_norad_id!r}"
        )

    epoch = _tle_epoch(line1)
    return ParsedTle(
        object_name=object_name, norad_id=norad_id, line1=line1, line2=line2, epoch=epoch
    )


def _tle_content_fingerprint(line1: str, line2: str) -> str:
    """Короткий отпечаток содержимого пары строк TLE.

    Часть ``source_version`` наравне с эпохой: эпоха одна не идентифицирует
    версию однозначно — поставщик может выпустить уточнённый набор
    элементов (изменились параметры орбиты), не сдвинув эпоху. Без
    отпечатка такое уточнение получило бы тот же дедуп-ключ
    ``(source_id, provider_record_id, source_version)``, что и прежняя
    запись, но с другим содержимым — хранилище (``src/store/records.py``)
    отклонило бы его как ``DuplicateKeyConflictError`` вместо того, чтобы
    сохранить рядом со старой версией (round 1 ревью PR #17). Один и тот же
    набор элементов, полученный повторно, даёт тот же отпечаток и потому
    остаётся идемпотентным дублем, как и требуется.
    """
    return hashlib.sha256(f"{line1}\n{line2}".encode("ascii")).hexdigest()[:12]


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
        source_version=(
            f"{parsed.epoch.isoformat()}:"
            f"{_tle_content_fingerprint(parsed.line1, parsed.line2)}"
        ),
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

    Гейт именно пути CelesTrak (:func:`fetch_elements_for_request`, живая
    сеть): Space-Track ``GP_HISTORY`` ещё не реализован (см.
    ``sources.yaml``), и текущие элементы CelesTrak не подставляются вместо
    исторических (.ai/main-prompt.md §11) — для исторического режима эта
    функция не пытается угадать доступность OEM, она просто не пускает
    CelesTrak-путь на исторический запрос. Актуальный путь исторических
    элементов — :func:`select_oem_elements_for_request` (NASA OEM, FN-33).
    """
    if mode != "current":
        raise HistoricalElementsUnsupportedError(
            f"historical orbital elements are not served via CelesTrak (mode={mode!r}); "
            "current CelesTrak elements are not substituted for historical ones "
            "(.ai/main-prompt.md §11) — see select_oem_elements_for_request (NASA OEM, "
            "src/sources/orbit_history.py) for the actual historical path, and "
            "sources.yaml product space-track-gp-history for the still-unimplemented "
            "Space-Track GP_HISTORY fallback"
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


def select_oem_elements_for_request(
    mode: RequestMode,
    releases: list[orbit_history.OemRelease],
    *,
    as_of: datetime | None = None,
    interval_start: datetime | None = None,
    interval_end: datetime | None = None,
    moment: datetime | None = None,
) -> orbit_history.OemSelection:
    """Отбирает исторические орбитальные элементы (NASA OEM) для запроса.

    Актуальная точка входа для ``historical_analysis``/``historical_forecast``
    (FN-33, S2-03) — в отличие от :func:`fetch_elements_for_request`
    (CelesTrak, только ``current``, не ходит по этому пути вообще), эта
    функция не ходит в сеть: ``releases`` — уже полученные и разобранные
    выпуски (``src/sources/orbit_history.py``, гейт/проба-масштаб задачи, см.
    её модульный docstring); загрузка полного архива в хранилище — задача
    следующего этапа.

    - ``mode="historical_forecast"``: требует ``as_of``, ``interval_start``,
      ``interval_end`` — делегирует
      :func:`orbit_history.select_release_for_forecast`
      (``published_at <= as_of`` и покрытие интервала целиком, максимальный
      пригодный ``published_at``); возвращает
      ``OemSelection(is_reconstruction=False)``.
    - ``mode="historical_analysis"``: требует ``moment`` — делегирует
      :func:`orbit_history.select_release_for_analysis` (НЕ фильтруется по
      ``as_of`` — main-prompt.md §1 «Последующие наблюдения — отдельная
      ветка кода»); возвращает ``OemSelection(is_reconstruction=True)``.
    - ``mode="current"``: не обслуживается здесь — поднимает ``ValueError``
      (текущий режим — :func:`fetch_elements_for_request`, CelesTrak).

    Если ни один выпуск не пригоден по правилу отбора, поднимается
    :class:`HistoricalElementsUnsupportedError` — критический пробел
    архива/публикации на запрошенный момент, а не повод взять более поздний
    или не покрывающий интервал выпуск, и не современные элементы CelesTrak
    (.ai/main-prompt.md §1, §11).
    """
    if mode == "current":
        raise ValueError(
            "mode='current' is served by fetch_elements_for_request (CelesTrak), "
            "not select_oem_elements_for_request"
        )
    if mode == "historical_forecast":
        if as_of is None or interval_start is None or interval_end is None:
            raise ValueError(
                "mode='historical_forecast' requires as_of, interval_start and interval_end"
            )
        release = orbit_history.select_release_for_forecast(
            releases, as_of=as_of, interval_start=interval_start, interval_end=interval_end
        )
        if release is None:
            raise HistoricalElementsUnsupportedError(
                f"no OEM release is published (S3 LastModified) by as_of={as_of.isoformat()} "
                f"and covers [{interval_start.isoformat()}, {interval_end.isoformat()}] — "
                "critical archive/publication gap, not a reason to substitute a later or "
                "non-covering release, or current CelesTrak elements (.ai/main-prompt.md §1, §11)"
            )
        return orbit_history.OemSelection(release=release, is_reconstruction=False)
    if mode == "historical_analysis":
        if moment is None:
            raise ValueError("mode='historical_analysis' requires moment")
        release = orbit_history.select_release_for_analysis(releases, moment=moment)
        if release is None:
            raise HistoricalElementsUnsupportedError(
                f"no OEM release covers moment={moment.isoformat()} "
                "(USEABLE_START_TIME..USEABLE_STOP_TIME of every known release misses it) — "
                "critical archive gap, not a reason to substitute current CelesTrak elements "
                "(.ai/main-prompt.md §1, §11)"
            )
        return orbit_history.OemSelection(release=release, is_reconstruction=True)
    raise ValueError(f"unknown mode {mode!r}")
