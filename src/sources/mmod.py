"""NASA MEO — почасовые факторы повышения потока метеороидов над
спорадическим фоном для LEO (FN-39, S2-08).

Слой ``sources``: разбирает уже полученный текстовый файл в
``src.store.records.RecordInput`` — не интерпретирует физику и не считает
``ratio_to_background``/пороги (.ai/main-prompt.md §8 «получение не считает
физику»). Интерпретация — ``src/domain/mmod/background.py``.

**Решение задачи (Jira-комментарии владельца задачи FN-39).** Научный gate
FN-32 остался Blocked, потому что в ``sources.yaml`` не было ни одного
подтверждённого числового значения спорадического фона, а сетевой доступ к
первичным источникам (NASA NTRS, arXiv, IMO) заблокирован прокси песочницы
во всех сессиях подряд (FN-25 → FN-32 → FN-37 → FN-39). Вместо того чтобы
реализовывать полную направленную модель фона (Helion/Anti-Helion/Apex/
Toroidal, ``sources.yaml#sporadic-background-dynamical-model`` — по-прежнему
не реализована) или придумывать эвристический коэффициент (прямо запрещено
приёмкой FN-39 п.1), владелец задачи предоставил применимый первичный
источник, который публикует УЖЕ согласованно отнормированный показатель:
NASA MEO «The 2024 meteor shower activity forecast for low Earth orbit»
(NTRS 20230015158) отдаёт почасовой ``factor`` — относительное повышение
потока над спорадическим фоном ТОЙ ЖЕ методики (Moorhead et al. 2019),
готовое к использованию как числитель/знаменатель одного отношения, без
повторной перенормировки. Обоснование источника, проверка байтов (SHA-256,
контрольные строки) и граница интеграции с геометрией станции —
``sources.yaml#nasa-meo-leo-forecast-2024`` и
``tests/fixtures/mmod/nasa_meo_leo_forecast_2024/README.md``.

**Эта задача — gate/проба-масштаб** (по аналогии с
``src/sources/orbit_history.py``, FN-33): работает над уже полученным
реальным файлом (fixture, не синтетика), сохранённым как fixture с
контрольной суммой. Production-шлюз с периодическим получением СЛЕДУЮЩЕГО
годового выпуска (2025 и далее) — отдельная задача, вне объёма FN-39: этот
модуль не ходит в сеть и не проверяет наличие более нового документа.

**Единица записи — как опубликовано, не производная.** ``RecordInput.value``
несёт ``factor_105j`` ИЗ ТАБЛИЦЫ (сырое значение поставщика), не
``ratio_to_background = 1 + factor``: получение не считает физику
(.ai/main-prompt.md §8) — вывод отношения к фону и классификация по порогам
1.2/2 (main-prompt.md §11) выполняются ``src/domain/mmod/background.py``.
"""

from __future__ import annotations

import gzip
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.store.records import Quality, RawOriginalStore, RecordInput, insert_record

UTC = timezone.utc

SOURCE_ID = "nasa-meo-leo-forecast-2024"
SOURCE_URL = "https://ntrs.nasa.gov/api/citations/20230015158/downloads/flux_data.txt"

#: Бандловая копия проверенного первичного файла (README —
#: ``tests/fixtures/mmod/nasa_meo_leo_forecast_2024/README.md``, SHA-256
#: РАСПАКОВАННОГО содержимого там же). Лежит под ``src/`` (не
#: ``tests/``/``data/``), потому что оба этих пути исключены из
#: Docker-образа (``.dockerignore``) — этот файл нужен РАБОТАЮЩЕМУ сервису
#: в ``mode=current`` (FN-39 приёмка п.4), не только тестам.
#:
#: Хранится gzip-сжатым (``.gz``, ~1.46 МБ → ~0.44 МБ), не как plain text:
#: round 1-2 ревью PR #31 — построчный текстовый файл на ~8800 строк
#: раздувал диф PR до 19933/11154 строк (лимит 3000), причём ``-diff`` в
#: ``.gitattributes`` не помогает — GitHub считает additions/deletions по
#: собственному content-sniffing (ищет NUL-байт), не по gitattributes
#: клиента. Настоящий gzip гарантированно бинарен для ЛЮБОГО такого
#: детектора (сжатые данные почти всегда содержат NUL-байты в первых же
#: килобайтах) — GitHub показывает файл как ``Bin … bytes`` по-настоящему,
#: не только в локальном ``git diff``. :func:`fetch` распаковывает на
#: лету — вся остальная кодовая база (``parse_flux_forecast`` и выше)
#: продолжает получать те же самые, побайтово те же исходные байты
#: документа, что и раньше.
DEFAULT_DATA_PATH = (
    Path(__file__).resolve().parent
    / "data"
    / "mmod"
    / "nasa_meo_leo_forecast_2024"
    / "flux_data.txt.gz"
)

#: Титульный лист LEO_Forecast_2024.pdf называет только КАЛЕНДАРНУЮ ДАТУ
#: («Issued November 2, 2023»), без времени суток и без подтверждённого
#: часового пояса — round 1 ревью PR #32 (FN-40): выдавать эту дату без
#: времени за точный ``published_at=2023-11-02T00:00:00Z`` нарушает
#: временную честность (.ai/main-prompt.md §1) и делает записи
#: ``replay_eligible`` РАНЬШЕ доказанного момента публикации.
#:
#: Вместо точного момента — консервативная ГРАНИЦА доступности: начало
#: следующих суток UTC после заявленной даты. Это НЕ заявленный поставщиком
#: момент публикации (тот неизвестен и не восстанавливается по календарной
#: дате) — это самый ранний момент, для которого можно быть уверенным, что
#: он не предшествует фактической публикации, с большим запасом (документ
#: не может быть публикован ПОСЛЕ 2023-11-02 по любому реалистичному
#: часовому поясу, если титульный лист называет эту дату). Используется
#: только чтобы ``published_at <= as_of`` не мог ошибочно включить запись
#: раньше реального момента публикации (главная ловушка Т4/§1) — обязательный
#: период 2024 года остаётся далеко после этой границы при любом разумном
#: допущении о часовом поясе титульного листа.
#:
#: **Открытый пробел provenance (FN-40, не устранён этой сессией):** ни PDF
#: (``LEO_Forecast_2024.pdf``), ни отдельные метаданные NTRS-цитирования
#: 20230015158 не сохранены в репозитории с контрольной суммой — сетевой
#: прокси этой сессии отклоняет и ``ntrs.nasa.gov``, и
#: ``api.media.atlassian.com`` (напрямую проверено в этой сессии, см.
#: ``sources.yaml#nasa-meo-leo-forecast-2024`` → ``access_restrictions``),
#: тем же классом ограничения, что блокировал FN-25/FN-32/FN-37/FN-39.
#: Поэтому заявленная календарная дата «2023-11-02» опирается только на
#: пересказ в истории Jira-задачи, не на проверяемый в этой сессии артефакт
#: — отсюда консервативная (никогда не более ранняя, чем могла быть на
#: самом деле) граница, а не точный момент, и явная пометка ниже.
PUBLISHED_AT_AVAILABILITY_BOUNDARY = datetime(2023, 11, 3, tzinfo=UTC)

#: Единица value записи (main-prompt.md §2 «единица едет рядом со значением»)
#: — безразмерный коэффициент повышения потока, КАК ОПУБЛИКОВАНО поставщиком
#: (см. docstring модуля выше), не ``ratio_to_background``.
VALUE_UNIT = "dimensionless_factor"

#: Индекс колонки ``factor 1.05e+02 J`` среди 13 whitespace-разделённых
#: полей строки данных (0-индекс): date, time, julian, solar_lon, zhr,
#: flux[6.7J], flux[105J], flux[2.83kJ], flux[105kJ], factor[6.7J],
#: factor[105J], factor[2.83kJ], factor[105kJ] — см. заголовок файла и
#: README fixture. Единственная колонка, которую использует эта задача
#: (FN-39, решение владельца задачи).
_FACTOR_105J_COLUMN = 10
_EXPECTED_FIELD_COUNT = 13


class MmodFluxForecastFormatError(RuntimeError):
    """Файл не соответствует ожидаемому формату NASA MEO flux_data.txt —
    поднимается вместо тихого пропуска/подстановки правдоподобного значения
    (.ai/main-prompt.md §2)."""


@dataclass(frozen=True)
class FluxForecastHourlyRow:
    """Один узел почасовой сетки: момент (UTC) и заявленный поставщиком
    ``factor_105j`` — ещё не ``ratio_to_background`` (см. docstring модуля)."""

    ut_datetime: datetime
    factor_105j: float


@dataclass(frozen=True)
class ParsedFluxForecast:
    """Разобранная таблица целиком, отсортированная по времени.

    ``grid_start``/``grid_end`` — включительные границы покрытия (первая и
    последняя строка); запрос вне этого диапазона — critical gap
    (``src/domain/mmod/background.py``), не экстраполяция."""

    rows: tuple[FluxForecastHourlyRow, ...]
    grid_start: datetime
    grid_end: datetime


def _parse_row(line: str, *, line_no: int) -> FluxForecastHourlyRow:
    fields = line.split()
    if len(fields) != _EXPECTED_FIELD_COUNT:
        raise MmodFluxForecastFormatError(
            f"line {line_no}: expected {_EXPECTED_FIELD_COUNT} whitespace-separated fields, "
            f"got {len(fields)}: {line!r}"
        )
    date_str, time_str = fields[0], fields[1]
    try:
        ut_datetime = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=UTC
        )
    except ValueError as exc:
        raise MmodFluxForecastFormatError(
            f"line {line_no}: cannot parse UT date/time from {date_str!r} {time_str!r}"
        ) from exc
    try:
        factor_105j = float(fields[_FACTOR_105J_COLUMN])
    except ValueError as exc:
        raise MmodFluxForecastFormatError(
            f"line {line_no}: factor_105j field {fields[_FACTOR_105J_COLUMN]!r} is not a number"
        ) from exc
    if not math.isfinite(factor_105j) or factor_105j < 0.0:
        raise MmodFluxForecastFormatError(
            f"line {line_no}: factor_105j={factor_105j!r} must be a finite, non-negative number "
            "(a flux enhancement factor cannot be negative)"
        )
    return FluxForecastHourlyRow(ut_datetime=ut_datetime, factor_105j=factor_105j)


def parse_flux_forecast(raw_bytes: bytes) -> ParsedFluxForecast:
    """Разбирает ``flux_data.txt`` NASA MEO (см. README fixture) в почасовые
    узлы. Строки, начинающиеся с ``#`` (заголовок из 5 строк), и пустые
    строки пропускаются; любая иная нераспознанная строка — жёсткая ошибка
    формата (:class:`MmodFluxForecastFormatError`), не тихий пропуск.

    Требует строго возрастающую последовательность моментов времени —
    неотсортированный или дублирующийся узел сетки был бы неотличим от
    повреждённого файла и ломает интерполяцию (``src/domain/mmod/background.py``).
    """
    text = raw_bytes.decode("ascii")
    rows: list[FluxForecastHourlyRow] = []
    previous: datetime | None = None
    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        row = _parse_row(line, line_no=line_no)
        if previous is not None and row.ut_datetime <= previous:
            raise MmodFluxForecastFormatError(
                f"line {line_no}: timestamps must be strictly increasing "
                f"({row.ut_datetime.isoformat()} is not after {previous.isoformat()})"
            )
        previous = row.ut_datetime
        rows.append(row)
    if not rows:
        raise MmodFluxForecastFormatError("no data rows found (file empty or all-comment)")
    return ParsedFluxForecast(
        rows=tuple(rows), grid_start=rows[0].ut_datetime, grid_end=rows[-1].ut_datetime
    )


def build_mmod_background_records(
    parsed: ParsedFluxForecast,
    *,
    raw_bytes: bytes,
    fetched_at: datetime,
    source_url: str = SOURCE_URL,
    quality: Quality = "nominal",
) -> list[RecordInput]:
    """Строит одну ``RecordInput`` (``record_kind='forecast'``) на каждый
    почасовой узел сетки.

    Четыре времени записи (.ai/main-prompt.md §1) для каждого узла:

    - ``observed_at`` = момент узла — контракт (``record.schema.json``)
      определяет ``observed_at`` для ``forecast`` как «момент, к которому
      относится содержимое», не момент выпуска документа;
    - ``valid_from``/``valid_to`` = ``[t, t + 1 час)`` — этот узел
      представляет значение почасового грида на этот час (интерполяция
      между узлами — забота ``src/domain/mmod/background.py``, не этой
      записи);
    - ``published_at`` = :data:`PUBLISHED_AT_AVAILABILITY_BOUNDARY` —
      консервативная граница доступности, ОДНА и та же для всех узлов
      (документ выпущен единовременно), НЕ точный момент публикации (см.
      docstring модуля — временная честность, FN-40);
    - ``fetched_at`` — аргумент вызывающей стороны.

    ``provider_record_id`` включает ISO-момент узла — устойчивый
    идентификатор продукта поставщика для ЭТОГО часа (main-prompt.md §2:
    повторная вставка того же узла — дедуплицируется по
    ``(source_id, provider_record_id, source_version)``, не создаёт вторую
    запись). ``source_version`` — одна версия на весь документ (единственный
    известный выпуск, см. ``sources.yaml#nasa-meo-leo-forecast-2024``).
    """
    source_version = "ntrs-20230015158-issued-2023-11-02"
    records: list[RecordInput] = []
    for row in parsed.rows:
        records.append(
            RecordInput(
                provider_record_id=f"nasa-meo-leo-2024-factor105j-{row.ut_datetime.isoformat()}",
                source_id=SOURCE_ID,
                source_url=source_url,
                record_kind="forecast",
                observed_at=row.ut_datetime,
                valid_from=row.ut_datetime,
                valid_to=row.ut_datetime + timedelta(hours=1),
                published_at=PUBLISHED_AT_AVAILABILITY_BOUNDARY,
                fetched_at=fetched_at,
                value=row.factor_105j,
                unit=VALUE_UNIT,
                spatial_context={
                    # round 1 ревью PR #32 (FN-40): "worst_case_unshielded_leo"
                    # вводило в заблуждение — sources.yaml документирует, что
                    # неучтённая ориентация способна УДВОИТЬ показатель для
                    # площадки, строго обращённой к радианту, так что этот
                    # factor не является абсолютным худшим случаем ни для
                    # какой конкретной поверхности. Явные, не обобщающие поля:
                    "unshielded_radiant_facing_reference": True,
                    "orientation_unmodeled": True,
                    "damage_response_unmodeled": True,
                    "not_spacecraft_surface_specific": True,
                    "kinetic_energy_j": 105.0,
                    "particle_equivalent_diameter_cm": 0.1,
                },
                source_version=source_version,
                quality=quality,
                raw_bytes=raw_bytes,
            )
        )
    return records


def fetch() -> bytes:
    """Читает и распаковывает бандловый файл (:data:`DEFAULT_DATA_PATH`,
    gzip — см. docstring модуля) — эта задача не ходит в сеть за NTRS (см.
    ``sources.yaml#nasa-meo-leo-forecast-2024``: годовой документ без
    live-эндпоинта в объёме FN-39, сетевой прокси песочницы всё равно
    отклоняет ``ntrs.nasa.gov``). Возвращает те же самые байты исходного
    текстового документа, что были проверены (SHA-256) при сохранении в
    репозиторий — сжатие/распаковка побайтово обратимы (`gzip` без потерь).
    Отдельная функция — тем же паттерном, что
    ``src.sources.swpc.fetch``/``src.sources.noaa_3day_forecast.fetch`` —
    чтобы тесты могли подменить источник байтов (``monkeypatch.setattr``),
    не трогая файл на диске."""
    return gzip.decompress(DEFAULT_DATA_PATH.read_bytes())


#: Запас по обе стороны окна для узлов интерполяции (main-prompt.md §11 не
#: у этой задачи — решение реализации): часового шага сетки достаточно,
#: чтобы гарантированно захватить оба узла, скобяющих границы окна, даже
#: когда сама граница не совпадает с началом часа.
_WINDOW_NODE_PADDING = timedelta(hours=1)


def ensure_mmod_records_for_window(
    conn: sqlite3.Connection,
    raw_store: RawOriginalStore,
    *,
    window_start: datetime,
    window_end: datetime,
    fetched_at: datetime,
) -> tuple[list[str], datetime, datetime]:
    """Гарантирует, что в хранилище есть почасовые записи
    ``nasa-meo-leo-forecast-2024``, нужные ДЛЯ ЭТОГО окна (плюс
    :data:`_WINDOW_NODE_PADDING` с каждой стороны, для интерполяции на
    границах) — не весь год целиком.

    **Почему не весь год сразу.** Вставка всех ~8791 часовых записей заранее
    (например при старте приложения) — реальный измеренный костыль: ~25
    секунд на holodную SQLite-вставку (по коммиту на запись, как у всех
    источников этого хранилища) на КАЖДЫЙ свежий файл хранилища — недопустимо
    для тестов (``tests/api/conftest.py``: у каждого теста свой временный
    каталог хранилища) и не нужно для одного расчёта, окна которого — часы,
    не год (main-prompt.md §8 «Не строй инфраструктуру там, где её не
    нужно»). Парсинг всего файла, наоборот, дёшев (~50 мс) — выполняется
    заново на каждый вызов, без кеша, тем же соображением, что и
    ``src.sources.swpc.load_source_config`` «не кешируется».

    Возвращает ``(record_ids, document_grid_start, document_grid_end)`` —
    ``record_ids`` только что вставленных/уже существующих узлов для этого
    окна, и границы покрытия ВСЕГО документа (не только загруженного
    подмножества) — для точных заметок ``assess_mmod_background`` о том,
    выходит ли окно за пределы годового прогноза целиком, а не только за
    пределы того, что было запрошено сейчас."""
    raw_bytes = fetch()
    parsed = parse_flux_forecast(raw_bytes)
    lo = window_start - _WINDOW_NODE_PADDING
    hi = window_end + _WINDOW_NODE_PADDING
    selected_rows = [row for row in parsed.rows if lo <= row.ut_datetime <= hi]
    if not selected_rows:
        return [], parsed.grid_start, parsed.grid_end
    windowed = ParsedFluxForecast(
        rows=tuple(selected_rows), grid_start=parsed.grid_start, grid_end=parsed.grid_end
    )
    records = build_mmod_background_records(windowed, raw_bytes=raw_bytes, fetched_at=fetched_at)
    record_ids = [insert_record(conn, raw_store, record) for record in records]
    return record_ids, parsed.grid_start, parsed.grid_end


__all__ = [
    "SOURCE_ID",
    "SOURCE_URL",
    "PUBLISHED_AT_AVAILABILITY_BOUNDARY",
    "VALUE_UNIT",
    "DEFAULT_DATA_PATH",
    "MmodFluxForecastFormatError",
    "FluxForecastHourlyRow",
    "ParsedFluxForecast",
    "parse_flux_forecast",
    "build_mmod_background_records",
    "fetch",
    "ensure_mmod_records_for_window",
]
