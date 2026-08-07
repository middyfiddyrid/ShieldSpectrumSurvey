"""ATSC 8VSB (US digital TV) channel verification — is the licensed station
actually on the air in this 6 MHz block, or is something else living there?

Why this exists. Clicking a TV station in the baseline used to show paperwork:
callsign, RF channel, distance, bearing — all of it from the FCC record, none of
it measured. That is the one place a sweep can be fooled. A 6 MHz block that the
operator has mentally filed as "that's just channel 34" is the most comfortable
place in the spectrum to hide something, precisely because nobody looks twice at
a big ugly haystack they already have a name for.

So this module measures the thing the FCC record cannot tell you: is the 8VSB
transmitter radiating, right now, at this location?

THE PILOT IS THE PROOF. 8VSB is a suppressed-carrier vestigial-sideband signal
with one deliberate exception — a small in-phase pilot left at the suppressed
carrier frequency, 309.440 kHz above the lower channel edge. Nothing else in the
spectrum puts a narrow CW spike at exactly that offset under a flat 5.38 MHz
plateau. Together the pair is decisive:

  * PILOT — a narrow peak within a few kHz of (channel_low + 309.440 kHz),
    standing proud of the data floor around it. Prominence is measured against
    the LOCAL median, not the whole capture, so a strong neighbouring station or
    a sloped noise floor cannot manufacture one.
  * PLATEAU — 8VSB data is noise-like and FLAT across 5.381 MHz (symbol rate
    10.762 MSym/s, 11.5 % raised-cosine roll-off). Flatness is what separates a
    real DTV haystack from a wide FM carrier, a jammer, or a bank of narrow
    emitters that happen to fill the channel.

A pilot with no plateau, or a plateau with no pilot, is reported as exactly that
— both are interesting, and both are lies if you call them "the TV station".

WHAT THIS DOES NOT DO, so nobody discovers it in a client's house:
  * It does NOT decode the picture. Recovering video from 8VSB needs symbol
    timing recovery, an adaptive equalizer over a 12-symbol trellis, Reed-Solomon
    and a full MPEG-2 transport demux — that is a C++-scale DSP chain, it cannot
    run in numpy, and it would not run in real time here if it could. For a
    DIGITAL station this module answers "is it on the air and is it really it",
    and says plainly that the picture is not available. `survey/video.py` is what
    recovers an actual picture, and it only works on ANALOG video rasters — which
    is the case that matters for TSCM anyway, because a wireless camera is analog
    far more often than it is ATSC.
  * A confirmed pilot proves an 8VSB transmitter is on that channel. It does NOT
    prove WHICH licensee is transmitting, and it cannot see a low-power device
    hiding under the haystack — a co-channel emitter 30 dB down is invisible
    beneath the data floor. Read a confirmation as "the licensed signal is
    present as expected", never as "this channel is clean".
  * Capture must span the pilot. The pilot sits 2.69 MHz BELOW channel centre, so
    a capture narrower than ~5.5 MHz centred on the channel will miss it and the
    verdict degrades to "pilot outside the captured span" — honest, not negative.

Pure numpy, no scipy — same house rule as demod.py / video.py / pocsag.py.
encode_atsc() lives here so tests synthesize a known 8VSB-shaped signal and run
it through the SAME detector; see tests/test_atsc.py.
"""

import numpy as np

PROTOCOL = "ATSC-8VSB"

# ---- the standard ----
CHANNEL_BW_HZ = 6_000_000.0
# 4.5 MHz / 286 * 684 — the ATSC symbol rate
SYMBOL_RATE_HZ = 10_762_237.76
# occupied bandwidth = symbol rate / 2 * (1 + 0.1152) * 2
OCCUPIED_BW_HZ = 5_381_118.88
# the pilot sits this far above the LOWER edge of the 6 MHz channel
PILOT_OFFSET_HZ = 309_440.559

# ---- detection thresholds (documented, not magic) ----
PILOT_SEARCH_HZ = 60_000.0     # how far either side of nominal we hunt the pilot
# The pilot must clear the DATA IT SITS ON. A real 8VSB pilot concentrates its
# power in ~one bin while the data spreads over ~11k bins, so in practice it
# stands 15-28 dB above the in-band floor; Welch-averaged noise fluctuation tops
# out around 5-6 dB. 10 dB sits in that gap.
PILOT_MIN_PROM_DB = 10.0
PLATEAU_MIN_FLAT_DB = 4.5      # max ripple across the plateau to call it flat
PLATEAU_MIN_FRAC = 0.70        # measured plateau / 5.381 MHz to call it full-width
# ...and the plateau has to be a SIGNAL, not just the noise floor being flat
# (broadband noise is perfectly flat and fills the channel by definition)
PLATEAU_MIN_SNR_DB = 6.0


def _welch_psd(iq, fs, nperseg=16384, overlap=0.5):
    """Baseband PSD in dB, DC-centred. Fine resolution on purpose: the pilot is
    a CW spike, so narrow bins concentrate it while the noise-like 8VSB data
    stays spread — that contrast IS the measurement."""
    iq = np.asarray(iq)
    n = int(iq.size)
    if n < 256:
        return np.zeros(0), np.zeros(0)
    nperseg = int(min(nperseg, n))
    step = max(1, int(nperseg * (1.0 - overlap)))
    win = np.hanning(nperseg)
    wsum = np.sum(win ** 2) or 1.0
    acc = None
    segs = 0
    for start in range(0, n - nperseg + 1, step):
        seg = iq[start:start + nperseg] * win
        p = np.abs(np.fft.fftshift(np.fft.fft(seg))) ** 2
        acc = p if acc is None else acc + p
        segs += 1
    if acc is None or segs == 0:
        return np.zeros(0), np.zeros(0)
    psd = acc / (segs * wsum * fs)
    freqs = np.fft.fftshift(np.fft.fftfreq(nperseg, 1.0 / fs))
    return freqs, 10.0 * np.log10(np.maximum(psd, 1e-30))


def _band_slice(freqs, psd_db, lo_hz, hi_hz):
    m = (freqs >= lo_hz) & (freqs <= hi_hz)
    return freqs[m], psd_db[m]


def _pilot_search(freqs, psd_db, want_hz):
    """Find the strongest narrow peak near `want_hz` and measure how far it
    stands above THE DATA IT SITS ON.

    The reference window is one-sided, taken from inside the channel ABOVE the
    pilot. That is not a detail: the pilot lands 309 kHz above the lower channel
    edge, i.e. right on the skirt, so a symmetric window straddles the edge and
    averages in-band data with out-of-band nothing. The resulting median is
    meaningless and an ordinary data bin measures ~26 dB "prominent" — which is
    exactly how an early build of this confirmed a station that had no pilot at
    all.
    """
    win = (freqs >= want_hz - PILOT_SEARCH_HZ) & (freqs <= want_hz + PILOT_SEARCH_HZ)
    if not np.any(win):
        return None
    wf, wp = freqs[win], psd_db[win]
    k = int(np.argmax(wp))
    peak_hz, peak_db = float(wf[k]), float(wp[k])
    # in-band reference: data above the pilot, clear of the pilot's own skirt
    ref = ((freqs >= want_hz + 60_000.0) & (freqs <= want_hz + 900_000.0))
    if not np.any(ref):
        return None
    floor_db = float(np.median(psd_db[ref]))
    return {"hz": peak_hz, "db": peak_db, "floor_db": floor_db,
            "prominence_db": peak_db - floor_db,
            "offset_error_hz": peak_hz - want_hz}


def _plateau(freqs, psd_db, centre_hz):
    """Measure the flat 8VSB data plateau around the channel centre."""
    half = OCCUPIED_BW_HZ / 2.0
    bf, bp = _band_slice(freqs, psd_db, centre_hz - half * 0.9, centre_hz + half * 0.9)
    if bp.size < 16:
        return None
    ref = float(np.median(bp))
    # ripple across the middle 90 % of the plateau, robust to a stray spur
    lo, hi = np.percentile(bp, [5.0, 95.0])
    # how much of the nominal occupied width actually sits within 10 dB of ref
    wide_f, wide_p = _band_slice(freqs, psd_db,
                                 centre_hz - CHANNEL_BW_HZ / 2.0,
                                 centre_hz + CHANNEL_BW_HZ / 2.0)
    if wide_p.size < 16:
        return None
    inband = wide_f[wide_p >= ref - 10.0]
    width_hz = float(inband.max() - inband.min()) if inband.size > 1 else 0.0
    # noise floor OUTSIDE the channel, for an honest channel-power figure
    out_lo = _band_slice(freqs, psd_db, centre_hz - CHANNEL_BW_HZ * 0.95,
                         centre_hz - CHANNEL_BW_HZ * 0.55)[1]
    out_hi = _band_slice(freqs, psd_db, centre_hz + CHANNEL_BW_HZ * 0.55,
                         centre_hz + CHANNEL_BW_HZ * 0.95)[1]
    outside = np.concatenate([x for x in (out_lo, out_hi) if x.size]) \
        if (out_lo.size or out_hi.size) else np.zeros(0)
    floor_db = float(np.median(outside)) if outside.size else None
    return {"ref_db": ref, "ripple_db": float(hi - lo), "width_hz": width_hz,
            "width_frac": width_hz / OCCUPIED_BW_HZ,
            "outside_floor_db": floor_db,
            "snr_db": (ref - floor_db) if floor_db is not None else None}


def atsc_signature(iq, fs, capture_center_hz, channel_low_hz):
    """Verify an ATSC 8VSB transmitter on the 6 MHz channel starting at
    `channel_low_hz`, from IQ captured at `capture_center_hz`.

    Returns a dict with `confirmed` (bool), `confidence` (0..1), the pilot and
    plateau measurements, a `summary` line, and `caveats` — never a bare boolean,
    because "no pilot" and "pilot outside the captured span" are different
    answers and the operator needs to know which one they got.
    """
    iq = np.asarray(iq)
    channel_centre_hz = channel_low_hz + CHANNEL_BW_HZ / 2.0
    want_abs_hz = channel_low_hz + PILOT_OFFSET_HZ
    # everything below works in BASEBAND offsets from the capture centre
    want_bb = want_abs_hz - capture_center_hz
    centre_bb = channel_centre_hz - capture_center_hz

    out = {"protocol": PROTOCOL, "confirmed": False, "confidence": 0.0,
           "channel_low_mhz": channel_low_hz / 1e6,
           "channel_high_mhz": (channel_low_hz + CHANNEL_BW_HZ) / 1e6,
           "pilot_expected_mhz": want_abs_hz / 1e6,
           "pilot": None, "plateau": None, "caveats": [],
           "picture": False,
           "summary": ""}

    freqs, psd_db = _welch_psd(iq, fs)
    if freqs.size == 0:
        out["summary"] = "capture too short to measure"
        return out

    nyq = fs / 2.0
    if not (-nyq < want_bb < nyq):
        out["summary"] = ("pilot sits outside the captured span — widen the "
                          "capture to at least 5.5 MHz centred on the channel")
        out["caveats"].append("pilot outside captured span; verdict inconclusive")
        return out
    if abs(want_bb) > nyq * 0.97:
        out["caveats"].append("pilot is at the very edge of the capture — "
                              "prominence may read low")

    pilot = _pilot_search(freqs, psd_db, want_bb)
    plateau = _plateau(freqs, psd_db, centre_bb)
    if pilot:
        pilot = dict(pilot, mhz=(pilot["hz"] + capture_center_hz) / 1e6)
    out["pilot"] = pilot
    out["plateau"] = plateau

    has_pilot = bool(pilot and pilot["prominence_db"] >= PILOT_MIN_PROM_DB
                     and abs(pilot["offset_error_hz"]) <= PILOT_SEARCH_HZ)
    # a plateau must be flat, full-width AND actually above the surrounding noise
    # — an empty channel is perfectly flat and perfectly full-width
    has_plateau = bool(plateau and plateau["ripple_db"] <= PLATEAU_MIN_FLAT_DB
                       and plateau["width_frac"] >= PLATEAU_MIN_FRAC
                       and (plateau["snr_db"] or 0.0) >= PLATEAU_MIN_SNR_DB)

    if has_pilot and has_plateau:
        out["confirmed"] = True
        out["confidence"] = 0.92
        out["summary"] = (
            f"ATSC 8VSB confirmed on air — pilot {pilot['prominence_db']:.1f} dB "
            f"above the data floor at {pilot['mhz']:.4f} MHz "
            f"({pilot['offset_error_hz'] / 1e3:+.1f} kHz from nominal), flat "
            f"{plateau['width_hz'] / 1e6:.2f} MHz data plateau")
    elif has_pilot:
        out["confidence"] = 0.55
        out["summary"] = (
            f"8VSB pilot present ({pilot['prominence_db']:.1f} dB) but the data "
            f"plateau is not flat/full-width — station may be at low power, "
            f"multipath-scarred, or something else shares the channel")
        out["caveats"].append("pilot without a clean plateau — worth a second look")
    elif has_plateau:
        out["confidence"] = 0.35
        out["summary"] = (
            f"a flat {plateau['width_hz'] / 1e6:.2f} MHz carrier fills this "
            f"channel but there is NO 8VSB pilot — this is not the licensed "
            f"DTV signal; identify it before dismissing the channel")
        out["caveats"].append("channel-filling energy with no pilot — NOT ATSC")
    else:
        out["summary"] = ("no ATSC signal detected on this channel — the "
                          "licensed station is not being received here")
    out["caveats"].append(
        "a confirmed pilot proves an 8VSB transmitter is on this channel, not "
        "which licensee it is, and cannot see a weak emitter hiding under the "
        "data floor")
    out["caveats"].append(
        "digital TV pictures are not decoded in this app — 8VSB video needs a "
        "full equalizer/trellis/RS/MPEG chain; analog video rasters DO get a "
        "recovered still (see the VIDEO check)")
    return out


def channel_for_frequency(center_mhz):
    """Which US TV RF channel contains this frequency? Returns (ch, low_mhz) or
    None. Inverse of baseline.fcc_broadcast.tv_channel_to_range."""
    f = float(center_mhz)
    for ch in range(2, 37):
        if 2 <= ch <= 4:
            lo = 54 + 6 * (ch - 2)
        elif 5 <= ch <= 6:
            lo = 76 + 6 * (ch - 5)
        elif 7 <= ch <= 13:
            lo = 174 + 6 * (ch - 7)
        else:
            lo = 470 + 6 * (ch - 14)
        if lo <= f < lo + 6:
            return ch, float(lo)
    return None


def encode_atsc(fs=8_000_000.0, seconds=0.05, channel_low_hz=None,
                capture_center_hz=None, pilot_db=12.0, snr_db=25.0, seed=0):
    """Synthesize an 8VSB-SHAPED test signal: flat band-limited noise across the
    5.381 MHz occupied width plus a CW pilot at the standard offset. This is a
    signature stand-in, not a real modulator — it exists so tests exercise the
    SAME detector against a known-good and a known-bad input.
    """
    rng = np.random.default_rng(seed)
    n = int(fs * seconds)
    if channel_low_hz is None:
        channel_low_hz = space = 585_000_000.0  # ch 34
    if capture_center_hz is None:
        capture_center_hz = channel_low_hz + CHANNEL_BW_HZ / 2.0
    t = np.arange(n) / fs

    # flat data: white noise shaped to the occupied band by FFT masking
    noise = (rng.standard_normal(n) + 1j * rng.standard_normal(n)) / np.sqrt(2.0)
    spec = np.fft.fft(noise)
    f = np.fft.fftfreq(n, 1.0 / fs)
    centre_bb = channel_low_hz + CHANNEL_BW_HZ / 2.0 - capture_center_hz
    mask = np.abs(f - centre_bb) <= (OCCUPIED_BW_HZ / 2.0)
    spec[~mask] = 0.0
    data = np.fft.ifft(spec)
    p = np.sqrt(np.mean(np.abs(data) ** 2)) or 1.0
    data = data / p

    # the pilot, at the standard offset above the lower channel edge
    pilot_bb = channel_low_hz + PILOT_OFFSET_HZ - capture_center_hz
    amp = 10.0 ** (pilot_db / 20.0) / np.sqrt(n / 16384.0)
    pilot = amp * np.exp(2j * np.pi * pilot_bb * t)

    floor = 10.0 ** (-snr_db / 20.0)
    bg = floor * (rng.standard_normal(n) + 1j * rng.standard_normal(n)) / np.sqrt(2.0)
    return (data + pilot + bg).astype(np.complex64)
