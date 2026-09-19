"""Последующие наблюдения — ОТДЕЛЬНАЯ ветка кода, не вход прогноза (FN-42).

.ai/main-prompt.md §1: «Последующие наблюдения — отдельная ветка кода.
Проверка качества прогноза и вход прогноза не должны быть одной функцией с
булевым флагом: флаг рано или поздно окажется не в том значении.»

Поэтому этот модуль существует отдельно от ``src/store/records.py`` и
намеренно **не** добавляет параметр к :func:`src.store.records.select_as_of`.
Две выборки разведены структурно, а не режимом одного вызова:

===============================  ==========================================
``records.select_as_of``         ``verification.select_verification_records``
===============================  ==========================================
``published_at <= as_of``        ``published_at > as_of``
вход строгого прогноза           проверка того, что прогноз угадал
``replay_eligible = 1``          ``replay_eligible`` не требуется
попадает в ``data_manifest``     НЕ попадает во вход расчёта никогда
===============================  ==========================================

Условия двух функций взаимно исключающи по построению (``<=`` против ``>``
на одном и том же поле), поэтому ни одна запись не может попасть в обе
выборки одновременно — это проверяется тестом
``tests/store/test_verification_path.py::test_forecast_input_and_verification_sets_are_disjoint``.
Вызов этого модуля из кода, собирающего вход ``historical_forecast``, — сам
по себе ошибка: результат этой функции по определению не был доступен к
моменту отсечения.

Записи с неизвестным ``published_at`` (``NULL``) не возвращаются и здесь: их
момент появления неизвестен, поэтому нельзя утверждать и того, что они
пришли **после** отсечения (.ai/main-prompt.md §1 — неизвестное время
публикации не превращается в утверждение ни в одну сторону).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

from src.store.records import _iso_utc, _require_aware


def select_verification_records(
    conn: sqlite3.Connection,
    as_of: datetime,
    *,
    source_id: str | None = None,
    record_kind: str | None = None,
    observed_from: datetime | None = None,
    observed_to: datetime | None = None,
) -> list[dict[str, Any]]:
    """Возвращает записи, появившиеся ПОСЛЕ отсечения ``as_of``.

    Единственное назначение — апостериорная проверка качества уже
    построенного прогноза (Т4: «последующая информация — только для
    проверки»). Результат нельзя передавать в расчёт прогноза: это
    информация из будущего относительно ``as_of``.

    В отличие от :func:`src.store.records.select_as_of`, здесь **не**
    выбирается «последняя версия на каждый ``provider_record_id``»:
    для проверки важны все поздние уточнения как они приходили, а не одно
    сводное состояние — схлопывание версий скрыло бы, что уточнение
    приходило дважды и во второй раз меняло вывод.

    ``observed_from``/``observed_to`` (полуоткрытый интервал
    ``[observed_from, observed_to)``) ограничивают проверку тем же периодом,
    на который делался прогноз, — иначе в разбор попали бы наблюдения о
    совершенно другом интервале.
    """
    _require_aware(as_of, "as_of")

    clauses = ["published_at IS NOT NULL", "published_at > ?"]
    params: list[Any] = [_iso_utc(as_of)]
    if source_id is not None:
        clauses.append("source_id = ?")
        params.append(source_id)
    if record_kind is not None:
        clauses.append("record_kind = ?")
        params.append(record_kind)
    if observed_from is not None:
        _require_aware(observed_from, "observed_from")
        clauses.append("observed_at >= ?")
        params.append(_iso_utc(observed_from))
    if observed_to is not None:
        _require_aware(observed_to, "observed_to")
        clauses.append("observed_at < ?")
        params.append(_iso_utc(observed_to))

    query = f"""
        SELECT payload_json FROM source_records
        WHERE {" AND ".join(clauses)}
        ORDER BY published_at ASC, record_id ASC
    """
    rows = conn.execute(query, params).fetchall()
    return [json.loads(row[0]) for row in rows]


__all__ = ["select_verification_records"]
