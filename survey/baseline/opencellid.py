"""OpenCellID — nearby cell towers by MCC/MNC, mapped to carrier-owned spectrum
blocks via the static US carrier band table. (Replaces any CellMapper scraping —
no official API there and TOS-fragile.)

OpenCellID's getInArea endpoint hard-caps each query's bounding box at
4,000,000 m^2 (~2 km x 2 km), so a survey radius is covered by TILING the area
into sub-boxes under that cap and de-duplicating the cells returned.
"""

import math
import time
from collections import Counter

import requests

from ..allocations import bands_for_carrier, carrier_for_cell
from .base import BaselineSource

URL = "https://opencellid.org/cell/getInArea"
TIMEOUT = 30
OCID_MAX_AREA_M2 = 4_000_000      # OpenCellID per-query bounding-box cap
TILE_M = 1800.0                   # tile side (~3.24e6 m^2, safely under the cap)
RADIUS_CAP_MI = 3.0               # central area actually queried (bounds request count)
MAX_TILES = 49                    # request ceiling per baseline build


class OpenCellIdSource(BaselineSource):
    name = "OpenCellID"
    cache_namespace = "opencellid"

    def __init__(self, cache, api_key: str):
        super().__init__(cache)
        self.api_key = api_key

    def configured(self):
        if not self.api_key:
            return False, "No OpenCellID API key (free at opencellid.org) — set api_keys.opencellid"
        return True, ""

    def _tile_bboxes(self, lat, lon, radius_mi):
        """Grid of bboxes (each <= OpenCellID's area cap) covering the radius box."""
        lat_half = radius_mi / 69.0
        coslat = max(math.cos(math.radians(lat)), 0.05)
        lon_half = radius_mi / (69.0 * coslat)
        lat_step = TILE_M / 111_320.0
        lon_step = TILE_M / (111_320.0 * coslat)
        n_lat = max(1, math.ceil(2 * lat_half / lat_step))
        n_lon = max(1, math.ceil(2 * lon_half / lon_step))
        boxes = []
        for i in range(n_lat):
            a0 = (lat - lat_half) + i * lat_step
            a1 = min(a0 + lat_step, lat + lat_half)
            for j in range(n_lon):
                o0 = (lon - lon_half) + j * lon_step
                o1 = min(o0 + lon_step, lon + lon_half)
                boxes.append((a0, o0, a1, o1))
                if len(boxes) >= MAX_TILES:
                    return boxes
        return boxes

    def _fetch_tiles(self, lat, lon, radius_mi):
        """Query each tile; aggregate unique cells. Per-tile failures are skipped
        so one bad/rate-limited tile doesn't sink the whole baseline."""
        seen, cells = set(), []
        for (a0, o0, a1, o1) in self._tile_bboxes(lat, lon, radius_mi):
            try:
                r = requests.get(URL, params={"key": self.api_key,
                                              "BBOX": f"{a0},{o0},{a1},{o1}",
                                              "format": "json", "limit": 1000},
                                 timeout=TIMEOUT)
                r.raise_for_status()
                payload = r.json()
            except Exception:  # noqa: BLE001 - skip this tile, keep going
                continue
            if isinstance(payload, dict) and payload.get("error"):
                continue
            tile_cells = (payload.get("cells", []) if isinstance(payload, dict)
                          else payload if isinstance(payload, list) else [])
            for c in tile_cells:
                key = (c.get("mcc"), c.get("mnc"), c.get("lac"),
                       c.get("cellid", c.get("cid")))
                if key in seen:
                    continue
                seen.add(key)
                cells.append(c)
            time.sleep(0.1)
        return cells

    def _query(self, lat, lon, radius_mi):
        eff_radius = min(radius_mi, RADIUS_CAP_MI)
        cells = self._fetch_tiles(lat, lon, eff_radius)
        coverage_note = ("" if radius_mi <= RADIUS_CAP_MI else
                         f" (OpenCellID area limit: towers sampled within "
                         f"~{RADIUS_CAP_MI:.0f} mi of the address)")

        carriers = Counter()
        radios = {}
        for c in cells:
            carrier = carrier_for_cell(c.get("mcc"), c.get("mnc"))
            carriers[carrier] += 1
            radios.setdefault(carrier, set()).add(str(c.get("radio", "?")))

        rows = []
        for carrier, count in carriers.most_common():
            bands = bands_for_carrier(carrier)
            if not bands:
                continue
            for band in bands:
                rows.append({
                    "source": self.name,
                    "freq_low_mhz": band.low_mhz, "freq_high_mhz": band.high_mhz,
                    "service": "CELL", "label": band.label,
                    "callsign": "", "licensee": carrier,
                    "erp": None, "distance_mi": None, "bearing_deg": None,
                    "confidence": "block", "channel_bw_khz": band.channel_bw_khz,
                    "category": "cellular", "caution": band.caution, "uplink": band.uplink,
                    "notes": (f"{count} {carrier} towers observed nearby "
                              f"({'/'.join(sorted(radios[carrier]))})"
                              + ("; " + band.notes if band.notes else "") + coverage_note),
                    "raw": {"tower_count": count}})
        return rows
