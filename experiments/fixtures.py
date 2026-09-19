"""Offline fixture loading for the FN-43 experiment stand.

Reads real, already-committed archival fixtures directly from
``tests/fixtures/...`` by path instead of duplicating the same large,
checksummed files a second time under ``experiments/`` — main-prompt.md §6
"не строй инфраструктуру там, где её не нужно": these bytes are already
reviewed, checksummed against their ``*.meta.json`` provenance and covered
by their own README in ``tests/fixtures/...``; copying them would only
create a second, driftable copy that could silently diverge from the
original. The NASA MEO (MMOD) document is not re-read here at all — it is
already bundled for the running service under
``src/sources/data/mmod/...`` and reused as-is via
``src.sources.mmod.fetch()``.

Everything this module touches is a local file already inside the repo
checkout — no network call happens anywhere in this stand.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from src.sources import archive_probe, orbit_history

UTC = timezone.utc

REPO_ROOT = Path(__file__).resolve().parents[1]
ARCHIVE_DIR = REPO_ROOT / "tests" / "fixtures" / "sources" / "archive"
OEM_DIR = REPO_ROOT / "tests" / "fixtures" / "orbit" / "history"
MMOD_META_PATH = (
    REPO_ROOT
    / "src"
    / "sources"
    / "data"
    / "mmod"
    / "nasa_meo_leo_forecast_2024"
    / "ntrs-citation-20230015158.meta.json"
)

#: The four real DONKI notification pages covering the whole mandatory
#: period 2024-05-01..2024-06-30 (tests/fixtures/sources/archive/README.md).
DONKI_FILES: tuple[str, ...] = (
    "donki_2024-05-01_2024-05-15.json",
    "donki_2024-05-16_2024-05-31.json",
    "donki_2024-06-01_2024-06-15.json",
    "donki_2024-06-16_2024-06-30.json",
)

#: The real query this archive was fetched with
#: (tests/fixtures/sources/archive/README.md) — used as ``source_url`` for
#: records built from these bytes, same convention as
#: ``src/sources/archive_probe.py``'s own callers.
DONKI_SOURCE_URL = (
    "https://api.nasa.gov/DONKI/notifications?startDate=2024-05-01&"
    "endDate=2024-06-30&type=all&api_key=DEMO_KEY"
)

#: The four real OEM releases covering the whole mandatory period
#: (tests/fixtures/orbit/history/README.md).
OEM_RELEASE_DATES: tuple[str, ...] = (
    "2024-05-08",
    "2024-05-12",
    "2024-06-14",
    "2024-06-18",
)


def load_donki_notifications() -> list[archive_probe.DonkiNotification]:
    """Parses all four real, bundled DONKI notification pages.

    Returns every notification the archive contains for the mandatory
    period, in file order — callers (experiments/production.py,
    experiments/baseline.py) apply their own ``as_of``/overlap filtering;
    this function does not filter anything itself (src/8 "получение не
    считает физику").
    """
    notifications: list[archive_probe.DonkiNotification] = []
    for filename in DONKI_FILES:
        raw = (ARCHIVE_DIR / filename).read_bytes()
        notifications.extend(archive_probe.parse_donki_notifications(raw))
    return notifications


def load_oem_release(release_date: str) -> tuple[orbit_history.OemRelease, bytes]:
    """Loads and parses one real, bundled OEM release + its S3 listing.

    Mirrors ``tests/orbit/test_orbit_history.py::_load_release`` — same
    real fixture files, same construction path
    (``orbit_history.parse_oem``/``parse_s3_listing``/``build_oem_release``).
    Returns the parsed release alongside its raw bytes, since
    ``build_oem_orbital_elements_record`` needs the original bytes for the
    stored record's checksum.
    """
    txt_path = OEM_DIR / f"nasa_iss_oem_{release_date}.txt"
    listing_path = OEM_DIR / f"nasa_iss_oem_listing_{release_date}.xml"
    raw_bytes = txt_path.read_bytes()
    parsed = orbit_history.parse_oem(raw_bytes)
    listing_entries = orbit_history.parse_s3_listing(listing_path.read_bytes())
    release = orbit_history.build_oem_release(
        parsed,
        listing_entries=listing_entries,
        release_date=release_date,
        source_url=orbit_history.OEM_URL_TEMPLATE.format(release_date=release_date),
    )
    return release, raw_bytes


def load_all_oem_releases() -> list[tuple[orbit_history.OemRelease, bytes]]:
    """Loads all four real, bundled OEM releases (see :data:`OEM_RELEASE_DATES`)."""
    return [load_oem_release(release_date) for release_date in OEM_RELEASE_DATES]


def _read_meta_timestamp(meta_path: Path, *, key: str) -> datetime:
    """Reads and parses one ISO 8601 timestamp field from a ``*.meta.json``
    provenance sidecar (FN-46 round 7 review, point 2) — the REAL moment
    this repository's bundled copy of a source document was retrieved, as
    recorded when the fixture was captured (``tests/fixtures/.../README.md``
    "около 2026-09-18..19"), never a scenario's ``as_of`` (main-prompt.md
    §1: ``fetched_at`` is "when WE received it", not a stand-in for a
    different one of the four times)."""
    raw = str(json.loads(meta_path.read_text(encoding="utf-8"))[key])
    text = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    return datetime.fromisoformat(text).astimezone(UTC)


def donki_fetched_at() -> datetime:
    """The real ``fetched_at`` of the LAST of the four DONKI archive pages
    (all four retrieved within the same ~16s window,
    ``tests/fixtures/sources/archive/*.meta.json``) — by that moment every
    notification this stand reads had genuinely been retrieved."""
    return max(
        _read_meta_timestamp(
            ARCHIVE_DIR / f"{filename.removesuffix('.json')}.meta.json", key="fetched_at"
        )
        for filename in DONKI_FILES
    )


def oem_release_fetched_at(release_date: str) -> datetime:
    """The real ``fetched_at`` of one specific OEM release's ``.txt`` object
    (``tests/fixtures/orbit/history/nasa_iss_oem_<release_date>.meta.json``)."""
    return _read_meta_timestamp(
        OEM_DIR / f"nasa_iss_oem_{release_date}.meta.json", key="fetched_at"
    )


def mmod_fetched_at() -> datetime:
    """The real ``captured_at`` of the NTRS metadata sidecar backing the
    bundled NASA MEO document (``src/sources/mmod.py``
    ``PUBLICATION_EVIDENCE_PATH``) — this document has no live per-run
    fetch (it is a gate-scope bundled file, see that module's docstring),
    so the moment its provenance sidecar was captured and checksummed is
    the closest honest analogue to ``fetched_at`` for it."""
    return _read_meta_timestamp(MMOD_META_PATH, key="captured_at")


__all__ = [
    "ARCHIVE_DIR",
    "DONKI_FILES",
    "DONKI_SOURCE_URL",
    "MMOD_META_PATH",
    "OEM_DIR",
    "OEM_RELEASE_DATES",
    "donki_fetched_at",
    "load_all_oem_releases",
    "load_donki_notifications",
    "load_oem_release",
    "mmod_fetched_at",
    "oem_release_fetched_at",
]
