"""Deterministic, content-derived ``record_id`` generation for the offline
experiment stand.

``src.store.records.insert_record`` assigns each newly-inserted record a
fresh ``uuid.uuid4()`` ``record_id`` — correct and required for the real
service (a storage surrogate key that must never be predictable or
content-derived), but it means two independent runs of this runner would
embed different random ``record_id`` values in otherwise byte-identical
JSON artifacts, breaking the byte-for-byte determinism this stand requires
(see ``experiments/run.py`` module docstring).

Rather than touch ``src/store`` (out of scope for FN-43, and it must stay a
correctly-random store for the real service), this context manager wraps
``insert_record`` for the duration of one scenario build so that the
``record_id`` it assigns to a genuinely NEW record is derived from that
record's own dedup key (``source_id``/``provider_record_id``/
``source_version``) plus a caller-supplied ``seed`` — not from call order.
This matters beyond "same scenario twice gives the same file": a leak test
(``tests/experiments/test_leak.py``) inserts one EXTRA record (a
future-published DONKI notification) and compares the built result against
a build without it — with a purely call-order-based counter, that single
extra ``insert_record`` call would shift every *unrelated* record inserted
afterwards to a different (still deterministic, but different) id, making
an otherwise-correct leak-safe result look "different" for a reason that
has nothing to do with the leak. Deriving the id from content instead of
position makes every record's id independent of what else happens to be
inserted around it.

``insert_record`` is imported directly (``from src.store.records import
... insert_record``) by three modules in this call graph
(``src.sources.mmod``, ``experiments.production``,
``experiments.donki_evidence``) — each binds its own local name at import
time, so patching ``src.store.records.insert_record`` alone would not
affect calls already bound through those local names. This context manager
therefore patches all four locations and restores every one of them
afterwards, even on exception.
"""

from __future__ import annotations

import contextlib
import functools
import uuid
from collections.abc import Iterator
from typing import Any, Protocol

import src.sources.mmod as _mmod_source_module
import src.store.records as _records_module

_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "experiments.fn43.cosmo-hack2026")


class _InsertRecord(Protocol):
    def __call__(self, conn: Any, raw_store: Any, record: Any, /) -> str: ...


def _make_deterministic_insert_record(original: _InsertRecord, *, seed: str) -> _InsertRecord:
    @functools.wraps(original)
    def _deterministic_insert_record(conn: Any, raw_store: Any, record: Any, /) -> str:
        dedup_key = f"{seed}:{record.source_id}:{record.provider_record_id}:{record.source_version}"
        deterministic_id = uuid.uuid5(_NAMESPACE, dedup_key)
        original_uuid4 = uuid.uuid4
        uuid.uuid4 = lambda: deterministic_id
        try:
            return original(conn, raw_store, record)
        finally:
            uuid.uuid4 = original_uuid4

    return _deterministic_insert_record


@contextlib.contextmanager
def deterministic_record_ids(seed: str) -> Iterator[None]:
    """Makes every ``insert_record`` call inside this block assign a
    deterministic, content-derived ``record_id``.

    ``seed`` should be unique per scenario (e.g. the scenario name) so that
    two different scenarios inserting a record with the same dedup key
    (should that ever happen) still get distinct ids.
    """
    # Imported lazily (not at module top level) to avoid a hard import-time
    # dependency of this low-level helper on experiments.production/
    # experiments.donki_evidence for callers that only need e.g.
    # experiments.config; both are safe to import here regardless (no
    # import cycle back into this module).
    from experiments import donki_evidence as _donki_evidence_module
    from experiments import production as _production_module

    patch_targets = (
        _records_module,
        _mmod_source_module,
        _donki_evidence_module,
        _production_module,
    )
    original_by_target = {target: target.insert_record for target in patch_targets}
    wrapped = _make_deterministic_insert_record(_records_module.insert_record, seed=seed)
    for target in patch_targets:
        target.insert_record = wrapped  # type: ignore[attr-defined]
    try:
        yield
    finally:
        for target, original in original_by_target.items():
            target.insert_record = original  # type: ignore[attr-defined]


__all__ = ["deterministic_record_ids"]
