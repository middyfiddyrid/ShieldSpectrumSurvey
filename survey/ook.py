"""Generic OOK/ASK + 2-FSK device decoder (rtl_433-style) — the long tail of cheap
wireless gadgets a TSCM sweep actually trips over: audio bugs with a keep-alive
beacon, GPS trackers, wireless cameras' control links, key fobs, door/window
sensors, doorbells, weather/PIR remotes. None of these are POCSAG-clean protocols;
they are bare line-coded packets, usually retransmitted N times, with no error
check of their own. So the integrity test here is NOT a CRC — it is REPEAT
AGREEMENT: a cheap remote keys the same payload 3-8 times in a burst, and a noise
envelope does not. >=2 byte-identical repeats (or a recognizable long preamble) is
the honesty gate; anything less returns NO messages rather than a hallucinated bit
soup.

Pure-numpy DSP, no scipy — same house rules as demod.py / pocsag.py. For OOK/ASK
we threshold the amplitude envelope abs(iq); for 2-FSK we run the SAME
demod._fm_discriminator the FM listen path and POCSAG use, then threshold the
instantaneous frequency. Either way we land on a pulse/gap train (run-length
encoded), histogram the timings to find the short/long symbol units, and try the
four line codes cheap devices actually use — PWM (pulse-width), PPM (gap-width),
Manchester, and plain NRZ — keeping whichever yields a bitstream that AGREES across
repeats. ook_encode() lives here too so the tests synthesize a known burst and
round-trip it through the same RLE/slicer (a broken slicer fails loudly).

Honesty note (Matt's pref #4): the decoder never invents a clean packet. A
noise-only or empty capture returns {"messages": []}. Every message reports how
many repeats agreed, so the operator can weight a 2/2 lucky agreement against an
8/8 obvious remote.
"""

import numpy as np

from .demod import _fm_discriminator

PROTOCOL = "OOK/FSK-DEVICE"

# A pulse train shorter than this many edges can't carry a real packet + repeats;
# below it we never even try (kills single-glitch false locks).
MIN_EDGES = 12
MIN_PULSE_US = 50.0          # ignore sub-50us slivers (envelope ripple, not symbols)
MAX_BITS = 256               # cheap-device packets are short; cap runaway slicing
GAP_PACKET_FACTOR = 6.0      # a gap >= 6x the median pulse ends a packet / repeat
REPEAT_AGREE_MIN = 2         # honesty gate: >=2 byte-identical repeats
PREAMBLE_MIN_TRANSITIONS = 16  # alt-bit preamble long enough to count as a sync


# ------------------------------------------------------------------ envelope / IF
def _amplitude_envelope(iq: np.ndarray) -> np.ndarray:
    """abs(iq) magnitude envelope for OOK/ASK. float64, DC kept (it's a power level)."""
    return np.abs(np.asarray(iq, dtype=np.complex64)).astype(np.float64)


def _smooth(x: np.ndarray, win: int) -> np.ndarray:
    win = max(1, int(win))
    if win == 1:
        return x
    return np.convolve(x, np.ones(win, dtype=np.float64) / win, mode="same")


def _otsu_threshold(x: np.ndarray, nbins: int = 64) -> float:
    """Otsu's two-class threshold over a 1-D signal. Splits the histogram into
    'off' and 'on' (or low-freq / high-freq) by maximising between-class variance —
    no assumption about the on/off duty ratio, unlike a fixed median."""
    lo, hi = float(np.min(x)), float(np.max(x))
    if hi - lo < 1e-12:
        return hi  # flat -> no pulses (threshold above everything)
    hist, edges = np.histogram(x, bins=nbins, range=(lo, hi))
    hist = hist.astype(np.float64)
    total = hist.sum()
    if total <= 0:
        return 0.5 * (lo + hi)
    centers = 0.5 * (edges[:-1] + edges[1:])
    w_bg = np.cumsum(hist)
    w_fg = total - w_bg
    sum_all = np.cumsum(hist * centers)
    grand = sum_all[-1]
    best_t, best_var = 0.5 * (lo + hi), -1.0
    for i in range(nbins - 1):
        wb, wf = w_bg[i], w_fg[i]
        if wb <= 0 or wf <= 0:
            continue
        mb = sum_all[i] / wb
        mf = (grand - sum_all[i]) / wf
        var = wb * wf * (mb - mf) ** 2
        if var > best_var:
            best_var, best_t = var, centers[i]
    return best_t


# ------------------------------------------------------------------ pulse train
def _run_length_train(level: np.ndarray, fs: float):
    """A boolean 'on/high' track -> list of (state, duration_us) runs, plus the
    leading state. This is the protocol-agnostic intermediate every line code is
    sliced from."""
    level = level.astype(np.int8)
    if level.size == 0:
        return [], 0
    change = np.nonzero(np.diff(level))[0] + 1
    bounds = np.concatenate(([0], change, [level.size]))
    runs = []
    us_per_sample = 1e6 / fs
    for a, b in zip(bounds[:-1], bounds[1:]):
        runs.append((int(level[a]), (b - a) * us_per_sample))
    return runs, int(level[0])


def _ook_level(iq: np.ndarray, fs: float):
    """OOK/ASK: smoothed amplitude envelope -> Otsu-thresholded on/off level."""
    env = _amplitude_envelope(iq)
    # Light smoothing kills sample-to-sample ripple without erasing real edges. We
    # don't know the symbol rate yet, so smooth a small fixed time constant (~5us).
    env = _smooth(env, max(1, int(round(fs * 5e-6))))
    thr = _otsu_threshold(env)
    return (env > thr).astype(np.int8), env, thr


def _fsk_level(iq: np.ndarray, fs: float):
    """2-FSK: instantaneous frequency (FM discriminator) -> Otsu-thresholded
    high/low level. A constant-envelope FSK signal has a flat amplitude (OOK slicer
    sees no pulses) but the IF track toggles between two rails."""
    x = np.asarray(iq, dtype=np.complex64)
    if x.size < 4:
        return np.zeros(0, dtype=np.int8), np.zeros(0), 0.0
    inst = _fm_discriminator(x)
    inst = _smooth(inst, max(1, int(round(fs * 5e-6))))
    # Centre on the midpoint of the two rails (10/90 pct, robust to duty imbalance —
    # same trick the POCSAG slicer uses) so the threshold sits between the marks.
    mid = 0.5 * (np.percentile(inst, 90) + np.percentile(inst, 10))
    return (inst > mid).astype(np.int8), inst, float(mid)


# ------------------------------------------------------------------ timing units
def _unit_times(runs):
    """From the run list, find the short ('one unit') and long timing scales.

    Cheap protocols use 1-2 distinct durations per state. We take the median of the
    shortest cluster as the base symbol unit. Returns (unit_us, long_us) where
    long_us is the median of the durations clearly above ~1.8x unit (the 'wide'
    symbol), or None if there's only one scale."""
    durs = np.array([d for _, d in runs if d >= MIN_PULSE_US], dtype=np.float64)
    if durs.size < 2:
        return None, None
    unit = float(np.median(durs[durs <= 1.8 * np.min(durs[durs > 0])]))
    if unit <= 0:
        unit = float(np.min(durs[durs > 0]))
    longs = durs[durs > 1.8 * unit]
    long_us = float(np.median(longs)) if longs.size else None
    return unit, long_us


def _quantize(dur_us: float, unit_us: float) -> int:
    """Round a duration to an integer number of symbol units (>=1)."""
    return max(1, int(round(dur_us / unit_us)))


# ------------------------------------------------------------------ line codecs
def _slice_pwm(runs, unit_us):
    """PWM / pulse-width: every symbol is one HIGH pulse. A short high = 0, a long
    high = 1 (the common OOK remote / EV1527-style coding). Gaps separate symbols
    and are ignored for the bit value. Returns a 0/1 list or None."""
    bits = []
    for state, dur in runs:
        if dur < MIN_PULSE_US:
            continue
        if state == 1:
            q = _quantize(dur, unit_us)
            bits.append(1 if q >= 2 else 0)
            if len(bits) > MAX_BITS:
                break
    return bits or None


def _slice_ppm(runs, unit_us):
    """PPM / pulse-position: pulse width is fixed; the GAP after it carries the bit.
    Short gap = 0, long gap = 1 (PT2262/keeloq-ish). Returns a 0/1 list or None."""
    bits = []
    for state, dur in runs:
        if dur < MIN_PULSE_US:
            continue
        if state == 0:
            q = _quantize(dur, unit_us)
            bits.append(1 if q >= 2 else 0)
            if len(bits) > MAX_BITS:
                break
    return bits or None


def _slice_nrz(runs, unit_us):
    """Plain NRZ: each run of N units emits N bits of that level. The simplest
    coding; also the fallback the FSK rail track decodes under."""
    bits = []
    for state, dur in runs:
        if dur < MIN_PULSE_US:
            continue
        q = _quantize(dur, unit_us)
        bits.extend([int(state)] * q)
        if len(bits) > MAX_BITS:
            bits = bits[:MAX_BITS]
            break
    return bits or None


def _manchester_from_halfcells(half, offset):
    """Pair half-cell levels from `offset`; each (a,b) with a!=b is a bit (rising
    0->1 = 1, falling = 0). Returns (bits, violations)."""
    bits, viol = [], 0
    i = offset
    while i + 1 < len(half):
        a, b = half[i], half[i + 1]
        if a == b:                  # no mid-bit transition -> phase violation
            viol += 1
            i += 1
            continue
        bits.append(1 if (a == 0 and b == 1) else 0)
        i += 2
        if len(bits) > MAX_BITS:
            break
    return bits, viol


def _slice_manchester(runs, unit_us):
    """Manchester: each bit is a half-cell of one level + a half-cell of the other.
    Expand to the half-cell unit stream, then pair. The leading half-cell can be
    absorbed by the preceding silence (a packet that starts LOW), so we try BOTH
    half-cell phase alignments and keep the one with fewer pairing violations —
    that's the standard way to resolve Manchester phase. None if neither pairs
    cleanly (this is also what rejects a non-Manchester stream)."""
    half = _slice_nrz(runs, unit_us)
    if not half or len(half) < 4:
        return None
    b0, v0 = _manchester_from_halfcells(half, 0)
    b1, v1 = _manchester_from_halfcells(half, 1)
    bits, viol, used = (b0, v0, len(b0)) if v0 <= v1 else (b1, v1, len(b1))
    if not bits or viol > max(2, used // 4):    # too many bad cells -> not Manchester
        return None
    return bits


_ENCODINGS = (("PWM", _slice_pwm), ("PPM", _slice_ppm),
              ("Manchester", _slice_manchester), ("NRZ", _slice_nrz))


# ------------------------------------------------------------------ packetization
def _split_packets(runs, unit_us):
    """Split the run train into repeated packets at the long inter-packet gaps. A
    cheap remote sends packet, big gap, packet, big gap... so a gap >= GAP_PACKET_
    FACTOR x unit is a packet boundary. Returns a list of run-sublists."""
    gap_thresh = GAP_PACKET_FACTOR * unit_us
    packets, cur = [], []
    for state, dur in runs:
        if state == 0 and dur >= gap_thresh:
            if cur:
                packets.append(cur)
                cur = []
            continue
        cur.append((state, dur))
    if cur:
        packets.append(cur)
    return packets


def _packet_variants(pk, unit_us):
    """Yield candidate run-lists for one captured packet: the packet as-is, plus
    variants with a lone ~1-unit HIGH guard pulse stripped from the FRONT and/or
    BACK. The encoder adds those guards to bound LOW symbols at a packet edge (which
    would otherwise merge into the inter-packet silence); a packet that legitimately
    starts/ends HIGH is covered by the unstripped variant. Repeat-agreement then
    arbitrates which interpretation is real — we never have to guess."""
    def _lead(p):
        return len(p) >= 2 and p[0][0] == 1 and _quantize(p[0][1], unit_us) == 1

    def _tail(p):
        return len(p) >= 2 and p[-1][0] == 1 and _quantize(p[-1][1], unit_us) == 1

    seen = []
    for strip_lead in (False, True):
        for strip_tail in (False, True):
            v = pk
            if strip_lead and _lead(v):
                v = v[1:]
            if strip_tail and _tail(v):
                v = v[:-1]
            if v and v not in seen:
                seen.append(v)
                yield v


def _bits_to_hex(bits):
    """0/1 list -> hex string, MSB-first, left-aligned (last nibble zero-padded)."""
    if not bits:
        return ""
    nibbles = []
    for i in range(0, len(bits), 4):
        v = 0
        for j in range(4):
            v = (v << 1) | (bits[i + j] if i + j < len(bits) else 0)
        nibbles.append("%x" % v)
    return "".join(nibbles)


def _bitstr(bits):
    return "".join("1" if b else "0" for b in bits)


def _has_preamble(bits) -> bool:
    """A long run of alternating bits at the head is a recognizable preamble/sync —
    an alternate honesty anchor when only one packet was captured."""
    if len(bits) < PREAMBLE_MIN_TRANSITIONS:
        return False
    head = bits[:PREAMBLE_MIN_TRANSITIONS]
    alt = sum(1 for k in range(1, len(head)) if head[k] != head[k - 1])
    return alt >= PREAMBLE_MIN_TRANSITIONS - 2


def _device_guess(n_bits, pulse_us, encoding) -> str:
    """Light heuristic label from packet length + timing. Not authoritative — a
    hint for the operator, mirrored on what rtl_433 device families actually look
    like."""
    if encoding == "Manchester" and n_bits >= 36:
        return "sensor/telemetry (long Manchester)"
    if 22 <= n_bits <= 28 and 200.0 <= pulse_us <= 600.0:
        return "generic remote / key fob (~24-bit PWM)"
    if 10 <= n_bits <= 14:
        return "door/window or PIR sensor (short code)"
    if n_bits >= 48:
        return "tracker / multi-field telemetry"
    return "unknown cheap-device packet"


def _classify_encoding(packets, unit_us):
    """Pick the most likely line code from the run STRUCTURE before slicing, so a
    PWM burst isn't silently re-read as a (longer, also-self-consistent) NRZ stream.

    The discriminator is which state's run durations vary:
      * PWM  -> HIGH runs take 2 lengths (short/long), LOW runs are a fixed separator.
      * PPM  -> HIGH runs are a fixed pulse, LOW runs take 2 lengths (the bit).
      * Manchester -> highs AND lows are mostly one unit (every cell transitions),
                      with no long inter-symbol runs.
      * NRZ  -> highs and lows both span variable multi-unit runs (run-length levels).
    Returns an ordered tuple of encoding names, most-likely first."""
    # measure on the longest packet (most symbols -> most stable statistics)
    pk = max(packets, key=len)
    highs = [d for s, d in pk if s == 1 and d >= MIN_PULSE_US]
    lows = [d for s, d in pk if s == 0 and d >= MIN_PULSE_US]
    if not highs or not lows:
        return ("NRZ",)

    def _q(durs):
        return [max(1, int(round(d / unit_us))) for d in durs]

    hq, lq = _q(highs), _q(lows)
    h_set, l_set = set(hq), set(lq)
    h_varies = len(h_set) >= 2
    l_varies = len(l_set) >= 2
    h_max, l_max = max(hq), max(lq)

    # PWM: high pulse carries the bit (short/long), low is a constant 1-unit
    # separator. Fixed low + a 2-width high that never exceeds 2 units.
    if h_varies and h_max <= 2 and not l_varies and l_max == 1:
        return ("PWM", "PPM", "Manchester", "NRZ")
    # PPM: low gap carries the bit (short/long), high is a constant 1-unit pulse.
    if l_varies and l_max <= 2 and not h_varies and h_max == 1:
        return ("PPM", "PWM", "Manchester", "NRZ")
    # Manchester self-clocks: a mid-bit transition every cell caps every run at 2
    # units (two adjacent like half-cells), and BOTH states show 1- and 2-unit runs.
    if h_max <= 2 and l_max <= 2:
        return ("Manchester", "NRZ", "PWM", "PPM")
    # Otherwise some run spans >=3 units -> NRZ run-length levels.
    return ("NRZ", "Manchester", "PWM", "PPM")


_SLICERS = dict(_ENCODINGS)


def _best_encoding_for_packets(packets, unit_us):
    """Classify the line code from run structure, then for each code (in that
    priority order) slice every packet and find the largest group of byte-identical
    decodes — the repeats that AGREE. Keep the first code whose top group both
    agrees (>=2) and is the structurally-preferred one; fall back through the
    priority list. Returns (encoding, bits, repeats, total_packets) or None.

    Ranking is by (repeats, structural-priority) — NOT by raw bit count, so NRZ's
    habit of expanding a PWM stream into more (also-consistent) bits can't outvote
    the correct structural read."""
    order = _classify_encoding(packets, unit_us)
    results = []
    for rank, name in enumerate(order):
        slicer = _SLICERS[name]
        groups, n_packets = {}, 0
        for pk in packets:
            seen = set()
            hit = False
            for variant in _packet_variants(pk, unit_us):
                b = slicer(variant, unit_us)
                if b and len(b) >= 4:
                    t = tuple(b)
                    if t not in seen:                 # don't double-count a packet
                        groups[t] = groups.get(t, 0) + 1
                        seen.add(t)
                    hit = True
            if hit:
                n_packets += 1
        if not groups:
            continue
        # Most agreement first. The trailing/leading-guard variants create two reads
        # that tie on repeat count and differ by one edge bit, so the tie-break must
        # know whether that edge bit is real DATA or a stripped GUARD. Manchester
        # self-clocks (no guard needed when it ends HIGH) so its longer read is the
        # true one; PWM/PPM append a real guard so their SHORTER read is true.
        prefer_longer = (name == "Manchester")
        bits, repeats = max(
            groups.items(),
            key=lambda kv: (kv[1], len(kv[0]) if prefer_longer else -len(kv[0])))
        # higher repeats first; on a tie, the structurally-earlier code (lower rank)
        results.append((repeats, -rank, name, list(bits), n_packets))
    if not results:
        return None
    results.sort(reverse=True)
    repeats, _, name, bits, total = results[0]
    return name, bits, repeats, total


def _decode_level(level, fs, modulation):
    """Shared back end for both the OOK envelope level and the FSK rail level:
    RLE -> timing units -> packetize -> pick the line code with the most agreeing
    repeats. Returns a message dict or None (honesty gate enforced here)."""
    runs, _ = _run_length_train(level, fs)
    runs = [(s, d) for (s, d) in runs if d >= MIN_PULSE_US] or runs
    if len(runs) < MIN_EDGES:
        return None
    unit_us, long_us = _unit_times(runs)
    if not unit_us or unit_us < MIN_PULSE_US:
        return None
    packets = _split_packets(runs, unit_us)
    if not packets:
        return None
    chosen = _best_encoding_for_packets(packets, unit_us)
    if chosen is None:
        return None
    encoding, bits, repeats, total = chosen
    # HONESTY GATE: require >=2 agreeing repeats OR a recognizable preamble. A noise
    # envelope yields many disagreeing one-off "packets" -> repeats==1, no preamble
    # -> rejected. This is the equivalent of POCSAG's clean-codeword count.
    if repeats < REPEAT_AGREE_MIN and not _has_preamble(bits):
        return None
    pulse_us = round(unit_us, 1)
    gap_us = round(long_us, 1) if long_us else pulse_us
    n_bits = len(bits)
    return {
        "encoding": encoding,
        "modulation": modulation,
        "n_bits": n_bits,
        "bits": _bitstr(bits),
        "hex": _bits_to_hex(bits),
        "pulse_us": pulse_us,
        "gap_us": gap_us,
        "repeats": repeats,
        "total_bursts": total,
        "preamble": _has_preamble(bits),
        "device_guess": _device_guess(n_bits, pulse_us, encoding),
    }


# ------------------------------------------------------------------ top level
def _empty_result(note: str) -> dict:
    return {"protocol": PROTOCOL, "messages": [], "note": note,
            "stats": {"ook_edges": 0, "fsk_edges": 0}}


def decode_ook(iq, fs: float) -> dict:
    """Top level: complex IQ -> decoded cheap-device packets.

    Runs BOTH front ends — the OOK/ASK amplitude slicer and the 2-FSK frequency
    slicer — and keeps whichever produces a message that clears the repeat-
    agreement honesty gate (preferring more agreeing repeats, then more bits). A
    noise or empty capture clears neither gate and returns {"messages": []}.

    Returns {"protocol", "messages":[{encoding, modulation, n_bits, bits, hex,
    pulse_us, gap_us, repeats, device_guess, ...}], "note", "stats"}.
    Mirrors demod/pocsag: only too-short input (<16 samples) raises."""
    iq = np.asarray(iq, dtype=np.complex64)
    if iq.size < 16:
        raise ValueError("capture too short to decode")

    ook_level, _env, _thr = _ook_level(iq, fs)
    fsk_level, _inst, _mid = _fsk_level(iq, fs)
    ook_runs, _ = _run_length_train(ook_level, fs)
    fsk_runs, _ = _run_length_train(fsk_level, fs)
    stats = {"ook_edges": max(0, len(ook_runs) - 1),
             "fsk_edges": max(0, len(fsk_runs) - 1)}

    ook_msg = _decode_level(ook_level, fs, "OOK/ASK")
    fsk_msg = _decode_level(fsk_level, fs, "2-FSK")

    candidates = [m for m in (ook_msg, fsk_msg) if m is not None]
    if not candidates:
        return {"protocol": PROTOCOL, "messages": [],
                "note": "no consistent pulse train (noise / empty channel)",
                "stats": stats}
    # Prefer the front end whose decode agreed across more repeats, then more bits.
    best = max(candidates, key=lambda m: (m["repeats"], m["n_bits"]))
    note = ("%s %s: %d-bit packet, %d/%d bursts agree (%s)"
            % (best["modulation"], best["encoding"], best["n_bits"],
               best["repeats"], best["total_bursts"], best["device_guess"]))
    return {"protocol": PROTOCOL, "messages": [best], "note": note, "stats": stats}


def looks_like_ook(feats: dict) -> bool:
    """Cheap pre-gate from identify.iq_features() output. Cheap devices are
    bursty/intermittent (they key, then go silent), narrowband, and either OOK
    (so identify sees ~no stable FSK shift) or simple 2-FSK with a small shift.
    We deliberately accept a WIDE net here — decode_ook's repeat gate is the real
    filter; this just keeps the auto-router from wasting a decode on obvious
    continuous wideband (Wi-Fi/broadcast) or clean paging (POCSAG handles that)."""
    if not feats:
        return False
    bw = feats.get("occupied_bw_khz", 0.0) or 0.0
    cont = feats.get("continuity", "")
    mod = feats.get("modulation_class", "")
    states = feats.get("fsk_states", 0) or 0
    shift = feats.get("fsk_shift_khz", 0.0) or 0.0
    duty = feats.get("duty_cycle", 1.0)
    if duty is None:
        duty = 1.0
    # Narrowband is a hard requirement: cheap ISM remotes live in <=300 kHz.
    if bw > 500.0:
        return False
    bursty = cont in ("bursty", "intermittent") or duty < 0.85
    is_ook = mod in ("ook", "ask", "am") or states < 2 or shift <= 0.1
    is_simple_fsk = (states >= 2 and 0.1 < shift <= 12.0)
    return bool(bursty and (is_ook or is_simple_fsk))


# ------------------------------------------------------------------ synth / encoder
def _bits_to_runs(bits, encoding, pulse_us):
    """Inverse of the slicers: a 0/1 list -> (state, duration_us) run list under the
    given line code. Shared by ook_encode so the tests round-trip through the SAME
    timing model the decoder reverses."""
    runs = []
    short = pulse_us
    long = 2.0 * pulse_us
    if encoding == "PWM":
        # each bit = one HIGH pulse (short=0/long=1) + a one-unit LOW separator
        for b in bits:
            runs.append((1, long if b else short))
            runs.append((0, short))
    elif encoding == "PPM":
        # fixed HIGH pulse + a gap that carries the bit (short=0/long=1)
        for b in bits:
            runs.append((1, short))
            runs.append((0, long if b else short))
    elif encoding == "Manchester":
        # each bit = two half-units; rising (0 then 1) = 1, falling = 0
        for b in bits:
            if b:
                runs.append((0, short)); runs.append((1, short))
            else:
                runs.append((1, short)); runs.append((0, short))
    else:  # NRZ
        # collapse consecutive equal bits into one run of N units
        i = 0
        while i < len(bits):
            j = i
            while j < len(bits) and bits[j] == bits[i]:
                j += 1
            runs.append((int(bits[i]), (j - i) * short))
            i = j
    return runs


def _runs_to_iq(runs, fs, modulation, pulse_us, deviation_hz, amp_on, amp_off,
                noise, rng):
    """(state, duration_us) runs -> complex IQ. OOK: amplitude on/off. FSK: constant
    envelope, frequency shifted +/- deviation by state."""
    chunks = []
    phase = 0.0
    for state, dur in runs:
        n = max(1, int(round(dur * 1e-6 * fs)))
        if modulation == "2-FSK":
            f = deviation_hz if state else -deviation_hz
            t = (np.arange(n) + 1) / fs
            seg = np.exp(1j * (phase + 2 * np.pi * f * t))
            phase = float(np.angle(seg[-1]))
            chunks.append(seg.astype(np.complex64))
        else:  # OOK/ASK: a steady carrier gated on/off (here: DC-ish baseband tone)
            amp = amp_on if state else amp_off
            t = (np.arange(n) + 1) / fs
            seg = amp * np.exp(1j * 2 * np.pi * 1000.0 * t)
            chunks.append(seg.astype(np.complex64))
    iq = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.complex64)
    if noise > 0.0:
        iq = iq + (noise * (rng.standard_normal(iq.size)
                            + 1j * rng.standard_normal(iq.size))).astype(np.complex64)
    return iq.astype(np.complex64)


def ook_encode(bits, encoding, pulse_us, fs, repeats=4, modulation="OOK/ASK",
               deviation_hz=20000.0, gap_units=8, preamble_bits=0,
               amp_on=1.0, amp_off=0.05, noise=0.0, seed=0):
    """Synthesize complex IQ for a cheap-device burst (test/self-check path).

    bits: 0/1 list (the payload). encoding: 'PWM'|'PPM'|'Manchester'|'NRZ'.
    repeats: how many times the packet is retransmitted (the agreement the decoder
    uses as its integrity check). Packets are separated by a gap of gap_units x
    pulse_us (>= GAP_PACKET_FACTOR so the decoder splits them). modulation:
    'OOK/ASK' (amplitude keyed) or '2-FSK' (frequency keyed). Shares _bits_to_runs
    with the decoder's slicers so a broken timing model fails loudly in tests."""
    bits = [int(b) & 1 for b in bits]
    rng = np.random.default_rng(seed)
    pre = []
    if preamble_bits > 0:
        pre = [k & 1 for k in range(preamble_bits)]   # alternating 0101...
    gap_dur = gap_units * pulse_us
    all_runs = []
    for r in range(max(1, int(repeats))):
        pkt_bits = pre + bits
        pkt_runs = _bits_to_runs(pkt_bits, encoding, pulse_us)
        # If the packet STARTS on a LOW run (e.g. Manchester bit 1 = low/high half-
        # cells), its leading half-cell would merge into the preceding silence and be
        # lost. Prepend a short HIGH guard so a rising edge precedes it; the decoder
        # strips this leading guard via _packet_variants.
        if pkt_runs and pkt_runs[0][0] == 0:
            all_runs.append((1, pulse_us))
        all_runs.extend(pkt_runs)
        # If the packet ends on a LOW run (PPM's last gap, NRZ's trailing 0, PWM's
        # separator), append a short HIGH guard pulse so a rising edge bounds that
        # final LOW symbol — otherwise it merges into the inter-packet silence under
        # RLE and the last bit is lost. The decoder strips this lone guard.
        if pkt_runs and pkt_runs[-1][0] == 0:
            all_runs.append((1, pulse_us))
        all_runs.append((0, gap_dur))                 # inter-packet silence
    # lead-in silence so the first edge isn't at sample 0
    all_runs = [(0, gap_dur)] + all_runs
    return _runs_to_iq(all_runs, fs, modulation, pulse_us, deviation_hz,
                       amp_on, amp_off, noise, rng)
