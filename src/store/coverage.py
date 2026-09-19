"""Фиксация «до какого fetched_at покрытие архива доверено» для строгого
historical_forecast — FN-42, round 4 ревью PR #37.

``src/domain/spaceweather/archive_assessment.py::assess_archive_window`` —
чистая функция без состояния: она честно считает покрытие по тому, что ей
передали, и не может сама заметить, что переданный список
``ingested_intervals`` вырос со времени предыдущего вызова для той же пары
(``source_id``, ``as_of``). А он растёт: production orchestration собирает
``ingested_intervals`` заново из ``merged_ingested_intervals`` по ВСЕМ
накопленным на текущий момент отчётам о загрузке — то есть каждый новый
раз, когда кто-то догружает архив (по любому поводу, не обязательно ради
именно этого ``as_of``), карта покрытия для уже оценённого в прошлом
``historical_forecast`` может внезапно стать полнее и превратить
``INSUFFICIENT_DATA`` в ``NO_EVENT_DETECTED`` задним числом — то есть
результат уже как бы «свершившегося» строгого прогноза из прошлого
незаметно меняется от того, что случилось (было догружено) уже СЕГОДНЯ.
main-prompt.md §3: «Сохранённый результат неизменяем. Пересчёт создаёт
новый result_id» — этот модуль обеспечивает именно это на уровне входа
оценки, а не только на уровне готового результата: коль скоро для пары
(``source_id``, ``as_of``) уже был использован какой-то предел ``fetched_at``,
он закрепляется навсегда и не отодвигается более поздними загрузками.

Важно, чего этот модуль **не** делает: он не сравнивает ``fetched_at`` с
самим ``as_of`` напрямую. ``as_of`` — историческая дата (например, май 2024),
а ``fetched_at`` — всегда «сегодня» реального конвейера (например, 2026) —
условие ``fetched_at <= as_of`` было бы невыполнимо в принципе и сделало бы
``historical_forecast`` невозможным по построению, что прямо противоречит
постановке (main-prompt.md §11: «строгий прогноз из прошлого... прямое
требование постановки»). Вместо этого фиксируется **первый увиденный**
``fetched_at`` для данной пары — это и есть версия покрытия, допустимая для
этого ``as_of``, а не сравнение с самим ``as_of``.

Таблица ``archive_coverage_cutoffs`` — только вставка (``INSERT OR IGNORE``),
как и весь остальной ``store/`` (.ai/backend-prompt.md §1–2): значение,
однажды закреплённое для пары (``source_id``, ``as_of``), никогда не
перезаписывается — в том числе более ранним значением, если по ошибке
переданный ``candidate_fetched_at`` окажется меньше уже закреплённого.

Этот модуль намеренно останавливается на самом пределе ``fetched_at`` и не
решает, КАК фильтровать или склеивать интервалы покрытия по этому пределу:
склейка соседних окон загрузки (``merged_ingested_intervals``) — понятие
``src/sources/``, а ``store/`` от ``sources/`` не зависит (main-prompt.md §8).
Комбинация «закрепить предел → отфильтровать НЕ склеенные интервалы →
склеить прошедшие фильтр» (в этом самом порядке — round 4 ревью PR #37:
склейка ДО фильтрации схлопывает ``fetched_at`` соседних интервалов в
``max()`` и совместно теряет уже доверенную раннюю часть) живёт в
``src/sources/archive_ingest.py::pin_and_merge_ingested_intervals``, единственном
месте, которому известны оба понятия.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from src.store.records import _iso_utc, _require_aware


def pin_coverage_cutoff(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    as_of: datetime,
    candidate_fetched_at: datetime,
) -> datetime:
    """Закрепляет (при первом обращении) или возвращает уже закреплённый
    предел ``fetched_at``, допустимый для покрытия архива при оценке
    ``historical_forecast`` с данным ``as_of``.

    Первый вызов для пары (``source_id``, ``as_of``) закрепляет
    ``candidate_fetched_at`` и возвращает его же. Любой последующий вызов —
    даже с бо́льшим ``candidate_fetched_at`` (архив догрузили) — возвращает
    ИСХОДНОЕ закреплённое значение, не заменяя его: строгий прогноз из
    прошлого для уже пройденного ``as_of`` не должен молча становиться
    полнее оттого, что кто-то сегодня доисследовал архив по несвязанному
    поводу.
    """
    _require_aware(as_of, "as_of")
    _require_aware(candidate_fetched_at, "candidate_fetched_at")
    conn.execute(
        "INSERT OR IGNORE INTO archive_coverage_cutoffs "
        "(source_id, as_of, fetched_at_cutoff) VALUES (?, ?, ?)",
        (source_id, _iso_utc(as_of), _iso_utc(candidate_fetched_at)),
    )
    conn.commit()
    row = conn.execute(
        "SELECT fetched_at_cutoff FROM archive_coverage_cutoffs "
        "WHERE source_id = ? AND as_of = ?",
        (source_id, _iso_utc(as_of)),
    ).fetchone()
    assert row is not None  # только что вставили либо строка уже существовала
    return datetime.fromisoformat(str(row[0]).replace("Z", "+00:00"))


__all__ = ["pin_coverage_cutoff"]
