"""Исторические орбитальные элементы МКС — NASA TOPO CCSDS OEM (FN-33, S2-03).

Слой ``sources``: парсит и нормализует уже полученные ответы (OEM-файл,
S3-листинг выпуска) в ``src.store.records.RecordInput`` — не интерпретирует
физику и не считает траекторию (.ai/main-prompt.md §8). Интерполяция —
``src/domain/orbit/interpolate.py``.

**Решение задачи (Jira-комментарии владельца задачи, воспроизведены в
``.ai/`` prompt этой сессии).** Space-Track ``GP_HISTORY`` остаётся
задокументированным, но не реализованным источником (нет учётной записи
команды) — ``src/sources/orbit.py::HistoricalElementsUnsupportedError``
продолжает быть его отказом, без изменений. Основной путь исторических
элементов ISS — публичный, не требующий ключа архив NASA JSC/FOD/TOPO CCSDS
OEM 2.0 (``https://nasa-public-data.s3.amazonaws.com/iss-coords/<дата>/
ISS_OEM/ISS.OEM_J2K_EPH.txt``), зарегистрированный в ``sources.yaml`` как
``nasa-iss-oem-history``. Формат — готовые векторы состояния (позиция +
скорость), а не GP/TLE — SGP4 к OEM не применяется (см.
``src/domain/orbit/interpolate.py``).

**Время честности — суть задачи.** ``published_at`` датированного выпуска —
S3 ``LastModified`` объекта из листинга бакета (см.
:func:`parse_s3_listing`), **никогда** ``CREATION_DATE`` заголовка OEM (это
момент, когда баллистик СОЗДАЛ файл — он может предшествовать появлению
файла в публичном архиве на срок до нескольких суток, реальный случай —
``2024-05-12``: ``CREATION_DATE=2024-05-10T18:51:53Z``,
``LastModified=2024-05-13T03:06:46Z``, см.
``tests/fixtures/orbit/history/README.md``) и **никогда** имя датированной
папки. :data:`PUBLICATION_EVIDENCE_NOTE` фиксирует точную границу того, что
это доказывает: время конкретной версии S3-объекта, а не аудированную
историю публичной доступности/ACL бакета — формулировка вида «доказан точный
момент публичного открытия» здесь и нигде рядом не используется.

**Два раздельных пути отбора** (main-prompt.md §1 «Последующие наблюдения —
отдельная ветка кода»):

- :func:`select_release_for_forecast` — вход строгого ``historical_forecast``:
  ``published_at <= as_of`` И интервал ``[USEABLE_START_TIME,
  USEABLE_STOP_TIME]`` выпуска покрывает расчётный интервал целиком; среди
  пригодных — выпуск с максимальным ``published_at``. Ни при каких
  обстоятельствах не возвращает более поздний выпуск и не «доливает»
  недостающее покрытие современными элементами CelesTrak
  (.ai/main-prompt.md §1, §11).
- :func:`select_release_for_analysis` — вход ``historical_analysis``, НЕ
  фильтруется по ``as_of``: предпочитается выпуск, созданный вскоре после
  интересующего момента (ближе к фактически прошедшей траектории, а не
  давний прогноз на этот момент). Запись, построенная по этому пути, всегда
  несёт ``quality="reconstructed"`` (:func:`build_oem_orbital_elements_record`)
  — она не должна тихо использоваться как вход строгого прогноза.

**Интерполяция, не SGP4.** OEM отдаёт готовые векторы состояния на
неравномерной сетке (номинальный шаг 240 с, плотные ~2-секундные участки
вокруг манёвров) — среднеэлементного набора для SGP4 в OEM нет.
Интерполяция (кубическая Эрмита по узлам) — чистая функция без I/O,
``src/domain/orbit/interpolate.py``; этот модуль только разбирает и
структурирует узлы, не интерполирует.

**Система координат.** ``REF_FRAME = EME2000`` совпадает с системой, в
которой заданы радианты MMOD (``src/domain/mmod``, main-prompt.md §11) —
преобразование в TEME здесь не требуется и не делается (TEME — только у
SGP4/TLE-пути ``src/sources/orbit.py``).

Область задачи (гейт/проба, main-prompt.md §11 «Это доказательство
доступности, не реализованный replay всего сервиса», по аналогии с
``src/sources/archive_probe.py``): парсинг и отбор работают над уже
полученными файлами (реальные фикстуры
``tests/fixtures/orbit/history/``, см. её README о происхождении) — этот
модуль не содержит production-шлюза с периодическим refresh/TTL наподобие
``src/sources/swpc.py::fetch_and_store``; постраничная загрузка полного
архива в хранилище — задача следующего этапа.
"""

from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Literal

from src.store.records import RecordInput

SOURCE_ID = "nasa-iss-oem-history"

ISS_OBJECT_ID = "1998-067-A"
ISS_NORAD_ID = "25544"

OEM_URL_TEMPLATE = (
    "https://nasa-public-data.s3.amazonaws.com/iss-coords/{release_date}/ISS_OEM/"
    "ISS.OEM_J2K_EPH.txt"
)

UTC = timezone.utc

Quality = Literal["nominal", "degraded", "reconstructed", "unknown"]

# .ai/main-prompt.md §1: ни один код в этом модуле не утверждает больше, чем
# доказывает S3 LastModified — см. использование в build_oem_orbital_elements_record
# и tests/fixtures/orbit/history/README.md.
PUBLICATION_EVIDENCE = "s3_last_modified"
PUBLICATION_EVIDENCE_NOTE = (
    "S3 LastModified датированного объекта доказывает момент ЭТОЙ ВЕРСИИ "
    "объекта в бакете, а не аудированную историю публичной доступности/ACL "
    "бакета. Формулировка «доказан точный момент публичного открытия» "
    "намеренно не используется — LastModified не раньше момента, когда файл "
    "стал общедоступен, но независимого журнала смены прав доступа к бакету "
    "нет (tests/fixtures/orbit/history/README.md)."
)


class OemFormatError(RuntimeError):
    """Ответ не соответствует формату CCSDS OEM 2.0 KVN, который ожидает парсер.

    Парсер написан под сохранённый реальный ответ
    (.ai/backend-prompt.md §3) — структурно неожиданный ответ явно
    отклоняется, а не превращается в частично разобранный набор узлов
    (main-prompt.md §2)."""


class OemListingFormatError(RuntimeError):
    """S3 ``ListBucketResult`` не соответствует ожидаемой форме листинга.

    Тот же принцип, что и :class:`OemFormatError`: код 200 со структурно
    неожиданным телом — ошибка источника, не пустой список объектов."""


class OemIntegrityError(RuntimeError):
    """Перепроверка целостности (sha256/ETag/Last-Modified) не сошлась.

    Кросс-проверка ``*.meta.json`` (заголовки исходного HTTP-ответа) против
    S3-листинга выпуска и фактических байтов файла — расхождение означает,
    что сохранённый оригинал, листинг и заявленные метаданные больше не
    описывают один и тот же объект, и запись не должна тихо приниматься как
    целая (см. tests/fixtures/orbit/history/README.md «Целостность
    подтверждена независимо»)."""


@dataclass(frozen=True)
class OemStateVectorRecord:
    """Один узел OEM: момент, положение (км) и скорость (км/с), EME2000."""

    time: datetime
    position_km: tuple[float, float, float]
    velocity_km_s: tuple[float, float, float]


@dataclass(frozen=True)
class ParsedOem:
    """Разобранный и провалидированный OEM-файл (один META/данные-сегмент)."""

    object_name: str
    object_id: str
    norad_id: str
    center_name: str
    ref_frame: str
    time_system: str
    originator: str
    creation_date: datetime  # заголовок CREATION_DATE — момент создания файла баллистиком
    start_time: datetime
    useable_start_time: datetime
    useable_stop_time: datetime
    stop_time: datetime
    state_vectors: tuple[OemStateVectorRecord, ...]


@dataclass(frozen=True)
class S3ListingEntry:
    """Один объект из ``ListBucketResult`` (S3-листинг папки выпуска)."""

    key: str
    last_modified: datetime  # UTC — доказуемое время ЭТОЙ версии объекта
    etag: str  # без обрамляющих кавычек
    size: int


@dataclass(frozen=True)
class OemRelease:
    """Один датированный выпуск OEM: разобранное содержимое + доказанная
    публикация (S3-листинг), достаточные для отбора и построения записи."""

    release_date: str  # "2024-05-08" — идентификатор датированной папки архива
    parsed: ParsedOem
    published_at: datetime  # S3 LastModified .txt-объекта — НЕ CREATION_DATE
    etag: str
    s3_key: str
    size: int
    source_url: str


@dataclass(frozen=True)
class MetaJsonProvenance:
    """Поля ``*.meta.json``, нужные для перекрёстной проверки целостности
    (:func:`verify_fetch_integrity`) — не весь файл провенанса."""

    last_modified_header: str  # HTTP-заголовок как сохранён, например "Wed, 08 May 2024 ... GMT"
    etag_header: str  # HTTP-заголовок как сохранён, включая кавычки, например '"652c...="'
    body_sha256: str


@dataclass(frozen=True)
class OemSelection:
    """Результат отбора выпуска для запроса: выпуск + метка реконструкции.

    ``is_reconstruction=True`` — выпуск отобран путём
    :func:`select_release_for_analysis` (главный признак того, что запись
    не должна тихо использоваться как вход строгого ``historical_forecast``,
    main-prompt.md §1 «геометрия помечается как реконструкция»)."""

    release: OemRelease
    is_reconstruction: bool


_OEM_HEADER_LINE_RE = re.compile(r"^(?P<key>[A-Z_]+)\s*=\s*(?P<value>.+)$")
_S3_NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}

_REQUIRED_HEADER_FIELDS = ("CCSDS_OEM_VERS", "CREATION_DATE", "ORIGINATOR")
_REQUIRED_META_FIELDS = (
    "OBJECT_NAME",
    "OBJECT_ID",
    "CENTER_NAME",
    "REF_FRAME",
    "TIME_SYSTEM",
    "START_TIME",
    "USEABLE_START_TIME",
    "USEABLE_STOP_TIME",
    "STOP_TIME",
)


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(
            f"{field_name} must be a timezone-aware UTC datetime (.ai/main-prompt.md §1)"
        )


def _parse_oem_datetime(raw: str) -> datetime:
    """Разбирает время OEM (``TIME_SYSTEM = UTC``, без явного смещения в тексте)."""
    text = raw.strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise OemFormatError(f"unparseable OEM timestamp: {raw!r}") from exc
    if parsed.tzinfo is not None:
        return parsed.astimezone(UTC)
    return parsed.replace(tzinfo=UTC)


def _parse_state_vector_line(line: str, *, line_no: int) -> OemStateVectorRecord:
    parts = line.split()
    if len(parts) != 7:
        raise OemFormatError(
            f"line {line_no}: expected 7 whitespace-separated fields "
            f"(epoch pos_x pos_y pos_z vel_x vel_y vel_z), got {len(parts)}: {line!r}"
        )
    time_text, *number_texts = parts
    try:
        numbers = [float(value) for value in number_texts]
    except ValueError as exc:
        raise OemFormatError(f"line {line_no}: non-numeric field: {line!r}") from exc
    time = _parse_oem_datetime(time_text)
    return OemStateVectorRecord(
        time=time,
        position_km=(numbers[0], numbers[1], numbers[2]),
        velocity_km_s=(numbers[3], numbers[4], numbers[5]),
    )


def parse_oem(raw_bytes: bytes) -> ParsedOem:
    """Разбирает CCSDS OEM 2.0 KVN: заголовок, единственный блок META, узлы
    состояния (пропуская строки ``COMMENT`` — main-prompt.md §11 «не парсить
    их содержание»).

    Парсер пишется под сохранённый реальный ответ (.ai/backend-prompt.md §3)
    — ``tests/fixtures/orbit/history/`` содержит 4 реальных выпуска,
    ``tests/orbit/test_orbit_history.py`` гоняется на них.
    """
    if not raw_bytes or not raw_bytes.strip():
        raise OemFormatError("empty OEM response body (HTTP 200 with no content)")
    try:
        text = raw_bytes.decode("ascii", errors="strict")
    except UnicodeDecodeError as exc:
        raise OemFormatError(f"OEM response is not ASCII: {exc}") from exc

    lines = text.splitlines()
    n = len(lines)

    header: dict[str, str] = {}
    idx = 0
    while idx < n and lines[idx].strip() != "META_START":
        stripped = lines[idx].strip()
        match = _OEM_HEADER_LINE_RE.match(stripped) if stripped else None
        if match is not None:
            header[match.group("key")] = match.group("value").strip()
        idx += 1
    if idx >= n:
        raise OemFormatError("META_START not found in OEM response")
    idx += 1  # пропускаем META_START

    meta: dict[str, str] = {}
    while idx < n and lines[idx].strip() != "META_STOP":
        stripped = lines[idx].strip()
        match = _OEM_HEADER_LINE_RE.match(stripped) if stripped else None
        if match is not None:
            meta[match.group("key")] = match.group("value").strip()
        idx += 1
    if idx >= n:
        raise OemFormatError("META_STOP not found in OEM response")
    idx += 1  # пропускаем META_STOP

    for key in _REQUIRED_HEADER_FIELDS:
        if key not in header:
            raise OemFormatError(f"missing required header field {key!r}")
    for key in _REQUIRED_META_FIELDS:
        if key not in meta:
            raise OemFormatError(f"missing required META field {key!r}")

    if meta["TIME_SYSTEM"] != "UTC":
        raise OemFormatError(f"unsupported TIME_SYSTEM {meta['TIME_SYSTEM']!r}, expected UTC")
    if meta["REF_FRAME"] != "EME2000":
        # EME2000 совпадает с системой радиантов MMOD (модульный docstring) —
        # другая система координат сделала бы этот выпуск непригодным без
        # ещё не реализованного преобразования (main-prompt.md §11).
        raise OemFormatError(f"unsupported REF_FRAME {meta['REF_FRAME']!r}, expected EME2000")
    if meta["OBJECT_ID"] != ISS_OBJECT_ID:
        raise OemFormatError(
            f"unexpected OBJECT_ID {meta['OBJECT_ID']!r}, expected {ISS_OBJECT_ID!r} "
            f"(NORAD {ISS_NORAD_ID})"
        )

    state_vectors: list[OemStateVectorRecord] = []
    for i in range(idx, n):
        stripped = lines[i].strip()
        if not stripped or stripped.startswith("COMMENT"):
            continue
        state_vectors.append(_parse_state_vector_line(stripped, line_no=i + 1))
    if not state_vectors:
        raise OemFormatError("no state vectors found after META_STOP")
    for previous, current in zip(state_vectors, state_vectors[1:], strict=False):
        if current.time <= previous.time:
            raise OemFormatError(
                f"state vectors are not strictly increasing in time near "
                f"{current.time.isoformat()}"
            )

    return ParsedOem(
        object_name=meta["OBJECT_NAME"],
        object_id=meta["OBJECT_ID"],
        norad_id=ISS_NORAD_ID,
        center_name=meta["CENTER_NAME"],
        ref_frame=meta["REF_FRAME"],
        time_system=meta["TIME_SYSTEM"],
        originator=header["ORIGINATOR"],
        creation_date=_parse_oem_datetime(header["CREATION_DATE"]),
        start_time=_parse_oem_datetime(meta["START_TIME"]),
        useable_start_time=_parse_oem_datetime(meta["USEABLE_START_TIME"]),
        useable_stop_time=_parse_oem_datetime(meta["USEABLE_STOP_TIME"]),
        stop_time=_parse_oem_datetime(meta["STOP_TIME"]),
        state_vectors=tuple(state_vectors),
    )


def _parse_s3_last_modified(raw: str) -> datetime:
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise OemListingFormatError(f"unparseable S3 LastModified: {raw!r}") from exc
    if parsed.tzinfo is None:
        raise OemListingFormatError(f"S3 LastModified has no UTC offset: {raw!r}")
    return parsed.astimezone(UTC)


def parse_s3_listing(xml_bytes: bytes) -> list[S3ListingEntry]:
    """Разбирает S3 ``ListBucketResult`` (стандартная библиотека
    ``xml.etree.ElementTree`` — новая XML-зависимость не нужна).

    Возвращает все объекты выпуска (обычно ``.txt`` и сопутствующий
    ``.xml``); выбор конкретно ``.txt``-объекта — :func:`_find_txt_entry`.
    """
    if not xml_bytes or not xml_bytes.strip():
        raise OemListingFormatError("empty S3 listing response body")
    try:
        root = ET.fromstring(xml_bytes)  # noqa: S314 — доверенная фикстура/архив, не пользовательский ввод
    except ET.ParseError as exc:
        raise OemListingFormatError(f"S3 listing is not valid XML: {exc}") from exc

    entries: list[S3ListingEntry] = []
    for contents in root.findall("s3:Contents", _S3_NS):
        key_el = contents.find("s3:Key", _S3_NS)
        last_modified_el = contents.find("s3:LastModified", _S3_NS)
        etag_el = contents.find("s3:ETag", _S3_NS)
        size_el = contents.find("s3:Size", _S3_NS)
        if key_el is None or last_modified_el is None or etag_el is None or size_el is None:
            raise OemListingFormatError("S3 listing <Contents> is missing a required child element")
        key_text, last_modified_text, etag_text, size_text = (
            key_el.text,
            last_modified_el.text,
            etag_el.text,
            size_el.text,
        )
        if key_text is None or last_modified_text is None or etag_text is None or size_text is None:
            raise OemListingFormatError("S3 listing <Contents> child element has no text content")
        entries.append(
            S3ListingEntry(
                key=key_text,
                last_modified=_parse_s3_last_modified(last_modified_text),
                etag=etag_text.strip().strip('"'),
                size=int(size_text),
            )
        )
    if not entries:
        raise OemListingFormatError("S3 listing has no <Contents> entries")
    return entries


def _find_txt_entry(entries: list[S3ListingEntry]) -> S3ListingEntry:
    txt_entries = [entry for entry in entries if entry.key.endswith(".txt")]
    if len(txt_entries) != 1:
        raise OemListingFormatError(
            f"expected exactly one '.txt' entry in S3 listing, got {len(txt_entries)}"
        )
    return txt_entries[0]


def build_oem_release(
    parsed: ParsedOem,
    *,
    listing_entries: list[S3ListingEntry],
    release_date: str,
    source_url: str,
) -> OemRelease:
    """Собирает :class:`OemRelease`: разобранный OEM + доказанная публикация.

    ``published_at`` — S3 ``LastModified`` объекта ``.txt``, НЕ
    ``parsed.creation_date`` (см. модульный docstring и
    ``tests/fixtures/orbit/history/README.md`` «Время доступности»).
    """
    txt_entry = _find_txt_entry(listing_entries)
    return OemRelease(
        release_date=release_date,
        parsed=parsed,
        published_at=txt_entry.last_modified,
        etag=txt_entry.etag,
        s3_key=txt_entry.key,
        size=txt_entry.size,
        source_url=source_url,
    )


def _parse_http_date(raw: str) -> datetime:
    parsed = parsedate_to_datetime(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def verify_fetch_integrity(
    *, raw_bytes: bytes, meta: MetaJsonProvenance, listing_entry: S3ListingEntry
) -> None:
    """Перепроверяет три независимых указания на один и тот же объект
    (``*.meta.json`` — исходные HTTP-заголовки при скачивании; S3-листинг
    выпуска; фактические байты сохранённого файла) — то, что
    ``tests/fixtures/orbit/history/README.md`` документирует как проверенное
    вручную при подготовке фикстур («Целостность подтверждена независимо»),
    здесь становится исполняемой проверкой, а не прозой.

    Несовпадение любой из проверок — :class:`OemIntegrityError`: сохранённый
    оригинал, листинг и заявленные метаданные должны описывать один и тот же
    объект бакета (.ai/backend-prompt.md §1 «оригинал сохраняется с версией и
    контрольной суммой»).
    """
    actual_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    if actual_sha256 != meta.body_sha256:
        raise OemIntegrityError(
            f"body sha256 mismatch: meta.json says {meta.body_sha256!r}, "
            f"actual bytes hash to {actual_sha256!r}"
        )

    meta_etag = meta.etag_header.strip().strip('"')
    if meta_etag != listing_entry.etag:
        raise OemIntegrityError(
            f"ETag mismatch: meta.json response header says {meta_etag!r}, "
            f"S3 listing says {listing_entry.etag!r}"
        )

    meta_last_modified = _parse_http_date(meta.last_modified_header)
    if meta_last_modified != listing_entry.last_modified:
        raise OemIntegrityError(
            f"Last-Modified mismatch: meta.json response header says "
            f"{meta_last_modified.isoformat()}, S3 listing says "
            f"{listing_entry.last_modified.isoformat()}"
        )

    # Для однокомпонентной (не multipart) загрузки S3 ETag — это MD5 тела
    # объекта: третья, независимая от sha256/Last-Modified проверка, что
    # скачанные байты — действительно те, что зарегистрированы в бакете
    # (tests/fixtures/orbit/history/README.md «Целостность подтверждена
    # независимо»). Не криптографическая защита от подделки — только
    # перепроверка целостности уже доверенной фикстуры.
    actual_md5 = hashlib.md5(raw_bytes, usedforsecurity=False).hexdigest()
    if actual_md5 != meta_etag:
        raise OemIntegrityError(
            f"S3 ETag {meta_etag!r} does not match MD5 of the downloaded bytes {actual_md5!r} "
            "— for a single-part upload these must be equal"
        )


def select_release_for_forecast(
    releases: list[OemRelease],
    *,
    as_of: datetime,
    interval_start: datetime,
    interval_end: datetime,
) -> OemRelease | None:
    """Отбор для строгого ``historical_forecast`` (main-prompt.md §1).

    Пригоден выпуск, у которого одновременно:
    - ``published_at <= as_of`` (S3 ``LastModified``, НЕ ``CREATION_DATE``);
    - ``[USEABLE_START_TIME, USEABLE_STOP_TIME]`` покрывает запрошенный
      расчётный интервал целиком (частичное покрытие не годится — не
      «доливается» ни более поздним выпуском, ни современными элементами).

    Среди пригодных возвращается выпуск с максимальным ``published_at``.
    ``None``, если пригодных нет — критический пробел, не повод брать
    более поздний или непокрывающий выпуск (см. тест на утечку времени,
    tests/orbit/test_orbit_history.py).
    """
    _require_aware(as_of, "as_of")
    _require_aware(interval_start, "interval_start")
    _require_aware(interval_end, "interval_end")

    eligible = [
        release
        for release in releases
        if release.published_at <= as_of
        and release.parsed.useable_start_time <= interval_start
        and release.parsed.useable_stop_time >= interval_end
    ]
    if not eligible:
        return None
    return max(eligible, key=lambda release: release.published_at)


def select_release_for_analysis(
    releases: list[OemRelease], *, moment: datetime
) -> OemRelease | None:
    """Отбор для ``historical_analysis`` — отдельная ветка от
    :func:`select_release_for_forecast` (main-prompt.md §1 «Последующие
    наблюдения — отдельная ветка кода»): НЕ фильтруется по ``as_of``.

    Среди выпусков, чей ``[USEABLE_START_TIME, USEABLE_STOP_TIME]`` покрывает
    ``moment``, предпочитается созданный вскоре ПОСЛЕ момента (минимальный
    неотрицательный ``CREATION_DATE - moment``) — ближе к фактически
    прошедшей траектории, чем давний прогноз на этот момент
    (tests/fixtures/orbit/history/README.md «Прогноз, а не наблюдение»). Если
    ни один покрывающий выпуск не создан после момента (весь архив за этот
    интервал — более ранние прогнозы), берётся созданный последним из
    покрывающих — тоже реконструкция, но самая свежая из доступных.

    Вызывающая сторона обязана пометить запись, построенную по этому пути,
    как ``quality="reconstructed"`` (:func:`build_oem_orbital_elements_record`)
    — она не заменяет вход строгого ``historical_forecast``.
    """
    _require_aware(moment, "moment")

    covering = [
        release
        for release in releases
        if release.parsed.useable_start_time <= moment <= release.parsed.useable_stop_time
    ]
    if not covering:
        return None

    created_after = [
        release for release in covering if release.parsed.creation_date >= moment
    ]
    if created_after:
        return min(created_after, key=lambda release: release.parsed.creation_date)
    return max(covering, key=lambda release: release.parsed.creation_date)


def _oem_state_vectors_payload(parsed: ParsedOem) -> list[dict[str, object]]:
    """Структурированные узлы для повторного использования интерполятором
    без обратного разбора текста OEM при каждом чтении записи (см.
    ``$defs/orbitalElementsValue`` в ``contracts/record.schema.json`` —
    ``additionalProperties: true`` сверх обязательного ``raw``)."""
    return [
        {
            "time": vector.time.isoformat().replace("+00:00", "Z"),
            "position_km": list(vector.position_km),
            "velocity_km_s": list(vector.velocity_km_s),
        }
        for vector in parsed.state_vectors
    ]


def build_oem_orbital_elements_record(
    release: OemRelease,
    *,
    raw_bytes: bytes,
    fetched_at: datetime,
    quality: Quality = "nominal",
) -> RecordInput:
    """Строит запись источника (``record_kind=orbital_elements``,
    ``orbital_elements_meta.format="OEM"``) для сохранения в ``store``.

    Четыре времени записи (main-prompt.md §1) для OEM-выпуска:

    - ``observed_at`` = ``CREATION_DATE`` — момент, когда была определена ЭТА
      траектория (ближайший аналог «измерения» у продукта, который сам по
      себе является прогнозом траектории, см. README фикстур «Прогноз, а не
      наблюдение»), а не момент публикации;
    - ``valid_from``/``valid_to`` = ``USEABLE_START_TIME``/``USEABLE_STOP_TIME``
      — интервал, на который выпуск пригоден к использованию;
    - ``published_at`` = ``release.published_at`` (S3 ``LastModified`` —
      см. модульный docstring, НИКОГДА ``CREATION_DATE``);
    - ``fetched_at`` — аргумент вызывающей стороны.

    ``orbital_elements_meta.epoch`` = ``CREATION_DATE`` (контракт требует
    ровно одно поле «эпоха»; для OEM это референсный момент баллистика,
    выбор задокументирован в ``contracts/record.schema.json``, тот же выбор,
    что и ``observed_at`` выше — оба поля намеренно совпадают, а не
    расходятся тихо).

    ``source_version`` включает S3-ключ выпуска, ``CREATION_DATE`` и
    ``ETag`` — три поля вместе отличают разные версии S3-объекта одного и
    того же датированного выпуска (``ETag``/``CREATION_DATE`` меняются при
    повторной загрузке того же выпуска поставщиком), не только
    ``CREATION_DATE`` в одиночку и не только имя датированной папки.

    ``quality`` — по умолчанию ``"nominal"``; вызывающая сторона обязана
    передать ``"reconstructed"`` для записей, отобранных
    :func:`select_release_for_analysis` (main-prompt.md §1).
    """
    parsed = release.parsed
    raw_text = raw_bytes.decode("ascii")
    value: dict[str, object] = {
        "raw": raw_text,
        "state_vectors": _oem_state_vectors_payload(parsed),
        "useable_start_time": parsed.useable_start_time.isoformat().replace("+00:00", "Z"),
        "useable_stop_time": parsed.useable_stop_time.isoformat().replace("+00:00", "Z"),
    }
    spatial_context: dict[str, object] = {
        "norad_id": parsed.norad_id,
        "object_id": parsed.object_id,
        "object_name": parsed.object_name,
        "center_name": parsed.center_name,
        "originator": parsed.originator,
        "s3_key": release.s3_key,
        "etag": release.etag,
        "publication_evidence": PUBLICATION_EVIDENCE,
        "publication_evidence_note": PUBLICATION_EVIDENCE_NOTE,
    }
    source_version = f"{release.s3_key}:{parsed.creation_date.isoformat()}:{release.etag}"

    return RecordInput(
        provider_record_id=f"iss-oem-{release.release_date}",
        source_id=SOURCE_ID,
        source_url=release.source_url,
        record_kind="orbital_elements",
        observed_at=parsed.creation_date,
        valid_from=parsed.useable_start_time,
        valid_to=parsed.useable_stop_time,
        published_at=release.published_at,
        fetched_at=fetched_at,
        value=value,
        unit=None,
        spatial_context=spatial_context,
        source_version=source_version,
        quality=quality,
        raw_bytes=raw_bytes,
        orbital_elements_meta={
            "epoch": parsed.creation_date.isoformat().replace("+00:00", "Z"),
            "format": "OEM",
            "coordinate_system": parsed.ref_frame,
        },
    )


__all__ = [
    "ISS_NORAD_ID",
    "ISS_OBJECT_ID",
    "OEM_URL_TEMPLATE",
    "PUBLICATION_EVIDENCE",
    "PUBLICATION_EVIDENCE_NOTE",
    "SOURCE_ID",
    "MetaJsonProvenance",
    "OemFormatError",
    "OemIntegrityError",
    "OemListingFormatError",
    "OemRelease",
    "OemSelection",
    "OemStateVectorRecord",
    "ParsedOem",
    "S3ListingEntry",
    "build_oem_orbital_elements_record",
    "build_oem_release",
    "parse_oem",
    "parse_s3_listing",
    "select_release_for_analysis",
    "select_release_for_forecast",
    "verify_fetch_integrity",
]
