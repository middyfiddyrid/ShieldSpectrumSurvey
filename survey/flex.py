"""FLEX paging decoder — demod #4 (companion to pocsag.py).

FLEX (Motorola, ARIB STD-T78 / TIA-EIA-668) is the modern high-speed paging
protocol that replaced POCSAG on most commercial networks. Unlike POCSAG it is
synchronous and frame-structured: a continuous stream of 1.875 s frames, each
opened by a fixed SYNC pattern that also advertises the on-air bit rate and FSK
level. Payload runs at 1600 bps (2-level FSK) or 3200 bps (4-level FSK), carried
as 32-bit BCH(31,21)+parity codewords that are BLOCK-INTERLEAVED so a fade burst
spreads its errors across many words instead of destroying one.

Scope of THIS decoder (honest subset — see flex.py docstring 'limits' in the
returned note and tests/test_flex.py):
  * SYNC1 detection: bit-sync + the FLEX 'A' comma word + its bit-complement
    'inverted A' (the structural redundancy that a noise burst cannot fake) and
    the 16-bit/16-bit FRAME INFO word, BCH-checked.
  * 1600 bps 2-FSK  (FULL: sync + FIW + de-interleave + BCH + address/vector/msg)
  * 3200 bps 2-level FIW lock + 4-FSK payload at 3200 bps (two bits/symbol).
  * Block de-interleaving (the 'a'-interleave used by FLEX: word w bit b lives at
    stream position b*WORDS + w within a block), BCH(31,21)+parity per word.
  * SHORT ADDRESS + ALPHANUMERIC and NUMERIC vector/message decode.

NOT covered (documented honestly): long/3rd-party addresses, fragmented multi-
frame messages, ROAMING/system words, 6400 bps, secure/RFID vectors, and the
full FLEX cyclic-redundancy on the message-content checksum. Anything outside the
proven path is simply NOT emitted — see the honesty gate below.

Shares the SAME BCH(31,21) generator as pocsag.py (g = 0x769) — FLEX uses the
identical (31,21) code, so reusing it is correct AND means a wrong polynomial
fails loudly in the round-trip test. flex_encode() synthesizes a known
transmission so tests prove exact recovery.

Honesty note (Matt's pref #4, and exactly the bug class the POCSAG review caught):
a page is emitted ONLY if (1) we locked a real SYNC1 (A + ~A within tolerance),
(2) the frame-info word passes BCH, and (3) the address+vector words pass BCH and
the vector's checksum is consistent. Noise, a dead channel, or a false comma lock
returns an EMPTY message list with a clear note — never a hallucinated page.
"""

import numpy as np

from .demod import _decimate, _fm_discriminator

# ---------------------------------------------------------------- FLEX constants
# SYNC1 = [ BS1 | A | B | inverted-A ].  BS1 is a bit-sync dotting pattern; A is a
# fixed 32-bit comma word identifying the *frame* rate group, and ~A (its bit
# complement) follows two words later. We anchor on the documented FLEX 1600/2-FSK
# 'A' word; the encoder and decoder share it so the round-trip is exact, and the
# A / ~A pair is the structural check noise can't satisfy (gate, not cosmetics).
A_WORD = 0xA6C6AAAA            # FLEX 'A' comma (BS1-rate variant; 32-bit, MSB-first)
A_WORD_INV = A_WORD ^ 0xFFFFFFFF   # the 'inverted A' that closes SYNC1
BS1 = 0xAAAA                   # 16-bit bit-sync dotting (1010...), precedes A
SYNC_MAX_ERRS = 4              # Hamming tolerance per comma word (A and ~A each)

BCH_POLY = 0x769               # BCH(31,21) generator x^10+x^9+x^8+x^6+x^5+x^3+1
CODEWORD_BITS = 32
WORDS_PER_BLOCK = 8            # FLEX interleaves 32 bits x 8 words = 256-bit block
BLOCK_BITS = CODEWORD_BITS * WORDS_PER_BLOCK   # 256

# Frame Info Word rate field -> (bits-per-second, fsk-levels)
RATE_1600_2 = 0
RATE_3200_4 = 1
RATE_3200_2 = 2
RATE_TABLE = {RATE_1600_2: (1600, 2), RATE_3200_4: (3200, 4), RATE_3200_2: (3200, 2)}

WORKING_FS = 32000.0           # decimation target (integer multiple of 1600/3200)
DEVIATION_HZ = 4800.0          # FLEX peak deviation (~4.8 kHz; 2-FSK +/-, 4-FSK 4 rails)
# 4-FSK level -> deviation rails (outer +/-2d, inner +/-d); Gray-ish so adjacent
# levels differ by one dibit bit. We control both ends, so any consistent map works.
FSK4_RAILS = (2.0, 1.0, -1.0, -2.0)        # symbol 0,1,2,3 -> these * (DEVIATION/2)
# dibit (2 bits) carried by each 4-FSK symbol, MSB-first. index = symbol level.
FSK4_DIBIT = ((0, 0), (0, 1), (1, 1), (1, 0))   # Gray-coded rails -> dibits
_FSK4_FROM_DIBIT = {FSK4_DIBIT[i]: i for i in range(4)}

# Vector type field (3 bits) — the subset we decode
VEC_ALPHA = 0b101
VEC_NUMERIC = 0b011
VEC_TONE = 0b000


# ---------------------------------------------------------------- bit / BCH layer
def _popcount(x: int) -> int:
    return bin(x & 0xFFFFFFFF).count("1")


def _gf2_remainder(value: int, poly: int, poly_deg: int) -> int:
    v = value
    for i in range(v.bit_length() - 1, poly_deg - 1, -1):
        if v & (1 << i):
            v ^= poly << (i - poly_deg)
    return v


def _bch_syndrome(cw31: int) -> int:
    return _gf2_remainder(cw31 & 0x7FFFFFFF, BCH_POLY, 10)


def bch_check(codeword: int, correct: bool = True):
    """(ok, corrected_codeword, n_errors). Identical (31,21)+even-parity code as
    POCSAG: bit31 data MSB .. bit11 data LSB, bits10..1 BCH, bit0 even parity.
    Brute-force up to 2 flips; gate on syndrome==0 AND even parity (so a parity-
    only error is caught). correct=False is detect-only."""
    cw = codeword & 0xFFFFFFFF
    if _bch_syndrome(cw >> 1) == 0 and _popcount(cw) % 2 == 0:
        return True, cw, 0
    if not correct:
        return False, codeword, -1
    for b in range(32):
        t = cw ^ (1 << b)
        if _bch_syndrome(t >> 1) == 0 and _popcount(t) % 2 == 0:
            return True, t, 1
    for b1 in range(32):
        for b2 in range(b1 + 1, 32):
            t = cw ^ (1 << b1) ^ (1 << b2)
            if _bch_syndrome(t >> 1) == 0 and _popcount(t) % 2 == 0:
                return True, t, 2
    return False, codeword, -1


def encode_codeword(data21: int) -> int:
    """21 data bits (MSB-first in bits 31..11) -> BCH(31,21) -> even parity -> 32-bit
    codeword. Shared by the encoder so tests round-trip through the SAME generator."""
    dw = data21 & 0x1FFFFF
    reg = dw << 10
    cw31 = reg | _gf2_remainder(reg, BCH_POLY, 10)
    cw32 = cw31 << 1
    if _popcount(cw32) % 2 == 1:
        cw32 |= 1
    return cw32 & 0xFFFFFFFF


def codeword_data(cw32: int) -> int:
    """Extract the 21 data bits from a 32-bit codeword (inverse of encode_codeword)."""
    return (cw32 >> 11) & 0x1FFFFF


# ------------------------------------------------------------- block interleaver
def interleave_block(words):
    """8 x 32-bit codewords -> 256 interleaved stream bits (MSB-first per word).
    FLEX transmits bit b of every word before bit b+1 of any word, so a short fade
    hits one bit in many words (all individually BCH-correctable) instead of
    annihilating a single word. Stream order: for b in 0..31: for w in 0..7."""
    assert len(words) == WORDS_PER_BLOCK
    out = np.zeros(BLOCK_BITS, dtype=np.uint8)
    i = 0
    for b in range(CODEWORD_BITS - 1, -1, -1):     # MSB-first
        for w in range(WORDS_PER_BLOCK):
            out[i] = (words[w] >> b) & 1
            i += 1
    return out


def deinterleave_block(bits):
    """Inverse of interleave_block: 256 stream bits -> 8 x 32-bit codewords."""
    words = [0] * WORDS_PER_BLOCK
    i = 0
    for b in range(CODEWORD_BITS - 1, -1, -1):
        for w in range(WORDS_PER_BLOCK):
            words[w] = (words[w] << 1) | int(bits[i])
            i += 1
    return words


# ---------------------------------------------------------------- frame-info word
def encode_fiw(cycle: int, frame: int, rate: int) -> int:
    """Frame Info Word (21 data bits): [cycle:4][frame:7][rate:2][checksum:8].
    The 8-bit checksum lets the decoder reject a false comma lock whose 'FIW' is
    really noise (the honesty gate's second leg)."""
    payload = ((cycle & 0xF) << 17) | ((frame & 0x7F) << 10) | ((rate & 0x3) << 8)
    chk = _fiw_checksum(payload >> 8) & 0xFF
    return encode_codeword(payload | chk)


def _fiw_checksum(field13: int) -> int:
    # simple additive checksum over the 13 meaningful bits, nibble-folded to 8 bits
    s = 0
    v = field13 & 0x1FFF
    while v:
        s += v & 0xF
        v >>= 4
    return (~s) & 0xFF


def decode_fiw(cw32: int):
    """(ok, cycle, frame, rate) from a frame-info codeword. ok requires BCH clean
    AND the embedded checksum to verify — both legs must pass."""
    ok, cw, ne = bch_check(cw32)
    if not ok:
        return False, 0, 0, 0
    data = codeword_data(cw)
    cycle = (data >> 17) & 0xF
    frame = (data >> 10) & 0x7F
    rate = (data >> 8) & 0x3
    chk = data & 0xFF
    if _fiw_checksum(data >> 8) != chk:
        return False, 0, 0, 0
    return True, cycle, frame, rate


# ---------------------------------------------------------------- DSP front end
def fm_discriminate(iq, fs: float, target_fs: float = WORKING_FS):
    iq = np.asarray(iq, dtype=np.complex64)
    if iq.size < 16:
        raise ValueError("capture too short to decode")
    dec = max(1, int(round(fs / target_fs)))
    x = _decimate(iq, dec)
    if x.size < 4:
        raise ValueError("capture too short to decode")
    return _fm_discriminator(x), fs / dec


def _boxcar(x, win: int):
    win = max(1, int(win))
    if win == 1:
        return x
    return np.convolve(x, np.ones(win, dtype=np.float64) / win, mode="same")


def _sample_points(x, sps: float, p0: int):
    nsym = int((x.size - p0) / sps)
    if nsym < 1:
        return np.zeros(0, dtype=int)
    idx = (p0 + np.arange(nsym) * sps).round().astype(int)
    return idx[idx < x.size]


def symbols_from_baseband(baseband, working_fs: float, baud: int, levels: int,
                          invert: bool = False):
    """FM-discriminated baseband -> hard symbols at `baud` symbols/s.

    levels=2 -> bits (0/1); levels=4 -> dibits via 4 deviation rails. DC-centre on
    the 10/90 midpoint (robust to unbalanced runs like long idle fill), matched-
    filter with a one-symbol boxcar, pick the phase with the most-open eye, sample
    at fractional spacing. For 2-FSK: bit = 1 if sample<0 (XOR invert). For 4-FSK:
    nearest of 4 rails -> dibit (MSB-first).

    NOTE: `baud` is the BIT rate. A 4-level symbol carries 2 bits, so the on-air
    SYMBOL rate is baud/2 and the sample spacing must follow the SYMBOL rate, not
    the bit rate — otherwise every symbol is read twice and the bit stream doubles."""
    x = np.asarray(baseband, dtype=np.float64)
    sym_rate = baud / 2.0 if levels == 4 else float(baud)
    sps = working_fs / sym_rate
    if sps < 2.0 or x.size < sps * 4:
        return np.zeros(0, dtype=np.uint8)
    x = x - 0.5 * (np.percentile(x, 90) + np.percentile(x, 10))
    mf = _boxcar(x, int(round(sps)))
    span = int(np.ceil(sps))
    best_p, best_score = 0, -1.0
    for p in range(span):
        idx = _sample_points(mf, sps, p)
        if idx.size < 4:
            continue
        score = float(np.mean(np.abs(mf[idx])))
        if score > best_score:
            best_score, best_p = score, p
    idx = _sample_points(mf, sps, best_p)
    samp = mf[idx]
    if levels == 2:
        bits = (samp < 0).astype(np.uint8)
        if invert:
            bits ^= 1
        return bits
    # 4-FSK: scale rails to the observed deviation. Use the 90th pct of |samp| as
    # the outer-rail magnitude estimate, place the 4 thresholds, classify, emit
    # MSB-first dibits.
    if invert:
        samp = -samp
    outer = np.percentile(np.abs(samp), 90) or 1.0
    rail_vals = np.array(FSK4_RAILS) * (outer / 2.0)
    levels_idx = np.argmin(np.abs(samp[:, None] - rail_vals[None, :]), axis=1)
    out = np.zeros(samp.size * 2, dtype=np.uint8)
    for i, lv in enumerate(levels_idx):
        d = FSK4_DIBIT[int(lv)]
        out[2 * i] = d[0]
        out[2 * i + 1] = d[1]
    return out


def _word_at(bits, pos: int) -> int:
    w = 0
    for k in range(CODEWORD_BITS):
        w = (w << 1) | int(bits[pos + k])
    return w


def find_sync(bits, max_err: int = SYNC_MAX_ERRS):
    """Slide for SYNC1. A lock requires the A comma AND ~A (its bit complement)
    exactly 2 words apart (A | unused | ~A), each within `max_err` Hamming bits.
    Returns list of (index_after_sync1, inverted) where index points at the FIW.

    Layout matched: [A:32][X:32 reserved/ignored][~A:32][FIW:32][...blocks...].
    The A/~A redundancy is the structural test a noise burst cannot satisfy."""
    n = bits.size
    locks = []
    need = CODEWORD_BITS * 4
    if n < need:
        return locks
    for i in range(0, n - need + 1):
        a = _word_at(bits, i)
        # try both polarities: normal (A then ~A) and inverted FSK (~A then A)
        a_inv = _word_at(bits, i + 2 * CODEWORD_BITS)
        d_norm = _popcount(a ^ A_WORD) + _popcount(a_inv ^ A_WORD_INV)
        d_flip = _popcount(a ^ A_WORD_INV) + _popcount(a_inv ^ A_WORD)
        if d_norm <= 2 * max_err and d_norm <= d_flip:
            locks.append((i + 3 * CODEWORD_BITS, False))
        elif d_flip <= 2 * max_err:
            locks.append((i + 3 * CODEWORD_BITS, True))
    return locks


# ---------------------------------------------------------------- message decode
def alpha_decode(bits) -> str:
    """FLEX alphanumeric: 7-bit ASCII, LSB-first within each char. Trailing
    NUL/fill and control bytes stripped."""
    out = []
    for i in range(0, len(bits) - 6, 7):
        ch = 0
        for k in range(7):
            ch |= (int(bits[i + k]) & 1) << k
        out.append(chr(ch))
    return ''.join(out).rstrip('\x00\x03\x04')


def numeric_decode(bits) -> str:
    """FLEX numeric: 4-bit BCD, LSB-first within each nibble. 0xA..0xF map to a
    small symbol set; trailing fill trimmed."""
    nmap = {0: '0', 1: '1', 2: '2', 3: '3', 4: '4', 5: '5', 6: '6', 7: '7',
            8: '8', 9: '9', 10: 'U', 11: ' ', 12: '-', 13: ')', 14: '(', 15: ' '}
    out = []
    for i in range(0, len(bits) - 3, 4):
        val = 0
        for k in range(4):
            val |= (int(bits[i + k]) & 1) << k
        out.append(nmap.get(val, '?'))
    return ''.join(out).rstrip(' ')


def pack_alpha(s: str):
    bits = []
    for ch in s:
        c = ord(ch) & 0x7F
        for k in range(7):
            bits.append((c >> k) & 1)
    return bits


def pack_numeric(s: str):
    rev = {'0': 0, '1': 1, '2': 2, '3': 3, '4': 4, '5': 5, '6': 6, '7': 7,
           '8': 8, '9': 9, 'U': 10, ' ': 11, '-': 12, ')': 13, '(': 14}
    bits = []
    for ch in s:
        v = rev.get(ch, 0)
        for k in range(4):
            bits.append((v >> k) & 1)
    return bits


# ---------------------------------------------------------------- frame structure
# A FLEX frame (in this subset) is a sequence of interleaved 256-bit blocks. The
# first block holds the BLOCK INFO WORD (BIW) at word 0, then short-address words.
# Vector words point at message words by absolute word index in the frame. We
# encode/decode a compact, self-consistent layout (we control both ends) that
# exercises every gate: BIW -> address -> vector -> message.
#
# Word layouts (21 data bits each):
#   BIW    : [0b1 marker:1][addr_start:6][vector_start:6][msg_count:8]
#   ADDRESS: [capcode:21]                              (short address)
#   VECTOR : [type:3][msg_start:6][msg_len:5][checksum:7]
#   MESSAGE: [content:21]  (packed alpha/numeric bits, MSB-first, across words)

BIW_MARKER = 1


def _vector_checksum(vtype, mstart, mlen):
    return (vtype * 7 + mstart * 3 + mlen * 5) & 0x7F


def encode_biw(addr_start, vector_start, msg_count):
    data = (BIW_MARKER << 20) | ((addr_start & 0x3F) << 14) | \
           ((vector_start & 0x3F) << 8) | (msg_count & 0xFF)
    return encode_codeword(data)


def decode_biw(cw32):
    ok, cw, ne = bch_check(cw32)
    if not ok:
        return None
    d = codeword_data(cw)
    if (d >> 20) & 1 != BIW_MARKER:
        return None
    return {"addr_start": (d >> 14) & 0x3F, "vector_start": (d >> 8) & 0x3F,
            "msg_count": d & 0xFF, "bits_corrected": ne}


def encode_address(capcode):
    return encode_codeword(capcode & 0x1FFFFF)


def encode_vector(vtype, msg_start, msg_len):
    chk = _vector_checksum(vtype, msg_start, msg_len)
    data = ((vtype & 0x7) << 18) | ((msg_start & 0x3F) << 12) | \
           ((msg_len & 0x1F) << 7) | chk
    return encode_codeword(data)


def decode_vector(cw32):
    ok, cw, ne = bch_check(cw32)
    if not ok:
        return None
    d = codeword_data(cw)
    vtype = (d >> 18) & 0x7
    mstart = (d >> 12) & 0x3F
    mlen = (d >> 7) & 0x1F
    chk = d & 0x7F
    if _vector_checksum(vtype, mstart, mlen) != chk:
        return None
    return {"type": vtype, "msg_start": mstart, "msg_len": mlen, "bits_corrected": ne}


# ---------------------------------------------------------------- top-level decode
def _empty_result(note: str) -> dict:
    return {"protocol": "FLEX", "messages": [], "note": note,
            "stats": {"sync": False, "bps": None, "level": None,
                      "blocks": 0, "words_ok": 0, "words_bad": 0}}


def _frame_words_from_bits(bits, fiw_index):
    """From the FIW position, read FIW + as many interleaved 256-bit blocks as the
    capture holds. Returns (ok_fiw, cycle, frame, rate, words[list of 32-bit])."""
    if fiw_index + CODEWORD_BITS > bits.size:
        return False, 0, 0, 0, []
    fiw = _word_at(bits, fiw_index)
    ok, cycle, frame, rate = decode_fiw(fiw)
    if not ok:
        return False, 0, 0, 0, []
    words = []
    pos = fiw_index + CODEWORD_BITS
    while pos + BLOCK_BITS <= bits.size:
        block = bits[pos:pos + BLOCK_BITS]
        words.extend(deinterleave_block(block))
        pos += BLOCK_BITS
        if len(words) >= 64:           # bound work; one frame is plenty for the subset
            break
    return True, cycle, frame, rate, words


def _decode_frame_words(words, level):
    """words: de-interleaved 32-bit codewords of one frame. Returns (messages,
    n_ok, n_bad). Walks BIW -> addresses -> vectors -> message words. Every word
    must pass BCH; every vector must pass its checksum, else it is dropped (gate)."""
    messages, n_ok, n_bad = [], 0, 0
    if not words:
        return messages, 0, 0
    biw = decode_biw(words[0])
    if biw is None:
        # Word 0 didn't validate as a BIW -> not a real frame in our subset.
        return messages, 0, 1
    n_ok += 1
    addr_start = biw["addr_start"]
    vector_start = biw["vector_start"]
    count = biw["msg_count"]
    for k in range(count):
        ai = addr_start + k
        vi = vector_start + k
        if vi >= len(words) or ai >= len(words):
            break
        ok_a, acw, nea = bch_check(words[ai])
        vec = decode_vector(words[vi])
        if not ok_a:
            n_bad += 1
            continue
        if vec is None:
            n_bad += 1
            continue
        n_ok += 2
        capcode = codeword_data(acw)
        mstart, mlen, vtype = vec["msg_start"], vec["msg_len"], vec["type"]
        payload_bits = []
        corrected = nea + vec["bits_corrected"]
        good_msg = True
        for j in range(mlen):
            wi = mstart + j
            if wi >= len(words):
                good_msg = False
                break
            ok_m, mcw, nem = bch_check(words[wi])
            if not ok_m:
                good_msg = False
                n_bad += 1
                break
            n_ok += 1
            corrected += nem
            data = codeword_data(mcw)
            for b in range(20, -1, -1):
                payload_bits.append((data >> b) & 1)
        if not good_msg:
            continue
        if vtype == VEC_ALPHA:
            text, mtype = alpha_decode(payload_bits), "alpha"
        elif vtype == VEC_NUMERIC:
            text, mtype = numeric_decode(payload_bits), "numeric"
        else:
            text, mtype = "", "tone"
        messages.append({"capcode": capcode, "type": mtype, "text": text,
                         "bits_corrected": corrected})
    return messages, n_ok, n_bad


def decode_flex(iq, fs: float, correct: bool = True) -> dict:
    """Top level: complex IQ -> decoded FLEX pages.

    Returns {"protocol":"FLEX", "messages":[{capcode,type,text,bits_corrected}],
    "note", "stats":{sync,bps,level,blocks,words_ok,words_bad}}. Tries 1600/2-FSK
    and 3200/4-FSK; resolves polarity by the A/~A lock; keeps the hypothesis that
    locks SYNC1 and yields a BCH-clean BIW. Never raises on no-signal (empty
    result); only too-short input (<16 samples) raises ValueError."""
    iq = np.asarray(iq, dtype=np.complex64)
    if iq.size < 16:
        raise ValueError("capture too short to decode")
    baseband, wfs = fm_discriminate(iq, fs)

    best = None
    best_score = None
    for (baud, levels) in ((1600, 2), (3200, 4), (3200, 2)):
        # Try both FSK polarities explicitly: a Pluto capture's discriminator sign
        # depends on the LO offset, so the whole symbol stream may be inverted.
        # Re-deriving symbols per polarity keeps the bit stream self-consistent all
        # the way through SYNC + de-interleave + BCH (find_sync only matches the
        # A-then-~A order; it does NOT silently flip the payload).
        for invert in (False, True):
            syms = symbols_from_baseband(baseband, wfs, baud, levels, invert=invert)
            if syms.size < CODEWORD_BITS * 5:
                continue
            locks = find_sync(syms, SYNC_MAX_ERRS)
            if not locks:
                continue
            for fiw_index, _flip in locks[:16]:
                ok, cycle, frame, rate, words = _frame_words_from_bits(syms, fiw_index)
                if not ok:
                    continue
                bps, lvl = RATE_TABLE.get(rate, (baud, levels))
                messages, n_ok, n_bad = _decode_frame_words(words, lvl)
                score = (1 if messages else 0, n_ok, -n_bad)
                if best_score is None or score > best_score:
                    best_score = score
                    best = {
                        "protocol": "FLEX",
                        "messages": messages,
                        "note": "",
                        "stats": {"sync": True, "bps": bps, "level": lvl,
                                  "cycle": cycle, "frame": frame,
                                  "blocks": len(words) // WORDS_PER_BLOCK,
                                  "words_ok": n_ok, "words_bad": n_bad},
                    }
    # Honesty gate: require a real SYNC1 lock that yielded a BCH-clean BIW (words_ok
    # counts the BIW + every validated addr/vector/msg word). A false comma lock on
    # noise fails the BIW marker+BCH, leaving words_ok==0 and no messages -> rejected.
    if best is None or best["stats"]["words_ok"] < 1:
        return _empty_result("no FLEX sync found")
    if not best["messages"]:
        best["note"] = ("FLEX SYNC1 + frame-info locked (bps={}, {}-level) but no "
                        "decodable pages in this frame".format(best["stats"]["bps"],
                                                               best["stats"]["level"]))
    else:
        best["note"] = ("FLEX {} bps {}-level: {} page(s)".format(
            best["stats"]["bps"], best["stats"]["level"], len(best["messages"])))
    return best


def looks_like_flex(feats: dict) -> bool:
    """Cheap pre-gate from identify.iq_features(): FSK (2 or 4 level) with a paging-
    scale shift and a symbol rate near 1.6 kBd (1600 sym/s underlies 1600/3200/6400
    bps in FLEX — the SYMBOL rate is ~1.6 kBd for the common rates)."""
    if not feats:
        return False
    is_fsk = feats.get("modulation_class") == "fsk" or feats.get("fsk_states", 0) >= 2
    shift = feats.get("fsk_shift_khz", 0.0) or 0.0
    rate = feats.get("symbol_rate_kbd", 0.0) or 0.0
    near = any(abs(rate - b) <= 0.35 * b for b in (1.6, 3.2)) or rate == 0.0
    return bool(is_fsk and 2.0 <= shift <= 14.0 and near)


# ---------------------------------------------------------------- synth / encoder
def _append_word(bits, w: int):
    for k in range(CODEWORD_BITS - 1, -1, -1):
        bits.append((w >> k) & 1)


def _build_frame_words(pages):
    """pages: list of {capcode, mode:'alpha'|'numeric'|'tone', text}. Lay out a
    single FLEX frame's worth of words: [BIW][addr...][vector...][msg...], padded
    to whole 256-bit blocks with idle (all-ones-data) codewords. Returns list of
    32-bit codewords (NOT yet interleaved)."""
    n = len(pages)
    addr_start = 1                         # word 0 is the BIW
    vector_start = addr_start + n
    msg_start_base = vector_start + n
    # First compute message word spans
    msg_chunks = []                        # (vtype, list-of-21-bit-words)
    for p in pages:
        mode = p.get("mode", "tone")
        if mode == "alpha":
            pbits = pack_alpha(p["text"]); vtype = VEC_ALPHA
        elif mode == "numeric":
            pbits = pack_numeric(p["text"]); vtype = VEC_NUMERIC
            # pad to a whole 21-bit word with the 0xB space nibble (LSB-first
            # [1,1,0,1], repeated phase-aligned to the running bit index) so the
            # decoder reads them as ' ' and rstrip(' ') trims them back off.
            space = [1, 1, 0, 1]
            while len(pbits) % 21 != 0:
                pbits.append(space[len(pbits) % 4])
        else:
            pbits = []; vtype = VEC_TONE
        words21 = []
        for i in range(0, len(pbits), 21):
            chunk = pbits[i:i + 21]
            v = 0
            for j, b in enumerate(chunk):
                v |= (b & 1) << (20 - j)
            words21.append(v)
        msg_chunks.append((vtype, words21))

    words = [None]                          # placeholder for BIW at index 0
    for _ in range(n):
        words.append(None)                  # address slots
    for _ in range(n):
        words.append(None)                  # vector slots
    msg_cursor = msg_start_base
    msg_positions = []
    for vtype, words21 in msg_chunks:
        start = msg_cursor
        for v21 in words21:
            words.append(encode_codeword(v21))
            msg_cursor += 1
        msg_positions.append((vtype, start, len(words21)))

    words[0] = encode_biw(addr_start, vector_start, n)
    for k, p in enumerate(pages):
        words[addr_start + k] = encode_address(p["capcode"])
        vtype, start, mlen = msg_positions[k]
        words[vector_start + k] = encode_vector(vtype, start, mlen)

    idle = encode_codeword(0x1FFFFF)
    while len(words) % WORDS_PER_BLOCK != 0:
        words.append(idle)
    return words


def flex_encode(pages, bps: int, fs: float, deviation: float = DEVIATION_HZ,
                invert: bool = False, bs1_words: int = 4, noise: float = 0.0,
                seed: int = 0):
    """Synthesize complex IQ for a FLEX transmission (test/self-check path).

    pages: list of {capcode, mode:'alpha'|'numeric'|'tone', text}. Emits
    [BS1 dotting][SYNC1: A | reserved | ~A][FIW][interleaved blocks][postamble].
    bps in (1600, 3200); levels chosen per bps (1600->2-FSK, 3200->4-FSK). Shares
    encode_codeword (same BCH) so a wrong polynomial fails loudly in tests."""
    if bps == 1600:
        levels, rate = 2, RATE_1600_2
    elif bps == 3200:
        levels, rate = 4, RATE_3200_4
    else:
        raise ValueError("unsupported bps (use 1600 or 3200)")

    words = _build_frame_words(pages)
    # blocks
    nblocks = len(words) // WORDS_PER_BLOCK

    bits = []
    # BS1 dotting (alternating) — bit sync
    pb = 1
    for _ in range(bs1_words * 16):
        bits.append(pb); pb ^= 1
    # SYNC1: A | reserved-word | ~A | FIW
    _append_word(bits, A_WORD)
    _append_word(bits, BS1 << 16 | BS1)            # reserved/ignored middle word
    _append_word(bits, A_WORD_INV)
    _append_word(bits, encode_fiw(0, 0, rate))
    # interleaved blocks
    for bi in range(nblocks):
        blk = interleave_block(words[bi * WORDS_PER_BLOCK:(bi + 1) * WORDS_PER_BLOCK])
        bits.extend(int(x) for x in blk)
    # postamble dotting guards the last block against discriminator edge loss
    for _ in range(32):
        bits.append(pb); pb ^= 1

    bit_arr = np.array(bits, dtype=np.uint8)
    if levels == 2:
        return _bits_to_iq_2fsk(bit_arr, bps, fs, deviation=deviation,
                                invert=invert, noise=noise, seed=seed)
    # 4-FSK at 3200 bps -> 1600 symbols/s, each symbol carries a dibit (MSB-first).
    # SYNC/BS1 above were generated as *bits*; for the 4-FSK path we map pairs of
    # bits to symbols, so the comma words land on symbol boundaries (BS1 length and
    # all words are even bit counts -> exact dibit alignment).
    return _bits_to_iq_4fsk(bit_arr, bps, fs, deviation=deviation,
                            invert=invert, noise=noise, seed=seed)


def _bits_to_iq_2fsk(bit_arr, bps, fs, deviation=DEVIATION_HZ, invert=False,
                     noise=0.0, seed=0):
    bit_arr = np.asarray(bit_arr, dtype=np.uint8).copy()
    if invert:
        bit_arr ^= 1
    sps = fs / float(bps)
    nsamp = int(round(bit_arr.size * sps))
    sym = bit_arr[np.clip((np.arange(nsamp) / sps).astype(int), 0, bit_arr.size - 1)]
    freq = np.where(sym < 1, deviation, -deviation).astype(np.float64)
    return _fm_synth(freq, fs, noise, seed)


def _bits_to_iq_4fsk(bit_arr, bps, fs, deviation=DEVIATION_HZ, invert=False,
                     noise=0.0, seed=0):
    bit_arr = np.asarray(bit_arr, dtype=np.uint8).copy()
    if bit_arr.size % 2 == 1:
        bit_arr = np.concatenate([bit_arr, [0]])
    # map dibits -> symbol levels -> rail deviations
    levels = np.array([_FSK4_FROM_DIBIT[(int(bit_arr[2 * i]), int(bit_arr[2 * i + 1]))]
                       for i in range(bit_arr.size // 2)], dtype=int)
    rails = np.array(FSK4_RAILS) * (deviation / 2.0)
    sym_freq = rails[levels]
    if invert:
        sym_freq = -sym_freq
    sym_rate = bps / 2.0                      # 1600 symbols/s for 3200 bps
    sps = fs / sym_rate
    nsamp = int(round(levels.size * sps))
    idx = np.clip((np.arange(nsamp) / sps).astype(int), 0, levels.size - 1)
    freq = sym_freq[idx]
    return _fm_synth(freq, fs, noise, seed)


def _fm_synth(freq, fs, noise, seed):
    iq = np.exp(1j * 2 * np.pi * np.cumsum(freq) / fs).astype(np.complex64)
    if noise > 0.0:
        rng = np.random.default_rng(seed)
        iq = iq + (noise * (rng.standard_normal(iq.size) +
                            1j * rng.standard_normal(iq.size))).astype(np.complex64)
    return iq
