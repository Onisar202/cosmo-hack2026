"""Сервисный слой S1-07: оркестрация запроса, источников, хранилища и результата.

Роутеры (``src/api/routes.py``) остаются тонкими (.ai/main-prompt.md §8):
разбор HTTP, вызов функций этого модуля, преобразование исключений в
HTTP-ответ. Вся логика — здесь.

Исходное решение объёма задачи S1-07 (API и хранение, не интерпретация
механизмов) оставляло оба обязательных механизма воздействия
``status = "not_implemented"`` в каждом окне. FN-38 (S2-08) подключает
``src/domain/spaceweather/observed_classifier.py`` — классификацию
НАБЛЮДАЕМОГО потока протонов GOES по шкале S NOAA (не путать с уже
подключённым FN-31 внешним суточным прогнозом-вероятностью, той же линией
``space_weather``, но другой величиной): ``mechanisms[*space_weather]``
теперь несёт ``status = "ok"`` (реальные уровень/exceedance), когда окно
полностью покрыто пригодными отсчётами в пределах ожидаемого шага между
измерениями, и явные ``missing_data``/``stale_data``/``source_error``/
``beyond_horizon`` иначе (main-prompt.md §2 — отказ/пробел не подменяется
благоприятной оценкой; см. ``_space_weather_status``). ``domain/mmod`` (FN-39)
ещё не реализован — ``mmod`` остаётся ``not_implemented`` в каждом окне до
отдельного закрытия его научного гейта.

По той же причине ``mode in {historical_analysis, historical_forecast}``
сейчас не может дать результат: строгий historical режим требует
исторических орбитальных элементов (Space-Track ``GP_HISTORY``), а этот
коннектор ещё не реализован (``src/sources/orbit.py:
HistoricalElementsUnsupportedError`` — современные элементы CelesTrak не
подставляются вместо исторических ни при каких обстоятельствах,
.ai/main-prompt.md §11). Задача явно требует понятный ``not_implemented``
здесь, а не фиктивный успех — реализовано как отказ задачи с явным кодом
ошибки, а не как результат с придуманной орбитой.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from src.api.schemas import ALGORITHM_VERSION, CalculationRequest, iso_utc
from src.config import Settings
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
from src.domain.spaceweather.observed_classifier import (
    ConflictingObservationsError,
    ObservedFluxAssessment,
    assess_observed_flux,
    observed_proton_samples_from_records,
)
from src.domain.windows import WindowCandidate, excluded_windows, recommend
from src.sources import noaa_3day_forecast as noaa_3day_source
from src.sources import orbit
from src.sources import swpc as swpc_source
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
            conn, registry, now=now, norad_id=norad_id, outcome="error_quota",
            exc=exc, quota_limited=True,
        )
    except orbit.OrbitSourceError as exc:
        return _orbit_fetch_failed(
            conn, registry, now=now, norad_id=norad_id, outcome="error_source",
            exc=exc, quota_limited=False,
        )
    except orbit.CorruptedElementsError as exc:
        return _orbit_fetch_failed(
            conn, registry, now=now, norad_id=norad_id, outcome="error_corrupted",
            exc=exc, quota_limited=False,
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


def _not_implemented_mechanism(mechanism: Literal["mmod"]) -> dict[str, Any]:
    """Заглушка ``mechanismAssessment`` для механизма без готовой пороговой
    интерпретации (main-prompt.md §11). С FN-38 (S2-08) применяется только к
    ``mmod`` — научный гейт нормировки MMOD (FN-32/FN-39) ещё не закрыт, этот
    механизм по-прежнему честно не имитируется готовым значением.
    ``space_weather`` с FN-38 строится отдельно, через
    ``_space_weather_mechanism`` (наблюдение GOES + внешний прогноз NOAA
    3-Day), и этой заглушкой больше не пользуется.
    """
    return {
        "mechanism": mechanism,
        "status": "not_implemented",
        "max_level": None,
        "exceedance_hours_by_level": None,
        "coverage_fraction": 0.0,
        "critical_gap": True,
        "notes": [
            "Интерпретация механизма MMOD (main-prompt.md §11, Механизм 2) не "
            "реализована в этой версии сервиса — оценка не имитируется готовым "
            "значением."
        ],
        "record_ids": [],
    }


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


def _window(
    window_id: str,
    start_at: datetime,
    duration_hours: float,
    *,
    space_weather_mechanism: dict[str, Any],
) -> dict[str, Any]:
    """Строит окно без ``excluded_from_comparison``/``exclusion_reason`` —
    эти два поля решает правило доминирования v2 (FN-34,
    ``src.domain.windows``) над ГОТОВЫМ набором окон, см.
    :func:`_apply_window_dominance`, а не эта функция для одного окна в
    изоляции. ``space_weather_mechanism`` строится заранее вызывающей
    стороной (:func:`_space_weather_mechanism`) — эта функция лишь собирает
    окно целиком, не решает уровень/статус механизма."""
    end_at = start_at + timedelta(hours=duration_hours)
    return {
        "window_id": window_id,
        "start_at": iso_utc(start_at),
        "end_at": iso_utc(end_at),
        "duration_hours": duration_hours,
        "mechanisms": [space_weather_mechanism, _not_implemented_mechanism("mmod")],
        "lighting": {"requested": False, "status": "not_requested", "note": None},
    }


def _apply_window_dominance(windows: list[dict[str, Any]]) -> dict[str, Any]:
    """Заполняет ``excluded_from_comparison``/``exclusion_reason`` каждого
    окна и строит ``result.recommendation`` через правило доминирования v2
    (FN-34/S2-04, ``src.domain.windows.dominance``) — единственное место,
    решающее это для ``mode = current``. Мутирует переданные словари окон на
    месте (тот же паттерн, что и остальная сборка результата в этом модуле)
    и возвращает ``recommendation`` для `result`.

    До FN-38 оба механизма (space_weather/mmod) всегда несли
    ``status = "not_implemented"``/``critical_gap = True``, поэтому это
    неизбежно давало ``all_windows_excluded`` для любого запроса. С FN-38
    ``space_weather`` может быть ``ok`` (см. ``_space_weather_status``), но
    ``mmod`` остаётся ``not_implemented``/``critical_gap = True`` до FN-39 —
    правило предпочтения окон (п.1, main-prompt.md §11) исключает окно при
    критическом пробеле по ЛЮБОМУ механизму, поэтому сегодня результат этой
    функции по-прежнему ``all_windows_excluded`` в каждом запросе. Это не
    захардкожено здесь: как только FN-39 подключит ``mmod``, поведение
    изменится само собой через то же общее правило, без правок этой функции.
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

    def _window_forecast_assessment(start_at: datetime, duration_hours: float) -> tuple[
        list[str], list[str]
    ]:
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
            "win-a", request.start_at, request.duration_hours,
            space_weather_mechanism=win_a_space_weather,
        ),
        _window(
            "win-b", request.search_end_at, request.duration_hours,
            space_weather_mechanism=win_b_space_weather,
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
        "Интерпретация Механизма 2 (MMOD) не реализована в этой версии сервиса "
        "(научный гейт нормировки к спорадическому фону не закрыт, FN-32/FN-39) "
        "— mechanisms[*mmod].status остаётся not_implemented в каждом окне, "
        "оценка не имитируется готовым значением.",
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
        if request.mode != "current":
            raise CalculationError(
                "historical_mode_not_implemented",
                f"mode={request.mode!r} is not implemented yet: the historical "
                "orbital elements connector (Space-Track GP_HISTORY) does not "
                "exist yet (sources.yaml: space-track-gp-history), and current "
                "CelesTrak elements are never substituted for historical ones "
                "(.ai/main-prompt.md §11 «Траектория»). This is a deliberate "
                "not_implemented failure, not a fabricated result "
                "(.ai/main-prompt.md §2).",
            )
        result = _build_current_result(
            request,
            conn=conn,
            raw_store=raw_store,
            registry=registry,
            settings=settings,
            now=moment,
            task_id=task_id,
        )
        store_result(conn, result)
        _log(
            "result_stored", task_id=task_id, result_id=result["result_id"], mode=result["mode"]
        )
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

        return [
            _source_status_dict(registry, orbit.SOURCE_ID_CURRENT, config_enabled=True),
            _source_status_dict(registry, swpc_source.SOURCE_ID, config_enabled=swpc_enabled),
            _source_status_dict(
                registry, noaa_3day_source.SOURCE_ID, config_enabled=noaa_3day_enabled
            ),
        ]
    finally:
        conn.close()


def get_all_source_status(*, registry: SourceStatusRegistry) -> list[dict[str, Any]]:
    """Статусы всех известных источников без обращения к сети."""
    return [
        _source_status_dict(registry, orbit.SOURCE_ID_CURRENT, config_enabled=True),
        _source_status_dict(
            registry, swpc_source.SOURCE_ID, config_enabled=_swpc_config_enabled()
        ),
        _source_status_dict(
            registry, noaa_3day_source.SOURCE_ID, config_enabled=_noaa_3day_config_enabled()
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
