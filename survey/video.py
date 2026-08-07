"""Analog composite video (NTSC / PAL) sync confirmation + one recovered still.

A wireless camera is a top-tier surveillance threat, and until now the app could
only say a wideband FM carrier was "compatible with analog video" — a shape guess
off the waterfall. This module answers the question the operator actually has: is
that carrier carrying a VIDEO RASTER, or is it just wideband FM?

Phase 1 — CONFIRMATION (this is the point; the picture is a bonus):
FM-discriminate the carrier with demod._fm_discriminator — the SAME discriminator
the listen path, POCSAG, FLEX and OOK use — slice the sync tips out of the
demodulated composite baseband, and test that pulse train for the three structures
a video raster has and other wideband FM does not:
  * horizontal sync at the NTSC (15734.264 Hz) or PAL (15625 Hz) LINE rate — a
    sharp, PROMINENT autocorrelation peak at the line period. Prominence, not raw
    correlation: any slowly-varying baseband (voice FM) is highly correlated at a
    63 us lag, but only a pulse train PEAKS there. The period is then refined
    against a high multiple of that lag, which is what makes the NTSC/PAL split
    (0.7 % apart) decidable instead of a coin flip;
  * a vertical interval — the per-line sync DUTY jumps for the broad pulses once a
    field, so the duty sequence is itself periodic at 262.5 / 312.5 lines (or at
    525 / 625 lines when the lock lands on the interlaced FRAME);
  * sync-tip structure — pulses ~4.7 us wide taking a horizontal-blanking-sized
    slice of each line. This is what rejects a plain 15.7 kHz tone or squarewave,
    which has the right period and the wrong shape.

Phase 2 — a STILL, and only once Phase 1 holds: slice the demodulated baseband at
the measured line period into a 2-D luminance array and emit ONE greyscale PNG
(data URL, stdlib zlib — no new dependency). Luminance only: no colour burst, no
chroma, no audio subcarrier, and deliberately no playback. A recognizable still is
enough to confirm a camera; a video player is not this tool's job.

What it CANNOT do, so nobody discovers it in a client's house:
  * It cannot tell a wireless camera from a CRT/monitor raster emanation, a video
    sender relaying a TV, or a bench test pattern. All four are video rasters.
  * It needs the FM signal to fit the capture. A 2.4 GHz camera link deviates
    wider than a 2.4 MHz Pluto capture, so the tips can alias and the lock can
    fail on a signal that IS video — capture as wide as the radio allows, and
    read a miss as "not confirmed", never as "not a camera".
  * A field/frame lock needs ~70 ms of clean carrier; a shorter or fading capture
    gets a horizontal-only verdict, and says so.
  * The still is one field of luminance. No colour, no audio subcarrier, no
    playback, no motion — and it is contrast-stretched, so it is legible, not
    photometric.

Honesty note (Matt's pref #4, and the house rule the other decoders follow): a
line-rate lock is EVIDENCE OF A VIDEO RASTER, NOT PROOF OF A CAMERA. A CRT/monitor
emanation, a video sender relaying someone else's feed, or a bench test pattern
all produce this exact signature — every positive carries that caveat in its own
`caveats` list. And a miss is honest: a wideband FM carrier with no sync structure
returns NO message and says which test failed. This decoder never upgrades
"wideband FM" into "camera".

Pure numpy DSP, no scipy — same house rules as demod.py / pocsag.py / ook.py.
encode_video() lives here too so the tests synthesize a known raster and run it
through the SAME detector — see tests/test_video.py.
"""

import base64
import struct
import zlib

import numpy as np

from .demod import _decimate, _fm_discriminator

PROTOCOL = "ANALOG-VIDEO"

# ---- standards ----
NTSC_LINE_HZ = 15734.264        # 4.5 MHz / 286
PAL_LINE_HZ = 15625.0           # exactly 15.625 kHz (CCIR B/G/I)
NTSC_FIELD_LINES = 262.5        # interlaced 525-line frame, two fields
PAL_FIELD_LINES = 312.5         # interlaced 625-line frame, two fields
SYNC_NOMINAL_US = 4.7           # horizontal sync pulse width, both standards
_STANDARDS = (("NTSC", NTSC_LINE_HZ, NTSC_FIELD_LINES),
              ("PAL", PAL_LINE_HZ, PAL_FIELD_LINES))

# ---- working rate ----
WORKING_FS = 1_000_000.0        # decimation target for the demodulated baseband
MIN_WORKING_FS = 200_000.0      # below this a 4.7 us sync pulse is under 1 sample
MAX_WORK_SAMPLES = 262_144      # ~0.26 s at WORKING_FS — 7+ NTSC frames, bounded compute
MIN_WORK_SAMPLES = 8192         # ~8 ms — fewer lines than this can't carry a lock

# ---- horizontal (line) gate ----
LINE_HZ_MIN, LINE_HZ_MAX = 11_000.0, 20_000.0   # search band around both standards
LINE_RATE_TOL = 0.0025          # 0.25 % — NTSC/PAL are 0.7 % apart, so this stays exclusive
LINE_LOCK_MIN = 0.25            # autocorrelation PROMINENCE needed to call a line lock
SYNC_FRAC = 0.15                # slice level, as a fraction of the sync-tip->peak-white span
MIN_SPAN_RAD = 1e-3             # smallest 1-99 pct baseband excursion worth slicing. The
                                #   discriminator returns rad/sample, so an UNMODULATED
                                #   carrier lands ~1e-7 (float rounding only) — without this
                                #   floor its quantization artifacts can fake a periodicity.
                                #   Even a 160 Hz-deviation FM signal clears it at 1 MHz.
SYNC_US_MIN, SYNC_US_MAX = 1.5, 10.0     # plausible measured sync width (nominal 4.7)
SYNC_DUTY_MIN, SYNC_DUTY_MAX = 0.01, 0.35   # sync tips as a fraction of total time

# ---- vertical (field) gate ----
FIELD_LAG_MIN, FIELD_LAG_MAX = 150, 700  # lines per field/frame worth searching
FIELD_LOCK_MIN = 0.15           # prominence needed to claim a vertical periodicity
FIELD_TOL = 0.02                # 2 % — 262.5 vs 312.5 are 19 % apart
VSYNC_DUTY_MIN = 0.30           # per-line sync duty that marks the vertical interval

# ---- frame recovery ----
ACTIVE_START_FRAC = 0.17        # sync + back porch is ~15 % of a line; start just after
ACTIVE_END_FRAC = 0.98          # stop before the front porch
FRAME_COLS = 320
FRAME_MIN_ROWS = 64


# ---------------------------------------------------------------- DSP front end
def fm_baseband(iq, fs: float, target_fs: float = WORKING_FS, max_out: int = 0):
    """Complex IQ -> (real FM-discriminated composite baseband, working_fs).

    Boxcar-decimate toward target_fs, then the same one-line discriminator
    demodulate() / POCSAG / FLEX use. The composite video waveform IS the
    instantaneous frequency of an FM video link, so this is the whole front end.

    `max_out` trims the INPUT to the samples that survive decimation, so a 2 s
    2.4 MHz analyze capture doesn't decimate 5 M samples we're about to throw
    away — a few frames is all any of this needs."""
    iq = np.asarray(iq, dtype=np.complex64)
    if iq.size < 16:
        raise ValueError("capture too short to decode")
    dec = max(1, int(round(fs / target_fs)))
    if max_out and iq.size > (max_out + 2) * dec:
        iq = iq[:(max_out + 2) * dec]
    x = _decimate(iq, dec)
    if x.size < 4:
        raise ValueError("capture too short to decode")
    return _fm_discriminator(x), fs / dec


def _autocorr(x: np.ndarray) -> np.ndarray:
    """Unbiased normalized autocorrelation via FFT (lag 0 == 1.0). Unbiased so the
    high-lag peaks used for period refinement aren't damped into the noise."""
    x = np.asarray(x, dtype=np.float64)
    x = x - float(np.mean(x))
    n = x.size
    if n < 8 or not np.any(x):
        return np.zeros(max(n, 1), dtype=np.float64)
    nfft = 1 << int(np.ceil(np.log2(2 * n)))
    spec = np.fft.rfft(x, nfft)
    ac = np.fft.irfft(spec * np.conj(spec), nfft)[:n]
    ac = ac / np.maximum(n - np.arange(n), 1.0)      # unbiased overlap normalization
    if ac[0] > 0:
        ac = ac / ac[0]
    return ac


def _prominence(ac: np.ndarray, lag: int) -> float:
    """How far the autocorrelation at `lag` stands above its own local level.

    THE discriminator of this module. A smoothly-varying baseband (analog voice FM,
    a slow drift) has a HIGH autocorrelation at a 63 us lag but no PEAK there — the
    local median tracks it and the prominence collapses to ~0. A sync pulse train
    peaks once per line period, so its prominence is what survives. The window is
    +/-0.75 lag: wide enough to hold the two adjacent valleys, narrow enough to
    exclude the neighbouring peaks."""
    if lag < 2 or lag >= ac.size:
        return 0.0
    half = max(2, int(round(0.75 * lag)))
    lo, hi = max(1, lag - half), min(ac.size, lag + half + 1)
    if hi - lo < 5:
        return 0.0
    return float(ac[lag] - np.median(ac[lo:hi]))


def _parabolic(y0: float, y1: float, y2: float) -> float:
    """Sub-sample offset of a peak from three samples around it (-0.5..+0.5)."""
    denom = y0 - 2.0 * y1 + y2
    if abs(denom) < 1e-18:
        return 0.0
    return float(np.clip(0.5 * (y0 - y2) / denom, -0.5, 0.5))


def _refine_period(ac: np.ndarray, p0: float, max_lag: int) -> float:
    """Sharpen a coarse period estimate by tracking the SAME peak at high multiples
    of the lag. A one-sample error at lag p is ~1.5 % at 63 samples/line — far too
    coarse to separate NTSC from PAL (0.7 %). The same one-sample error at lag 64p
    is 0.02 %. We walk the multiple up x4 at a time, each step re-finding the peak
    within +/-0.4 p of where the previous estimate predicts it, so the ladder can't
    slip onto a neighbouring peak. Falls back to the coarse value if it wanders."""
    p = float(p0)
    m = 1
    while True:
        m2 = m * 4
        c = int(round(m2 * p))
        w = max(2, int(round(0.4 * p)))
        lo, hi = c - w, c + w
        if lo < 2 or hi >= min(max_lag, ac.size - 1):
            break
        k = lo + int(np.argmax(ac[lo:hi + 1]))
        if k <= 0 or k + 1 >= ac.size:
            break
        p = (k + _parabolic(float(ac[k - 1]), float(ac[k]), float(ac[k + 1]))) / m2
        m = m2
    if p <= 0 or abs(p - p0) > 0.05 * p0:            # ladder slipped — don't trust it
        return float(p0)
    return float(p)


# ---------------------------------------------------------------- sync extraction
def _sync_track(bb: np.ndarray, polarity: float):
    """Demodulated baseband -> (soft sync indicator, binary sync mask, duty).

    `polarity` is +1 when the sync tips are the most NEGATIVE excursion (the usual
    composite convention) and -1 when the capture inverted them — a Pluto's LO side
    flips the discriminator sign, so decode_video tries BOTH and keeps the better
    lock rather than assuming one. The slice sits SYNC_FRAC of the way up from the
    tip (1st percentile) toward peak white (99th): sync tip to blanking is 40 of
    the 140 IRE sync-to-white span, so anything under ~0.28 stays below blanking
    even on a full-white picture, and 0.15 keeps margin at both ends — above the
    tips on a dark picture, below blanking on a bright one. Returns None when the
    baseband has no excursion at all (an unmodulated carrier discriminates to a
    constant)."""
    y = polarity * np.asarray(bb, dtype=np.float64)
    tip = float(np.percentile(y, 1.0))
    top = float(np.percentile(y, 99.0))
    span = top - tip
    if span <= MIN_SPAN_RAD:
        return None
    thr = tip + SYNC_FRAC * span
    mask = y <= thr
    duty = float(np.mean(mask))
    if not (0.002 <= duty <= 0.6):                   # degenerate slice — not a sync track
        return None
    soft = np.maximum(thr - y, 0.0)                  # rectified depth below the slice
    return soft, mask, duty


def _mask_runs(mask: np.ndarray):
    """Lengths (in samples) of the contiguous True runs — the sync pulse widths."""
    m = mask.astype(np.int8)
    if m.size == 0:
        return np.zeros(0, dtype=np.int64)
    edges = np.diff(np.concatenate(([0], m, [0])))
    starts = np.nonzero(edges == 1)[0]
    ends = np.nonzero(edges == -1)[0]
    if starts.size == 0 or ends.size == 0:
        return np.zeros(0, dtype=np.int64)
    return (ends - starts).astype(np.int64)


def _line_lock(soft: np.ndarray, wfs: float):
    """Find the line period from the sync indicator's autocorrelation.

    Scores EVERY candidate lag in the 11-20 kHz band by prominence (not by raw
    correlation) and keeps the best, then refines it against a high multiple.
    Returns (period_samples, line_rate_hz, prominence) or None."""
    n = soft.size
    ac = _autocorr(soft)
    lo = max(2, int(np.floor(wfs / LINE_HZ_MAX)))
    hi = int(np.ceil(wfs / LINE_HZ_MIN))
    hi = min(hi, ac.size // 3)
    if hi <= lo + 2:
        return None
    best_lag, best_prom = 0, -1.0
    for lag in range(lo, hi + 1):
        pr = _prominence(ac, lag)
        if pr > best_prom:
            best_prom, best_lag = pr, lag
    if best_lag <= 0:
        return None
    p = _refine_period(ac, float(best_lag), max_lag=max(4, n // 3))
    if p <= 1.0:
        return None
    return p, wfs / p, float(best_prom)


def _line_duty(mask: np.ndarray, period: float) -> np.ndarray:
    """Per-line fraction of samples sitting in sync. Ordinary lines read ~7 %
    (the horizontal sync pulse); the vertical interval's broad pulses invert that
    to ~80 %, so this sequence carries the FIELD periodicity."""
    n = mask.size
    nlines = int(n / period)
    if nlines < 8:
        return np.zeros(0, dtype=np.float64)
    cs = np.concatenate(([0.0], np.cumsum(mask.astype(np.float64))))
    starts = np.round(np.arange(nlines) * period).astype(np.int64)
    ends = np.minimum(np.round((np.arange(nlines) + 1) * period).astype(np.int64), n)
    width = np.maximum(ends - starts, 1)
    return (cs[ends] - cs[starts]) / width


def _field_lock(duty: np.ndarray):
    """Vertical periodicity, in LINES, from the per-line duty sequence.
    Returns (lag_lines, prominence) or None."""
    if duty.size < 2 * FIELD_LAG_MIN:
        return None
    ac = _autocorr(duty)
    hi = min(FIELD_LAG_MAX, duty.size // 2, ac.size - 1)
    if hi <= FIELD_LAG_MIN:
        return None
    best_lag, best_prom = 0, -1.0
    for lag in range(FIELD_LAG_MIN, hi + 1):
        pr = _prominence(ac, lag)
        if pr > best_prom:
            best_prom, best_lag = pr, lag
    if best_lag <= 0 or best_prom < FIELD_LOCK_MIN:
        return None
    return best_lag, float(best_prom)


def _match_standard(line_rate: float):
    """(name, nominal_line_hz, lines_per_field) for the standard this line rate
    matches within LINE_RATE_TOL, else (None, None, None). No 'closest match' —
    an unmatched rate is reported as unmatched."""
    for name, ref, field_lines in _STANDARDS:
        if abs(line_rate - ref) <= LINE_RATE_TOL * ref:
            return name, ref, field_lines
    return None, None, None


# ---------------------------------------------------------------- frame recovery
def _png_bytes(gray: np.ndarray) -> bytes:
    """8-bit greyscale PNG from a 2-D uint8 array. stdlib zlib/struct only — no
    imaging dependency is added to this app for one still."""
    h, w = gray.shape
    raw = b"".join(b"\x00" + gray[r].tobytes() for r in range(h))

    def _chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0)   # 8-bit, colour type 0
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", zlib.compress(raw, 6)) + _chunk(b"IEND", b""))


def _picture_start_line(duty: np.ndarray):
    """First picture line after a vertical interval: find the run of high-duty
    lines (the broad pulses), then step past it. Returns None if no vertical
    interval is visible — in which case we do NOT guess a frame boundary."""
    if duty.size < 32:
        return None
    thr = max(VSYNC_DUTY_MIN, 4.0 * float(np.median(duty)))
    hits = np.nonzero(duty > thr)[0]
    if hits.size == 0:
        return None
    start = int(hits[0])
    end = start
    for i in hits:
        if i - end <= 2:            # tolerate the equalizing-pulse gaps
            end = int(i)
        elif i > end:
            break
    return end + 2                  # skip the trailing equalizing lines


def _render_field(y: np.ndarray, duty: np.ndarray, period: float,
                  lines_per_field: float, cols: int = FRAME_COLS):
    """Slice the demodulated baseband into ONE field's worth of luminance rows.

    Rows are sampled on a fractional line grid (start + i*period) so a non-integer
    samples-per-line doesn't shear the picture, and each row is linearly
    interpolated across the active window between the porches. Contrast is
    stretched over the field's own 2nd..99.5th percentile — this is a legibility
    aid for the operator, not a calibrated luminance measurement.

    ONE FIELD only: that is every OTHER scan line of the interlaced frame, so the
    still is at half vertical resolution by construction. Returns (uint8 image,
    rows, cols) or None."""
    start = _picture_start_line(duty)
    if start is None:
        return None
    avail = duty.size - start - 1
    rows = int(min(avail, round(lines_per_field) - 12))
    if rows < FRAME_MIN_ROWS:
        return None
    frac = ACTIVE_START_FRAC + (np.arange(cols) + 0.5) * \
        (ACTIVE_END_FRAC - ACTIVE_START_FRAC) / cols
    pos = ((start + np.arange(rows))[:, None] + frac[None, :]) * period
    if float(pos.max()) >= y.size - 1:
        return None
    vals = np.interp(pos.ravel(), np.arange(y.size, dtype=np.float64), y)
    vals = vals.reshape(rows, cols)
    lo = float(np.percentile(vals, 2.0))
    hi = float(np.percentile(vals, 99.5))
    if hi - lo <= 1e-12:
        return None
    img = np.clip((vals - lo) / (hi - lo), 0.0, 1.0)
    return (img * 255.0).astype(np.uint8), rows, cols


# ---------------------------------------------------------------- top level
def _empty_result(note: str, stats: dict | None = None) -> dict:
    return {"protocol": PROTOCOL, "messages": [], "confidence": 0.0, "note": note,
            "stats": stats or {"line_rate_hz": None, "line_lock": 0.0,
                               "working_fs": None}}


def _confidence_word(c: float) -> str:
    return "strong" if c >= 0.75 else ("moderate" if c >= 0.5 else "weak")


def _analyse(bb: np.ndarray, wfs: float, polarity: float):
    """One polarity hypothesis: sync track -> line lock -> duty/width measurements.
    Returns a dict of measurements (arrays included, for the caller) or None."""
    track = _sync_track(bb, polarity)
    if track is None:
        return None
    soft, mask, duty = track
    lock = _line_lock(soft, wfs)
    if lock is None:
        return None
    period, line_rate, prom = lock
    runs = _mask_runs(mask)
    width_us = float(np.median(runs)) * 1e6 / wfs if runs.size else 0.0
    return {"polarity": polarity, "period": period, "line_rate": line_rate,
            "line_lock": prom, "sync_duty": duty, "sync_width_us": width_us,
            "mask": mask, "n_pulses": int(runs.size)}


def decode_video(iq, fs: float, frame: bool = True) -> dict:
    """Top level: complex IQ -> an analog-video verdict (and one still).

    Returns {"protocol", "messages", "confidence", "note", "stats"}. `messages` is
    a single-entry list ONLY when the capture clears every gate: a prominent
    autocorrelation peak at a line rate that matches NTSC or PAL, with a
    plausible sync pulse width and duty. Anything else returns NO message and a
    note saying which structure was missing — a wideband FM carrier with no sync
    is reported as exactly that.

    The message carries: standard, line_rate_hz, line_period_us, field_rate_hz,
    lines_per_field, sync_width_us, sync_duty, sync_polarity, line_lock,
    field_lock, confidence, evidence[], caveats[], and frame_png (a
    data:image/png;base64 greyscale still) when one could be recovered.

    Mirrors demod/pocsag/ook: only too-short input (<16 samples) raises."""
    iq = np.asarray(iq, dtype=np.complex64)
    if iq.size < 16:
        raise ValueError("capture too short to decode")
    bb, wfs = fm_baseband(iq, fs, max_out=MAX_WORK_SAMPLES)
    stats = {"line_rate_hz": None, "line_lock": 0.0, "working_fs": round(wfs, 1)}
    if wfs < MIN_WORKING_FS:
        return _empty_result(
            "capture rate too low to resolve video sync — a 4.7 us sync pulse needs "
            "at least %d kHz of baseband (this capture demodulates to %.0f kHz)"
            % (int(MIN_WORKING_FS / 1e3), wfs / 1e3), stats)
    if bb.size < MIN_WORK_SAMPLES:
        return _empty_result(
            "capture too short to test for video sync — need ~%d ms, got %.1f ms"
            % (int(1000 * MIN_WORK_SAMPLES / wfs), 1000.0 * bb.size / wfs), stats)
    bb = bb[:MAX_WORK_SAMPLES]

    # Both discriminator polarities: a Pluto's LO side decides whether the sync
    # tips come out as the negative or positive excursion. Keep the better lock
    # rather than assuming a convention.
    best = None
    for polarity in (1.0, -1.0):
        cand = _analyse(bb, wfs, polarity)
        if cand and (best is None or cand["line_lock"] > best["line_lock"]):
            best = cand
    if best is None:
        return _empty_result("no usable sync track — the demodulated baseband has no "
                             "sync-tip excursion (unmodulated carrier or dead channel)",
                             stats)

    stats["line_rate_hz"] = round(best["line_rate"], 1)
    stats["line_lock"] = round(best["line_lock"], 3)
    stats["sync_width_us"] = round(best["sync_width_us"], 2)
    stats["sync_duty"] = round(best["sync_duty"], 4)

    # ---- GATE 1: a PROMINENT line-rate autocorrelation peak --------------------
    if best["line_lock"] < LINE_LOCK_MIN:
        return _empty_result(
            "no video sync found — the demodulated baseband carries no line-rate "
            "periodicity (best autocorrelation prominence %.2f at %.0f Hz, need "
            "%.2f). If this carrier is wideband FM it stays UNCONFIRMED, not cleared"
            % (best["line_lock"], best["line_rate"], LINE_LOCK_MIN),
            stats)

    # ---- GATE 2: the line rate must BE NTSC or PAL -----------------------------
    std, ref_hz, field_lines = _match_standard(best["line_rate"])
    if std is None:
        return _empty_result(
            "periodic structure at %.0f Hz in the demodulated baseband, but that is "
            "neither the NTSC (15734 Hz) nor the PAL (15625 Hz) line rate — not "
            "called as video" % best["line_rate"], stats)

    # ---- GATE 3: sync-tip SHAPE, not just period -------------------------------
    # A 15.7 kHz tone or squarewave has the right period and the wrong shape; its
    # "pulses" run a third of a line. Real horizontal sync is ~4.7 us.
    if not (SYNC_US_MIN <= best["sync_width_us"] <= SYNC_US_MAX):
        return _empty_result(
            "periodicity at the %s line rate (%.0f Hz) but the pulses are %.1f us "
            "wide — horizontal sync is ~%.1f us, so this is periodic interference, "
            "not composite video" % (std, best["line_rate"], best["sync_width_us"],
                                     SYNC_NOMINAL_US), stats)
    if not (SYNC_DUTY_MIN <= best["sync_duty"] <= SYNC_DUTY_MAX):
        return _empty_result(
            "periodicity at the %s line rate (%.0f Hz) but the sync tips occupy "
            "%.0f%% of the capture — horizontal blanking is a few percent, so this "
            "is not composite video" % (std, best["line_rate"],
                                        100.0 * best["sync_duty"]), stats)

    # ---- vertical structure (supporting evidence, not a gate) ------------------
    period = best["period"]
    duty = _line_duty(best["mask"], period)
    field = _field_lock(duty)
    field_rate = field_lock_val = None
    lines_measured = None
    vertical_note = ""
    if field is not None:
        lag, fprom = field
        field_lock_val = round(fprom, 3)
        lines_measured = lag
        for lines, kind in ((field_lines, "field"), (2.0 * field_lines, "frame")):
            if abs(lag - lines) <= FIELD_TOL * lines:
                field_rate = best["line_rate"] / field_lines
                vertical_note = ("vertical structure repeats every %d lines (%s "
                                 "period, %s nominal %.1f) -> %.2f Hz field rate"
                                 % (lag, kind, std, lines, field_rate))
                break
        if field_rate is None:
            vertical_note = ("vertical periodicity at %d lines does NOT match the %s "
                             "field (%.1f) or frame (%.1f) structure" %
                             (lag, std, field_lines, 2.0 * field_lines))
    elif duty.size < 2 * FIELD_LAG_MIN:
        vertical_note = ("capture too short to test the field rate — need ~%d ms for "
                         "two frames (got %.0f ms)"
                         % (int(1000 * 2 * 2 * field_lines / best["line_rate"]),
                            1000.0 * bb.size / wfs))
    else:
        vertical_note = "no vertical (field) periodicity found in the per-line sync duty"

    # ---- Phase 2: one still ---------------------------------------------------
    frame_png = frame_rows = frame_cols = None
    if frame:
        rendered = _render_field(best["polarity"] * bb, duty, period, field_lines)
        if rendered is not None:
            img, frame_rows, frame_cols = rendered
            frame_png = ("data:image/png;base64,"
                         + base64.b64encode(_png_bytes(img)).decode())

    # ---- evidence + confidence -------------------------------------------------
    evidence = [
        "horizontal sync periodicity at %.1f Hz (%s nominal %.1f Hz, %+.2f%%); "
        "autocorrelation prominence %.2f at the line period"
        % (best["line_rate"], std, ref_hz,
           100.0 * (best["line_rate"] - ref_hz) / ref_hz, best["line_lock"]),
        "sync pulses %.1f us wide (NTSC/PAL horizontal sync is %.1f us)"
        % (best["sync_width_us"], SYNC_NOMINAL_US),
        "sync tips occupy %.1f%% of the capture — horizontal-blanking sized"
        % (100.0 * best["sync_duty"]),
    ]
    if vertical_note:
        evidence.append(vertical_note)
    if frame_png:
        evidence.append("a %dx%d luminance still was sliced at the measured line "
                        "period (single field — every other scan line)"
                        % (frame_cols, frame_rows))

    conf = 0.40 + 0.15 * min(1.0, best["line_lock"] / 0.6)
    if field_rate is not None:
        conf += 0.25
    if abs(best["sync_width_us"] - SYNC_NOMINAL_US) <= 2.5:
        conf += 0.10
    if 0.03 <= best["sync_duty"] <= 0.30:
        conf += 0.05
    if frame_png:
        conf += 0.05
    conf = round(min(conf, 0.95), 2)

    caveats = [
        "a sync lock is evidence of a VIDEO RASTER, not proof of a camera — a "
        "CRT/monitor emanation, a video sender relaying another feed, or a bench "
        "test pattern all produce this same signature",
        "luminance only: no colour decoding, no audio subcarrier, no playback — "
        "the still is one field (half vertical resolution), contrast-stretched",
    ]
    if field_rate is None:
        caveats.append("horizontal lock only — the vertical/field structure was not "
                       "confirmed, so the standard call rests on the line rate alone")
    if frame_png is None:
        caveats.append("no still recovered — no vertical interval was visible in this "
                       "capture, so no frame boundary could be located without guessing")

    msg = {
        "standard": std,
        "line_rate_hz": round(best["line_rate"], 1),
        "line_period_us": round(1e6 / best["line_rate"], 3),
        "field_rate_hz": round(field_rate, 2) if field_rate else None,
        "lines_per_field": field_lines if field_rate else None,
        "lines_measured": lines_measured,
        "sync_width_us": round(best["sync_width_us"], 2),
        "sync_duty": round(best["sync_duty"], 4),
        "sync_polarity": "negative" if best["polarity"] > 0 else "positive",
        "sync_pulses": best["n_pulses"],
        "line_lock": round(best["line_lock"], 3),
        "field_lock": field_lock_val,
        "confidence": conf,
        "evidence": evidence,
        "caveats": caveats,
        "frame_png": frame_png,
        "frame_rows": frame_rows,
        "frame_cols": frame_cols,
    }
    stats.update({"field_lines": lines_measured, "field_lock": field_lock_val,
                  "frame_recovered": bool(frame_png)})
    note = ("%s composite video sync structure — %s confidence (%.2f): line %.1f Hz%s. "
            "Evidence of an analog video raster on this carrier; NOT a confirmed "
            "camera identification."
            % (std, _confidence_word(conf), conf, best["line_rate"],
               ", field %.2f Hz" % field_rate if field_rate else
               ", field rate unconfirmed"))
    return {"protocol": PROTOCOL, "messages": [msg], "confidence": conf,
            "note": note, "stats": stats}


def looks_like_video(feats: dict, bw_khz_hint: float = None) -> bool:
    """Cheap pre-gate from identify.iq_features() output: a WIDE, CONTINUOUS
    carrier. An analog video link is continuously transmitting and occupies MHz,
    never the tens of kHz a bug or remote does.

    Deliberately a wide net: identify.classify_modulation() tests the flat/wide/
    strong 'ofdm_wideband' branch BEFORE its 'analog_video' branch, so a real
    camera can arrive here labelled OFDM. Excluding that class to save a decode
    would lose the very signals this module exists for. The narrowband/keyed
    classes ARE excluded — they can't be a video raster at any bandwidth — and
    decode_video's own sync gate is the real filter.

    `bw_khz_hint` — pass the width the SWEEP measured when you have it, because
    iq_features UNDER-READS the width of an FM carrier and this gate then throws
    away real cameras. An FM signal is constant-envelope and instantaneously
    narrow: over a short IQ slice the carrier has not yet swept its full
    deviation, so occupied_bw_khz reads a fraction of the true occupancy.
    Measured 2026-08-07 on a synthetic NTSC sender with 2 MHz deviation, against
    a decoder that locks on it and returns a picture: 78 kHz over a 0.41 ms
    slice, 117 kHz over 1.6 ms, 723 kHz over 6.6 ms — all under this gate's
    1 MHz threshold, so route_decode never tried video and the camera came back
    labelled an OOK/FSK device. A sweep's max-hold width does not have that
    problem. The hint only ever RAISES the measured width, so callers that do
    not pass one behave exactly as before.

    The narrowband/keyed exclusion is likewise skipped when the hint says
    wideband — those classes are what a mis-measured FM carrier gets labelled.
    """
    if not feats:
        return False
    if feats.get("signal_present") is False:
        return False
    hint = float(bw_khz_hint or 0.0)
    wide_by_hint = hint >= 1000.0
    if not wide_by_hint and feats.get("modulation_class") in ("fsk", "ook",
                                                             "noise_floor"):
        return False
    bw = max(feats.get("occupied_bw_khz", 0.0) or 0.0, hint)
    duty = feats.get("duty_cycle")
    duty = 1.0 if duty is None else duty
    cont = feats.get("continuity", "")
    if wide_by_hint:
        # the sweep says this occupies MHz; let the sync gate decide, rather
        # than a duty/continuity read taken from a sub-millisecond slice
        return True
    return bool(bw >= 1000.0 and (cont == "continuous" or duty >= 0.85))


# ---------------------------------------------------------------- synth / encoder
def _pattern(x: np.ndarray, row: np.ndarray, rows_total: float) -> np.ndarray:
    """Test-card luminance (0 = black, 1 = white) at horizontal position x in
    [0,1) on picture row `row`: vertical bars + a vertical gradient + a bright
    centre block, so a recovered still is visibly a picture and not noise."""
    yy = row / max(rows_total, 1.0)
    bars = (np.floor(x * 8.0).astype(np.int64) % 2) * 0.55
    val = 0.15 + bars + 0.30 * yy
    box = (np.abs(x - 0.5) < 0.12) & (np.abs(yy - 0.5) < 0.12)
    return np.clip(np.where(box, 0.95, val), 0.0, 1.0)


def encode_video(standard: str = "NTSC", fields: int = 6, fs: float = 2_000_000.0,
                 deviation_hz: float = 200_000.0, invert: bool = False,
                 noise: float = 0.0, seed: int = 0, blank_picture: bool = False):
    """Synthesize complex IQ for an FM-modulated composite video raster.

    A test fixture, and honest about it: ACCURATE in everything the detector reads
    — line period (63.556 us NTSC / 64.0 us PAL), 4.7 us horizontal sync, the
    half-line vertical interval (6 equalizing + 6 broad + 6 equalizing pulses),
    262.5 / 312.5 lines per field and the resulting interlace — and SIMPLIFIED in
    everything it doesn't: no colour burst, no audio subcarrier, nominal porch
    widths, and a stylized (not broadcast-exact) equalizing-pulse count.

    The whole field is laid out on a HALF-LINE grid, which is how a real raster
    gets its half-line field offset for free and why the frame period comes out at
    exactly 525 / 625 lines with no accumulated drift.

    invert=True flips the composite so the sync tips come out as the POSITIVE
    excursion — the same thing an LO on the other side of the carrier does to a
    real capture, and what the decoder's polarity search must survive."""
    std = (standard or "NTSC").upper()
    if std == "NTSC":
        line_us, field_lines = 1e6 / NTSC_LINE_HZ, NTSC_FIELD_LINES
    elif std == "PAL":
        line_us, field_lines = 1e6 / PAL_LINE_HZ, PAL_FIELD_LINES
    else:
        raise ValueError("standard must be NTSC or PAL")
    half_us = 0.5 * line_us
    half_per_field = int(round(2 * field_lines))        # 525 / 625 half-lines
    sync_us = SYNC_NOMINAL_US
    bp_us, fp_us = 4.7, 1.5                             # back / front porch
    eq_us = 2.3                                         # equalizing pulse
    serr_us = 4.7                                       # broad-pulse serration
    active_us = line_us - sync_us - bp_us - fp_us
    sync_level, blank_level = -0.30, 0.0                # sync tip / blanking (IRE-ish)

    total_us = fields * half_per_field * half_us
    n = int(total_us * 1e-6 * fs)
    if n < 16:
        raise ValueError("too few fields to synthesize")
    t_us = np.arange(n, dtype=np.float64) * (1e6 / fs)
    hi = np.floor(t_us / half_us).astype(np.int64)
    phase = t_us - hi * half_us
    hif = hi % half_per_field
    parity = (hi // half_per_field) % 2

    comp = np.full(n, blank_level, dtype=np.float64)
    eq = (hif < 6) | ((hif >= 12) & (hif < 18))         # pre/post equalizing pulses
    comp[eq & (phase < eq_us)] = sync_level
    broad = (hif >= 6) & (hif < 12)                     # vertical sync (broad pulses)
    comp[broad & (phase < half_us - serr_us)] = sync_level

    act = hif >= 18                                     # active lines
    pos = phase + ((hif - 18) % 2) * half_us            # position within the full line
    comp[act & (pos < sync_us)] = sync_level
    if not blank_picture:
        pic = act & (pos >= sync_us + bp_us) & (pos < line_us - fp_us)
        if np.any(pic):
            x = (pos[pic] - (sync_us + bp_us)) / active_us
            row = 2 * ((hif[pic] - 18) // 2) + parity[pic]     # interlaced row index
            comp[pic] = _pattern(x, row.astype(np.float64), 2 * field_lines)

    if invert:
        comp = -comp
    iq = np.exp(1j * 2 * np.pi * np.cumsum(deviation_hz * comp) / fs).astype(np.complex64)
    if noise > 0.0:
        rng = np.random.default_rng(seed)
        iq = iq + (noise * (rng.standard_normal(iq.size)
                            + 1j * rng.standard_normal(iq.size))).astype(np.complex64)
    return iq.astype(np.complex64)
