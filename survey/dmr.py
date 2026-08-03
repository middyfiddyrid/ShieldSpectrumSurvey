"""DMR (ETSI TS 102 361) frame layer — built on the C4FM symbol stream from c4fm.py.

DMR is 2-slot TDMA on a 12.5 kHz carrier: 4800 C4FM symbols/s = 9600 bit/s, cut
into 30 ms TDMA frames. Each frame carries one 264-bit BURST:

    [ 108 bits ][ 48 bits ][ 108 bits ]
       info       SYNC or      info
                  EMB+ES

  * DATA / control bursts split it further:
        info(98) | SLOT TYPE(10) | SYNC(48) | SLOT TYPE(10) | info(98)
    The two 98-bit halves are one BPTC(196,96) product code carrying a 96-bit
    payload; the two SLOT TYPE halves are one Golay(20,8) carrying the COLOUR
    CODE (4 bits) and the DATA TYPE (4 bits).
  * VOICE bursts carry 6 AMBE frames across the two 108-bit halves. Burst A of
    each superframe puts a voice SYNC in the middle; bursts B-F replace it with
    EMB(8) + embedded signalling(32) + EMB(8), the EMB being a (16,7,6) code
    holding the colour code, the PRIVACY INDICATOR and a 2-bit LCSS.

What this module extracts, and why it matters for a sweep:
    colour code, sync flavour (BS / MS / direct, voice / data, and for direct
    mode the SLOT), data type, and from a Voice LC Header or a Terminator-with-LC
    the TALKGROUP and SOURCE RADIO ID — plus, first-class, the ENCRYPTION state.

ENCRYPTION IS A RESULT, NOT A FAILURE. Three independent indicators are read and
any of them is enough: a PI-header data type, the privacy bit in the link
control's service options, and the PI bit in a voice burst's EMB. "DMR, colour
code 1, talkgroup 4021, ENCRYPTED" is a complete, useful, honest finding — the
module never reports encrypted traffic as a failed decode or as silence.

HONESTY GATE: a burst is only believed when a 48-bit sync pattern matches within
SYNC_MAX_ERRS AND some FEC in it validates (Golay slot type, BPTC product code,
or EMB), or when three independent syncs are found. Noise, analog FM and 2-FSK
therefore return an empty message list, not invented talkgroups. Every emitted
field carries its FEC provenance (sync_errors, slot_type_errors, bptc_ok,
bptc_corrected) so a marginal decode is visible rather than silent.

NOT IMPLEMENTED — say so rather than fake it:
  * The RS(12,9) outer check on the 72-bit Full LC. The BPTC(196,96) product code
    is the integrity gate instead; lc["rs_checked"] is always False. (The RS root
    convention is not something this build can verify without off-air traffic,
    and a wrong convention used as a GATE would silently reject real calls.)
  * CACH, so a BS-sourced burst's ABSOLUTE slot number is unknown — only direct
    mode names its slot. Slots inferred from burst ordering are labelled
    slot_source="alternation" and are RELATIVE.
  * PI-header field layout (the header is reported as encrypted, with its raw
    payload hex, but its algorithm/key/IV fields are not parsed).
  * Voice payload: the AMBE frames are located and counted, never decoded. See
    survey/ambe.py.
  * Long/3rd-party LC formats, CSBK opcode semantics, and data-message reassembly.

VALIDATION STATUS (Matt's pref #4): NOTHING here has been run against a real
off-air DMR signal. tests/test_dmr.py drives synthetic bursts built by the
encoder in this same file, plus negative cases. Two things are anchored to the
published specs rather than to self-consistency, and the tests assert them: the
ten sync patterns of TS 102 361-1 Table 9.2, and the MINIMUM DISTANCE of each FEC
code (Golay(20,8,8), (16,7,6), Hamming(15,11,3), Hamming(13,9,3)) — a wrong
generator polynomial or parity mask changes the distance and fails loudly. The
BPTC bit-ordering conventions (the x181 interleave, the 13x15 matrix layout and
the 3 reserved info bits) are implemented per the common reading of the spec and
are the most likely thing to need a one-constant fix once real traffic is seen.
"""

import numpy as np

from . import c4fm
from .c4fm import demod_c4fm, dibits_to_bits, invert_dibits

# ---------------------------------------------------------------- burst geometry
BURST_BITS = 264
BURST_DIBITS = BURST_BITS // 2                # 132 symbols
SYNC_BITS = 48
SYNC_DIBITS = SYNC_BITS // 2                  # 24 symbols
SYNC_OFFSET_BITS = 108                        # sync starts 108 bits into the burst
SYNC_OFFSET_DIBITS = SYNC_OFFSET_BITS // 2    # 54 symbols
TDMA_FRAME_BITS = 288                         # 30 ms at 9600 bit/s = burst + CACH/guard
TDMA_FRAME_DIBITS = TDMA_FRAME_BITS // 2      # 144 symbols

INFO1 = (0, 98)                               # data burst: first info half
SLOT_TYPE1 = (98, 10)
SLOT_TYPE2 = (156, 10)
INFO2 = (166, 98)                             # data burst: second info half
EMB1 = (108, 8)                               # voice burst B-F: EMB, first half
EMBEDDED_SIG = (116, 32)
EMB2 = (148, 8)

SYNC_MAX_ERRS = 4        # of 48 bits. P(<=4 random bit errors in 48) ~ 8e-10 per
                         #   position, so this is still an anchor a fade cannot fake.
GOLAY_MAX_ERRS = 3       # Golay(20,8,8) corrects 3; a 4th is detected, not guessed
EMB_MAX_ERRS = 2         # (16,7,6) corrects 2
MIN_SYNCS_WITHOUT_FEC = 3        # see the honesty gate in decode_dmr()
MAX_BURSTS = 512                 # bound the work on a long capture
EMB_PROBE_FRAMES = 6             # TDMA frames either side of an anchor to probe

# ---- sync patterns, ETSI TS 102 361-1 Table 9.2 (48 bits each, MSB first) ----
# CAREFUL: these ten words are five POLARITY PAIRS. Inverting the C4FM rails maps
# each dibit d -> d ^ 0b10, i.e. XORs a 48-bit sync window with 0xAAAAAAAAAAAA,
# and bs_voice ^ 0xAAAAAAAAAAAA is EXACTLY bs_data (likewise ms, both direct-mode
# slots, and ms_rc/reserved). So a sync match can NEVER resolve receive polarity,
# and a data burst read with the wrong polarity masquerades as a voice burst.
# decode_dmr() therefore fixes polarity once per capture from FEC evidence — see
# _decode_with_polarity() — instead of trusting the sync word. Getting this wrong
# silently swaps voice and data for the whole capture.
SYNC_PATTERNS = {
    "bs_voice":         0x755FD7DF75F7,
    "bs_data":          0xDFF57D75DF5D,
    "ms_voice":         0x7F7D5DD57DFD,
    "ms_data":          0xD5D7F77FD757,
    "ms_rc":            0x77D55F7DFD77,
    "direct_ts1_voice": 0x5D577F7757FF,
    "direct_ts1_data":  0xF7FDD5DDFD55,
    "direct_ts2_voice": 0x7DFFD5F55D5F,
    "direct_ts2_data":  0xD7557F5FF7F5,
    "reserved":         0xDD7FF5D757DD,
}
VOICE_SYNCS = ("bs_voice", "ms_voice", "direct_ts1_voice", "direct_ts2_voice")
DATA_SYNCS = ("bs_data", "ms_data", "direct_ts1_data", "direct_ts2_data")
# direct mode names its own slot; BS/MS bursts do not carry it in the sync
SYNC_SLOT = {"direct_ts1_voice": 1, "direct_ts1_data": 1,
             "direct_ts2_voice": 2, "direct_ts2_data": 2}
SYNC_DIRECTION = {"bs": "BS", "ms": "MS", "di": "direct", "re": None}

# ---- SLOT TYPE data-type field (4 bits) ----
DT_PI_HEADER = 0x0
DT_VOICE_LC_HEADER = 0x1
DT_TERMINATOR_LC = 0x2
DT_CSBK = 0x3
DATA_TYPES = {
    0x0: "PI header", 0x1: "Voice LC header", 0x2: "Terminator with LC",
    0x3: "CSBK", 0x4: "MBC header", 0x5: "MBC continuation",
    0x6: "Data header", 0x7: "Rate 1/2 data", 0x8: "Rate 3/4 data",
    0x9: "Idle", 0xA: "Rate 1 data", 0xB: "Unified single-block data",
}
# data types whose BPTC payload is a 72-bit Full Link Control word
LC_DATA_TYPES = (DT_VOICE_LC_HEADER, DT_TERMINATOR_LC)

# ---- Full Link Control ----
FLCO_GROUP_VOICE = 0x00       # Group Voice Channel User  -> dst is a TALKGROUP
FLCO_UNIT_VOICE = 0x03        # Unit to Unit Voice Channel User -> dst is a radio
FLCO_NAMES = {FLCO_GROUP_VOICE: "group voice", FLCO_UNIT_VOICE: "unit-to-unit voice"}
SVC_EMERGENCY_BIT = 0x80      # service options bit 7
SVC_PRIVACY_BIT = 0x40        # service options bit 6 — THE encryption indicator

# ---- FEC generators. Each is pinned by a minimum-distance test, not by faith ----
GOLAY_23_12_POLY = 0xC75      # x^11+x^10+x^6+x^5+x^4+x^2+1; extended and shortened
                              #   by 4 info bits gives the DMR Golay(20,8,8)
EMB_BCH_POLY = 0x1D1          # x^8+x^7+x^6+x^4+1; BCH(15,7,5) + overall parity
                              #   = the (16,7,6) code DMR uses for the EMB
# Hamming parity masks over the data word, d0 in the HIGH bit (row/column order)
HAMMING_15_11_MASKS = (0b11110101100, 0b01111010110, 0b00111101011, 0b11101011001)
HAMMING_13_9_MASKS = (0b110101100, 0b111010110, 0b111101011, 0b101011001)

# ---- BPTC(196,96) ----
BPTC_BITS = 196
BPTC_INTERLEAVE_STEP = 181    # deinterleaved[i] = raw[(i*181) % 196]
BPTC_ROWS = 13                # rows 0..8 data, rows 9..12 column-parity
BPTC_COLS = 15                # cols 0..10 data, cols 11..14 row-parity
BPTC_DATA_ROWS = 9
BPTC_DATA_COLS = 11
BPTC_RESERVED_INFO_BITS = 3   # R(0..2) lead the 99-bit info field; payload is the
                              #   remaining 96. (deinterleaved[0] is a 4th R bit,
                              #   outside the 13x15 matrix.)
BPTC_MAX_PASSES = 4
# Correction BUDGET, and it is the honesty gate, not a performance knob. Iterating
# row/column Hamming correction is a projection onto the code: run it long enough
# on RANDOM bits and it converges to a valid codeword, which would make bptc_ok
# meaningless. Measured on this implementation: 6000 random 196-bit vectors gave
# 542 "clean" convergences, and the cheapest of those needed 26 bit flips; a
# genuine burst carrying up to ~10 real bit errors closes in <= 22, and past ~12
# errors the recovered payload is often wrong anyway. 20 sits in that gap — with
# it, 0 of those 6000 random vectors validate. See test_random_bits_do_not_validate.
BPTC_MAX_CORRECTIONS = 20


# ---------------------------------------------------------------- bit helpers
def _popcount(x: int) -> int:
    return bin(x & ((1 << 64) - 1)).count("1")


def _gf2_remainder(value: int, poly: int, poly_deg: int) -> int:
    """Remainder of `value` divided by `poly` (degree poly_deg) over GF(2). Same
    routine as pocsag._gf2_remainder — kept local so dmr.py stands alone."""
    v = value
    for i in range(v.bit_length() - 1, poly_deg - 1, -1):
        if v & (1 << i):
            v ^= poly << (i - poly_deg)
    return v


def _bits_to_int(bits, start: int, n: int) -> int:
    v = 0
    for i in range(start, start + n):
        v = (v << 1) | (int(bits[i]) & 1)
    return v


def _int_to_bits(value: int, n: int):
    return [(value >> k) & 1 for k in range(n - 1, -1, -1)]


def _bits_hex(bits) -> str:
    """MSB-first bit list -> hex string, left-padded to whole nibbles."""
    v = 0
    for b in bits:
        v = (v << 1) | (int(b) & 1)
    return "%0*X" % ((len(bits) + 3) // 4, v)


def _nearest_codeword(word: int, table, max_err: int):
    """ML decode against a full codeword table: (data, errors) or (None, -1).

    Both DMR block codes here have tiny codebooks (256 and 128 words), so an
    exhaustive nearest-codeword search IS the maximum-likelihood decoder and
    needs no syndrome algebra. It also fails HONESTLY: past max_err it returns
    nothing instead of snapping to whatever happened to be closest."""
    best_d, best_data = 65, None
    for data, cw in table:
        d = _popcount(word ^ cw)
        if d < best_d:
            best_d, best_data = d, data
            if d == 0:
                break
    if best_data is None or best_d > max_err:
        return None, -1
    return best_data, best_d


# ---------------------------------------------------------------- Golay(20,8,8)
def golay_20_8_encode(data8: int) -> int:
    """8 data bits -> 20-bit codeword, systematic (data in bits 19..12).

    Construction: the 8 bits are the low 8 of a 12-bit Golay(23,12) info word
    (the top 4 are forced to zero), the classic 11-bit Golay parity is appended,
    then an overall even-parity bit extends it to (24,12); dropping the 4
    always-zero info bits leaves (20,8). Shortening preserves the extended
    Golay's distance, so this is the d=8 code the spec calls for — pinned by
    test_golay_minimum_distance_is_8."""
    info = data8 & 0xFF
    reg = info << 11
    cw23 = reg | _gf2_remainder(reg, GOLAY_23_12_POLY, 11)
    cw24 = (cw23 << 1) | (_popcount(cw23) & 1)
    return cw24 & 0xFFFFF


_GOLAY_TABLE = [(d, golay_20_8_encode(d)) for d in range(256)]


def golay_20_8_decode(word20: int, max_err: int = GOLAY_MAX_ERRS):
    """(data8, n_bit_errors) or (None, -1) if beyond correction."""
    return _nearest_codeword(word20 & 0xFFFFF, _GOLAY_TABLE, max_err)


# ---------------------------------------------------------------- EMB (16,7,6)
def emb_encode(data7: int) -> int:
    """7 data bits -> 16-bit codeword (data in bits 15..9). BCH(15,7,5) with
    EMB_BCH_POLY plus an overall parity bit — the (16,7,6) code, pinned by
    test_emb_code_minimum_distance_is_6."""
    reg = (data7 & 0x7F) << 8
    cw15 = reg | _gf2_remainder(reg, EMB_BCH_POLY, 8)
    return ((cw15 << 1) | (_popcount(cw15) & 1)) & 0xFFFF


_EMB_TABLE = [(d, emb_encode(d)) for d in range(128)]


def emb_decode(word16: int, max_err: int = EMB_MAX_ERRS):
    """(data7, n_bit_errors) or (None, -1)."""
    return _nearest_codeword(word16 & 0xFFFF, _EMB_TABLE, max_err)


def pack_emb(colour_code: int, pi: int, lcss: int) -> int:
    """EMB payload: CC(4) | PI(1) | LCSS(2) -> the 7 data bits of the (16,7,6)."""
    return (((colour_code & 0xF) << 3) | ((pi & 1) << 2) | (lcss & 0x3)) & 0x7F


def unpack_emb(data7: int) -> dict:
    return {"colour_code": (data7 >> 3) & 0xF, "pi": (data7 >> 2) & 1,
            "lcss": data7 & 0x3}


# ---------------------------------------------------------------- Hamming codes
def _hamming_encode(data_bits, masks):
    """data bits (MSB first) -> data + parity bits. Each mask selects the data
    positions XORed into one parity bit, d0 in the mask's HIGH bit."""
    k = len(data_bits)
    d = 0
    for b in data_bits:
        d = (d << 1) | (int(b) & 1)
    return list(data_bits) + [_popcount(d & m) & 1 for m in masks]


def _hamming_syndrome_table(k: int, masks):
    """syndrome -> single-error bit position, for the (k + len(masks)) code."""
    n = k + len(masks)
    table = {}
    zero = _hamming_encode([0] * k, masks)
    for pos in range(n):
        cw = list(zero)
        cw[pos] ^= 1
        data, par = cw[:k], cw[k:]
        exp = _hamming_encode(data, masks)[k:]
        syn = 0
        for a, b in zip(exp, par):
            syn = (syn << 1) | (a ^ b)
        table.setdefault(syn, pos)
    return table


_H15_TABLE = _hamming_syndrome_table(11, HAMMING_15_11_MASKS)
_H13_TABLE = _hamming_syndrome_table(9, HAMMING_13_9_MASKS)


def _hamming_decode(cw, k: int, masks, table):
    """(corrected codeword list, n_errors). n_errors is 0, 1, or -1 when the
    syndrome is non-zero and maps to no single-bit position (detected, not
    corrected — these codes are d=3, so 2 errors are detectable only)."""
    data, par = list(cw[:k]), list(cw[k:])
    exp = _hamming_encode(data, masks)[k:]
    syn = 0
    for a, b in zip(exp, par):
        syn = (syn << 1) | (int(a) ^ int(b))
    if syn == 0:
        return list(cw), 0
    pos = table.get(syn)
    if pos is None:
        return list(cw), -1
    out = list(cw)
    out[pos] ^= 1
    return out, 1


def hamming_15_11_encode(data11):
    return _hamming_encode(data11, HAMMING_15_11_MASKS)


def hamming_15_11_decode(cw15):
    return _hamming_decode(cw15, 11, HAMMING_15_11_MASKS, _H15_TABLE)


def hamming_13_9_encode(data9):
    return _hamming_encode(data9, HAMMING_13_9_MASKS)


def hamming_13_9_decode(cw13):
    return _hamming_decode(cw13, 9, HAMMING_13_9_MASKS, _H13_TABLE)


# ---------------------------------------------------------------- BPTC(196,96)
def bptc_interleave(bits196):
    """Inverse of bptc_deinterleave — used by the encoder so the tests round-trip
    through the same permutation."""
    out = [0] * BPTC_BITS
    for i in range(BPTC_BITS):
        out[(i * BPTC_INTERLEAVE_STEP) % BPTC_BITS] = int(bits196[i]) & 1
    return out


def bptc_deinterleave(bits196):
    return [int(bits196[(i * BPTC_INTERLEAVE_STEP) % BPTC_BITS]) & 1
            for i in range(BPTC_BITS)]


def _matrix_from_deint(deint):
    """deint[0] is a reserved bit outside the array; the rest is 13 rows x 15
    columns, row-major."""
    return [[deint[1 + r * BPTC_COLS + c] for c in range(BPTC_COLS)]
            for r in range(BPTC_ROWS)]


def _deint_from_matrix(matrix, r0: int = 0):
    out = [r0 & 1]
    for r in range(BPTC_ROWS):
        out.extend(int(b) & 1 for b in matrix[r])
    return out


def bptc_encode(payload96) -> list:
    """96 payload bits -> 196 interleaved on-air bits.

    Lays the 96 bits (behind BPTC_RESERVED_INFO_BITS zeroed R bits) into the
    9x11 data corner, adds Hamming(15,11) parity down each row and Hamming(13,9)
    parity down each column, then interleaves."""
    info = [0] * BPTC_RESERVED_INFO_BITS + [int(b) & 1 for b in payload96]
    if len(info) != BPTC_DATA_ROWS * BPTC_DATA_COLS:
        raise ValueError("payload must be 96 bits")
    matrix = [[0] * BPTC_COLS for _ in range(BPTC_ROWS)]
    for r in range(BPTC_DATA_ROWS):
        row = info[r * BPTC_DATA_COLS:(r + 1) * BPTC_DATA_COLS]
        matrix[r] = hamming_15_11_encode(row)
    for c in range(BPTC_COLS):
        col = [matrix[r][c] for r in range(BPTC_DATA_ROWS)]
        full = hamming_13_9_encode(col)
        for r in range(BPTC_ROWS):
            matrix[r][c] = full[r]
    return bptc_interleave(_deint_from_matrix(matrix))


def bptc_decode(bits196):
    """196 on-air bits -> (payload96 or None, ok, n_corrected).

    Alternates column (Hamming 13,9) and row (Hamming 15,11) correction — that is
    the whole point of a product code: an error a row cannot place is usually
    trivial for its column. `ok` is True only when EVERY row and column syndrome
    is finally clean AND the total number of bit flips stayed inside
    BPTC_MAX_CORRECTIONS, which is what stops the iteration from quietly
    manufacturing a valid codeword out of noise."""
    matrix = _matrix_from_deint(bptc_deinterleave(bits196))
    corrected = 0
    for _ in range(BPTC_MAX_PASSES):
        for c in range(BPTC_COLS):
            col = [matrix[r][c] for r in range(BPTC_ROWS)]
            fixed, ne = hamming_13_9_decode(col)
            if ne > 0:
                corrected += ne
                for r in range(BPTC_ROWS):
                    matrix[r][c] = fixed[r]
        for r in range(BPTC_ROWS):
            fixed, ne = hamming_15_11_decode(matrix[r])
            if ne > 0:
                corrected += ne
                matrix[r] = fixed
        if _bptc_clean(matrix):
            break
    if not _bptc_clean(matrix) or corrected > BPTC_MAX_CORRECTIONS:
        return None, False, corrected
    info = []
    for r in range(BPTC_DATA_ROWS):
        info.extend(matrix[r][:BPTC_DATA_COLS])
    return info[BPTC_RESERVED_INFO_BITS:], True, corrected


def _bptc_clean(matrix) -> bool:
    for r in range(BPTC_ROWS):
        if hamming_15_11_decode(matrix[r])[1] != 0:
            return False
    for c in range(BPTC_COLS):
        if hamming_13_9_decode([matrix[r][c] for r in range(BPTC_ROWS)])[1] != 0:
            return False
    return True


# ---------------------------------------------------------------- Full LC
def pack_full_lc(flco: int, fid: int, service_options: int, dst: int, src: int):
    """72-bit Full Link Control word: PF(1) R(1) FLCO(6) FID(8) SVC(8) DST(24)
    SRC(24)."""
    bits = _int_to_bits(0, 1) + _int_to_bits(0, 1) + _int_to_bits(flco & 0x3F, 6)
    bits += _int_to_bits(fid & 0xFF, 8)
    bits += _int_to_bits(service_options & 0xFF, 8)
    bits += _int_to_bits(dst & 0xFFFFFF, 24)
    bits += _int_to_bits(src & 0xFFFFFF, 24)
    return bits


def parse_full_lc(payload96) -> dict:
    """96-bit BPTC payload -> parsed Full LC. Bits 72..95 are the RS(12,9) parity;
    they are carried through as hex but NOT verified — see the module docstring."""
    b = list(payload96)
    flco = _bits_to_int(b, 2, 6)
    svc = _bits_to_int(b, 16, 8)
    dst = _bits_to_int(b, 24, 24)
    src = _bits_to_int(b, 48, 24)
    return {
        "pf": b[0], "flco": flco, "flco_name": FLCO_NAMES.get(flco),
        "fid": _bits_to_int(b, 8, 8), "service_options": svc,
        "emergency": bool(svc & SVC_EMERGENCY_BIT),
        "privacy": bool(svc & SVC_PRIVACY_BIT),
        "dst": dst, "src": src,
        "talkgroup": dst if flco == FLCO_GROUP_VOICE else None,
        "target_radio": dst if flco == FLCO_UNIT_VOICE else None,
        "rs_parity_hex": _bits_hex(b[72:96]), "rs_checked": False,
    }


def parse_csbk(payload96) -> dict:
    """CSBK header fields only: LB / PF / opcode / feature-set ID. Opcode-level
    semantics are deliberately not decoded (see the module docstring), so the
    rest is handed up as hex for an analyst rather than guessed at."""
    b = list(payload96)
    return {"lb": b[0], "pf": b[1], "csbko": _bits_to_int(b, 2, 6),
            "fid": _bits_to_int(b, 8, 8), "payload_hex": _bits_hex(b[16:96])}


# ---------------------------------------------------------------- burst parsing
def _sync_kind(name: str) -> str:
    if name in VOICE_SYNCS:
        return "voice"
    if name in DATA_SYNCS:
        return "data"
    return "rc" if name == "ms_rc" else "reserved"


def find_sync_bursts(dibits, max_err: int = SYNC_MAX_ERRS, max_bursts: int = MAX_BURSTS):
    """Every dibit position whose 24-symbol window matches one of the ten DMR
    sync words AT THE GIVEN POLARITY. Returns [(burst_start_dibit, sync_name,
    errors), ...] in stream order, best match kept per position, and only where a
    WHOLE 264-bit burst fits in the capture.

    The opposite polarity is deliberately NOT searched here: because the ten
    words are polarity pairs (see SYNC_PATTERNS), searching both would just
    return every hit twice under two different names. Polarity is a property of
    the capture, resolved once by decode_dmr()."""
    d = np.asarray(dibits, dtype=np.uint8)
    n = d.size
    best = {}
    for name, pattern in SYNC_PATTERNS.items():
        for idx, _inv, errs in c4fm.find_pattern(d, pattern, SYNC_DIBITS, max_err,
                                                 allow_inverted=False):
            start = idx - SYNC_OFFSET_DIBITS
            if start < 0 or start + BURST_DIBITS > n:
                continue
            prev = best.get(start)
            if prev is None or errs < prev[1]:
                best[start] = (name, errs)
    out = [(s, v[0], v[1]) for s, v in sorted(best.items())]
    return out[:max_bursts]


def burst_bits(dibits, start: int):
    """The 264 bits of the burst beginning at dibit `start` (the stream is
    already at the resolved polarity)."""
    seg = np.asarray(dibits, dtype=np.uint8)[start:start + BURST_DIBITS]
    if seg.size < BURST_DIBITS:
        return None
    return dibits_to_bits(seg)


def parse_slot_type(bits):
    """SLOT TYPE lives in two 10-bit halves either side of the sync; together
    they are one Golay(20,8) word. Returns (colour_code, data_type, errors) or
    (None, None, -1)."""
    word = (_bits_to_int(bits, SLOT_TYPE1[0], SLOT_TYPE1[1]) << 10) | \
        _bits_to_int(bits, SLOT_TYPE2[0], SLOT_TYPE2[1])
    data, errs = golay_20_8_decode(word)
    if data is None:
        return None, None, -1
    return (data >> 4) & 0xF, data & 0xF, errs


def parse_emb(bits):
    """EMB is 8 bits before and 8 bits after the 32-bit embedded signalling.
    Returns a dict with colour_code / pi / lcss / errors, or None."""
    word = (_bits_to_int(bits, EMB1[0], EMB1[1]) << 8) | \
        _bits_to_int(bits, EMB2[0], EMB2[1])
    data, errs = emb_decode(word)
    if data is None:
        return None
    out = unpack_emb(data)
    out["errors"] = errs
    return out


def _parse_burst(bits, sync_name, sync_errs, start, inverted):
    """One burst -> the full per-burst record documented in decode_dmr()."""
    kind = _sync_kind(sync_name) if sync_name else "voice"
    rec = {"index": int(start), "sync": sync_name, "sync_errors": int(sync_errs),
           "inverted": bool(inverted), "kind": kind,
           "direction": SYNC_DIRECTION.get((sync_name or "")[:2]),
           "slot": SYNC_SLOT.get(sync_name), "slot_source": None,
           "colour_code": None, "data_type": None, "data_type_name": None,
           "slot_type_errors": None, "bptc_ok": None, "bptc_corrected": None,
           "payload_hex": None, "lc": None, "csbk": None, "emb": None,
           "encrypted": None, "encryption_evidence": None}
    if rec["slot"] is not None:
        rec["slot_source"] = "sync"
    if kind == "voice" and sync_name:
        # burst A of a superframe: the sync replaces the EMB, so there is no
        # colour code here. Six AMBE frames sit in the two 108-bit halves; we
        # locate them, we do NOT pretend to decode them.
        rec["voice_bits"] = 216
        return rec
    if kind != "data":
        return rec
    cc, dt, errs = parse_slot_type(bits)
    rec["colour_code"], rec["data_type"], rec["slot_type_errors"] = cc, dt, errs
    if dt is None:
        return rec
    rec["data_type_name"] = DATA_TYPES.get(dt, "reserved (0x%X)" % dt)
    info = [int(b) for b in bits[INFO1[0]:INFO1[0] + INFO1[1]]] + \
           [int(b) for b in bits[INFO2[0]:INFO2[0] + INFO2[1]]]
    payload, ok, corrected = bptc_decode(info)
    rec["bptc_ok"], rec["bptc_corrected"] = ok, corrected
    if not ok:
        return rec
    rec["payload_hex"] = _bits_hex(payload)
    if dt in LC_DATA_TYPES:
        lc = parse_full_lc(payload)
        rec["lc"] = lc
        rec["encrypted"] = bool(lc["privacy"])
        if lc["privacy"]:
            rec["encryption_evidence"] = "privacy bit set in the link control header"
    elif dt == DT_PI_HEADER:
        # The PI header EXISTS only to carry encryption parameters. Its presence
        # is the finding; its field layout is not parsed (see module docstring).
        rec["encrypted"] = True
        rec["encryption_evidence"] = "privacy (PI) header present"
    elif dt == DT_CSBK:
        rec["csbk"] = parse_csbk(payload)
    return rec


def _emb_record(s: int, emb: dict, inverted: bool) -> dict:
    return {"index": int(s), "sync": None, "sync_errors": None,
            "inverted": bool(inverted), "kind": "voice_emb",
            "direction": None, "slot": None, "slot_source": None,
            "colour_code": emb["colour_code"], "data_type": None,
            "data_type_name": None, "slot_type_errors": None,
            "bptc_ok": None, "bptc_corrected": None, "payload_hex": None,
            "lc": None, "csbk": None, "emb": emb, "voice_bits": 216,
            "encrypted": bool(emb["pi"]),
            "encryption_evidence": ("privacy indicator set in the embedded "
                                    "signalling (EMB)") if emb["pi"] else None}


def _probe_embedded_voice(dibits, bursts, seen_starts, inverted):
    """Find voice bursts B-F around the bursts we already synced on.

    Those bursts have no sync word — the EMB stands in its place — so they cannot
    be found by searching for one. Instead we step whole TDMA frames away from a
    confirmed burst and test whether an EMB validates there. Sixteen bits of
    (16,7,6) alone would false-alarm constantly — 17536 of the 65536 possible
    words (26.8 %) sit within 2 bits of some codeword — so a hit only counts as
    evidence when it ALSO agrees on the colour code:

      * with a colour code already proven by a Golay slot type, the EMB must
        match it;
      * with no colour code known anywhere in the capture, only ERROR-FREE EMBs
        count, and only where at least two of them agree with each other.

    Anything failing either leg is simply not reported."""
    n = np.asarray(dibits).size
    known = {b["colour_code"] for b in bursts if b["colour_code"] is not None}
    cands = []
    for b in bursts:
        for k in range(-EMB_PROBE_FRAMES, EMB_PROBE_FRAMES + 1):
            if k == 0:
                continue
            s = b["index"] + k * TDMA_FRAME_DIBITS
            if s < 0 or s + BURST_DIBITS > n or s in seen_starts:
                continue
            bits = burst_bits(dibits, s)
            if bits is None:
                continue
            emb = parse_emb(bits)
            if emb is None:
                continue
            if known:
                if emb["colour_code"] not in known:
                    continue
            elif emb["errors"] != 0:
                continue
            seen_starts.add(s)
            cands.append(_emb_record(s, emb, inverted))
    if known:
        return cands
    keep = [cc for cc in {c["colour_code"] for c in cands}
            if sum(1 for c in cands if c["colour_code"] == cc) >= 2]
    return [c for c in cands if c["colour_code"] in keep]


def _assign_relative_slots(bursts):
    """DMR alternates slots every TDMA frame. Without the CACH we cannot name the
    ABSOLUTE slot of a BS-sourced burst, so bursts whose sync did not tell us
    are labelled with a RELATIVE 1/2 derived from the TDMA-frame parity of their
    position, and slot_source says so. Never present this as the real slot."""
    anchor = None
    for b in bursts:
        if b["slot"] is not None and b["slot_source"] == "sync":
            anchor = b
            break
    base = bursts[0]["index"] if bursts else 0
    base_slot = 1
    if anchor is not None:
        base = anchor["index"]
        base_slot = anchor["slot"]
    for b in bursts:
        if b["slot"] is not None:
            continue
        frames = int(round((b["index"] - base) / float(TDMA_FRAME_DIBITS)))
        b["slot"] = 1 + ((base_slot - 1 + frames) % 2)
        b["slot_source"] = "alternation"


# ---------------------------------------------------------------- aggregation
def _encryption_summary(encrypted, evidence):
    """Plain-language, and never a euphemism. Encrypted traffic is a FINDING for
    a sweep, so it is stated outright; 'unknown' is stated outright too rather
    than being allowed to read as 'clear'."""
    if encrypted is True:
        return "ENCRYPTED (%s)" % (evidence or "privacy indicated")
    if encrypted is False:
        return "clear, not encrypted"
    return "encryption state unknown (no link control or EMB captured)"


def _slot_phrase(slot, source) -> str:
    if slot is None:
        return ""
    if source == "sync":
        return ", slot %d" % slot
    return ", slot %d (relative — no CACH, so the absolute slot is unknown)" % slot


def _aggregate(bursts):
    """Per-burst records -> the call/event list the router and the UI show.

    Encryption evidence is merged ACROSS the capture: a privacy bit in one
    header, a PI header, or a PI bit in any voice burst of the same colour code
    all mark the traffic encrypted. Under-reporting encryption would be the
    worse error for a sweep."""
    enc_by_cc, evidence_by_cc = {}, {}
    for b in bursts:
        cc = b["colour_code"]
        if b["encrypted"]:
            enc_by_cc[cc] = True
            evidence_by_cc.setdefault(cc, b["encryption_evidence"])
        elif b["encrypted"] is False:
            enc_by_cc.setdefault(cc, False)

    msgs, seen = [], set()
    for b in bursts:
        lc = b["lc"]
        if not lc:
            continue
        key = ("lc", b["colour_code"], lc["flco"], lc["dst"], lc["src"])
        if key in seen:
            continue
        seen.add(key)
        cc = b["colour_code"]
        encrypted = enc_by_cc.get(cc, lc["privacy"])
        evidence = evidence_by_cc.get(cc) or b["encryption_evidence"]
        who = ("talkgroup %d" % lc["dst"] if lc["flco"] == FLCO_GROUP_VOICE
               else "to radio %d" % lc["dst"])
        msgs.append({
            "type": "voice_call", "protocol": "DMR",
            "colour_code": cc, "slot": b["slot"], "slot_source": b["slot_source"],
            "talkgroup": lc["talkgroup"], "target_radio": lc["target_radio"],
            "source": lc["src"], "flco": lc["flco"], "flco_name": lc["flco_name"],
            "emergency": lc["emergency"], "encrypted": encrypted,
            "encryption_evidence": evidence,
            "data_type": b["data_type_name"], "bits_corrected": b["bptc_corrected"],
            "text": "DMR %s call — %s, from radio %d, colour code %s%s%s — %s" % (
                lc["flco_name"] or "link control", who, lc["src"], cc,
                _slot_phrase(b["slot"], b["slot_source"]),
                ", EMERGENCY" if lc["emergency"] else "",
                _encryption_summary(encrypted, evidence)),
        })

    for b in bursts:
        if b["data_type"] != DT_CSBK or not b["csbk"]:
            continue
        key = ("csbk", b["colour_code"], b["csbk"]["csbko"], b["csbk"]["payload_hex"])
        if key in seen:
            continue
        seen.add(key)
        msgs.append({
            "type": "csbk", "protocol": "DMR", "colour_code": b["colour_code"],
            "slot": b["slot"], "slot_source": b["slot_source"],
            "csbko": b["csbk"]["csbko"], "fid": b["csbk"]["fid"],
            "payload_hex": b["csbk"]["payload_hex"], "encrypted": None,
            "text": "DMR control burst (CSBK opcode 0x%02X, colour code %s) — "
                    "signalling, not voice; opcode payload not decoded"
                    % (b["csbk"]["csbko"], b["colour_code"]),
        })

    # Voice traffic with no link control captured is still a real finding: the
    # colour code and the encryption state are usually all a sweep needs.
    if not any(m["type"] == "voice_call" for m in msgs):
        voice = [b for b in bursts if b["kind"] in ("voice", "voice_emb")]
        if voice:
            ccs = sorted({b["colour_code"] for b in voice if b["colour_code"] is not None})
            cc = ccs[0] if ccs else None
            encrypted = enc_by_cc.get(cc)
            evidence = evidence_by_cc.get(cc)
            msgs.append({
                "type": "voice_activity", "protocol": "DMR", "colour_code": cc,
                "slot": voice[0]["slot"], "slot_source": voice[0]["slot_source"],
                "talkgroup": None, "source": None, "encrypted": encrypted,
                "encryption_evidence": evidence, "bursts": len(voice),
                "text": "DMR voice traffic — %d burst(s), colour code %s — %s; no "
                        "link control header in this capture, so talkgroup and "
                        "radio ID are unknown" % (
                            len(voice), cc if cc is not None else "unknown",
                            _encryption_summary(encrypted, evidence)),
            })

    for b in bursts:
        if b["data_type"] != DT_PI_HEADER or not b["payload_hex"]:
            continue
        key = ("pi", b["colour_code"], b["payload_hex"])
        if key in seen:
            continue
        seen.add(key)
        msgs.append({
            "type": "pi_header", "protocol": "DMR", "colour_code": b["colour_code"],
            "slot": b["slot"], "slot_source": b["slot_source"], "encrypted": True,
            "encryption_evidence": "privacy (PI) header present",
            "payload_hex": b["payload_hex"],
            "text": "DMR privacy header (colour code %s) — the call is ENCRYPTED. "
                    "Raw header %s; algorithm/key fields not parsed."
                    % (b["colour_code"], b["payload_hex"]),
        })
    return msgs


# ---------------------------------------------------------------- top level
def _empty_result(note: str, quality: float = 0.0) -> dict:
    return {"protocol": "DMR", "messages": [], "bursts": [], "note": note,
            "quality": float(quality),
            "stats": {"syncs": 0, "bursts": 0, "voice_bursts": 0,
                      "data_bursts": 0, "slot_type_ok": 0, "bptc_ok": 0,
                      "bptc_failed": 0, "emb_ok": 0, "colour_codes": [],
                      "inverted": False, "polarity_resolved": False}}


def _decode_with_polarity(dibits, inverted: bool):
    """Find and parse every burst under ONE receive polarity. Returns
    (bursts, stats, score); score is the weight of FEC evidence that this
    polarity is the right one."""
    d = invert_dibits(dibits) if inverted else dibits
    found = find_sync_bursts(d)
    bursts, seen = [], set()
    for start, name, errs in found:
        bits = burst_bits(d, start)
        if bits is None:
            continue
        bursts.append(_parse_burst(bits, name, errs, start, inverted))
        seen.add(start)
    bursts.extend(_probe_embedded_voice(d, list(bursts), seen, inverted))
    bursts.sort(key=lambda b: b["index"])
    stats = {
        "syncs": len(found), "bursts": len(bursts),
        "voice_bursts": sum(1 for b in bursts if b["kind"] in ("voice", "voice_emb")),
        "data_bursts": sum(1 for b in bursts if b["kind"] == "data"),
        "slot_type_ok": sum(1 for b in bursts if b["data_type"] is not None),
        "bptc_ok": sum(1 for b in bursts if b["bptc_ok"]),
        "bptc_failed": sum(1 for b in bursts if b["bptc_ok"] is False),
        "emb_ok": sum(1 for b in bursts if b["emb"]),
        "colour_codes": sorted({b["colour_code"] for b in bursts
                                if b["colour_code"] is not None}),
        "inverted": bool(inverted),
    }
    # Weighted because the three checks are not equally hard to fake. A
    # BPTC(196,96) product code coming back clean is near-conclusive; an EMB is
    # 16 bits and needs its colour-code corroboration; a Golay(20,8) slot type
    # accepts 33.0 % of all 20-bit words within its 3-bit radius, so it is worth
    # little on its own — and a slot type that decoded while its BPTC failed is
    # positive evidence of the WRONG polarity (a voice burst being read as data).
    score = (4 * stats["bptc_ok"] + 3 * stats["emb_ok"]
             + stats["slot_type_ok"] - stats["bptc_failed"])
    return bursts, stats, score


def decode_dmr(iq, fs: float, symbols=None) -> dict:
    """Top level: complex IQ -> DMR bursts, calls, and encryption state.

    `symbols` may be a c4fm.demod_c4fm() result already computed by a caller
    (the router hands the same IQ to several decoders; re-running the front end
    per protocol is pure waste). Returns:

        {protocol, messages, bursts, note, quality, stats}

    messages: the aggregated call/control events (see _aggregate) — each with a
    plain-English `text`, a `talkgroup`/`source` where a link control word was
    recovered, and an `encrypted` tri-state (True / False / None-for-unknown).
    bursts: every accepted burst with its FEC provenance, for an analyst.

    Never raises on no-signal; only a too-short capture (<16 samples) raises
    ValueError, mirroring decode_pocsag()."""
    if symbols is None:
        symbols = demod_c4fm(iq, fs)
    dibits = symbols.get("dibits")
    if dibits is None or np.asarray(dibits).size < BURST_DIBITS:
        return _empty_result(symbols.get("note") or "no C4FM symbols recovered",
                             symbols.get("quality", 0.0))
    dibits = np.asarray(dibits, dtype=np.uint8)
    quality = float(symbols.get("quality", 0.0))

    # Resolve receive polarity from FEC evidence, not from the sync word — the
    # sync words are polarity pairs and cannot tell us (see SYNC_PATTERNS).
    normal = _decode_with_polarity(dibits, False)
    flipped = _decode_with_polarity(dibits, True)
    bursts, stats, _score = normal if normal[2] >= flipped[2] else flipped
    # Ties mean nothing in the capture distinguished the two polarities (a
    # voice-only fragment with no FEC anywhere). Say so rather than imply the
    # voice/data split below is settled.
    stats["polarity_resolved"] = normal[2] != flipped[2]
    if not bursts:
        return _empty_result("no DMR sync pattern found", quality)
    _assign_relative_slots(bursts)

    # HONESTY GATE. A 48-bit sync is already hard to fake, but a lone sync in a
    # long noise capture is exactly the kind of thing this codebase refuses to
    # report. Demand either an independent FEC block that validated (BPTC or a
    # corroborated EMB), or three separate sync hits. A Golay slot type on its
    # own is NOT enough — 33 % of random 20-bit words decode as one.
    fec_ok = stats["bptc_ok"] or stats["emb_ok"]
    if not fec_ok and stats["syncs"] < MIN_SYNCS_WITHOUT_FEC:
        return _empty_result(
            "%d DMR sync match(es) but no FEC block validated — not reported"
            % stats["syncs"], quality)

    messages = _aggregate(bursts)
    note = "DMR: %d burst(s), colour code(s) %s" % (
        len(bursts), ", ".join(str(c) for c in stats["colour_codes"]) or "unknown")
    if any(m.get("encrypted") for m in messages):
        note += " — ENCRYPTED traffic present"
    if not stats["polarity_resolved"]:
        note += (" — receive polarity unresolved (no FEC block distinguished the "
                 "two, so the voice/data split is provisional)")
    return {"protocol": "DMR", "messages": messages, "bursts": bursts,
            "note": note, "quality": quality, "stats": stats}


def looks_like_dmr(feats: dict) -> bool:
    """Cheap pre-gate from identify.iq_features(): the C4FM signature (4-level
    FSK, ~4.8 kBd) in a 12.5 kHz-class channel. Same shape as P25 Phase 1 — the
    two are separated by their sync words, not by their spectrum."""
    return c4fm.looks_like_c4fm(feats)


# ---------------------------------------------------------------- synth/encoder
def _dotting_dibits(n: int):
    """Filler for the inter-burst guard/CACH interval and the acquisition
    preamble: alternating outer rails, which is what a real dotting sequence
    looks like and cannot collide with a sync word."""
    return np.array([1 if i % 2 == 0 else 3 for i in range(n)], dtype=np.uint8)


def build_data_burst(sync_name: str, colour_code: int, data_type: int, payload96):
    """A 264-bit data/control burst: BPTC(196,96) info either side of the sync,
    with the Golay(20,8) slot type wrapped around it."""
    if sync_name not in SYNC_PATTERNS:
        raise ValueError("unknown sync %r" % (sync_name,))
    info = bptc_encode(payload96)
    slot = golay_20_8_encode(((colour_code & 0xF) << 4) | (data_type & 0xF))
    bits = list(info[:INFO1[1]])
    bits += _int_to_bits(slot >> 10, 10)
    bits += _int_to_bits(SYNC_PATTERNS[sync_name], SYNC_BITS)
    bits += _int_to_bits(slot & 0x3FF, 10)
    bits += list(info[INFO1[1]:])
    assert len(bits) == BURST_BITS
    return bits


def build_voice_burst(sync_name=None, emb=None, voice_seed: int = 0):
    """A 264-bit voice burst. With `sync_name` this is burst A (voice sync in the
    middle); with `emb` = (colour_code, pi, lcss) it is a burst B-F carrying the
    EMB + embedded signalling instead. The 216 voice bits are pseudo-random
    stand-ins for AMBE frames — this module never claims to encode voice."""
    rng = np.random.default_rng(voice_seed)
    v = rng.integers(0, 2, 216).astype(int).tolist()
    bits = v[:108]
    if sync_name is not None:
        bits += _int_to_bits(SYNC_PATTERNS[sync_name], SYNC_BITS)
    elif emb is not None:
        cw = emb_encode(pack_emb(*emb))
        bits += _int_to_bits(cw >> 8, 8)
        bits += rng.integers(0, 2, 32).astype(int).tolist()   # embedded signalling
        bits += _int_to_bits(cw & 0xFF, 8)
    else:
        raise ValueError("a voice burst needs either a sync name or an EMB")
    bits += v[108:]
    assert len(bits) == BURST_BITS
    return bits


def encode_dmr(bursts, fs: float, gap_bits: int = TDMA_FRAME_BITS - BURST_BITS,
               preamble_dibits: int = 48, invert: bool = False,
               noise: float = 0.0, seed: int = 0, **c4fm_kw):
    """Burst bit-lists -> complex C4FM IQ (test / self-check path).

    Bursts are laid on the real 30 ms TDMA grid — 264 burst bits plus a 24-bit
    guard/CACH interval = 288 bits per frame — because _probe_embedded_voice()
    steps on exactly that grid to find the sync-less voice bursts. A preamble of
    dotting up front gives the timing loop something to acquire on."""
    stream = [_dotting_dibits(preamble_dibits)]
    for i, b in enumerate(bursts):
        if len(b) != BURST_BITS:
            raise ValueError("burst %d is %d bits, expected %d" % (i, len(b), BURST_BITS))
        stream.append(c4fm.bits_to_dibits(np.asarray(b, dtype=np.uint8)))
        if gap_bits:
            stream.append(_dotting_dibits(gap_bits // 2))
    stream.append(_dotting_dibits(preamble_dibits))
    dibits = np.concatenate(stream)
    if invert:
        dibits = invert_dibits(dibits)
    return c4fm.encode_c4fm(dibits, fs, noise=noise, seed=seed, **c4fm_kw)
