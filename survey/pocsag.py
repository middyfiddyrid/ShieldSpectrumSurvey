"""POCSAG (CCIR Radio Paging Code No. 1) decoder — demod #3 (DEMOD_LISTEN_DESIGN.md).

POCSAG is plaintext 2-FSK paging: 512 / 1200 / 2400 baud, NRZ, with a 576-bit
alternating preamble and 544-bit batches (1 frame-sync codeword + 8 frames x 2
codewords). Each 32-bit codeword is BCH(31,21) + even parity and is either an
ADDRESS codeword (18 address bits + 2 function bits; the low 3 bits of the 21-bit
RIC come from the frame the address sits in) or a MESSAGE codeword (20 payload
bits). Message payload decodes as 4-bit reversed-BCD numeric or 7-bit LSB-first
alphanumeric.

High TSCM value: pagers are plaintext (alarm/telemetry, the occasional covert
device). Receive-only, like the rest of the app. Pure-numpy DSP front end + pure
Python bit/BCH back end — no scipy. It taps the FM discriminator directly
(demod._fm_discriminator), NOT demodulate()'s 24 kHz audio tail, so the existing
listen path is untouched. encode_pocsag() lives here too so the unit tests can
synthesize a known transmission and round-trip it through the SAME BCH — see
tests/test_pocsag.py.

Honesty note (Matt's pref #4): the decoder NEVER invents a clean-looking string.
Every message carries bits_corrected / uncorrectable counts, and a no-signal or
garbage capture returns an empty result rather than hallucinated pages.
"""

import numpy as np

from .demod import _decimate, _fm_discriminator

# ---- protocol constants (CCIR Radio Paging Code No. 1 / POCSAG) ----
FSC = 0x7CD215D8            # frame sync codeword, prefixes every batch (MSB-first)
FSC_INV = FSC ^ 0xFFFFFFFF  # 0x832DEA27 — a lock here means inverted FSK polarity
IDLE = 0x7A89C197           # idle codeword filling unused frame slots; skipped
BCH_POLY = 0x769            # BCH(31,21) generator x^10+x^9+x^8+x^6+x^5+x^3+1
BAUDS = (512, 1200, 2400)
WORKING_FS = 38400.0        # decimation target; ~integer multiple of all bauds
PREAMBLE_BITS = 576
CODEWORD_BITS = 32
BATCH_CODEWORDS = 16        # payload codewords after the FSC (8 frames x 2)
FRAME_COUNT = 8
SYNC_MAX_ERRS = 2           # Hamming tolerance for an FSC / FSC_INV lock
MAX_LOCK_TRIES = 24         # candidate FSC locks to try per baud (real captures may
                            #   start mid-batch / behind a false lock — don't trust locks[0])
DEVIATION_HZ = 4500.0       # typical POCSAG peak deviation (~9 kHz tone separation)
ALPHA_BITS = 7
# numeric nibble -> glyph, applied AFTER reversing each 4-bit group (vendor-variable spares)
NUMERIC_MAP = {0: '0', 1: '1', 2: '2', 3: '3', 4: '4', 5: '5', 6: '6', 7: '7',
               8: '8', 9: '9', 10: '*', 11: 'U', 12: ' ', 13: '-', 14: ')', 15: '('}
_NUMERIC_REV = {v: k for k, v in NUMERIC_MAP.items()}


# ---------------------------------------------------------------- bit/BCH layer
def _popcount(x: int) -> int:
    return bin(x & 0xFFFFFFFF).count("1")


def _gf2_remainder(value: int, poly: int, poly_deg: int) -> int:
    """Remainder of `value` divided by `poly` (degree poly_deg) over GF(2)."""
    v = value
    for i in range(v.bit_length() - 1, poly_deg - 1, -1):
        if v & (1 << i):
            v ^= poly << (i - poly_deg)
    return v


def _bch_syndrome(cw31: int) -> int:
    """Syndrome of the 31-bit BCH field (0 => clean)."""
    return _gf2_remainder(cw31 & 0x7FFFFFFF, BCH_POLY, 10)


def bch_check(codeword: int, correct: bool = True):
    """(ok, corrected_codeword, n_errors). n_errors in {0,1,2}; -1 if uncorrectable.

    The 32-bit codeword = bit31 flag + bits30..11 data + bits10..1 BCH + bit0 even
    parity. A word is clean when its 31-bit BCH field (codeword>>1) has syndrome 0
    AND overall parity is even. With correct=True we brute-force up to 2 bit flips
    over the full 32 bits (<=496 trials), accepting the first that satisfies BOTH
    the syndrome and the parity gate. correct=False is detect-only (never flips)."""
    cw = codeword & 0xFFFFFFFF
    if _bch_syndrome(cw >> 1) == 0 and _popcount(cw) % 2 == 0:
        return True, cw, 0
    if not correct:
        return False, codeword, -1
    for b in range(32):                          # single-bit (incl. parity-only error)
        t = cw ^ (1 << b)
        if _bch_syndrome(t >> 1) == 0 and _popcount(t) % 2 == 0:
            return True, t, 1
    for b1 in range(32):                         # two-bit
        for b2 in range(b1 + 1, 32):
            t = cw ^ (1 << b1) ^ (1 << b2)
            if _bch_syndrome(t >> 1) == 0 and _popcount(t) % 2 == 0:
                return True, t, 2
    return False, codeword, -1


def encode_codeword(flag: int, data20: int) -> int:
    """Build a 32-bit codeword: 21-bit dataword -> BCH(31,21) check -> even parity.
    Used by the synth/encoder so tests round-trip through the SAME generator."""
    dw = ((flag & 1) << 20) | (data20 & 0xFFFFF)     # 21 data bits
    reg = dw << 10
    cw31 = reg | _gf2_remainder(reg, BCH_POLY, 10)   # 31-bit BCH codeword
    cw32 = cw31 << 1
    if _popcount(cw32) % 2 == 1:
        cw32 |= 1                                    # even parity
    return cw32 & 0xFFFFFFFF


# ---------------------------------------------------------------- DSP front end
def fm_discriminate(iq, fs: float, target_fs: float = WORKING_FS):
    """Complex IQ -> (real FM-discriminated baseband, working_fs). Boxcar-decimate
    toward target_fs, then the same one-line discriminator demodulate() uses."""
    iq = np.asarray(iq, dtype=np.complex64)
    if iq.size < 16:
        raise ValueError("capture too short to decode")
    dec = max(1, int(round(fs / target_fs)))
    x = _decimate(iq, dec)
    if x.size < 4:
        raise ValueError("capture too short to decode")
    return _fm_discriminator(x), fs / dec


def _boxcar(x: np.ndarray, win: int) -> np.ndarray:
    win = max(1, int(win))
    if win == 1:
        return x
    return np.convolve(x, np.ones(win, dtype=np.float64) / win, mode="same")


def bits_from_baseband(baseband, working_fs: float, baud: int,
                       invert: bool = False) -> np.ndarray:
    """FM-discriminated baseband -> hard NRZ bits (first bit at index 0).

    DC-centre on the median (robust to long same-symbol runs), matched-filter with
    a one-symbol boxcar, recover the sampling phase by maximising eye energy, then
    sample at FRACTIONAL symbol spacing (so a non-integer sps from real capture
    rates doesn't drift over a batch). bit = 1 if sample < 0 (XOR `invert`)."""
    x = np.asarray(baseband, dtype=np.float64)
    sps = working_fs / float(baud)
    if sps < 2.0 or x.size < sps * 4:
        return np.zeros(0, dtype=np.uint8)
    # Decision threshold = midpoint between the two FSK rails. A median/mean would
    # sit ON a rail when the bit stream is unbalanced (e.g. IDLE-heavy batches);
    # the 10/90 percentile midpoint tracks the centre regardless of bit balance.
    x = x - 0.5 * (np.percentile(x, 90) + np.percentile(x, 10))
    mf = _boxcar(x, int(round(sps)))
    span = int(np.ceil(sps))
    best_p, best_score = 0, -1.0
    for p in range(span):                            # pick the phase with the open eye
        nsym = int((x.size - p) / sps)
        if nsym < 4:
            continue
        idx = (p + np.arange(nsym) * sps).round().astype(int)
        idx = idx[idx < x.size]
        score = float(np.mean(np.abs(mf[idx])))
        if score > best_score:
            best_score, best_p = score, p
    nsym = int((x.size - best_p) / sps)
    idx = (best_p + np.arange(nsym) * sps).round().astype(int)
    idx = idx[idx < x.size]
    bits = (mf[idx] < 0).astype(np.uint8)
    if invert:
        bits ^= 1
    return bits


def _word_at(bits: np.ndarray, pos: int) -> int:
    w = 0
    for k in range(CODEWORD_BITS):
        w = (w << 1) | int(bits[pos + k])
    return w


def find_sync(bits: np.ndarray, max_err: int = SYNC_MAX_ERRS):
    """Slide a 32-bit MSB-first window; emit (index_after_FSC, inverted) for every
    position within `max_err` Hamming bits of FSC or its complement."""
    n = bits.size
    locks = []
    if n < CODEWORD_BITS:
        return locks
    mask = 0xFFFFFFFF
    w = 0
    for k in range(CODEWORD_BITS):
        w = ((w << 1) | int(bits[k])) & mask
    i = 0
    while True:
        d0, d1 = _popcount(w ^ FSC), _popcount(w ^ FSC_INV)
        if min(d0, d1) <= max_err:
            locks.append((i + CODEWORD_BITS, d1 < d0))
        nxt = i + CODEWORD_BITS
        if nxt >= n:
            break
        w = ((w << 1) | int(bits[nxt])) & mask
        i += 1
    return locks


# ---------------------------------------------------------------- message decode
def numeric_decode(bits) -> str:
    """4 bits/digit, LSB-first within each group, mapped via NUMERIC_MAP."""
    out = []
    for i in range(0, len(bits) - 3, 4):
        val = 0
        for k in range(4):
            val |= (int(bits[i + k]) & 1) << k
        out.append(NUMERIC_MAP.get(val, '?'))
    # trailing spaces are the conventional numeric fill (vendor-variable) -> trim
    return ''.join(out).rstrip(' ')


def alpha_decode(bits) -> str:
    """7 bits/char, LSB-first (first stream bit = char bit0), ASCII; trailing
    NUL/fill and optional ETX/EOT stripped."""
    out = []
    for i in range(0, len(bits) - 6, 7):
        ch = 0
        for k in range(7):
            ch |= (int(bits[i + k]) & 1) << k
        out.append(chr(ch))
    return ''.join(out).rstrip('\x00\x03\x04')


def _guess_type(numeric: str, alpha: str):
    a = alpha.rstrip()
    printable = sum(1 for c in a if 32 <= ord(c) < 127)
    if a and printable >= max(1, int(0.8 * len(a))):
        return "alpha", a
    return "numeric", numeric


def _finalize(cur: dict) -> dict:
    payload = cur["payload_bits"]
    numeric = numeric_decode(payload)
    alpha = alpha_decode(payload)
    if not payload:
        mtype, text = "tone", ""
    else:
        mtype, text = _guess_type(numeric, alpha)
    return {"ric": cur["ric"], "function": cur["function"], "frame": cur["frame"],
            "type": mtype, "text": text, "numeric": numeric, "alpha": alpha,
            "bits_corrected": cur["bits_corrected"], "uncorrectable": cur["uncorrectable"]}


def decode_codewords(bits, sync_index: int, inverted: bool, correct: bool = True):
    """Walk codewords from a sync point, one at a time. Tolerates a truncated final
    batch (decodes whatever codewords are present) and re-locks the FSC at each
    batch boundary so a message can span batches. Returns (messages, stats)."""
    n = bits.size
    messages, cur = [], None
    stats = {"batches": 1, "codewords_ok": 0, "codewords_corrected": 0, "codewords_bad": 0}
    inv_mask = 0xFFFFFFFF if inverted else 0
    pos, slot = sync_index, 0

    def flush():
        nonlocal cur
        if cur is not None:
            messages.append(_finalize(cur))
            cur = None

    while pos + CODEWORD_BITS <= n:
        if slot == BATCH_CODEWORDS:                      # batch boundary -> expect FSC
            nxt = _word_at(bits, pos)
            if min(_popcount(nxt ^ FSC), _popcount(nxt ^ FSC_INV)) > SYNC_MAX_ERRS:
                break                                    # lost sync / transmission ended
            pos += CODEWORD_BITS                          # consume FSC, keep cur open
            slot = 0
            stats["batches"] += 1
            continue
        frame = slot // 2
        raw = _word_at(bits, pos) ^ inv_mask
        pos += CODEWORD_BITS
        slot += 1
        ok, cw, ne = bch_check(raw, correct=correct)
        if not ok:
            stats["codewords_bad"] += 1
            if cur is not None:
                cur["uncorrectable"] += 1
            continue
        stats["codewords_ok" if ne == 0 else "codewords_corrected"] += 1
        if cw == IDLE:
            flush()
        elif (cw >> 31) & 1 == 0:                         # address codeword
            flush()
            addr18 = (cw >> 13) & 0x3FFFF
            cur = {"ric": (addr18 << 3) | frame, "function": (cw >> 11) & 0x3,
                   "frame": frame, "payload_bits": [], "bits_corrected": ne,
                   "uncorrectable": 0}
        elif cur is not None:                             # message codeword
            data20 = (cw >> 11) & 0xFFFFF
            for b in range(19, -1, -1):                   # 20 payload bits, MSB-first
                cur["payload_bits"].append((data20 >> b) & 1)
            cur["bits_corrected"] += ne
    flush()
    return messages, stats


def _has_preamble(bits, sync_index: int) -> bool:
    end = sync_index - CODEWORD_BITS
    if end < 64:
        return False
    seg = bits[max(0, end - PREAMBLE_BITS):end]
    if seg.size < 64:
        return False
    return bool(np.mean(seg[1:] != seg[:-1]) > 0.8)


def _empty_result(note: str) -> dict:
    return {"baud": None, "inverted": False, "sync_count": 0, "preamble": False,
            "messages": [], "note": note,
            "stats": {"batches": 0, "codewords_ok": 0, "codewords_corrected": 0,
                      "codewords_bad": 0}}


def decode_pocsag(iq, fs: float, bauds=BAUDS, correct: bool = True) -> dict:
    """Top level: complex IQ -> decoded POCSAG pages. Tries every baud (polarity is
    resolved by the FSC/FSC_INV lock) and keeps the hypothesis that locks best.
    Returns a dict with 'messages' (each {ric, function, frame, type, text, numeric,
    alpha, bits_corrected, uncorrectable}) + 'baud'/'inverted'/'sync_count'/'stats'.
    Never raises on no-signal (returns an empty result); only too-short input
    (<16 samples) raises ValueError, mirroring demod.demodulate()."""
    iq = np.asarray(iq, dtype=np.complex64)
    if iq.size < 16:
        raise ValueError("capture too short to decode")
    baseband, wfs = fm_discriminate(iq, fs)
    best_score, best = None, None
    for baud in bauds:
        bits = bits_from_baseband(baseband, wfs, baud)
        if bits.size < 2 * CODEWORD_BITS:
            continue
        locks = find_sync(bits, SYNC_MAX_ERRS)
        if not locks:
            continue
        # Don't commit to locks[0]: a leading noise burst can produce an early
        # near-FSC match, and a real capture may begin mid-batch or with its first
        # FSC faded beyond tolerance, so the true batch origin can be a later lock.
        # Try candidate locks (bounded) and keep whichever decodes best.
        for sync_index, inverted in locks[:MAX_LOCK_TRIES]:
            messages, stats = decode_codewords(bits, sync_index, inverted, correct=correct)
            # Rank CLEAN (uncorrected, ne==0) codewords first. ~half of random 32-bit
            # words are within 2 bits of a valid codeword, so a garbage batch behind a
            # false FSC lock is almost all 2-bit "corrections" -> low codewords_ok, and
            # can't outrank a real batch (whose IDLE fill + address are ne==0).
            score = (stats["codewords_ok"], len(messages),
                     stats["codewords_ok"] + stats["codewords_corrected"])
            if best_score is None or score > best_score:
                best_score = score
                best = {"baud": baud, "inverted": inverted, "sync_count": len(locks),
                        "preamble": _has_preamble(bits, sync_index), "messages": messages,
                        "stats": stats, "note": ""}
    # Confidence gate: a real transmission yields several perfectly-clean codewords;
    # a batch reconstructed ENTIRELY from 2-bit corrections is garbage behind a false
    # lock -> reject it rather than surface hallucinated pages (honesty over recall).
    if best is not None and best["stats"]["codewords_ok"] < 2:
        best = None
    return best if best is not None else _empty_result("no POCSAG sync found")


def looks_like_pocsag(feats: dict) -> bool:
    """Cheap pre-gate from identify.iq_features() output: 2-FSK with a paging-ish
    shift and a symbol rate near 0.512 / 1.2 / 2.4 kBd."""
    if not feats:
        return False
    is_fsk = feats.get("modulation_class") == "fsk" or feats.get("fsk_states", 0) >= 2
    shift = feats.get("fsk_shift_khz", 0.0) or 0.0
    rate = feats.get("symbol_rate_kbd", 0.0) or 0.0
    near = any(abs(rate - b) <= 0.3 * b for b in (0.512, 1.2, 2.4))
    return bool(is_fsk and 2.0 <= shift <= 12.0 and near)


# ---------------------------------------------------------------- synth / encoder
def pack_numeric(s: str):
    bits = []
    for ch in s:
        val = _NUMERIC_REV.get(ch, 0)
        for k in range(4):                       # LSB-first within the group
            bits.append((val >> k) & 1)
    return bits


def pack_alpha(s: str):
    bits = []
    for ch in s:
        c = ord(ch) & 0x7F
        for k in range(ALPHA_BITS):              # LSB-first
            bits.append((c >> k) & 1)
    return bits


def _append_word(bits, w: int):
    for k in range(CODEWORD_BITS - 1, -1, -1):   # MSB-first
        bits.append((w >> k) & 1)


def encode_pocsag(messages, baud: int, fs: float, deviation: float = DEVIATION_HZ,
                  invert: bool = False, preamble_bits: int = PREAMBLE_BITS,
                  noise: float = 0.0, seed: int = 0):
    """Synthesize complex IQ for a POCSAG transmission (test/self-check path).

    messages: list of {ric, mode:'numeric'|'alpha'|'tone', text, function?}. Each
    address is placed in its frame (ric & 7); message codewords follow; unused
    slots are IDLE. Shares encode_codeword (same BCH g) so a wrong polynomial fails
    loudly in tests. NRZ -> 2-FSK: bit 0 = +deviation, bit 1 = -deviation (so the
    decoder's 'sample<0 -> 1' slice round-trips with invert=False)."""
    slots = []
    for m in messages:
        ric, frame = m["ric"], m["ric"] & 7
        while (len(slots) % BATCH_CODEWORDS) // 2 != frame:
            slots.append(IDLE)
        addr18 = (ric >> 3) & 0x3FFFF
        slots.append(encode_codeword(0, (addr18 << 2) | (m.get("function", 0) & 3)))
        mode = m.get("mode", "tone")
        pbits = pack_numeric(m["text"]) if mode == "numeric" else \
            pack_alpha(m["text"]) if mode == "alpha" else []
        if mode == "numeric":
            while len(pbits) % 20 != 0:               # pad with the 0xC space nibble
                pbits += [0, 0, 1, 1]                 #   (LSB-first) -> decoder trims it
        for i in range(0, len(pbits), 20):
            chunk = pbits[i:i + 20]
            data20 = 0
            for j, b in enumerate(chunk):            # chunk[0] first in time = MSB
                data20 |= (b & 1) << (19 - j)
            slots.append(encode_codeword(1, data20))
    while len(slots) == 0 or len(slots) % BATCH_CODEWORDS != 0:
        slots.append(IDLE)

    bits = []
    pb = 1
    for _ in range(preamble_bits):
        bits.append(pb)
        pb ^= 1
    for bi in range(len(slots) // BATCH_CODEWORDS):
        _append_word(bits, FSC)
        for s in range(BATCH_CODEWORDS):
            _append_word(bits, slots[bi * BATCH_CODEWORDS + s])
    for _ in range(16):                          # postamble: guards the last codeword
        bits.append(pb)                          #   against the discriminator's 1-sample
        pb ^= 1                                  #   loss + integer symbol-count floor
    return _bits_to_iq(np.array(bits, dtype=np.uint8), baud, fs,
                       deviation=deviation, invert=invert, noise=noise, seed=seed)


def _bits_to_iq(bit_arr, baud: int, fs: float, deviation: float = DEVIATION_HZ,
                invert: bool = False, noise: float = 0.0, seed: int = 0):
    """NRZ bits -> 2-FSK complex IQ at fs. bit 0 = +deviation, bit 1 = -deviation
    (so the decoder's 'sample<0 -> 1' slice round-trips with invert=False)."""
    bit_arr = np.asarray(bit_arr, dtype=np.uint8).copy()
    if invert:
        bit_arr ^= 1
    sps = fs / float(baud)
    nsamp = int(round(bit_arr.size * sps))
    sym = bit_arr[np.clip((np.arange(nsamp) / sps).astype(int), 0, bit_arr.size - 1)]
    freq = np.where(sym < 1, deviation, -deviation).astype(np.float64)
    iq = np.exp(1j * 2 * np.pi * np.cumsum(freq) / fs).astype(np.complex64)
    if noise > 0.0:
        rng = np.random.default_rng(seed)
        iq = iq + (noise * (rng.standard_normal(iq.size) +
                            1j * rng.standard_normal(iq.size))).astype(np.complex64)
    return iq
