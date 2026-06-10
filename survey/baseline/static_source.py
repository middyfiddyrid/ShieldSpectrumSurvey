"""Static national allocation table as a baseline source (works with zero
internet) + airport-proximity check that gates the aviation-band entries."""

import csv
import io
import os

import requests

from ..allocations import CELLULAR_BANDS, STATIC_ALLOCATIONS
from ..geocode import haversine_miles
from .base import BaselineSource, DiskCache, SourceResult

OURAIRPORTS_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"
AIRPORT_RADIUS_MI = 10.0


def airports_within(lat, lon, radius_mi, cache: DiskCache):
    """Returns (list_of_airport_names, status). Downloads/caches the OurAirports
    dataset (US large+medium airports only, slimmed before caching).
    status: ok | cached | unknown (no internet, no cache)."""
    cached = cache.get("airports_us", {"v": 1})
    airports, status = None, "ok"
    if cached is not None and not cached.stale:
        airports, status = cached.value, "cached"
    else:
        try:
            r = requests.get(OURAIRPORTS_URL, timeout=60,
                             headers={"User-Agent": "ShieldTSCM-SpectrumSurvey/0.1"})
            r.raise_for_status()
            airports = []
            for row in csv.DictReader(io.StringIO(r.text)):
                if row.get("iso_country") == "US" and \
                        row.get("type") in ("large_airport", "medium_airport"):
                    try:
                        airports.append({"name": row["name"],
                                         "lat": float(row["latitude_deg"]),
                                         "lon": float(row["longitude_deg"])})
                    except (ValueError, KeyError):
                        continue
            cache.put("airports_us", {"v": 1}, airports)
        except Exception:  # noqa: BLE001 - degrade to stale cache or unknown
            if cached is not None:
                airports, status = cached.value, "cached"
            else:
                return [], "unknown"
    hits = [a["name"] for a in airports
            if haversine_miles(lat, lon, a["lat"], a["lon"]) <= radius_mi]
    return hits, status


class StaticAllocationSource(BaselineSource):
    name = "Static allocations"
    cache_namespace = "static_alloc"

    def fetch(self, lat, lon, radius_mi) -> SourceResult:
        nearby_airports, ap_status = airports_within(lat, lon, AIRPORT_RADIUS_MI, self.cache)
        rows = []
        # cellular blocks belong in the allocation baseline too: a hit inside one is
        # PLAUSIBLE even without OpenCellID; OpenCellID upgrades downlinks to VERIFIED
        for a in STATIC_ALLOCATIONS + CELLULAR_BANDS:
            notes = a.notes
            if a.conditional == "airport":
                if ap_status == "unknown":
                    notes = (notes + "; " if notes else "") + \
                        "airport proximity unknown (offline, no cache)"
                elif not nearby_airports:
                    continue  # no airport within 10 mi -> aviation bands not expected
                else:
                    notes = (notes + "; " if notes else "") + \
                        f"airport within {AIRPORT_RADIUS_MI:.0f} mi: {nearby_airports[0]}"
            rows.append({"source": self.name,
                         "freq_low_mhz": a.low_mhz, "freq_high_mhz": a.high_mhz,
                         "service": a.service, "label": a.label,
                         "callsign": "", "licensee": "", "erp": None,
                         "distance_mi": None, "bearing_deg": None,
                         "confidence": "allocation", "channel_bw_khz": a.channel_bw_khz,
                         "category": a.category, "caution": a.caution,
                         "uplink": a.uplink, "notes": notes, "raw": {}})
        msg = ""
        if ap_status == "unknown":
            msg = "airport list unavailable — aviation bands included unconditionally"
        elif nearby_airports:
            msg = f"aviation bands active ({len(nearby_airports)} airport(s) within 10 mi)"
        return SourceResult(self.name, "ok", rows, msg)
