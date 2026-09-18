"""Зонд пригодности архивов космопогоды для строгого replay (FN-23, S1-05).

Отвечает на один вопрос: можно ли по этому архиву честно реализовать
``historical_forecast`` (.ai/main-prompt.md §1) — то есть есть ли у продукта
подтверждаемое время выпуска, и виден ли по архиву охват обязательного
периода 01.05–30.06.2024 с его пробелами. Модуль **не** реализует
production-шлюз наподобие ``src/sources/swpc.py::fetch_and_store``
(периодический refresh, TTL, заморозка/отключение) — main-prompt.md §11
прямо ограничивает объём этой задачи: «Это доказательство доступности, не
реализованный replay всего сервиса». Постраничная загрузка всего периода в
хранилище — задача этапа 3, см. docs/method.md §5 «Способ загрузки».

Два кандидата, оба подтверждены на реальных сохранённых ответах
(``tests/fixtures/sources/archive/``, см. README этой папки о происхождении):

- **NASA CCMC DONKI** — уведомления, каждое несёт ``messageIssueTime``.
  Основная линия строгого replay: архив покрывает весь обязательный период.
- **NOAA SWPC Forecast Discussion** (архив NCEI) — время публикации берётся
  из строки ``:Issued:`` внутри тела бюллетеня, а не из имени файла или
  времени запроса. Дополнительная линия: в архиве есть подтверждённый
  пробел 15.05–16.06.2024 (нет ни одного выпуска), который зонд обязан
  показать, а не скрыть (main-prompt.md §2, §5).

Обе линии нормализуются в ``RecordInput`` (``src/store/records.py``) и
пригодны для реальной вставки в хранилище тем же путём, что и любой другой
источник — ``select_as_of`` уже реализует и уже протестирован
(``tests/store/test_as_of.py``) на правиле «``published_at <= as_of`` и
``replay_eligible``»; этот модуль не переизобретает это правило, а только
поставляет ему корректно нормализованные записи.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from src.store.records import RecordInput

DONKI_SOURCE_ID = "nasa-donki-notifications"
SWPC_ARCHIVE_SOURCE_ID = "noaa-swpc-forecast-discussion-archive"

UTC = timezone.utc


class ArchiveFormatError(RuntimeError):
    """Архивный ответ — не то, что ожидает парсер, либо не несёт разбираемого
    времени публикации.

    main-prompt.md §1: неизвестное время публикации — «непригодно», не
    «вероятно было доступно». Запись, чьё время выпуска не удалось разобрать,
    здесь не превращается в ``published_at=None`` и не сохраняется как
    номинально нормализованная — она отклоняется на границе получения той
    же ошибкой, что и структурно неожиданный ответ: обе ситуации в равной
    мере не могут подтвердить «выпущено до ``as_of``».
    """


# ---------------------------------------------------------------------------
# NASA CCMC DONKI — уведомления
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DonkiNotification:
    """Одно нормализованное уведомление DONKI."""

    message_id: str
    message_type: str
    issue_time: datetime  # UTC-aware
    url: str
    raw_entry: dict[str, Any]


def parse_donki_notifications(raw_bytes: bytes) -> list[DonkiNotification]:
    """Разбирает ответ ``DONKI/notifications`` (список уведомлений).

    Пустое тело и не-JSON-массив — :class:`ArchiveFormatError`, как и
    отсутствие обязательных полей у отдельного уведомления (аналогично
    ``src/sources/swpc.py::parse_response`` — тихий пустой список
    неотличим от «источник поменял формат», main-prompt.md §2).
    """
    if not raw_bytes or not raw_bytes.strip():
        raise ArchiveFormatError("empty response body (HTTP 200 with no content)")
    try:
        data: Any = json.loads(raw_bytes)
    except json.JSONDecodeError as exc:
        raise ArchiveFormatError(f"response is not valid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise ArchiveFormatError(
            f"expected a JSON array of notifications, got {type(data).__name__}"
        )

    notifications: list[DonkiNotification] = []
    for index, entry in enumerate(data):
        if not isinstance(entry, dict):
            raise ArchiveFormatError(f"entry #{index} is not a JSON object")
        try:
            message_id = str(entry["messageID"])
            message_type = str(entry["messageType"])
            issue_raw = str(entry["messageIssueTime"])
            url = str(entry["messageURL"])
        except KeyError as exc:
            raise ArchiveFormatError(f"entry #{index} is missing field {exc}") from exc
        issue_time = _parse_donki_issue_time(issue_raw, index=index)
        notifications.append(
            DonkiNotification(
                message_id=message_id,
                message_type=message_type,
                issue_time=issue_time,
                url=url,
                raw_entry=entry,
            )
        )
    return notifications


def _parse_donki_issue_time(raw: str, *, index: int) -> datetime:
    """Разбирает ``messageIssueTime`` (ISO 8601, обычно с точностью до минуты).

    Наблюдение на реальных фикстурах (tests/fixtures/sources/archive/README.md):
    это поле округлено до минуты, тогда как то же время внутри
    ``messageBody`` (строка ``## Message Issue Date:``) несёт секунды. Здесь
    используется поле верхнего уровня как задокументированное машиночитаемое
    время публикации — разбор текста тела письма для секундной точности не
    требуется правилу ``published_at <= as_of`` (минутной точности достаточно
    для отсечения по часу-минуте, которого требует §11) и это ограничение
    явно фиксируется, а не скрывается.
    """
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ArchiveFormatError(
            f"entry #{index} has an unparseable messageIssueTime: {raw!r}"
        ) from exc
    if parsed.tzinfo is None:
        raise ArchiveFormatError(
            f"entry #{index} messageIssueTime has no UTC offset: {raw!r}"
        )
    return parsed.astimezone(UTC)


def _canonical_entry_bytes(entry: dict[str, Any]) -> bytes:
    """Детерминированная сериализация одной записи (не всего массива-ответа)."""
    return json.dumps(entry, sort_keys=True, ensure_ascii=False).encode("utf-8")


def donki_notification_to_record_input(
    notification: DonkiNotification, *, source_url: str, fetched_at: datetime
) -> RecordInput:
    """Нормализует уведомление DONKI в :class:`RecordInput`.

    ``record_kind = "warning"``: уведомление DONKI — предупреждение о
    зафиксированном или ожидаемом явлении, а не первичное измерение и не
    отдельный количественный прогноз команды (.ai/main-prompt.md §4).
    Точная классификация по ``messageType`` (наблюдение события vs. прогноз
    его воздействия) — работа зоны 3 при интерпретации, не этого зонда
    (.ai/main-prompt.md §8 «получение не считает физику»).

    ``observed_at``/``valid_from``/``valid_to`` намеренно равны времени
    публикации: извлечение фактического времени события требует разбора
    свободного текста ``messageBody`` по каждому ``messageType`` отдельно
    (даты внутри тела не структурированы одинаково для CME/FLR/GST/…) — это
    явно вне объёма зонда пригодности и зафиксировано как ограничение
    (docs/method.md), а не подменено правдоподобным, но не разобранным
    значением.

    ``source_version`` — время публикации в фиксированном формате: у DONKI
    нет ``ETag``/``Last-Modified`` (подтверждено по реальным ответам, см.
    README фикстур), а ``messageID`` уже уникален и неизменяем у поставщика,
    так что версия нужна лишь как непустая метка одной публикации, не как
    инструмент разрешения дублей.
    """
    source_version = notification.issue_time.strftime("%Y%m%dT%H%M%SZ")
    return RecordInput(
        provider_record_id=notification.message_id,
        source_id=DONKI_SOURCE_ID,
        source_url=source_url,
        record_kind="warning",
        observed_at=notification.issue_time,
        valid_from=notification.issue_time,
        valid_to=notification.issue_time,
        published_at=notification.issue_time,
        fetched_at=fetched_at,
        value=None,
        unit=None,
        spatial_context={
            "provider": "NASA CCMC DONKI",
            "message_type": notification.message_type,
            "message_url": notification.url,
        },
        source_version=source_version,
        quality="nominal",
        raw_bytes=_canonical_entry_bytes(notification.raw_entry),
    )


# ---------------------------------------------------------------------------
# NOAA SWPC Forecast Discussion (архив NCEI)
# ---------------------------------------------------------------------------

_ISSUED_LINE_RE = re.compile(r"^:Issued:\s*(?P<value>.+?)\s*$", re.MULTILINE)
_PRODUCT_LINE_RE = re.compile(r"^:Product:\s*(?P<value>.+?)\s*$", re.MULTILINE)
_ISSUED_VALUE_RE = re.compile(
    r"^(?P<year>\d{4})\s+(?P<month>[A-Za-z]{3})\s+(?P<day>\d{1,2})\s+"
    r"(?P<hhmm>\d{3,4})\s+UTC$"
)
_MONTH_BY_ABBR = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}


@dataclass(frozen=True)
class SwpcForecastDiscussion:
    """Один нормализованный выпуск Forecast Discussion."""

    product: str
    issued_at: datetime  # UTC-aware, из строки ``:Issued:``
    raw_text: str


def parse_swpc_forecast_discussion(raw_bytes: bytes) -> SwpcForecastDiscussion:
    """Разбирает текстовый бюллетень SWPC Forecast Discussion.

    Время публикации — **только** из строки ``:Issued:`` внутри тела, не из
    имени файла (в имени время выпуска слота, а не факта публикации — они
    расходятся, tests/fixtures/sources/archive/README.md: выпуск за
    «...0030» 10 мая фактически помечен как выпущенный в 00:35 UTC) и не из
    времени HTTP-запроса. Отсутствие разбираемой строки ``:Issued:`` — не
    признак «время публикации неизвестно, но запись всё равно годится» — это
    :class:`ArchiveFormatError`: для этого архива задокументировано, что
    ``:Issued:`` есть всегда (см. README фикстур), и его отсутствие в
    полученном ответе означает или порчу передачи, или принципиально другой
    документ — то же обращение, что и со структурно неожиданным ответом
    (.ai/backend-prompt.md §3).
    """
    if not raw_bytes or not raw_bytes.strip():
        raise ArchiveFormatError("empty response body (HTTP 200 with no content)")
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ArchiveFormatError(f"response is not valid UTF-8 text: {exc}") from exc

    issued_match = _ISSUED_LINE_RE.search(text)
    if issued_match is None:
        raise ArchiveFormatError(
            "no ':Issued:' line found in bulletin body — publication time is "
            "unknown for this response (main-prompt.md §1: unknown publication "
            "time means 'not suitable', not 'probably available')"
        )
    issued_at = _parse_swpc_issued_value(issued_match.group("value"))

    product_match = _PRODUCT_LINE_RE.search(text)
    product = product_match.group("value") if product_match else "unknown"

    return SwpcForecastDiscussion(product=product, issued_at=issued_at, raw_text=text)


def _parse_swpc_issued_value(raw: str) -> datetime:
    """Разбирает значение вида ``2024 May 10 0035 UTC``."""
    match = _ISSUED_VALUE_RE.match(raw.strip())
    if match is None:
        raise ArchiveFormatError(f"unrecognized ':Issued:' value: {raw!r}")
    month = _MONTH_BY_ABBR.get(match.group("month"))
    if month is None:
        raise ArchiveFormatError(f"unrecognized month name in ':Issued:' value: {raw!r}")
    hhmm = match.group("hhmm").zfill(4)
    hour, minute = int(hhmm[:2]), int(hhmm[2:])
    if hour > 23 or minute > 59:
        raise ArchiveFormatError(f"unrecognized time-of-day in ':Issued:' value: {raw!r}")
    return datetime(
        int(match.group("year")), month, int(match.group("day")), hour, minute, tzinfo=UTC
    )


def swpc_forecast_discussion_to_record_input(
    discussion: SwpcForecastDiscussion, *, source_url: str, fetched_at: datetime
) -> RecordInput:
    """Нормализует выпуск Forecast Discussion в :class:`RecordInput`.

    ``source_version`` — сам ``:Issued:`` в фиксированном формате: архив не
    хранит повторных версий одного слота (README фикстур), у продукта нет
    отдельного номера ревизии.
    """
    source_version = discussion.issued_at.strftime("%Y%m%dT%H%M%SZ")
    return RecordInput(
        provider_record_id=f"forecast-discussion:{source_version}",
        source_id=SWPC_ARCHIVE_SOURCE_ID,
        source_url=source_url,
        record_kind="forecast",
        observed_at=discussion.issued_at,
        valid_from=discussion.issued_at,
        valid_to=discussion.issued_at + timedelta(hours=12),
        published_at=discussion.issued_at,
        fetched_at=fetched_at,
        value=None,
        unit=None,
        spatial_context={"provider": "NOAA SWPC", "product": discussion.product},
        source_version=source_version,
        quality="nominal",
        raw_bytes=discussion.raw_text.encode("utf-8"),
    )


_LISTING_ROW_RE = re.compile(
    r'<a href="(?P<filename>(?P<slot>\d{12})forecast_discussion\.txt)">'
)


def parse_swpc_forecast_discussion_listing(html_bytes: bytes) -> list[date]:
    """Разбирает HTML-листинг каталога NCEI в список дат слотов (по имени файла).

    Не читает тело ни одного бюллетеня и не даёт ``published_at`` (в имени
    файла — слот запроса, не время выпуска, см. модульный docstring и
    :func:`parse_swpc_forecast_discussion`). Единственное назначение —
    независимое от выборочно скачанных тел доказательство того, **какие
    слоты вообще существуют в архиве**: 32-дневный пробел
    (tests/fixtures/sources/archive/README.md) подтверждается тем, что в
    листинге каталога попросту нет файлов с именами вида
    ``202405{15..31}...`` и ``202406{01..16}...``, а не тем, что эта задача
    не скачала их тела.
    """
    if not html_bytes or not html_bytes.strip():
        raise ArchiveFormatError("empty response body (HTTP 200 with no content)")
    try:
        text = html_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ArchiveFormatError(f"response is not valid UTF-8 text: {exc}") from exc

    slots: list[date] = []
    for match in _LISTING_ROW_RE.finditer(text):
        slot_text = match.group("slot")
        year, month, day = int(slot_text[0:4]), int(slot_text[4:6]), int(slot_text[6:8])
        slots.append(date(year, month, day))
    if not slots:
        raise ArchiveFormatError(
            "no 'forecast_discussion.txt' entries found in directory listing — "
            "directory structure may have changed"
        )
    return slots


# ---------------------------------------------------------------------------
# Карта наличия/пробелов
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GapRun:
    """Непрерывный ряд дней без публикаций одного источника внутри окна."""

    first_day: date
    last_day: date

    @property
    def length_days(self) -> int:
        return (self.last_day - self.first_day).days + 1


@dataclass(frozen=True)
class CoverageReport:
    """Карта наличия/пробелов одного источника за обязательный период.

    ``daily_counts`` — число публикаций на каждый день окна (включая дни с
    нулём — присутствие в словаре, а не отсутствие ключа, обязательно, иначе
    «нет записи о дне» неотличимо от «день не входит в окно»). ``gap_runs`` —
    непрерывные ряды дней с нулём публикаций, уже сгруппированные, чтобы
    32-дневный пробел не приходилось искать по 32 отдельным нулям.
    """

    window_start: date
    window_end: date
    daily_counts: dict[date, int]
    gap_runs: tuple[GapRun, ...]

    @property
    def covered_days(self) -> int:
        return sum(1 for count in self.daily_counts.values() if count > 0)

    @property
    def total_days(self) -> int:
        return (self.window_end - self.window_start).days + 1


def build_coverage_report(
    publish_times: Iterable[datetime | date], *, window_start: date, window_end: date
) -> CoverageReport:
    """Строит карту наличия/пробелов по дням в окне ``[window_start, window_end]``.

    Принимает как моменты публикации (``datetime``, приводится к дате в UTC —
    например от :class:`DonkiNotification`/:class:`SwpcForecastDiscussion`),
    так и уже готовые даты (``date`` — например от
    :func:`parse_swpc_forecast_discussion_listing`, где известно только
    какой слот существует в архиве, без времени публикации внутри него).

    Публикация вне окна не учитывается и не расширяет окно молча — окно
    задаётся вызывающей стороной явно (обязательный период 01.05–30.06.2024,
    main-prompt.md §11), а не выводится из данных.
    """
    if window_end < window_start:
        raise ValueError("window_end must not be before window_start")

    daily_counts: dict[date, int] = {}
    day = window_start
    while day <= window_end:
        daily_counts[day] = 0
        day += timedelta(days=1)

    for moment in publish_times:
        day = moment.astimezone(UTC).date() if isinstance(moment, datetime) else moment
        if window_start <= day <= window_end:
            daily_counts[day] += 1

    gap_runs: list[GapRun] = []
    run_start: date | None = None
    day = window_start
    while day <= window_end:
        if daily_counts[day] == 0:
            if run_start is None:
                run_start = day
        else:
            if run_start is not None:
                gap_runs.append(GapRun(first_day=run_start, last_day=day - timedelta(days=1)))
                run_start = None
        day += timedelta(days=1)
    if run_start is not None:
        gap_runs.append(GapRun(first_day=run_start, last_day=window_end))

    return CoverageReport(
        window_start=window_start,
        window_end=window_end,
        daily_counts=daily_counts,
        gap_runs=tuple(gap_runs),
    )


__all__ = [
    "DONKI_SOURCE_ID",
    "SWPC_ARCHIVE_SOURCE_ID",
    "ArchiveFormatError",
    "CoverageReport",
    "DonkiNotification",
    "GapRun",
    "SwpcForecastDiscussion",
    "build_coverage_report",
    "donki_notification_to_record_input",
    "parse_donki_notifications",
    "parse_swpc_forecast_discussion",
    "parse_swpc_forecast_discussion_listing",
    "swpc_forecast_discussion_to_record_input",
]
