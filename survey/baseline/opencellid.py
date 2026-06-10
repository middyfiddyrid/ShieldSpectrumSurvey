"""OpenCellID — nearby cell towers by MCC/MNC, mapped to carrier-owned spectrum
blocks via the static US carrier band table. (Replaces any CellMapper scraping —
no official API there and TOS-fragile.)"""

import math
from collections import Counter

import requests

from ..allocations import bands_for_carrier, carrier_for_cell
from .base import BaselineSource

URL = "https://opencellid.org/cell/getInArea"
TIMEOUT = 30


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

    def _query(self, lat, lon, radius_mi):
        dlat = radius_mi / 69.0
        dlon = radius_mi / (69.0 * max(math.cos(math.radians(lat)), 0.05))
        bbox = f"{lat - dlat},{lon - dlon},{lat + dlat},{lon + dlon}"
        r = requests.get(URL, params={"key": self.api_key, "BBOX": bbox,
                                      "format": "json", "limit": 1000},
                         timeout=TIMEOUT)
        r.raise_for_status()
        payload = r.json()
        cells = payload.get("cells", payload if isinstance(payload, list) else [])

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
                              + ("; " + band.notes if band.notes else "")),
                    "raw": {"tower_count": count}})
        return rows
