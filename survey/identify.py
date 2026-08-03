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

def _occupied_bw_hz(freqs_hz: np.ndarray, psd_db: np.ndarray, down_db: float = 10.0,
                    floor_margin_db: float = 6.0, gap_bins: int = 4) -> float:
    """Width of the occupied band around the tuned signal. See _occupied_band_hz."""
    lo_hz, hi_hz = _occupied_band_hz(freqs_hz, psd_db, down_db, floor_margin_db, gap_bins)
    return max(0.0, hi_hz - lo_hz)


def _occupied_band_hz(freqs_hz: np.ndarray, psd_db: np.ndarray, down_db: float = 10.0,
                      floor_margin_db: float = 6.0,
                      gap_bins: int = 4) -> "tuple[float, float]":
    """Occupied bandwidth — bins both within `down_db` of the peak AND clearly
    above the noise floor. The noise-floor gate is what stops a WEAK signal from
    reading as full-capture-width: at low SNR the peak is barely above the floor,
    so a plain peak-minus-10dB threshold smears across the whole window and
    measures noise. Anchoring to median + margin keeps a faint narrowband carrier
    narrow instead of mislabeling it as a 2 MHz wideband emission."""
    peak = float(np.max(psd_db))
    floor = float(np.median(psd_db))
    thr = max(peak - down_db, floor + floor_margin_db)
    above = psd_db >= thr
    if not np.any(above):
        return 0.0, 0.0

    # Measure the run CONTAINING the tuned signal — not the span from the lowest
    # to the highest above-threshold bin anywhere in the capture. The old
    # first-to-last span assumed the window held ONE emitter: point a 20 MHz
    # capture at 101 MHz and it spans ~91-111 MHz, so it returned the distance
    # from the lowest FM station to the highest and called a 200 kHz broadcast
    # channel 9.26 MHz wide. Every downstream feature inherited that error, so
    # nothing could be classified correctly whenever a neighbour shared the
    # window — which, in a real RF environment, is nearly always.
    n = above.size
    filled = above.copy()
    i = 0
    while i < n:                        # bridge nulls INSIDE one signal (FM has
        if filled[i]:                   # them) without merging separate signals
            i += 1
            continue
        j = i
        while j < n and not filled[j]:
            j += 1
        if 0 < i and j < n and (j - i) <= gap_bins:
            filled[i:j] = True
        i = j

    anchor = int(np.argmin(np.abs(freqs_hz)))     # band center = what was tuned
    if not filled[anchor]:
        anchor = int(np.argmax(psd_db))           # tuned off-centre: use the peak
    lo = hi = anchor
    while lo > 0 and filled[lo - 1]:
        lo -= 1
    while hi < n - 1 and filled[hi + 1]:
        hi += 1
    return float(freqs_hz[lo]), float(freqs_hz[hi])


def _isolate_channel(iq: np.ndarray, fs: float, center_hz: float, bw_hz: float,
                     guard: float = 1.6) -> np.ndarray:
    """Down-convert the occupied band to DC and low-pass to it, so the features
    below describe THE tuned emitter rather than everything else sharing the
    capture. Without this, a 20 MHz window on the FM band measured duty cycle,
    continuity, tone count and flatness across a dozen stations at once — which
    is how a 24/7 broadcast carrier came back as 'intermittent' with 34 tones.
    Reuses demod's windowed-sinc filter so isolation means the same thing here
    as it does on the listen path."""
    from .demod import _lowpass
    if center_hz:
        t = np.arange(iq.size, dtype=np.float64) / fs
        iq = (iq * np.exp(-1j * 2 * np.pi * center_hz * t)).astype(np.complex64)
    # guard band keeps the signal's own skirts; floor keeps the filter sane for
    # a very narrow carrier measured with coarse bins.
    cutoff = min(max(bw_hz * guard / 2.0, 4.0 * fs / max(iq.size, 1)), fs * 0.45)
    return _lowpass(iq, fs, cutoff).astype(np.complex64)


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
    nperseg = min(1024, iq.size // 4 or 256)
    freqs, psd_db = welch_psd(iq, fs, nperseg=nperseg)
    # SNR of the strongest bin over the median (noise) level, measured on the
    # RAW capture — the wide window is what holds a true noise reference. A real
    # emitter stands proud of the floor; an empty channel is flat (low SNR) and
    # must NOT be classified as a wideband signal just because noise is flat.
    snr_db = float(np.max(psd_db) - np.median(psd_db))
    signal_present = snr_db >= 8.0
    band_lo_hz, band_hi_hz = _occupied_band_hz(freqs, psd_db, down_db=10.0)
    occ_bw = max(0.0, band_hi_hz - band_lo_hz)

    # Isolate the tuned channel BEFORE measuring anything else. Every feature
    # past this point (duty, continuity, flatness, tones, FSK, symbol rate) is
    # meaningless when a neighbour shares the capture, and in a real RF
    # environment one nearly always does.
    # ...but only when something ELSE is actually in the window. Filtering is
    # not free: it rounds the keying edges of a bursty signal (OOK's sharp
    # transitions live in the skirts, outside the -10 dB occupied band), which
    # would cost us the very burst structure the classifier reads. So isolate
    # only when a meaningful share of the captured power sits outside the tuned
    # channel — i.e. when a neighbour is there to contaminate the measurement.
    if signal_present and 0.0 < occ_bw < fs * 0.8:
        # A NEIGHBOUR is a distinct peak well clear of the tuned channel — not
        # merely power outside it, which a bursty signal's own sinc skirts also
        # produce. Measured: an isolated OOK burst's far peak sits ~21 dB below
        # its carrier, while a co-resident FM station is within ~0 dB. Skip the
        # guard band adjacent to the channel so those skirts are not mistaken
        # for company.
        bin_hz = float(freqs[1] - freqs[0]) if freqs.size > 1 else 0.0
        guard_hz = max(occ_bw * 2.0, 4.0 * bin_hz)
        far = (freqs < band_lo_hz - guard_hz) | (freqs > band_hi_hz + guard_hz)
        if np.any(far) and float(np.max(psd_db[far])) > float(np.max(psd_db)) - 15.0:
            iq = _isolate_channel(iq, fs, 0.5 * (band_lo_hz + band_hi_hz), occ_bw)
            freqs, psd_db = welch_psd(iq, fs, nperseg=nperseg)
    psd_lin = 10.0 ** (psd_db / 10.0)

    # Duty / activity by ENVELOPE DYNAMIC RANGE, on the isolated channel. A
    # constant-envelope signal (CW/FM/FSK/PSK) has no quiet reference inside the
    # capture, so a median-relative threshold is meaningless; instead compare the
    # low (5th pct = "off"/noise) and high (95th pct = "on") levels. A clear
    # on/off gap => bursty/OOK, measure the on-fraction; otherwise continuous.
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

    flatness = _spectral_flatness(psd_lin)
    tones = _count_tones(psd_db)
    fsk = _fsk_analysis(iq, fs, active)
    symbol_rate = _symbol_rate_hz(iq, fs)
    # Burst period (ms) from envelope autocorrelation — keyfob vs HID keepalive
    burst_period_ms = 0.0
    if duty < 0.85 and symbol_rate > 0:
        burst_period_ms = round(1000.0 / symbol_rate, 2) if symbol_rate > 1 else 0.0
    elif duty < 0.85:
        # fall back: first strong lag on raw envelope AC (period of onsets)
        mag0 = mag.astype(np.float64) - float(np.mean(mag))
        n = min(mag0.size, 1 << 14)
        if n > 64 and not np.allclose(mag0[:n], 0):
            ac = np.correlate(mag0[:n], mag0[:n], mode="full")[n - 1:]
            if ac[0] > 0:
                ac = ac / ac[0]
                # look for lags ~0.5 ms .. 200 ms
                lo_i = max(2, int(fs * 0.0005))
                hi_i = min(ac.size - 1, int(fs * 0.2))
                if hi_i > lo_i:
                    k = int(np.argmax(ac[lo_i:hi_i])) + lo_i
                    if ac[k] >= 0.15:
                        burst_period_ms = round(1000.0 * k / fs, 2)

    feats = {
        "occupied_bw_khz": round(occ_bw / 1e3, 1),
        "duty_cycle": round(duty, 3),
        "continuity": "continuous" if duty > 0.85 else ("bursty" if duty < 0.35 else "intermittent"),
        "spectral_flatness": round(flatness, 3),
        "distinct_tones": tones,
        "fsk_states": fsk["states"],
        "fsk_shift_khz": round(fsk["shift_hz"] / 1e3, 1),
        "symbol_rate_kbd": round(symbol_rate / 1e3, 2),
        "burst_period_ms": burst_period_ms,
        "snr_db": round(snr_db, 1),
        "signal_present": signal_present,
    }
    # An empty channel (low SNR, just noise) is reported as such, not classified.
    feats["modulation_class"] = (classify_modulation(feats) if signal_present
                                 else "noise_floor")
    return feats


def classify_modulation(f: dict) -> str:
    """Coarse modulation class from features — matches sigidwiki_ref 'class' tags.

    Order matters: keyed/bursty and FSK are decided BEFORE any wideband-digital
    call, and the 'scary' wideband labels (OFDM/video/continuous-digital) require
    a STRONG, CONTINUOUS signal. This stops the old failure where a weak, bursty,
    keyed carrier (a beacon / Morse / remote) got mislabeled 'wideband digital'
    because a low-SNR bandwidth measurement smeared across the whole capture."""
    bw = f["occupied_bw_khz"]
    snr = float(f.get("snr_db", 0.0))
    cont = f["continuity"]
    # too weak to characterize — don't manufacture a class from noisy features
    if snr < 6.0:
        return "unknown"
    # genuine multi-state FSK wins (bursty or continuous)
    if f["fsk_states"] >= 2 and f["fsk_shift_khz"] > 0.1:
        return "fsk"
    # bursty / on-off keyed => OOK (keyed carrier, beacon, Morse, remote, sensor).
    # A low-duty signal is NEVER OFDM/Wi-Fi, regardless of a smeared bandwidth.
    if cont == "bursty":
        return "ook"
    # wideband digital ONLY when strong + continuous + flat + genuinely wide
    if cont == "continuous" and f["spectral_flatness"] > 0.45 and bw > 4000 and snr >= 15:
        return "ofdm_wideband"       # Wi-Fi / LTE: flat, wide, strong
    if cont == "continuous" and f["distinct_tones"] <= 2 and bw > 2000 and snr >= 12:
        return "analog_video"        # wide continuous FM
    if cont == "continuous" and bw <= 300:
        return "analog_voice"        # narrow continuous carrier
    if cont == "continuous" and f["spectral_flatness"] > 0.35 and bw > 500 and snr >= 12:
        return "continuous_digital"
    return "unknown"


# ---------------------------------------------------------------------------
# Reference set + matching
# ---------------------------------------------------------------------------

# reference set ships WITH the package (data/ is gitignored for runtime DBs)
DEFAULT_REF_PATH = os.path.join(os.path.dirname(__file__), "sigidwiki_ref.json")
CATALOG_PATH = os.path.join(os.path.dirname(__file__), "surveillance_catalog.json")


def load_reference(path: str | None = None) -> list[dict]:
    """Reference signals for matching. The sigidwiki starter set ALWAYS loads;
    when the default path is used we additively fold in the surveillance-device
    catalog (survey/surveillance_catalog.json) — its device_classes flattened to
    signatures in the same field shape, each tagged with its parent device_class
    + confirm_steps + false_positive_overlap so a match can surface them. Passing
    an explicit `path` loads ONLY that file (keeps tests/custom refs isolated)."""
    with open(path or DEFAULT_REF_PATH, "r", encoding="utf-8") as fh:
        refs = (json.load(fh) or {}).get("signals", [])
    if path is None:
        try:
            with open(CATALOG_PATH, "r", encoding="utf-8") as fh:
                cat = json.load(fh) or {}
            for dc in cat.get("device_classes", []):
                name = dc.get("device_class", "")
                steps = dc.get("confirm_steps", [])
                fps = dc.get("false_positive_overlap", [])
                for sig in dc.get("signatures", []):
                    r = dict(sig)
                    r["device_class"] = name
                    r["confirm_steps"] = steps
                    r["false_positive_overlap"] = fps
                    refs.append(r)
        except (FileNotFoundError, ValueError):
            pass                                     # catalog optional; sigidwiki still works
    return refs


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
            "device_class": ref.get("device_class"),            # catalog parent (None for sigidwiki entries)
            "confirm_steps": ref.get("confirm_steps", []),      # how to confirm this device class
            "false_positive_overlap": ref.get("false_positive_overlap", []),  # legit things that look the same
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


def waterfall_stft(iq, fs: float, nperseg: int = 512, rows: int = 8) -> dict:
    """Fast contiguous waterfall for the live listen view: `rows` STFT slices that
    SPAN the whole capture (continuous time axis, not isolated snapshots), via a
    single batched FFT over a sliding window. Each audio block yields a smooth
    multi-row scroll. Returns db as an ndarray (rows x nperseg). Falls back to the
    spectrogram() loop on numpy < 1.20."""
    iq = np.asarray(iq, dtype=np.complex64)
    nperseg = min(nperseg, iq.size)
    fr = np.fft.fftshift(np.fft.fftfreq(nperseg, d=1.0 / fs))
    try:
        swv = np.lib.stride_tricks.sliding_window_view
        hop = max(1, (iq.size - nperseg) // max(rows - 1, 1))
        frames = swv(iq, nperseg)[::hop][:rows]
        win = np.hanning(nperseg).astype(np.float64)
        sp = np.fft.fftshift(np.fft.fft(frames * win, axis=1), axes=1)
        db = 10.0 * np.log10(np.abs(sp) ** 2 + 1e-20)
        return {"freqs_hz": fr.tolist(), "db": np.round(db, 1)}
    except (AttributeError, ValueError):       # numpy < 1.20 or degenerate size
        spec = spectrogram(iq, fs, n_time=rows, nperseg=nperseg)
        return {"freqs_hz": spec["freqs_hz"], "db": np.asarray(spec["db"])}
