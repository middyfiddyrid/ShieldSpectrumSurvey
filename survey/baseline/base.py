"""Disk cache + the BaselineSource interface every external query module implements."""

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class CacheEntry:
    value: object
    age_hours: float
    stale: bool


class DiskCache:
    """JSON-on-disk cache keyed by (namespace, params-hash). Entries past max_age
    are still returned but flagged stale — offline repeat visits keep working."""

    def __init__(self, directory: str, max_age_hours: float = 720):
        self.dir = directory
        self.max_age_hours = max_age_hours
        os.makedirs(directory, exist_ok=True)

    def _path(self, namespace: str, params: dict) -> str:
        key = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:24]
        return os.path.join(self.dir, f"{namespace}_{key}.json")

    def get(self, namespace: str, params: dict) -> Optional[CacheEntry]:
        path = self._path(namespace, params)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                blob = json.load(f)
            age_h = (time.time() - blob["ts"]) / 3600.0
            return CacheEntry(blob["value"], age_h, age_h > self.max_age_hours)
        except Exception:  # noqa: BLE001 - corrupt cache entry is a miss
            return None

    def put(self, namespace: str, params: dict, value):
        path = self._path(namespace, params)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "value": value}, f)
        os.replace(tmp, path)


@dataclass
class SourceResult:
    source: str
    status: str            # ok | cached | stale | failed | not_configured | degraded
    rows: list = field(default_factory=list)   # normalized baseline row dicts
    message: str = ""

    @property
    def count(self):
        return len(self.rows)


class BaselineSource:
    """Interface: fetch(lat, lon, radius_mi) -> SourceResult.

    Subclasses implement _query(); fetch() wraps it with cache + degradation:
      - fresh network result -> cached to disk, status 'ok'
      - network failure with cache -> status 'cached' or 'stale'
      - network failure, no cache  -> status 'failed' with the error message
    """

    name = "base"
    cache_namespace = "base"

    def __init__(self, cache: DiskCache):
        self.cache = cache

    def configured(self) -> tuple[bool, str]:
        return True, ""

    def _query(self, lat: float, lon: float, radius_mi: float) -> list[dict]:
        raise NotImplementedError

    def fetch(self, lat: float, lon: float, radius_mi: float) -> SourceResult:
        ok, why = self.configured()
        if not ok:
            return SourceResult(self.name, "not_configured", [], why)
        params = {"lat": round(lat, 4), "lon": round(lon, 4), "r": radius_mi}
        try:
            rows = self._query(lat, lon, radius_mi)
            self.cache.put(self.cache_namespace, params, rows)
            return SourceResult(self.name, "ok", rows)
        except Exception as e:  # noqa: BLE001 - degrade to cache
            cached = self.cache.get(self.cache_namespace, params)
            if cached is not None:
                status = "stale" if cached.stale else "cached"
                return SourceResult(self.name, status, cached.value,
                                    f"live query failed ({e}); using {cached.age_hours:.0f}h-old cache")
            return SourceResult(self.name, "failed", [], str(e))
