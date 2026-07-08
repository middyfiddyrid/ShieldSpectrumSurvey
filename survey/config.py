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
    # spectrum source family: "rfexplorer" (default, unchanged), "pluto", or
    # "b220" (SUOGOEST B220 Mini / USRP B210-compatible via UHD).
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
        "analyze_seconds": 2.0,            # ANALYZE one-shot: longer so decoders get enough symbols
        # continuous audio stream: lower rate so USB2 sustains REAL-TIME (smooth);
        # 600 kHz still covers WFM (~200 kHz) and NFM channels.
        "stream_sample_rate": 2_400_000,
        "listen_block_seconds": 0.15,      # spectrum latency lever: smaller -> more
                                           # live (faster scroll); bigger -> smoother
                                           # audio if the USB capture underruns
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
        # DUAL-DEVICE (Phase 2): when true, the app holds the RF Explorer (sweeper)
        # and the Pluto (monitor) at once, each on its own lock, so a sweep and a
        # live monitor/analyze/record run concurrently. Default OFF — enable only
        # after a two-radio bench check (HANDOFF: full simultaneous sweep+monitor
        # on ONE radio waits for the July 2RX/2TX B220).
        "concurrent_monitor": False,
    },
    # SUOGOEST B220 Mini (USRP B210-compatible, AD9361 2R2T) via UHD. Only used
    # when device.source == "b220". Sweep-engine knobs mirror the pluto section
    # (same stepped-sweep code in sdr_source.py, bound through uhd_source.py).
    # NOTE: the listen/analyze/identify DSP tuning in server.py currently reads
    # pluto.* regardless of which SDR is connected — those paths see normalised
    # IQ from both radios, so the shared values stay consistent.
    "b220": {
        "device_args": "type=b200",   # UHD device args; add ,serial=... for two units
        "uhd_dir": "",                # dir with uhd.dll; "" => C:\\Program Files\\UHD\\bin
        # The B220 Mini is a LibreSDR clone (Artix-7), NOT a genuine B210
        # (Spartan-6). The stock Ettus usrp_b210_fpga.bin can't configure it
        # ("fx3 is in state 5"). B220Source auto-installs the correct bitstream
        # bundled in native/uhd/ into UHD's images dir at connect. Override only
        # if needed:
        "fpga": "",                   # explicit FPGA .bin path; set = skip auto-install
        "fpga_bundled": "usrp_b210_fpga.bin",  # bundled image name in native/uhd/ (100T; swap for 200T)
        "antenna": "RX2",             # per-channel RX port: "RX2" (RX-only) or "TX/RX"
        "sample_rate": 20_000_000,    # instantaneous bandwidth per tune (USB3 sustains more; 20M is safe)
        "keep_fraction": 0.8,
        "overlap": 0.5,
        "fft_size": 1024,
        "capture_samples": 32768,
        "settle_seconds": 0.01,
        "dc_notch_bins": 2,
        "dc_guard_hz": 250_000,
        "sweep_bins": 2048,
        "gain_mode": "manual",        # "manual" (repeatable baseline); anything else = AGC
        "gain_db": 60.0,              # B2xx RX gain range is 0-76 dB
        "sweep_avg_passes": 2,
        "identify_samples": 131072,
        "identify_captures": 8,
        # UHD fc32 samples are normalised ~[-1,1] (vs the Pluto sweep path's raw
        # +/-2048 ADC counts), so the equivalent offset sits ~+66 dB above the
        # Pluto's -30. APPROXIMATE — tune per-unit; lean on baseline-delta.
        "cal_offset_db": 36.0,
        # AD9361: full 70 MHz - 6 GHz out of the box (no extension dance)
        "freq_min_hz": 70_000_000,
        "freq_max_hz": 6_000_000_000,
        "require_extended": False,
    },
    # Phase-interferometry direction finding on the B220's two coherent RX
    # chains. spacing_m is the physical antenna separation — MUST be <= half a
    # wavelength at the target frequency for an unambiguous bearing (0.35 m is
    # good to ~428 MHz; tighten for higher bands). sample_rate 4e6 fails this
    # AD9361's VCO search — 5e6 is the safe floor.
    "df": {
        "spacing_m": 0.35,
        "sample_rate": 5_000_000,
        "capture_samples": 1 << 17,
        "nperseg": 4096,
        "snr_gate_db": 6.0,
        "coherence_gate": 0.5,
    },
    "cache": {"dir": "cache", "max_age_hours": 720},
    # monitor recordings (WAV of a received signal). Client-sensitive PII: the
    # dir is gitignored and purged per engagement (move-to-archive, never delete).
    "recordings": {"dir": "recordings", "max_seconds": 120.0},
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
        # session-only overrides (e.g. --simulate): consulted by get() but
        # kept out of self.data so set_and_save() can never write them to disk
        self._runtime_overrides = {}
        self.load()

    def load(self):
        if os.path.exists(self.path):
            with open(self.path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
            self.data = _merge(DEFAULTS, raw)
        return self

    def get(self, *keys, default=None):
        # getattr: test fakes subclass Config without running __init__
        overrides = getattr(self, "_runtime_overrides", None)
        if overrides and keys in overrides:
            return overrides[keys]
        cur = self.data
        for k in keys:
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        return cur

    def set_runtime_override(self, keys: tuple, value):
        """Override a setting for this process only. get(*keys) returns the
        override (full key path only), and it is never persisted — set_and_save()
        dumps self.data, which this does not touch."""
        if getattr(self, "_runtime_overrides", None) is None:
            self._runtime_overrides = {}
        self._runtime_overrides[tuple(keys)] = value

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
