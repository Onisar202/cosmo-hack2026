"""Публичный интерфейс неизменяемого хранилища записей источников.

Ядро — ``insert_record`` и хранилище оригиналов ``RawOriginalStore``. Модуль
намеренно не предоставляет функций обновления или удаления: поздняя версия
того же продукта поставщика — это новая строка рядом со старой, а не правка
существующей (.ai/main-prompt.md §2, .ai/backend-prompt.md §1).

Дедупликация — по паре ``(source_id, provider_record_id, source_version)``:
повторная вставка того же сочетания не создаёт новую строку и не считается
вторым воздействием, а возвращает ``record_id`` уже сохранённой записи.

``replay_eligible`` вычисляется здесь, при записи, а не при чтении: запись
без известного времени публикации навсегда непригодна для строгого прогноза
из прошлого (.ai/main-prompt.md §1) — восстановить флаг задним числом по
дате измерения было бы подделкой replay.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

RecordKind = Literal["observation", "forecast", "warning", "orbital_elements"]
Quality = Literal["nominal", "degraded", "reconstructed", "unknown"]


class ChecksumMismatchError(RuntimeError):
    """Прочитанный с диска оригинал не совпадает с сохранённой контрольной суммой."""


class DuplicateKeyConflictError(ValueError):
    """Тот же дедуп-ключ (``source_id``, ``provider_record_id``, ``source_version``)
    уже занят записью с другим содержимым — либо оригинал (checksum), либо
    любое из значимых нормализованных полей (``value``, ``unit``,
    ``published_at``, ``quality`` и т.п.) отличается от уже сохранённой
    записи.

    Это не повторная передача того же сообщения (тогда и checksum, и все
    нормализованные поля совпали бы, и вставка была бы идемпотентна), а
    конфликт: поставщик не должен переиспользовать версию продукта для
    другого содержимого, и ошибка нормализации выше по пайплайну не должна
    молча сохраниться как «уже было». Тихая подмена здесь была бы хуже
    отказа — заявленная версия перестала бы
    однозначно определять содержимое (round 1 ревью)."""


class RawOriginalStore:
    """Контент-адресуемое хранилище оригиналов ответов источников на диске.

    Путь файла определяется контрольной суммой содержимого (SHA-256), поэтому
    повторная запись тех же байт — не операция, а нет-оп: файл уже на месте.
    """

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    def put(self, raw_bytes: bytes) -> tuple[str, str]:
        """Сохраняет оригинал, возвращает ``(raw_ref, checksum)``.

        ``raw_ref`` — путь относительно корня хранилища оригиналов, годный
        для последующего чтения через :meth:`get`.
        """
        checksum = hashlib.sha256(raw_bytes).hexdigest()
        path = self._path_for(checksum)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(raw_bytes)
        return str(path.relative_to(self._root)), checksum

    def get(self, raw_ref: str, expected_checksum: str) -> bytes:
        """Читает оригинал и проверяет его контрольную сумму.

        Несовпадение — повреждение хранилища, а не штатный случай: файл не
        должен был измениться после записи (.ai/backend-prompt.md §1).
        """
        data = (self._root / raw_ref).read_bytes()
        actual = hashlib.sha256(data).hexdigest()
        if actual != expected_checksum:
            raise ChecksumMismatchError(
                f"checksum mismatch for {raw_ref}: "
                f"expected {expected_checksum}, got {actual}"
            )
        return data

    def _path_for(self, checksum: str) -> Path:
        return self._root / checksum[:2] / f"{checksum}.bin"


@dataclass(frozen=True)
class RecordInput:
    """Поля новой записи источника, ещё не имеющей ``record_id``.

    Форма после сохранения соответствует ``contracts/record.schema.json``;
    здесь не хватает только полей, которые вычисляет хранилище: ``record_id``,
    ``raw_ref``, ``checksum``, ``replay_eligible``.
    """

    provider_record_id: str
    source_id: str
    source_url: str
    record_kind: RecordKind
    observed_at: datetime
    valid_from: datetime
    valid_to: datetime
    published_at: datetime | None
    fetched_at: datetime
    value: Any
    unit: str | None
    spatial_context: dict[str, Any]
    source_version: str
    quality: Quality
    raw_bytes: bytes
    orbital_elements_meta: dict[str, Any] | None = None


def _require_aware(dt: datetime, field_name: str) -> None:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(
            f"{field_name} must be a timezone-aware UTC datetime (.ai/main-prompt.md §1)"
        )


def _iso_utc(dt: datetime) -> str:
    """Нормализует к UTC и сериализует в фиксированный по ширине формат.

    Нормализация обязательна: запись со смещением, например
    ``2024-05-10T00:30:00-05:00`` (= ``05:30Z``), при сравнении как есть
    строкой с отсечением ``as_of = 2024-05-10T03:00:00Z`` прошла бы фильтр
    ``published_at <= as_of`` лексикографически, хотя фактический момент
    публикации позже отсечения — утечка будущих данных (round 1 ревью).
    Микросекунды форматируются фиксированной шириной (всегда 6 знаков), иначе
    запись без дробной части и запись с ней сортировались бы не так, как
    сравнивались бы как моменты времени.
    """
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _require_non_empty(value: str, field_name: str) -> None:
    if not value or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string (contracts/record.schema.json)")


def insert_record(
    conn: sqlite3.Connection, raw_store: RawOriginalStore, record: RecordInput
) -> str:
    """Вставляет запись источника, идемпотентно по дедуп-ключу.

    Возвращает ``record_id`` — либо только что созданной строки, либо уже
    существующей с тем же ``(source_id, provider_record_id, source_version)``
    (дубликат не добавляет воздействия, .ai/main-prompt.md §2).
    """
    for field_name, dt in (
        ("observed_at", record.observed_at),
        ("valid_from", record.valid_from),
        ("valid_to", record.valid_to),
        ("fetched_at", record.fetched_at),
    ):
        _require_aware(dt, field_name)
    if record.published_at is not None:
        _require_aware(record.published_at, "published_at")

    # Контракт требует непустую source_version (contracts/record.schema.json,
    # minLength 1); неизвестная версия непригодна для replay точно так же,
    # как неизвестная публикация (.ai/main-prompt.md §1) — запись с пустой
    # версией отклоняется целиком, а не тихо помечается неприемлемой.
    for field_name, value in (
        ("provider_record_id", record.provider_record_id),
        ("source_id", record.source_id),
        ("source_version", record.source_version),
    ):
        _require_non_empty(value, field_name)

    new_checksum = hashlib.sha256(record.raw_bytes).hexdigest()

    # Поля содержания записи — то, что обязано совпадать у двух вставок с
    # одним и тем же дедуп-ключом и одним и тем же оригиналом. ``fetched_at``
    # сюда не входит: момент получения того же оригинала законно отличается
    # между повторными обращениями к источнику и не является содержанием
    # записи (round 2 ревью). Времена уже нормализованы к UTC здесь же, чтобы
    # сравнение ниже не повторяло округление/форматирование по-своему.
    content_fields: dict[str, Any] = {
        "source_url": record.source_url,
        "record_kind": record.record_kind,
        "observed_at": _iso_utc(record.observed_at),
        "valid_from": _iso_utc(record.valid_from),
        "valid_to": _iso_utc(record.valid_to),
        "published_at": _iso_utc(record.published_at) if record.published_at else None,
        "value": record.value,
        "unit": record.unit,
        "spatial_context": record.spatial_context,
        "quality": record.quality,
        "orbital_elements_meta": record.orbital_elements_meta,
    }

    existing = conn.execute(
        """
        SELECT record_id, checksum, payload_json FROM source_records
        WHERE source_id = ? AND provider_record_id = ? AND source_version = ?
        """,
        (record.source_id, record.provider_record_id, record.source_version),
    ).fetchone()
    if existing is not None:
        existing_record_id, existing_checksum, existing_payload_json = existing
        existing_payload = json.loads(existing_payload_json)
        existing_content = {key: existing_payload.get(key) for key in content_fields}
        # И оригинал (checksum), и все значимые нормализованные поля должны
        # совпасть — иначе тот же дедуп-ключ описывает другое содержание
        # (например ошибку нормализации выше по пайплайну), и тихо
        # подтверждать первую попавшуюся запись нельзя (round 2 ревью).
        if existing_checksum != new_checksum or existing_content != content_fields:
            raise DuplicateKeyConflictError(
                f"source_id={record.source_id!r}, "
                f"provider_record_id={record.provider_record_id!r}, "
                f"source_version={record.source_version!r} is already stored "
                "with different content: "
                f"checksum {existing_checksum!r} vs {new_checksum!r}, "
                f"fields {existing_content!r} vs {content_fields!r}"
            )
        return str(existing_record_id)

    raw_ref, checksum = raw_store.put(record.raw_bytes)
    assert checksum == new_checksum
    # published_at или source_version неизвестны => запись навсегда непригодна
    # для строгого replay (.ai/main-prompt.md §1); source_version уже
    # проверена выше как непустая, проверка здесь остаётся как явное
    # выражение обоих условий правила, а не только published_at.
    replay_eligible = record.published_at is not None and bool(record.source_version.strip())

    record_id = str(uuid.uuid4())
    payload: dict[str, Any] = {
        "record_id": record_id,
        "provider_record_id": record.provider_record_id,
        "source_id": record.source_id,
        "source_url": content_fields["source_url"],
        "record_kind": content_fields["record_kind"],
        "observed_at": content_fields["observed_at"],
        "valid_from": content_fields["valid_from"],
        "valid_to": content_fields["valid_to"],
        "published_at": content_fields["published_at"],
        "fetched_at": _iso_utc(record.fetched_at),
        "value": content_fields["value"],
        "unit": content_fields["unit"],
        "spatial_context": content_fields["spatial_context"],
        "source_version": record.source_version,
        "raw_ref": raw_ref,
        "checksum": checksum,
        "quality": content_fields["quality"],
        "replay_eligible": replay_eligible,
    }
    if content_fields["orbital_elements_meta"] is not None:
        payload["orbital_elements_meta"] = content_fields["orbital_elements_meta"]

    conn.execute(
        """
        INSERT INTO source_records (
            record_id, source_id, provider_record_id, source_version,
            record_kind, published_at, observed_at, fetched_at,
            replay_eligible, checksum, raw_ref, payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record_id,
            record.source_id,
            record.provider_record_id,
            record.source_version,
            record.record_kind,
            payload["published_at"],
            payload["observed_at"],
            payload["fetched_at"],
            int(replay_eligible),
            checksum,
            raw_ref,
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
        ),
    )
    conn.commit()
    return record_id


def get_record(conn: sqlite3.Connection, record_id: str) -> dict[str, Any] | None:
    """Возвращает запись (в форме ``contracts/record.schema.json``) или ``None``."""
    row = conn.execute(
        "SELECT payload_json FROM source_records WHERE record_id = ?", (record_id,)
    ).fetchone()
    if row is None:
        return None
    payload: dict[str, Any] = json.loads(row[0])
    return payload


def get_original(conn: sqlite3.Connection, raw_store: RawOriginalStore, record_id: str) -> bytes:
    """Восстанавливает оригинал ответа источника по ``record_id``.

    Контрольная сумма проверяется на чтении (см. :meth:`RawOriginalStore.get`).
    """
    row = conn.execute(
        "SELECT raw_ref, checksum FROM source_records WHERE record_id = ?", (record_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"unknown record_id: {record_id}")
    raw_ref, checksum = row
    return raw_store.get(raw_ref, checksum)


def get_latest_record(
    conn: sqlite3.Connection, *, source_id: str, record_kind: str
) -> dict[str, Any] | None:
    """Возвращает самую недавно полученную (по ``fetched_at``) запись
    заданного источника и вида, независимо от ``published_at``/
    ``replay_eligible``.

    Отдельный запрос от :func:`select_as_of`: тот обслуживает строгий
    historical replay (только записи, пригодные и опубликованные не позже
    отсечения); эта функция — другой сценарий, «последний пригодный ответ
    сохраняется и отдаётся с явной давностью, когда источник недоступен»
    (.ai/main-prompt.md §5). Для источников без времени публикации (например
    ``celestrak-gp`` — ``published_at`` всегда ``None``) ``select_as_of``
    никогда бы не вернул ни одной записи; здесь это и не нужно — вызывающая
    сторона (обычно fallback при живом отказе источника) сама решает, как
    показать давность и происхождение записи.
    """
    row = conn.execute(
        """
        SELECT payload_json FROM source_records
        WHERE source_id = ? AND record_kind = ?
        ORDER BY fetched_at DESC, record_id DESC
        LIMIT 1
        """,
        (source_id, record_kind),
    ).fetchone()
    if row is None:
        return None
    payload: dict[str, Any] = json.loads(row[0])
    return payload


def select_as_of(
    conn: sqlite3.Connection,
    as_of: datetime,
    *,
    source_id: str | None = None,
    record_kind: str | None = None,
) -> list[dict[str, Any]]:
    """Возвращает записи, пригодные для строгого прогноза из прошлого на момент ``as_of``.

    Правило (.ai/main-prompt.md §1): в выборку попадают только записи с
    ``published_at <= as_of`` и ``replay_eligible == true`` — неизвестное
    время публикации не значит «вероятно, было доступно».

    Внутри одного продукта поставщика (``source_id``, ``provider_record_id``)
    возвращается запись с наибольшим ``published_at`` среди пригодных к
    ``as_of`` — самое позднее уточнение, уже известное к этому моменту
    (.ai/backend-prompt.md §2: «наибольшая версия на каждый интервал»).
    Публикация позже отсечения в выборку не попадает вовсе, даже если это
    более новая версия того же продукта.
    """
    _require_aware(as_of, "as_of")

    clauses = ["replay_eligible = 1", "published_at IS NOT NULL", "published_at <= ?"]
    params: list[Any] = [_iso_utc(as_of)]
    if source_id is not None:
        clauses.append("source_id = ?")
        params.append(source_id)
    if record_kind is not None:
        clauses.append("record_kind = ?")
        params.append(record_kind)
    where = " AND ".join(clauses)

    query = f"""
        SELECT payload_json FROM (
            SELECT
                payload_json,
                ROW_NUMBER() OVER (
                    PARTITION BY source_id, provider_record_id
                    ORDER BY published_at DESC, record_id DESC
                ) AS rn
            FROM source_records
            WHERE {where}
        )
        WHERE rn = 1
    """
    rows = conn.execute(query, params).fetchall()
    return [json.loads(row[0]) for row in rows]
