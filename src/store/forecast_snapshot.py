"""Неизменяемый снимок входа строгого ``historical_forecast`` — записи и
карта покрытия ВМЕСТЕ, закреплённые за конкретным расчётом — FN-42, round
4/6/7 ревью PR #37.

``src/domain/spaceweather/archive_assessment.py::assess_archive_window`` —
чистая функция без состояния: она честно считает покрытие по тому, что ей
передали и не может сама заметить, что переданные записи или интервалы
покрытия выросли со времени предыдущего вызова для того же расчёта. А они
растут: production orchestration собирает и записи (``select_as_of``), и
карту покрытия (``merged_ingested_intervals``) заново каждый раз, когда
архив дозагружается — по любому поводу, не обязательно ради конкретного
``as_of``. Без дополнительной меры вход уже вычисленного в прошлом
``historical_forecast`` мог бы задним числом стать полнее и изменить
оценку — то есть результат уже случившегося строгого прогноза из прошлого
незаметно менялся бы от того, что произошло (было загружено) уже СЕГОДНЯ.
main-prompt.md §3: «Сохранённый результат неизменяем. Пересчёт создаёт
новый result_id» — этот модуль обеспечивает именно это на уровне входа
оценки, а не только на уровне готового результата.

**Записи и покрытие закреплены ОДНИМ снимком, не двумя независимыми
закреплениями** (round 7 ревью PR #37, finding 2). Более ранняя редакция
(round 4/6) закрепляла только предел ``fetched_at`` для карты покрытия
сравнением с сохранённым скаляром при каждом вызове — набор ЗАПИСЕЙ при
этом каждый раз выбирался заново через ``select_as_of`` и мог тихо
измениться между двумя вызовами одного и того же расчёта (архив пополнился
записью с ``published_at <= as_of``, но её загрузили позже первого вызова).
Здесь снимок — это дословно сохранённое СОДЕРЖИМОЕ (``record_id`` и
интервалы), а не правило пересчёта: первый вызов сохраняет ровно то, что
ему передали (включая пустой вход — round 6 ревью, finding 1, снимок
закрепляется и тогда), любой последующий читает это содержимое обратно
целиком и не пересчитывает и не сравнивает ничего заново — структурно
неспособен унаследовать баг «фильтрация после склейки» предыдущих раундов.

**``computation_id`` — идентификатор конкретного расчёта, обязательный
параметр** (round 6 ревью PR #37, finding 3). Без него закрепление было бы
завязано только на (``source_id``, ``as_of``) — общий, разделяемый между
ВСЕМИ вызывающими ключ: первый же расчёт для этой пары, в том числе
случайный или несвязанный, необратимо решал бы, какой вход увидят все
последующие независимые расчёты с тем же ``as_of``. Повторный вызов с ТЕМ
ЖЕ ``computation_id`` — идемпотентный повтор/ретрай одного и того же
расчёта; с ДРУГИМ — независимое закрепление.

**``snapshot_id`` — собственный, сервером сгенерированный идентификатор
именно этого снимка** (round 7 ревью PR #37, finding 3, ⚠️), не совпадающий
и не обязанный совпадать с ``computation_id``. Его можно сохранить в
``data_manifest`` результата и по нему одному восстановить использованные
``record_id`` и интервалы покрытия из выгрузки — не полагаясь на то, что
``computation_id`` (генерируемый вызывающей стороной, а не этим модулем)
когда-нибудь окажется равен будущему ``result_id`` production orchestration.

Таблицы — только вставка (``INSERT OR IGNORE``), как и весь остальной
``store/`` (.ai/backend-prompt.md §1–2): содержимое, однажды закреплённое за
(``computation_id``, ``source_id``, ``as_of``), никогда не перезаписывается.
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import NamedTuple

from src.store.records import _iso_utc, _require_aware


class SealedForecastSnapshot(NamedTuple):
    """Итог :func:`seal_forecast_input_snapshot`: не обязательно то, что
    было передано этим вызовом — то, что было закреплено ПЕРВЫМ вызовом для
    этого ключа, каким бы он ни был."""

    snapshot_id: str
    record_ids: tuple[str, ...]
    intervals: tuple[tuple[datetime, datetime, datetime], ...]


def seal_forecast_input_snapshot(
    conn: sqlite3.Connection,
    *,
    computation_id: str,
    source_id: str,
    as_of: datetime,
    candidate_record_ids: Sequence[str],
    candidate_intervals: Sequence[tuple[datetime, datetime, datetime]],
) -> SealedForecastSnapshot:
    """Закрепляет (при первом обращении) или читает уже закреплённый снимок
    входа для (``computation_id``, ``source_id``, ``as_of``).

    Первый вызов сохраняет ``candidate_record_ids``/``candidate_intervals``
    как есть, генерирует новый ``snapshot_id`` и возвращает их же. Любой
    последующий вызов с ТЕМ ЖЕ ключом полностью игнорирует переданные
    ``candidate_*`` этого вызова и возвращает то, что было сохранено при
    первом вызове — даже если с тех пор набор пригодных записей или карта
    покрытия честно выросли (см. модульный докстринг).

    ``candidate_intervals`` — тройки ``(start, end, fetched_at)``, а не
    ``IngestedInterval`` напрямую: этот модуль в ``store/`` не должен
    зависеть от ``src/sources/`` (main-prompt.md §8, тот же приём, что и в
    прежней редакции этого модуля и в ``archive_assessment.py``).
    """
    _require_aware(as_of, "as_of")
    for start, end, fetched_at in candidate_intervals:
        _require_aware(start, "interval start")
        _require_aware(end, "interval end")
        _require_aware(fetched_at, "interval fetched_at")

    candidate_snapshot_id = str(uuid.uuid4())
    conn.execute(
        "INSERT OR IGNORE INTO archive_forecast_snapshots "
        "(computation_id, source_id, as_of, snapshot_id) VALUES (?, ?, ?, ?)",
        (computation_id, source_id, _iso_utc(as_of), candidate_snapshot_id),
    )
    row = conn.execute(
        "SELECT snapshot_id FROM archive_forecast_snapshots "
        "WHERE computation_id = ? AND source_id = ? AND as_of = ?",
        (computation_id, source_id, _iso_utc(as_of)),
    ).fetchone()
    assert row is not None  # только что вставили либо строка уже существовала
    snapshot_id = str(row[0])

    if snapshot_id == candidate_snapshot_id:
        # Этот вызов первым закрепил снимок за этим ключом — записываем то,
        # что нам передали, как окончательное содержимое снимка.
        conn.executemany(
            "INSERT OR IGNORE INTO archive_forecast_snapshot_records "
            "(snapshot_id, seq, record_id) VALUES (?, ?, ?)",
            [
                (snapshot_id, seq, record_id)
                for seq, record_id in enumerate(candidate_record_ids)
            ],
        )
        conn.executemany(
            "INSERT OR IGNORE INTO archive_forecast_snapshot_intervals "
            "(snapshot_id, seq, interval_start, interval_end, fetched_at) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (snapshot_id, seq, _iso_utc(start), _iso_utc(end), _iso_utc(fetched_at))
                for seq, (start, end, fetched_at) in enumerate(candidate_intervals)
            ],
        )
        conn.commit()
        return SealedForecastSnapshot(
            snapshot_id=snapshot_id,
            record_ids=tuple(candidate_record_ids),
            intervals=tuple(candidate_intervals),
        )

    # Снимок для этого ключа уже был закреплён раньше — читаем сохранённое
    # тогда содержимое и полностью игнорируем candidate_* этого вызова.
    sealed = read_sealed_snapshot(conn, snapshot_id)
    assert sealed is not None  # строка archive_forecast_snapshots только что найдена выше
    return sealed


def read_sealed_snapshot(
    conn: sqlite3.Connection, snapshot_id: str
) -> SealedForecastSnapshot | None:
    """Восстанавливает закреплённое содержимое снимка **только** по его
    собственному ``snapshot_id`` — без ``computation_id``/``source_id``/
    ``as_of`` (round 7 ревью PR #37, finding 3, ⚠️): это и есть то самое
    «восстановить использованный вход из выгрузки», ради чего снимок несёт
    отдельный от ``computation_id`` идентификатор. Возвращает ``None``, если
    такого снимка не существует.
    """
    exists = conn.execute(
        "SELECT 1 FROM archive_forecast_snapshots WHERE snapshot_id = ?", (snapshot_id,)
    ).fetchone()
    if exists is None:
        return None
    record_rows = conn.execute(
        "SELECT record_id FROM archive_forecast_snapshot_records "
        "WHERE snapshot_id = ? ORDER BY seq",
        (snapshot_id,),
    ).fetchall()
    interval_rows = conn.execute(
        "SELECT interval_start, interval_end, fetched_at "
        "FROM archive_forecast_snapshot_intervals WHERE snapshot_id = ? ORDER BY seq",
        (snapshot_id,),
    ).fetchall()
    return SealedForecastSnapshot(
        snapshot_id=snapshot_id,
        record_ids=tuple(str(r[0]) for r in record_rows),
        intervals=tuple(
            (_parse_iso(str(r[0])), _parse_iso(str(r[1])), _parse_iso(str(r[2])))
            for r in interval_rows
        ),
    )


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


__all__ = ["SealedForecastSnapshot", "read_sealed_snapshot", "seal_forecast_input_snapshot"]
