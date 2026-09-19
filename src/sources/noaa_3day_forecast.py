"""Коннектор NOAA SWPC «3-Day Forecast»: суточная вероятность S1+ (FN-31, S2-01).

Реализует обязательное изменение №1 из ``docs/decision/01_solution.md`` и
``03_verdict.md`` (main-prompt.md §11 «Механизм 1. Радиационная обстановка»,
абзац «Прогноз … минимум на 6 часов»): отдельная от наблюдения GOES
(``src/sources/swpc.py``, поток протонов >=10 МэВ) линия внешнего прогноза —
текстовый бюллетень NOAA SWPC «3-Day Forecast», раздел B «NOAA Solar
Radiation Storm Forecast», строка «S1 or greater» — суточная вероятность
события уровня S1 и выше на каждый из трёх прогнозных дней.

**Обязательная семантика этой задачи (main-prompt.md §1, §4; FN-31):**

- Суточная вероятность (0–100%) НЕ делится по часам, НЕ умножается на
  длительность окна ВКД, НЕ суммируется через полночь и нигде в этом модуле
  не называется «вероятностью ВКД» — она проходит через получение и
  хранение как есть, ровно как опубликована.
- ``published_at``/``valid_from``/``valid_to``/``fetched_at``/``observed_at``
  различаются и не подменяют друг друга (см. :func:`noaa_3day_forecast_to_record_inputs`).
- Наблюдение (GOES pfu, ``src/sources/swpc.py``) и этот внешний прогноз —
  разные ``record_kind`` (``observation`` против ``forecast``), разные
  единицы (``pfu`` против ``percent``) и разные источники в ``sources.yaml``
  — смешать их на уровне записи структурно невозможно.
- ``historical_forecast`` использует только версии с ``published_at <= as_of``
  — это обеспечивает уже существующее и протестированное правило
  ``src/store/records.py::select_as_of`` без изменений в самом правиле:
  этот модуль лишь производит корректно версионированные записи (см.
  docstring :func:`noaa_3day_forecast_to_record_inputs` про выбор
  ``provider_record_id``/``source_version``).

**Известное ограничение этой сессии (см. также ``docs/method.md`` §7 и
README).** Прямого доступа к сети (``services.swpc.noaa.gov``,
``www.ngdc.noaa.gov``) в песочнице, где готовился этот PR, нет — исходящий
egress-прокси отвечает ``403`` на оба хоста (та же ситуация, что
задокументирована в README «Известное ограничение окружения сборки» для
FN-28, и уже встречалась в FN-23/FN-24). Парсер и коннектор ниже реализованы
по полностью документированному и стабильному публичному текстовому формату
продукта (тот же заголовочный стиль ``:Product:``/``:Issued:``, что и у уже
проверенного ``src/sources/archive_probe.py::parse_swpc_forecast_discussion``),
и протестированы на **синтетических**, явно помеченных как синтетические,
фикстурах (``tests/fixtures/sources/noaa_3day_forecast/synthetic/``) — они
не выдаются за реальные архивные ответы. Приёмка FN-31 п.1 («парсер проверен
на сохранённых реальных текущем и историческом выпусках, включая
2024-05-10 12:30 UTC») и п.2 («карта пробелов мая-июня 2024 по телам
выпусков») этой сессией **не закрыты** — это явно перечислено как
оставшаяся работа в PR (main-prompt.md §2: пробел не маскируется под
выполненное условие).
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

import httpx
import yaml

from src.sources.http import (
    SourceHttpError,
    SourceQuotaLimitedError,
    SourceTimeoutError,
    fetch,
)
from src.sources.status import (
    SourceStatus,
    SourceStatusRegistry,
    effective_status,
    staleness_seconds,
)
from src.store.records import (
    DuplicateKeyConflictError,
    RawOriginalStore,
    RecordInput,
    insert_record,
)

UTC = timezone.utc

#: Живой (текущий) продукт NOAA SWPC.
SOURCE_ID = "noaa-swpc-3day-forecast"
#: Архивный (исторический) источник того же продукта (NCEI) — та же форма
#: бюллетеня, отдельная регистрация в sources.yaml (main-prompt.md §7:
#: каждый источник — отдельная запись реестра, даже если парсер общий).
ARCHIVE_SOURCE_ID = "noaa-swpc-3day-forecast-archive"

_DEFAULT_SOURCES_YAML = Path(__file__).resolve().parent.parent.parent / "sources.yaml"

_ROW_LABEL = "S1 or greater"
_UNIT = "percent"

Outcome = Literal[
    "stored",
    "skipped_disabled",
    "skipped_frozen",
    "skipped_fresh",
    "skipped_quota_cooldown",
    "error_timeout",
    "error_quota",
    "error_format",
    "error_http",
    "error_conflict",
]


class Noaa3DayForecastFormatError(RuntimeError):
    """Ответ — не то, что ожидает парсер этого продукта (main-prompt.md §5).

    Покрывает пустое тело 200, отсутствие строки ``:Issued:``, отсутствие
    или неожиданную форму секции «Solar Radiation Storm Forecast» / строки
    «S1 or greater». Вероятность, записанная не как ``\\d{1,3}%`` (например
    ``<1%`` — такая форма встречается в реальных бюллетенях NOAA для других
    строк, но не подтверждена для строки S1 в этой сессии из-за отсутствия
    сетевого доступа), тоже поднимает эту ошибку, а не подставляет
    придуманное число — main-prompt.md §1 «пропуск не заменяется нулём»
    распространяется и на «неразбираемое значение не заменяется
    приблизительным».
    """


# ---------------------------------------------------------------------------
# Разбор заголовка бюллетеня (":Issued:") — тот же стиль, что и у
# src/sources/archive_probe.py::parse_swpc_forecast_discussion, отдельная
# копия ради независимости модулей источника (main-prompt.md §8).
# ---------------------------------------------------------------------------

_ISSUED_LINE_RE = re.compile(r"^:Issued:\s*(?P<value>.+?)\s*$", re.MULTILINE)
_ISSUED_VALUE_RE = re.compile(
    r"^(?P<year>\d{4})\s+(?P<month>[A-Za-z]{3})\s+(?P<day>\d{1,2})\s+"
    r"(?P<hhmm>\d{3,4})\s+UTC$"
)
_MONTH_BY_ABBR = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}

_HEADER_DATE_RE = r"(?P<month>[A-Za-z]{3})\s+(?P<day>\d{1,2})"
_S1_TABLE_RE = re.compile(
    r"Solar Radiation Storm Forecast for Next 3 Days:[ \t]*\n"
    rf"[ \t]*{_HEADER_DATE_RE.replace('month', 'h1m').replace('day', 'h1d')}"
    rf"[ \t]+{_HEADER_DATE_RE.replace('month', 'h2m').replace('day', 'h2d')}"
    rf"[ \t]+{_HEADER_DATE_RE.replace('month', 'h3m').replace('day', 'h3d')}[ \t]*\n"
    r"S1 or greater[ \t]+(?P<p1>\d{1,3})%[ \t]+(?P<p2>\d{1,3})%[ \t]+(?P<p3>\d{1,3})%"
)


def _parse_issued_at(text: str) -> datetime:
    match = _ISSUED_LINE_RE.search(text)
    if match is None:
        raise Noaa3DayForecastFormatError(
            "no ':Issued:' line found in bulletin body — publication time is "
            "unknown for this response (main-prompt.md §1: unknown publication "
            "time means 'not suitable', not 'probably available')"
        )
    raw = match.group("value").strip()
    value_match = _ISSUED_VALUE_RE.match(raw)
    if value_match is None:
        raise Noaa3DayForecastFormatError(f"unrecognized ':Issued:' value: {raw!r}")
    month = _MONTH_BY_ABBR.get(value_match.group("month"))
    if month is None:
        raise Noaa3DayForecastFormatError(f"unrecognized month name in ':Issued:' value: {raw!r}")
    hhmm = value_match.group("hhmm").zfill(4)
    hour, minute = int(hhmm[:2]), int(hhmm[2:])
    if hour > 23 or minute > 59:
        raise Noaa3DayForecastFormatError(f"unrecognized time-of-day in ':Issued:' value: {raw!r}")
    return datetime(
        int(value_match.group("year")), month, int(value_match.group("day")),
        hour, minute, tzinfo=UTC,
    )


def _resolve_forecast_day(month_abbr: str, day_str: str, *, issued_at: datetime) -> date:
    """Восстанавливает календарную дату колонки таблицы (без года в тексте).

    Горизонт продукта — трое суток, поэтому единственный возможный перенос
    года — бюллетень, выпущенный в декабре, прогнозирующий январь. Более
    общая проверка (дата получилась в прошлом относительно суток выпуска)
    защищает и от этого, и от любого другого однократного переноса без
    жёсткого предположения «месяц == 12» (main-prompt.md §1 — не гадать,
    но и не привязываться к частному случаю сильнее, чем нужно).
    """
    month = _MONTH_BY_ABBR.get(month_abbr)
    if month is None:
        raise Noaa3DayForecastFormatError(
            f"unrecognized month name in table header: {month_abbr!r}"
        )
    day = int(day_str)
    year = issued_at.year
    try:
        candidate = date(year, month, day)
    except ValueError as exc:
        raise Noaa3DayForecastFormatError(
            f"invalid table header date: {month_abbr} {day_str}"
        ) from exc
    if candidate < issued_at.date() - timedelta(days=1):
        candidate = date(year + 1, month, day)
    return candidate


@dataclass(frozen=True)
class DailyS1PlusProbability:
    """Одна колонка таблицы «Solar Radiation Storm Forecast», строка S1+.

    ``day_index`` — позиция в бюллетене (1..3, «Day 1»/«Day 2»/«Day 3»),
    сохраняется для прослеживаемости, но НЕ используется как замена
    ``forecast_day`` — постановка требует показывать исходные прогнозные
    дни, а не относительные номера.
    """

    forecast_day: date
    day_index: Literal[1, 2, 3]
    probability_percent: float


@dataclass(frozen=True)
class Noaa3DayForecast:
    """Один нормализованный бюллетень «3-Day Forecast»."""

    issued_at: datetime
    probabilities: tuple[DailyS1PlusProbability, DailyS1PlusProbability, DailyS1PlusProbability]
    raw_text: str


def parse_noaa_3day_forecast(raw_bytes: bytes) -> Noaa3DayForecast:
    """Разбирает текстовый бюллетень NOAA SWPC «3-Day Forecast».

    Тот же принцип, что и у ``archive_probe.parse_swpc_forecast_discussion``:
    время публикации — только из строки ``:Issued:`` внутри тела, никогда из
    времени HTTP-запроса. Извлекается ровно раздел B, строка «S1 or greater»
    — остальные разделы бюллетеня (геомагнитная активность Kp, радиошумовые
    блэкауты R) не разбираются этим коннектором: main-prompt.md §11 отводит
    Kp отдельным контекстом, а R-шкале — отдельным контекстом без
    собственного уровня Механизма 1; извлекать их значения здесь означало бы
    расширять эту задачу за пределы «конкретной линии … NOAA 3-Day Forecast
    с S1+».

    Пустое тело, отсутствие ``:Issued:`` или отсутствие/неожиданная форма
    таблицы S1+ — :class:`Noaa3DayForecastFormatError`, не пустой результат
    (main-prompt.md §2: смена формата источника не должна читаться как
    «нет данных»).
    """
    if not raw_bytes or not raw_bytes.strip():
        raise Noaa3DayForecastFormatError("empty response body (HTTP 200 with no content)")
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise Noaa3DayForecastFormatError(f"response is not valid UTF-8 text: {exc}") from exc

    issued_at = _parse_issued_at(text)

    normalized = text.replace("\r\n", "\n")
    table_match = _S1_TABLE_RE.search(normalized)
    if table_match is None:
        raise Noaa3DayForecastFormatError(
            "no parseable 'Solar Radiation Storm Forecast ... S1 or greater' "
            "table found in bulletin body — source format may have changed"
        )

    columns = (
        (table_match.group("h1m"), table_match.group("h1d"), table_match.group("p1")),
        (table_match.group("h2m"), table_match.group("h2d"), table_match.group("p2")),
        (table_match.group("h3m"), table_match.group("h3d"), table_match.group("p3")),
    )
    probabilities: list[DailyS1PlusProbability] = []
    for index, (month_abbr, day_str, percent_str) in enumerate(columns, start=1):
        percent = float(percent_str)
        if not (0.0 <= percent <= 100.0):
            raise Noaa3DayForecastFormatError(f"S1+ probability out of range: {percent_str}%")
        forecast_day = _resolve_forecast_day(month_abbr, day_str, issued_at=issued_at)
        day_index: Literal[1, 2, 3] = index  # type: ignore[assignment]
        probabilities.append(
            DailyS1PlusProbability(
                forecast_day=forecast_day, day_index=day_index, probability_percent=percent
            )
        )

    return Noaa3DayForecast(
        issued_at=issued_at,
        probabilities=(probabilities[0], probabilities[1], probabilities[2]),
        raw_text=text,
    )


def noaa_3day_forecast_to_record_inputs(
    forecast: Noaa3DayForecast, *, source_id: str, source_url: str, fetched_at: datetime
) -> list[RecordInput]:
    """Нормализует бюллетень в три :class:`RecordInput` — по одному на день.

    ``provider_record_id`` идентифицирует **прогнозируемый календарный день**
    (``noaa-3day-s1-plus:<YYYY-MM-DD>``), не сам бюллетень: тот же день
    появляется в трёх последовательных бюллетенях (как Day 3, затем Day 2,
    затем Day 1) — main-prompt.md §2 «поздние уточнения хранятся рядом с
    прежними версиями» требует именно этого, а не одной строки на бюллетень.
    ``source_version`` — момент выпуска ЭТОГО конкретного бюллетеня; вместе с
    ``provider_record_id`` это даёт ровно нужное свойство для
    ``historical_forecast`` (main-prompt.md §1): ``select_as_of(as_of=…)``
    для данного дня вернёт версию из самого позднего бюллетеня, выпущенного
    не позже ``as_of`` — более поздние уточнения того же дня, выпущенные
    после отсечения, исключаются автоматически уже существующим правилом
    ``src/store/records.py::select_as_of``, без специального кода здесь.

    ``valid_from``/``valid_to`` — начало и конец прогнозируемых суток UTC
    (главное отличие от ``archive_probe.swpc_forecast_discussion_to_record_input``,
    где интервал действия бюллетеня как целого не извлекаем из свободного
    текста: здесь колонка таблицы прямо называет конкретный календарный
    день, поэтому интервал — не придуманное число, а прямое чтение
    структуры таблицы). ``observed_at`` = ``valid_from`` — оценка
    сформирована для целых суток, «момент, к которому относится содержимое»
    (contracts/record.schema.json) естественно совпадает с началом
    интервала, отдельная точка внутри дня не документирована источником.

    ``quality = "nominal"``: значение читается из таблицы как есть, без
    восстановления/приближения этим коннектором (в отличие от
    ``archive_probe`` — там точка вместо интервала была реконструкцией).

    ``unit = "percent"``: суточная вероятность в процентах — namespace,
    несовместимый с ``pfu`` наблюдения GOES (main-prompt.md §4 «наблюдение,
    внешний прогноз и расчёт команды — разные типы данных»); ни одна функция
    в этом модуле не конвертирует между ними.
    """
    records: list[RecordInput] = []
    source_version = forecast.issued_at.strftime("%Y%m%dT%H%M%SZ")
    raw_bytes = forecast.raw_text.encode("utf-8")
    for probability in forecast.probabilities:
        valid_from = datetime(
            probability.forecast_day.year,
            probability.forecast_day.month,
            probability.forecast_day.day,
            tzinfo=UTC,
        )
        valid_to = valid_from + timedelta(days=1)
        records.append(
            RecordInput(
                provider_record_id=f"noaa-3day-s1-plus:{probability.forecast_day.isoformat()}",
                source_id=source_id,
                source_url=source_url,
                record_kind="forecast",
                observed_at=valid_from,
                valid_from=valid_from,
                valid_to=valid_to,
                published_at=forecast.issued_at,
                fetched_at=fetched_at,
                value=probability.probability_percent,
                unit=_UNIT,
                spatial_context={
                    "provider": "NOAA SWPC",
                    "product": "3-Day Forecast",
                    "row": _ROW_LABEL,
                    "day_index": probability.day_index,
                    "issued_at": forecast.issued_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                },
                source_version=source_version,
                quality="nominal",
                raw_bytes=raw_bytes,
            )
        )
    return records


# ---------------------------------------------------------------------------
# Живой продукт: production-шлюз fetch_and_store, по образцу src/sources/swpc.py.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Noaa3DayForecastSourceConfig:
    """Конфигурация коннектора, читаемая из ``sources.yaml`` (не из кода)."""

    source_id: str
    url: str
    connect_timeout_seconds: float
    read_timeout_seconds: float
    max_retries: int
    backoff_base_seconds: float
    ttl_seconds: float
    critical_staleness_seconds: float
    enabled: bool


@dataclass(frozen=True)
class FetchOutcome:
    """Итог одного вызова :func:`fetch_and_store` / :func:`fetch_and_store_archived_bulletin`."""

    outcome: Outcome
    message: str | None
    stored_record_ids: tuple[str, ...]
    status: SourceStatus


def load_source_config(
    path: str | Path = _DEFAULT_SOURCES_YAML, *, source_id: str = SOURCE_ID
) -> Noaa3DayForecastSourceConfig:
    """Читает запись источника из ``sources.yaml`` заново при каждом вызове
    (не кешируется — .ai/backend-prompt.md §3, тот же принцип, что и
    ``src/sources/swpc.py::load_source_config``)."""
    text = Path(path).read_text(encoding="utf-8")
    doc = yaml.safe_load(text) or {}
    entries = doc.get("space_weather") or []
    for entry in entries:
        if entry.get("id") == source_id:
            network = entry.get("network") or {}
            freshness = entry.get("freshness") or {}
            return Noaa3DayForecastSourceConfig(
                source_id=source_id,
                url=entry["url"],
                connect_timeout_seconds=float(network["connect_timeout_seconds"]),
                read_timeout_seconds=float(network["read_timeout_seconds"]),
                max_retries=int(network["max_retries"]),
                backoff_base_seconds=float(network["retry_backoff_base_seconds"]),
                ttl_seconds=float(freshness["ttl_seconds"]),
                critical_staleness_seconds=float(freshness["critical_staleness_seconds"]),
                enabled=bool(entry.get("enabled", True)),
            )
    raise KeyError(f"source_id {source_id!r} is not registered in {path}")


def _fetch_and_parse(
    url: str,
    *,
    connect_timeout_seconds: float,
    read_timeout_seconds: float,
    max_retries: int,
    backoff_base_seconds: float,
    http_client: httpx.Client | None,
    moment: datetime,
    source_id: str,
    registry: SourceStatusRegistry,
) -> Noaa3DayForecast | FetchOutcome:
    """Общий шаг ``fetch`` + ``parse`` для живой и архивной линии.

    Возвращает разобранный бюллетень при успехе либо уже готовый
    :class:`FetchOutcome` с явным статусом ошибки (таймаут/квота/HTTP/формат)
    — вызывающая сторона (:func:`fetch_and_store` /
    :func:`fetch_and_store_archived_bulletin`) отличает одно от другого по
    типу и возвращает результат как есть, не повторяя обработку исключений
    дважды (приёмка FN-31 п.6 действует одинаково для обеих линий).
    """
    try:
        result = fetch(
            url,
            connect_timeout_seconds=connect_timeout_seconds,
            read_timeout_seconds=read_timeout_seconds,
            max_retries=max_retries,
            backoff_base_seconds=backoff_base_seconds,
            client=http_client,
            now=moment,
        )
    except SourceQuotaLimitedError as exc:
        updated = registry.record_error(
            source_id, at=moment, message=str(exc), quota_limited=True,
            retry_after_seconds=exc.retry_after_seconds,
        )
        return FetchOutcome(
            outcome="error_quota", message=str(exc), stored_record_ids=(), status=updated
        )
    except SourceTimeoutError as exc:
        updated = registry.record_error(
            source_id, at=moment, message=str(exc), quota_limited=False
        )
        return FetchOutcome(
            outcome="error_timeout", message=str(exc), stored_record_ids=(), status=updated
        )
    except SourceHttpError as exc:
        updated = registry.record_error(
            source_id, at=moment, message=str(exc), quota_limited=False
        )
        return FetchOutcome(
            outcome="error_http", message=str(exc), stored_record_ids=(), status=updated
        )

    try:
        return parse_noaa_3day_forecast(result.body)
    except Noaa3DayForecastFormatError as exc:
        updated = registry.record_error(
            source_id, at=moment, message=str(exc), quota_limited=False
        )
        return FetchOutcome(
            outcome="error_format", message=str(exc), stored_record_ids=(), status=updated
        )


def fetch_and_store(
    conn: sqlite3.Connection,
    raw_store: RawOriginalStore,
    *,
    config: Noaa3DayForecastSourceConfig | None = None,
    registry: SourceStatusRegistry,
    now: datetime | None = None,
    force: bool = False,
    http_client: httpx.Client | None = None,
) -> FetchOutcome:
    """Получает, парсит и сохраняет текущий бюллетень «3-Day Forecast».

    Структура и порядок проверок — намеренно идентичны
    ``src/sources/swpc.py::fetch_and_store`` (тот же шлюз для другого
    продукта того же механизма): отключение/заморозка останавливают сеть
    вовсе, TTL делает периодический refresh дешёвым no-op, активная квотная
    пауза не обходится даже ``force=True``, а таймаут/квота/неожиданный
    формат/конфликт сохранения дают явный статус источника — приёмка FN-31
    п.6 «Timeout/retry/429/пустой 200 дают явный статус, не спокойную
    оценку».
    """
    moment = now if now is not None else datetime.now(UTC)
    if moment.tzinfo is None:
        raise ValueError("now must be a timezone-aware UTC datetime (.ai/main-prompt.md §1)")

    cfg = config if config is not None else load_source_config()
    raw_status = registry.get(cfg.source_id)
    status = effective_status(raw_status, config_enabled=cfg.enabled)

    if not cfg.enabled:
        return FetchOutcome(
            outcome="skipped_disabled",
            message=f"source {cfg.source_id!r} is disabled in sources.yaml",
            stored_record_ids=(),
            status=status,
        )
    if status.frozen:
        return FetchOutcome(
            outcome="skipped_frozen",
            message=f"source {cfg.source_id!r} is frozen",
            stored_record_ids=(),
            status=status,
        )

    if not force:
        age = staleness_seconds(status, now=moment)
        if age is not None and age < cfg.ttl_seconds:
            return FetchOutcome(
                outcome="skipped_fresh",
                message=f"last success {age:.1f}s ago is within ttl={cfg.ttl_seconds}s",
                stored_record_ids=(),
                status=status,
            )

    cooldown_until = registry.quota_cooldown_until(cfg.source_id)
    if cooldown_until is not None and moment < cooldown_until:
        return FetchOutcome(
            outcome="skipped_quota_cooldown",
            message=f"quota cooldown active until {cooldown_until.isoformat()}",
            stored_record_ids=(),
            status=status,
        )

    outcome_or_forecast = _fetch_and_parse(
        cfg.url,
        connect_timeout_seconds=cfg.connect_timeout_seconds,
        read_timeout_seconds=cfg.read_timeout_seconds,
        max_retries=cfg.max_retries,
        backoff_base_seconds=cfg.backoff_base_seconds,
        http_client=http_client,
        moment=moment,
        source_id=cfg.source_id,
        registry=registry,
    )
    if isinstance(outcome_or_forecast, FetchOutcome):
        return outcome_or_forecast

    return _store_forecast(
        conn, raw_store, outcome_or_forecast, source_id=cfg.source_id, source_url=cfg.url,
        fetched_at=moment, registry=registry,
    )


def fetch_and_store_archived_bulletin(
    conn: sqlite3.Connection,
    raw_store: RawOriginalStore,
    url: str,
    *,
    source_id: str = ARCHIVE_SOURCE_ID,
    connect_timeout_seconds: float,
    read_timeout_seconds: float,
    max_retries: int,
    backoff_base_seconds: float,
    registry: SourceStatusRegistry,
    now: datetime | None = None,
    http_client: httpx.Client | None = None,
) -> FetchOutcome:
    """Получает и сохраняет ОДИН исторический выпуск по прямому URL слота.

    В отличие от :func:`fetch_and_store` — без TTL/периодического refresh:
    архивный ответ не меняется, кеш архивных ответов бессрочен
    (main-prompt.md §5, тот же принцип, что и у
    ``src/sources/archive_probe.py`` для DONKI/Forecast Discussion).
    Таймаут/квота/неожиданный формат по-прежнему дают явный статус — приёмка
    FN-31 п.6 не делает исключения для архивной линии. Постраничная загрузка
    всего периода (перебор дат/слотов, main-prompt.md §6 «стенд ходит в
    архив пакетно») — вызывающая сторона (стенд экспериментов/загрузчик
    архива), не эта функция: она нормализует и сохраняет один уже известный
    слот, по аналогии с разделением ролей в ``archive_probe``.
    """
    moment = now if now is not None else datetime.now(UTC)
    if moment.tzinfo is None:
        raise ValueError("now must be a timezone-aware UTC datetime (.ai/main-prompt.md §1)")

    outcome_or_forecast = _fetch_and_parse(
        url,
        connect_timeout_seconds=connect_timeout_seconds,
        read_timeout_seconds=read_timeout_seconds,
        max_retries=max_retries,
        backoff_base_seconds=backoff_base_seconds,
        http_client=http_client,
        moment=moment,
        source_id=source_id,
        registry=registry,
    )
    if isinstance(outcome_or_forecast, FetchOutcome):
        return outcome_or_forecast

    return _store_forecast(
        conn, raw_store, outcome_or_forecast, source_id=source_id, source_url=url,
        fetched_at=moment, registry=registry,
    )


def _store_forecast(
    conn: sqlite3.Connection,
    raw_store: RawOriginalStore,
    forecast: Noaa3DayForecast,
    *,
    source_id: str,
    source_url: str,
    fetched_at: datetime,
    registry: SourceStatusRegistry,
) -> FetchOutcome:
    records = noaa_3day_forecast_to_record_inputs(
        forecast, source_id=source_id, source_url=source_url, fetched_at=fetched_at
    )
    stored_ids: list[str] = []
    conflicts: list[str] = []
    for record_input in records:
        try:
            record_id = insert_record(conn, raw_store, record_input)
        except DuplicateKeyConflictError as exc:
            conflicts.append(str(exc))
            continue
        stored_ids.append(record_id)

    if conflicts:
        message = (
            f"{len(conflicts)} of {len(records)} day(s) conflicted "
            f"({len(stored_ids)} stored anyway): {conflicts[0]}"
        )
        updated = registry.record_error(
            source_id, at=fetched_at, message=message, quota_limited=False
        )
        return FetchOutcome(
            outcome="error_conflict", message=message,
            stored_record_ids=tuple(stored_ids), status=updated,
        )

    updated = registry.record_success(source_id, at=fetched_at)
    return FetchOutcome(
        outcome="stored", message=f"stored {len(stored_ids)} day(s)",
        stored_record_ids=tuple(stored_ids), status=updated,
    )


__all__ = [
    "ARCHIVE_SOURCE_ID",
    "SOURCE_ID",
    "DailyS1PlusProbability",
    "FetchOutcome",
    "Noaa3DayForecast",
    "Noaa3DayForecastFormatError",
    "Noaa3DayForecastSourceConfig",
    "fetch_and_store",
    "fetch_and_store_archived_bulletin",
    "load_source_config",
    "noaa_3day_forecast_to_record_inputs",
    "parse_noaa_3day_forecast",
]
