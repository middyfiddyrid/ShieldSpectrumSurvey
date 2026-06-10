"""Runs every baseline source in parallel, normalizes results into the session
baseline table, and records per-source status for the UI (counts, failures,
staleness). If every networked source fails, the baseline degrades to the
static allocation table only and is labeled accordingly."""

import re
from concurrent.futures import ThreadPoolExecutor

from .base import DiskCache
from .fcc_broadcast import FccAmSource, FccFmSource, FccTvSource
from .fcc_uls import FccUlsSource
from .opencellid import OpenCellIdSource
from .radioreference import RadioReferenceSource
from .static_source import StaticAllocationSource


def _zip_from_address(address: str) -> str:
    m = re.search(r"\b(\d{5})(?:-\d{4})?\b", address or "")
    return m.group(1) if m else ""


def build_sources(cfg, cache: DiskCache, address: str):
    return [
        StaticAllocationSource(cache),
        FccFmSource(cache),
        FccAmSource(cache),
        FccTvSource(cache),
        FccUlsSource(cache, cfg.get("uls", "extract_dir", default=""),
                     cfg.get("uls", "stale_days", default=30)),
        OpenCellIdSource(cache, cfg.get("api_keys", "opencellid", default="")),
        RadioReferenceSource(cache,
                             cfg.get("api_keys", "radioreference_user", default=""),
                             cfg.get("api_keys", "radioreference_pass", default=""),
                             _zip_from_address(address)),
    ]


# sources that use the broadcast radius vs the land-mobile radius
BROADCAST_SOURCES = {"FCC FM", "FCC AM", "FCC TV", "Static allocations"}


def build_baseline(db, cfg, cache: DiskCache, session: dict, progress_cb=None) -> dict:
    """Blocking build; call from a worker thread. Returns summary dict."""
    lat, lon = session["lat"], session["lon"]
    r_bcast = session["radius_broadcast_mi"]
    r_lmr = session["radius_lmr_mi"]
    sources = build_sources(cfg, cache, session["address"])

    def run(src):
        radius = r_bcast if src.name in BROADCAST_SOURCES else r_lmr
        result = src.fetch(lat, lon, radius)
        if progress_cb:
            progress_cb(src.name, result.status, result.count)
        return result

    with ThreadPoolExecutor(max_workers=len(sources)) as ex:
        results = list(ex.map(run, sources))

    db.clear_baseline(session["id"])
    total = 0
    networked_ok = False
    for res in results:
        db.set_source_status(session["id"], res.source, res.status, res.count, res.message)
        if res.rows:
            db.add_baseline_rows(session["id"], res.rows)
            total += res.count
        if res.source != "Static allocations" and res.status in ("ok", "cached", "stale"):
            networked_ok = True

    degraded = not networked_ok
    return {
        "total": total,
        "degraded": degraded,
        "label": ("DEGRADED BASELINE — static allocation table only; no licensed-record "
                  "verification possible" if degraded else "baseline complete"),
        "sources": [{"source": r.source, "status": r.status, "count": r.count,
                     "message": r.message} for r in results],
    }
