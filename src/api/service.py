"""Сервисный слой S1-07: оркестрация запроса, источников, хранилища и результата.

Роутеры (``src/api/routes.py``) остаются тонкими (.ai/main-prompt.md §8):
разбор HTTP, вызов функций этого модуля, преобразование исключений в
HTTP-ответ. Вся логика — здесь.

Ключевое решение объёма этой задачи (S1-07 — API и хранение, не
интерпретация механизмов): ``src/domain/spaceweather`` и ``src/domain/mmod``
ещё не реализованы (пустые модули-заглушки), поэтому оба обязательных
механизма воздействия несут ``status = "not_implemented"`` в каждом окне —
контракт (``contracts/result.schema.json``) прямо предусматривает это
значение именно для такого случая: «механизм ещё не реализован в этой версии
сервиса (не имитируется готовым)». Эта задача честно поставляет реальные
входные данные (орбитальные элементы МКС, при доступности — поток протонов)
и корректную, проверяемую форму результата; оценка риска появится вместе с
задачами зоны 2/3, реализующими ``domain/spaceweather`` и ``domain/mmod``.

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
from typing import Any, Literal

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
from src.sources import orbit
from src.sources import swpc as swpc_source
from src.sources.status import SourceStatusRegistry, effective_status
from src.store import RawOriginalStore, insert_record, store_result
from src.store.schema import connect as connect_store

OrbitOutcome = Literal["stored", "error_source", "error_quota", "error_corrupted"]


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


def _log(event: str, **fields: Any) -> None:
    """Одна структурированная запись в stdout на этап (.ai/backend-prompt.md §5).

    Не заменяет полноценный логгер — минимальная реализация, достаточная,
    чтобы по ``result_id``/``task_id`` можно было восстановить историю
    конкретного расчёта, как того требует приёмка.
    """
    line = {"event": event, **fields}
    print(json.dumps(line, ensure_ascii=False, default=str), file=sys.stderr)


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
    """
    try:
        parsed, raw_bytes, source_url = orbit.fetch_elements_for_request(
            "current", norad_id=norad_id
        )
    except orbit.OrbitSourceQuotaError as exc:
        registry.record_error(
            orbit.SOURCE_ID_CURRENT, at=now, message=str(exc), quota_limited=True
        )
        return OrbitFetchResult("error_quota", str(exc), None, None, None)
    except orbit.OrbitSourceError as exc:
        registry.record_error(
            orbit.SOURCE_ID_CURRENT, at=now, message=str(exc), quota_limited=False
        )
        return OrbitFetchResult("error_source", str(exc), None, None, None)
    except orbit.CorruptedElementsError as exc:
        registry.record_error(
            orbit.SOURCE_ID_CURRENT, at=now, message=str(exc), quota_limited=False
        )
        return OrbitFetchResult("error_corrupted", str(exc), None, None, None)

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
    registry.record_success(orbit.SOURCE_ID_CURRENT, at=now)
    return OrbitFetchResult("stored", None, record_id, parsed, record.source_version)


def _not_implemented_mechanism(mechanism: Literal["space_weather", "mmod"]) -> dict[str, Any]:
    label = (
        "космической погоды (main-prompt.md §11, Механизм 1)"
        if mechanism == "space_weather"
        else "MMOD (main-prompt.md §11, Механизм 2)"
    )
    return {
        "mechanism": mechanism,
        "status": "not_implemented",
        "max_level": None,
        "exceedance_hours_by_level": None,
        "coverage_fraction": 0.0,
        "critical_gap": True,
        "notes": [
            f"Интерпретация механизма {label} не реализована в этой версии "
            "сервиса — оценка не имитируется готовым значением."
        ],
        "record_ids": [],
    }


def _source_status_dict(
    registry: SourceStatusRegistry, source_id: str, *, config_enabled: bool
) -> dict[str, Any]:
    status = effective_status(registry.get(source_id), config_enabled=config_enabled)
    return {
        "source_id": status.source_id,
        "last_success_at": (
            iso_utc(status.last_success_at) if status.last_success_at is not None else None
        ),
        "last_error_at": (
            iso_utc(status.last_error_at) if status.last_error_at is not None else None
        ),
        "last_error_message": status.last_error_message,
        "frozen": status.frozen,
        "quota_limited": status.quota_limited,
    }


def _swpc_config_enabled() -> bool:
    try:
        return swpc_source.load_source_config().enabled
    except Exception:  # noqa: BLE001 — статус источника не должен падать из-за
        # временной проблемы с чтением sources.yaml; по умолчанию считаем
        # источник включённым (сам fetch_and_store всё равно перечитает файл).
        return True


def _window(window_id: str, start_at: datetime, duration_hours: float) -> dict[str, Any]:
    end_at = start_at + timedelta(hours=duration_hours)
    return {
        "window_id": window_id,
        "start_at": iso_utc(start_at),
        "end_at": iso_utc(end_at),
        "duration_hours": duration_hours,
        "mechanisms": [
            _not_implemented_mechanism("space_weather"),
            _not_implemented_mechanism("mmod"),
        ],
        "lighting": {"requested": False, "status": "not_requested", "note": None},
        "excluded_from_comparison": True,
        "exclusion_reason": (
            "Критический пробел по обоим обязательным механизмам (space_weather и "
            "mmod ещё не реализованы в этой версии сервиса) — окно выводится из "
            "сравнения, а не проигрывает по баллам (.ai/main-prompt.md §11, "
            "правило предпочтения окон, п.1)."
        ),
    }


def _build_current_result(
    request: CalculationRequest,
    *,
    conn: sqlite3.Connection,
    raw_store: RawOriginalStore,
    registry: SourceStatusRegistry,
    settings: Settings,
    now: datetime,
) -> dict[str, Any]:
    orbit_fetch = fetch_and_store_orbit(conn, raw_store, registry, now=now)
    _log("orbit_fetch", outcome=orbit_fetch.outcome, source_id=orbit.SOURCE_ID_CURRENT)
    orbit_ready = (
        orbit_fetch.outcome == "stored"
        and orbit_fetch.parsed is not None
        and orbit_fetch.record_id is not None
    )
    if not orbit_ready:
        raise CalculationError(
            f"orbit_{orbit_fetch.outcome}",
            f"orbital elements are required for any result (main-prompt.md §11 "
            f"«Траектория участвует хотя бы в одном расчёте»); fetch failed: "
            f"{orbit_fetch.message}",
        )
    assert orbit_fetch.parsed is not None  # narrowed by orbit_ready above, for mypy
    assert orbit_fetch.record_id is not None
    parsed = orbit_fetch.parsed

    elements = load_elements(parsed.line1, parsed.line2, norad_id=parsed.norad_id)
    age_hours = elements_age_hours(parsed.epoch, now)
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
    )

    swpc_warning: dict[str, Any] | None = None
    try:
        swpc_cfg = swpc_source.load_source_config()
        swpc_outcome = swpc_source.fetch_and_store(
            conn, raw_store, config=swpc_cfg, registry=registry, now=now
        )
    except Exception as exc:  # noqa: BLE001 — отказ одного источника не должен
        # обрушивать расчёт целиком (.ai/main-prompt.md §5, приёмка FN-26
        # «при отказе одного источника остальные доступны»); неожиданная
        # ошибка коннектора фиксируется как статус источника, а не падение API.
        registry.record_error(
            swpc_source.SOURCE_ID, at=now, message=str(exc), quota_limited=False
        )
        swpc_outcome = None

    _log(
        "swpc_fetch",
        outcome=(swpc_outcome.outcome if swpc_outcome is not None else "error_unexpected"),
        source_id=swpc_source.SOURCE_ID,
    )
    if swpc_outcome is not None and swpc_outcome.outcome.startswith("error_"):
        swpc_warning = {
            "code": f"space-weather-{swpc_outcome.outcome}",
            "severity": "advisory",
            "mechanism": "space_weather",
            "message": (
                f"Получение потока протонов не удалось ({swpc_outcome.outcome}): "
                f"{swpc_outcome.message}. Интерпретация механизма пока не "
                "реализована в любом случае, но провенанс отказа источника "
                "сохранён отдельно от оценки."
            ),
            "record_ids": [],
            "fetch_attempt_id": f"fa-{swpc_source.SOURCE_ID}-{iso_utc(now)}",
            "window_id": None,
        }

    windows = [
        _window("win-a", request.start_at, request.duration_hours),
        _window("win-b", request.search_end_at, request.duration_hours),
    ]

    source_status = [
        _source_status_dict(registry, orbit.SOURCE_ID_CURRENT, config_enabled=True),
        _source_status_dict(
            registry, swpc_source.SOURCE_ID, config_enabled=_swpc_config_enabled()
        ),
    ]

    result_id = f"res-{uuid.uuid4()}"
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
            }
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
        "limitations": [
            "Интерпретация обоих обязательных механизмов воздействия (космическая "
            "погода, MMOD) не реализована в этой версии сервиса — этот эндпоинт "
            "получает и сохраняет реальные исходные данные (орбитальные элементы "
            "МКС и, при доступности источника, поток протонов), но не вычисляет "
            "уровень риска.",
            "Траектория станции рассчитана по SGP4 на предоставленных элементах "
            "(см. orbit.elements_age_hours/is_reconstructed).",
        ],
        "warnings": [swpc_warning] if swpc_warning is not None else [],
        "recommendation": {
            "status": "all_windows_excluded",
            "window_id": None,
            "explanation": (
                "Оба сравниваемых окна исключены из сравнения: ни один из двух "
                "обязательных механизмов воздействия ещё не оценивается в этой "
                "версии сервиса (S1-07 поставляет API и хранение; интерпретация "
                "— последующие задачи зон 2/3). Рекомендация появится вместе с "
                "реализацией механизмов."
            ),
        },
        "source_status": source_status,
    }
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
        )
        store_result(conn, result)
        _log("result_stored", result_id=result["result_id"], mode=result["mode"])
        return str(result["result_id"])
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
        except Exception as exc:  # noqa: BLE001 — см. _build_current_result
            registry.record_error(
                swpc_source.SOURCE_ID, at=moment, message=str(exc), quota_limited=False
            )
            swpc_enabled = True

        return [
            _source_status_dict(registry, orbit.SOURCE_ID_CURRENT, config_enabled=True),
            _source_status_dict(registry, swpc_source.SOURCE_ID, config_enabled=swpc_enabled),
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
]
