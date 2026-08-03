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
SYNC_MAX_ERRS *and* some FEC independent of it validates — a clean BPTC(196,96)
product code, or an EMB corroborated on its colour code — or, failing that, when
MIN_SYNCS_WITHOUT_FEC COHERENT syncs are found (same sync word, same TDMA grid
phase, i.e. plausibly one transmitter rather than several coincidences). Noise,
analog FM and 2-FSK therefore return an empty message list, not invented
talkgroups. Every emitted field carries its provenance (sync_errors,
slot_type_errors, bptc_ok, bptc_corrected, rs_checked, rs_ok) and every emitted
MESSAGE carries an `evidence` string naming what stands behind it, so a marginal
decode is visible rather than silent.

WHAT IS PROVEN, WHAT IS CORROBORATED, WHAT IS STILL ASSUMED
-----------------------------------------------------------
Sources: ETSI TS 102 361-1 V2.7.1 (2026-05), free from etsi.org — clauses 6-9 for
the burst layout and annex B for every code below. Nothing in this module is
derived from op25, dsd, dsd-fme, jmbe, dmrlib, dmrshark or mbelib-neo; those are
GPL and this is closed-source commercial work. (mbelib/szechyjs is ISC and would
be usable with attribution, but nothing from it is used here either.) The spec is
the only source, which is also why the constants below can be cited by clause.

PROVEN — checked structurally against published external data, by enumerating a
whole codebook and asserting EQUALITY, never by round-tripping this file's
encoder against its own decoder:
  * the ten sync patterns of table 9.2, including the polarity-pair property
  * Golay(20,8) == generator matrix table B.11         (all 256 codewords)
  * EMB quadratic-residue (16,7,6) == table B.12       (all 128 codewords)
  * Hamming(15,11,3) == table B.15                     (all 2048 codewords)
  * Hamming(13,9,3) == table B.14                      (all 512 codewords)
  * BPTC(196,96) bit ordering == tables B.2/B.3. The x181 interleave, the 13x15
    layout, the three reserved info bits R(0..2) and the fourth pad bit R(3) at
    index 0 were all flagged by a previous revision of this docstring as the most
    likely one-constant fix once real traffic was seen. They are now CONFIRMED
    CORRECT: four worked entries of table B.3 are recomputed from these constants
    in the tests, which also fixes the row-parity order as H_R(3) leftmost.
  * RS(12,9,4) generator, rebuilt from its roots and matched to eq. B.10, and its
    generator matrix table B.18 reproduced row by row
  * the minimum distance of every code above

  Two of those checks changed the code rather than confirming it. EMB_QR_POLY was
  0x1D1 and is now 0x139 (eq. B.5): the old constant produced a valid (16,7,6)
  code, so the distance test passed and encode/decode round-tripped perfectly,
  but it was a DIFFERENT code with a disjoint codeword set and every EMB in real
  traffic would have failed. That is precisely the matched-pair-of-bugs failure
  this codebase's proof standard exists to catch, and only codebook equality
  against the published matrix catches it.

CORROBORATED — computed and reported, but deliberately NOT used as a gate:
  * the RS(12,9) outer checksum on the 72-bit Full LC (B.3.6), including the
    per-data-type CRC mask of table B.21 that is XORed onto the three parity
    octets before the BPTC encoder (Voice LC Header 0x969696, Terminator with LC
    0x999999). The first-consecutive-root convention that the previous build
    correctly refused to guess is stated outright in eq. B.9 — the generator is
    (x+a)(x+a^2)(x+a^3), so FCR = 1, not 0.
    It is corroboration and not a gate on purpose: if any part of this were still
    wrong the failure mode is a call marked "unverified", never a real call
    silently discarded. lc["rs_checked"] says whether the check could run at all
    and lc["rs_ok"] says whether it passed; both are reported.
    Because the mask differs per data type, an RS pass also confirms the Golay
    slot type's data-type field, which is why it is worth this much.
  * colour-code agreement across bursts, and the plausibility of decoded
    addresses (a source or destination of 0). Both are weak on their own and are
    only ever used to *withhold* a claim, never to manufacture one.

STILL NOT IMPLEMENTED — say so rather than fake it:
  * CACH, so a BS-sourced burst's ABSOLUTE slot number is unknown — only direct
    mode names its slot. Slots inferred from burst ordering are labelled
    slot_source="alternation" and are RELATIVE.
  * PI-header field layout (the header is reported as encrypted, with its raw
    payload hex, but its algorithm/key/IV fields are not parsed).
  * the CRC-CCITT carried by CSBK / MBC / data headers. Their 16-bit masks are
    listed in DATA_TYPE_CRC_MASK for reference but no CRC is computed, so a CSBK
    rests on its BPTC alone.
  * Voice payload: the AMBE frames are located and counted, never decoded. See
    survey/ambe.py.
  * Long/3rd-party LC formats, CSBK opcode semantics, and data-message reassembly.

VALIDATION STATUS (Matt's pref #4): NOTHING here has been run against a real
off-air DMR signal. Everything above is the published spec plus synthetic
traffic built by the encoder at the bottom of this file. What that buys is that a
wrong constant now fails against ETSI's own tables instead of agreeing with
itself — but it is not the same as having heard a radio.

FALSE-ACCEPT BUDGET, measured rather than hoped for. n = 6000 for every cell,
both columns, the before column run against the previous revision of this file so
the comparison is like for like. The construction is a random C4FM symbol stream
carrying planted real sync words, decoded at the symbol layer with the DSP front
end bypassed, so the number is a property of the framing and FEC gate rather than
of the demodulator. Two rates are tracked because they are not the same failure:
ACCEPTED means any message at all, FABRICATED means a message asserting a colour
code, talkgroup, radio ID or encryption state — the claim that reaches a client
report, and the one that matters.

                              BEFORE                      AFTER
                          accepted  fabricated      accepted  fabricated
    random symbols          0.000 %    0.000 %       0.000 %    0.000 %
    + 1 planted sync        4.200 %    4.200 %       0.000 %    0.000 %
    + 2 planted syncs       8.967 %    8.967 %       0.000 %    0.000 %
    + 3 planted syncs      80.233 %   13.467 %       0.450 %    0.000 %
    2 framed random bursts  8.600 %    8.583 %       0.000 %    0.000 %
    3 framed random bursts 45.667 %   11.817 %      39.900 %    0.000 %

FABRICATION IS ZERO EVERYWHERE. Not one of 36 000 noise trials produced a colour
code, a talkgroup, a radio ID or an encryption claim.

Detection was not traded away for that: a full superframe still yields the
talkgroup, the radio ID and an RS-verified link control 60/60 at every noise
level from 0.00 to 0.40, and the cliff between 0.48 and 0.56 is the C4FM front
end, not this gating. SYNC_MAX_ERRS stays at 4 because detection measured
IDENTICAL for tolerances 1 through 6 — the sync search is nowhere near the
binding constraint — so tightening it would be an unvalidated change with no
measured benefit.

The residual acceptances are the honest ones and they fabricate nothing: those
constructions contain three or more genuine, identical DMR sync words on the
exact TDMA grid, which is what a real DMR transmitter emits. The module reports
them as "DMR framing is present" with the colour code, talkgroup and encryption
state all stated as UNKNOWN and the text saying in terms that the evidence is the
sync pattern alone. Suppressing that would lose a real sweep finding — a DMR
transmitter you cannot decode is still a DMR transmitter — so it is reported and
graded instead of hidden.
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
MIN_SYNCS_WITHOUT_FEC = 3        # see the honesty gate in decode_dmr(). These
                                 #   must be COHERENT syncs — see _coherent_syncs
MAX_BURSTS = 512                 # bound the work on a long capture
EMB_PROBE_FRAMES = 6             # TDMA frames either side of an anchor to probe
EMB_MIN_AGREEING_UNANCHORED = 2  # EMB hits that must agree with EACH OTHER when
                                 #   nothing in the capture is proven. One 16-bit
                                 #   word is not a detection: 26.8 % of random
                                 #   words sit within the code's 2-bit correction
                                 #   radius. With a BPTC-proven colour code to
                                 #   match against, one EMB is enough — the
                                 #   anchor is what does the work, and the
                                 #   requirement scales with its strength.
                                 #   See _probe_embedded_voice.

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

# ---- Data Type CRC Mask, TS 102 361-1 table B.21 ----
# "A unique Data Type CRC Mask shall be applied to the entire CRC portion of the
# data or control burst information contents ... For transmission, the CRC mask
# shall be applied after the CRC calculation and before the FEC encoding ... For
# reception, the CRC mask shall be applied after the FEC decoding and before the
# CRC check." (B.3.12)
#
# This table is the reason a naive RS check fails on real traffic and then gets
# blamed on the root convention: the three RS parity octets on air are the
# computed ones XORed with a mask that DEPENDS ON THE DATA TYPE. Getting it
# right also makes the check far more informative than a plain checksum, because
# a Voice LC Header and a Terminator with LC carry identical link control and are
# distinguished ONLY by their mask — so an RS pass simultaneously confirms the
# payload AND the Golay slot type's data-type field.
DATA_TYPE_CRC_MASK = {
    DT_PI_HEADER: 0x6969,             # 16-bit (CRC-CCITT, not implemented here)
    DT_VOICE_LC_HEADER: 0x969696,     # 24-bit -> the RS(12,9) parity
    DT_TERMINATOR_LC: 0x999999,       # 24-bit -> the RS(12,9) parity
    DT_CSBK: 0xA5A5,                  # 16-bit
    0x4: 0xAAAA,                      # MBC header,   16-bit
    0x6: 0xCCCC,                      # Data header,  16-bit
    0xB: 0x3333,                      # Unified single block data, 16-bit
}
# Only the two 24-bit entries belong to the Full LC's RS(12,9) parity. The rest
# are listed because they are the same published table and an analyst reading
# this file should not have to go find them, but this module implements no
# CRC-CCITT, so it never applies them.
FULL_LC_RS_MASK = {dt: DATA_TYPE_CRC_MASK[dt] for dt in LC_DATA_TYPES}

# ---- Full Link Control ----
FLCO_GROUP_VOICE = 0x00       # Group Voice Channel User  -> dst is a TALKGROUP
FLCO_UNIT_VOICE = 0x03        # Unit to Unit Voice Channel User -> dst is a radio
FLCO_NAMES = {FLCO_GROUP_VOICE: "group voice", FLCO_UNIT_VOICE: "unit-to-unit voice"}
SVC_EMERGENCY_BIT = 0x80      # service options bit 7
SVC_PRIVACY_BIT = 0x40        # service options bit 6 — THE encryption indicator

# ---- FEC generators. Each is pinned to a PUBLISHED ETSI generator matrix by an
# ---- exhaustive codebook-equality test, not by faith and not by round-trip ----
GOLAY_23_12_POLY = 0xC75      # x^11+x^10+x^6+x^5+x^4+x^2+1 (TS 102 361-1 B.3.1,
                              #   eq. B.4 = 6165 octal); extended to (24,12,8) by
                              #   an overall parity bit and shortened by 4 info
                              #   bits gives the DMR Golay(20,8). Reproduces
                              #   table B.11 exactly — test_golay_matches_etsi_
                              #   generator_matrix.
EMB_QR_POLY = 0x139           # x^8+x^5+x^4+x^3+1 (TS 102 361-1 B.3.2, eq. B.5 =
                              #   0471 octal). The EMB code is a shortened
                              #   QUADRATIC RESIDUE (16,7,6), not a BCH code:
                              #   the primitive (17,9,5) QR code loses its first
                              #   two info bits and gains one overall parity bit.
                              #   Reproduces table B.12 exactly.
                              #   HISTORY: this constant was 0x1D1 until the spec
                              #   was actually read. 0x1D1 also yields a (16,7,6)
                              #   code, so the minimum-distance test passed and
                              #   the encoder/decoder pair round-tripped
                              #   perfectly — but it is a DIFFERENT code (127 of
                              #   128 codewords differ, and the codeword SETS are
                              #   disjoint), so every EMB in real off-air traffic
                              #   would have failed. Exactly the matched pair of
                              #   bugs this codebase's proof standard exists to
                              #   catch; only equality against the published
                              #   generator matrix catches it.
# Hamming parity masks over the data word, d0 in the HIGH bit. These are columns
# 11..14 of TS 102 361-1 table B.15 and columns 9..12 of table B.14, in the
# spec's own left-to-right order (which is H_R(3), H_R(2), H_R(1), H_R(0) — see
# the BPTC section below). Verified by codebook equality, not by inspection.
HAMMING_15_11_MASKS = (0b11110101100, 0b01111010110, 0b00111101011, 0b11101011001)
HAMMING_13_9_MASKS = (0b110101100, 0b111010110, 0b111101011, 0b101011001)

# ---- BPTC(196,96), TS 102 361-1 B.1.1 ----
# CONFIRMED against the spec, not assumed. Every convention below was checked
# against clause B.1.1 and its two worked tables (B.2 "Interleaving indices" and
# B.3 "Transmit bit ordering"), and four independent entries of table B.3 were
# reproduced from these constants — see test_bptc_layout_matches_etsi_table_b3,
# which recomputes them rather than trusting this comment.
BPTC_BITS = 196
BPTC_INTERLEAVE_STEP = 181    # "Interleave Index = Index x 181 modulo 196"
                              #   (B.1.1 eq. B.1). deinterleaved[i] = raw[i*181 % 196]
BPTC_ROWS = 13                # rows 0..8 data, rows 9..12 column-parity (H_C)
BPTC_COLS = 15                # cols 0..10 data, cols 11..14 row-parity (H_R)
BPTC_DATA_ROWS = 9
BPTC_DATA_COLS = 11
BPTC_RESERVED_INFO_BITS = 3   # R(0..2) lead the 99-bit info field, then I(95)
                              #   down to I(0) row-major; payload is those 96.
                              #   deinterleaved[0] is a 4th reserved bit R(3),
                              #   OUTSIDE the 13x15 matrix — table B.3 index 0 is
                              #   named R(3) and this is what pads 195 -> 196.
# Worked entries of table B.3 that pin the whole permutation. Each is
# (interleave_index, row, col) — the bit named at that interleave index really
# does live at that matrix cell under the constants above:
#   idx   0 -> R(3)     (outside the matrix)
#   idx   1 -> H_R1(2)  = row 0, col 12
#   idx   2 -> I(77)    = row 1, col 10
#   idx  66 -> H_R5(1)  = row 4, col 13
#   idx 132 -> H_C13(3) = row 9, col 12
# The col-12/col-13 entries are what fix the ROW-PARITY ORDER as H_R(3), H_R(2),
# H_R(1), H_R(0) left to right, which is the ordering HAMMING_*_MASKS use.
BPTC_TABLE_B3_ANCHORS = ((1, 0, 12), (2, 1, 10), (66, 4, 13), (132, 9, 12))
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
    """7 data bits -> 16-bit codeword (data in bits 15..9). Shortened quadratic
    residue (16,7,6) of TS 102 361-1 B.3.2: the primitive (17,9,5) QR code
    generated by EMB_QR_POLY, shortened by two info bits, then extended with one
    overall parity bit. Pinned to table B.12 by
    test_emb_matches_etsi_generator_matrix (codebook equality), and to d=6 by
    test_emb_code_minimum_distance_is_6."""
    reg = (data7 & 0x7F) << 8
    cw15 = reg | _gf2_remainder(reg, EMB_QR_POLY, 8)
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


# ---------------------------------------------------------------- RS(12,9)
# TS 102 361-1 B.3.6. The parameters that matter, all from the published text:
#
#   field       GF(2^8) modulo a^8 + a^4 + a^3 + a^2 + 1  (eq. B.14) = 0x11D
#   code        (12,9,4), SHORTENED from (255,252,4) by nulling the first 243
#               message symbols — so it is a plain systematic RS, no puncturing
#   generator   G(x) = (x + a)(x + a^2)(x + a^3)          (eq. B.9)
#               g(x) = x^3 + 0E x^2 + 38 x + 40           (eq. B.10)
#   FIRST CONSECUTIVE ROOT = 1.  This is the convention the previous build could
#               not source and correctly refused to guess. It is written plainly
#               in eq. B.9: the roots are a^1, a^2, a^3, NOT a^0, a^1, a^2.
#   layout      c = (m8 .. m0, p2, p1, p0) (eq. B.13) — 9 message octets, then
#               the 3 parity octets, message first, most significant octet first
#
# Two independent checks pin this, both in tests/test_dmr.py and both structural:
# expanding (x+a)(x+a^2)(x+a^3) over the field must reproduce eq. B.10's
# 01 0E 38 40, and long-dividing by that g(x) must reproduce all nine rows of the
# published generator matrix (table B.18). FCR=0 gives 01 07 0E 08 and FCR=2
# gives 01 1C E0 3A, neither of which is the spec's polynomial, so the test
# distinguishes the conventions rather than merely being consistent with one.
RS_PRIMITIVE_POLY = 0x11D
RS_FIRST_CONSECUTIVE_ROOT = 1
RS_GENERATOR = (0x01, 0x0E, 0x38, 0x40)   # g(x), highest power first
RS_PARITY_OCTETS = 3
RS_MESSAGE_OCTETS = 9
# Table B.18, parity columns only (the left 9x9 block is the identity because the
# code is systematic). Row i is the parity triple contributed by message octet i.
RS_GENERATOR_MATRIX_PARITY = (
    (0x1C, 0xBC, 0xFD), (0x89, 0x31, 0x08), (0xAD, 0x41, 0x36),
    (0x7D, 0x71, 0x16), (0xF3, 0xA6, 0x3A), (0x08, 0x83, 0x7B),
    (0x3F, 0x6F, 0x02), (0x6C, 0x0D, 0xA7), (0x0E, 0x38, 0x40),
)


def _gf_tables():
    exp, log = [0] * 512, [0] * 256
    x = 1
    for i in range(255):
        exp[i] = x
        log[x] = i
        x <<= 1
        if x & 0x100:
            x ^= RS_PRIMITIVE_POLY
    for i in range(255, 512):
        exp[i] = exp[i - 255]
    return exp, log


_GF_EXP, _GF_LOG = _gf_tables()


def gf_mul(a: int, b: int) -> int:
    """Multiply in GF(2^8) as DMR defines it. Exposed because the tests rebuild
    the generator polynomial from its roots with it."""
    if a == 0 or b == 0:
        return 0
    return _GF_EXP[_GF_LOG[a & 0xFF] + _GF_LOG[b & 0xFF]]


def rs_12_9_parity(message9):
    """9 message octets -> the 3 RS(12,9) parity octets, in on-air order.

    Systematic encoding: the parity is the remainder of message(x)*x^3 modulo
    g(x). UNMASKED — the caller applies the Data Type CRC Mask."""
    msg = [int(m) & 0xFF for m in message9]
    if len(msg) != RS_MESSAGE_OCTETS:
        raise ValueError("RS(12,9) takes 9 message octets")
    reg = msg + [0] * RS_PARITY_OCTETS
    for i in range(RS_MESSAGE_OCTETS):
        c = reg[i]
        if c:
            for j in range(len(RS_GENERATOR)):
                reg[i + j] ^= gf_mul(RS_GENERATOR[j], c)
    return reg[RS_MESSAGE_OCTETS:]


def _mask_octets(mask: int):
    return [(mask >> 16) & 0xFF, (mask >> 8) & 0xFF, mask & 0xFF]


def rs_12_9_check(payload96, data_type):
    """(checked, ok) for a 96-bit Full LC BPTC payload.

    `checked` is False — NOT a failure — when this data type has no defined
    24-bit mask in table B.21, which is the honest answer for anything that is
    not a Voice LC Header or a Terminator with LC.

    This is CORROBORATION, never a gate. See _parse_burst: a False `ok` lowers
    confidence and is reported, it does not discard the link control. That
    ordering is deliberate — if any part of this convention were still wrong,
    the failure mode is 'unverified', not 'real calls silently dropped'."""
    mask = FULL_LC_RS_MASK.get(data_type)
    if mask is None:
        return False, False
    b = list(payload96)
    if len(b) != 96:
        return False, False
    msg = [_bits_to_int(b, i * 8, 8) for i in range(RS_MESSAGE_OCTETS)]
    on_air = [_bits_to_int(b, 72 + i * 8, 8) for i in range(RS_PARITY_OCTETS)]
    unmasked = [p ^ m for p, m in zip(on_air, _mask_octets(mask))]
    return True, rs_12_9_parity(msg) == unmasked


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


def parse_full_lc(payload96, data_type=None) -> dict:
    """96-bit BPTC payload -> parsed Full LC.

    Bits 0..71 are the link control; bits 72..95 are the RS(12,9) parity, masked
    per table B.21. Pass `data_type` (from the Golay slot type) to have the RS
    verified; without it the parity is still carried through as hex and
    rs_checked stays False."""
    b = list(payload96)
    flco = _bits_to_int(b, 2, 6)
    svc = _bits_to_int(b, 16, 8)
    dst = _bits_to_int(b, 24, 24)
    src = _bits_to_int(b, 48, 24)
    rs_checked, rs_ok = rs_12_9_check(b, data_type)
    return {
        "pf": b[0], "flco": flco, "flco_name": FLCO_NAMES.get(flco),
        "fid": _bits_to_int(b, 8, 8), "service_options": svc,
        "emergency": bool(svc & SVC_EMERGENCY_BIT),
        "privacy": bool(svc & SVC_PRIVACY_BIT),
        "dst": dst, "src": src,
        "talkgroup": dst if flco == FLCO_GROUP_VOICE else None,
        "target_radio": dst if flco == FLCO_UNIT_VOICE else None,
        "rs_parity_hex": _bits_hex(b[72:96]),
        "rs_checked": rs_checked, "rs_ok": bool(rs_checked and rs_ok),
        "plausible": lc_fields_plausible(dst, src),
    }


def lc_fields_plausible(dst: int, src: int) -> bool:
    """Cheap sanity check on the two decoded addresses.

    A DMR source address is the transmitting radio's individual ID and is never
    0; a destination of 0 is likewise not an assignable talkgroup or radio. Noise
    that survives everything else still has to produce two non-zero 24-bit
    numbers, and all-zero is by far its most likely output when the BPTC decoder
    has flattened a block. This is a WEAK check used only as one term of the
    corroboration score — it is not a gate on its own, because a plausible
    address is not evidence of anything."""
    return dst != 0 and src != 0


def build_lc_payload(flco: int, fid: int, service_options: int, dst: int, src: int,
                     data_type: int = DT_VOICE_LC_HEADER):
    """A complete 96-bit BPTC payload: the 72-bit Full LC plus REAL RS(12,9)
    parity, masked for `data_type` per table B.21.

    Used by the tests so the round trip exercises the RS path end to end instead
    of shipping 24 zero bits and declaring the checksum untested."""
    lc = pack_full_lc(flco, fid, service_options, dst, src)
    msg = [_bits_to_int(lc, i * 8, 8) for i in range(RS_MESSAGE_OCTETS)]
    mask = _mask_octets(FULL_LC_RS_MASK.get(data_type, 0))
    out = list(lc)
    for p, m in zip(rs_12_9_parity(msg), mask):
        out += _int_to_bits(p ^ m, 8)
    return out


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
           "rs_checked": False, "rs_ok": False,
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
        lc = parse_full_lc(payload, dt)
        rec["lc"] = lc
        rec["rs_checked"], rec["rs_ok"] = lc["rs_checked"], lc["rs_ok"]
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
            "rs_checked": False, "rs_ok": False,
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
    evidence when it ALSO agrees on a colour code, AND at least two hits agree
    with each other.

    THE ANCHOR MUST BE PROVEN, AND THIS IS THE WHOLE POINT. An earlier build
    anchored on any colour code seen anywhere, including one read out of a Golay
    slot type whose own BPTC had FAILED. That is circular: a Golay(20,8) decodes
    33 % of random 20-bit words, so noise supplied its own anchor, one 2-error
    EMB in ~12 probe positions then "agreed" with it, and the agreement was
    counted as the independent FEC evidence that opened the honesty gate. It was
    the single largest source of fabricated DMR detections — every measured false
    accept had exactly that signature. An anchor is now only a colour code from a
    burst whose BPTC(196,96) PRODUCT CODE validated, which noise does not fake
    (0 of 6000 random blocks).

    Two legs, and what each demands scales with how strong its anchor is:
      * ANCHORED — the colour code was proven by a clean BPTC block. That block
        is the evidence; matching it is a 1-in-16 coincidence on top of
        something noise does not produce at all, so a single EMB within the
        code's 2-bit correction radius counts.
      * UNANCHORED — nothing in the capture is proven, so the EMBs have to
        corroborate each other instead: only ERROR-FREE ones count, and at least
        EMB_MIN_AGREEING_UNANCHORED of them must agree on the same colour code.
        0.195 % of random 16-bit words are codewords exactly, against 26.8 %
        within 2 bits, so this leg is ~137x harder to fake per probe position.

    Anything failing its leg is simply not reported."""
    n = np.asarray(dibits).size
    proven = {b["colour_code"] for b in bursts
              if b.get("bptc_ok") and b["colour_code"] is not None}
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
            if proven:
                if emb["colour_code"] not in proven:
                    continue
            elif emb["errors"] != 0:
                continue
            seen_starts.add(s)
            cands.append(_emb_record(s, emb, inverted))
    if proven:
        # every candidate already had to match a BPTC-verified colour code
        return cands
    keep = {cc for cc in {c["colour_code"] for c in cands}
            if sum(1 for c in cands if c["colour_code"] == cc)
            >= EMB_MIN_AGREEING_UNANCHORED}
    return [c for c in cands if c["colour_code"] in keep]


def _coherent_syncs(found) -> int:
    """The largest set of sync hits that could belong to ONE transmitter.

    Counting raw sync hits was the other way noise got in. Three unrelated 48-bit
    matches scattered through a capture are three coincidences, not a call, and
    the old gate accepted them with no FEC at all — measured at 80 % acceptance
    on random symbol streams carrying three planted syncs.

    A real DMR transmitter is far more constrained than that. Every one of its
    bursts carries the SAME sync word (a base station's voice bursts are all
    bs_voice), and every burst starts on the same 30 ms TDMA grid, so the burst
    positions are all congruent modulo TDMA_FRAME_DIBITS. Both facts are free to
    check and neither is something independent coincidences will satisfy: two
    random hits agree on the sync name with probability 1/10 and on the grid
    phase with probability 1/144.

    So this returns the size of the largest (sync name, grid phase) group, and
    the honesty gate counts THAT rather than len(found)."""
    groups = {}
    for start, name, _errs in found:
        key = (name, start % TDMA_FRAME_DIBITS)
        groups[key] = groups.get(key, 0) + 1
    return max(groups.values()) if groups else 0


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


def _evidence_phrase(b) -> str:
    """What actually stands behind this message, in the report's own words.

    Every emitted message carries one. A sweep report that says "DMR detected"
    without saying WHY is the thing this module exists not to produce, and the
    grades below are not interchangeable: a verified BPTC product code is
    arithmetic that noise does not pass, whereas a bare sync run says only that
    something is transmitting DMR framing."""
    if b.get("rs_ok"):
        return ("BPTC(196,96) product code verified and RS(12,9) outer checksum "
                "verified against the data-type mask")
    if b.get("bptc_ok"):
        if b.get("rs_checked"):
            return ("BPTC(196,96) product code verified; RS(12,9) outer checksum "
                    "did NOT verify — fields are from the BPTC alone")
        return "BPTC(196,96) product code verified"
    if b.get("emb"):
        return ("EMB (16,7,6) validated and corroborated on the colour code "
                "(either against a BPTC-verified block or by at least %d "
                "agreeing error-free EMBs)" % EMB_MIN_AGREEING_UNANCHORED)
    return "48-bit sync pattern only — no FEC block validated"


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
        # A link control whose addresses are impossible AND whose RS(12,9) did
        # not verify is the shape noise takes when the BPTC decoder has flattened
        # a block. With the RS verified we believe the fields even if they look
        # odd — a real system's addressing is not ours to second-guess.
        if not lc["plausible"] and not lc["rs_ok"]:
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
            "evidence": _evidence_phrase(b), "rs_checked": lc["rs_checked"],
            "rs_ok": lc["rs_ok"],
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
            "evidence": _evidence_phrase(b),
            "text": "DMR control burst (CSBK opcode 0x%02X, colour code %s) — "
                    "signalling, not voice; opcode payload not decoded"
                    % (b["csbk"]["csbko"], b["colour_code"]),
        })

    # Voice traffic with no link control captured is still a real finding: the
    # colour code and the encryption state are usually all a sweep needs.
    #
    # This is also the WEAKEST message this module emits and the one every
    # measured fabrication came out of, so it states its own evidence grade. When
    # nothing but the sync run stands behind it the colour code and the
    # encryption state are reported as unknown rather than guessed — a burst with
    # no validated EMB has no colour code to give, and saying so is the finding.
    if not any(m["type"] == "voice_call" for m in msgs):
        voice = [b for b in bursts if b["kind"] in ("voice", "voice_emb")]
        if voice:
            ccs = sorted({b["colour_code"] for b in voice if b["colour_code"] is not None})
            cc = ccs[0] if ccs else None
            encrypted = enc_by_cc.get(cc)
            evidence = evidence_by_cc.get(cc)
            corroborated = any(b.get("emb") for b in voice)
            msgs.append({
                "type": "voice_activity", "protocol": "DMR", "colour_code": cc,
                "slot": voice[0]["slot"], "slot_source": voice[0]["slot_source"],
                "talkgroup": None, "source": None, "encrypted": encrypted,
                "encryption_evidence": evidence, "bursts": len(voice),
                "evidence": _evidence_phrase(
                    next((b for b in voice if b.get("emb")), voice[0])),
                "text": "DMR voice traffic — %d burst(s), colour code %s — %s; no "
                        "link control header in this capture, so talkgroup and "
                        "radio ID are unknown%s" % (
                            len(voice), cc if cc is not None else "unknown",
                            _encryption_summary(encrypted, evidence),
                            "" if corroborated else
                            ". EVIDENCE IS THE SYNC PATTERN ALONE — DMR framing "
                            "is present but no FEC block validated, so treat this "
                            "as 'a DMR-shaped signal is here', not as a decode"),
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
            "evidence": _evidence_phrase(b), "payload_hex": b["payload_hex"],
            "text": "DMR privacy header (colour code %s) — the call is ENCRYPTED. "
                    "Raw header %s; algorithm/key fields not parsed."
                    % (b["colour_code"], b["payload_hex"]),
        })
    return msgs


# ---------------------------------------------------------------- top level
def _empty_result(note: str, quality: float = 0.0) -> dict:
    return {"protocol": "DMR", "messages": [], "bursts": [], "note": note,
            "quality": float(quality),
            "stats": {"syncs": 0, "bursts": 0, "coherent_syncs": 0,
                      "voice_bursts": 0, "data_bursts": 0, "slot_type_ok": 0,
                      "bptc_ok": 0, "bptc_failed": 0, "emb_ok": 0,
                      "rs_checked": 0, "rs_ok": 0, "colour_codes": [],
                      "proven_colour_codes": [], "colour_code_consistent": True,
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
    proven_ccs = sorted({b["colour_code"] for b in bursts
                         if b.get("bptc_ok") and b["colour_code"] is not None})
    stats = {
        "syncs": len(found), "bursts": len(bursts),
        "coherent_syncs": _coherent_syncs(found),
        "voice_bursts": sum(1 for b in bursts if b["kind"] in ("voice", "voice_emb")),
        "data_bursts": sum(1 for b in bursts if b["kind"] == "data"),
        "slot_type_ok": sum(1 for b in bursts if b["data_type"] is not None),
        "bptc_ok": sum(1 for b in bursts if b["bptc_ok"]),
        "bptc_failed": sum(1 for b in bursts if b["bptc_ok"] is False),
        "emb_ok": sum(1 for b in bursts if b["emb"]),
        "rs_checked": sum(1 for b in bursts if b.get("rs_checked")),
        "rs_ok": sum(1 for b in bursts if b.get("rs_ok")),
        "colour_codes": sorted({b["colour_code"] for b in bursts
                                if b["colour_code"] is not None}),
        "proven_colour_codes": proven_ccs,
        # One transmitter has one colour code. Several is either two systems on
        # the channel or noise, and the note says so rather than picking one.
        "colour_code_consistent": len(proven_ccs) <= 1,
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
    # report. Demand either an independent FEC block that validated (a clean
    # BPTC, or EMBs that agree with each other and with a BPTC-proven colour
    # code), or MIN_SYNCS_WITHOUT_FEC COHERENT syncs — same sync word, same TDMA
    # grid phase, i.e. plausibly one transmitter rather than three coincidences.
    # A Golay slot type on its own is NOT enough and never counts here: 33 % of
    # random 20-bit words decode as one, and a slot type whose own BPTC failed is
    # evidence AGAINST the burst, not for it.
    fec_ok = stats["bptc_ok"] or stats["emb_ok"]
    if not fec_ok and stats["coherent_syncs"] < MIN_SYNCS_WITHOUT_FEC:
        return _empty_result(
            "%d DMR sync match(es) (%d coherent) but no FEC block validated — "
            "not reported" % (stats["syncs"], stats["coherent_syncs"]), quality)

    messages = _aggregate(bursts)
    if not messages:
        return _empty_result(
            "DMR framing found but nothing survived the corroboration checks — "
            "not reported", quality)
    note = "DMR: %d burst(s), colour code(s) %s" % (
        len(bursts), ", ".join(str(c) for c in stats["colour_codes"]) or "unknown")
    if stats["rs_ok"]:
        note += " — %d link control word(s) RS(12,9)-verified" % stats["rs_ok"]
    elif stats["rs_checked"]:
        note += (" — RS(12,9) did NOT verify on %d link control word(s); fields "
                 "rest on the BPTC alone" % stats["rs_checked"])
    if not stats["colour_code_consistent"]:
        note += (" — colour codes %s disagree across the capture, so this is two "
                 "systems or a marginal decode"
                 % ", ".join(str(c) for c in stats["proven_colour_codes"]))
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
