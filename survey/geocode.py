"""Address geocoding: US Census Geocoder (free, no key) with OSM Nominatim fallback.
Results cached to disk so a repeat visit to the same address works offline.
"""

import math

import requests

from .baseline.base import DiskCache

CENSUS_URL = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "ShieldTSCM-SpectrumSurvey/0.1 (field TSCM survey tool; offline-cached)"


def geocode(address: str, cache: DiskCache) -> dict:
    """Returns {lat, lon, matched, geocoder} or raises RuntimeError."""
    cached = cache.get("geocode", {"address": address.strip().lower()})
    if cached is not None:
        return cached.value

    errors = []
    for fn in (_census, _nominatim):
        try:
            result = fn(address)
            if result:
                cache.put("geocode", {"address": address.strip().lower()}, result)
                return result
        except Exception as e:  # noqa: BLE001 - degrade to next geocoder
            errors.append(f"{fn.__name__}: {e}")
    raise RuntimeError("Geocoding failed (no internet and no cached result?). " + "; ".join(errors))


def _census(address: str):
    r = requests.get(CENSUS_URL, params={
        "address": address, "benchmark": "Public_AR_Current", "format": "json"},
        timeout=15, headers={"User-Agent": USER_AGENT})
    r.raise_for_status()
    matches = r.json().get("result", {}).get("addressMatches", [])
    if not matches:
        return None
    m = matches[0]
    return {"lat": m["coordinates"]["y"], "lon": m["coordinates"]["x"],
            "matched": m.get("matchedAddress", address), "geocoder": "us_census"}


def _nominatim(address: str):
    r = requests.get(NOMINATIM_URL, params={"q": address, "format": "json", "limit": 1},
                     timeout=15, headers={"User-Agent": USER_AGENT})
    r.raise_for_status()
    rows = r.json()
    if not rows:
        return None
    return {"lat": float(rows[0]["lat"]), "lon": float(rows[0]["lon"]),
            "matched": rows[0].get("display_name", address), "geocoder": "nominatim"}


def haversine_miles(lat1, lon1, lat2, lon2) -> float:
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def bearing_deg(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360
