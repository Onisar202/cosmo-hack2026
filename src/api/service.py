"""Сервисный слой S1-07: оркестрация запроса, источников, хранилища и результата.

Роутеры (``src/api/routes.py``) остаются тонкими (.ai/main-prompt.md §8):
разбор HTTP, вызов функций этого модуля, преобразование исключений в
HTTP-ответ. Вся логика — здесь.

Исходное решение объёма задачи S1-07 (API и хранение, не интерпретация
механизмов) оставляло оба обязательных механизма воздействия
``status = "not_implemented"`` в каждом окне. С тех пор оба обязательных
механизма подключены к реальной оценке:

- FN-38 (S2-08) подключает ``src/domain/spaceweather/observed_classifier.py``
  — классификацию НАБЛЮДАЕМОГО потока протонов GOES по шкале S NOAA (не
  путать с уже подключённым FN-31 внешним суточным прогнозом-вероятностью,
  той же линией ``space_weather``, но другой величиной):
  ``mechanisms[*space_weather]`` несёт ``status = "ok"`` (реальные
  уровень/exceedance), когда окно полностью покрыто пригодными отсчётами в
  пределах ожидаемого шага между измерениями, и явные
  ``missing_data``/``stale_data``/``source_error``/``beyond_horizon`` иначе
  (main-prompt.md §2 — отказ/пробел не подменяется благоприятной оценкой;
  см. ``_space_weather_status``).
- FN-32 (геометрия) и FN-39 (``ratio_to_background``/пороги/подключение, см.
  :func:`_mmod_mechanism_assessment`) реализуют Механизм 2 (MMOD):
  ``mechanisms[*]`` с ``mechanism="mmod"`` несёт настоящую оценку
  (``status`` — ``ok``, ``missing_data`` или ``source_error``, не всегда
  ``not_implemented``).

**FN-41 (этап 3) снимает прежний общий отказ
``historical_mode_not_implemented``** и подключает к API два РАЗДЕЛЬНЫХ
исторических режима — :func:`_build_historical_analysis_result` и
:func:`_build_historical_forecast_result`. Разделение проходит именно там,
где его требует main-prompt.md §1 («последующие наблюдения — отдельная
ветка кода»): **пригодность записей** решают разные функции на каждой
линии данных, а не один флаг внутри общей —

- орбита: ``orbit_history.select_release_for_forecast`` (строго
  ``published_at <= as_of`` + полное покрытие интервала) против
  ``select_release_for_analysis`` (без отсечения, результат помечается
  реконструкцией), обе через ``orbit.select_oem_elements_for_request``;
- космопогода: :func:`_donki_records_for_forecast` (``select_as_of`` —
  только ``published_at <= as_of`` и ``replay_eligible``) против
  :func:`_donki_records_for_analysis` (весь архивный диапазон по времени
  события).

Общими остаются только сборочные помощники, ничего не решающие о
пригодности записей (интерполяция траектории, перевод оценки в
``mechanismAssessment``, сборка окна/манифеста/результата) — ровно тем же
способом, каким ``_window_observed_mechanism``/``_mmod_mechanism_assessment``
одинаково вызываются для обоих окон в ``mode=current``.

Орбита исторических режимов — NASA TOPO CCSDS OEM
(``src/sources/orbit_history.py``, ``sources.yaml#nasa-iss-oem-history``),
никогда не CelesTrak: ``result.orbit.source`` для них —
``"nasa-iss-oem"``, и подставить туда современные элементы нельзя по
построению (``src/sources/orbit.py::fetch_elements_for_request`` вообще не
пускает исторический режим на путь CelesTrak, .ai/main-prompt.md §1, §11).
Когда пригодного выпуска нет, задача честно отказывает ИМЕНОВАННЫМ кодом
пробела (``historical_orbit_archive_gap``), а не общим
``not_implemented`` и не результатом с придуманной орбитой.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Protocol

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from src.api.schemas import ALGORITHM_VERSION, CalculationRequest, iso_utc
from src.config import Settings
from src.domain.mmod import background as mmod_background
from src.domain.orbit.interpolate import InterpolationError, OemNode, interpolate_state
from src.domain.orbit.propagate import (
    PropagationError,
    elements_age_hours,
    is_reconstructed_geometry,
    load_elements,
    propagate,
    time_grid,
)
from src.domain.spaceweather.external_forecast import (
    ExternalForecastDay,
    assess_external_forecast,
    external_forecast_days_from_records,
)
from src.domain.spaceweather.historical_events import (
    ArchivedNotification,
    HistoricalEventAssessment,
    archived_notifications_from_records,
    assess_archived_events,
)
from src.domain.spaceweather.observed_classifier import (
    ConflictingObservationsError,
    ObservedFluxAssessment,
    assess_observed_flux,
    observed_proton_samples_from_records,
)
from src.domain.windows import WindowCandidate, excluded_windows, recommend
from src.sources import donki as donki_source
from src.sources import mmod as mmod_source
from src.sources import noaa_3day_forecast as noaa_3day_source
from src.sources import orbit, orbit_history
from src.sources import swpc as swpc_source
from src.sources.http import SourceHttpError, SourceQuotaLimitedError
from src.sources.status import (
    SourceStatus,
    SourceStatusRegistry,
    effective_status,
    is_critically_stale,
)
from src.store import (
    RawOriginalStore,
    get_latest_record,
    insert_record,
    select_as_of,
    select_observed_range,
    store_result,
)
from src.store.schema import connect as connect_store

OrbitOutcome = Literal[
    "stored", "stored_from_cache", "error_source", "error_quota", "error_corrupted"
]


class _ResultBuilder(Protocol):
    """Единая сигнатура трёх оркестраций режима (см. :func:`run_calculation`).

    Именно Protocol, а не ``Callable[..., dict]``: так mypy проверяет, что
    все три builder'а действительно принимают одни и те же именованные
    аргументы — подмена одного режима другим по ошибке становится ошибкой
    типов, а не тихим падением во время расчёта.
    """

    def __call__(
        self,
        request: CalculationRequest,
        *,
        conn: sqlite3.Connection,
        raw_store: RawOriginalStore,
        registry: SourceStatusRegistry,
        settings: Settings,
        now: datetime,
        task_id: str | None = None,
    ) -> dict[str, Any]: ...
_ORBIT_READY_OUTCOMES = frozenset({"stored", "stored_from_cache"})

_CONTRACTS_DIR = Path(__file__).resolve().parents[2] / "contracts"


class ApiError(Exception):
    """Ошибка HTTP-уровня: статус-код + единый формат ``{"error": {...}}``."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


class CalculationError(RuntimeError):
    """Расчёт не может быть выполнен — задача завершается статусом ``failed``,
    а не фиктивным успехом (.ai/main-prompt.md §2)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class OrbitFetchResult:
    outcome: OrbitOutcome
    message: str | None
    record_id: str | None
    parsed: orbit.ParsedTle | None
    source_version: str | None
    # Статус ИМЕННО ЭТОЙ попытки, построенный из её собственного исхода —
    # не из общего реестра. round 1 ревью PR #19 останавливал повторное
    # чтение реестра ПОЗЖЕ по функции, но `registry.record_success()` сам по
    # себе строит объект из ТЕКУЩЕГО общего состояния
    # (`dataclasses.replace(current, last_success_at=at)`) и не стирает
    # `last_error_*`, унаследованные от чужой, более ранней/конкурентной
    # ошибки на том же source_id — так успешная попытка могла сохранить в
    # своём результате чужой отказ. См. _attempt_success_status/
    # _attempt_error_status (round 2 ревью PR #19).
    status: SourceStatus


def sanitize_unexpected_error(exc: Exception) -> str:
    """Текст ЛЮБОГО непредвиденного исключения не логируется и не
    показывается как есть — он мог бы содержать секрет (URL с ключом в
    query-строке, путь к файлу и т.п., .ai/backend-prompt.md §4
    «маскирование на уровне логгера, а не на уровне дисциплины»). Только
    тип исключения — этого достаточно, чтобы отличить один класс сбоя от
    другого при разборе инцидента, не рискуя утечкой содержимого
    (round 2 ревью PR #19). Не применяется к curated ``CalculationError``/
    ``SwpcFormatError``/``OrbitSourceError`` и т.п. — их текст уже
    осознанно информативен и не содержит секретов (main-prompt.md §12
    «ошибка понятна»)."""
    return f"unexpected {type(exc).__name__} (see server log for correlation by task_id)"


def _attempt_success_status(source_id: str, *, at: datetime, frozen: bool) -> SourceStatus:
    """Статус источника, каким его увидела ИМЕННО эта успешная попытка —
    без полей ошибки, унаследованных от чужого, ранее записанного в общий
    реестр состояния (round 2 ревью PR #19)."""
    return SourceStatus(
        source_id=source_id,
        last_success_at=at,
        last_error_at=None,
        last_error_message=None,
        frozen=frozen,
        quota_limited=False,
    )


def _attempt_error_status(
    source_id: str, *, at: datetime, message: str, quota_limited: bool, frozen: bool
) -> SourceStatus:
    """Статус источника, каким его увидела ИМЕННО эта неудачная попытка —
    без поля успеха, унаследованного от чужого состояния (round 2 ревью
    PR #19)."""
    return SourceStatus(
        source_id=source_id,
        last_success_at=None,
        last_error_at=at,
        last_error_message=message,
        frozen=frozen,
        quota_limited=quota_limited,
    )


def _log(
    event: str, *, task_id: str | None = None, result_id: str | None = None, **fields: Any
) -> None:
    """Одна структурированная запись в stderr на этап (.ai/backend-prompt.md §5).

    ``task_id``/``result_id`` — сквозные идентификаторы (когда уже известны
    вызывающей стороне): по одному из них должна восстанавливаться вся
    история конкретного расчёта, включая ранние этапы до сохранения
    результата (round 1 ревью PR #19 — до этой правки не передавались).
    Не заменяет полноценный логгер — минимальная реализация, достаточная для
    приёмки.
    """
    line: dict[str, Any] = {"event": event}
    if task_id is not None:
        line["task_id"] = task_id
    if result_id is not None:
        line["result_id"] = result_id
    line.update(fields)
    print(json.dumps(line, ensure_ascii=False, default=str), file=sys.stderr)


def _fetch_attempt_id(source_id: str, *, now: datetime) -> str:
    """Устойчивый идентификатор попытки обращения к источнику.

    Логируется вместе с исходом попытки (см. вызовы :func:`_log` ниже) —
    так предупреждение, ссылающееся на этот id (``warning.fetch_attempt_id``,
    contracts/result.schema.json), доказуемо восстанавливается в структурном
    логе, а не остаётся строкой без следа (round 1 ревью PR #19).
    """
    return f"fa-{source_id}-{iso_utc(now)}-{uuid.uuid4().hex[:8]}"


def fetch_and_store_orbit(
    conn: sqlite3.Connection,
    raw_store: RawOriginalStore,
    registry: SourceStatusRegistry,
    *,
    now: datetime,
    norad_id: str = orbit.ISS_NORAD_ID,
) -> OrbitFetchResult:
    """Получает текущие орбитальные элементы и сохраняет запись, если получилось.

    Никогда не поднимает исключение сама — отказ источника выражается полем
    ``outcome``, как ``FetchOutcome`` у ``src/sources/swpc.py``: вызывающая
    сторона решает, фатально ли это (для расчёта — да, для принудительного
    обновления источников — нет).

    При живом отказе источника (любой из трёх типов ошибки) пробует отдать
    последнюю сохранённую запись вместо немедленного отказа —
    .ai/main-prompt.md §5 «последний пригодный ответ сохраняется и отдаётся
    с явной давностью, когда источник недоступен» (round 1 ревью PR #19).
    Отказ задачи целиком остаётся единственным исходом, только когда ни
    живого, ни ранее сохранённого набора элементов нет вовсе.
    """
    try:
        parsed, raw_bytes, source_url = orbit.fetch_elements_for_request(
            "current", norad_id=norad_id
        )
    except orbit.OrbitSourceQuotaError as exc:
        return _orbit_fetch_failed(
            conn,
            registry,
            now=now,
            norad_id=norad_id,
            outcome="error_quota",
            exc=exc,
            quota_limited=True,
        )
    except orbit.OrbitSourceError as exc:
        return _orbit_fetch_failed(
            conn,
            registry,
            now=now,
            norad_id=norad_id,
            outcome="error_source",
            exc=exc,
            quota_limited=False,
        )
    except orbit.CorruptedElementsError as exc:
        return _orbit_fetch_failed(
            conn,
            registry,
            now=now,
            norad_id=norad_id,
            outcome="error_corrupted",
            exc=exc,
            quota_limited=False,
        )

    record = orbit.build_orbital_elements_record(
        parsed, raw_bytes=raw_bytes, fetched_at=now, source_url=source_url
    )
    try:
        record_id = insert_record(conn, raw_store, record)
    except sqlite3.IntegrityError:
        # Два конкурентных запроса, увидевших одни и те же (ещё не
        # изменившиеся) элементы CelesTrak, могут гонку "SELECT видит пусто у
        # обоих -> INSERT" внутри insert_record: один поток коммитит первым,
        # второй получает UNIQUE constraint failed от sqlite, а не мирный
        # идемпотентный возврат record_id (src/store/records.py делает
        # SELECT-затем-INSERT без транзакционной защиты от гонки записи).
        # Повторный вызов теперь застаёт уже закоммиченную строку своим же
        # SELECT и корректно возвращает её id — контент идентичен, это не
        # DuplicateKeyConflictError. Без этой обработки два одновременных
        # запроса с одинаковыми (на этот момент) орбитальными элементами
        # ложно завершались бы ошибкой вместо того, чтобы оба получить один
        # и тот же результат fetch (приёмка FN-26 «два конкурентных запроса
        # не смешивают ... данные»).
        record_id = insert_record(conn, raw_store, record)
    # Обновляет общий реестр (для /sources/status, /sources/refresh) — его
    # возвращаемое значение НЕ используется как статус этой попытки (см.
    # OrbitFetchResult.status docstring, round 2 ревью PR #19).
    registry.record_success(orbit.SOURCE_ID_CURRENT, at=now)
    frozen = registry.get(orbit.SOURCE_ID_CURRENT).frozen
    status = _attempt_success_status(orbit.SOURCE_ID_CURRENT, at=now, frozen=frozen)
    return OrbitFetchResult("stored", None, record_id, parsed, record.source_version, status)


def _orbit_fetch_failed(
    conn: sqlite3.Connection,
    registry: SourceStatusRegistry,
    *,
    now: datetime,
    norad_id: str,
    outcome: OrbitOutcome,
    exc: Exception,
    quota_limited: bool,
) -> OrbitFetchResult:
    # Обновляет общий реестр — его возвращаемое значение НЕ используется как
    # статус этой попытки (см. OrbitFetchResult.status docstring, round 2
    # ревью PR #19).
    registry.record_error(
        orbit.SOURCE_ID_CURRENT, at=now, message=str(exc), quota_limited=quota_limited
    )
    frozen = registry.get(orbit.SOURCE_ID_CURRENT).frozen
    status = _attempt_error_status(
        orbit.SOURCE_ID_CURRENT,
        at=now,
        message=str(exc),
        quota_limited=quota_limited,
        frozen=frozen,
    )
    cached = get_latest_record(
        conn, source_id=orbit.SOURCE_ID_CURRENT, record_kind="orbital_elements"
    )
    if cached is None:
        return OrbitFetchResult(outcome, str(exc), None, None, None, status)

    cached_parsed = orbit.parse_tle_response(
        cached["value"]["raw"].encode("ascii"), expected_norad_id=norad_id
    )
    message = (
        f"live CelesTrak fetch failed ({outcome}): {exc}; served the last stored "
        f"record {cached['record_id']!r} instead, with its real staleness "
        "(.ai/main-prompt.md §5 «последний пригодный ответ сохраняется и "
        "отдаётся с явной давностью, когда источник недоступен»)"
    )
    return OrbitFetchResult(
        "stored_from_cache",
        message,
        cached["record_id"],
        cached_parsed,
        cached["source_version"],
        status,
    )


def _swpc_attempt_status(
    outcome: swpc_source.FetchOutcome | None,
    *,
    registry: SourceStatusRegistry,
    now: datetime,
    unexpected_error: str | None,
) -> SourceStatus:
    """Статус источника, каким его увидела ИМЕННО эта попытка — не
    ``FetchOutcome.status`` (round 2 ревью PR #19). ``src/sources/swpc.py``
    вне объёма этой задачи (FN-22, уже сдана) и не меняется:
    ``FetchOutcome.status`` там — тоже снимок общего реестра, возвращаемый
    ``registry.record_success``/``record_error`` (``dataclasses.replace`` от
    ТЕКУЩЕГО состояния), а значит подвержен той же гонке, что и
    ``OrbitFetchResult.status`` до этой правки — статус строится заново
    здесь из собственного исхода этого вызова, реестр используется только
    как приёмник обновления (для ``/sources/status``).

    Исключение — исходы ``skipped_*``: сеть в этой попытке вообще не
    затрагивалась (TTL/заморозка/отключение/квотная пауза), у неё нет
    собственного наблюдения — самое честное, что можно показать, это
    последнее известное состояние источника из общего реестра.
    """
    frozen = registry.get(swpc_source.SOURCE_ID).frozen
    if outcome is None:
        return _attempt_error_status(
            swpc_source.SOURCE_ID,
            at=now,
            message=unexpected_error or "unexpected error",
            quota_limited=False,
            frozen=frozen,
        )
    if outcome.outcome == "stored":
        return _attempt_success_status(swpc_source.SOURCE_ID, at=now, frozen=frozen)
    if outcome.outcome.startswith("error_"):
        return _attempt_error_status(
            swpc_source.SOURCE_ID,
            at=now,
            message=outcome.message or outcome.outcome,
            quota_limited=(outcome.outcome == "error_quota"),
            frozen=frozen,
        )
    return registry.get(swpc_source.SOURCE_ID)


def _noaa_3day_attempt_status(
    outcome: noaa_3day_source.FetchOutcome | None,
    *,
    registry: SourceStatusRegistry,
    now: datetime,
    unexpected_error: str | None,
) -> SourceStatus:
    """Статус источника NOAA 3-Day Forecast для ИМЕННО этой попытки — тот же
    принцип, что и :func:`_swpc_attempt_status` (FN-31, следующая после
    FN-22 линия того же Механизма 1)."""
    frozen = registry.get(noaa_3day_source.SOURCE_ID).frozen
    if outcome is None:
        return _attempt_error_status(
            noaa_3day_source.SOURCE_ID,
            at=now,
            message=unexpected_error or "unexpected error",
            quota_limited=False,
            frozen=frozen,
        )
    if outcome.outcome == "stored":
        return _attempt_success_status(noaa_3day_source.SOURCE_ID, at=now, frozen=frozen)
    if outcome.outcome.startswith("error_"):
        return _attempt_error_status(
            noaa_3day_source.SOURCE_ID,
            at=now,
            message=outcome.message or outcome.outcome,
            quota_limited=(outcome.outcome == "error_quota"),
            frozen=frozen,
        )
    return registry.get(noaa_3day_source.SOURCE_ID)


def _space_weather_status(
    assessment: ObservedFluxAssessment,
    *,
    swpc_status: SourceStatus,
    fetch_errored: bool,
    critical_staleness_seconds: float,
    now: datetime,
) -> str:
    """Выбирает ``mechanismAssessment.status`` для ``space_weather`` из
    покрытия, посчитанного чистым расчётом (``assess_observed_flux``), и
    состояния получения, которое знает только этот слой (main-prompt.md §8
    «получение не считает физику», здесь — обратное: расчёт не знает про
    сеть/реестр источников, поэтому решение о коде статуса — здесь, а не в
    ``src/domain/spaceweather``).

    Порядок проверок — от самого объясняющего пробел к самому общему
    (main-prompt.md §2 «различаются три состояния, а не два»):

    1. нет пробела вовсе — ``ok``;
    2. окно (хотя бы частично) выходит за ``now`` — ``beyond_horizon``:
       GOES не даёт прогноза, это не отказ источника и не «нет данных»,
       даже если сам источник сейчас исправен;
    3. эта самая попытка получения только что завершилась ошибкой (таймаут/
       квота/HTTP/неожиданное исключение) — ``source_error``: 429/timeout из
       приёмки FN-38 п.3 обязаны быть видны как явная причина пробела, а не
       слиться с «просто нет данных»;
    4. источник давно не отвечал успехом (устарел относительно
       ``critical_staleness_seconds`` из sources.yaml) — ``stale_data``: есть
       что-то сохранённое, но оно не свежее;
    5. иначе — ``missing_data``: источник исправен и свеж, но для конкретного
       интервала окна пригодных отсчётов действительно нет (пробел в истории
       наблюдений, а не сбой прямо сейчас).
    """
    if not assessment.critical_gap:
        return "ok"
    if assessment.beyond_horizon:
        return "beyond_horizon"
    if fetch_errored:
        return "source_error"
    if is_critically_stale(
        swpc_status, now=now, critical_staleness_seconds=critical_staleness_seconds
    ):
        return "stale_data"
    return "missing_data"


def _space_weather_mechanism(
    assessment: ObservedFluxAssessment,
    *,
    status: str,
    extra_notes: list[str] | None = None,
    extra_record_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Строит ``mechanismAssessment`` для ``space_weather`` из наблюдения
    GOES (``assess_observed_flux``) и выбранного :func:`_space_weather_status`.

    ``extra_notes``/``extra_record_ids`` (FN-31) — внешний суточный прогноз
    NOAA 3-Day для этого же окна: остаётся видимым в ``notes``/``record_ids``
    независимо от ``status`` наблюдения (contracts/result.schema.json не
    ограничивает эти поля статусом) — суточная вероятность по-прежнему НЕ
    создаёт собственного уровня/exceedance (см. ``external_forecast.py``),
    просто идёт рядом как дополнительный контекст того же механизма.
    """
    notes = list(assessment.notes)
    if extra_notes:
        notes.extend(extra_notes)
    record_ids = list(assessment.record_ids)
    if extra_record_ids:
        record_ids.extend(rid for rid in extra_record_ids if rid not in record_ids)
    return {
        "mechanism": "space_weather",
        "status": status,
        # FN-41: архивная событийная линия (DONKI) в `mode=current` не
        # участвует — Механизм 1 здесь оценивается наблюдением GOES и
        # внешним прогнозом NOAA. NOT_APPLICABLE — это «линия не
        # применялась», и её нельзя прочитать как «событий не было»
        # (contracts/result.schema.json, spaceWeatherEventState).
        "event_state": "NOT_APPLICABLE",
        "max_level": assessment.max_level if status == "ok" else None,
        "exceedance_hours_by_level": (
            dict(assessment.exceedance_hours_by_level) if status == "ok" else None
        ),
        "coverage_fraction": assessment.coverage_fraction,
        "critical_gap": assessment.critical_gap,
        "notes": notes,
        "record_ids": record_ids,
    }


def _space_weather_error_mechanism(
    message: str, *, extra_notes: list[str] | None = None, extra_record_ids: list[str] | None = None
) -> dict[str, Any]:
    """``mechanismAssessment`` для ``space_weather`` когда классификация
    наблюдения не может быть построена вообще — конфигурация источника
    недоступна, или сохранённая запись/выборка повреждена (round 1 ревью
    PR #29, 🚨/⚠️). ``status="source_error"`` всегда: main-prompt.md §2
    запрещает отказу обработки тихо стать обычным ``missing_data`` — отличие
    от «данных для окна правда нет» важно (О4 «видно ограничение уверенности»).
    Не считает ничего на запасных/угаданных числах — в отличие от
    ``_space_weather_mechanism``, здесь никакого ``ObservedFluxAssessment`` нет."""
    notes = [message]
    if extra_notes:
        notes.extend(extra_notes)
    return {
        "mechanism": "space_weather",
        "status": "source_error",
        "event_state": "NOT_APPLICABLE",  # см. _space_weather_mechanism
        "max_level": None,
        "exceedance_hours_by_level": None,
        "coverage_fraction": 0.0,
        "critical_gap": True,
        "notes": notes,
        "record_ids": list(extra_record_ids) if extra_record_ids else [],
    }


def _status_dict(status: SourceStatus, *, config_enabled: bool) -> dict[str, Any]:
    effective = effective_status(status, config_enabled=config_enabled)
    return {
        "source_id": effective.source_id,
        "last_success_at": (
            iso_utc(effective.last_success_at) if effective.last_success_at is not None else None
        ),
        "last_error_at": (
            iso_utc(effective.last_error_at) if effective.last_error_at is not None else None
        ),
        "last_error_message": effective.last_error_message,
        "frozen": effective.frozen,
        "quota_limited": effective.quota_limited,
    }


def _source_status_dict(
    registry: SourceStatusRegistry, source_id: str, *, config_enabled: bool
) -> dict[str, Any]:
    """Текущий ГЛОБАЛЬНЫЙ статус источника из реестра — для эндпоинтов,
    не привязанных к одному расчёту (``/sources/status``,
    ``/sources/refresh``). Внутри одного расчёта используется
    :func:`_status_dict` над снимком, возвращённым САМИМ вызовом этого
    расчёта (``OrbitFetchResult.status``/``FetchOutcome.status``), а не этот
    более поздний повторный запрос к реестру — см. :func:`_build_current_result`.
    """
    return _status_dict(registry.get(source_id), config_enabled=config_enabled)


def _swpc_config_enabled() -> bool:
    try:
        return swpc_source.load_source_config().enabled
    except Exception:  # noqa: BLE001 — статус источника не должен падать из-за
        # временной проблемы с чтением sources.yaml; по умолчанию считаем
        # источник включённым (сам fetch_and_store всё равно перечитает файл).
        return True


def _noaa_3day_config_enabled() -> bool:
    try:
        return noaa_3day_source.load_source_config().enabled
    except Exception:  # noqa: BLE001 — см. _swpc_config_enabled выше.
        return True


def _mmod_mechanism_assessment(
    conn: sqlite3.Connection,
    raw_store: RawOriginalStore,
    *,
    start_at: datetime,
    duration_hours: float,
    now: datetime,
    selection_as_of: datetime,
    log_ctx: dict[str, Any],
) -> dict[str, Any]:
    """Реальная оценка Механизма 2 (MMOD) для одного окна — FN-39 (S2-08).

    Закрывает научный gate FN-32: ``ratio_to_background`` даёт
    ``src.domain.mmod.background`` из уже согласованно отнормированного
    первичного источника (``sources.yaml#nasa-meo-leo-forecast-2024``), не
    эвристический коэффициент. Только эта, годовая (2024), линия участвует
    в численном уровне — геометрия станции (``src.domain.mmod.geometry``,
    FN-32: экранирование, относительная скорость, ``effective_flux_ratio``)
    сюда НЕ перемножается (решение владельца задачи FN-39,
    ``sources.yaml#nasa-meo-leo-forecast-2024`` implementation_note): NASA
    factor — опубликованный unshielded/radiant-facing reference, не
    привязанный к конкретной поверхности станции; ориентация, damage
    response и экранирование явно не моделируются. Повторное умножение на
    единственную геометрическую поправку FN-32 исказило бы уже нормированное
    агрегированное число. Совместное траекторное объединение с конкретной
    ориентацией и моделью повреждаемости — вне объёма этой задачи.

    Реальный `now` этого окружения (2026+) лежит за пределами годового
    документа NASA (только 2024) — в `mode=current` это ЧЕСТНО даёт
    ``status="missing_data"``/``critical_gap=True`` (main-prompt.md §2: за
    пределами данных — не спокойная обстановка), не имитирует уровень.
    Тесты (``tests/api/``) проверяют ok/conflict/equal/critical_gap
    сценарии через инъекцию ``now`` внутри обязательного периода
    01.05–30.06.2024, тем же способом, что и остальные тесты этого модуля.

    **FN-41: ``selection_as_of`` отделён от ``now``.** ``now`` — момент
    получения (``fetched_at`` вставляемых записей), ``selection_as_of`` —
    момент отсечения выборки. Раньше оба были одним аргументом, и для
    ``historical_forecast`` это была бы утечка: выборка велась бы по
    реальному «сейчас», а не по ``as_of`` запроса. Проверено, что эта линия
    не могла протечь и иначе (приёмка FN-41, п.10 задания): документ NASA
    MEO — один годовой выпуск с единственным ``published_at`` (2023-11-02,
    ``src/sources/mmod.py::PUBLISHED_AT``), покрывающий весь обязательный
    период, а ``ensure_mmod_records_for_window`` читает бандловый файл, не
    сеть, и ``fetched_at`` в отбор не входит вовсе (``select_as_of``
    фильтрует только по ``published_at``/``replay_eligible``). Тем не менее
    отсечение теперь передаётся явно: свойство «строгий режим не видит
    ничего позже ``as_of``» не должно держаться на том, что у конкретного
    источника сегодня одна версия.
    """
    end_at = start_at + timedelta(hours=duration_hours)
    try:
        _record_ids, doc_start, doc_end = mmod_source.ensure_mmod_records_for_window(
            conn, raw_store, window_start=start_at, window_end=end_at, fetched_at=now
        )
        mmod_records = select_as_of(
            conn, selection_as_of, source_id=mmod_source.SOURCE_ID, record_kind="forecast"
        )
        nodes = mmod_background.background_nodes_from_records(mmod_records)
        assessment = mmod_background.assess_mmod_background(
            nodes,
            window_start=start_at,
            window_end=end_at,
            document_grid_start=doc_start,
            document_grid_end=doc_end,
        )
    except Exception as exc:  # noqa: BLE001 — отказ этого источника не должен
        # обрушивать расчёт целиком (.ai/main-prompt.md §5), тем же
        # принципом, что и swpc/noaa_3day-блоки выше; бандловый файл не
        # ходит в сеть, но повреждение/отсутствие файла — тот же класс
        # отказа источника, не бизнес-ошибка расчёта.
        error_message = sanitize_unexpected_error(exc)
        _log("mmod_background_failed", error=error_message, **log_ctx)
        return {
            "mechanism": "mmod",
            "status": "source_error",
            # Архивная событийная линия — линия Механизма 1; для MMOD она не
            # определена (contracts/result.schema.json требует здесь ровно
            # NOT_APPLICABLE, а не отсутствие поля).
            "event_state": "NOT_APPLICABLE",
            "max_level": None,
            "exceedance_hours_by_level": None,
            "coverage_fraction": 0.0,
            "critical_gap": True,
            "notes": [
                "NASA MEO 2024 LEO forecast: ошибка при получении/оценке "
                f"({error_message}) — критический пробел, не спокойная обстановка "
                "(main-prompt.md §2)."
            ],
            "record_ids": [],
        }
    return {
        "mechanism": "mmod",
        "status": assessment.status,
        "event_state": "NOT_APPLICABLE",  # см. выше
        "max_level": assessment.max_level,
        "exceedance_hours_by_level": assessment.exceedance_hours_by_level,
        "coverage_fraction": assessment.coverage_fraction,
        "critical_gap": assessment.critical_gap,
        "notes": list(assessment.notes),
        "record_ids": list(assessment.record_ids),
    }


def _window(
    window_id: str,
    start_at: datetime,
    duration_hours: float,
    *,
    space_weather_mechanism: dict[str, Any],
    mmod_mechanism: dict[str, Any],
) -> dict[str, Any]:
    """Строит окно без ``excluded_from_comparison``/``exclusion_reason`` —
    эти два поля решает правило доминирования v2 (FN-34,
    ``src.domain.windows``) над ГОТОВЫМ набором окон, см.
    :func:`_apply_window_dominance`, а не эта функция для одного окна в
    изоляции. Оба механизма строятся заранее вызывающей стороной —
    ``space_weather_mechanism`` через :func:`_space_weather_mechanism`
    (FN-38), ``mmod_mechanism`` через :func:`_mmod_mechanism_assessment`
    (FN-39) — эта функция лишь собирает окно целиком, не решает
    уровень/статус ни одного механизма."""
    end_at = start_at + timedelta(hours=duration_hours)
    return {
        "window_id": window_id,
        "start_at": iso_utc(start_at),
        "end_at": iso_utc(end_at),
        "duration_hours": duration_hours,
        "mechanisms": [space_weather_mechanism, mmod_mechanism],
        "lighting": {"requested": False, "status": "not_requested", "note": None},
    }


def _apply_window_dominance(windows: list[dict[str, Any]]) -> dict[str, Any]:
    """Заполняет ``excluded_from_comparison``/``exclusion_reason`` каждого
    окна и строит ``result.recommendation`` через правило доминирования v2
    (FN-34/S2-04, ``src.domain.windows.dominance``) — единственное место,
    решающее это для ``mode = current``. Мутирует переданные словари окон на
    месте (тот же паттерн, что и остальная сборка результата в этом модуле)
    и возвращает ``recommendation`` для `result`.

    До FN-38/FN-39 оба механизма (space_weather/mmod) всегда несли
    ``status = "not_implemented"``/``critical_gap = True``, поэтому это
    неизбежно давало ``all_windows_excluded`` для любого запроса. С FN-38
    ``space_weather`` может быть ``ok`` (см. ``_space_weather_status``), и с
    FN-39 ``mmod`` тоже может быть ``ok`` (см.
    :func:`_mmod_mechanism_assessment`) — правило предпочтения окон (п.1,
    main-prompt.md §11) по-прежнему исключает окно при критическом пробеле
    по ЛЮБОМУ механизму, но теперь, когда оба покрыты пригодными данными,
    результат этой функции действительно зависит от обстановки:
    доминирование, конфликт, равенство или недостаточность оснований (п.2-4
    того же раздела), а не гарантированный ``all_windows_excluded``. Это не
    захардкожено здесь — то же общее правило доминирования/tie/conflict
    отрабатывает одинаково независимо от того, сколько механизмов реально
    покрыты.
    """
    candidates = [
        WindowCandidate.from_assessments(w["window_id"], w["duration_hours"], w["mechanisms"])
        for w in windows
    ]
    exclusions = excluded_windows(candidates)
    for window in windows:
        reason = exclusions.get(window["window_id"])
        window["excluded_from_comparison"] = reason is not None
        window["exclusion_reason"] = reason

    outcome = recommend(candidates)
    return {
        "status": outcome.status,
        "window_id": outcome.window_id,
        "explanation": outcome.explanation,
    }


@lru_cache
def _result_validator() -> Draft202012Validator:
    """Валидатор ``contracts/result.schema.json`` (плюс вложенный
    ``request.schema.json``), построенный один раз за процесс.

    Собранный результат проверяется этим валидатором до сохранения (см.
    :func:`_build_current_result`) — контрактное нарушение (например
    отрицательная давность элементов, main-prompt.md §… или пропущенное
    обязательное поле) обязано остановить сохранение явной ошибкой, а не
    молча лечь в неизменяемое хранилище и уйти клиенту как «корректный»
    результат (round 1 ревью PR #19).
    """
    schemas = {
        name: json.loads((_CONTRACTS_DIR / f"{name}.schema.json").read_text(encoding="utf-8"))
        for name in ("request", "result")
    }
    registry: Registry[Any] = Registry().with_resources(
        (schema["$id"], Resource.from_contents(schema)) for schema in schemas.values()
    )
    return Draft202012Validator(schemas["result"], registry=registry)


def _validate_result_or_raise(result: dict[str, Any]) -> None:
    errors = sorted(_result_validator().iter_errors(result), key=lambda e: list(e.path))
    if not errors:
        return
    details = "; ".join(f"{list(e.path)}: {e.message}" for e in errors[:5])
    raise CalculationError(
        "result_schema_violation",
        f"built result does not conform to contracts/result.schema.json, refusing "
        f"to store it: {details}",
    )


def _build_current_result(
    request: CalculationRequest,
    *,
    conn: sqlite3.Connection,
    raw_store: RawOriginalStore,
    registry: SourceStatusRegistry,
    settings: Settings,
    now: datetime,
    task_id: str | None = None,
) -> dict[str, Any]:
    result_id = f"res-{uuid.uuid4()}"
    log_ctx = {"task_id": task_id, "result_id": result_id}

    orbit_attempt_id = _fetch_attempt_id(orbit.SOURCE_ID_CURRENT, now=now)
    orbit_fetch = fetch_and_store_orbit(conn, raw_store, registry, now=now)
    _log(
        "orbit_fetch",
        outcome=orbit_fetch.outcome,
        source_id=orbit.SOURCE_ID_CURRENT,
        fetch_attempt_id=orbit_attempt_id,
        **log_ctx,
    )
    orbit_ready = (
        orbit_fetch.outcome in _ORBIT_READY_OUTCOMES
        and orbit_fetch.parsed is not None
        and orbit_fetch.record_id is not None
    )
    if not orbit_ready:
        raise CalculationError(
            f"orbit_{orbit_fetch.outcome}",
            f"orbital elements are required for any result (main-prompt.md §11 "
            f"«Траектория участвует хотя бы в одном расчёте») and no previously "
            f"stored record is available either; live fetch failed: "
            f"{orbit_fetch.message}",
        )
    assert orbit_fetch.parsed is not None  # narrowed by orbit_ready above, for mypy
    assert orbit_fetch.record_id is not None
    parsed = orbit_fetch.parsed

    elements = load_elements(parsed.line1, parsed.line2, norad_id=parsed.norad_id)
    raw_age_hours = elements_age_hours(parsed.epoch, now)
    # contracts/result.schema.json: orbit.elements_age_hours >= 0. Отрицательное
    # значение (эпоха элементов позже момента расчёта — например
    # рассинхронизация часов) не отбрасывается и не заменяется нулём, а
    # приводится к модулю; направление всё равно виден из сравнения
    # elements_epoch и computed_at самого результата (round 1 ревью PR #19).
    age_hours = abs(raw_age_hours)
    reconstructed = is_reconstructed_geometry(
        parsed.epoch, now, max_confirmed_age_hours=settings.orbit_max_confirmed_age_hours
    )

    calc_hours = (request.calc_end_at - request.start_at).total_seconds() / 3600.0
    grid = time_grid(
        request.start_at,
        hours=calc_hours,
        step_minutes=settings.orbit_step_minutes,
        max_hours=32.0,
    )
    try:
        states = propagate(elements, grid)
    except PropagationError as exc:
        raise CalculationError("orbit_propagation_failed", str(exc)) from exc
    _log(
        "orbit_propagated",
        record_id=orbit_fetch.record_id,
        grid_points=len(states),
        epoch=parsed.epoch.isoformat(),
        elements_age_hours=age_hours,
        is_reconstructed=reconstructed,
        **log_ctx,
    )

    warnings: list[dict[str, Any]] = []
    if orbit_fetch.outcome == "stored_from_cache":
        warnings.append(
            {
                "code": "orbit-source-stale-fallback",
                "severity": "advisory",
                "mechanism": "orbit",
                "message": orbit_fetch.message or "",
                "record_ids": [orbit_fetch.record_id],
                "fetch_attempt_id": orbit_attempt_id,
                "window_id": None,
            }
        )

    swpc_attempt_id = _fetch_attempt_id(swpc_source.SOURCE_ID, now=now)
    swpc_unexpected_error: str | None = None
    # round 1 ревью PR #29 (🚨): ``swpc_cfg`` захватывается здесь и переиспользуется
    # ниже (ttl_seconds/critical_staleness_seconds для классификатора) — НЕ
    # перечитывается заново отдельным вызовом с запасными константами на
    # случай отказа чтения. Остаётся ``None``, только если сама загрузка
    # конфигурации не удалась — тогда классификация ниже честно возвращает
    # ``source_error``, а не считает на угаданных числах (main-prompt.md §7
    # «пороги/горизонты... в конфиге, не в коде», §2 «отказ не подменяется
    # благоприятной/правдоподобной оценкой»).
    swpc_cfg: swpc_source.SwpcSourceConfig | None = None
    try:
        swpc_cfg = swpc_source.load_source_config()
        swpc_outcome = swpc_source.fetch_and_store(
            conn, raw_store, config=swpc_cfg, registry=registry, now=now
        )
    except Exception as exc:  # noqa: BLE001 — отказ одного источника не должен
        # обрушивать расчёт целиком (.ai/main-prompt.md §5, приёмка FN-26
        # «при отказе одного источника остальные доступны»); неожиданная
        # ошибка коннектора фиксируется как статус источника, а не падение API.
        # Сообщение маскируется (см. sanitize_unexpected_error) прежде чем
        # попасть и в общий реестр (виден любому вызову /sources/status), и в
        # эту переменную — непредвиденное исключение могло бы содержать URL с
        # ключом или другую внутреннюю деталь (round 2 ревью PR #19).
        swpc_unexpected_error = sanitize_unexpected_error(exc)
        registry.record_error(
            swpc_source.SOURCE_ID, at=now, message=swpc_unexpected_error, quota_limited=False
        )
        swpc_outcome = None

    swpc_status = _swpc_attempt_status(
        swpc_outcome, registry=registry, now=now, unexpected_error=swpc_unexpected_error
    )

    _log(
        "swpc_fetch",
        outcome=(swpc_outcome.outcome if swpc_outcome is not None else "error_unexpected"),
        source_id=swpc_source.SOURCE_ID,
        fetch_attempt_id=swpc_attempt_id,
        **log_ctx,
    )
    swpc_fetch_errored = swpc_outcome is None or swpc_outcome.outcome.startswith("error_")
    if swpc_fetch_errored:
        failure_detail = swpc_outcome.message if swpc_outcome is not None else swpc_unexpected_error
        outcome_code = swpc_outcome.outcome if swpc_outcome is not None else "error_unexpected"
        message = (
            f"Получение потока протонов не удалось: {failure_detail}. Классификация "
            "ниже использует любые ранее сохранённые отсчёты в диапазоне окна через "
            "select_observed_range — отказ этой попытки не означает отсутствие любых "
            "данных, но виден в mechanisms[*space_weather].status отдельно от оценки."
        )
        warnings.append(
            {
                "code": f"space-weather-{outcome_code}",
                "severity": "advisory",
                "mechanism": "space_weather",
                "message": message,
                "record_ids": [],
                "fetch_attempt_id": swpc_attempt_id,
                "window_id": None,
            }
        )

    # NOAA 3-Day Forecast (S1+) — FN-31: отдельная от наблюдения выше линия
    # внешнего прогноза Механизма 1. Тот же управляемый шлюз/статус/warning
    # паттерн, что и у потока протонов (main-prompt.md §5 «отказ одного
    # источника не должен обрушивать расчёт целиком»).
    noaa_3day_attempt_id = _fetch_attempt_id(noaa_3day_source.SOURCE_ID, now=now)
    noaa_3day_unexpected_error: str | None = None
    try:
        noaa_3day_cfg = noaa_3day_source.load_source_config()
        noaa_3day_outcome = noaa_3day_source.fetch_and_store(
            conn, raw_store, config=noaa_3day_cfg, registry=registry, now=now
        )
    except Exception as exc:  # noqa: BLE001 — см. swpc-блок выше.
        noaa_3day_unexpected_error = sanitize_unexpected_error(exc)
        registry.record_error(
            noaa_3day_source.SOURCE_ID,
            at=now,
            message=noaa_3day_unexpected_error,
            quota_limited=False,
        )
        noaa_3day_outcome = None

    noaa_3day_status = _noaa_3day_attempt_status(
        noaa_3day_outcome, registry=registry, now=now, unexpected_error=noaa_3day_unexpected_error
    )
    noaa_3day_outcome_code = (
        noaa_3day_outcome.outcome if noaa_3day_outcome is not None else "error_unexpected"
    )
    _log(
        "noaa_3day_forecast_fetch",
        outcome=noaa_3day_outcome_code,
        source_id=noaa_3day_source.SOURCE_ID,
        fetch_attempt_id=noaa_3day_attempt_id,
        **log_ctx,
    )
    if noaa_3day_outcome is None or noaa_3day_outcome.outcome.startswith("error_"):
        failure_detail = (
            noaa_3day_outcome.message
            if noaa_3day_outcome is not None
            else noaa_3day_unexpected_error
        )
        warnings.append(
            {
                "code": f"space-weather-forecast-{noaa_3day_outcome_code}",
                "severity": "advisory",
                "mechanism": "space_weather",
                "message": (
                    f"Получение суточного прогноза NOAA 3-Day (S1+) не удалось: "
                    f"{failure_detail}. Ранее сохранённые версии (если есть) всё "
                    "равно используются ниже через select_as_of — отказ этой "
                    "попытки не означает отсутствие любых данных."
                ),
                "record_ids": [],
                "fetch_attempt_id": noaa_3day_attempt_id,
                "window_id": None,
            }
        )

    # select_as_of(as_of=now, ...) для record_kind="forecast" реализует и
    # режим current: published_at каждой записи этой линии всегда известен
    # (парсер требует ':Issued:'), поэтому "текущая" выборка — это просто
    # строгий replay на момент now, тем же уже протестированным правилом,
    # что и historical_forecast (main-prompt.md §1) — без отдельного кода.
    forecast_records: list[dict[str, Any]] = []
    forecast_days: list[ExternalForecastDay] = []
    try:
        forecast_records = select_as_of(
            conn, now, source_id=noaa_3day_source.SOURCE_ID, record_kind="forecast"
        )
        forecast_days = external_forecast_days_from_records(forecast_records)
    except Exception as exc:  # noqa: BLE001 — повреждённая сохранённая запись не
        # должна обрушивать расчёт целиком; окна ниже просто не увидят
        # прогнозных дней (main-prompt.md §2 — это не благоприятная замена,
        # отсутствие данных остаётся видимым через notes/warnings выше).
        _log(
            "noaa_3day_forecast_selection_failed",
            error=sanitize_unexpected_error(exc),
            **log_ctx,
        )
        forecast_records = []
        forecast_days = []

    forecast_records_by_id = {str(r["record_id"]): r for r in forecast_records}

    def _window_forecast_assessment(
        start_at: datetime, duration_hours: float
    ) -> tuple[list[str], list[str]]:
        end_at = start_at + timedelta(hours=duration_hours)
        assessment = assess_external_forecast(
            forecast_days, window_start=start_at, window_end=end_at
        )
        return list(assessment.notes), list(assessment.record_ids)

    # FN-38 (S2-08): наблюдение GOES pfu для этого же окна — независимая от
    # NOAA 3-Day Forecast линия того же Механизма 1 (см. модульный докстринг
    # src/domain/spaceweather/observed_classifier.py). ``select_observed_range``
    # читает весь диапазон [start - hold, end) по ``observed_at``, не только
    # самую свежую запись — published_at у этого источника всегда null
    # (sources.yaml), select_as_of здесь неприменим.
    observed_records_by_id: dict[str, dict[str, Any]] = {}

    def _observation_processing_failed_mechanism(
        window_id: str, attempt_id: str, error_detail: str, *, record_ids: list[str],
        forecast_notes: list[str], forecast_record_ids: list[str],
    ) -> dict[str, Any]:
        # round 2 ревью PR #29 (⚠️ «остаётся непрослеживаемой»): отдельное
        # предупреждение с window_id/record_ids (тот же паттерн, что и
        # существующие warnings этой функции — space-weather-{outcome_code}
        # выше), не только notes самого mechanismAssessment — и то, и другое
        # ссылается на конкретные id, не на факт отказа вообще.
        warnings.append(
            {
                "code": "space-weather-observation-processing-error",
                "severity": "advisory",
                "mechanism": "space_weather",
                "message": (
                    f"Классификация наблюдения GOES не построена для окна {window_id}: "
                    f"{error_detail}."
                ),
                "record_ids": record_ids,
                "fetch_attempt_id": attempt_id,
                "window_id": window_id,
            }
        )
        return _space_weather_error_mechanism(
            f"Классификация наблюдения GOES не построена: {error_detail}.",
            extra_notes=forecast_notes,
            extra_record_ids=list(forecast_record_ids) + record_ids,
        )

    def _window_observed_mechanism(
        window_id: str, start_at: datetime, duration_hours: float, *,
        forecast_notes: list[str], forecast_record_ids: list[str],
    ) -> dict[str, Any]:
        end_at = start_at + timedelta(hours=duration_hours)
        cfg = swpc_cfg
        if cfg is None:
            # round 1 ревью PR #29 (🚨): конфигурация источника недоступна —
            # ttl_seconds/critical_staleness_seconds неизвестны для ЭТОЙ
            # попытки расчёта, поэтому честный отказ обработки, а не расчёт
            # на запасных/угаданных числах (main-prompt.md §7 «пороги... в
            # конфиге, не в коде», §2 «отказ не подменяется правдоподобной
            # оценкой»). Уже покрыто отдельным warning'ом выше (при отказе
            # самой попытки получения, см. swpc_fetch_errored) — здесь
            # никаких записей нет вовсе, второе предупреждение было бы
            # дубликатом.
            return _space_weather_error_mechanism(
                "Классификация наблюдения GOES не построена: конфигурация "
                "источника (sources.yaml) недоступна для этой попытки расчёта.",
                extra_notes=forecast_notes, extra_record_ids=forecast_record_ids,
            )

        # round 3 ревью PR #29 (⚠️): СВОЙ идентификатор попытки выборки/
        # обработки для ЭТОГО окна — не переиспользует swpc_attempt_id
        # (сетевое получение потока, залогированное раньше в этой же функции
        # и вполне могло уже завершиться успехом к этому моменту). Логируется
        # вместе с window_id в оба except ниже — тот же принцип, что и
        # _fetch_attempt_id для сетевых источников (round 1 ревью PR #19):
        # warning.fetch_attempt_id обязан доказуемо вести к залогированной
        # попытке именно этой обработки, а не к постороннему событию.
        observation_attempt_id = _fetch_attempt_id(
            f"{swpc_source.SOURCE_ID}:{window_id}", now=now
        )

        try:
            records = select_observed_range(
                conn,
                source_id=swpc_source.SOURCE_ID,
                record_kind="observation",
                start_at=start_at - timedelta(seconds=cfg.ttl_seconds),
                end_at=end_at,
            )
        except Exception as exc:  # noqa: BLE001 — сама выборка не удалась
            # (например недоступное хранилище) — записей нет вовсе, не
            # только их обработка; текст маскируется как непредвиденное
            # исключение (main-prompt.md §7).
            error_detail = sanitize_unexpected_error(exc)
            _log(
                "swpc_observation_selection_failed",
                error=error_detail, window_id=window_id,
                fetch_attempt_id=observation_attempt_id, **log_ctx,
            )
            return _observation_processing_failed_mechanism(
                window_id, observation_attempt_id, error_detail, record_ids=[],
                forecast_notes=forecast_notes, forecast_record_ids=forecast_record_ids,
            )

        for record in records:
            observed_records_by_id[str(record["record_id"])] = record

        try:
            samples = observed_proton_samples_from_records(records)
            assessment = assess_observed_flux(
                samples, window_start=start_at, window_end=end_at, now=now,
                hold_seconds=cfg.ttl_seconds,
            )
        except ConflictingObservationsError as exc:
            # round 2 ревью PR #29 (⚠️): сообщение построено этим же модулем
            # из provider_record_id/fetched_at/наблюдаемых значений — не из
            # сырого ответа источника, поэтому показывается как есть, не
            # маскируется sanitize_unexpected_error (та защищает от утечки
            # секретов из НЕИЗВЕСТНЫХ исключений, не от собственных доменных
            # ошибок); id конкретных конфликтующих записей — из exc.record_ids,
            # не всей выборки окна.
            #
            # round 4 ревью PR #29 (⚠️): эта ветка возвращала warning с
            # fetch_attempt_id, для которого не было ни одной записи в логе —
            # тот же принцип прослеживаемости, что и у соседнего except ниже
            # (и у _fetch_attempt_id для сетевых источников, round 1 ревью
            # PR #19), обязан выполняться и здесь: id логируется ДО возврата,
            # не только передаётся в warning.
            conflicting_ids = list(exc.record_ids)
            _log(
                "swpc_observation_processing_failed",
                error=str(exc), window_id=window_id, record_ids=conflicting_ids,
                fetch_attempt_id=observation_attempt_id, **log_ctx,
            )
            return _observation_processing_failed_mechanism(
                window_id, observation_attempt_id, str(exc), record_ids=conflicting_ids,
                forecast_notes=forecast_notes, forecast_record_ids=forecast_record_ids,
            )
        except Exception as exc:  # noqa: BLE001 — повреждённая нормализация
            # записи (неожиданная форма payload_json и т.п.) — неизвестное
            # исключение, текст маскируется; конкретный повреждённый
            # record_id этот блок не выделяет (round 2 ревью отметил это как
            # желательное дальнейшее улучшение, не обязательное), но весь
            # набор ID, ЗАТРОНУТЫХ этой попыткой обработки, остаётся видимым
            # — уже не «где-то в логе», а в warnings/data_manifest.
            error_detail = sanitize_unexpected_error(exc)
            _log(
                "swpc_observation_processing_failed",
                error=error_detail, window_id=window_id,
                fetch_attempt_id=observation_attempt_id, **log_ctx,
            )
            return _observation_processing_failed_mechanism(
                window_id, observation_attempt_id, error_detail,
                record_ids=sorted({str(r["record_id"]) for r in records}),
                forecast_notes=forecast_notes, forecast_record_ids=forecast_record_ids,
            )

        status = _space_weather_status(
            assessment,
            swpc_status=swpc_status,
            fetch_errored=swpc_fetch_errored,
            critical_staleness_seconds=cfg.critical_staleness_seconds,
            now=now,
        )
        return _space_weather_mechanism(
            assessment, status=status,
            extra_notes=forecast_notes, extra_record_ids=forecast_record_ids,
        )

    win_a_forecast_notes, win_a_forecast_record_ids = _window_forecast_assessment(
        request.start_at, request.duration_hours
    )
    win_b_forecast_notes, win_b_forecast_record_ids = _window_forecast_assessment(
        request.search_end_at, request.duration_hours
    )

    win_a_mmod = _mmod_mechanism_assessment(
        conn,
        raw_store,
        start_at=request.start_at,
        duration_hours=request.duration_hours,
        now=now,
        # mode=current: отсечение выборки — сам момент расчёта (FN-41 сделал
        # это отсечение явным аргументом, см. _mmod_mechanism_assessment).
        selection_as_of=now,
        log_ctx=log_ctx,
    )
    win_b_mmod = _mmod_mechanism_assessment(
        conn,
        raw_store,
        start_at=request.search_end_at,
        duration_hours=request.duration_hours,
        now=now,
        selection_as_of=now,
        log_ctx=log_ctx,
    )

    win_a_space_weather = _window_observed_mechanism(
        "win-a", request.start_at, request.duration_hours,
        forecast_notes=win_a_forecast_notes, forecast_record_ids=win_a_forecast_record_ids,
    )
    win_b_space_weather = _window_observed_mechanism(
        "win-b", request.search_end_at, request.duration_hours,
        forecast_notes=win_b_forecast_notes, forecast_record_ids=win_b_forecast_record_ids,
    )

    windows = [
        _window(
            "win-a",
            request.start_at,
            request.duration_hours,
            space_weather_mechanism=win_a_space_weather,
            mmod_mechanism=win_a_mmod,
        ),
        _window(
            "win-b",
            request.search_end_at,
            request.duration_hours,
            space_weather_mechanism=win_b_space_weather,
            mmod_mechanism=win_b_mmod,
        ),
    ]
    recommendation = _apply_window_dominance(windows)

    forecast_manifest_entries = [
        {
            "record_id": record_id,
            "source_id": noaa_3day_source.SOURCE_ID,
            "source_version": forecast_records_by_id[record_id]["source_version"],
            "record_kind": "forecast",
        }
        for record_id in sorted(set(win_a_forecast_record_ids) | set(win_b_forecast_record_ids))
    ]
    # Только реально ИСПОЛЬЗОВАННЫЕ (попавшие в покрытый сегмент хотя бы
    # одного окна) записи наблюдения — не все, что вернул select_observed_range
    # (main-prompt.md §3 «каждый вывод ссылается на record_id» — но не
    # наоборот: запись вне покрытия окна не должна создавать видимость
    # использования, которого не было).
    observed_manifest_entries = [
        {
            "record_id": record_id,
            "source_id": swpc_source.SOURCE_ID,
            "source_version": observed_records_by_id[record_id]["source_version"],
            "record_kind": "observation",
        }
        for record_id in sorted(
            (set(win_a_space_weather["record_ids"]) | set(win_b_space_weather["record_ids"]))
            & set(observed_records_by_id)
        )
    ]

    # Манифест MMOD (FN-39) — той же формой, что и forecast_manifest_entries
    # выше: только записи, ФАКТИЧЕСКИ использованные хотя бы одним окном
    # (main-prompt.md §3 «манифест собирается фактически использованными
    # записями»), не весь загруженный набор.
    mmod_manifest_record_ids = sorted(set(win_a_mmod["record_ids"]) | set(win_b_mmod["record_ids"]))
    mmod_records_by_id: dict[str, dict[str, Any]] = {}
    if mmod_manifest_record_ids:
        mmod_records_by_id = {
            str(r["record_id"]): r
            for r in select_as_of(
                conn, now, source_id=mmod_source.SOURCE_ID, record_kind="forecast"
            )
        }
    mmod_manifest_entries = [
        {
            "record_id": record_id,
            "source_id": mmod_source.SOURCE_ID,
            "source_version": mmod_records_by_id[record_id]["source_version"],
            "record_kind": "forecast",
        }
        for record_id in mmod_manifest_record_ids
        if record_id in mmod_records_by_id
    ]

    # Статусы, построенные из СОБСТВЕННОГО исхода именно этой попытки
    # (orbit_fetch.status, swpc_status, noaa_3day_status) — не из общего
    # реестра ни поздним повторным registry.get() (round 1 ревью), ни через
    # возвращаемое значение record_success/record_error, которое само
    # строится из текущего общего состояния и может унаследовать поля
    # чужой, конкурентной попытки (round 2 ревью PR #19, приёмка FN-26 «два
    # конкурентных запроса не смешивают статусы»).
    source_status = [
        _status_dict(orbit_fetch.status, config_enabled=True),
        _status_dict(swpc_status, config_enabled=_swpc_config_enabled()),
        _status_dict(noaa_3day_status, config_enabled=_noaa_3day_config_enabled()),
    ]

    limitations = [
        "Механизм 1 (космическая погода) сочетает две отдельные величины: "
        "классификацию НАБЛЮДЕНИЯ (поток протонов GOES >=10 МэВ, шкала S NOAA "
        "S1/S2/S3, FN-38) — она даёт mechanisms[*space_weather].status=ok "
        "только когда всё окно покрыто пригодными отсчётами в пределах "
        "ожидаемого шага между измерениями, и суточную вероятность S1+ NOAA "
        "3-Day Forecast (FN-31) — показана в notes/record_ids как есть, без "
        "деления по часам, умножения на длительность окна или суммирования "
        "через полночь, и НЕ создаёт собственного уровня/exceedance ни при "
        "каком статусе наблюдения.",
        "Наблюдение GOES не имеет собственного горизонта прогноза вперёд: "
        "часть окна после текущего момента расчёта (computed_at) всегда "
        "beyond_horizon для space_weather — «не покрыто», не «спокойно» "
        "(main-prompt.md §4). Геомагнитный модулятор (Kp>=5) и контекст шкалы "
        "R (main-prompt.md §11) в этой версии не подключены.",
        "Механизм 2 (MMOD, FN-39) вычислен из NASA MEO 'The 2024 meteor "
        "shower activity forecast for low Earth orbit' — годовой документ, "
        "покрывающий только 2024-01-01T00:00Z..2025-01-01T06:00Z; запрос вне "
        "этого диапазона честно даёт critical_gap/missing_data, а не "
        "спокойную обстановку. Геометрия станции (экранирование Землёй, "
        "относительная скорость встречи, src/domain/mmod/geometry.py, "
        "FN-32) в это число НЕ подмешана. NASA-показатель используется как "
        "unshielded/radiant-facing reference; ориентация конкретной поверхности, "
        "damage response и экранирование явно не моделируются "
        "(sources.yaml#nasa-meo-leo-forecast-2024).",
        "Траектория станции рассчитана по SGP4 на предоставленных элементах "
        "(см. orbit.elements_age_hours/is_reconstructed).",
    ]
    if raw_age_hours < 0:
        limitations.append(
            "Эпоха орбитальных элементов позже момента расчёта "
            f"(на {abs(raw_age_hours):.3f} ч) — вероятна рассинхронизация часов "
            "источника/сервиса; показанная давность — модуль этого смещения."
        )

    result: dict[str, Any] = {
        "result_id": result_id,
        "computed_at": iso_utc(now),
        "request": request.to_contract_dict(),
        "mode": "current",
        "as_of": None,
        "algorithm_version": ALGORITHM_VERSION,
        "data_manifest": [
            {
                "record_id": orbit_fetch.record_id,
                "source_id": orbit.SOURCE_ID_CURRENT,
                "source_version": orbit_fetch.source_version,
                "record_kind": "orbital_elements",
            },
            *forecast_manifest_entries,
            *observed_manifest_entries,
            *mmod_manifest_entries,
        ],
        "orbit": {
            "source": "celestrak",
            "norad_id": parsed.norad_id,
            "elements_epoch": iso_utc(parsed.epoch),
            "elements_age_hours": age_hours,
            "coordinate_system": elements.coordinate_system,
            "is_reconstructed": reconstructed,
            "record_id": orbit_fetch.record_id,
        },
        "windows": windows,
        "coverage": {"requested_period_supported": True, "archive_gaps": []},
        "limitations": limitations,
        "warnings": warnings,
        "recommendation": recommendation,
        "source_status": source_status,
    }
    _validate_result_or_raise(result)
    return result


# ---------------------------------------------------------------------------
# Исторические режимы (FN-41, этап 3)
#
# Две отдельные оркестрации ниже (_build_historical_analysis_result и
# _build_historical_forecast_result) НЕ являются одной функцией с флагом.
# Всё, что решает ПРИГОДНОСТЬ записи, живёт в них раздельно:
#   * орбита — какую функцию отбора выпуска передать в
#     _historical_orbit_stage (select_oem_elements_for_request с режимом
#     forecast/analysis, main-prompt.md §1);
#   * космопогода — какой выборкой получены уведомления
#     (_donki_records_for_forecast против _donki_records_for_analysis) и
#     чем ограничены интервалы подтверждённого покрытия;
#   * MMOD — какой момент передан как selection_as_of.
# Общими остаются только помощники, не принимающие таких решений: сетевой
# шлюз архива, интерполяция траектории, перевод готовой оценки в
# mechanismAssessment, сборка окна/манифеста/результата.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _HistoricalOrbitStage:
    """Готовая историческая геометрия одного расчёта: выбранный выпуск OEM,
    его запись в хранилище и покрытие расчётной сетки интерполяцией."""

    selection: orbit_history.OemSelection
    record_id: str
    source_version: str
    status: SourceStatus
    grid_points_total: int
    grid_points_covered: int
    fetch_errors: tuple[str, ...]


def _oem_config_enabled() -> bool:
    try:
        return orbit_history.load_source_config().enabled
    except Exception:  # noqa: BLE001 — см. _swpc_config_enabled.
        return True


def _donki_config_enabled() -> bool:
    try:
        return donki_source.load_source_config().enabled
    except Exception:  # noqa: BLE001 — см. _swpc_config_enabled.
        return True


def _historical_orbit_stage(
    conn: sqlite3.Connection,
    raw_store: RawOriginalStore,
    registry: SourceStatusRegistry,
    *,
    interval_start: datetime,
    interval_end: datetime,
    grid: list[datetime],
    now: datetime,
    include_lookahead: bool,
    select: Callable[[list[orbit_history.OemRelease]], orbit_history.OemSelection],
    log_ctx: dict[str, Any],
) -> _HistoricalOrbitStage:
    """Получает кандидатов OEM, отбирает выпуск переданной функцией ``select``
    и интерполирует траекторию на расчётной сетке.

    ``select`` — это и есть ветка пригодности: вызывающая сторона передаёт
    либо строгий отбор ``historical_forecast`` (``published_at <= as_of`` +
    полное покрытие интервала), либо отбор ``historical_analysis`` (без
    отсечения, с пометкой реконструкции). Этот помощник ни одного правила
    отбора не содержит и не может «перепутать флаг» — он не знает режима.

    Отказ получения или отсутствие пригодного выпуска — ``CalculationError``
    с ИМЕНОВАННЫМ кодом: без исторических элементов результат не строится
    вовсе, и современные элементы CelesTrak вместо них не подставляются ни
    при каких обстоятельствах (.ai/main-prompt.md §1, §11).
    """
    attempt_id = _fetch_attempt_id(orbit_history.SOURCE_ID, now=now)
    try:
        config = orbit_history.load_source_config()
    except Exception as exc:  # noqa: BLE001 — конфигурация источника недоступна:
        # таймауты/повторы для этой попытки неизвестны, считать на запасных
        # константах нельзя (main-prompt.md §7).
        raise CalculationError(
            "historical_orbit_config_unavailable",
            "конфигурация источника исторических элементов (sources.yaml → "
            f"{orbit_history.SOURCE_ID}) недоступна для этой попытки расчёта: "
            f"{sanitize_unexpected_error(exc)}",
        ) from exc

    if not config.enabled:
        raise CalculationError(
            "historical_orbit_source_disabled",
            f"источник {orbit_history.SOURCE_ID!r} отключён в sources.yaml; исторические "
            "орбитальные элементы не могут быть получены, а современные элементы "
            "CelesTrak вместо них не подставляются (.ai/main-prompt.md §11)",
        )

    try:
        archive = orbit_history.ensure_releases_for_interval(
            conn,
            raw_store,
            interval_start=interval_start,
            interval_end=interval_end,
            fetched_at=now,
            config=config,
            include_lookahead=include_lookahead,
        )
    except Exception as exc:  # noqa: BLE001 — индекс архива недоступен/не
        # разобран: кандидатов нет вовсе. Текст маскируется, если исключение
        # неожиданное; отказ виден в реестре источников и в коде ошибки задачи.
        message = (
            str(exc)
            if isinstance(exc, (orbit_history.OemListingFormatError, SourceHttpError))
            else sanitize_unexpected_error(exc)
        )
        registry.record_error(
            orbit_history.SOURCE_ID,
            at=now,
            message=message,
            quota_limited=isinstance(exc, SourceQuotaLimitedError),
        )
        _log(
            "oem_archive_fetch_failed",
            error=message,
            source_id=orbit_history.SOURCE_ID,
            fetch_attempt_id=attempt_id,
            **log_ctx,
        )
        raise CalculationError(
            "historical_orbit_source_error",
            f"архив исторических элементов (NASA TOPO OEM) недоступен: {message}. "
            "Результат не строится: современные элементы CelesTrak не подставляются "
            "вместо исторических (.ai/main-prompt.md §1, §11)",
        ) from exc

    _log(
        "oem_archive_fetch",
        source_id=orbit_history.SOURCE_ID,
        fetch_attempt_id=attempt_id,
        fetched=list(archive.fetched_release_dates),
        cached=list(archive.cached_release_dates),
        errors=list(archive.errors),
        **log_ctx,
    )

    if archive.releases:
        registry.record_success(orbit_history.SOURCE_ID, at=now)
        frozen = registry.get(orbit_history.SOURCE_ID).frozen
        status = _attempt_success_status(orbit_history.SOURCE_ID, at=now, frozen=frozen)
    else:
        message = "; ".join(archive.errors) or "ни одного датированного выпуска-кандидата"
        registry.record_error(
            orbit_history.SOURCE_ID, at=now, message=message, quota_limited=False
        )
        frozen = registry.get(orbit_history.SOURCE_ID).frozen
        status = _attempt_error_status(
            orbit_history.SOURCE_ID,
            at=now,
            message=message,
            quota_limited=False,
            frozen=frozen,
        )

    try:
        selection = select(list(archive.releases))
    except orbit.HistoricalElementsUnsupportedError as exc:
        raise CalculationError(
            "historical_orbit_archive_gap",
            f"{exc}. Кандидаты, полученные для этого интервала: "
            f"{sorted(archive.record_ids) or 'нет'}; отказы получения: "
            f"{list(archive.errors) or 'нет'}. Это именованный критический пробел "
            "архива/публикации, а не общий not_implemented и не повод взять "
            "непокрывающий выпуск или современные элементы (.ai/main-prompt.md §1, §11)",
        ) from exc

    release = selection.release
    record_id = archive.record_ids.get(release.release_date)
    if record_id is None:
        # Выпуск отобран, но его запись не сохранена — прослеживаемость до
        # первоисточника (main-prompt.md §3) была бы утрачена; результат с
        # record_id «которого нет» контракт всё равно не пропустит.
        raise CalculationError(
            "historical_orbit_record_missing",
            f"выпуск OEM {release.release_date!r} отобран, но его запись отсутствует в "
            "хранилище — результат без record_id использованных элементов не сохраняется",
        )

    nodes = [
        OemNode(
            time=vector.time,
            position_km=vector.position_km,
            velocity_km_s=vector.velocity_km_s,
        )
        for vector in release.parsed.state_vectors
    ]
    covered = 0
    for moment in grid:
        try:
            interpolate_state(nodes, moment)
        except InterpolationError:
            # Экстраполяция запрещена (src/domain/orbit/interpolate.py):
            # точка вне охваченного выпуском интервала остаётся НЕ
            # рассчитанной, а не «приблизительно такой же, как крайняя».
            continue
        covered += 1

    _log(
        "oem_trajectory_interpolated",
        record_id=record_id,
        release_date=release.release_date,
        grid_points=len(grid),
        grid_points_covered=covered,
        is_reconstruction=selection.is_reconstruction,
        **log_ctx,
    )

    source_version = (
        f"{release.s3_key}:{release.parsed.creation_date.isoformat()}:{release.etag}"
    )
    return _HistoricalOrbitStage(
        selection=selection,
        record_id=record_id,
        source_version=source_version,
        status=status,
        grid_points_total=len(grid),
        grid_points_covered=covered,
        fetch_errors=archive.errors,
    )


def _historical_orbit_dict(
    stage: _HistoricalOrbitStage, *, reference_moment: datetime
) -> dict[str, Any]:
    """``result.orbit`` для исторического режима.

    ``source = "nasa-iss-oem"`` — не ``celestrak`` и не ``space-track``:
    подставить современные элементы в исторический результат невозможно по
    построению (см. модульный docstring). ``elements_epoch`` — та же
    величина, что и ``orbital_elements_meta.epoch`` записи
    (``CREATION_DATE`` выпуска), ``elements_age_hours`` — её давность
    относительно момента, к которому расчёт привязан (``as_of`` для
    строгого прогноза, начало интересующего окна для разбора), а не
    относительно реального «сейчас» 2026 года, которое к исторической
    геометрии отношения не имеет.
    """
    release = stage.selection.release
    age_hours = abs((reference_moment - release.parsed.creation_date).total_seconds()) / 3600.0
    return {
        "source": "nasa-iss-oem",
        "norad_id": release.parsed.norad_id,
        "elements_epoch": iso_utc(release.parsed.creation_date),
        "elements_age_hours": age_hours,
        "coordinate_system": release.parsed.ref_frame,
        "is_reconstructed": stage.selection.is_reconstruction,
        "record_id": stage.record_id,
    }


def _donki_records_for_forecast(
    conn: sqlite3.Connection, *, as_of: datetime
) -> list[dict[str, Any]]:
    """Вход СТРОГОГО ``historical_forecast``: только ``published_at <= as_of``
    и ``replay_eligible`` (``select_as_of``, .ai/main-prompt.md §1).

    Отдельная функция от :func:`_donki_records_for_analysis` намеренно:
    правило пригодности не должно быть ветвью одной функции с флагом — флаг
    рано или поздно окажется не в том значении (§1). Уведомление с
    неразрешимым временем публикации сюда не попадает вовсе
    (``replay_eligible = false`` по правилу хранилища) — и это не «события
    не было», см. ``ambiguous_publication_count`` ниже по вызову.
    """
    return select_as_of(conn, as_of, source_id=donki_source.SOURCE_ID, record_kind="warning")


def _donki_records_for_analysis(
    conn: sqlite3.Connection, *, interval_start: datetime, interval_end: datetime
) -> list[dict[str, Any]]:
    """Вход ``historical_analysis``: весь архивный диапазон по ВРЕМЕНИ
    СОБЫТИЯ, без отсечения по публикации — ретроспективная реконструкция.

    Записи, опубликованные позже интересующего момента, здесь допустимы и
    ожидаемы, но обязаны быть помечены как реконструкция
    (:func:`_analysis_reconstruction_notes`) — они не являются входом
    строгого прогноза (.ai/main-prompt.md §1).
    """
    return select_observed_range(
        conn,
        source_id=donki_source.SOURCE_ID,
        record_kind="warning",
        start_at=interval_start,
        end_at=interval_end,
    )


def _analysis_reconstruction_notes(
    notifications: Sequence[ArchivedNotification], *, window_start: datetime
) -> list[str]:
    """Явная пометка «ретроспективная реконструкция» для линии космопогоды
    в ``historical_analysis`` — тот же принцип, что ``is_reconstruction`` у
    орбиты (``OemSelection``), распространённый на архивную событийную
    линию, как того требует FN-41.
    """
    later = [
        notification
        for notification in notifications
        if notification.published_at is not None and notification.published_at > window_start
    ]
    notes = [
        "Ретроспективная реконструкция: эта линия НЕ отсечена по времени "
        "публикации — в неё входят уведомления, выпущенные позже начала окна "
        "(режим historical_analysis, .ai/main-prompt.md §1). Как вход строгого "
        "прогноза из прошлого она непригодна; строгий режим "
        "(historical_forecast) отбирает записи отдельной функцией."
    ]
    if later:
        notes.append(
            f"{len(later)} из {len(notifications)} использованных уведомлений выпущены "
            f"позже начала окна ({iso_utc(window_start)}) — на момент начала ВКД они "
            "ещё не были доступны."
        )
    return notes


def _historical_space_weather_mechanism(
    assessment: HistoricalEventAssessment, *, status: str, extra_notes: list[str] | None = None
) -> dict[str, Any]:
    """``mechanismAssessment`` космопогоды для исторического окна.

    Соответствие состояния линии и ``status`` (contracts/result.schema.json):

    - ``NO_EVENT_DETECTED`` → ``ok`` с ``max_level = "background"`` и
      нулевой длительностью превышения **каждого** порога. Это не
      «пропуск, заменённый нулём» (что запрещает main-prompt.md §2), а
      вывод из подтверждённого покрытия: DONKI выпускает уведомление SEP
      при каждом пересечении порога 10 pfu (>10 МэВ) — того же порога
      S1 шкалы NOAA, — поэтому подтверждённое отсутствие таких уведомлений
      на полностью покрытом интервале означает, что порог S1 в нём не
      пересекался. Именно этим ``NO_EVENT_DETECTED`` отличается от
      ``INSUFFICIENT_DATA``, где покрытие не подтверждено и вывод сделать
      нельзя;
    - ``EVENT_PRESENT`` → ``qualitative_only``: событие подтверждено, но
      уровень и длительность превышения по этой линии не восстанавливаются
      (уведомление фиксирует пересечение порога, не профиль потока).
      ``max_level``/``exceedance_hours_by_level`` — ``null``, окно выводится
      из автоматического сравнения (``critical_gap``), но «не оценено»
      здесь явно не равно «спокойно»: ``event_state`` доходит до API, UI и
      обеих выгрузок;
    - ``INSUFFICIENT_DATA`` → ``missing_data``/``stale_data``/
      ``source_error``/``beyond_horizon`` — конкретную причину выбирает
      ветка режима (только она знает исход получения и отношение окна к
      ``as_of``), как и для наблюдения GOES (см. ``_space_weather_status``).
    """
    notes = list(assessment.notes)
    if extra_notes:
        notes.extend(extra_notes)
    quantified = status == "ok"
    return {
        "mechanism": "space_weather",
        "status": status,
        "event_state": assessment.state,
        "max_level": "background" if quantified else None,
        "exceedance_hours_by_level": (
            {"S1": 0.0, "S2": 0.0, "S3": 0.0} if quantified else None
        ),
        "coverage_fraction": assessment.coverage_fraction,
        "critical_gap": assessment.critical_gap,
        "notes": notes,
        "record_ids": list(assessment.record_ids),
    }


def _historical_space_weather_config_error_mechanism(message: str) -> dict[str, Any]:
    """``mechanismAssessment`` космопогоды, когда оценка не может быть
    построена вообще: конфигурация источника (``sources.yaml``) недоступна
    для этой попытки расчёта, а значит неизвестны ни квалифицирующие типы
    уведомлений, ни обратный запас.

    Тот же принцип, что у :func:`_space_weather_error_mechanism` для
    наблюдения GOES (round 1 ревью PR #29): честный отказ обработки вместо
    расчёта на запасных, зашитых в код числах (main-prompt.md §7 «пороги —
    в конфиге, не в коде»; §2 «отказ не подменяется правдоподобной
    оценкой»). ``event_state = INSUFFICIENT_DATA``, потому что состояние
    архивной линии действительно не определено — но по причине, не имеющей
    отношения к самому архиву, и причина названа в ``notes``.
    """
    return {
        "mechanism": "space_weather",
        "status": "source_error",
        "event_state": "INSUFFICIENT_DATA",
        "max_level": None,
        "exceedance_hours_by_level": None,
        "coverage_fraction": 0.0,
        "critical_gap": True,
        "notes": [message],
        "record_ids": [],
    }


def _historical_event_warning(
    assessment: HistoricalEventAssessment,
    *,
    window_id: str,
    attempt_id: str,
) -> dict[str, Any] | None:
    """Предупреждение по архивной событийной линии — доказуемое: либо
    ``record_ids`` конкретных уведомлений (``EVENT_PRESENT``), либо
    ``fetch_attempt_id`` залогированной попытки получения
    (``INSUFFICIENT_DATA``), contracts/result.schema.json «Каждое
    предупреждение доказуемо». ``NO_EVENT_DETECTED`` предупреждения не
    порождает — предупреждать не о чем."""
    if assessment.state == "EVENT_PRESENT":
        return {
            "code": "space-weather-archived-event-present",
            "severity": "critical",
            "mechanism": "space_weather",
            "message": (
                f"Окно {window_id}: в архиве есть уведомление(я) о квалифицирующем "
                f"событии космической погоды ({assessment.event_count} событие(й) по "
                f"{len(assessment.record_ids)} записи(ям)) в интервале "
                f"[{iso_utc(assessment.required_start)}, {iso_utc(assessment.window_end)}]. "
                "Уровень и длительность превышения порогов по этой линии не "
                "восстанавливаются — окно не сравнивается автоматически, но это "
                "подтверждённое воздействие, а не недостаток данных."
            ),
            "record_ids": list(assessment.record_ids),
            "fetch_attempt_id": attempt_id,
            "window_id": window_id,
        }
    if assessment.state == "INSUFFICIENT_DATA":
        return {
            "code": "space-weather-archive-coverage-unconfirmed",
            "severity": "advisory",
            "mechanism": "space_weather",
            "message": (
                f"Окно {window_id}: покрытие архивной событийной линии на интервал "
                f"[{iso_utc(assessment.required_start)}, {iso_utc(assessment.window_end)}] "
                "подтвердить не удалось — оценить наличие события невозможно. Это "
                "критический пробел, а не вывод «событий не было» (main-prompt.md §2)."
            ),
            "record_ids": list(assessment.record_ids),
            "fetch_attempt_id": attempt_id,
            "window_id": window_id,
        }
    return None


def _donki_manifest_entries(
    record_ids: Iterable[str], records_by_id: Mapping[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Манифест архивной линии — только ФАКТИЧЕСКИ использованные записи
    (main-prompt.md §3), той же формой, что и остальные записи манифеста."""
    return [
        {
            "record_id": record_id,
            "source_id": donki_source.SOURCE_ID,
            "source_version": records_by_id[record_id]["source_version"],
            "record_kind": "warning",
        }
        for record_id in sorted(set(record_ids))
        if record_id in records_by_id
    ]


def _historical_donki_stage(
    conn: sqlite3.Connection,
    raw_store: RawOriginalStore,
    registry: SourceStatusRegistry,
    *,
    interval_start: datetime,
    interval_end: datetime,
    now: datetime,
    log_ctx: dict[str, Any],
) -> tuple[
    donki_source.DonkiFetchOutcome | None,
    donki_source.DonkiSourceConfig | None,
    str,
    str | None,
]:
    """Общий сетевой шлюз архивной линии: один запрос на расчёт, покрывающий
    оба окна вместе с обратным запасом.

    ``interval_start``/``interval_end`` — границы САМИХ окон; запрашиваемый
    период расширяется назад на ``event_persistence_lookback_hours`` из
    ``sources.yaml``, потому что ровно этот интервал оценке и понадобится
    (уже объявленное событие может продолжаться в окне — см.
    ``src/domain/spaceweather/historical_events.py``). Если бы запас
    применялся только в расчёте, но не в запросе, покрытие на нужный
    интервал никогда не подтверждалось бы, и каждое окно получало бы
    INSUFFICIENT_DATA по чисто технической причине.

    Ничего не решает о пригодности записей (это делают
    :func:`_donki_records_for_forecast`/:func:`_donki_records_for_analysis`
    в своих ветках) — только получает и сохраняет, честно возвращая исход.
    ``None`` в первом элементе — непредвиденный отказ шлюза: ни одной записи
    не получено и покрытия нет (never «событий не было», main-prompt.md §2).
    """
    attempt_id = _fetch_attempt_id(donki_source.SOURCE_ID, now=now)
    # Конфигурация читается ОТДЕЛЬНЫМ шагом от получения: её отсутствие и
    # отказ сети — разные ситуации с разными последствиями. Без конфигурации
    # неизвестны ни таймауты, ни пороги оценки (квалифицирующие типы,
    # обратный запас) — считать на запасных константах нельзя
    # (main-prompt.md §7, round 1 ревью PR #29 по тому же поводу для swpc);
    # при живом отказе сети конфигурация, наоборот, известна и должна
    # дойти до ветки режима, чтобы оценка объяснила пробел теми же
    # порогами, что и при успехе.
    config: donki_source.DonkiSourceConfig | None = None
    try:
        config = donki_source.load_source_config()
    except Exception as exc:  # noqa: BLE001 — см. _swpc_config_enabled/
        # _window_observed_mechanism: отсутствие конфигурации — честный отказ
        # обработки, а не расчёт на угаданных числах.
        message = sanitize_unexpected_error(exc)
        registry.record_error(
            donki_source.SOURCE_ID, at=now, message=message, quota_limited=False
        )
        _log(
            "donki_config_unavailable",
            error=message,
            source_id=donki_source.SOURCE_ID,
            fetch_attempt_id=attempt_id,
            **log_ctx,
        )
        return None, None, attempt_id, message

    try:
        outcome = donki_source.fetch_and_store_window(
            conn,
            raw_store,
            config=config,
            registry=registry,
            interval_start=(
                interval_start - timedelta(hours=config.event_persistence_lookback_hours)
            ),
            interval_end=interval_end,
            fetched_at=now,
        )
    except Exception as exc:  # noqa: BLE001 — отказ одного источника не роняет
        # расчёт целиком (main-prompt.md §5); текст маскируется — он мог бы
        # содержать URL с ключом API (main-prompt.md §7).
        message = sanitize_unexpected_error(exc)
        registry.record_error(
            donki_source.SOURCE_ID, at=now, message=message, quota_limited=False
        )
        _log(
            "donki_fetch_failed",
            error=message,
            source_id=donki_source.SOURCE_ID,
            fetch_attempt_id=attempt_id,
            **log_ctx,
        )
        return None, config, attempt_id, message

    _log(
        "donki_fetch",
        outcome=outcome.outcome,
        source_id=donki_source.SOURCE_ID,
        fetch_attempt_id=attempt_id,
        stored=len(outcome.stored_record_ids),
        skipped_without_event_time=outcome.skipped_without_event_time,
        **log_ctx,
    )
    return outcome, config, attempt_id, outcome.message


def _donki_attempt_status(
    outcome: donki_source.DonkiFetchOutcome | None,
    *,
    registry: SourceStatusRegistry,
    now: datetime,
    unexpected_error: str | None,
) -> SourceStatus:
    """Статус источника, каким его увидела ИМЕННО эта попытка — тот же
    принцип, что и :func:`_swpc_attempt_status` (round 2 ревью PR #19)."""
    frozen = registry.get(donki_source.SOURCE_ID).frozen
    if outcome is None:
        return _attempt_error_status(
            donki_source.SOURCE_ID,
            at=now,
            message=unexpected_error or "unexpected error",
            quota_limited=False,
            frozen=frozen,
        )
    if outcome.outcome == "stored":
        return _attempt_success_status(donki_source.SOURCE_ID, at=now, frozen=frozen)
    if outcome.outcome.startswith("error_"):
        return _attempt_error_status(
            donki_source.SOURCE_ID,
            at=now,
            message=outcome.message or outcome.outcome,
            quota_limited=(outcome.outcome == "error_quota"),
            frozen=frozen,
        )
    return registry.get(donki_source.SOURCE_ID)


_HISTORICAL_LIMITATIONS_COMMON = [
    "Исторические орбитальные элементы — датированный выпуск NASA TOPO CCSDS "
    "OEM (sources.yaml#nasa-iss-oem-history): готовые векторы состояния и "
    "кубическая интерполяция Эрмита (src/domain/orbit/interpolate.py), не "
    "SGP4 по GP/TLE. Современные элементы CelesTrak в исторический результат "
    "не подставляются ни при каких обстоятельствах (main-prompt.md §1, §11); "
    "published_at выпуска — S3 LastModified объекта, не CREATION_DATE.",
    "Механизм 1 в исторических режимах оценивается АРХИВНОЙ СОБЫТИЙНОЙ линией "
    "(уведомления NASA CCMC DONKI): она даёт три различимых состояния "
    "(event_state: EVENT_PRESENT / NO_EVENT_DETECTED / INSUFFICIENT_DATA), но "
    "не измеренное значение потока — наблюдение GOES "
    "(noaa-swpc-proton-flux) архива за 2024 год не хранит вовсе, а архив "
    "NOAA 3-Day покрывает период не полностью. Какая из архивных линий "
    "(DONKI или NOAA SWPC Forecast Discussion) является основной, FN-41 "
    "намеренно НЕ решает: DONKI выбран как единственная линия с полным "
    "покрытием обязательного периода (у линии NOAA — подтверждённый пробел "
    "15.05–16.06.2024), это решение реализации этой задачи, а не итоговый "
    "архитектурный ответ.",
]


def _build_historical_analysis_result(
    request: CalculationRequest,
    *,
    conn: sqlite3.Connection,
    raw_store: RawOriginalStore,
    registry: SourceStatusRegistry,
    settings: Settings,
    now: datetime,
    task_id: str | None = None,
) -> dict[str, Any]:
    """Исторический РАЗБОР: полный архив без отсечения по публикации.

    Отдельная ветка от :func:`_build_historical_forecast_result`
    (.ai/main-prompt.md §1). Всё, что здесь допускает записи «позже
    интересующего момента», обязано быть помечено как ретроспективная
    реконструкция: орбита — ``OemSelection.is_reconstruction`` →
    ``result.orbit.is_reconstructed``; космопогода —
    :func:`_analysis_reconstruction_notes` в ``notes`` каждого окна.
    """
    result_id = f"res-{uuid.uuid4()}"
    log_ctx = {"task_id": task_id, "result_id": result_id}
    warnings: list[dict[str, Any]] = []

    calc_hours = (request.calc_end_at - request.start_at).total_seconds() / 3600.0
    grid = time_grid(
        request.start_at,
        hours=calc_hours,
        step_minutes=settings.orbit_step_minutes,
        max_hours=32.0,
    )

    orbit_stage = _historical_orbit_stage(
        conn,
        raw_store,
        registry,
        interval_start=request.start_at,
        interval_end=request.calc_end_at,
        grid=grid,
        now=now,
        # Разбор предпочитает выпуск, созданный вскоре ПОСЛЕ интересующего
        # момента (ближе к фактически прошедшей траектории) — поэтому набор
        # кандидатов на скачивание шире вперёд.
        include_lookahead=True,
        select=lambda releases: orbit.select_oem_elements_for_request(
            "historical_analysis", releases, moment=request.start_at
        ),
        log_ctx=log_ctx,
    )

    donki_outcome, donki_config, donki_attempt_id, donki_error = _historical_donki_stage(
        conn,
        raw_store,
        registry,
        interval_start=request.start_at,
        interval_end=request.calc_end_at,
        now=now,
        log_ctx=log_ctx,
    )
    donki_status = _donki_attempt_status(
        donki_outcome, registry=registry, now=now, unexpected_error=donki_error
    )
    fetch_ok = donki_outcome is not None and donki_outcome.outcome == "stored"
    if not fetch_ok:
        warnings.append(
            {
                "code": "space-weather-archive-fetch-failed",
                "severity": "advisory",
                "mechanism": "space_weather",
                "message": (
                    "Получение архивной событийной линии (DONKI) не удалось: "
                    f"{donki_error or (donki_outcome.message if donki_outcome else 'нет ответа')}. "
                    "Ранее сохранённые записи (если есть) всё равно используются, но "
                    "подтвердить покрытие этой попыткой нельзя — окна получают "
                    "INSUFFICIENT_DATA, а не «событий не было» (main-prompt.md §2)."
                ),
                "record_ids": [],
                "fetch_attempt_id": donki_attempt_id,
                "window_id": None,
            }
        )

    coverage_intervals: list[tuple[datetime, datetime]] = []
    if donki_outcome is not None and donki_outcome.covered_start is not None:
        assert donki_outcome.covered_end is not None
        coverage_intervals.append((donki_outcome.covered_start, donki_outcome.covered_end))

    records: list[dict[str, Any]] = []
    notifications: list[ArchivedNotification] = []
    if donki_config is not None:
        records = _donki_records_for_analysis(
            conn,
            interval_start=(
                request.start_at
                - timedelta(hours=donki_config.event_persistence_lookback_hours)
            ),
            # select_observed_range — полуоткрытый интервал [start, end): без
            # этого микросекундного запаса событие, совпавшее ровно с концом
            # последнего окна, не попало бы в выборку.
            interval_end=request.calc_end_at + timedelta(microseconds=1),
        )
        notifications = archived_notifications_from_records(records)
    records_by_id = {str(record["record_id"]): record for record in records}

    windows: list[dict[str, Any]] = []
    used_record_ids: set[str] = set()
    for window_id, start_at in (
        ("win-a", request.start_at),
        ("win-b", request.search_end_at),
    ):
        end_at = start_at + timedelta(hours=request.duration_hours)
        if donki_config is None:
            # Пороги оценки неизвестны — честный отказ обработки, а не
            # расчёт на зашитых в код запасных числах (main-prompt.md §7).
            mechanism = _historical_space_weather_config_error_mechanism(
                "Оценка архивной событийной линии не построена: конфигурация "
                "источника (sources.yaml → nasa-donki-notifications) недоступна "
                f"для этой попытки расчёта ({donki_error})."
            )
        else:
            assessment = assess_archived_events(
                notifications,
                window_start=start_at,
                window_end=end_at,
                coverage_intervals=coverage_intervals,
                qualifying_message_types=donki_config.qualifying_message_types,
                persistence_lookback_hours=(
                    donki_config.event_persistence_lookback_hours
                ),
                # Разбор видит записи с неизвестным временем публикации как есть
                # (они попадают в выборку по времени события) — отдельной поправки
                # «нечем подтвердить отсутствие» здесь не нужно, в отличие от
                # строгой ветки, где такие записи невидимы по построению.
                ambiguous_publication_count=0,
            )
            if assessment.state == "INSUFFICIENT_DATA" and not fetch_ok:
                status = "source_error"
            elif assessment.state == "INSUFFICIENT_DATA":
                status = "missing_data"
            elif assessment.state == "EVENT_PRESENT":
                status = "qualitative_only"
            else:
                status = "ok"

            mechanism = _historical_space_weather_mechanism(
                assessment,
                status=status,
                extra_notes=_analysis_reconstruction_notes(
                    notifications, window_start=start_at
                ),
            )
            warning = _historical_event_warning(
                assessment, window_id=window_id, attempt_id=donki_attempt_id
            )
            if warning is not None:
                warnings.append(warning)
        used_record_ids.update(mechanism["record_ids"])

        mmod_mechanism = _mmod_mechanism_assessment(
            conn,
            raw_store,
            start_at=start_at,
            duration_hours=request.duration_hours,
            now=now,
            # Разбор не отсекает по публикации: пригодны все версии, уже
            # существующие к моменту расчёта.
            selection_as_of=now,
            log_ctx=log_ctx,
        )
        windows.append(
            _window(
                window_id,
                start_at,
                request.duration_hours,
                space_weather_mechanism=mechanism,
                mmod_mechanism=mmod_mechanism,
            )
        )

    recommendation = _apply_window_dominance(windows)

    return _assemble_historical_result(
        request,
        result_id=result_id,
        now=now,
        as_of=None,
        orbit_stage=orbit_stage,
        orbit_reference_moment=request.start_at,
        windows=windows,
        recommendation=recommendation,
        warnings=warnings,
        donki_manifest=_donki_manifest_entries(used_record_ids, records_by_id),
        donki_status=donki_status,
        conn=conn,
        now_for_mmod_manifest=now,
        archive_gaps=_historical_archive_gaps(
            fetch_ok=fetch_ok,
            windows=windows,
            gap_start=(
                request.start_at
                - timedelta(
                    hours=(
                        donki_config.event_persistence_lookback_hours
                        if donki_config is not None
                        else 0.0
                    )
                )
            ),
            gap_end=request.calc_end_at,
            as_of=None,
        ),
        extra_limitations=[
            "Режим historical_analysis: записи, выпущенные позже интересующего "
            "момента, допускаются и используются, но явно помечены как "
            "ретроспективная реконструкция (orbit.is_reconstructed, notes окна). "
            "Как проверка прогноза из прошлого этот результат непригоден — для "
            "этого есть отдельный режим historical_forecast.",
        ],
    )


def _build_historical_forecast_result(
    request: CalculationRequest,
    *,
    conn: sqlite3.Connection,
    raw_store: RawOriginalStore,
    registry: SourceStatusRegistry,
    settings: Settings,
    now: datetime,
    task_id: str | None = None,
) -> dict[str, Any]:
    """СТРОГИЙ прогноз из прошлого: во вход попадают только записи с
    ``published_at <= as_of`` и ``replay_eligible`` — по КАЖДОЙ линии, и по
    орбите, и по космопогоде (.ai/main-prompt.md §1).

    Отдельная ветка от :func:`_build_historical_analysis_result`: ни одной
    общей функции, принимающей решение о пригодности записи, у них нет.
    Обязательный тест на утечку (``published_at > as_of`` не меняет
    результат ни в одном поле) идёт через эту функцию целиком, а не через
    изолированную выборку (tests/api/test_requests.py).
    """
    assert request.as_of is not None  # гарантировано схемой запроса
    as_of = request.as_of
    result_id = f"res-{uuid.uuid4()}"
    log_ctx = {"task_id": task_id, "result_id": result_id}
    warnings: list[dict[str, Any]] = []

    calc_hours = (request.calc_end_at - request.start_at).total_seconds() / 3600.0
    grid = time_grid(
        request.start_at,
        hours=calc_hours,
        step_minutes=settings.orbit_step_minutes,
        max_hours=32.0,
    )

    orbit_stage = _historical_orbit_stage(
        conn,
        raw_store,
        registry,
        interval_start=request.start_at,
        interval_end=request.calc_end_at,
        grid=grid,
        now=now,
        # Строгая ветка не смотрит вперёд: выпуск, появившийся в архиве
        # позже as_of, не может быть входом прогноза из прошлого.
        include_lookahead=False,
        select=lambda releases: orbit.select_oem_elements_for_request(
            "historical_forecast",
            releases,
            as_of=as_of,
            interval_start=request.start_at,
            interval_end=request.calc_end_at,
        ),
        log_ctx=log_ctx,
    )

    donki_outcome, donki_config, donki_attempt_id, donki_error = _historical_donki_stage(
        conn,
        raw_store,
        registry,
        interval_start=request.start_at,
        interval_end=request.calc_end_at,
        now=now,
        log_ctx=log_ctx,
    )
    donki_status = _donki_attempt_status(
        donki_outcome, registry=registry, now=now, unexpected_error=donki_error
    )
    fetch_ok = donki_outcome is not None and donki_outcome.outcome == "stored"
    if not fetch_ok:
        warnings.append(
            {
                "code": "space-weather-archive-fetch-failed",
                "severity": "advisory",
                "mechanism": "space_weather",
                "message": (
                    "Получение архивной событийной линии (DONKI) не удалось: "
                    f"{donki_error or (donki_outcome.message if donki_outcome else 'нет ответа')}. "
                    "Отказ получения не превращается в вывод «событий не было» "
                    "(main-prompt.md §2)."
                ),
                "record_ids": [],
                "fetch_attempt_id": donki_attempt_id,
                "window_id": None,
            }
        )

    # Подтверждённое покрытие СТРОГОЙ ветки обрезается моментом отсечения:
    # то, что архив опубликовал позже as_of, не может подтверждать полноту
    # знания на as_of (.ai/main-prompt.md §1).
    coverage_intervals: list[tuple[datetime, datetime]] = []
    if donki_outcome is not None and donki_outcome.covered_start is not None:
        assert donki_outcome.covered_end is not None
        eligible_end = min(donki_outcome.covered_end, as_of)
        if eligible_end > donki_outcome.covered_start:
            coverage_intervals.append((donki_outcome.covered_start, eligible_end))

    records = _donki_records_for_forecast(conn, as_of=as_of) if donki_config is not None else []
    records_by_id = {str(record["record_id"]): record for record in records}
    notifications = archived_notifications_from_records(records)

    # Уведомления, чьё время публикации неразрешимо (два независимых поля
    # расходятся): в строгую выборку они не попадают по построению
    # (replay_eligible=false), поэтому их отсутствие там нельзя читать как
    # «события не было» — считаем их отдельно и отдаём в оценку.
    ambiguous_count = (
        donki_outcome.ambiguous_publication_count if donki_outcome is not None else 0
    )

    windows: list[dict[str, Any]] = []
    used_record_ids: set[str] = set()
    for window_id, start_at in (
        ("win-a", request.start_at),
        ("win-b", request.search_end_at),
    ):
        end_at = start_at + timedelta(hours=request.duration_hours)
        if donki_config is None:
            # См. ту же ветку в _build_historical_analysis_result: без
            # конфигурации пороги оценки неизвестны (main-prompt.md §7).
            mechanism = _historical_space_weather_config_error_mechanism(
                "Оценка архивной событийной линии не построена: конфигурация "
                "источника (sources.yaml → nasa-donki-notifications) недоступна "
                f"для этой попытки расчёта ({donki_error})."
            )
        else:
            assessment = assess_archived_events(
                notifications,
                window_start=start_at,
                window_end=end_at,
                coverage_intervals=coverage_intervals,
                qualifying_message_types=donki_config.qualifying_message_types,
                persistence_lookback_hours=(
                    donki_config.event_persistence_lookback_hours
                ),
                ambiguous_publication_count=ambiguous_count,
            )
            if assessment.state == "EVENT_PRESENT":
                status = "qualitative_only"
            elif assessment.state == "NO_EVENT_DETECTED":
                status = "ok"
            elif not fetch_ok:
                status = "source_error"
            elif end_at > as_of:
                # Главный честный случай строгого режима: событийная линия
                # DONKI выпускает уведомление в момент НАЧАЛА события и не
                # является прогнозным продуктом с горизонтом — за отсечением
                # она не покрывает окно вовсе. «Не покрыто», а не «спокойно»
                # (main-prompt.md §4).
                status = "beyond_horizon"
            else:
                status = "missing_data"

            mechanism = _historical_space_weather_mechanism(assessment, status=status)
            warning = _historical_event_warning(
                assessment, window_id=window_id, attempt_id=donki_attempt_id
            )
            if warning is not None:
                warnings.append(warning)
        used_record_ids.update(mechanism["record_ids"])

        mmod_mechanism = _mmod_mechanism_assessment(
            conn,
            raw_store,
            start_at=start_at,
            duration_hours=request.duration_hours,
            now=now,
            # Строгое отсечение распространяется на КАЖДЫЙ механизм, не
            # только на космопогоду и орбиту (приёмка FN-41).
            selection_as_of=as_of,
            log_ctx=log_ctx,
        )
        windows.append(
            _window(
                window_id,
                start_at,
                request.duration_hours,
                space_weather_mechanism=mechanism,
                mmod_mechanism=mmod_mechanism,
            )
        )

    recommendation = _apply_window_dominance(windows)

    return _assemble_historical_result(
        request,
        result_id=result_id,
        now=now,
        as_of=as_of,
        orbit_stage=orbit_stage,
        orbit_reference_moment=as_of,
        windows=windows,
        recommendation=recommendation,
        warnings=warnings,
        donki_manifest=_donki_manifest_entries(used_record_ids, records_by_id),
        donki_status=donki_status,
        conn=conn,
        now_for_mmod_manifest=as_of,
        archive_gaps=_historical_archive_gaps(
            fetch_ok=fetch_ok,
            windows=windows,
            gap_start=as_of,
            gap_end=request.calc_end_at,
            as_of=as_of,
        ),
        extra_limitations=[
            "Режим historical_forecast: во вход расчёта попадают только записи с "
            f"published_at <= {iso_utc(as_of)} и replay_eligible=true — по всем "
            "линиям (орбита NASA OEM по S3 LastModified, архивная событийная линия "
            "DONKI, NASA MEO). Архивная событийная линия при этом публикует "
            "уведомление в момент НАЧАЛА события и не является прогнозным "
            "продуктом: для окна целиком после отсечения она честно даёт "
            "beyond_horizon («не покрыто»), а вывод «событий не будет» по ней "
            "невозможен в принципе (main-prompt.md §4). Горизонт собственного "
            "прогноза (6 ч против 24 ч) FN-41 намеренно не решает.",
        ],
    )


def _historical_archive_gaps(
    *,
    fetch_ok: bool,
    windows: list[dict[str, Any]],
    gap_start: datetime,
    gap_end: datetime,
    as_of: datetime | None,
) -> list[dict[str, Any]]:
    """``coverage.archive_gaps`` для исторического результата — пробел
    архива виден в результате, а не маскируется «спокойным» периодом
    (contracts/result.schema.json, main-prompt.md §2)."""
    unconfirmed = [
        window["window_id"]
        for window in windows
        for mechanism in window["mechanisms"]
        if mechanism["mechanism"] == "space_weather"
        and mechanism["event_state"] == "INSUFFICIENT_DATA"
    ]
    if not unconfirmed:
        return []
    if not fetch_ok:
        note = (
            "Получение архивной событийной линии не удалось — покрытие на этот "
            f"интервал не подтверждено (окна: {', '.join(unconfirmed)}). "
            "Отказ источника не превращается в вывод «событий не было»."
        )
    elif as_of is not None:
        note = (
            f"За моментом отсечения ({iso_utc(as_of)}) архивная событийная линия "
            "DONKI пригодных записей не даёт: она публикует уведомление в момент "
            "начала события, поэтому будущее относительно отсечения окно "
            f"(окна: {', '.join(unconfirmed)}) — «не покрыто», а не «спокойно» "
            "(main-prompt.md §4)."
        )
    else:
        note = (
            "Покрытие архивной событийной линии на этот интервал подтвердить не "
            f"удалось (окна: {', '.join(unconfirmed)}) — оценить наличие события "
            "невозможно."
        )
    return [
        {
            "source_id": donki_source.SOURCE_ID,
            "gap_start": iso_utc(gap_start),
            "gap_end": iso_utc(gap_end),
            "note": note,
        }
    ]


def _assemble_historical_result(
    request: CalculationRequest,
    *,
    result_id: str,
    now: datetime,
    as_of: datetime | None,
    orbit_stage: _HistoricalOrbitStage,
    orbit_reference_moment: datetime,
    windows: list[dict[str, Any]],
    recommendation: dict[str, Any],
    warnings: list[dict[str, Any]],
    donki_manifest: list[dict[str, Any]],
    donki_status: SourceStatus,
    conn: sqlite3.Connection,
    now_for_mmod_manifest: datetime,
    archive_gaps: list[dict[str, Any]],
    extra_limitations: list[str],
) -> dict[str, Any]:
    """Сборка сохраняемого результата для обоих исторических режимов.

    Намеренно общая: форма результата, манифест, ограничения и валидация —
    не решения о пригодности записей, а одна и та же дисциплина контракта
    (main-prompt.md §3, «интерфейс и обе выгрузки читают один и тот же
    сохранённый объект»). Параллельной формы результата для исторических
    режимов не создаётся: используются те же ``_validate_result_or_raise`` и
    ``store_result``, что и для ``mode=current``.
    """
    mmod_manifest_record_ids = sorted(
        {
            record_id
            for window in windows
            for mechanism in window["mechanisms"]
            if mechanism["mechanism"] == "mmod"
            for record_id in mechanism["record_ids"]
        }
    )
    mmod_records_by_id: dict[str, dict[str, Any]] = {}
    if mmod_manifest_record_ids:
        mmod_records_by_id = {
            str(r["record_id"]): r
            for r in select_as_of(
                conn,
                now_for_mmod_manifest,
                source_id=mmod_source.SOURCE_ID,
                record_kind="forecast",
            )
        }
    mmod_manifest_entries = [
        {
            "record_id": record_id,
            "source_id": mmod_source.SOURCE_ID,
            "source_version": mmod_records_by_id[record_id]["source_version"],
            "record_kind": "forecast",
        }
        for record_id in mmod_manifest_record_ids
        if record_id in mmod_records_by_id
    ]

    limitations = [*_HISTORICAL_LIMITATIONS_COMMON, *extra_limitations]
    if orbit_stage.grid_points_covered < orbit_stage.grid_points_total:
        limitations.append(
            f"Траектория рассчитана на {orbit_stage.grid_points_covered} из "
            f"{orbit_stage.grid_points_total} узлов расчётной сетки: остальные лежат "
            "вне интервала выбранного выпуска OEM, а экстраполяция запрещена "
            "(src/domain/orbit/interpolate.py) — непокрытые моменты остались без "
            "положения станции, а не приближены крайним известным."
        )
    if orbit_stage.fetch_errors:
        limitations.append(
            "Часть датированных выпусков-кандидатов OEM не получена "
            f"({'; '.join(orbit_stage.fetch_errors)}) — отбор шёл среди оставшихся."
        )

    result: dict[str, Any] = {
        "result_id": result_id,
        "computed_at": iso_utc(now),
        "request": request.to_contract_dict(),
        "mode": request.mode,
        "as_of": iso_utc(as_of) if as_of is not None else None,
        "algorithm_version": ALGORITHM_VERSION,
        "data_manifest": [
            {
                "record_id": orbit_stage.record_id,
                "source_id": orbit_history.SOURCE_ID,
                "source_version": orbit_stage.source_version,
                "record_kind": "orbital_elements",
            },
            *donki_manifest,
            *mmod_manifest_entries,
        ],
        "orbit": _historical_orbit_dict(orbit_stage, reference_moment=orbit_reference_moment),
        "windows": windows,
        "coverage": {"requested_period_supported": True, "archive_gaps": archive_gaps},
        "limitations": limitations,
        "warnings": warnings,
        "recommendation": recommendation,
        "source_status": [
            _status_dict(orbit_stage.status, config_enabled=_oem_config_enabled()),
            _status_dict(donki_status, config_enabled=_donki_config_enabled()),
        ],
    }
    _validate_result_or_raise(result)
    return result


def ensure_store_ready(settings: Settings) -> None:
    """Открывает и сразу закрывает соединение с хранилищем один раз при
    старте приложения, чтобы файл SQLite и его схема (``CREATE TABLE IF NOT
    EXISTS`` + ``PRAGMA journal_mode = WAL``, см. ``src/store/schema.py``)
    были созданы до первого конкурентного запроса.

    Без этого первый набор одновременных запросов сам гонится за созданием
    файла БД: несколько потоков одновременно открывают ``sqlite3.connect``
    на ещё не существующий файл и применяют DDL, и SQLite отвечает
    ``OperationalError: database is locked`` части из них — не отказ
    источника и не бизнес-ошибка, а инфраструктурная гонка первого доступа,
    которая ломала бы приёмку FN-26 «два конкурентных запроса не смешивают
    статусы» на первом же обращении к свежему хранилищу. ``connect()`` сам
    по себе идемпотентен (``IF NOT EXISTS``) — после однократного
    последовательного вызова здесь конкурентные ``connect()`` из
    :func:`run_calculation`/:func:`refresh_sources` лишь открывают
    независимые соединения к уже готовой схеме и не гонятся за её созданием.
    """
    connect_store(settings.store_db_path).close()


def run_calculation(
    request: CalculationRequest,
    *,
    settings: Settings,
    raw_store: RawOriginalStore,
    registry: SourceStatusRegistry,
    now: datetime | None = None,
    task_id: str | None = None,
) -> str:
    """Выполняет расчёт и сохраняет результат. Возвращает ``result_id``.

    Открывает собственное соединение SQLite для этого вызова (не делит
    соединение с другими конкурентными расчётами) — так конкурентные задачи в
    разных потоках не соревнуются за один и тот же объект `sqlite3.Connection`
    (.ai/backend-prompt.md §2 «одновременные запросы изолированы»); SQLite в
    режиме WAL (src/store/schema.py) поддерживает несколько независимых
    соединений к одному файлу.
    """
    moment = now if now is not None else datetime.now(timezone.utc)
    _log("job_started", task_id=task_id, mode=request.mode)
    conn = connect_store(settings.store_db_path)
    try:
        # FN-41: три режима — три раздельные оркестрации, выбираемые ЗДЕСЬ, а
        # не флагом внутри общей функции (.ai/main-prompt.md §1 «три режима не
        # смешиваются»). Прежний общий отказ historical_mode_not_implemented
        # снят: исторические режимы либо дают сохранённый неизменяемый
        # результат, либо отказывают ИМЕНОВАННЫМ кодом критического пробела
        # (historical_orbit_archive_gap и соседние), но уже никогда не «режим
        # не реализован».
        builders: dict[str, _ResultBuilder] = {
            "current": _build_current_result,
            "historical_analysis": _build_historical_analysis_result,
            "historical_forecast": _build_historical_forecast_result,
        }
        build = builders[request.mode]
        result = build(
            request,
            conn=conn,
            raw_store=raw_store,
            registry=registry,
            settings=settings,
            now=moment,
            task_id=task_id,
        )
        store_result(conn, result)
        _log("result_stored", task_id=task_id, result_id=result["result_id"], mode=result["mode"])
        return str(result["result_id"])
    except CalculationError as exc:
        _log("job_failed", task_id=task_id, mode=request.mode, code=exc.code, message=exc.message)
        raise
    finally:
        conn.close()


def refresh_sources(
    *,
    settings: Settings,
    raw_store: RawOriginalStore,
    registry: SourceStatusRegistry,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Принудительно обновляет все известные источники (``force=True``,
    только для ``mode=current`` — README «Endpoint shapes для S1-07»)."""
    moment = now if now is not None else datetime.now(timezone.utc)
    conn = connect_store(settings.store_db_path)
    try:
        orbit_result = fetch_and_store_orbit(conn, raw_store, registry, now=moment)
        _log("orbit_refresh", outcome=orbit_result.outcome)
        try:
            swpc_cfg = swpc_source.load_source_config()
            swpc_outcome = swpc_source.fetch_and_store(
                conn, raw_store, config=swpc_cfg, registry=registry, now=moment, force=True
            )
            _log("swpc_refresh", outcome=swpc_outcome.outcome)
            swpc_enabled = swpc_cfg.enabled
        except Exception as exc:  # noqa: BLE001 — см. _build_current_result;
            # сообщение маскируется тем же способом (round 2 ревью PR #19) —
            # оно попадёт в общий реестр, видимый любому вызову
            # /sources/status.
            registry.record_error(
                swpc_source.SOURCE_ID,
                at=moment,
                message=sanitize_unexpected_error(exc),
                quota_limited=False,
            )
            swpc_enabled = True

        try:
            noaa_3day_cfg = noaa_3day_source.load_source_config()
            noaa_3day_outcome = noaa_3day_source.fetch_and_store(
                conn, raw_store, config=noaa_3day_cfg, registry=registry, now=moment, force=True
            )
            _log("noaa_3day_forecast_refresh", outcome=noaa_3day_outcome.outcome)
            noaa_3day_enabled = noaa_3day_cfg.enabled
        except Exception as exc:  # noqa: BLE001 — см. swpc-блок выше.
            registry.record_error(
                noaa_3day_source.SOURCE_ID,
                at=moment,
                message=sanitize_unexpected_error(exc),
                quota_limited=False,
            )
            noaa_3day_enabled = True

        # FN-41: архивные источники (NASA OEM, DONKI) в принудительное
        # обновление НЕ включены намеренно. У архивного продукта нет
        # «текущего» состояния, которое можно освежить: его запрос всегда
        # привязан к конкретному периоду расчёта, а кеш архивных ответов
        # бессрочен (main-prompt.md §5). Безадресный refresh либо ничего не
        # значил бы, либо тратил квоту источника впустую. Их последний
        # известный статус при этом виден — и здесь, и в /api/sources/status.
        return [
            _source_status_dict(registry, orbit.SOURCE_ID_CURRENT, config_enabled=True),
            _source_status_dict(registry, swpc_source.SOURCE_ID, config_enabled=swpc_enabled),
            _source_status_dict(
                registry, noaa_3day_source.SOURCE_ID, config_enabled=noaa_3day_enabled
            ),
            _source_status_dict(
                registry, orbit_history.SOURCE_ID, config_enabled=_oem_config_enabled()
            ),
            _source_status_dict(
                registry, donki_source.SOURCE_ID, config_enabled=_donki_config_enabled()
            ),
        ]
    finally:
        conn.close()


def get_all_source_status(*, registry: SourceStatusRegistry) -> list[dict[str, Any]]:
    """Статусы всех известных источников без обращения к сети."""
    return [
        _source_status_dict(registry, orbit.SOURCE_ID_CURRENT, config_enabled=True),
        _source_status_dict(registry, swpc_source.SOURCE_ID, config_enabled=_swpc_config_enabled()),
        _source_status_dict(
            registry, noaa_3day_source.SOURCE_ID, config_enabled=_noaa_3day_config_enabled()
        ),
        # Источники исторических режимов (FN-41): видны в статусе всегда,
        # даже пока ни один исторический расчёт не выполнялся — иначе
        # «источник ни разу не отвечал» было бы неотличимо от «источника нет».
        _source_status_dict(
            registry, orbit_history.SOURCE_ID, config_enabled=_oem_config_enabled()
        ),
        _source_status_dict(
            registry, donki_source.SOURCE_ID, config_enabled=_donki_config_enabled()
        ),
    ]


__all__ = [
    "ApiError",
    "CalculationError",
    "OrbitFetchResult",
    "ensure_store_ready",
    "fetch_and_store_orbit",
    "get_all_source_status",
    "refresh_sources",
    "run_calculation",
    "sanitize_unexpected_error",
]
