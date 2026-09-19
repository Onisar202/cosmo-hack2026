"""Archival DONKI evidence probe for the FN-43 experiment stand.

**This is NOT the NOAA S-scale and NOT a production classification.** It is
the fact that an archived NASA CCMC DONKI warning was published (with a
proven ``published_at``, main-prompt.md §1) and its extracted event interval
overlaps a given EVA window, using the already-tested strict ``as_of``
replay rule (``src.store.records.select_as_of``). It exists ONLY to feed
this experiment stand's ``warnings[]``/``mechanismAssessment.notes`` (for
traceability, О4) and ``experiments/metrics.py`` (for comparison against the
naive baseline) — it must never leak into
``mechanismAssessment.status="ok"``/``max_level`` anywhere.
``experiments/production.py`` enforces this by construction: the
``space_weather`` mechanism it builds is always ``status="missing_data"``
(see its module docstring for why), and this module's findings only ever
land in that assessment's ``notes``/``record_ids`` and in top-level
``warnings[]``, never in ``max_level``/``exceedance_hours_by_level``.

Русским по белому (main-prompt.md §4, О2 «без необоснованных заявлений»):
это НЕ шкала S NOAA, это факт публикации предупреждения архива DONKI,
используется только для сравнения с базовым методом и вычисления метрик
``experiments/``, не является production-классификацией пространства
"космическая погода".

This module is deliberately separate from ``experiments/baseline.py``: the
naive baseline must not benefit from this module's window-overlap logic
(that is exactly the "computing intersections" the naive method is defined
NOT to do, main-prompt.md §11) — see
``tests/experiments/test_baseline_independence.py``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from src.sources import archive_probe
from src.store import RawOriginalStore, insert_record, select_as_of

UTC = timezone.utc


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def ensure_donki_records(
    conn: sqlite3.Connection,
    raw_store: RawOriginalStore,
    *,
    notifications: list[archive_probe.DonkiNotification],
    source_url: str,
    fetched_at: datetime,
) -> list[str]:
    """Normalizes and inserts every DONKI notification with an extractable
    event window.

    ``archive_probe.donki_notification_to_record_input`` returns ``None``
    for notifications without a structured ``Activity ID``/``Report
    Coverage`` field (~18% of the real archive, docs/method.md §6) — those
    are honestly skipped here, not filled in with a placeholder time
    (main-prompt.md §8). Returns the ``record_id`` of every inserted (or
    already-present, idempotent) record, in input order.
    """
    record_ids: list[str] = []
    for notification in notifications:
        record_input = archive_probe.donki_notification_to_record_input(
            notification, source_url=source_url, fetched_at=fetched_at
        )
        if record_input is None:
            continue
        record_ids.append(insert_record(conn, raw_store, record_input))
    return record_ids


@dataclass(frozen=True)
class DonkiEvidenceEntry:
    """One archived DONKI notification whose stored event interval overlaps
    a queried EVA window, known as of a given cutoff — evidence only, see
    module docstring."""

    record_id: str
    message_id: str
    message_type: str
    published_at: datetime
    valid_from: datetime
    valid_to: datetime


def evidence_for_window(
    conn: sqlite3.Connection,
    *,
    as_of: datetime,
    window_start: datetime,
    window_end: datetime,
) -> list[DonkiEvidenceEntry]:
    """Strict ``as_of`` replay (``select_as_of``) restricted to DONKI
    ``warning`` records whose stored ``[valid_from, valid_to]`` interval
    overlaps ``[window_start, window_end)``.

    Records must already be inserted (see :func:`ensure_donki_records`) —
    this function only selects and filters, it does not fetch/parse/insert
    anything (main-prompt.md §8 layering).
    """
    records: list[dict[str, Any]] = select_as_of(
        conn, as_of, source_id=archive_probe.DONKI_SOURCE_ID, record_kind="warning"
    )
    entries: list[DonkiEvidenceEntry] = []
    for record in records:
        valid_from = _parse_iso(str(record["valid_from"]))
        valid_to = _parse_iso(str(record["valid_to"]))
        if valid_from < window_end and valid_to >= window_start:
            spatial_context = record.get("spatial_context") or {}
            entries.append(
                DonkiEvidenceEntry(
                    record_id=str(record["record_id"]),
                    message_id=str(record["provider_record_id"]),
                    message_type=str(spatial_context.get("message_type", "")),
                    published_at=_parse_iso(str(record["published_at"])),
                    valid_from=valid_from,
                    valid_to=valid_to,
                )
            )
    entries.sort(key=lambda entry: entry.valid_from)
    return entries


__all__ = ["DonkiEvidenceEntry", "ensure_donki_records", "evidence_for_window"]
