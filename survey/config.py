"""YAML config loading with safe defaults and write-back (used by calibration mode)."""

import copy
import os
import threading

import yaml

_LOCK = threading.Lock()

DEFAULTS = {
    "app": {"host": "127.0.0.1", "port": 8765, "open_browser": True},
    "branding": {"company": "Shield TSCM", "logo_path": "", "operator": ""},
    "radius": {"broadcast_miles": 25, "land_mobile_miles": 5},
    "api_keys": {"opencellid": "", "radioreference_user": "", "radioreference_pass": ""},
    "uls": {"extract_dir": "", "stale_days": 30},
    "detection": {
        "threshold_db": 12.0,
        "merge_gap_bins": 2,
        "min_persistence": 0.05,
        "interior_delta_flag_db": 10.0,
        "room_delta_flag_db": 12.0,
    },
    "sweep": {
        "sweep_points": 112,
        "seconds_per_sweep": 0.35,
        "segment_width_mhz": {"subghz": 75, "mid": 100, "high": 200},
        "profiles": {
            "quick": {"minutes": 15, "avg_passes": 5, "span_scale": 2.0},
            "standard": {"minutes": 45, "avg_passes": 10, "span_scale": 1.0},
            "deep": {"minutes": 120, "avg_passes": 20, "span_scale": 0.5},
        },
        "band_dwell_weights": {
            "ism_433": 3.0, "ism_915": 3.0, "ism_2400": 3.0, "dect": 2.5,
            "cellular_uplink": 2.5, "ism_5800": 2.0, "gps": 1.5,
            "default": 1.0, "broadcast": 0.4,
        },
    },
    "cellmapper": {"import_path": "data/cellmapper_towers.json"},
    "integrations": {"discord_webhook": ""},
    "device": {"simulate": False, "port": "", "baud": 500000},
    "cache": {"dir": "cache", "max_age_hours": 720},
    "calibration": {},
}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


class Config:
    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self.data = copy.deepcopy(DEFAULTS)
        self.load()

    def load(self):
        if os.path.exists(self.path):
            with open(self.path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
            self.data = _merge(DEFAULTS, raw)
        return self

    def get(self, *keys, default=None):
        cur = self.data
        for k in keys:
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        return cur

    def set_and_save(self, keys: tuple, value):
        """Used by calibration mode to persist tuned values back to config.yaml."""
        with _LOCK:
            cur = self.data
            for k in keys[:-1]:
                cur = cur.setdefault(k, {})
            cur[keys[-1]] = value
            with open(self.path, "w", encoding="utf-8") as f:
                yaml.safe_dump(self.data, f, sort_keys=False, allow_unicode=True)

    @property
    def base_dir(self) -> str:
        return os.path.dirname(self.path)

    def abs_path(self, rel: str) -> str:
        return rel if os.path.isabs(rel) else os.path.join(self.base_dir, rel)
