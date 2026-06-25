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
    "api_keys": {"opencellid": "", "radioreference_user": "", "radioreference_pass": "",
                 "anthropic": ""},
    "uls": {"extract_dir": "", "stale_days": 30},
    "detection": {
        "threshold_db": 12.0,
        "merge_gap_bins": 2,
        "min_persistence": 0.05,
        "interior_delta_flag_db": 10.0,
        "room_delta_flag_db": 12.0,
        "ignore_below_mhz": 0.15,        # LF conducted-noise floor
        "lf_extra_threshold_db": 6.0,    # extra SNR demanded below lf_extra_below_mhz
        "lf_extra_below_mhz": 50.0,
    },
    "capture": {"avg_passes": 20, "maxhold_dwell_s": 12.0},
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
    # spectrum source family: "rfexplorer" (default, unchanged) or "pluto".
    # env SWEEP_SOURCE overrides this.
    "device": {"simulate": False, "port": "", "baud": 500000, "source": "rfexplorer"},
    # ADALM-Pluto / AD936x SDR source. Only used when device.source == "pluto".
    "pluto": {
        "uri": "ip:192.168.2.1",          # env PLUTO_URI overrides; or "usb:"
        "libiio_dir": "",                  # Windows: dir with libiio.dll; "" => %LOCALAPPDATA%\\libiio\\bin
        "sample_rate": 20_000_000,         # instantaneous bandwidth per tune (<= ~20 MSPS sustained on USB2)
        "keep_fraction": 0.8,              # central fraction of each segment kept (analog filter edge rolloff)
        "overlap": 0.5,                    # inter-segment overlap; >=0.5 also covers each tune-center's DC notch
        "fft_size": 1024,                  # Welch segment length
        "capture_samples": 32768,          # IQ samples per tune (tuned: ~32 FFT averages, fast USB transfer)
        "settle_seconds": 0.01,            # post-retune settle before discarding the stale buffer (tuned)
        "dc_notch_bins": 2,                # +/- bins for the notch_dc() helper (read_sweep uses dc_guard_hz)
        "dc_guard_hz": 250_000,            # central guard band discarded at each tune center (LO leakage); refilled by overlap
        "sweep_bins": 2048,                # resampled grid points per read_sweep frame (caps host-side cost)
        "gain_mode": "manual",             # "manual" (repeatable baseline) or "slow_attack" (first-look survey)
        "gain_db": 64.0,                   # fixed RX gain when gain_mode == manual
        # SDR frames are already fully averaged, so the RF Explorer's many host
        # AVERAGE passes are redundant — cap them here. (MAX-HOLD dwell is NOT
        # capped: it's real watch-time for intermittent/bursty emitters and is
        # governed by the sweep profile + per-band risk weighting.)
        "sweep_avg_passes": 2,
        # sigidwiki identify: take the best-SNR of N short IQ captures so bursty
        # emitters (Wi-Fi/BLE) get caught; an empty channel reads noise across all.
        "identify_samples": 131072,
        "identify_captures": 8,
        # "tune & listen" inspect panel: IQ capture for analyzer + waterfall + audio
        "listen_sample_rate": 2_400_000,   # wide one-shot IQ for the analyzer/waterfall snapshot
        "listen_seconds": 0.5,
        # continuous audio stream: lower rate so USB2 sustains REAL-TIME (smooth);
        # 600 kHz still covers WFM (~200 kHz) and NFM channels.
        "stream_sample_rate": 2_400_000,
        "listen_block_seconds": 0.5,       # bigger blocks amortize the per-buffer refill gap
        # live analyzer + scrolling waterfall (the poll-tuned /api/listen_spectrum
        # path). Each audio block emits listen_wf_rows_per_block contiguous STFT
        # rows so the waterfall scrolls smoothly; the client owns the scroll
        # history (listen_wf_history_rows must match the client ROWS constant).
        "listen_wf_nperseg": 512,          # waterfall FFT size -> 512 freq bins
        "listen_wf_rows_per_block": 8,     # contiguous waterfall rows per block
        "listen_wf_history_rows": 240,     # visible history depth (client ROWS)
        "listen_analyzer_nperseg": 2048,   # Welch FFT for the analyzer trace
        "listen_analyzer_bins": 512,       # analyzer points (power-domain decimate)
        "listen_analyzer_ema": 0.4,        # analyzer smoothing across blocks
        "listen_wf_floor_ema": 0.1,        # noise-floor EMA for a stable color map
        "listen_wf_span_lo_db": -5.0,      # color span: [floor+lo, floor+hi]
        "listen_wf_span_hi_db": 40.0,
        # ~approximate~ dBFS -> dBm calibration offset. NOT lab-grade; lean on
        # baseline-delta detection. Tune per-unit in calibration mode.
        "cal_offset_db": -30.0,
        # tunable limits. Stock AD9363A ~ 325 MHz-3.8 GHz. After the AD9364
        # frequency extension, set these to 70e6 / 6000e6.
        "freq_min_hz": 325_000_000,
        "freq_max_hz": 3_800_000_000,
        # set true to HARD-FAIL at connect if the radio can't reach freq_min/max
        # (i.e. you expect the frequency extension to be applied)
        "require_extended": False,
    },
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
