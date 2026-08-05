"""Shodan exposure lookup — the outside-in complement to netscan.py.

netscan.py enumerates hosts on the client's OWN LAN (inside the perimeter).
This module asks the opposite question: what devices at (or geolocated near)
the client's address are already indexed as reachable from the public internet?
An exposed IP camera / DVR / NAS is a finding a client understands instantly,
and it is the kind of leak a physical bug sweep alone would never surface.

WHERE THIS FITS
  - INTERNAL ONLY. Like /api/netscan and /api/report/internal_memo, exposure
    findings feed the confidential pre-sweep memo, never the client-facing
    spectral report. They are NOT spectrum baseline rows (a Shodan host has no
    frequency), so this is deliberately NOT a baseline.BaselineSource and is not
    registered in baseline/manager.py — that would pollute the expected-spectrum
    table the correlation engine matches against.
  - Requires a PAID Shodan plan (the geo: search filter is not on the free tier).
    Commercial use is permitted WITH attribution to Shodan — ATTRIBUTION below is
    surfaced so any memo that cites this data can carry it.

HONEST LIMITS (state these in any memo that uses the output)
  - Shodan geolocates hosts by IP, which is coarse — often city/ISP-level, not
    rooftop. A small radius around a residential address commonly returns few or
    zero hosts, and hosts that DO return may belong to neighbors or the ISP, not
    the client. Treat every hit as a LEAD to confirm (whose IP is it?), never as
    proof a device is the client's.
  - Results are Shodan's last-scan snapshot, not live — a host may be stale.
  - PII: hosts belong to third parties. Same guardrail as POI/background data —
    internal memo only, excluded from client deliverables, minimized to what the
    sweep actually needs.
"""

import math
from dataclasses import dataclass, field

import requests

SEARCH_URL = "https://api.shodan.io/shodan/host/search"
TIMEOUT = 30
RADIUS_CAP_MI = 2.0          # keep the geo box local; IP geoloc past this is noise
MAX_RESULTS = 25             # cap credit burn per query
ATTRIBUTION = "Exposure data: Shodan (shodan.io)"

# product/banner keyword -> device class, ordered most-specific first. The TSCM
# angle is exposed cameras/DVRs/NAS, so those lead.
_CLASS_KEYWORDS = [
    ("camera", ["camera", "webcam", "ipcam", "hikvision", "dahua", "reolink",
                "axis", "nvr", "rtsp", "mjpeg", "onvif"]),
    ("dvr", ["dvr", "xmeye", "dvrdvs"]),
    ("nas", ["nas", "synology", "qnap", "diskstation", "truenas", "freenas"]),
    ("router", ["router", "mikrotik", "openwrt", "dd-wrt", "gpon", "draytek",
                "asuswrt", "tp-link", "routeros"]),
    ("printer", ["printer", "jetdirect", "cups", "ipp"]),
    ("iot", ["iot", "smart", "thermostat", "doorbell", "sonos", "tuya",
             "espressif", "shelly"]),
]


@dataclass
class ExposureResult:
    status: str                                  # ok | not_configured | denied | failed | empty
    hosts: list = field(default_factory=list)
    message: str = ""
    attribution: str = ATTRIBUTION

    @property
    def count(self):
        return len(self.hosts)


def classify_exposed(product: str, data: str, tags) -> str:
    """Best-effort device class from the banner. 'unknown' when nothing matches —
    we do NOT guess, mirroring the identify.py 'noise_floor / no signal' honesty."""
    hay = " ".join(x for x in (product or "", data or "",
                               " ".join(tags or [])) if x).lower()
    for label, needles in _CLASS_KEYWORDS:
        if any(n in hay for n in needles):
            return label
    return "unknown"


class ShodanExposure:
    """Query Shodan for internet-exposed hosts near a lat/lon. Mirrors the
    baseline sources' cache + graceful-degrade contract without being one:
    a live failure with a warm cache still returns the cached snapshot."""

    name = "Shodan exposure"
    cache_namespace = "shodan"

    def __init__(self, cache, api_key: str):
        self.cache = cache
        self.api_key = api_key or ""

    def configured(self) -> tuple[bool, str]:
        if not self.api_key:
            return False, ("No Shodan API key — paid plan required for geo search; "
                           "set api_keys.shodan")
        return True, ""

    @staticmethod
    def _radius_km(radius_mi: float) -> float:
        return round(min(radius_mi, RADIUS_CAP_MI) * 1.609344, 3)

    def _normalize(self, match: dict) -> dict:
        loc = match.get("location", {}) or {}
        data = match.get("data", "") or ""
        product = match.get("product", "") or ""
        tags = match.get("tags", []) or []
        return {
            "ip": match.get("ip_str", ""),
            "port": match.get("port"),
            "transport": match.get("transport", ""),
            "product": product,
            "device_class": classify_exposed(product, data, tags),
            "org": match.get("org", "") or match.get("isp", "") or "",
            "os": match.get("os", "") or "",
            "hostnames": match.get("hostnames", []) or [],
            "lat": loc.get("latitude"),
            "lon": loc.get("longitude"),
            "city": loc.get("city", "") or "",
            "tags": tags,
            "timestamp": match.get("timestamp", ""),
            "banner": data[:200],       # trimmed; full banners are noisy + PII-heavy
        }

    def _query(self, lat: float, lon: float, radius_mi: float) -> list[dict]:
        query = f"geo:{lat:.5f},{lon:.5f},{self._radius_km(radius_mi)}"
        r = requests.get(SEARCH_URL,
                         params={"key": self.api_key, "query": query,
                                 "minify": "true"},
                         timeout=TIMEOUT)
        if r.status_code in (401, 403):
            # invalid key, or plan without the search-filter entitlement
            raise PermissionError(
                f"Shodan denied the request ({r.status_code}) — check the key and "
                f"that the plan allows geo search")
        r.raise_for_status()
        payload = r.json()
        matches = payload.get("matches", []) if isinstance(payload, dict) else []
        hosts, seen = [], set()
        for m in matches[:MAX_RESULTS]:
            h = self._normalize(m)
            key = (h["ip"], h["port"])
            if key in seen:
                continue
            seen.add(key)
            hosts.append(h)
        # cameras/DVRs first, then everything else, loudest-signal analog = most ports
        hosts.sort(key=lambda h: (h["device_class"] in ("unknown",), h["ip"]))
        return hosts

    def fetch(self, lat: float, lon: float, radius_mi: float) -> ExposureResult:
        ok, why = self.configured()
        if not ok:
            return ExposureResult("not_configured", [], why)
        params = {"lat": round(lat, 4), "lon": round(lon, 4),
                  "r": round(min(radius_mi, RADIUS_CAP_MI), 3)}
        try:
            hosts = self._query(lat, lon, radius_mi)
            self.cache.put(self.cache_namespace, params, hosts)
            if not hosts:
                return ExposureResult("empty", [],
                                      "no internet-exposed hosts indexed near this address")
            n_cam = sum(1 for h in hosts if h["device_class"] in ("camera", "dvr"))
            msg = f"{len(hosts)} exposed host(s) indexed nearby"
            if n_cam:
                msg += f"; {n_cam} look like camera/DVR — confirm ownership before reporting"
            return ExposureResult("ok", hosts, msg)
        except PermissionError as e:
            return ExposureResult("denied", [], str(e))
        except Exception as e:  # noqa: BLE001 - degrade to cache like the baseline sources
            cached = self.cache.get(self.cache_namespace, params)
            if cached is not None:
                age = "stale" if cached.stale else "cached"
                return ExposureResult("ok", cached.value,
                                      f"live query failed ({e}); using {age} snapshot")
            return ExposureResult("failed", [], str(e))
