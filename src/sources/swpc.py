"""Коннектор NOAA SWPC: интегральный поток протонов >=10 МэВ (main-prompt.md §11).

Реализует задачу FN-22 (S1-04): выбранный и проверенный продукт NOAA SWPC —
``integral-protons-1-day`` первичного спутника GOES, канал ``>=10 MeV`` —
ровно та величина, что задаёт шкалу S NOAA (Механизм 1 «Радиационная
обстановка»). Модуль только получает и нормализует (не интерпретирует
уровни/пороги — это домен, добавится последующей задачей).

Ключевое ограничение источника, зафиксированное в ``sources.yaml``: этот
продукт не несёт времени публикации, только время измерения (``time_tag``).
Поэтому ``published_at`` каждой записи — всегда ``None``, и, как следствие
правила хранилища (``src/store/records.py``), ``replay_eligible`` — всегда
``False``. Это ожидаемо и явно допущено приёмкой задачи: строгий replay из
прошлого по космопогоде здесь не заявляется, он реализуется другой линией
(архивные сводки NOAA SWPC/NCEI с явным Issued — вне объёма этой задачи).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

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
    is_critically_stale,
    staleness_seconds,
)
from src.store.records import (
    DuplicateKeyConflictError,
    RawOriginalStore,
    RecordInput,
    insert_record,
)

SOURCE_ID = "noaa-swpc-proton-flux"
_TARGET_ENERGY = ">=10 MeV"
_DEFAULT_SOURCES_YAML = Path(__file__).resolve().parent.parent.parent / "sources.yaml"

Outcome = Literal[
    "stored",
    "skipped_disabled",
    "skipped_frozen",
    "skipped_fresh",
    "error_timeout",
    "error_quota",
    "error_format",
    "error_http",
]


class SwpcFormatError(RuntimeError):
    """Ответ источника — не то, что ожидает парсер (main-prompt.md §5).

    Покрывает и «пустой 200» (main-prompt.md §5: код 200 с пустым или
    неожиданным телом — ошибка источника, а не набор нулей), и структурно
    неожиданный JSON, и полное отсутствие ожидаемого канала энергии (тихая
    смена формата источника не должна читаться как «нет данных»,
    .ai/backend-prompt.md §3).
    """


@dataclass(frozen=True)
class SwpcSourceConfig:
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
class SwpcSample:
    """Один нормализованный отсчёт потока после парсинга и фильтрации по каналу."""

    satellite: str
    observed_at: datetime
    value: float | None
    degraded: bool


@dataclass(frozen=True)
class FetchOutcome:
    """Итог одного вызова :func:`fetch_and_store`."""

    outcome: Outcome
    message: str | None
    stored_record_ids: tuple[str, ...]
    status: SourceStatus


def load_source_config(
    path: str | Path = _DEFAULT_SOURCES_YAML, *, source_id: str = SOURCE_ID
) -> SwpcSourceConfig:
    """Читает запись источника из ``sources.yaml`` заново при каждом вызове.

    Не кешируется: изменение ``enabled`` (или таймаутов/TTL) в файле должно
    действовать без перезапуска сервиса (.ai/backend-prompt.md §3, приёмка
    FN-22 «отключение и заморозка обновлений»).
    """
    text = Path(path).read_text(encoding="utf-8")
    doc = yaml.safe_load(text) or {}
    entries = doc.get("space_weather") or []
    for entry in entries:
        if entry.get("id") == source_id:
            network = entry.get("network") or {}
            freshness = entry.get("freshness") or {}
            return SwpcSourceConfig(
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


def _parse_time_tag(raw: str) -> datetime:
    """Разбирает ``time_tag`` NOAA SWPC (UTC; иногда без суффикса ``Z``)."""
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        # NOAA документирует time_tag этого продукта как UTC даже когда
        # суффикс смещения отсутствует; здесь это единственное место, где
        # это предположение делается (main-prompt.md §1 — naive datetime
        # запрещён дальше по пайплайну).
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_response(raw_bytes: bytes) -> list[SwpcSample]:
    """Разбирает и нормализует ответ ``integral-protons-1-day.json``.

    Возвращает только отсчёты канала ``>=10 MeV`` (main-prompt.md §11). Любое
    отклонение от ожидаемой формы — :class:`SwpcFormatError`, а не тихий
    пустой список: неотличимость «источник ничего не прислал» от «формат
    источника поменялся» — прямой путь к ложному «фон» (main-prompt.md §2).
    """
    if not raw_bytes or not raw_bytes.strip():
        raise SwpcFormatError("empty response body (HTTP 200 with no content)")

    try:
        data: Any = json.loads(raw_bytes)
    except json.JSONDecodeError as exc:
        raise SwpcFormatError(f"response is not valid JSON: {exc}") from exc

    if not isinstance(data, list):
        raise SwpcFormatError(
            f"expected a JSON array of samples, got {type(data).__name__}"
        )
    if not data:
        raise SwpcFormatError("response is an empty JSON array")

    samples: list[SwpcSample] = []
    for index, entry in enumerate(data):
        if not isinstance(entry, dict):
            raise SwpcFormatError(f"entry #{index} is not a JSON object")
        energy = entry.get("energy")
        if energy != _TARGET_ENERGY:
            continue
        try:
            satellite = entry["satellite"]
            observed_at = _parse_time_tag(str(entry["time_tag"]))
        except KeyError as exc:
            raise SwpcFormatError(f"entry #{index} is missing field {exc}") from exc
        except ValueError as exc:
            raise SwpcFormatError(f"entry #{index} has an unparseable time_tag: {exc}") from exc

        flux = entry.get("flux")
        value: float | None
        if flux is None:
            value = None
        elif isinstance(flux, (int, float)) and flux >= 0:
            value = float(flux)
        else:
            # Отрицательный/нечисловой отсчёт — задокументированный признак
            # невалидного измерения у этого продукта (sources.yaml
            # quality_notes): сохраняется как «нет данных», а не как
            # правдоподобное отрицательное число и не отбрасывается целиком.
            value = None

        degraded = bool(entry.get("yaw_flip"))
        samples.append(
            SwpcSample(
                satellite=str(satellite),
                observed_at=observed_at,
                value=value,
                degraded=degraded,
            )
        )

    if not samples:
        raise SwpcFormatError(
            f"no entries with energy == {_TARGET_ENERGY!r} found; "
            "source format may have changed"
        )
    return samples


def _source_version(sample: SwpcSample) -> str:
    """Версия, производная от содержания измерения (у продукта нет своей).

    NOAA не публикует номер версии для этого real-time продукта. Версия,
    зависящая только от значения, даёт нужное свойство само по себе: если
    NOAA когда-нибудь переиздаст тот же ``time_tag`` с другим значением
    (пересчёт/despiking), это станет новой версией — новой строкой рядом со
    старой (main-prompt.md §2 «поздние уточнения хранятся рядом с
    прежними»), а не конфликтом дедупликации; повторная выборка того же
    значения — тот же хеш, тот же дедуп-ключ, идемпотентно.
    """
    payload = f"v1:{sample.value!r}:{sample.degraded!r}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def to_record_input(
    sample: SwpcSample, *, source_url: str, fetched_at: datetime, raw_bytes: bytes
) -> RecordInput:
    """Нормализует один отсчёт в :class:`RecordInput` готовый к ``insert_record``."""
    unit = "pfu" if sample.value is not None else None
    quality: Literal["nominal", "degraded", "unknown"]
    if sample.value is None:
        quality = "unknown"
    elif sample.degraded:
        quality = "degraded"
    else:
        quality = "nominal"

    return RecordInput(
        provider_record_id=f"{sample.satellite}:{sample.observed_at.isoformat()}",
        source_id=SOURCE_ID,
        source_url=source_url,
        record_kind="observation",
        # Точечный отсчёт, не интервальный продукт: valid_from/valid_to
        # намеренно равны observed_at, а не растянуты на период до
        # следующего отсчёта — это было бы придуманным интервалом
        # (main-prompt.md §1, запись не подменяет одно время другим).
        observed_at=sample.observed_at,
        valid_from=sample.observed_at,
        valid_to=sample.observed_at,
        published_at=None,
        fetched_at=fetched_at,
        value=sample.value,
        unit=unit,
        spatial_context={
            "platform": "GOES",
            "satellite": sample.satellite,
            "orbit": "geostationary",
            "role": "remote_proxy",
        },
        source_version=_source_version(sample),
        quality=quality,
        raw_bytes=raw_bytes,
    )


def fetch_and_store(
    conn: sqlite3.Connection,
    raw_store: RawOriginalStore,
    *,
    config: SwpcSourceConfig | None = None,
    registry: SourceStatusRegistry,
    now: datetime | None = None,
    force: bool = False,
    http_client: httpx.Client | None = None,
) -> FetchOutcome:
    """Получает, парсит и сохраняет текущий отсчёт(-ы) потока протонов.

    Реализует приёмку FN-22 как единый управляемый шлюз:

    - ``config.enabled is False`` — источник отключён (документированный
      статический переключатель) — сеть не трогается вовсе;
    - заморожен в реестре — то же самое, но переключатель оперативный;
    - иначе, если не ``force`` и последний успех моложе ``ttl_seconds`` —
      периодический refresh: вызов дешёвый no-op, сеть не трогается;
    - иначе — реальное обращение к источнику; таймаут/квота/неожиданный
      формат дают явный статус источника, а не тихий пропуск.
    """
    moment = now if now is not None else datetime.now(timezone.utc)
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

    try:
        result = fetch(
            cfg.url,
            connect_timeout_seconds=cfg.connect_timeout_seconds,
            read_timeout_seconds=cfg.read_timeout_seconds,
            max_retries=cfg.max_retries,
            backoff_base_seconds=cfg.backoff_base_seconds,
            client=http_client,
        )
    except SourceQuotaLimitedError as exc:
        updated = registry.record_error(
            cfg.source_id, at=moment, message=str(exc), quota_limited=True
        )
        return FetchOutcome(
            outcome="error_quota", message=str(exc), stored_record_ids=(), status=updated
        )
    except SourceTimeoutError as exc:
        updated = registry.record_error(
            cfg.source_id, at=moment, message=str(exc), quota_limited=False
        )
        return FetchOutcome(
            outcome="error_timeout", message=str(exc), stored_record_ids=(), status=updated
        )
    except SourceHttpError as exc:
        updated = registry.record_error(
            cfg.source_id, at=moment, message=str(exc), quota_limited=False
        )
        return FetchOutcome(
            outcome="error_http", message=str(exc), stored_record_ids=(), status=updated
        )

    try:
        samples = parse_response(result.body)
    except SwpcFormatError as exc:
        updated = registry.record_error(
            cfg.source_id, at=moment, message=str(exc), quota_limited=False
        )
        return FetchOutcome(
            outcome="error_format", message=str(exc), stored_record_ids=(), status=updated
        )

    stored_ids: list[str] = []
    conflicts: list[str] = []
    for sample in samples:
        record_input = to_record_input(
            sample, source_url=cfg.url, fetched_at=moment, raw_bytes=result.body
        )
        try:
            record_id = insert_record(conn, raw_store, record_input)
        except DuplicateKeyConflictError as exc:
            # Не должно происходить при версии, производной от содержания
            # (см. _source_version), но не должно и обрушивать весь пакет
            # из-за одной аномальной записи — остальные отсчёты в ответе
            # сохраняются.
            conflicts.append(str(exc))
            continue
        stored_ids.append(record_id)

    updated = registry.record_success(cfg.source_id, at=moment)
    message = f"stored {len(stored_ids)} sample(s)"
    if conflicts:
        message += f"; {len(conflicts)} conflict(s): {conflicts[0]}"
    return FetchOutcome(
        outcome="stored",
        message=message,
        stored_record_ids=tuple(stored_ids),
        status=updated,
    )


__all__ = [
    "SOURCE_ID",
    "FetchOutcome",
    "SwpcFormatError",
    "SwpcSample",
    "SwpcSourceConfig",
    "fetch_and_store",
    "is_critically_stale",
    "load_source_config",
    "parse_response",
    "to_record_input",
]
