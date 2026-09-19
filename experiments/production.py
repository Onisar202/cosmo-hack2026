"""Standalone production-method assembly for the FN-43 experiment stand.

Builds a ``CalculationResult``-shaped dict (contracts/result.schema.json)
for ``mode="historical_forecast"`` DIRECTLY from the already-merged,
lower-level domain/source modules this repository ships
(``src.sources.orbit_history``/``src.sources.orbit``, ``src.sources.mmod``,
``src.domain.mmod.background``, ``src.domain.windows``) — the same modules
``src/api/service.py`` itself calls for ``mode="current"``. It does NOT call
``src/api/service.py`` (whose ``mode != "current"`` path always raises
``CalculationError("historical_mode_not_implemented", ...)`` — FN-41/FN-42
are separate, not-yet-merged tickets, see ``src/api/service.py`` module
docstring and around line 1402) and does not modify it.

**space_weather is ALWAYS reported honestly as ``status="missing_data"`` for
every window here.** There is no archived quantitative pfu OBSERVATION
source in this repository today — only the live ``noaa-swpc-proton-flux``
(``record_kind="observation"``, ``published_at`` always ``None`` →
never ``replay_eligible``) that ``src.domain.spaceweather.observed_classifier``
consumes for ``mode="current"``. Archived DONKI notifications
(``record_kind="warning"``) and archived SWPC Forecast Discussion
(``record_kind="forecast"``) are not quantitative flux observations and
``observed_classifier.assess_observed_flux`` cannot consume them; nothing in
this repository can produce a numeric DONKI→S-scale classification (that is
what FN-41/FN-42 are meant to deliver). Forcing ``status="ok"`` here would be
exactly the "fabricate a result the codebase cannot actually produce" this
ticket forbids (main-prompt.md §2). The real archived DONKI evidence is
still gathered and surfaced — see ``experiments/donki_evidence.py`` — but
only in ``notes``/``record_ids``/``warnings[]``, never in
``max_level``/``exceedance_hours_by_level``.

MMOD (Mechanism 2), by contrast, CAN be ``status="ok"`` here: the bundled
NASA MEO 2024 document (``src.sources.mmod``) covers the whole mandatory
period, exactly mirroring what ``src/api/service.py::_mmod_mechanism_assessment``
does for ``mode="current"`` (this module does not call that private
function — it is a small, independent equivalent built only from the
public ``src.sources.mmod``/``src.domain.mmod.background``/``src.store``
functions, per the ticket).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from experiments import donki_evidence, fixtures
from experiments.config import Scenario
from src.domain.mmod import background as mmod_background
from src.domain.windows import WindowCandidate, excluded_windows, recommend
from src.sources import mmod as mmod_source
from src.sources import orbit, orbit_history
from src.store import RawOriginalStore, insert_record, select_as_of

UTC = timezone.utc

#: experiments-scoped algorithm version, deliberately SEPARATE from
#: ``src.api.schemas.ALGORITHM_VERSION`` (currently "0.4.0"): this stand
#: computes a different, standalone thing (it never runs
#: ``src/api/service.py`` at all, and its space_weather line is honestly
#: fixed at ``missing_data`` rather than reflecting the real observed-GOES
#: classifier main-prompt.md §3 ties algorithm_version to "the algorithm
#: producing the result" — conflating the two version numbers would wrongly
#: imply this stand's output is comparable/reproducible against a
#: production API result of the same version, which it is not (different
#: mode, different space_weather honesty floor). Bump this whenever this
#: module's own logic changes.
ALGORITHM_VERSION = "0.1.0"

DONKI_EVENT_MESSAGE_TYPES: frozenset[str] = frozenset({"SEP", "GST"})


class OrbitSelectionFailure(Exception):
    """Wraps ``orbit.HistoricalElementsUnsupportedError`` with the extra
    scenario context needed to build ``production_failure.json`` — a
    structured, honest "result" in its own right (acceptance criterion:
    "each result, baseline included, must be saved machine-readably")."""

    def __init__(
        self,
        *,
        scenario: Scenario,
        interval_start: datetime,
        interval_end: datetime,
        cause: Exception,
    ) -> None:
        super().__init__(str(cause))
        self.scenario = scenario
        self.interval_start = interval_start
        self.interval_end = interval_end
        self.cause = cause

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario.name,
            "as_of": _iso(self.scenario.as_of),
            "requested_interval": {
                "start": _iso(self.interval_start),
                "end": _iso(self.interval_end),
            },
            "error": {
                "type": type(self.cause).__name__,
                "message": str(self.cause),
            },
            "why_this_is_correct_not_a_bug": (
                "src.sources.orbit.select_oem_elements_for_request found no NASA "
                "OEM release whose useable interval "
                "[USEABLE_START_TIME, USEABLE_STOP_TIME] covers the requested "
                "interval in full while also being published (S3 LastModified) at "
                "or before as_of. This is a real, pre-selected, proven archival "
                "gap (the 2024-05-12 release's useable interval ends "
                "2024-05-25T12:00Z, the next release, 2024-06-14, starts "
                "2024-06-14T12:00Z — tests/fixtures/orbit/history/README.md), not "
                "a bug: current CelesTrak elements are never substituted for "
                "historical ones (.ai/main-prompt.md §1, §11), and no later or "
                "non-covering release is substituted either. The honest artifact "
                "for this scenario is this structured failure, not a fabricated "
                "orbit or a fabricated calm result (.ai/main-prompt.md §2)."
            ),
        }


@dataclass(frozen=True)
class WindowSpec:
    window_id: str
    start_at: datetime
    end_at: datetime


@dataclass(frozen=True)
class ProductionBuild:
    """Exactly one of ``result``/``failure`` is set.

    ``evidence_by_window`` is always populated (even on orbit failure is
    NOT populated then, since no windows were evaluated) — it is exposed
    separately from ``result`` so ``experiments/metrics.py`` can use the
    DONKI evidence without re-parsing the archive or re-querying the store.
    """

    result: dict[str, Any] | None
    failure: dict[str, Any] | None
    evidence_by_window: dict[str, list[donki_evidence.DonkiEvidenceEntry]]


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _mmod_mechanism_assessment(
    conn: sqlite3.Connection,
    raw_store: RawOriginalStore,
    *,
    window: WindowSpec,
    as_of: datetime,
    fetched_at: datetime,
) -> dict[str, Any]:
    """Small, independent equivalent of
    ``src/api/service.py::_mmod_mechanism_assessment`` for ``mode="current"``
    — same public functions (``mmod_source.ensure_mmod_records_for_window``,
    ``select_as_of``, ``mmod_background.background_nodes_from_records``,
    ``mmod_background.assess_mmod_background``), same shape, no geometry
    multiplied in (production itself does not multiply MMOD geometry into
    the numeric level either — see that function's docstring).

    ``as_of``/``fetched_at`` are deliberately separate (FN-46 round 7
    review, point 2): ``as_of`` gates ``select_as_of``'s strict replay
    filter (``published_at <= as_of``) — it must stay the scenario's own
    cutoff; ``fetched_at`` is the record's own provenance field (when WE
    retrieved this document) — it must be the real retrieval time
    (:func:`experiments.fixtures.mmod_fetched_at`), never the scenario's
    ``as_of`` substituted in its place.
    """
    try:
        _record_ids, doc_start, doc_end = mmod_source.ensure_mmod_records_for_window(
            conn,
            raw_store,
            window_start=window.start_at,
            window_end=window.end_at,
            fetched_at=fetched_at,
        )
        mmod_records = select_as_of(
            conn, as_of, source_id=mmod_source.SOURCE_ID, record_kind="forecast"
        )
        nodes = mmod_background.background_nodes_from_records(mmod_records)
        assessment = mmod_background.assess_mmod_background(
            nodes,
            window_start=window.start_at,
            window_end=window.end_at,
            document_grid_start=doc_start,
            document_grid_end=doc_end,
        )
    except Exception as exc:  # noqa: BLE001 - a source failure here must not
        # crash the whole scenario build, same principle as src/api/service.py.
        return {
            "mechanism": "mmod",
            "status": "source_error",
            "max_level": None,
            "exceedance_hours_by_level": None,
            "coverage_fraction": 0.0,
            "critical_gap": True,
            "notes": [
                f"NASA MEO 2024 LEO forecast: error while reading/assessing "
                f"({type(exc).__name__}: {exc}) — critical gap, not a calm "
                "assessment (main-prompt.md §2)."
            ],
            "record_ids": [],
        }
    return {
        "mechanism": "mmod",
        "status": assessment.status,
        "max_level": assessment.max_level,
        "exceedance_hours_by_level": assessment.exceedance_hours_by_level,
        "coverage_fraction": assessment.coverage_fraction,
        "critical_gap": assessment.critical_gap,
        "notes": list(assessment.notes),
        "record_ids": list(assessment.record_ids),
    }


def _space_weather_mechanism(
    evidence: list[donki_evidence.DonkiEvidenceEntry],
) -> dict[str, Any]:
    """Always ``status="missing_data"`` — see the module docstring. DONKI
    evidence overlapping this window is surfaced in ``notes``/``record_ids``
    ONLY, clearly labeled as an evidence probe, never as a level."""
    record_ids = [entry.record_id for entry in evidence]
    notes = [
        "space_weather: нет архивного количественного наблюдения потока "
        "протонов (pfu) за этот период — noaa-swpc-proton-flux не несёт "
        "published_at и никогда не replay_eligible (FN-41/FN-42 — не "
        "слитые задачи, вне области FN-43). Честный статус — missing_data, "
        "не имитация уровня (main-prompt.md §2)."
    ]
    if evidence:
        entries_text = "; ".join(
            f"{entry.message_id} ({entry.message_type}, published {_iso(entry.published_at)}, "
            f"event [{_iso(entry.valid_from)}..{_iso(entry.valid_to)}])"
            for entry in evidence
        )
        notes.append(
            "Архивная evidence-проба DONKI (experiments/donki_evidence.py) — это "
            "НЕ шкала S NOAA, а факт публикации предупреждения архива, "
            "пересекающегося с этим окном, используется только для сравнения с "
            f"базовым методом и метрик experiments/: {entries_text}."
        )
    else:
        notes.append(
            "Архивная evidence-проба DONKI не нашла ни одного предупреждения, "
            "пересекающегося с этим окном и известного к as_of."
        )
    return {
        "mechanism": "space_weather",
        "status": "missing_data",
        "max_level": None,
        "exceedance_hours_by_level": None,
        "coverage_fraction": 0.0,
        "critical_gap": True,
        "notes": notes,
        "record_ids": record_ids,
    }


def _evidence_warning(
    window_id: str, evidence: list[donki_evidence.DonkiEvidenceEntry]
) -> dict[str, Any]:
    entries_text = "; ".join(
        f"{entry.message_id} ({entry.message_type})" for entry in evidence
    )
    return {
        "code": "donki-archive-evidence",
        "severity": "info",
        "mechanism": "space_weather",
        "message": (
            f"Окно {window_id}: архивные предупреждения DONKI, известные к as_of и "
            f"пересекающиеся с этим окном: {entries_text}. Это evidence-проба "
            "(experiments/donki_evidence.py), не production-классификация "
            "механизма space_weather."
        ),
        "record_ids": [entry.record_id for entry in evidence],
        "fetch_attempt_id": None,
        "window_id": window_id,
    }


def _window_dict(
    window: WindowSpec,
    *,
    space_weather_mechanism: dict[str, Any],
    mmod_mechanism: dict[str, Any],
    duration_hours: float,
) -> dict[str, Any]:
    return {
        "window_id": window.window_id,
        "start_at": _iso(window.start_at),
        "end_at": _iso(window.end_at),
        "duration_hours": duration_hours,
        "mechanisms": [space_weather_mechanism, mmod_mechanism],
        "lighting": {"requested": False, "status": "not_requested", "note": None},
    }


def _request_dict(scenario: Scenario) -> dict[str, Any]:
    return {
        "mode": scenario.mode,
        "start_at": _iso(scenario.window_a_start),
        "duration_hours": scenario.duration_hours,
        "search_window_hours": scenario.search_window_hours,
        "as_of": _iso(scenario.as_of),
    }


def _source_status(
    *,
    orbit_record_id: str | None,
    mmod_record_ids: list[str],
    donki_record_ids: list[str],
    as_of: datetime,
) -> list[dict[str, Any]]:
    """Archival, offline "source status": this stand never makes a live
    network call, so every source here is reported as successfully read
    from its bundled/fixture file at ``as_of`` (the deterministic moment
    this scenario's forecast is evaluated at) — never frozen/quota-limited/
    erroring, since a local file read cannot fail those ways here."""
    entries = []
    for source_id, used in (
        (orbit_history.SOURCE_ID, orbit_record_id is not None),
        (mmod_source.SOURCE_ID, bool(mmod_record_ids)),
        ("nasa-donki-notifications", bool(donki_record_ids)),
    ):
        entries.append(
            {
                "source_id": source_id,
                "last_success_at": _iso(as_of) if used else None,
                "last_error_at": None,
                "last_error_message": None,
                "frozen": False,
                "quota_limited": False,
            }
        )
    return entries


def build_production_result(
    scenario: Scenario,
    *,
    conn: sqlite3.Connection,
    raw_store: RawOriginalStore,
    run_started_at: datetime,
) -> ProductionBuild:
    """Builds the production-method result for one scenario, or an honest
    structured failure when the real archival gap makes that impossible.

    ``run_started_at`` is the real moment THIS calculation run was executed
    (``result.computed_at`` — "когда расчёт был выполнен и сохранён",
    contracts/result.schema.json) — deliberately a caller-supplied,
    explicit value rather than an internal ``datetime.now()`` call (FN-46
    round 7 review, point 2): it must never be the scenario's own
    ``as_of`` (a different one of the four times, main-prompt.md §1), and
    callers that need byte-identical determinism across two runs (
    ``tests/experiments/test_determinism.py``) get it by passing the same
    explicit value twice, not by this function silently reusing ``as_of``
    as a deterministic stand-in.
    """
    windows = [
        WindowSpec("win-a", scenario.window_a_start, scenario.window_a_end),
        WindowSpec("win-b", scenario.window_b_start, scenario.window_b_end),
    ]
    interval_start = min(w.start_at for w in windows)
    interval_end = max(w.end_at for w in windows)

    releases_with_bytes = fixtures.load_all_oem_releases()
    try:
        selection = orbit.select_oem_elements_for_request(
            "historical_forecast",
            [release for release, _raw in releases_with_bytes],
            as_of=scenario.as_of,
            interval_start=interval_start,
            interval_end=interval_end,
        )
    except orbit.HistoricalElementsUnsupportedError as exc:
        failure = OrbitSelectionFailure(
            scenario=scenario, interval_start=interval_start, interval_end=interval_end, cause=exc
        )
        return ProductionBuild(result=None, failure=failure.to_dict(), evidence_by_window={})

    release = selection.release
    raw_bytes = next(raw for rel, raw in releases_with_bytes if rel is release)
    orbit_record = orbit_history.build_oem_orbital_elements_record(
        release,
        raw_bytes=raw_bytes,
        fetched_at=fixtures.oem_release_fetched_at(release.release_date),
        quality="reconstructed" if selection.is_reconstruction else "nominal",
    )
    orbit_record_id = insert_record(conn, raw_store, orbit_record)

    elements_epoch = release.parsed.creation_date
    age_hours = abs((scenario.as_of - elements_epoch).total_seconds()) / 3600.0
    orbit_block: dict[str, Any] = {
        # The actual source used here is NASA OEM (orbit_history.SOURCE_ID),
        # never Space-Track GP_HISTORY (still unimplemented, see
        # src/sources/orbit.py SOURCE_ID_HISTORICAL) — FN-46 round 7 review,
        # point 3: the hardcoded "space-track" literal previously here
        # claimed a source this build never reads from.
        "source": orbit_history.SOURCE_ID,
        "norad_id": orbit_history.ISS_NORAD_ID,
        "elements_epoch": _iso(elements_epoch),
        "elements_age_hours": age_hours,
        "coordinate_system": release.parsed.ref_frame,
        "is_reconstructed": selection.is_reconstruction,
        "record_id": orbit_record_id,
    }

    notifications = fixtures.load_donki_notifications()
    donki_record_ids = donki_evidence.ensure_donki_records(
        conn,
        raw_store,
        notifications=notifications,
        source_url=fixtures.DONKI_SOURCE_URL,
        fetched_at=fixtures.donki_fetched_at(),
    )

    warnings: list[dict[str, Any]] = []
    window_dicts: list[dict[str, Any]] = []
    evidence_by_window: dict[str, list[donki_evidence.DonkiEvidenceEntry]] = {}
    mmod_record_ids: list[str] = []
    mmod_fetched_at = fixtures.mmod_fetched_at()
    for window in windows:
        mmod_mechanism = _mmod_mechanism_assessment(
            conn, raw_store, window=window, as_of=scenario.as_of, fetched_at=mmod_fetched_at
        )
        mmod_record_ids.extend(mmod_mechanism["record_ids"])
        evidence = donki_evidence.evidence_for_window(
            conn, as_of=scenario.as_of, window_start=window.start_at, window_end=window.end_at
        )
        evidence_by_window[window.window_id] = evidence
        space_weather_mechanism = _space_weather_mechanism(evidence)
        if evidence:
            warnings.append(_evidence_warning(window.window_id, evidence))
        window_dicts.append(
            _window_dict(
                window,
                space_weather_mechanism=space_weather_mechanism,
                mmod_mechanism=mmod_mechanism,
                duration_hours=scenario.duration_hours,
            )
        )

    candidates = [
        WindowCandidate.from_assessments(wd["window_id"], wd["duration_hours"], wd["mechanisms"])
        for wd in window_dicts
    ]
    exclusions = excluded_windows(candidates)
    for wd in window_dicts:
        reason = exclusions.get(wd["window_id"])
        wd["excluded_from_comparison"] = reason is not None
        wd["exclusion_reason"] = reason
    outcome = recommend(candidates)
    recommendation = {
        "status": outcome.status,
        "window_id": outcome.window_id,
        "explanation": outcome.explanation,
    }

    data_manifest = [
        {
            "record_id": orbit_record_id,
            "source_id": orbit_history.SOURCE_ID,
            "source_version": orbit_record.source_version,
            "record_kind": "orbital_elements",
        }
    ]
    seen_manifest_ids = {orbit_record_id}
    for record_id in sorted(dict.fromkeys(mmod_record_ids)):
        if record_id in seen_manifest_ids:
            continue
        seen_manifest_ids.add(record_id)
        data_manifest.append(
            {
                "record_id": record_id,
                "source_id": mmod_source.SOURCE_ID,
                "source_version": mmod_source.SOURCE_VERSION,
                "record_kind": "forecast",
            }
        )
    used_donki_ids = sorted(
        {entry.record_id for entries in evidence_by_window.values() for entry in entries}
    )
    for record_id in used_donki_ids:
        if record_id in seen_manifest_ids:
            continue
        seen_manifest_ids.add(record_id)
        data_manifest.append(
            {
                "record_id": record_id,
                "source_id": "nasa-donki-notifications",
                # DONKI has no server-side version marker (README of
                # tests/fixtures/sources/archive) — archive_probe.py builds
                # source_version from the reported issue time, mirrored here
                # by re-deriving it the same way rather than guessing.
                "source_version": _donki_source_version(conn, record_id),
                "record_kind": "warning",
            }
        )

    limitations = [
        "space_weather (Механизм 1): status=missing_data для каждого окна — "
        "нет архивного количественного наблюдения потока протонов (pfu) в "
        "этом репозитории (только живой noaa-swpc-proton-flux, никогда не "
        "replay_eligible). Архивная evidence-проба DONKI (record_kind=warning) "
        "показана в notes/record_ids/warnings как контекст и вход метрик "
        "experiments/, не как классификация. Строгая DONKI→шкала-S "
        "классификация — предмет FN-41/FN-42 (не слиты).",
        "mmod (Механизм 2): ratio_to_background из NASA MEO 2024 LEO forecast, "
        "пороги 1.2/2 — эвристика команды поверх честного отношения "
        "(main-prompt.md §11). Геометрия станции (экранирование Землёй, "
        "относительная скорость встречи) не перемножается — тот же принцип, "
        "что и production mode=current.",
        f"orbit: NASA TOPO CCSDS OEM выпуск {orbit_block['elements_epoch']!s} "
        f"(is_reconstructed={orbit_block['is_reconstructed']}), интерполяция "
        "кубическая Эрмита между узлами; траектория участвует в отборе "
        "источника, а не в самих оценках механизмов этого стенда (MMOD-уровень "
        "не умножается на геометрию, см. выше).",
        "Этот стенд — experiments/, отдельная от src/api/service.py сборка "
        "результата теми же нижележащими доменными/source-модулями "
        "(main-prompt.md §11 «Это доказательство доступности»); "
        "algorithm_version этого стенда самостоятельный, не совпадает с "
        "src.api.schemas.ALGORITHM_VERSION.",
    ]

    result: dict[str, Any] = {
        "result_id": f"res-experiment-{scenario.name}",
        "computed_at": _iso(run_started_at),
        "request": _request_dict(scenario),
        "mode": scenario.mode,
        "as_of": _iso(scenario.as_of),
        "algorithm_version": ALGORITHM_VERSION,
        "data_manifest": data_manifest,
        "orbit": orbit_block,
        "windows": window_dicts,
        "coverage": {"requested_period_supported": True, "archive_gaps": []},
        "limitations": limitations,
        "warnings": warnings,
        "recommendation": recommendation,
        "source_status": _source_status(
            orbit_record_id=orbit_record_id,
            mmod_record_ids=mmod_record_ids,
            donki_record_ids=donki_record_ids,
            as_of=scenario.as_of,
        ),
    }
    return ProductionBuild(result=result, failure=None, evidence_by_window=evidence_by_window)


def _donki_source_version(conn: sqlite3.Connection, record_id: str) -> str:
    row = conn.execute(
        "SELECT source_version FROM source_records WHERE record_id = ?", (record_id,)
    ).fetchone()
    if row is None:  # pragma: no cover - defensive, record_id came from this same conn
        raise KeyError(f"unknown record_id: {record_id}")
    return str(row[0])


__all__ = [
    "ALGORITHM_VERSION",
    "DONKI_EVENT_MESSAGE_TYPES",
    "OrbitSelectionFailure",
    "ProductionBuild",
    "WindowSpec",
    "build_production_result",
]
