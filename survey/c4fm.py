"""C4FM / 4-level FSK symbol recovery — the shared front end for DMR and P25 Phase 1.

C4FM is the 4-level continuous-phase FSK carrying both DMR (ETSI TS 102 361) and
P25 Phase 1 (TIA-102): 4800 symbols/s, two bits per symbol, on four frequency-
deviation rails. TIA-102 and ETSI use the SAME rail -> dibit mapping, so one
recovery layer serves both protocols:

      rail          deviation (P25 / DMR)     dibit (MSB,LSB)
      +3 outer +    +1800 Hz / +1944 Hz       01
      +1 inner +     +600 Hz /  +648 Hz       00
      -1 inner -     -600 Hz /  -648 Hz       10
      -3 outer -    -1800 Hz / -1944 Hz       11

Chain: complex IQ -> boxcar decimate toward ~48 kHz -> demod._fm_discriminator()
(the SAME instantaneous-frequency step the FM listen path and the POCSAG/FLEX
decoders use — deliberately NOT a second implementation) -> root-raised-cosine
matched filter -> coarse symbol-phase scan -> Gardner timing recovery (2nd-order,
so a symbol-rate offset is tracked, not just a phase offset) -> 4-level slicer
with a least-squares fit of (dc, rail spacing).

PUBLIC API — frozen; survey/dmr.py and survey/p25.py are built on it:
    demod_c4fm(iq, fs, ...)                 -> result dict (see below)
    symbols_from_baseband(bb, wfs, ...)     -> same dict, for callers that already
                                               have a discriminated baseband
    dibits_to_bits(d) / bits_to_dibits(b)   -> MSB-first bit <-> dibit conversion
    invert_dibits(d)                        -> polarity flip (see NOTE below)
    sliding_words(d, n) / popcount(x)       -> vectorised sync-search primitives
    find_pattern(d, pattern, n, max_err)    -> dibit-aligned pattern search
    encode_c4fm(dibits, fs, ...)            -> synthetic IQ (tests / self-check)
    looks_like_c4fm(feats)                  -> cheap identify.iq_features() pre-gate

result dict keys:
    dibits (uint8 0..3), bits (uint8, 2 per dibit, MSB first), symbols (float,
    the fitted-scale soft values), levels (int8 in {-3,-1,1,3}), quality (0..1 —
    the headline confidence, = fit x coverage), fit (constellation tightness),
    coverage (how many of the four rails the stream actually visits), rails_used,
    evm, dc, spacing, sps, working_fs, symbol_rate, n_symbols, inverted, note.
    A garbage capture returns empty arrays and quality 0.0 — it never raises.

NOTE ON POLARITY — an inverted spectrum (wrong LO side) negates every rail, which
maps dibit d -> d ^ 0b10. It is NOT a complement of the bit stream: only the MSB
of each dibit flips. Use invert_dibits() / the `invert` argument; never `bits ^ 1`.
That mistake silently corrupts every sync search downstream.

VALIDATION STATUS (Matt's pref #4 — say what is proven): NOTHING here has been
run against a real off-air C4FM signal. Every assertion in tests/test_c4fm.py is
driven by encode_c4fm() in this same file, i.e. synthetic IQ, plus negative cases
(noise, analog FM, 2-FSK) that must NOT produce a confident symbol stream. The
rail/dibit table, 4800 sym/s rate and RRC roll-off are taken from the published
specs; the timing loop is generic DSP. Treat quality<1.0 numbers from the field
as unknown until a live capture has been decoded and cross-checked.
"""

import numpy as np

from .demod import _decimate, _fm_discriminator

# ---------------------------------------------------------------- constants
SYMBOL_RATE = 4800.0        # symbols/s — identical for DMR and P25 Phase 1
DEVIATION_HZ = 1800.0       # OUTER rail deviation (P25 C4FM; DMR is 1944 Hz).
                            #   Only the ratio matters to the slicer — it fits the
                            #   observed spacing — so this is an encoder default.
WORKING_FS = 48000.0        # decimation target: 10 samples/symbol at 4800 Bd
RRC_BETA = 0.2              # TIA-102 C4FM uses a raised-cosine with alpha = 0.2
RRC_SPAN = 12               # matched-filter length in symbols. 8 looks tempting but
                            #   truncating an alpha=0.2 RRC that hard leaves ~2 % ISI
                            #   at the 4-symbol lag, which alone caps a NOISELESS
                            #   round trip at quality ~0.84; 12 drops it to ~0.02 %.
MIN_SPS = 4.0               # below this the timing loop has nothing to interpolate

GARDNER_GAIN = 0.02         # timing-phase loop gain (x normalised to unit RMS)
GARDNER_RATE_GAIN = 4e-4    # 2nd-order (period) integrator gain; ~gain/50
GARDNER_MAX_ADJ = 0.05      # per-symbol phase correction clip, in symbol periods
GARDNER_RATE_LIMIT = 0.05   # sps_est is clamped to +/- 5 % of nominal
COARSE_SUBSTEPS = 4         # sub-sample resolution of the initial phase scan
FINE_PHASE_STEPS = 40       # data-aided phase polish resolution (1/40 of a symbol)

# rail order used everywhere below: outer+, inner+, inner-, outer-
LEVELS = (3, 1, -1, -3)
# dibit carried by each rail (TIA-102 / ETSI TS 102 361-1 agree)
DIBIT_FOR_LEVEL = {3: 0b01, 1: 0b00, -1: 0b10, -3: 0b11}
LEVEL_FOR_DIBIT = {0b01: 3, 0b00: 1, 0b10: -1, 0b11: -3}
# array forms, indexed by dibit value 0..3 and by rail index 0..3
_LEVEL_BY_DIBIT = np.array([1, 3, -1, -3], dtype=np.int8)
_DIBIT_BY_RAIL = np.array([1, 0, 2, 3], dtype=np.uint8)

# quality = fit x coverage.
#   fit      = 1 - (rms residual / half-rail-spacing) / QUALITY_EVM_FLOOR, clipped
#              to [0,1]. A residual of half the decision distance means the eye is
#              shut, so that scores 0: noise and analog FM land there.
#   coverage = (rails_used - 1) / 3, where a rail counts as used once it holds
#              RAIL_MIN_OCCUPANCY of the symbols. Tightness ALONE is not evidence
#              of C4FM: an unmodulated carrier parks every symbol on one rail and
#              fits it perfectly (fit 1.00), and 2-FSK fits two rails perfectly
#              (fit ~0.68). Requiring all four rails to be visited is the cheap,
#              scale-free structural test that separates 4-level from those.
QUALITY_EVM_FLOOR = 0.5
RAIL_MIN_OCCUPANCY = 0.02

_POP8 = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)


# ---------------------------------------------------------------- bit plumbing
def dibits_to_bits(dibits) -> np.ndarray:
    """dibit stream -> bit stream, MSB of each dibit first (on-air order)."""
    d = np.asarray(dibits, dtype=np.uint8)
    out = np.empty(d.size * 2, dtype=np.uint8)
    out[0::2] = (d >> 1) & 1
    out[1::2] = d & 1
    return out


def bits_to_dibits(bits) -> np.ndarray:
    """bit stream (MSB first) -> dibit stream. A trailing odd bit is dropped."""
    b = np.asarray(bits, dtype=np.uint8)
    n = (b.size // 2) * 2
    return ((b[0:n:2].astype(np.uint8) << 1) | b[1:n:2]).astype(np.uint8)


def invert_dibits(dibits) -> np.ndarray:
    """Polarity flip: every rail negated, i.e. dibit ^ 0b10. See the module note —
    this is NOT a bit-stream complement."""
    return (np.asarray(dibits, dtype=np.uint8) ^ 0b10).astype(np.uint8)


def popcount(x):
    """Hamming weight of an int, or elementwise over a uint64 numpy array."""
    if isinstance(x, np.ndarray):
        b = np.ascontiguousarray(x, dtype=np.uint64).view(np.uint8)
        return _POP8[b].reshape(-1, 8).sum(axis=1)
    return bin(int(x) & 0xFFFFFFFFFFFFFFFF).count("1")


def sliding_words(dibits, n_dibits: int) -> np.ndarray:
    """Every length-`n_dibits` window of the dibit stream packed MSB-dibit-first
    into a uint64 (so a 24-dibit / 48-bit sync word compares in one XOR).
    Returns an array of length len(dibits)-n_dibits+1 (empty if too short)."""
    d = np.asarray(dibits, dtype=np.uint8)
    if n_dibits < 1 or n_dibits > 32 or d.size < n_dibits:
        return np.zeros(0, dtype=np.uint64)
    n = d.size - n_dibits + 1
    w = np.zeros(n, dtype=np.uint64)
    for k in range(n_dibits):
        w = (w << np.uint64(2)) | d[k:k + n].astype(np.uint64)
    return w


def dibit_invert_mask(n_dibits: int) -> int:
    """0b10 repeated `n_dibits` times — XOR a packed window with this to get the
    opposite-polarity window (see invert_dibits)."""
    m = 0
    for _ in range(n_dibits):
        m = (m << 2) | 0b10
    return m


def find_pattern(dibits, pattern: int, n_dibits: int, max_err: int = 0,
                 allow_inverted: bool = True):
    """Dibit-aligned search for a packed sync `pattern` of `n_dibits` symbols.

    Returns [(dibit_index, inverted, n_bit_errors), ...] in stream order. Searching
    on DIBIT boundaries (never half-symbol) is what makes a C4FM sync hunt cheap
    and correct — a symbol stream has no sub-symbol alignment to slide through."""
    w = sliding_words(dibits, n_dibits)
    if w.size == 0:
        return []
    hits = []
    d0 = popcount(w ^ np.uint64(pattern & 0xFFFFFFFFFFFFFFFF))
    cand = np.nonzero(d0 <= max_err)[0]
    for i in cand:
        hits.append((int(i), False, int(d0[i])))
    if allow_inverted:
        pinv = pattern ^ dibit_invert_mask(n_dibits)
        d1 = popcount(w ^ np.uint64(pinv & 0xFFFFFFFFFFFFFFFF))
        for i in np.nonzero(d1 <= max_err)[0]:
            hits.append((int(i), True, int(d1[i])))
    hits.sort(key=lambda h: (h[0], h[2]))
    return hits


# ---------------------------------------------------------------- DSP front end
def fm_discriminate(iq, fs: float, target_fs: float = WORKING_FS):
    """Complex IQ -> (real FM-discriminated baseband, working_fs). Same shape as
    pocsag.fm_discriminate / flex.fm_discriminate: boxcar-decimate toward
    target_fs, then demod._fm_discriminator()."""
    iq = np.asarray(iq, dtype=np.complex64)
    if iq.size < 16:
        raise ValueError("capture too short to decode")
    dec = max(1, int(round(fs / target_fs)))
    x = _decimate(iq, dec)
    if x.size < 4:
        raise ValueError("capture too short to decode")
    return _fm_discriminator(x), fs / dec


def rrc_taps(sps: float, span: int = RRC_SPAN, beta: float = RRC_BETA) -> np.ndarray:
    """Root-raised-cosine matched filter, unit energy, odd length (symmetric ->
    integer group delay). The C4FM transmit filter is a full raised cosine, so a
    receive RRC is not the exact matched filter; it is the standard practical
    choice and encode_c4fm() shapes with the same RRC, making the synthetic
    cascade RRC*RRC = raised cosine, i.e. ISI-free at the symbol instants."""
    n = int(round(span * sps))
    if n % 2 == 0:
        n += 1
    n = max(n, 3)
    t = (np.arange(n) - (n - 1) / 2.0) / float(sps)
    h = np.empty(n, dtype=np.float64)
    for i, ti in enumerate(t):
        if abs(ti) < 1e-9:
            h[i] = 1.0 - beta + 4.0 * beta / np.pi
        elif beta > 0 and abs(abs(ti) - 1.0 / (4.0 * beta)) < 1e-9:
            h[i] = (beta / np.sqrt(2.0)) * (
                (1.0 + 2.0 / np.pi) * np.sin(np.pi / (4.0 * beta))
                + (1.0 - 2.0 / np.pi) * np.cos(np.pi / (4.0 * beta)))
        else:
            num = (np.sin(np.pi * ti * (1.0 - beta))
                   + 4.0 * beta * ti * np.cos(np.pi * ti * (1.0 + beta)))
            den = np.pi * ti * (1.0 - (4.0 * beta * ti) ** 2)
            h[i] = num / den
    nrm = np.sqrt(np.sum(h * h))
    return h / (nrm if nrm > 0 else 1.0)


def _cubic(x, i: int, mu: float) -> float:
    """Catmull-Rom interpolation of x at i+mu (needs x[i-1..i+2])."""
    ym1, y0, y1, y2 = x[i - 1], x[i], x[i + 1], x[i + 2]
    c1 = 0.5 * (y1 - ym1)
    c2 = ym1 - 2.5 * y0 + 2.0 * y1 - 0.5 * y2
    c3 = 0.5 * (y2 - ym1) + 1.5 * (y0 - y1)
    return ((c3 * mu + c2) * mu + c1) * mu + y0


def _coarse_phase(x: np.ndarray, sps: float) -> float:
    """Initial symbol phase = the offset whose sample set has the most-open eye
    (largest mean |amplitude|). Scanned at COARSE_SUBSTEPS per sample and scored
    on INTERPOLATED samples — rounding the trial instants to integers (what
    pocsag/flex do for their 2-level slicers) would make the sub-sample steps
    alias onto each other and hand Gardner a phase that is up to half a sample
    out, which a 4-level eye cannot absorb."""
    n = np.arange(x.size, dtype=np.float64)
    best_p, best_score = 0.0, -1.0
    steps = max(1, int(np.ceil(sps)) * COARSE_SUBSTEPS)
    for k in range(steps):
        p = k * sps / steps
        t = p + np.arange(int((x.size - 1 - p) / sps)) * sps
        if t.size < 4:
            continue
        score = float(np.mean(np.abs(np.interp(t, n, x))))
        if score > best_score:
            best_score, best_p = score, p
    return best_p


def _gardner(x: np.ndarray, sps: float, start: float,
             gain: float = GARDNER_GAIN, rate_gain: float = GARDNER_RATE_GAIN):
    """Gardner timing recovery over the matched-filtered baseband.

    e[n] = (y[n] - y[n-1]) * y[n-1/2]: zero when the midpoint sits on the zero
    crossing, signed by whether we are sampling early or late, and it needs no
    carrier phase (this is a real PAM stream after the discriminator). Second
    order: the phase correction handles jitter, the slow period integrator
    (sps_est) absorbs a symbol-RATE error so a clock offset does not walk the
    sampling instant out of the eye across a long burst.

    Returns (symbol samples, sample positions)."""
    n = x.size
    xl = x.tolist()                       # list indexing is ~3x faster than ndarray
    max_adj = GARDNER_MAX_ADJ * sps
    sps_est = float(sps)
    lo, hi = sps * (1.0 - GARDNER_RATE_LIMIT), sps * (1.0 + GARDNER_RATE_LIMIT)
    # The FIRST emitted symbol sits at pos-sps and the first midpoint at
    # pos-1.5*sps, and the cubic interpolator needs one sample either side, so
    # walk the start forward until every one of those is in range. (Skipping this
    # silently returned an EMPTY stream whenever the coarse phase landed near 0.)
    pos = float(start) + sps
    while pos - 1.5 * sps < 1.0:
        pos += sps
    syms, positions = [], []
    ppos = pos - sps
    pi = int(ppos)
    if pi < 1 or pi + 2 >= n:
        return np.zeros(0), np.zeros(0)
    prev = _cubic(xl, pi, ppos - pi)
    syms.append(prev)
    positions.append(ppos)
    while True:
        ci = int(pos)
        mpos = pos - 0.5 * sps_est
        mi = int(mpos)
        if ci < 1 or ci + 2 >= n or mi < 1 or mi + 2 >= n:
            break
        cur = _cubic(xl, ci, pos - ci)
        mid = _cubic(xl, mi, mpos - mi)
        syms.append(cur)
        positions.append(pos)
        err = (cur - prev) * mid
        adj = gain * err
        if adj > max_adj:
            adj = max_adj
        elif adj < -max_adj:
            adj = -max_adj
        sps_est -= rate_gain * err        # late  -> err>0 -> shorten the period
        if sps_est < lo:
            sps_est = lo
        elif sps_est > hi:
            sps_est = hi
        pos += sps_est - adj
        prev = cur
    return np.asarray(syms, dtype=np.float64), np.asarray(positions, dtype=np.float64)


def _fit_rails(s: np.ndarray, iters: int = 4):
    """Least-squares fit of (dc, spacing) to the 4-level constellation.

    A capture has an arbitrary DC (carrier offset) and an arbitrary rail spacing
    (deviation x discriminator gain), so the slicer cannot use fixed thresholds.
    Seed from the 2/98 percentile span, slice, re-fit s ~ dc + a*level, repeat.
    Returns (dc, a, levels) with a = HALF the distance between adjacent rails
    (rails sit at dc + a*{-3,-1,1,3}), or (0,0,None) if the fit is degenerate."""
    if s.size < 8:
        return 0.0, 0.0, None
    hi, lo = np.percentile(s, 98), np.percentile(s, 2)
    dc = 0.5 * (hi + lo)
    a = (hi - lo) / 6.0
    if not np.isfinite(a) or a <= 0:
        return 0.0, 0.0, None
    lv = None
    for _ in range(iters):
        lv = np.clip(np.round(((s - dc) / a + 3.0) / 2.0) * 2.0 - 3.0, -3.0, 3.0)
        if np.unique(lv).size < 2:
            return dc, a, lv
        m = np.column_stack([np.ones_like(lv), lv])
        try:
            coef, *_ = np.linalg.lstsq(m, s, rcond=None)
        except np.linalg.LinAlgError:      # pragma: no cover - numerical guard
            break
        if not np.all(np.isfinite(coef)) or coef[1] <= 0:
            break
        dc, a = float(coef[0]), float(coef[1])
    return dc, a, lv


def _score(syms: np.ndarray, trim: int = 0):
    """(dc, a, levels, fit, evm) for a candidate symbol-sample set, or None if the
    set has no usable multi-level structure. `fit` measures constellation
    tightness ONLY — rail coverage is applied afterwards, in
    symbols_from_baseband, so that the choice between timing hypotheses is made
    on tightness alone. (Folding coverage in here would let a badly-timed 2-FSK
    stream, whose smear spans all four rails, outscore a correctly-timed one.)

    `trim` drops that many symbols from EACH END before measuring the residual:
    a 'same'-mode matched filter runs off the end of the capture, so the first
    and last few symbols are always half-filtered. Their dibits are still emitted
    (they are usually still on the right side of a decision boundary) but letting
    a handful of guaranteed-bad symbols set the headline number would understate
    a clean capture by ~0.15."""
    dc, a, lv = _fit_rails(syms)
    if lv is None or a <= 0 or np.unique(lv).size < 2:
        return None
    resid = syms - (dc + a * lv)
    if trim > 0 and resid.size > 4 * trim:
        resid = resid[trim:resid.size - trim]
    rms_res = float(np.sqrt(np.mean(resid * resid)))
    fit = float(np.clip(1.0 - (rms_res / a) / QUALITY_EVM_FLOOR, 0.0, 1.0))
    # EVM against the RMS of an equiprobable {+-1,+-3} reference constellation
    evm = float(rms_res / (a * np.sqrt(5.0)))
    return dc, a, lv, fit, evm


def _rail_coverage(lv: np.ndarray):
    """(rails_used, coverage). See the QUALITY_EVM_FLOOR comment block: a stream
    that never leaves one rail is a carrier, two rails is 2-FSK, and only four
    earns full marks."""
    used = int(sum(1 for v in LEVELS
                   if float(np.mean(lv == v)) >= RAIL_MIN_OCCUPANCY))
    return used, float(np.clip((used - 1) / 3.0, 0.0, 1.0))


def _linear_clock(pos: np.ndarray):
    """(t0, period) from a least-squares straight line through the Gardner
    sampling instants.

    A radio's symbol clock is stable across a burst, so the true instants ARE a
    straight line and the loop's per-symbol wander is jitter we can throw away.
    The fit DELIBERATELY ignores the first eighth of the sequence — that stretch
    is the loop pulling in, and letting the acquisition transient into the
    regression tilts the line across the whole burst."""
    if pos.size < 16:
        return None
    k = np.arange(pos.size, dtype=np.float64)
    k0 = pos.size // 8
    m = np.column_stack([np.ones_like(k), k])
    try:
        coef, *_ = np.linalg.lstsq(m[k0:], pos[k0:], rcond=None)
    except np.linalg.LinAlgError:          # pragma: no cover - numerical guard
        return None
    if not np.all(np.isfinite(coef)) or coef[1] <= 0:
        return None
    return float(coef[0]), float(coef[1])


def _sample_at(x: np.ndarray, positions: np.ndarray) -> np.ndarray:
    """Vectorised Catmull-Rom sampling of x at fractional positions (in-range
    only). Vectorised because _refine_phase() re-samples the whole stream dozens
    of times; the per-symbol Python loop in _gardner() is fine because it is
    inherently sequential, this is not."""
    p = np.asarray(positions, dtype=np.float64)
    i = np.floor(p).astype(np.int64)
    ok = (i >= 1) & (i + 2 < x.size)
    i, mu = i[ok], (p[ok] - np.floor(p[ok]))
    if i.size == 0:
        return np.zeros(0)
    ym1, y0, y1, y2 = x[i - 1], x[i], x[i + 1], x[i + 2]
    c1 = 0.5 * (y1 - ym1)
    c2 = ym1 - 2.5 * y0 + 2.0 * y1 - 0.5 * y2
    c3 = 0.5 * (y2 - ym1) + 1.5 * (y0 - y1)
    return ((c3 * mu + c2) * mu + c1) * mu + y0


def _refine_phase(mf: np.ndarray, t0: float, period: float, trim: int):
    """Data-aided phase polish on top of the Gardner period estimate.

    Gardner gives an excellent PERIOD (a straight-line fit through its instants
    is good to ~1e-5 relative) but its steady-state phase wanders by a few
    hundredths of a symbol, and a 4-level eye is steep enough that 0.03 T of bias
    costs ~0.2 of quality. So sweep the phase across a whole symbol on the fitted
    clock and keep the offset whose constellation actually fits best. Returns
    (samples, score) or None."""
    if not np.isfinite(period) or period < MIN_SPS:
        return None
    lo = max(t0 - 0.5 * period, 1.5 + 0.5 * period)
    n_sym = int((mf.size - 4.0 - (lo + period)) / period)
    if n_sym < 8:
        return None
    k = np.arange(n_sym, dtype=np.float64)
    best = None
    for delta in np.linspace(0.0, 1.0, FINE_PHASE_STEPS, endpoint=False):
        s = _sample_at(mf, lo + delta * period + k * period)
        if s.size != n_sym:
            continue
        sc = _score(s, trim)
        if sc is not None and (best is None or sc[3] > best[1][3]):
            best = (s, sc)
    return best


def _empty_result(note: str, **kw) -> dict:
    r = {"dibits": np.zeros(0, dtype=np.uint8), "bits": np.zeros(0, dtype=np.uint8),
         "symbols": np.zeros(0), "levels": np.zeros(0, dtype=np.int8),
         "quality": 0.0, "fit": 0.0, "coverage": 0.0, "rails_used": 0,
         "evm": 1.0, "dc": 0.0, "spacing": 0.0, "sps": 0.0,
         "working_fs": 0.0, "symbol_rate": SYMBOL_RATE, "n_symbols": 0,
         "inverted": False, "note": note}
    r.update(kw)
    return r


def symbols_from_baseband(baseband, working_fs: float,
                          symbol_rate: float = SYMBOL_RATE, invert: bool = False,
                          beta: float = RRC_BETA, span: int = RRC_SPAN,
                          gardner_gain: float = GARDNER_GAIN) -> dict:
    """FM-discriminated baseband -> C4FM result dict (see module docstring).

    Never raises on garbage: an unusable capture comes back with an empty dibit
    stream, quality 0.0 and a note. The honesty gate lives in the protocol layers
    (a sync pattern + FEC that must validate), not here — this layer's job is to
    hand up a symbol stream AND an honest confidence number for it."""
    x = np.asarray(baseband, dtype=np.float64)
    sps = float(working_fs) / float(symbol_rate)
    if sps < MIN_SPS:
        return _empty_result("sample rate too low for %g Bd (%.1f sps)"
                             % (symbol_rate, sps), working_fs=working_fs, sps=sps)
    if x.size < sps * (span + 4):
        return _empty_result("capture shorter than the matched filter",
                             working_fs=working_fs, sps=sps)
    rms = float(np.sqrt(np.mean(x * x)))
    if not np.isfinite(rms) or rms <= 0:
        return _empty_result("flat baseband — no signal", working_fs=working_fs, sps=sps)
    x = x / rms                                  # unit RMS -> loop gain is scale-free
    if invert:
        x = -x
    mf = np.convolve(x, rrc_taps(sps, span, beta), mode="same")
    syms, pos = _gardner(mf, sps, _coarse_phase(mf, sps), gain=gardner_gain)
    if syms.size < 8:
        return _empty_result("too few symbols recovered", working_fs=working_fs, sps=sps)
    # Three timing hypotheses, keep whichever fits the constellation best:
    #   (a) the raw tracking loop — the only one that survives non-linear drift,
    #   (b) the loop's straight-line clock with a data-aided phase polish,
    #   (c) the same polish on the NOMINAL period, for the (common) case where a
    #       short/noisy capture left the loop's period estimate worse than 1:1.
    trim = max(1, int(span) // 2)
    scored = _score(syms, trim)
    clock = _linear_clock(pos)
    cands = []
    if clock is not None:
        cands.append(_refine_phase(mf, clock[0], clock[1], trim))
        cands.append(_refine_phase(mf, clock[0], sps, trim))
    for cand in cands:
        if cand is not None and (scored is None or cand[1][3] > scored[3]):
            syms, scored = cand[0], cand[1]
    if scored is None:
        return _empty_result("no multi-level structure — not C4FM",
                             working_fs=working_fs, sps=sps, n_symbols=int(syms.size))
    dc, a, lv, fit, evm = scored
    rails_used, coverage = _rail_coverage(lv)
    quality = float(fit * coverage)
    note = ""
    if rails_used < 3:
        note = ("only %d of 4 C4FM rails occupied — this looks like %s, not C4FM"
                % (rails_used, "an unmodulated carrier" if rails_used < 2
                   else "2-level FSK"))
    rail_idx = ((3.0 - lv) / 2.0).astype(int)    # +3,+1,-1,-3 -> 0,1,2,3
    dibits = _DIBIT_BY_RAIL[np.clip(rail_idx, 0, 3)]
    return {"dibits": dibits, "bits": dibits_to_bits(dibits),
            "symbols": (syms - dc) / a, "levels": lv.astype(np.int8),
            "quality": quality, "fit": fit, "coverage": coverage,
            "rails_used": rails_used, "evm": evm, "dc": float(dc),
            "spacing": float(a), "sps": sps, "working_fs": float(working_fs),
            "symbol_rate": float(symbol_rate), "n_symbols": int(syms.size),
            "inverted": bool(invert), "note": note}


def demod_c4fm(iq, fs: float, symbol_rate: float = SYMBOL_RATE,
               invert: bool = False, target_fs: float = WORKING_FS,
               beta: float = RRC_BETA, span: int = RRC_SPAN,
               gardner_gain: float = GARDNER_GAIN) -> dict:
    """Top level: complex IQ -> C4FM dibit stream + quality. See the module
    docstring for the result dict. Only a too-short capture (<16 samples) raises
    ValueError, mirroring demod.demodulate() / decode_pocsag()."""
    iq = np.asarray(iq, dtype=np.complex64)
    if iq.size < 16:
        raise ValueError("capture too short to decode")
    baseband, wfs = fm_discriminate(iq, fs, target_fs)
    return symbols_from_baseband(baseband, wfs, symbol_rate, invert=invert,
                                 beta=beta, span=span, gardner_gain=gardner_gain)


def looks_like_c4fm(feats: dict) -> bool:
    """Cheap pre-gate from identify.iq_features(): a 4-level (or at least multi-
    level) FSK at roughly 4.8 kBd inside a 12.5 kHz-class channel. Deliberately
    loose — the real gate is the protocol sync + FEC in dmr.py / p25.py."""
    if not feats:
        return False
    is_fsk = feats.get("modulation_class") == "fsk" or feats.get("fsk_states", 0) >= 2
    rate = feats.get("symbol_rate_kbd", 0.0) or 0.0
    bw = feats.get("occupied_bw_khz", 0.0) or 0.0
    near_rate = abs(rate - 4.8) <= 0.4 * 4.8 or rate == 0.0
    return bool(is_fsk and near_rate and 3.0 <= bw <= 30.0)


# ---------------------------------------------------------------- synth / encoder
def _shaped_freq(levels: np.ndarray, sps: float, shaping: str,
                 beta: float, span: int) -> np.ndarray:
    """Symbol rails -> a baseband frequency waveform at `sps` samples/symbol.
    'rrc' pulse-shapes (the realistic case, and RRC*RRC = Nyquist against the
    receiver's matched filter); 'rect' is a hard NRZ hold, used by the tests to
    prove the receiver is not merely matched to its own transmit filter."""
    n = int(round(levels.size * sps))
    if shaping == "rect":
        idx = np.clip((np.arange(n) / sps).astype(int), 0, levels.size - 1)
        return levels[idx].astype(np.float64)
    up = np.zeros(n, dtype=np.float64)
    pos = (np.arange(levels.size) * sps).round().astype(int)
    pos = pos[pos < n]
    up[pos] = levels[:pos.size]
    h = rrc_taps(sps, span, beta)
    return np.convolve(up, h, mode="same") * np.sqrt(sps)


def encode_c4fm(dibits, fs: float, symbol_rate: float = SYMBOL_RATE,
                deviation: float = DEVIATION_HZ, invert: bool = False,
                shaping: str = "rrc", beta: float = RRC_BETA, span: int = RRC_SPAN,
                noise: float = 0.0, seed: int = 0, work_fs: float = WORKING_FS,
                freq_offset: float = 0.0) -> np.ndarray:
    """Synthesize complex IQ for a C4FM transmission (test / self-check path).

    dibits: iterable of 0..3. The rail for each dibit is LEVEL_FOR_DIBIT, scaled
    so the OUTER rails sit at +/- `deviation`. The shaped baseband is built at
    ~work_fs (cheap: a 2.4 MHz RRC would be a 4000-tap convolution) and linearly
    resampled to fs — the waveform is band-limited to a few kHz, so it is grossly
    oversampled at 48 kHz and the interpolation is transparent.

    `freq_offset` adds a constant carrier offset in Hz, which lands as a DC bias
    on the discriminator — exactly the impairment the receiver's (dc, spacing) fit
    has to absorb."""
    d = np.asarray(list(dibits), dtype=np.uint8) & 0b11
    if d.size == 0:
        raise ValueError("no dibits to encode")
    levels = _LEVEL_BY_DIBIT[d].astype(np.float64)
    if invert:
        levels = -levels
    sps_w = max(4.0, float(work_fs) / float(symbol_rate))
    freq_w = _shaped_freq(levels, sps_w, shaping, beta, span) * (deviation / 3.0)
    n_out = int(round(d.size * float(fs) / float(symbol_rate)))
    t_w = np.arange(freq_w.size) / sps_w                      # in symbol periods
    t_o = np.arange(n_out) * (float(symbol_rate) / float(fs))
    freq = np.interp(t_o, t_w, freq_w) + float(freq_offset)
    iq = np.exp(1j * 2 * np.pi * np.cumsum(freq) / float(fs)).astype(np.complex64)
    if noise > 0.0:
        rng = np.random.default_rng(seed)
        iq = iq + (noise * (rng.standard_normal(iq.size)
                            + 1j * rng.standard_normal(iq.size))).astype(np.complex64)
    return iq
