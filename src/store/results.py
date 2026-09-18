"""Публичный интерфейс неизменяемого хранилища результатов расчёта.

Ни одна функция этого модуля не обновляет и не удаляет сохранённый результат:
пересчёт (в том числе с теми же параметрами) создаёт новый ``result_id`` и
новую строку, существующая не трогается (.ai/main-prompt.md §3,
.ai/backend-prompt.md §1). Параметры разных результатов изолированы —
строки друг от друга не зависят и хранятся в отдельных, независимо читаемых
записях.

``store_result`` дополнительно проверяет, что ``data_manifest`` результата —
это не список правдоподобных, а фактически существующих в хранилище записей:
каждая запись манифеста должна быть найдена по ``record_id`` и совпадать с
ним по ``source_id``/``source_version``/``record_kind``. Так «immutable
result с фактическим manifest» из приёмки задачи проверяется хранилищем, а
не остаётся соглашением между слоями на словах.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

_REQUIRED_FIELDS = (
    "result_id",
    "computed_at",
    "mode",
    "algorithm_version",
    "data_manifest",
)


class ManifestVerificationError(ValueError):
    """``data_manifest`` результата ссылается на запись, которой нет в хранилище."""


def store_result(
    conn: sqlite3.Connection,
    result: dict[str, Any],
    *,
    verify_manifest: bool = True,
) -> str:
    """Сохраняет уже полностью сформированный результат расчёта.

    Форма результата — ``contracts/result.schema.json``; полную валидацию по
    JSON Schema выполняет вызывающий слой (домен/API, задачи зоны 3).
    Хранилище проверяет только предпосылки собственной неизменности: наличие
    полей, нужных для индексации, уникальность ``result_id`` и (по умолчанию)
    ссылочную целостность ``data_manifest``.
    """
    missing = [f for f in _REQUIRED_FIELDS if f not in result]
    if missing:
        raise ValueError(f"result is missing required fields: {missing}")

    result_id = result["result_id"]
    existing = conn.execute(
        "SELECT 1 FROM calculation_results WHERE result_id = ?", (result_id,)
    ).fetchone()
    if existing is not None:
        raise ValueError(
            f"result_id already stored, results are immutable: {result_id!r}"
        )

    if verify_manifest:
        _verify_manifest(conn, result["data_manifest"])

    conn.execute(
        """
        INSERT INTO calculation_results (
            result_id, computed_at, mode, as_of, algorithm_version, payload_json
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            result_id,
            result["computed_at"],
            result["mode"],
            result.get("as_of"),
            result["algorithm_version"],
            json.dumps(result, ensure_ascii=False, sort_keys=True),
        ),
    )
    conn.commit()
    return str(result_id)


def _verify_manifest(conn: sqlite3.Connection, data_manifest: list[dict[str, Any]]) -> None:
    for entry in data_manifest:
        row = conn.execute(
            "SELECT source_id, source_version, record_kind FROM source_records WHERE record_id = ?",
            (entry["record_id"],),
        ).fetchone()
        if row is None:
            raise ManifestVerificationError(
                f"data_manifest references unknown record_id: {entry['record_id']!r}"
            )
        actual = {"source_id": row[0], "source_version": row[1], "record_kind": row[2]}
        expected = {
            "source_id": entry["source_id"],
            "source_version": entry["source_version"],
            "record_kind": entry["record_kind"],
        }
        if actual != expected:
            raise ManifestVerificationError(
                f"data_manifest entry for {entry['record_id']!r} does not match "
                f"the stored record: expected {expected}, stored {actual}"
            )


def get_result(conn: sqlite3.Connection, result_id: str) -> dict[str, Any] | None:
    """Возвращает результат (в форме ``contracts/result.schema.json``) или ``None``."""
    row = conn.execute(
        "SELECT payload_json FROM calculation_results WHERE result_id = ?", (result_id,)
    ).fetchone()
    if row is None:
        return None
    payload: dict[str, Any] = json.loads(row[0])
    return payload


def list_results(
    conn: sqlite3.Connection, *, mode: str | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    """Список сохранённых результатов, новейшие первыми."""
    if mode is not None:
        rows = conn.execute(
            """
            SELECT payload_json FROM calculation_results
            WHERE mode = ? ORDER BY computed_at DESC LIMIT ?
            """,
            (mode, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT payload_json FROM calculation_results ORDER BY computed_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [json.loads(row[0]) for row in rows]
