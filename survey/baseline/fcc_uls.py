"""FCC ULS land-mobile / microwave licenses near the coordinates.

FCC's ULS web search has no stable public API and the advanced-search endpoint
breaks regularly, so this module works from the official ULS weekly database
extracts instead (deterministic, offline-friendly). Point config uls.extract_dir
at an unzipped extract (e.g. l_LMpriv.zip -> HD.dat/EN.dat/FR.dat/LO.dat from
https://www.fcc.gov/uls/transactions/daily-weekly). Multiple extracts can be
unzipped into the same directory tree; all *.dat sets found are parsed.

Status reporting:
  - not_configured  -> uls.extract_dir blank (UI shows download instructions)
  - stale           -> extract files older than uls.stale_days
  - ok              -> parsed, filtered by distance

The module is swappable: anything that returns normalized rows can replace it.
"""

import os
import time
from collections import defaultdict

from ..geocode import bearing_deg, haversine_miles
from .base import BaselineSource, SourceResult

# ULS pipe-delimited record layouts (public access database definitions)
LO_LAT_D, LO_LAT_M, LO_LAT_S, LO_LAT_DIR = 19, 20, 21, 22
LO_LON_D, LO_LON_M, LO_LON_S, LO_LON_DIR = 23, 24, 25, 26
FR_FREQ = 10
EN_NAME = 7
HD_STATUS, HD_SERVICE = 5, 6

SERVICE_NAMES = {
    "IG": "Industrial/Business", "PW": "Public Safety", "YG": "Industrial/Business (trunked)",
    "GB": "Business", "GO": "Other Govt", "MW": "Microwave", "CF": "Common Carrier Fixed",
    "MG": "Microwave (industrial)", "YW": "Microwave (trunked)", "AF": "Aeronautical Fixed",
    "BA": "Broadcast Auxiliary", "BB": "Broadcast Auxiliary", "BL": "Broadcast Auxiliary (low power)",
    "RP": "Broadcast Auxiliary (remote pickup)", "CW": "Paging", "PC": "Paging (common carrier)",
}


def _walk_dat(extract_dir: str, prefix: str):
    for root, _dirs, files in os.walk(extract_dir):
        for fn in files:
            if fn.upper() == f"{prefix}.DAT":
                yield os.path.join(root, fn)


class FccUlsSource(BaselineSource):
    name = "FCC ULS"
    cache_namespace = "fcc_uls"

    def __init__(self, cache, extract_dir: str, stale_days: int = 30):
        super().__init__(cache)
        self.extract_dir = extract_dir
        self.stale_days = stale_days

    def configured(self):
        if not self.extract_dir:
            return False, ("No ULS extract configured. Download a weekly Land Mobile extract "
                           "(l_LMpriv.zip) from fcc.gov/uls/transactions/daily-weekly, unzip it, "
                           "and set uls.extract_dir in config.yaml.")
        if not os.path.isdir(self.extract_dir):
            return False, f"uls.extract_dir does not exist: {self.extract_dir}"
        return True, ""

    def fetch(self, lat, lon, radius_mi) -> SourceResult:
        ok, why = self.configured()
        if not ok:
            return SourceResult(self.name, "not_configured", [], why)
        try:
            rows = self._query(lat, lon, radius_mi)
        except Exception as e:  # noqa: BLE001
            return SourceResult(self.name, "failed", [], f"extract parse failed: {e}")
        # staleness from the extract files' own modification time
        newest = max((os.path.getmtime(p) for p in _walk_dat(self.extract_dir, "FR")),
                     default=0)
        age_days = (time.time() - newest) / 86400 if newest else 9999
        if age_days > self.stale_days:
            return SourceResult(self.name, "stale", rows,
                                f"extract is {age_days:.0f} days old — refresh from FCC")
        return SourceResult(self.name, "ok", rows)

    def _query(self, lat, lon, radius_mi):
        def parse(line):
            return line.rstrip("\n").split("|")

        # Pass 1: locations within radius -> {usi: (dist, bearing)}
        nearby = {}
        for path in _walk_dat(self.extract_dir, "LO"):
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    p = parse(line)
                    if len(p) <= LO_LON_DIR or p[0] != "LO":
                        continue
                    try:
                        slat = (float(p[LO_LAT_D]) + float(p[LO_LAT_M]) / 60
                                + float(p[LO_LAT_S]) / 3600)
                        if p[LO_LAT_DIR] == "S":
                            slat = -slat
                        slon = (float(p[LO_LON_D]) + float(p[LO_LON_M]) / 60
                                + float(p[LO_LON_S]) / 3600)
                        if p[LO_LON_DIR] == "W":
                            slon = -slon
                    except (ValueError, IndexError):
                        continue
                    d = haversine_miles(lat, lon, slat, slon)
                    if d <= radius_mi:
                        usi = p[1]
                        if usi not in nearby or d < nearby[usi][0]:
                            nearby[usi] = (d, bearing_deg(lat, lon, slat, slon))
        if not nearby:
            return []

        # Pass 2: active license headers + service codes
        active = {}
        for path in _walk_dat(self.extract_dir, "HD"):
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    p = parse(line)
                    if len(p) > HD_SERVICE and p[0] == "HD" and p[1] in nearby \
                            and p[HD_STATUS] == "A":
                        active[p[1]] = (p[4], p[HD_SERVICE])   # callsign, service code

        # Pass 3: licensee names
        names = {}
        for path in _walk_dat(self.extract_dir, "EN"):
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    p = parse(line)
                    if len(p) > EN_NAME and p[0] == "EN" and p[1] in active:
                        names.setdefault(p[1], p[EN_NAME])

        # Pass 4: assigned frequencies
        freqs = defaultdict(set)
        for path in _walk_dat(self.extract_dir, "FR"):
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    p = parse(line)
                    if len(p) > FR_FREQ and p[0] == "FR" and p[1] in active:
                        try:
                            f_mhz = float(p[FR_FREQ])
                        except ValueError:
                            continue
                        if 0.05 <= f_mhz <= 7500:
                            freqs[p[1]].add(round(f_mhz, 5))

        rows = []
        for usi, fset in freqs.items():
            callsign, svc = active[usi]
            dist, brg = nearby[usi]
            for f_mhz in sorted(fset):
                rows.append({
                    "source": self.name, "freq_low_mhz": f_mhz, "freq_high_mhz": f_mhz,
                    "service": "LMR", "label": SERVICE_NAMES.get(svc, f"ULS {svc}"),
                    "callsign": callsign, "licensee": names.get(usi, ""),
                    "erp": None, "distance_mi": round(dist, 1),
                    "bearing_deg": round(brg, 0), "confidence": "record",
                    "channel_bw_khz": 12.5, "category": "land_mobile",
                    "raw": {"usi": usi, "service_code": svc}})
        return rows
