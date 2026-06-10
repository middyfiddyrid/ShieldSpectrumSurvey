"""CellMapper tower data as a baseline source — via IMPORT FILE, not scraping.

CellMapper has no official API and its terms prohibit automated scraping, so
this module reads a tower list the operator exports/captures manually from
their own logged-in CellMapper session (or any other source) into
data/cellmapper_towers.json:

    [
      {"carrier": "T-Mobile", "lat": 38.94, "lon": -94.69,
       "band": "n41", "site": "...optional..."},
      {"mcc": 310, "mnc": 260, "lat": ..., "lon": ...},
      ...
    ]

Either "carrier" or mcc/mnc identifies the operator. Towers within the
land-mobile radius are counted per carrier and mapped to carrier-owned
spectrum blocks exactly like the OpenCellID source (uplink blocks keep their
caution flag). Distance/bearing to the nearest tower per carrier is reported.
"""

import json
import os
import time
from collections import defaultdict

from ..allocations import bands_for_carrier, carrier_for_cell
from ..geocode import bearing_deg, haversine_miles
from .base import BaselineSource, SourceResult


class CellMapperImportSource(BaselineSource):
    name = "CellMapper (import)"
    cache_namespace = "cellmapper"

    def __init__(self, cache, import_path: str):
        super().__init__(cache)
        self.import_path = import_path

    def configured(self):
        if not self.import_path or not os.path.exists(self.import_path):
            return False, ("No tower import file. Export towers from your CellMapper "
                           "session into data/cellmapper_towers.json "
                           "(see survey/baseline/cellmapper.py for the format).")
        return True, ""

    def fetch(self, lat, lon, radius_mi) -> SourceResult:
        ok, why = self.configured()
        if not ok:
            return SourceResult(self.name, "not_configured", [], why)
        try:
            with open(self.import_path, "r", encoding="utf-8") as f:
                towers = json.load(f)
            rows = self._build(lat, lon, radius_mi, towers)
        except Exception as e:  # noqa: BLE001
            return SourceResult(self.name, "failed", [], f"import parse failed: {e}")
        age_days = (time.time() - os.path.getmtime(self.import_path)) / 86400
        status = "stale" if age_days > 90 else "ok"
        msg = f"import file is {age_days:.0f} days old" if status == "stale" else \
            f"{len(towers)} towers in import file"
        return SourceResult(self.name, status, rows, msg)

    def _build(self, lat, lon, radius_mi, towers: list[dict]) -> list[dict]:
        nearest: dict[str, dict] = {}
        counts = defaultdict(int)
        bands_seen = defaultdict(set)
        for t in towers:
            try:
                tlat, tlon = float(t["lat"]), float(t["lon"])
            except (KeyError, TypeError, ValueError):
                continue
            d = haversine_miles(lat, lon, tlat, tlon)
            if d > radius_mi:
                continue
            carrier = t.get("carrier") or carrier_for_cell(t.get("mcc", "?"),
                                                           t.get("mnc", "?"))
            counts[carrier] += 1
            if t.get("band"):
                bands_seen[carrier].add(str(t["band"]))
            if carrier not in nearest or d < nearest[carrier]["d"]:
                nearest[carrier] = {"d": d, "brg": bearing_deg(lat, lon, tlat, tlon)}

        rows = []
        for carrier, count in sorted(counts.items(), key=lambda kv: -kv[1]):
            blocks = bands_for_carrier(carrier)
            if not blocks:
                continue
            near = nearest[carrier]
            seen = ("; bands seen: " + ",".join(sorted(bands_seen[carrier]))
                    if bands_seen[carrier] else "")
            for band in blocks:
                rows.append({
                    "source": self.name,
                    "freq_low_mhz": band.low_mhz, "freq_high_mhz": band.high_mhz,
                    "service": "CELL", "label": band.label,
                    "callsign": "", "licensee": carrier,
                    "erp": None, "distance_mi": round(near["d"], 1),
                    "bearing_deg": round(near["brg"], 0),
                    "confidence": "block", "channel_bw_khz": band.channel_bw_khz,
                    "category": "cellular", "caution": band.caution,
                    "uplink": band.uplink,
                    "notes": (f"{count} {carrier} towers in import (nearest "
                              f"{near['d']:.1f} mi){seen}"
                              + ("; " + band.notes if band.notes else "")),
                    "raw": {"tower_count": count}})
        return rows
