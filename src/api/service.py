"""Сервисный слой S1-07: оркестрация запроса, источников, хранилища и результата.

Роутеры (``src/api/routes.py``) остаются тонкими (.ai/main-prompt.md §8):
разбор HTTP, вызов функций этого модуля, преобразование исключений в
HTTP-ответ. Вся логика — здесь.

Исходное решение объёма задачи S1-07 (API и хранение, не интерпретация
механизмов) оставляло оба обязательных механизма ``status =
"not_implemented"`` в каждом окне. С тех пор Механизм 2 (MMOD) реализован
(FN-32 — геометрия, FN-39 — ``ratio_to_background``/пороги/подключение,
см. :func:`_mmod_mechanism_assessment`): ``mechanisms[*]`` с
``mechanism="mmod"`` несёт настоящую оценку (``status`` — ``ok``,
``missing_data`` или ``source_error``, не всегда ``not_implemented``).
Механизм 1 (space_weather) остаётся ``not_implemented`` — комбинирование
наблюдения GOES pfu и внешнего суточного прогноза NOAA 3-Day в один уровень
ещё не реализовано (контракт, ``contracts/result.schema.json``, прямо
предусматривает ``not_implemented`` именно для этого случая).

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
from src.domain.mmod import background as mmod_background
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
from src.domain.windows import WindowCandidate, excluded_windows, recommend
from src.sources import mmod as mmod_source
from src.sources import noaa_3day_forecast as noaa_3day_source
from src.sources import orbit
from src.sources import swpc as swpc_source
from src.sources.status import SourceStatus, SourceStatusRegistry, effective_status
from src.store import RawOriginalStore, get_latest_record, insert_record, select_as_of, store_result
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


def _not_implemented_mechanism(
    mechanism: Literal["space_weather", "mmod"],
    *,
    extra_notes: list[str] | None = None,
    extra_record_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Заглушка ``mechanismAssessment`` для механизма без готовой пороговой
    интерпретации (main-prompt.md §11: комбинирование наблюдения и внешнего
    прогноза в один уровень/exceedance ещё не реализовано).

    ``extra_notes``/``extra_record_ids`` (FN-31) — реальные, уже полученные
    входные данные, которые ЭТА версия сервиса умеет показать без готовой
    пороговой логики: суточная вероятность S1+ NOAA 3-Day Forecast для
    конкретного окна (``assess_external_forecast``). ``status`` остаётся
    ``not_implemented``, а ``critical_gap``/``coverage_fraction`` — ``True``/
    ``0.0`` как и раньше: они описывают полноту данных ДЛЯ ГОТОВОЙ ОЦЕНКИ
    механизма в целом (наблюдение + внешний прогноз главной формулой §11), а
    не полноту одной лишь линии внешнего прогноза — наблюдение GOES pfu всё
    ещё не входит ни в один расчёт уровня (main-prompt.md §2: не подменять
    частичный прогресс благоприятной/готовой на вид оценкой). ``notes`` и
    ``record_ids`` контрактом не ограничены статусом ``not_implemented``
    (contracts/result.schema.json → mechanismAssessment), поэтому реальная
    информация, полученная и сохранённая этим сервисом, доходит до клиента
    уже сейчас — О4 «от предупреждения — к значению и первоисточнику».
    """
    label = (
        "космической погоды (main-prompt.md §11, Механизм 1)"
        if mechanism == "space_weather"
        else "MMOD (main-prompt.md §11, Механизм 2)"
    )
    notes = [
        f"Интерпретация механизма {label} не реализована в этой версии "
        "сервиса — оценка не имитируется готовым значением."
    ]
    if extra_notes:
        notes.extend(extra_notes)
    return {
        "mechanism": mechanism,
        "status": "not_implemented",
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
    factor уже worst-case для полностью открытой, обращённой к радианту
    площадки (не привязан к конкретной траектории станции), а повторное
    умножение на геометрию исказило бы уже нормированное число. Совместное
    траекторное объединение с ориентацией конкретной поверхности — вне
    объёма этой задачи, отдельное развитие ``src/domain/mmod/geometry.py``.

    Реальный `now` этого окружения (2026+) лежит за пределами годового
    документа NASA (только 2024) — в `mode=current` это ЧЕСТНО даёт
    ``status="missing_data"``/``critical_gap=True`` (main-prompt.md §2: за
    пределами данных — не спокойная обстановка), не имитирует уровень.
    Тесты (``tests/api/``) проверяют ok/conflict/equal/critical_gap
    сценарии через инъекцию ``now`` внутри обязательного периода
    01.05–30.06.2024, тем же способом, что и остальные тесты этого модуля.
    """
    end_at = start_at + timedelta(hours=duration_hours)
    try:
        _record_ids, doc_start, doc_end = mmod_source.ensure_mmod_records_for_window(
            conn, raw_store, window_start=start_at, window_end=end_at, fetched_at=now
        )
        mmod_records = select_as_of(
            conn, now, source_id=mmod_source.SOURCE_ID, record_kind="forecast"
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
    mmod_mechanism: dict[str, Any],
    space_weather_notes: list[str] | None = None,
    space_weather_record_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Строит окно без ``excluded_from_comparison``/``exclusion_reason`` —
    эти два поля решает правило доминирования v2 (FN-34,
    ``src.domain.windows``) над ГОТОВЫМ набором окон, см.
    :func:`_apply_window_dominance`, а не эта функция для одного окна в
    изоляции.

    ``mmod_mechanism`` — уже готовый ``mechanismAssessment`` (FN-39,
    :func:`_mmod_mechanism_assessment`), не строится здесь: в отличие от
    space_weather (всё ещё ``not_implemented`` — Механизм 1 ждёт
    интерпретации наблюдения GOES, см. :func:`_not_implemented_mechanism`),
    MMOD теперь настоящая, не заглушка."""
    end_at = start_at + timedelta(hours=duration_hours)
    return {
        "window_id": window_id,
        "start_at": iso_utc(start_at),
        "end_at": iso_utc(end_at),
        "duration_hours": duration_hours,
        "mechanisms": [
            _not_implemented_mechanism(
                "space_weather",
                extra_notes=space_weather_notes,
                extra_record_ids=space_weather_record_ids,
            ),
            mmod_mechanism,
        ],
        "lighting": {"requested": False, "status": "not_requested", "note": None},
    }


def _apply_window_dominance(windows: list[dict[str, Any]]) -> dict[str, Any]:
    """Заполняет ``excluded_from_comparison``/``exclusion_reason`` каждого
    окна и строит ``result.recommendation`` через правило доминирования v2
    (FN-34/S2-04, ``src.domain.windows.dominance``) — единственное место,
    решающее это для ``mode = current``. Мутирует переданные словари окон на
    месте (тот же паттерн, что и остальная сборка результата в этом модуле)
    и возвращает ``recommendation`` для `result`.

    Реальные механизмы (space_weather/mmod) в этой версии сервиса всегда
    ``status = "not_implemented"``/``critical_gap = True`` (см.
    ``_not_implemented_mechanism``), поэтому сегодня это неизбежно даёт
    ``all_windows_excluded`` — то же наблюдаемое поведение, что и раньше
    захардкоженная константа, но теперь через общее правило, готовое к
    подключению настоящих оценок механизмов зон 2/3 без изменений здесь.
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
    if swpc_outcome is None or swpc_outcome.outcome.startswith("error_"):
        failure_detail = swpc_outcome.message if swpc_outcome is not None else swpc_unexpected_error
        message = (
            f"Получение потока протонов не удалось: {failure_detail}. "
            "Интерпретация механизма пока не реализована в любом случае, но "
            "провенанс отказа источника сохранён отдельно от оценки."
        )
        outcome_code = swpc_outcome.outcome if swpc_outcome is not None else "error_unexpected"
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

    win_a_notes, win_a_record_ids = _window_forecast_assessment(
        request.start_at, request.duration_hours
    )
    win_b_notes, win_b_record_ids = _window_forecast_assessment(
        request.search_end_at, request.duration_hours
    )

    win_a_mmod = _mmod_mechanism_assessment(
        conn,
        raw_store,
        start_at=request.start_at,
        duration_hours=request.duration_hours,
        now=now,
        log_ctx=log_ctx,
    )
    win_b_mmod = _mmod_mechanism_assessment(
        conn,
        raw_store,
        start_at=request.search_end_at,
        duration_hours=request.duration_hours,
        now=now,
        log_ctx=log_ctx,
    )

    windows = [
        _window(
            "win-a",
            request.start_at,
            request.duration_hours,
            mmod_mechanism=win_a_mmod,
            space_weather_notes=win_a_notes,
            space_weather_record_ids=win_a_record_ids,
        ),
        _window(
            "win-b",
            request.search_end_at,
            request.duration_hours,
            mmod_mechanism=win_b_mmod,
            space_weather_notes=win_b_notes,
            space_weather_record_ids=win_b_record_ids,
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
        for record_id in sorted(set(win_a_record_ids) | set(win_b_record_ids))
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
        "Интерпретация Механизма 1 (космическая погода) не реализована в "
        "этой версии сервиса — этот эндпоинт получает и сохраняет реальные "
        "исходные данные (орбитальные элементы МКС, при доступности "
        "источника — поток протонов, суточную вероятность S1+ NOAA 3-Day "
        "Forecast), но не вычисляет уровень риска для space_weather.",
        "Суточная вероятность S1+ NOAA 3-Day Forecast (FN-31) показана в "
        "notes/record_ids каждого окна как есть, без деления по часам, "
        "умножения на длительность окна или суммирования через полночь — но "
        "не создаёт оценку уровня механизма: комбинирование с наблюдением "
        "GOES pfu (пороги S1/S2/S3, main-prompt.md §11) ещё не реализовано, "
        "поэтому mechanisms[*].status для space_weather остаётся "
        "not_implemented.",
        "Механизм 2 (MMOD, FN-39) вычислен из NASA MEO 'The 2024 meteor "
        "shower activity forecast for low Earth orbit' — годовой документ, "
        "покрывающий только 2024-01-01T00:00Z..2025-01-01T06:00Z; запрос вне "
        "этого диапазона честно даёт critical_gap/missing_data, а не "
        "спокойную обстановку. Геометрия станции (экранирование Землёй, "
        "относительная скорость встречи, src/domain/mmod/geometry.py, "
        "FN-32) в это число НЕ подмешана — NASA-показатель уже worst-case "
        "для полностью открытой площадки (sources.yaml#nasa-meo-leo-forecast-2024).",
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
        _source_status_dict(registry, swpc_source.SOURCE_ID, config_enabled=_swpc_config_enabled()),
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
