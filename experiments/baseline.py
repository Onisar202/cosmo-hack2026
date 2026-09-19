"""Naive baseline method for the FN-43 experiment stand.

main-prompt.md §11: "Последнее доступное наблюдение переносится на весь
горизонт без расчёта пересечений" — the last available signal is carried
forward across the whole horizon, without computing window intersections.
This module implements exactly that and nothing cleverer:

- it looks, over the ENTIRE known archive published at or before ``as_of``
  (no fixed recent lookback window — "carried forward across the whole
  horizon" is read literally: the signal never expires on its own), for the
  SINGLE MOST RECENT DONKI notification of ANY type with a known publication
  time;
- that one notification's ``messageType`` is naively classified as
  "event-type" or not (see :data:`_EVENT_MESSAGE_TYPES` below for the exact,
  documented choice) and the resulting flat verdict is applied uniformly to
  EVERY candidate window, regardless of whether that notification's own
  event timing actually overlaps that window — this is deliberately the
  "without computing intersections" part;
- if no notification with a known publication time exists at all before
  ``as_of``, it honestly reports ``no_prior_observation`` rather than
  silently defaulting to "no event" (main-prompt.md §2 — absence of a
  record must never read as a confirmed-favorable assessment).

**Event-type classification (design decision).** Every DONKI notification
is technically an "event" in the sense that something crossed a reporting
threshold, but ``FLR`` (solar flare) and ``CME`` (coronal mass ejection)
notifications occur on most days of the mandatory period (91 and 78 times
respectively across 61 days — verified against the real archive) and are
not, by themselves, the radiation/geomagnetic risk main-prompt.md §11
Mechanism 1 is about; the control-period definition in main-prompt.md §11
itself is phrased as "no SEP or GST notifications", not "no notifications
at all". This baseline therefore treats only ``SEP`` (solar energetic
particle — the proton flux this mechanism is about) and ``GST``
(geomagnetic storm — the Kp modulator main-prompt.md §11 names) as
"event-type"; when the single most recent known notification is of any
other type (``Report``, ``CME``, ``FLR``, ``IPS``, ``MPC``, ``RBE``) the
flat verdict is ``quiet_carried_forward`` — naively reading "the last thing
we heard about wasn't a proton event or a storm" as "carry quiet forward",
without checking whether an OLDER SEP/GST might still be more relevant.
This is still a naive, single-field classification of a single
most-recent record (it never inspects severity, magnitude, or timing) — it
is not the sophisticated overlap-aware evidence probe in
``experiments/donki_evidence.py``.

**Strict cutoff.** ``published_at`` (DONKI's ``resolved_issue_time``, see
``src.sources.archive_probe``) is still respected: a naive method that
leaked future information would fail main-prompt.md §1's mandatory leak
test — being "naive" is about skipping windowed intersection math, not
about ignoring ``as_of`` (see this module's docstring above and
``tests/experiments/test_leak.py``).

**Independence (structural, tested).** This module intentionally imports
ONLY the raw DONKI parsing primitives
(``src.sources.archive_probe.DonkiNotification``) — it does NOT import
``experiments.production`` or ``experiments.donki_evidence``, so the thing
being compared against cannot leak into the baseline being compared. See
``tests/experiments/test_baseline_independence.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from src.sources import archive_probe

Verdict = Literal["no_prior_observation", "event_carried_forward", "quiet_carried_forward"]

#: See the module docstring "Event-type classification" section above for
#: why exactly these two DONKI ``messageType`` values, and not every type.
_EVENT_MESSAGE_TYPES: frozenset[str] = frozenset({"SEP", "GST"})


@dataclass(frozen=True)
class BaselineWindowVerdict:
    window_id: str
    start_at: datetime
    end_at: datetime
    verdict: Verdict


@dataclass(frozen=True)
class BaselineResult:
    """The baseline's own machine-readable output for one scenario run.

    Carries enough traceability (``basis_message_id``/``basis_published_at``)
    for a human to check by hand which single archived notification the
    flat verdict was carried forward from (or that none was found) —
    acceptance criterion "baseline_result.json ... enough traceability ...
    to be checked by a human".
    """

    as_of: datetime
    verdict: Verdict
    basis_message_id: str | None
    basis_message_type: str | None
    basis_published_at: datetime | None
    lookback_note: str
    windows: tuple[BaselineWindowVerdict, ...]


def _most_recent_known_notification(
    notifications: list[archive_probe.DonkiNotification], *, as_of: datetime
) -> archive_probe.DonkiNotification | None:
    """The single most recent DONKI notification of ANY type with a KNOWN
    publication time (``resolved_issue_time is not None``) at or before
    ``as_of``, over the whole archive.

    A notification whose publication time could not be resolved
    (``resolved_issue_time is None`` — the two independent issue-time
    fields disagreed, ``src.sources.archive_probe._resolve_donki_issue_time``)
    is not "probably available by as_of" (main-prompt.md §1) — it is simply
    excluded, the same rule ``select_as_of`` already enforces for the
    production path.
    """
    eligible = [
        notification
        for notification in notifications
        if notification.resolved_issue_time is not None
        and notification.resolved_issue_time <= as_of
    ]
    if not eligible:
        return None
    best = eligible[0]
    for notification in eligible[1:]:
        assert notification.resolved_issue_time is not None
        assert best.resolved_issue_time is not None
        if notification.resolved_issue_time > best.resolved_issue_time:
            best = notification
    return best


def run_baseline(
    notifications: list[archive_probe.DonkiNotification],
    *,
    as_of: datetime,
    windows: list[tuple[str, datetime, datetime]],
) -> BaselineResult:
    """Runs the naive baseline for one scenario.

    ``windows`` is ``[(window_id, start_at, end_at), ...]`` — the SAME two
    candidate windows the production method evaluates, so the two are
    genuinely comparable (``experiments/metrics.py``).
    """
    basis = _most_recent_known_notification(notifications, as_of=as_of)
    verdict: Verdict
    if basis is None:
        verdict = "no_prior_observation"
    elif basis.message_type in _EVENT_MESSAGE_TYPES:
        verdict = "event_carried_forward"
    else:
        verdict = "quiet_carried_forward"

    window_verdicts = tuple(
        BaselineWindowVerdict(
            window_id=window_id, start_at=start_at, end_at=end_at, verdict=verdict
        )
        for window_id, start_at, end_at in windows
    )

    return BaselineResult(
        as_of=as_of,
        verdict=verdict,
        basis_message_id=basis.message_id if basis is not None else None,
        basis_message_type=basis.message_type if basis is not None else None,
        basis_published_at=basis.resolved_issue_time if basis is not None else None,
        lookback_note=(
            "Looks back over the ENTIRE known DONKI archive published at or "
            "before as_of (no fixed recent lookback window) for the single "
            "most recent notification of ANY type, classifies it as "
            "event/quiet by its messageType (SEP/GST vs. everything else — "
            "see this module's docstring), and applies that SAME flat "
            "verdict to every candidate window without checking whether "
            "that notification's own event timing overlaps that window "
            "(main-prompt.md §11)."
        ),
        windows=window_verdicts,
    )


def baseline_result_to_dict(result: BaselineResult) -> dict[str, object]:
    """Serializes :class:`BaselineResult` to the JSON shape saved as
    ``baseline_result.json`` — the baseline's own, self-describing shape
    (it does not need to satisfy contracts/result.schema.json, per the
    ticket: only production-method results go through that contract)."""
    return {
        "method": "naive_last_observation_carried_forward",
        "as_of": result.as_of.isoformat().replace("+00:00", "Z"),
        "verdict": result.verdict,
        "basis": (
            {
                "message_id": result.basis_message_id,
                "message_type": result.basis_message_type,
                "published_at": (
                    result.basis_published_at.isoformat().replace("+00:00", "Z")
                    if result.basis_published_at is not None
                    else None
                ),
            }
            if result.basis_message_id is not None
            else None
        ),
        "lookback_note": result.lookback_note,
        "windows": [
            {
                "window_id": w.window_id,
                "start_at": w.start_at.isoformat().replace("+00:00", "Z"),
                "end_at": w.end_at.isoformat().replace("+00:00", "Z"),
                "verdict": w.verdict,
            }
            for w in result.windows
        ],
    }


__all__ = [
    "BaselineResult",
    "BaselineWindowVerdict",
    "Verdict",
    "baseline_result_to_dict",
    "run_baseline",
]
