"""Tone signalling decoders — DTMF and CW/Morse (demod #4, sibling of pocsag.py).

Two classic narrowband tone protocols a TSCM sweep runs into constantly:

(A) DTMF (Dual-Tone Multi-Frequency) — the touch-tones a phone, autodialer, or a
    covert audio device's control channel emits. Each symbol is ONE of four low
    tones (697/770/852/941 Hz) summed with ONE of four high tones
    (1209/1336/1477/1633 Hz); the (low,high) pair maps to a glyph 0-9 * # A-D.
    Detected with the GOERTZEL algorithm (a single-bin DFT — cheaper than a full
    FFT) over short overlapping windows, debounced into a digit string.

(B) CW / Morse — on/off-keyed carrier (an unmodulated key-down beep). Recover the
    envelope, slice it into marks (dot/dash) and gaps (intra-char/char/word) using
    the dot length estimated from the run-length histogram, then look the symbols
    up in the Morse table -> text, and report words-per-minute.

Both demodulate to "audio" off the IQ magnitude envelope (np.abs(iq)) — DTMF tones
and a keyed carrier both live in the envelope, so no FM discriminator is needed
(unlike POCSAG, which taps demod._fm_discriminator). Pure numpy, no scipy.

Honesty gate (Matt's pref #4 — same discipline as pocsag.py): noise or an empty
channel returns an EMPTY result, never a hallucinated decode. DTMF requires BOTH a
dominant low AND a dominant high tone clearly above the rest of the row/column in a
window; CW requires a stable two-level keying rhythm (a coherent dot-length cluster
in the run-length histogram). Encoders (dtmf_encode / cw_encode) live here so the
unit tests synthesize a known signal and round-trip it through the SAME tables.
"""

import numpy as np

# ----------------------------------------------------------------- DTMF constants
DTMF_LOW = (697.0, 770.0, 852.0, 941.0)
DTMF_HIGH = (1209.0, 1336.0, 1477.0, 1633.0)
# (low_index, high_index) -> glyph
DTMF_KEYS = (
    ("1", "2", "3", "A"),
    ("4", "5", "6", "B"),
    ("7", "8", "9", "C"),
    ("*", "0", "#", "D"),
)
_DTMF_GLYPH_TO_FREQ = {}
for _r, _row in enumerate(DTMF_KEYS):
    for _c, _g in enumerate(_row):
        _DTMF_GLYPH_TO_FREQ[_g] = (DTMF_LOW[_r], DTMF_HIGH[_c])

DTMF_WIN_MS = 25.0          # Goertzel analysis window (>= the ~40 ms ITU min tone is
                            #   covered by requiring consecutive hits, below)
DTMF_HOP_MS = 10.0          # window hop
DTMF_MIN_TONE_MS = 40.0     # ITU-T Q.23 minimum valid tone duration
DTMF_DOMINANCE = 4.0        # winning tone must be >= this x the next-loudest in its group
DTMF_REL_FLOOR = 0.08       # window total tone energy must clear this x peak energy (vs silence)

# ----------------------------------------------------------------- Morse constants
MORSE = {
    "A": ".-", "B": "-...", "C": "-.-.", "D": "-..", "E": ".", "F": "..-.",
    "G": "--.", "H": "....", "I": "..", "J": ".---", "K": "-.-", "L": ".-..",
    "M": "--", "N": "-.", "O": "---", "P": ".--.", "Q": "--.-", "R": ".-.",
    "S": "...", "T": "-", "U": "..-", "V": "...-", "W": ".--", "X": "-..-",
    "Y": "-.--", "Z": "--..",
    "0": "-----", "1": ".----", "2": "..---", "3": "...--", "4": "....-",
    "5": ".....", "6": "-....", "7": "--...", "8": "---..", "9": "----.",
    ".": ".-.-.-", ",": "--..--", "?": "..--..", "/": "-..-.", "=": "-...-",
    "+": ".-.-.", "-": "-....-", "(": "-.--.", ")": "-.--.-", ":": "---...",
    "'": ".----.", "@": ".--.-.",
}
_MORSE_REV = {v: k for k, v in MORSE.items()}
CW_TONE_HZ = 700.0          # synthesized CW beat/sidetone frequency (encoder)
# Standard timing: dot=1u, dash=3u, intra-char gap=1u, char gap=3u, word gap=7u.
# PARIS word = 50 units, so wpm = 1200 / dot_ms.


# ===================================================================== DTMF =====
def _goertzel_power(samples: np.ndarray, target_hz: float, fs: float) -> float:
    """Single-bin Goertzel power at target_hz over `samples` (real audio).

    The Goertzel recurrence is a second-order IIR that computes one DFT bin in O(N)
    with no full FFT. Returns squared magnitude, normalized by window length so
    windows of different sizes are comparable."""
    n = samples.size
    if n == 0:
        return 0.0
    k = round(n * target_hz / fs)
    omega = 2.0 * np.pi * k / n
    coeff = 2.0 * np.cos(omega)
    s_prev = 0.0
    s_prev2 = 0.0
    for x in samples:
        s = float(x) + coeff * s_prev - s_prev2
        s_prev2 = s_prev
        s_prev = s
    power = s_prev2 * s_prev2 + s_prev * s_prev - coeff * s_prev * s_prev2
    return power / (n * n)


def _dtmf_envelope(iq, fs: float):
    """Complex IQ -> real audio envelope for tone analysis. DTMF tones ride the
    magnitude; DC-block so the carrier offset doesn't bias Goertzel bin 0."""
    iq = np.asarray(iq, dtype=np.complex64)
    env = np.abs(iq).astype(np.float64)
    env = env - np.mean(env)
    return env


def _detect_dtmf_window(win: np.ndarray, fs: float, peak_ref: float):
    """One window -> a glyph or None. Honesty gate: requires a clearly dominant low
    AND a clearly dominant high tone, and enough total tone energy vs silence."""
    low_pows = [_goertzel_power(win, f, fs) for f in DTMF_LOW]
    high_pows = [_goertzel_power(win, f, fs) for f in DTMF_HIGH]
    total = sum(low_pows) + sum(high_pows)
    if peak_ref > 0 and total < DTMF_REL_FLOOR * peak_ref:
        return None                                   # essentially silence
    li = int(np.argmax(low_pows))
    hi = int(np.argmax(high_pows))

    def _dominant(pows, idx):
        win_p = pows[idx]
        rest = max((p for j, p in enumerate(pows) if j != idx), default=0.0)
        return win_p > 0.0 and win_p >= DTMF_DOMINANCE * rest

    if not (_dominant(low_pows, li) and _dominant(high_pows, hi)):
        return None
    return DTMF_KEYS[li][hi]


def decode_dtmf(iq, fs: float) -> dict:
    """Complex IQ -> decoded DTMF digit string.

    Slides a Goertzel window across the |iq| envelope, emits a glyph per window
    when one low + one high tone clearly dominate, then debounces: a glyph must
    persist for >= DTMF_MIN_TONE_MS, and a silence gap between equal glyphs splits
    them into separate presses (so '11' isn't collapsed). Returns the standard
    decode dict. Empty / noisy capture -> messages: []."""
    iq = np.asarray(iq, dtype=np.complex64)
    if iq.size < 16:
        return _empty_dtmf("capture too short")
    env = _dtmf_envelope(iq, fs)
    win_n = max(8, int(round(DTMF_WIN_MS * 1e-3 * fs)))
    hop_n = max(1, int(round(DTMF_HOP_MS * 1e-3 * fs)))
    if env.size < win_n:
        return _empty_dtmf("capture too short")

    # Reference energy = strongest window's tone energy, for the silence floor.
    peak_ref = 0.0
    starts = list(range(0, env.size - win_n + 1, hop_n))
    win_glyphs = []
    powers = []
    for s in starts:
        w = env[s:s + win_n]
        lp = [_goertzel_power(w, f, fs) for f in DTMF_LOW]
        hp = [_goertzel_power(w, f, fs) for f in DTMF_HIGH]
        powers.append((lp, hp))
        peak_ref = max(peak_ref, sum(lp) + sum(hp))

    for (lp, hp) in powers:
        total = sum(lp) + sum(hp)
        if peak_ref > 0 and total < DTMF_REL_FLOOR * peak_ref:
            win_glyphs.append(None)
            continue
        li, hi = int(np.argmax(lp)), int(np.argmax(hp))

        def _dom(pows, idx):
            wp = pows[idx]
            rest = max((p for j, p in enumerate(pows) if j != idx), default=0.0)
            return wp > 0.0 and wp >= DTMF_DOMINANCE * rest

        win_glyphs.append(DTMF_KEYS[li][hi] if (_dom(lp, li) and _dom(hp, hi)) else None)

    # Collapse consecutive equal glyphs into runs; a run must last the min tone
    # duration. Any None (silence) breaks a run, so repeated digits stay separate.
    min_wins = max(1, int(round(DTMF_MIN_TONE_MS / DTMF_HOP_MS)))
    digits = []
    run_glyph, run_len = None, 0
    for g in win_glyphs:
        if g == run_glyph:
            run_len += 1
        else:
            if run_glyph is not None and run_len >= min_wins:
                digits.append(run_glyph)
            run_glyph, run_len = g, 1
    if run_glyph is not None and run_len >= min_wins:
        digits.append(run_glyph)

    s = "".join(digits)
    if not s:
        return _empty_dtmf("no DTMF tones detected")
    return {
        "protocol": "DTMF",
        "messages": [{"digits": s, "count": len(s)}],
        "note": f"decoded {len(s)} DTMF digit(s): {s}",
        "stats": {"windows": len(starts), "win_ms": DTMF_WIN_MS, "hop_ms": DTMF_HOP_MS},
    }


def _empty_dtmf(note: str) -> dict:
    return {"protocol": "DTMF", "messages": [], "note": note,
            "stats": {"windows": 0, "win_ms": DTMF_WIN_MS, "hop_ms": DTMF_HOP_MS}}


def looks_like_dtmf(feats: dict) -> bool:
    """Cheap pre-gate from identify.iq_features(): DTMF is a narrowband (a few kHz),
    multi-tone, voiceband emission. It shows up as a continuous/intermittent narrow
    signal with >= 2 distinct tones, NOT wideband digital or FSK."""
    if not feats:
        return False
    bw = feats.get("occupied_bw_khz", 0.0) or 0.0
    tones = feats.get("distinct_tones", 0) or 0
    cls = feats.get("modulation_class", "")
    if cls in ("ofdm_wideband", "fsk", "continuous_digital"):
        return False
    return bool(bw <= 5.0 and tones >= 2)


# ================================================================== CW / MORSE ==
def _cw_envelope(iq, fs: float):
    """Complex IQ -> smoothed power envelope for OOK keying detection."""
    iq = np.asarray(iq, dtype=np.complex64)
    env = np.abs(iq).astype(np.float64) ** 2          # power: sharper on/off contrast
    return env


def _smooth(x: np.ndarray, win: int) -> np.ndarray:
    win = max(1, int(win))
    if win == 1:
        return x
    return np.convolve(x, np.ones(win) / win, mode="same")


def _key_states(env: np.ndarray):
    """Envelope -> boolean key-down array via a hysteresis-free midpoint threshold
    between the noise floor (10th pct) and the on level (90th pct). Returns None if
    there is no clear two-level structure (i.e. no keying / pure noise)."""
    lo = float(np.percentile(env, 10))
    hi = float(np.percentile(env, 90))
    if hi <= 4.0 * (lo + 1e-12):                      # no clear on/off contrast
        return None
    thr = lo + 0.5 * (hi - lo)
    return env > thr


def _runs(states: np.ndarray):
    """Boolean array -> list of (is_on, length) runs."""
    out = []
    if states.size == 0:
        return out
    cur = bool(states[0])
    length = 1
    for v in states[1:]:
        if bool(v) == cur:
            length += 1
        else:
            out.append((cur, length))
            cur = bool(v)
            length = 1
    out.append((cur, length))
    return out


def _estimate_dot(on_lengths):
    """Estimate the dot length (in samples) from ON-run lengths. Marks cluster into
    dots (~1u) and dashes (~3u); the dot is near the shortest cluster. Use the
    median of the lower half as a robust dot estimate. Returns None if too few."""
    if len(on_lengths) < 2:
        # a single mark: assume it's a dot (shortest unit) so a lone 'E'/'T' decodes
        return float(on_lengths[0]) if on_lengths else None
    arr = np.sort(np.asarray(on_lengths, dtype=np.float64))
    # split marks at 2x the shortest -> dot cluster vs dash cluster
    shortest = arr[0]
    dots = arr[arr <= 2.0 * shortest]
    return float(np.median(dots)) if dots.size else float(shortest)


def decode_cw(iq, fs: float) -> dict:
    """Complex IQ -> decoded Morse text + estimated WPM.

    Envelope -> on/off keying -> dot length from run-length structure -> classify
    each ON run (dot vs dash at the 2u boundary) and each OFF run (intra-char <2u,
    char <5u, word >=5u) -> Morse table -> text. Honesty gate: requires a clear
    two-level keying envelope AND a coherent dot cluster; pure noise -> empty."""
    iq = np.asarray(iq, dtype=np.complex64)
    if iq.size < 16:
        return _empty_cw("capture too short")
    env = _cw_envelope(iq, fs)
    # Light smoothing (~1 ms) to de-glitch the threshold without blurring short dots.
    env = _smooth(env, max(1, int(round(0.001 * fs))))
    states = _key_states(env)
    if states is None:
        return _empty_cw("no keying detected")
    runs = _runs(states)
    # strip leading/trailing OFF (dead air before/after the message)
    while runs and not runs[0][0]:
        runs.pop(0)
    while runs and not runs[-1][0]:
        runs.pop()
    on_lengths = [ln for on, ln in runs if on]
    if len(on_lengths) < 1:
        return _empty_cw("no keying detected")
    dot = _estimate_dot(on_lengths)
    if not dot or dot <= 0:
        return _empty_cw("no stable dot length")

    # Honesty gate: ON runs must actually cluster near 1u and 3u multiples of the
    # dot. Random noise produces runs scattered everywhere -> reject.
    units = np.asarray(on_lengths, dtype=np.float64) / dot
    near = np.minimum(np.abs(units - 1.0), np.abs(units - 3.0))
    if float(np.mean(near < 0.5)) < 0.6:
        return _empty_cw("keying not consistent with Morse")

    text_parts = []
    symbol = ""
    for on, ln in runs:
        u = ln / dot
        if on:
            symbol += "." if u < 2.0 else "-"
        else:
            if u < 2.0:
                continue                              # intra-character gap
            # char or word gap -> flush current symbol
            if symbol:
                text_parts.append(_MORSE_REV.get(symbol, "?"))
                symbol = ""
            if u >= 5.0:
                text_parts.append(" ")
    if symbol:
        text_parts.append(_MORSE_REV.get(symbol, "?"))
    text = "".join(text_parts).strip()
    if not text:
        return _empty_cw("no decodable Morse")

    wpm = 1200.0 / (dot / fs * 1000.0)                # PARIS: wpm = 1200 / dot_ms
    return {
        "protocol": "CW/Morse",
        "messages": [{"text": text, "wpm": round(wpm, 1)}],
        "note": f"decoded Morse: '{text}' @ ~{round(wpm)} wpm",
        "stats": {"dot_ms": round(dot / fs * 1000.0, 2), "marks": len(on_lengths)},
    }


def _empty_cw(note: str) -> dict:
    return {"protocol": "CW/Morse", "messages": [], "note": note,
            "stats": {"dot_ms": None, "marks": 0}}


def looks_like_cw(feats: dict) -> bool:
    """Cheap pre-gate: CW is a very narrow on/off-keyed carrier — bursty/intermittent
    continuity, narrow bandwidth, 1-2 distinct tones (the keyed carrier). Matches
    identify's 'ook' class or a narrow non-continuous carrier."""
    if not feats:
        return False
    bw = feats.get("occupied_bw_khz", 0.0) or 0.0
    cls = feats.get("modulation_class", "")
    cont = feats.get("continuity", "")
    if cls in ("ofdm_wideband", "fsk", "continuous_digital", "analog_video"):
        return False
    return bool(bw <= 2.0 and cont in ("bursty", "intermittent"))


# ==================================================================== ENCODERS ==
def dtmf_encode(digits: str, fs: float, tone_ms: float = 80.0, gap_ms: float = 60.0,
                amplitude: float = 0.5, noise: float = 0.0, seed: int = 0):
    """Synthesize complex IQ for a DTMF digit string (test/self-check path).

    Each glyph = sum of its two sinusoids at `amplitude` for tone_ms, separated by
    gap_ms of silence. Returned as a (real-valued) complex64 baseband so decode_dtmf
    sees it on the magnitude envelope. Shares DTMF_LOW/HIGH/KEYS with the decoder."""
    fs = float(fs)
    tone_n = int(round(tone_ms * 1e-3 * fs))
    gap_n = int(round(gap_ms * 1e-3 * fs))
    rng = np.random.default_rng(seed)
    out = []
    out.append(np.zeros(gap_n, dtype=np.float64))     # leading silence
    for ch in digits:
        if ch not in _DTMF_GLYPH_TO_FREQ:
            raise ValueError(f"not a DTMF glyph: {ch!r}")
        f_lo, f_hi = _DTMF_GLYPH_TO_FREQ[ch]
        t = np.arange(tone_n) / fs
        tone = amplitude * (np.sin(2 * np.pi * f_lo * t) + np.sin(2 * np.pi * f_hi * t))
        out.append(tone)
        out.append(np.zeros(gap_n, dtype=np.float64))
    audio = np.concatenate(out) if out else np.zeros(0, dtype=np.float64)
    # Carrier offset so the signal isn't purely real (mimics a real baseband
    # capture); the decoder uses |iq| so DC bias from the carrier is removed there.
    iq = (1.0 + audio).astype(np.float64)             # AM-style: envelope carries tones
    iq = iq.astype(np.complex64)
    if noise > 0.0:
        iq = iq + (noise * (rng.standard_normal(iq.size)
                            + 1j * rng.standard_normal(iq.size))).astype(np.complex64)
    return iq


def cw_encode(text: str, wpm: float, fs: float, tone_hz: float = CW_TONE_HZ,
              amplitude: float = 0.7, noise: float = 0.0, seed: int = 0):
    """Synthesize complex IQ for a CW/Morse transmission (test/self-check path).

    Standard timing from `wpm` (dot_ms = 1200/wpm): dot=1u, dash=3u, intra-char=1u,
    char=3u, word=7u. Keyed tone -> OOK envelope the decoder recovers. Shares the
    MORSE table with the decoder so a wrong table fails the round-trip loudly."""
    fs = float(fs)
    dot_ms = 1200.0 / float(wpm)
    u = int(round(dot_ms * 1e-3 * fs))                # samples per unit
    if u < 2:
        raise ValueError("dot too short for this fs/wpm")
    rng = np.random.default_rng(seed)

    def _on(n_units):
        t = np.arange(n_units * u) / fs
        return amplitude * np.sin(2 * np.pi * tone_hz * t)

    def _off(n_units):
        return np.zeros(n_units * u, dtype=np.float64)

    parts = [_off(7)]                                  # leading silence (word gap)
    words = text.strip().upper().split(" ")
    for wi, word in enumerate(words):
        if wi > 0:
            parts.append(_off(7))                      # word gap
        chars = [c for c in word if c in MORSE]
        for ci, ch in enumerate(chars):
            if ci > 0:
                parts.append(_off(3))                  # char gap
            sym = MORSE[ch]
            for si, mark in enumerate(sym):
                if si > 0:
                    parts.append(_off(1))              # intra-char gap
                parts.append(_on(1 if mark == "." else 3))
    parts.append(_off(7))                              # trailing silence
    audio = np.concatenate(parts) if parts else np.zeros(0, dtype=np.float64)
    iq = audio.astype(np.complex64)                    # tone present = key down
    if noise > 0.0:
        iq = iq + (noise * (rng.standard_normal(iq.size)
                            + 1j * rng.standard_normal(iq.size))).astype(np.complex64)
    return iq
