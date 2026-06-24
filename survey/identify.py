"""sigidwiki-style signal identification.

The RF Explorer is amplitude-only, so historically the app could only fingerprint
a signal's SHAPE (see sigcapture.signal_features). The Pluto gives real IQ, so we
can extract MODULATION features (occupied bandwidth, FSK shift / state count,
duty cycle, spectral flatness, symbol rate) and match them against a reference
set of known signals modeled on sigidwiki.com — i.e. answer "what IS this
emitter?", not just "what licensed service should be here?".

Matching is by EXTRACTED FEATURES + frequency band, NOT by pixel-comparing
waterfalls (sigidwiki images vary wildly in span/scale). We rank candidate
reference signals and surface them with the reasons they matched; the operator
confirms against the live sigidwiki page (link) by eye/ear.

Pure DSP (numpy only) — unit tested in tests/test_identify.py. No hardware here;
the caller supplies IQ (from SpectrumSource.capture_iq).
"""

import json
import logging
import os

import numpy as np

from .sweep.sdr_source import welch_psd

log = logging.getLogger("survey.identify")


# ---------------------------------------------------------------------------
# IQ modulation feature extraction
# ---------------------------------------------------------------------------

def _occupied_bw_hz(freqs_hz: np.ndarray, psd_db: np.ndarray, down_db: float = 10.0) -> float:
    """Width of the band within `down_db` of the peak bin (rough occupied BW)."""
    peak = float(np.max(psd_db))
    above = psd_db >= (peak - down_db)
    if not np.any(above):
        return 0.0
    idx = np.where(above)[0]
    return float(freqs_hz[idx[-1]] - freqs_hz[idx[0]])


def _spectral_flatness(psd_lin: np.ndarray) -> float:
    """Wiener entropy: ~1.0 = flat/noise-like (OFDM, DSSS), ~0 = tonal (CW/FM)."""
    psd_lin = np.maximum(psd_lin, 1e-20)
    gm = np.exp(np.mean(np.log(psd_lin)))
    am = float(np.mean(psd_lin))
    return float(gm / am) if am > 0 else 0.0


def _count_tones(psd_db: np.ndarray, prominence_db: float = 10.0) -> int:
    """Distinct spectral peaks standing `prominence_db` above the local floor."""
    floor = float(np.median(psd_db))
    thr = floor + prominence_db
    n = psd_db.size
    peaks = 0
    last = -10
    for i in range(1, n - 1):
        if psd_db[i] >= thr and psd_db[i] >= psd_db[i - 1] and psd_db[i] >= psd_db[i + 1]:
            if i - last > 2:
                peaks += 1
                last = i
    return peaks


def _fsk_analysis(iq: np.ndarray, fs: float, active: np.ndarray) -> dict:
    """Detect FSK from the instantaneous-frequency distribution. True FSK is
    genuinely MULTIMODAL — tight clusters at the keying frequencies with a clear
    valley between them. A noisy CW tone or an OOK burst is unimodal (one cluster
    at the carrier), so this must NOT fire on those."""
    none = {"is_fsk": False, "states": 0, "shift_hz": 0.0}
    if active.sum() < 128:
        return none
    inst = np.diff(np.unwrap(np.angle(iq))) * fs / (2.0 * np.pi)
    inst = inst[active[1:]]
    if inst.size < 128:
        return none
    lo, hi = np.percentile(inst, 5), np.percentile(inst, 95)
    if hi - lo < 200:                       # essentially single-frequency
        return none
    bins = 25
    hist, edges = np.histogram(np.clip(inst, lo, hi), bins=bins)
    centers = (edges[:-1] + edges[1:]) / 2.0
    min_count = 0.12 * hist.sum()           # each mode must hold >=12% of samples
    peaks = [i for i in range(bins)
             if hist[i] >= min_count
             and (i == 0 or hist[i] >= hist[i - 1])
             and (i == bins - 1 or hist[i] >= hist[i + 1])]
    merged = []                              # merge adjacent bins into one peak
    for i in peaks:
        if merged and i - merged[-1] <= 1:
            if hist[i] > hist[merged[-1]]:
                merged[-1] = i
        else:
            merged.append(i)
    if len(merged) < 2:
        return {"is_fsk": False, "states": len(merged), "shift_hz": 0.0}
    strong = sorted(merged, key=lambda i: hist[i], reverse=True)[:2]
    a, b = sorted(strong)
    valley = int(hist[a:b + 1].min())
    if valley > 0.45 * min(hist[a], hist[b]):   # no real gap -> unimodal
        return {"is_fsk": False, "states": 1, "shift_hz": 0.0}
    shift = float(centers[b] - centers[a])
    return {"is_fsk": shift > 1000, "states": len(merged), "shift_hz": round(shift, 1)}


def _symbol_rate_hz(iq: np.ndarray, fs: float) -> float:
    """Estimate symbol/keying rate from the autocorrelation of the envelope's
    AC component (first strong non-zero-lag peak)."""
    mag = np.abs(iq).astype(np.float64)
    mag = mag - np.mean(mag)
    if np.allclose(mag, 0):
        return 0.0
    n = min(mag.size, 1 << 14)
    ac = np.correlate(mag[:n], mag[:n], mode="full")[n - 1:]
    if ac[0] <= 0:
        return 0.0
    ac = ac / ac[0]
    lo = max(2, int(fs / 2e6))            # ignore lags faster than ~2 MBaud
    seg = ac[lo:min(ac.size, lo + 4000)]
    if seg.size == 0:
        return 0.0
    k = int(np.argmax(seg)) + lo
    if ac[k] < 0.2:
        return 0.0
    return float(fs / k)


def iq_features(iq, fs: float) -> dict:
    """Extract modulation features from a complex baseband capture."""
    iq = np.asarray(iq, dtype=np.complex64)
    if iq.size < 256:
        raise ValueError("need >= 256 IQ samples")
    # Duty / activity by ENVELOPE DYNAMIC RANGE. A constant-envelope signal
    # (CW/FM/FSK/PSK) has no quiet reference inside the capture, so a
    # median-relative threshold is meaningless; instead compare the low (5th
    # pct = "off"/noise) and high (95th pct = "on") levels. A clear on/off gap =>
    # bursty/OOK, measure the on-fraction; otherwise it's a continuous emission.
    mag = np.abs(iq)
    lo = float(np.percentile(mag, 5))
    hi = float(np.percentile(mag, 95))
    if hi > 5.0 * (lo + 1e-9):
        thr = lo + 0.3 * (hi - lo)
        active = mag > thr
        duty = float(np.mean(active))
    else:
        active = np.ones(mag.size, dtype=bool)
        duty = 1.0

    freqs, psd_db = welch_psd(iq, fs, nperseg=min(1024, iq.size // 4 or 256))
    psd_lin = 10.0 ** (psd_db / 10.0)
    # SNR of the strongest bin over the median (noise) level. A real emitter
    # stands proud of the floor; an empty channel is flat (low SNR) and must NOT
    # be classified as a wideband signal just because noise is flat and wide.
    snr_db = float(np.max(psd_db) - np.median(psd_db))
    signal_present = snr_db >= 8.0
    occ_bw = _occupied_bw_hz(freqs, psd_db, down_db=10.0)
    flatness = _spectral_flatness(psd_lin)
    tones = _count_tones(psd_db)
    fsk = _fsk_analysis(iq, fs, active)
    symbol_rate = _symbol_rate_hz(iq, fs)

    feats = {
        "occupied_bw_khz": round(occ_bw / 1e3, 1),
        "duty_cycle": round(duty, 3),
        "continuity": "continuous" if duty > 0.85 else ("bursty" if duty < 0.35 else "intermittent"),
        "spectral_flatness": round(flatness, 3),
        "distinct_tones": tones,
        "fsk_states": fsk["states"],
        "fsk_shift_khz": round(fsk["shift_hz"] / 1e3, 1),
        "symbol_rate_kbd": round(symbol_rate / 1e3, 2),
        "snr_db": round(snr_db, 1),
        "signal_present": signal_present,
    }
    # An empty channel (low SNR, just noise) is reported as such, not classified.
    feats["modulation_class"] = (classify_modulation(feats) if signal_present
                                 else "noise_floor")
    return feats


def classify_modulation(f: dict) -> str:
    """Coarse modulation class from features — matches sigidwiki_ref 'class' tags."""
    bw = f["occupied_bw_khz"]
    if f["spectral_flatness"] > 0.45 and bw > 4000:
        return "ofdm_wideband"       # Wi-Fi / LTE: flat, wide
    if f["fsk_states"] >= 2 and f["fsk_shift_khz"] > 0.1:
        return "fsk"
    if f["continuity"] == "bursty" and bw < 300:
        return "ook"                 # short narrow on/off bursts (keying sidebands ok)
    if f["continuity"] == "continuous" and f["distinct_tones"] <= 2 and bw > 2000:
        return "analog_video"        # wide continuous FM
    if f["continuity"] == "continuous" and bw <= 300:
        return "analog_voice"        # narrow continuous carrier
    if f["spectral_flatness"] > 0.35 and bw > 500:
        return "continuous_digital"
    return "unknown"


# ---------------------------------------------------------------------------
# Reference set + matching
# ---------------------------------------------------------------------------

# reference set ships WITH the package (data/ is gitignored for runtime DBs)
DEFAULT_REF_PATH = os.path.join(os.path.dirname(__file__), "sigidwiki_ref.json")


def load_reference(path: str | None = None) -> list[dict]:
    with open(path or DEFAULT_REF_PATH, "r", encoding="utf-8") as fh:
        return (json.load(fh) or {}).get("signals", [])


def _bw_score(meas_khz: float, ref_khz: float) -> float:
    """1.0 when bandwidths match, decaying with the log-ratio (0..1)."""
    if meas_khz <= 0 or ref_khz <= 0:
        return 0.0
    r = abs(np.log10(meas_khz / ref_khz))
    return float(max(0.0, 1.0 - r))        # within ~1 decade -> partial credit


def identify(center_hz: float, feats: dict | None, reference: list[dict],
             top_n: int = 5) -> list[dict]:
    """Rank reference signals for an emitter at center_hz.

    feats (from iq_features) may be None when no IQ is available (RF Explorer):
    then matching falls back to frequency band + bandwidth only.
    """
    center_mhz = center_hz / 1e6
    out = []
    for ref in reference:
        score, reasons = 0.0, []
        in_band = ref["freq_min_mhz"] <= center_mhz <= ref["freq_max_mhz"]
        if in_band:
            score += 50.0
            reasons.append(f"{center_mhz:.3f} MHz is in this signal's band "
                           f"({ref['freq_min_mhz']}-{ref['freq_max_mhz']} MHz)")
        else:
            # out of band is a strong negative but not disqualifying (ranges are approximate)
            edge = min(abs(center_mhz - ref["freq_min_mhz"]),
                       abs(center_mhz - ref["freq_max_mhz"]))
            score -= min(40.0, edge)
        if feats:
            bws = _bw_score(feats["occupied_bw_khz"], ref["bandwidth_khz"])
            score += 30.0 * bws
            if bws > 0.6:
                reasons.append(f"bandwidth ~{feats['occupied_bw_khz']:.0f} kHz "
                               f"≈ {ref['bandwidth_khz']} kHz")
            if feats["modulation_class"] == ref["class"]:
                score += 20.0
                reasons.append(f"modulation looks like {ref['class']}")
            # duty/continuity consistency
            if feats["continuity"] == ref.get("duty"):
                score += 8.0
                reasons.append(f"{feats['continuity']} — consistent")
        else:
            # frequency-only mode: lean on bandwidth from the sweep if present
            reasons.append("frequency/bandwidth only (connect the Pluto for "
                           "modulation-based matching)")
        out.append({
            "name": ref["name"], "url": ref["url"], "mode": ref["mode"],
            "class": ref["class"], "bandwidth_khz": ref["bandwidth_khz"],
            "tscm": ref.get("tscm", ""), "in_band": in_band,
            "score": round(score, 1), "reasons": reasons,
        })
    out.sort(key=lambda c: c["score"], reverse=True)
    # only return candidates with positive evidence
    out = [c for c in out if c["score"] > 0]
    return out[:top_n]


def spectrogram(iq, fs: float, n_time: int = 120, nperseg: int = 256) -> dict:
    """Coarse waterfall (time x freq dB) of a capture, for the operator to eyeball
    next to the candidate sigidwiki entries. Downsampled for cheap rendering."""
    iq = np.asarray(iq, dtype=np.complex64)
    nperseg = min(nperseg, iq.size)
    step = max(1, (iq.size - nperseg) // max(n_time - 1, 1))
    rows = []
    for start in range(0, iq.size - nperseg + 1, step):
        seg = iq[start:start + nperseg]
        sp = np.fft.fftshift(np.fft.fft(seg * np.hanning(nperseg)))
        rows.append(10.0 * np.log10(np.abs(sp) ** 2 + 1e-20))
        if len(rows) >= n_time:
            break
    grid = np.array(rows) if rows else np.zeros((1, nperseg))
    fr = np.fft.fftshift(np.fft.fftfreq(nperseg, d=1.0 / fs))
    return {"freqs_hz": fr.tolist(),
            "db": np.round(grid, 1).tolist(),
            "vmin": float(np.percentile(grid, 5)),
            "vmax": float(np.percentile(grid, 99))}
