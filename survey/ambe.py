"""AMBE / IMBE vocoder frames -> PCM — the final stage of DMR and P25 Phase 1 voice.

Everything upstream of this module (FSK/C4FM demod, burst sync, transport
de-interleave) hands us a bag of BITS. This module turns those bits into audio:
FEC-correct the vocoder frame, unpack the Multi-Band Excitation parameters, and
resynthesize speech as harmonic sinusoids (voiced bands) plus band-shaped noise
(unvoiced bands). 8 kHz mono, numpy float32 in -1..1.

Two frame flavours, kept strictly separate because they are DIFFERENT codecs that
merely share the MBE model:
  - P25 Phase 1 : IMBE 7200x4400 — 144 coded bits/20 ms -> 88 information bits.
  - DMR         : AMBE+2 3600x2450 — 72 coded bits/20 ms -> 49 information bits.

Patent status was assessed by the operator (IMBE late-1980s, AMBE/AMBE+2 early
2000s; both lapsed by 2026) and this work explicitly authorised. Not re-argued here.

==============================================================================
HONESTY — READ THIS BEFORE YOU TRUST ANY AUDIO OUT OF THIS MODULE
==============================================================================
NOTHING here has been tested against real off-air audio. Not one frame. Every
claim below is backed only by synthetic vectors in tests/test_ambe.py.

WHAT IS IMPLEMENTED AND SYNTHETICALLY VERIFIED
  * Golay(23,12), extended Golay(24,12), Hamming(15,11): encode + syndrome
    decode. Verified structurally, not just by round-trip — the tests enumerate
    all 4096 / 2048 codewords and assert minimum distance 7 / 8 / 3, and assert
    the Golay syndrome table is a perfect bijection over all 2048 error patterns
    of weight <= 3. That proves these really are the named codes.
  * (FIXED) The Hamming(15,11,3) GENERATOR MATRIX is now TIA-102.BAAA-A
    Table 5-4's, not a CRC on g(x) = x^4+x+1. Those two differ: they generate
    the same code with the rows PERMUTED, so the old one had correct minimum
    distance, a correct perfect-code syndrome table and a perfect round-trip
    through our own encoder — and mapped u_4/u_5/u_6 onto different codewords
    than a real radio. Exactly the bug class that hid an invented Golay
    generator in survey/p25.py. The eleven parity rows are now pinned per-row
    in the tests, because distance tests provably cannot see this.
  * The prioritized-bit scrambler (Lehmer LCG x <- 173x + 13849 mod 2^16, MSB
    tapped, seeded from u_0) used by both codecs to whiten the FEC-protected
    vectors. Round-trips through pack_*/unpack_*.
  * Frame geometry: IMBE 144 bits -> 8 vectors (4x Golay23, 3x Hamming15, 1 raw)
    -> 88 bits. DMR 72 bits -> 4 vectors (Golay24, Golay23, 2 raw) -> 49 bits.
  * IMBE pitch decode: b0 -> w0 = 4*pi/(b0+39.5) rad/sample, f0 = 16000/(b0+39.5)
    Hz (405 Hz down to 64.9 Hz over b0 = 0..207), harmonic count
    L = floor(0.9254 * floor(pi/w0 + 0.25)) clamped to 9..56, band count
    K = min(12, floor((L+2)/3)), and the K voiced/unvoiced band decisions
    expanded to per-harmonic flags (3 harmonics per band).
  * MBE synthesis: voiced harmonics with amplitude cross-fade and quadratic
    phase interpolation across the 20 ms frame boundary (no clicks, phase is
    continuous), unvoiced bands as spectrally-shaped noise overlap-added with a
    power-complementary sine window. Verified by asserting the FFT peak of the
    synthesized audio lands on the requested fundamental.

  * The IMBE SPECTRAL AMPLITUDE DEQUANTIZER — the stage that turns the coded
    amplitude bits into a spectral envelope, and therefore the stage that makes
    the difference between a buzz and a voice. Implemented from
    docs/IMBE_SPECTRAL_AMPLITUDES.md (written from TIA/EIA-102.BABA and
    cross-checked against mbelib): the 6-block split of the prediction residual,
    the DCT pair, the gain vector and its 6-point inverse transform, the
    higher-order coefficient mapping, the inter-frame log-amplitude prediction
    with FREQUENCY WARPING when L changes, the frame-mean correction, and the
    clause-8 envelope enhancement. Everything computable is COMPUTED — block
    lengths, the m->(block,coefficient) map and all 240 gain-vector step sizes
    are generated from a 3-line rule rather than tabulated, which keeps them off
    the transcription-risk surface entirely.
  * The three genuinely irreducible tables (the 64-level gain quantizer and the
    two bit-allocation tables) live in survey/imbe_tables.py, taken from mbelib
    under its ISC licence. See the ATTRIBUTION block in that file: shipping this
    product requires carrying mbelib's copyright and permission notice, and that
    obligation is discharged in THIRD_PARTY_NOTICES.md at the repo root.
  * Both bit-allocation tables now have PER-ENTRY verification. They were parsed
    deterministically out of a hash-pinned copy of mbelib's
    imbe7200x4400_const.h (sha256 62982451...24737, confirmed byte-identical
    from two independent origins), rather than read back by a summarising fetch.
    All 1272 higher-order integers and all 240 gain bit counts are checked
    against the actual file, row indexing is cross-checked against the file's
    own `// L=NN` labels, and both tables are locked by fingerprints the test
    suite recomputes on every run — so an intra-row swap, which a sum checksum
    physically cannot see, now fails the suite.

WHAT IS **NOT** IMPLEMENTED — THE GAPS THAT REMAIN
  * (CLOSED) The L = 42, 43, 44 higher-order allocation gap is gone. Those three
    rows were recovered from mbelib's hoba[] and each satisfies the bit-budget
    checksum (14 + 47 == 61 == 73 - K). All 48 legal harmonic counts decode.
  * Whether MBELIB ITSELF is faithful to TIA-102.BABA Annex G is corroborated
    independently only for L = 9, 10, 11, 12, 13, 15, 16, 17. For L = 18..56 the
    higher-order allocation is provably what mbelib says, but mbelib is its own
    authority there. That is not paranoia: mbelib demonstrably contains at least
    one real error (the L=23 gain step typo). Buying the standard would close it.
  * (CLOSED) The 88-bit FIELD ORDER is no longer this module's own convention.
    It used to read b0 as info[0:8], then b1, then b2, then the amplitude
    quantizers — plausible, self-consistent, and WRONG. TIA-102.BABA clause 7.1
    scatters b_0..b_{L+2} across u_0..u_7 by priority: only the top SIX bits of
    the pitch index lead the frame, its bottom two sit at info[85..86], the gain
    index is split 3 + 2 + 1 across the frame, and every amplitude quantizer is
    interleaved by bit significance. Under the old layout a perfectly
    FEC-cleaned off-air frame decoded to the wrong pitch — hence the wrong L,
    hence the wrong everything — on essentially every frame. The scan is now
    DERIVED from the bit allocation (no new tables) and reproduces mbelib's
    tabulated bo[48][79][2] map for all 48 x 79 = 3792 entries. See the block
    comment at IMBE_PITCH_BITS for the full provenance.
  * Adaptive smoothing (clause 9) and the frame-repeat / erasure concealment
    behaviour behind the reserved pitch codes.
  * The AMBE+2 2450 parameter quantizer. Its pitch/gain/magnitude tables differ
    from IMBE's and I do not have them reliably either. So the DMR path decodes
    FEC and hands back 49 verified information bits and NO parameters. That is
    still useful in the field ("do these bursts FEC-clean as real AMBE voice, or
    is this noise?") but it is not audio.
  * The AMBE+2 (DMR) field order. Everything above about IMBE clause 7.1 is
    IMBE-only; AMBE+2 3600x2450 has its own prioritization that is not
    implemented and not sourced. The DMR path still returns FEC-verified bits
    and nothing else.

WHAT THE CALLER IS NOW RESPONSIBLE FOR
  * Transport-layer de-interleaving is NOT done here — but survey/p25.py now
    does it. It de-interleaves per TIA-102.BAAA-A Table 5-1 and hands voice
    frames over in code-word order c_0..c_7, MSB-first, which is exactly the
    144-bit layout unpack_imbe_frame() expects. Any other caller must do the
    same; passing raw on-air bit order will fail every Golay and Hamming decode.

CONSEQUENCE, STATED PLAINLY: this module now decodes the full IMBE parameter
set — pitch, voicing AND the spectral envelope — for ALL 48 legal harmonic
counts, so a P25 Phase 1 frame that FEC-cleans comes out as speech-shaped audio
with the transmitted formant structure rather than as a buzz. That is a real
change: `intelligible` can now be True in a report, where before it never could.

Two of the three things that would have broken real traffic are now closed: the
Hamming(15,11) mapping is the standard's, and the 88-bit FIELD ORDER is the
standard's clause-7.1 priority scan rather than a local guess. Transport
de-interleaving lives in the caller and survey/p25.py implements it.

But read this next part before anyone puts it in front of an operator, because
it is still the whole risk: NO OFF-AIR FRAME HAS EVER BEEN DECODED BY THIS CODE.
Every claim is backed by synthetic vectors, by published tables parsed
deterministically out of hash-pinned artifacts, and by agreement with an
independent implementation — not by a recording of a radio. A chain of stages
that are each individually right can still be wired together wrongly, and only
real traffic finds that. Until someone decodes a real capture and listens to it,
"intelligible=True" means "the dequantizer ran and believes it produced an
envelope", NOT "a human heard words". Say that to the operator.

No legal harmonic count returns magnitudes=None any more. The placeholder
envelope (envelope="synthetic") therefore has no frame left to serve on the IMBE
path and is now effectively dead code kept for one reason: it is the documented
fallback if a future gap is ever reinstated in survey/imbe_tables.py. It is
never enabled implicitly, never overrides a real decode, and every frame using
it is labelled intelligible=False.

Failed FEC returns silence plus fec_ok=False. This module never invents audio to
cover a bad frame.
"""

import math

import numpy as np

from survey import imbe_tables as _T

SAMPLE_RATE = 8000            # AMBE and IMBE are 8 kHz vocoders, always
FRAME_MS = 20.0
FRAME_SAMPLES = 160           # 20 ms at 8 kHz
MAX_HARMONICS = 56            # IMBE L range is 9..56

# ---- FEC generator polynomials ----
# Golay(23,12): g(x) = x^11+x^10+x^6+x^5+x^4+x^2+1. The reciprocal 0xAE3 generates
# an equivalent code with a DIFFERENT codeword mapping — so if off-air P25/DMR
# frames ever fail to FEC-clean here, this constant is the first thing to flip.
GOLAY23_POLY = 0xC75

# ---- Hamming(15,11,3): TIA-102.BAAA-A section 5.8, Table 5-4 ----------------
# The 4 parity bits generated by each of the 11 information bits, MSB row first.
#
# THIS USED TO BE A CRC ON g(x) = x^4+x+1 (0x13) AND THAT WAS WRONG. Section 5.8
# does say the code is "derived from the code that is generated by the
# polynomial g(x) = x^4 + x + 1", but it then says how: "The (15,11,3) standard
# Hamming code is derived from the cyclic version by arranging the rows of the
# four parity check columns in ascending order." The systematic CRC on that same
# polynomial produces the rows in a DIFFERENT order —
#     1001 1101 1111 1110 0111 1010 0101 1011 1100 0110 0011
# which is a permutation of the same eleven nibbles. Same code, same minimum
# distance 3, same perfect-code syndrome structure, PERFECT ROUND-TRIP through
# our own encoder — and a different information-to-codeword mapping than a real
# radio, so every u_4/u_5/u_6 would have come out scrambled off-air. Identical
# failure mode to the invented Golay generator that survived in survey/p25.py.
#
# (The set of eleven rows is forced: a systematic (15,11) code has d=3 iff its
# parity rows are distinct and all of weight >= 2, and there are exactly eleven
# such nibbles. So ONLY the order was ever in question, and only the published
# table settles it. That is precisely why weight/distance tests cannot see this
# bug — see tests/test_ambe.py::TestFecStructure.)
HAMMING15_PARITY_ROWS = (0b1111, 0b1110, 0b1101, 0b1100, 0b1011, 0b1010,
                         0b1001, 0b0111, 0b0110, 0b0101, 0b0011)

# ---- IMBE 7200x4400 (P25 Phase 1) frame geometry ----
# 88 information bits split into 8 prioritized vectors u_0..u_7; the four most
# important get Golay(23,12), the next three Hamming(15,11), the least important
# 7 bits ride bare. 4*23 + 3*15 + 7 = 144 bits = 7200 bps at 50 frames/s.
IMBE_FRAME_BITS = 144
IMBE_INFO_BITS = 88
IMBE_INFO_WIDTHS = (12, 12, 12, 12, 11, 11, 11, 7)
IMBE_CODED_WIDTHS = (23, 23, 23, 23, 15, 15, 15, 7)
IMBE_PN_BITS = 114            # vectors 1..6 coded: 3*23 + 3*15
IMBE_ERR_LIMIT = 6            # heuristic mute gate — see _fec_verdict()

# ---- AMBE+2 3600x2450 (DMR) frame geometry ----
# 49 information bits in 4 vectors: u_0 gets extended Golay(24,12) (it seeds the
# scrambler, so a silent mis-correct there wrecks the whole frame — the extra
# parity bit buys 4-error DETECTION), u_1 gets Golay(23,12), u_2/u_3 ride bare.
# 24 + 23 + 11 + 14 = 72 bits = 3600 bps at 50 frames/s.
AMBE_FRAME_BITS = 72
AMBE_INFO_BITS = 49
AMBE_INFO_WIDTHS = (12, 12, 11, 14)
AMBE_CODED_WIDTHS = (24, 23, 11, 14)
AMBE_PN_BITS = 23             # vector 1 coded
AMBE_ERR_LIMIT = 4            # heuristic mute gate — see _fec_verdict()

# ==============================================================================
# IMBE information-bit field layout — TIA-102.BABA section 7.1, "Bit
# Prioritization". THIS IS THE STANDARD'S ORDER, NOT A LOCAL CONVENTION.
# ==============================================================================
# The 88 information bits are NOT the quantizer values laid end to end. The
# encoder scatters b_0..b_{L+2} across the eight vectors u_0..u_7 by PRIORITY,
# so that the bits whose corruption hurts most land in the best-protected
# vectors. Concatenating the vectors MSB-first (which is what unpack_imbe_frame
# returns, and what mbelib calls imbe_d[88]) gives:
#
#   info[0:6]    u_0 bits 11..6  = b_0 bits 7..2   (pitch, TOP SIX BITS ONLY)
#   info[6:9]    u_0 bits  5..3  = b_2 bits 5..3   (gain, top three bits)
#   info[9:85]   u_0 bits  2..0, then all of u_1, u_2, u_3, u_4, u_5, u_6 and
#                u_7 bits 6..3 — a 79-position stream (counting from info[6])
#                carrying b_1, the rest of b_2, and every spectral-amplitude
#                quantizer, interleaved by the Fig. 22 priority scan below
#   info[85]     u_7 bit 2       = b_0 bit 1
#   info[86]     u_7 bit 1       = b_0 bit 0
#   info[87]     u_7 bit 0       = b_{L+2}         (not used by the decoder)
#
# So the pitch index is SPLIT: six bits at the very front and two bits 85 places
# later. An implementation that reads b_0 as info[0:8] gets the top six bits
# right and then two bits of the gain index where the pitch's LSBs should be —
# a wrong pitch, hence a wrong L, hence a wrong everything, on every frame that
# FEC-cleans perfectly. This module did exactly that until it was fixed.
#
# PROVENANCE — verified two ways, neither of them a summarising fetch:
#   1. TIA-102.BABA section 7.1's own text, downloaded as raw bytes and read
#      directly. It states the u_0 11..6 <- b_0 7..2 assignment, the u_0 5..3
#      <- b_2 5..3 assignment, the Fig. 22 scan, the b_1-then-b_2-bits-2-and-1
#      insertion beginning at u_4 bit 10, and the closing four bits of u_7 as
#      b_2 bit 0, b_0 bit 1, b_0 bit 0, b_{L+2}.
#   2. mbelib's tabulated bo[48][79][2] priority map, parsed deterministically
#      out of imbe7200x4400_const.h — the same hash-pinned artifact
#      survey/imbe_tables.py already sources (sha256 62982451...24737, present
#      independently in the Software Heritage archive). imbe_priority_map()
#      reproduces all 48 x 79 = 3792 of its entries EXACTLY, and mbelib's own
#      b_0 assembly reads imbe_d[0..5] + imbe_d[85..86], matching (1).
#
# Note what is NOT here: a table. The scan is DERIVED from the bit allocation
# this module already has and already verifies per-entry, so the field order
# adds no new transcribed numbers and no new licence obligation — mbelib's
# bo[] served only as the check, and a check is not the data.
IMBE_PITCH_BITS = 8           # b_0 is 8 bits WIDE; it is not 8 CONTIGUOUS bits
IMBE_PITCH_HIGH_BITS = 6      # ...of which the top 6 lead the frame
IMBE_B0_LOW_INDICES = (85, 86)   # ...and the bottom 2 sit here, bit 1 then bit 0
IMBE_SCAN_START = 6           # first prioritized position (u_0 bit 5)
IMBE_SCAN_STOP = 85           # one past the last (u_7 bit 3)
IMBE_TRAILING_INDEX = 87      # b_{L+2}; carried, never decoded
IMBE_MAX_QUANT_BITS = 11      # Fig. 22 has 11 rows, MSB row 10 down to row 0
IMBE_CONTROL_PREFIX = 3       # gain bits 5..3 lead the prioritized stream
IMBE_SCAN_BREAK = 39          # amplitude scan bits emitted before b_1 appears
IMBE_GAIN_BITS = 6
IMBE_PITCH_RESERVED = 208     # b0 >= this is reserved (silence / frame repeat)
# TIA-102.BABA initialises a new stream's amplitude history to M_l(-1) = 1.0
# (log2 = 0) with L(-1) = 30, so the first frame of a call predicts against a
# flat unity spectrum rather than against an empty one.
IMBE_INIT_PREV_L = 30


# ------------------------------------------------------------------ GF(2) helpers
def _popcount(x: int) -> int:
    return bin(x).count("1")


def _gf2_remainder(value: int, poly: int, poly_deg: int) -> int:
    """Remainder of `value` divided by `poly` (degree poly_deg) over GF(2).

    Deliberately duplicated from pocsag.py rather than imported: a vocoder has no
    business depending on the pager decoder, and this is six lines."""
    v = value
    for i in range(v.bit_length() - 1, poly_deg - 1, -1):
        if v & (1 << i):
            v ^= poly << (i - poly_deg)
    return v


def _build_syndrome_table(n: int, k: int, poly: int, max_weight: int) -> dict:
    """syndrome -> error pattern, for every error pattern up to max_weight bits.

    For the CYCLIC codes only — Golay(23,12), whose 2048 entries enumerate at
    import in microseconds. Hamming(15,11) no longer comes through here: it is
    defined by TIA-102.BAAA-A Table 5-4's generator MATRIX rather than by a
    polynomial, so its syndromes are built from that matrix in
    _build_hamming15_syndromes().

    Golay(23,12) is PERFECT, so this enumeration is a bijection onto the
    syndrome space; the tests assert exactly that, which is what proves the
    generator polynomial really produces the named code. Note the limit of that
    argument, learned the hard way from the Hamming bug directly below: being
    the named code does not make it the same MAPPING as the transmitter's."""
    deg = n - k
    table = {0: 0}
    positions = list(range(n))
    if max_weight >= 1:
        for a in positions:
            e = 1 << a
            table.setdefault(_gf2_remainder(e, poly, deg), e)
    if max_weight >= 2:
        for a in positions:
            for b in range(a + 1, n):
                e = (1 << a) | (1 << b)
                table.setdefault(_gf2_remainder(e, poly, deg), e)
    if max_weight >= 3:
        for a in positions:
            for b in range(a + 1, n):
                for c in range(b + 1, n):
                    e = (1 << a) | (1 << b) | (1 << c)
                    table.setdefault(_gf2_remainder(e, poly, deg), e)
    return table


_GOLAY23_SYN = _build_syndrome_table(23, 12, GOLAY23_POLY, 3)


def _hamming15_parity(data11: int) -> int:
    """4 parity bits for 11 information bits, per TIA-102.BAAA-A Table 5-4.

    Row i of the table applies to the i'th information bit counting from the
    MSB — the same MSB-first convention bits_to_int() uses everywhere else."""
    par = 0
    for i in range(11):
        if (data11 >> (10 - i)) & 1:
            par ^= HAMMING15_PARITY_ROWS[i]
    return par


def _hamming15_syndrome(cw15: int) -> int:
    return _hamming15_parity((cw15 >> 4) & 0x7FF) ^ (cw15 & 0xF)


def _build_hamming15_syndromes() -> dict:
    """syndrome -> single-bit error pattern, enumerated over the 15 columns.

    No longer a polynomial remainder: the code is now defined by the standard's
    generator matrix, so its syndrome table has to be built from that matrix.
    Hamming(15,11) is perfect, so this must come out a bijection onto all 16
    syndromes — which tests/test_ambe.py asserts, and which would fail if
    Table 5-4 had been transcribed with a repeated or a weight-1 row."""
    table = {0: 0}
    for b in range(15):
        e = 1 << b
        table.setdefault(_hamming15_syndrome(e), e)
    return table


_HAMMING15_SYN = _build_hamming15_syndromes()


# ------------------------------------------------------------------------- FEC
def golay23_encode(data12: int) -> int:
    """12 data bits -> systematic 23-bit Golay codeword (data in the high bits)."""
    reg = (data12 & 0xFFF) << 11
    return reg | _gf2_remainder(reg, GOLAY23_POLY, 11)


def golay23_decode(cw23: int):
    """(data12, n_corrected, ok). Golay(23,12) is a PERFECT code: every one of the
    2048 syndromes maps to exactly one error pattern of weight <= 3, so decoding
    ALWAYS succeeds and ok is always True. With 4+ errors it silently mis-corrects
    — that is a property of the code, not a bug here, and it is exactly why the
    frame-level gate in _fec_verdict() watches the CORRECTION COUNT rather than
    trusting a per-vector success flag."""
    cw = cw23 & 0x7FFFFF
    e = _GOLAY23_SYN[_gf2_remainder(cw, GOLAY23_POLY, 11)]
    fixed = cw ^ e
    return (fixed >> 11) & 0xFFF, _popcount(e), True


def golay24_encode(data12: int) -> int:
    """Extended Golay(24,12): the (23,12) codeword plus an overall even-parity bit."""
    cw23 = golay23_encode(data12)
    return (cw23 << 1) | (_popcount(cw23) & 1)


def golay24_decode(cw24: int):
    """(data12, n_corrected, ok). Minimum distance 8 -> corrects 3, DETECTS 4.

    Correct the 23-bit body first, then reconcile with the received overall
    parity. If the body needed 3 flips but the parity still disagrees, the true
    error weight is 4 (a weight-4 error in the body always aliases to a weight-3
    coset leader, because the minimum nonzero Golay weight is 7 = 4+3) — that is
    the detect-4 case, and we refuse it instead of emitting a wrong dataword."""
    cw = cw24 & 0xFFFFFF
    parity = _popcount(cw) & 1
    data, nerr, _ = golay23_decode(cw >> 1)
    if (nerr & 1) != parity:
        if nerr == 3:
            return data, 4, False          # detected, uncorrectable
        nerr += 1                          # the parity bit itself was the extra flip
    return data, nerr, True


def hamming15_encode(data11: int) -> int:
    """11 data bits -> systematic 15-bit Hamming codeword (data in the high bits).

    Uses TIA-102.BAAA-A Table 5-4's generator matrix, NOT a CRC on g(x). See the
    HAMMING15_PARITY_ROWS comment: the CRC gives an equivalent code with a
    different mapping, which round-trips perfectly here and decodes to garbage
    on a real radio."""
    data11 &= 0x7FF
    return (data11 << 4) | _hamming15_parity(data11)


def hamming15_decode(cw15: int):
    """(data11, n_corrected, ok). Also a perfect code (d=3, corrects 1) — every
    nonzero syndrome is a single-bit pattern, so ok is always True and a double
    error mis-corrects into a third wrong bit. Same reasoning as golay23_decode:
    the honest gate is the frame-level correction count."""
    cw = cw15 & 0x7FFF
    e = _HAMMING15_SYN[_hamming15_syndrome(cw)]
    fixed = cw ^ e
    return (fixed >> 4) & 0x7FF, _popcount(e), True


# ------------------------------------------------------------- bit-array plumbing
def bits_to_int(bits) -> int:
    """MSB-first bit sequence -> int. bits[0] is the most significant."""
    v = 0
    for b in bits:
        v = (v << 1) | (int(b) & 1)
    return v


def int_to_bits(value: int, n: int) -> np.ndarray:
    """int -> MSB-first uint8 bit array of length n."""
    return np.array([(int(value) >> k) & 1 for k in range(n - 1, -1, -1)],
                    dtype=np.uint8)


def _as_bits(bits, n: int, what: str) -> np.ndarray:
    arr = np.asarray(bits, dtype=np.uint8).ravel() & 1
    if arr.size != n:
        raise ValueError(f"{what} needs exactly {n} bits, got {arr.size}")
    return arr


def _spans(widths):
    """(start, stop) index pairs for consecutive fields of the given widths."""
    out, pos = [], 0
    for w in widths:
        out.append((pos, pos + w))
        pos += w
    return tuple(out)


IMBE_VECTOR_SPANS = _spans(IMBE_CODED_WIDTHS)   # where u_0..u_7 sit in the 144 bits
AMBE_VECTOR_SPANS = _spans(AMBE_CODED_WIDTHS)   # where u_0..u_3 sit in the 72 bits


# --------------------------------------------------------- prioritized-bit scramble
def pn_bits(seed12: int, n: int) -> np.ndarray:
    """The IMBE/AMBE prioritized-bit scrambler: a Lehmer LCG seeded from u_0.

    x0 = 16 * u_0, then x <- (173*x + 13849) mod 2^16, tapping the MSB of each
    iterate. Its purpose is to spread the damage: because the seed is u_0 — the
    ONE vector protected before anything else is touched — a frame whose u_0 is
    wrong descrambles to noise across every other vector at once, which makes a
    bad frame obviously bad instead of subtly wrong."""
    x = (16 * (int(seed12) & 0xFFF)) & 0xFFFF
    out = np.empty(int(n), dtype=np.uint8)
    for i in range(int(n)):
        x = (173 * x + 13849) & 0xFFFF
        out[i] = (x >> 15) & 1
    return out


# ---------------------------------------------------------------- frame unpacking
def _fec_verdict(errors: int, limit: int, detected_bad: bool):
    """Frame-level FEC gate. Perfect codes never report failure per-vector, so the
    only honest signal available is HOW MUCH correction the frame needed: a real
    frame off a usable channel needs a few bits at most, while random noise fed
    into four Golay decoders reliably burns most of their 3-bit budget. Above the
    limit we call the frame bad and the caller emits silence."""
    if detected_bad:
        return False, "FEC detected an uncorrectable vector"
    if errors > limit:
        return False, f"FEC correction count {errors} exceeds limit {limit}"
    return True, ""


def unpack_imbe_frame(bits144, err_limit: int = IMBE_ERR_LIMIT) -> dict:
    """144 coded IMBE bits -> the 88 information bits + FEC telemetry.

    Order of operations matters: u_0 must be Golay-decoded BEFORE anything else,
    because it seeds the scrambler that whitens vectors 1..6. Returns
    {info_bits, errors, vector_errors, fec_ok, note}. info_bits is returned even
    when fec_ok is False — the caller may want the payload for analysis, it just
    must not turn it into audio."""
    bits = _as_bits(bits144, IMBE_FRAME_BITS, "IMBE coded frame")
    vec = [bits[a:b] for a, b in IMBE_VECTOR_SPANS]

    u0, e0, _ = golay23_decode(bits_to_int(vec[0]))
    pn = pn_bits(u0, IMBE_PN_BITS)

    # de-scramble vectors 1..6 as one contiguous run, in vector order, MSB-first
    scrambled = np.concatenate(vec[1:7])
    clear = scrambled ^ pn
    parts, pos = [], 0
    for w in IMBE_CODED_WIDTHS[1:7]:
        parts.append(clear[pos:pos + w])
        pos += w

    info = [int_to_bits(u0, 12)]
    verr = [e0]
    for i, part in enumerate(parts):
        if i < 3:                                   # vectors 1..3: Golay(23,12)
            d, e, _ = golay23_decode(bits_to_int(part))
            info.append(int_to_bits(d, 12))
        else:                                       # vectors 4..6: Hamming(15,11)
            d, e, _ = hamming15_decode(bits_to_int(part))
            info.append(int_to_bits(d, 11))
        verr.append(e)
    info.append(vec[7].copy())                      # u_7 rides bare, 7 bits
    verr.append(0)

    errors = int(sum(verr))
    ok, note = _fec_verdict(errors, err_limit, False)
    return {"info_bits": np.concatenate(info).astype(np.uint8), "errors": errors,
            "vector_errors": verr, "fec_ok": ok, "note": note}


def pack_imbe_frame(bits88) -> np.ndarray:
    """88 information bits -> 144 coded IMBE bits. Inverse of unpack_imbe_frame;
    shares the same FEC and the same scrambler, so a wrong polynomial or a wrong
    bit order fails loudly in the round-trip tests rather than quietly."""
    bits = _as_bits(bits88, IMBE_INFO_BITS, "IMBE information frame")
    spans = _spans(IMBE_INFO_WIDTHS)
    u = [bits_to_int(bits[a:b]) for a, b in spans]

    coded = [int_to_bits(golay23_encode(u[0]), 23)]
    body = [int_to_bits(golay23_encode(u[i]), 23) for i in (1, 2, 3)]
    body += [int_to_bits(hamming15_encode(u[i]), 15) for i in (4, 5, 6)]
    scrambled = np.concatenate(body) ^ pn_bits(u[0], IMBE_PN_BITS)
    coded.append(scrambled)
    coded.append(bits[spans[7][0]:spans[7][1]].copy())
    return np.concatenate(coded).astype(np.uint8)


def unpack_ambe_frame(bits72, err_limit: int = AMBE_ERR_LIMIT) -> dict:
    """72 coded DMR AMBE+2 bits -> the 49 information bits + FEC telemetry.

    Same shape as unpack_imbe_frame, but u_0 carries an extended Golay(24,12) so a
    4-bit hit on the scrambler seed is DETECTED rather than silently mis-decoded."""
    bits = _as_bits(bits72, AMBE_FRAME_BITS, "AMBE coded frame")
    vec = [bits[a:b] for a, b in AMBE_VECTOR_SPANS]

    u0, e0, ok0 = golay24_decode(bits_to_int(vec[0]))
    clear = vec[1] ^ pn_bits(u0, AMBE_PN_BITS)
    u1, e1, _ = golay23_decode(bits_to_int(clear))

    info = np.concatenate([int_to_bits(u0, 12), int_to_bits(u1, 12),
                           vec[2].copy(), vec[3].copy()]).astype(np.uint8)
    verr = [e0, e1, 0, 0]
    errors = int(sum(verr))
    ok, note = _fec_verdict(errors, err_limit, not ok0)
    return {"info_bits": info, "errors": errors, "vector_errors": verr,
            "fec_ok": ok, "note": note}


def pack_ambe_frame(bits49) -> np.ndarray:
    """49 information bits -> 72 coded DMR AMBE+2 bits. Inverse of unpack_ambe_frame."""
    bits = _as_bits(bits49, AMBE_INFO_BITS, "AMBE information frame")
    spans = _spans(AMBE_INFO_WIDTHS)
    u = [bits_to_int(bits[a:b]) for a, b in spans]
    c0 = int_to_bits(golay24_encode(u[0]), 24)
    c1 = int_to_bits(golay23_encode(u[1]), 23) ^ pn_bits(u[0], AMBE_PN_BITS)
    return np.concatenate([c0, c1, bits[spans[2][0]:spans[2][1]],
                           bits[spans[3][0]:spans[3][1]]]).astype(np.uint8)


# ----------------------------------------------------------- IMBE parameter decode
def imbe_pitch(b0: int):
    """Pitch index -> (w0 rad/sample, f0 Hz, L harmonics).

    w0 = 4*pi/(b0+39.5) puts b0=0 at 405.1 Hz and b0=207 at 64.9 Hz, which is the
    IMBE pitch range. L = floor(0.9254 * floor(pi/w0 + 0.25)) is the spec's rule
    for how many harmonics fit under the 0.9254*pi (~3.7 kHz) speech band; it
    lands on 9 at the top of the pitch range and 56 at the bottom, matching the
    documented L limits, which is the check that the constants are in the right
    places."""
    b0 = int(b0)
    w0 = 4.0 * math.pi / (b0 + 39.5)
    f0_hz = SAMPLE_RATE * w0 / (2.0 * math.pi)
    L = int(math.floor(0.9254 * math.floor(math.pi / w0 + 0.25)))
    L = int(np.clip(L, 9, MAX_HARMONICS))
    return w0, f0_hz, L


def imbe_band_count(L: int) -> int:
    """K voiced/unvoiced bands for L harmonics: 3 harmonics per band, capped at 12
    (the V/UV field is at most 12 bits, so the top bands share a decision)."""
    return int(min(12, (int(L) + 2) // 3))


def _expand_voiced(band_flags, L: int) -> np.ndarray:
    """K band decisions -> per-harmonic voiced flags, index 1..L (index 0 unused,
    so the array indexes by harmonic number the way the literature does)."""
    K = len(band_flags)
    v = np.zeros(int(L) + 1, dtype=bool)
    for l in range(1, int(L) + 1):
        v[l] = bool(band_flags[min((l - 1) // 3, K - 1)])
    return v


# ==============================================================================
# IMBE SPECTRAL AMPLITUDES — the stage between parameters and intelligible speech
# ==============================================================================
# Implemented from docs/IMBE_SPECTRAL_AMPLITUDES.md, which was written from
# TIA/EIA-102.BABA (the P25 IMBE Vocoder Description) and cross-checked
# line-by-line against mbelib. Equation numbers below are the standard's.
#
# The split that matters, and it is enforced by file boundary:
#   DERIVED (here)  — block lengths, the DCT pair, the m->(block,coefficient)
#                     mapping, all 240 gain-vector step sizes, the prediction
#                     coefficient, the frequency warping, the enhancement.
#                     No transcribed numbers, so no transcription risk.
#   SOURCED (survey/imbe_tables.py) — the three things that genuinely cannot be
#                     computed: the 64-level gain quantizer and the two bit
#                     allocation tables. Taken from mbelib under ISC, with the
#                     attribution obligation spelled out in that file.
#
# Decode order (clause 6.4): gain vector -> 6-point IDCT -> block means; higher
# order coefficients -> per-block IDCT -> prediction residual T_l; undo the
# frequency-warped inter-frame prediction -> log2 amplitudes; exponentiate;
# enhance. The UNENHANCED log2 amplitudes are what feed the next frame.
# ------------------------------------------------------------------------------

IMBE_AMPLITUDE_BLOCKS = 6     # the residual is always split into 6 DCT blocks
IMBE_TRAILING_BITS = 1        # b_{L+2}, the final bit of the 88

# The decoded amplitudes M_l are referenced to a 16-bit PCM full scale: the gain
# quantizer tops out at log2 = 8.695827, i.e. a single harmonic of ~415, and a
# loud frame's harmonics reach a few thousand — which only makes sense against
# +/-32768. This module's contract is float in -1..1, so that is the conversion.
# It is a UNIT CONVERSION, not a tuning knob: the standard defines the amplitudes
# relatively and says nothing about absolute output level, so a decoded frame can
# still clip on the loudest gain indices. mbe_synthesize reports clipping rather
# than hiding it, and decode_p25_voice passes that count through per frame.
IMBE_OUTPUT_SCALE = 1.0 / 32768.0


def imbe_amplitude_blocks(L: int) -> tuple:
    """L prediction residuals -> the 6 DCT block lengths.

    J_i = floor(L/6), with the last R = L mod 6 blocks one longer. TIA eqs.
    (58)/(59) and (65)/(66) constrain the blocks to sum to L and to satisfy
    floor(L/6) <= J_i <= J_{i+1} <= ceil(L/6), which this uniquely satisfies.
    Verified exact against mbelib's tabulated ImbeJi for all 48 values of L, so
    the standard's Annex J table is redundant and is deliberately NOT copied.
    Sanity: L=34 -> (5,5,6,6,6,6), which is TIA Figure 17.

    Blocks with J_i = 1 have no higher-order coefficients at all."""
    L = int(L)
    if L < IMBE_AMPLITUDE_BLOCKS:
        raise ValueError(f"L={L} cannot be split into {IMBE_AMPLITUDE_BLOCKS} blocks")
    base, rem = divmod(L, IMBE_AMPLITUDE_BLOCKS)
    return tuple([base] * (IMBE_AMPLITUDE_BLOCKS - rem) + [base + 1] * rem)


def imbe_amplitude_bit_budget(L: int) -> dict:
    """How many amplitude bits exist and how many quantized values they buy.

    The 88 information bits are b0 (8, pitch) + b1 (K, V/UV) + b2 (6, gain) +
    b3..b_{L+1} (the amplitude quantizers) + b_{L+2} (1), so the amplitude field
    is 73 - K bits carrying L-1 quantized values: 5 gain-vector elements
    (G_2..G_6, from b3..b7) and L-6 higher-order DCT coefficients (b8..b_{L+1}).

    The 5 + (L-6) = L-1 split is itself a check on the whole structure — the
    6-block DCT produces exactly L coefficients, of which the first PRBA element
    is the frame gain already sent as b2."""
    L = int(L)
    K = imbe_band_count(L)
    bits = (IMBE_INFO_BITS - IMBE_PITCH_BITS - K - IMBE_GAIN_BITS
            - IMBE_TRAILING_BITS)
    return {"L": L, "K": K, "bits": int(bits), "quantizers": L - 1,
            "gain_quantizers": IMBE_AMPLITUDE_BLOCKS - 1,
            "higher_quantizers": L - IMBE_AMPLITUDE_BLOCKS,
            "blocks": imbe_amplitude_blocks(L),
            "bits_per_quantizer": bits / float(L - 1)}


# --------------------------------------------------------------- derived: the DCT
def imbe_dct_matrix(J: int) -> np.ndarray:
    """The J x J analysis matrix, rows k = 0..J-1: cos(pi*k*(j+0.5)/J) / J."""
    J = int(J)
    if J < 1:
        raise ValueError("J must be >= 1")
    k = np.arange(J, dtype=np.float64)[:, None]
    j = np.arange(J, dtype=np.float64)[None, :]
    return np.cos(math.pi * k * (j + 0.5) / J) / float(J)


def imbe_dct(x) -> np.ndarray:
    """Forward DCT, normalized so c[0] is exactly the MEAN of x.

    The 1/J on every coefficient (rather than the orthonormal 1/sqrt(J)) is what
    makes the zeroth coefficient the block average — which is the point, since
    those averages are what the gain vector carries."""
    x = np.asarray(x, dtype=np.float64).ravel()
    return imbe_dct_matrix(x.size) @ x


def imbe_idct(c) -> np.ndarray:
    """Inverse DCT — TIA eqs. (69)/(70) and (73)/(74), which are the same form:

        x_j = sum_k a(k) * c_k * cos(pi*(k-1)*(j-0.5)/J),  a(1)=1, a(k>=2)=2

    Note the non-standard normalization: no leading 1/N, and the factor 2 on
    every coefficient above the first. That falls out of the basis norms
    (||phi_0||^2 = J but ||phi_k||^2 = J/2), so it is derived, not tuned."""
    c = np.asarray(c, dtype=np.float64).ravel()
    J = c.size
    basis = imbe_dct_matrix(J) * float(J)          # undo the 1/J to get raw cosines
    w = c.copy()
    w[1:] *= 2.0
    return basis.T @ w


def imbe_ho_coefficient_map(L: int) -> tuple:
    """Which (block, coefficient) each higher-order quantizer b8..b_{L+1} feeds.

    TIA eq. (72), in its corrected form m = 6 + k + sum(J_n - 1): block-major,
    k ascending from 2 to J_i within each block, skipping blocks of length 1.
    (The accessible scan of the standard OCRs this as sum(J_n), which leaves b9
    unassigned — see docs/IMBE_SPECTRAL_AMPLITUDES.md.)

    Returns ((block_index_0based, k_1based), ...) of length L-6. Confirmed by the
    standard's own L=16 worked example, whose step sizes only come out right if
    this mapping is exactly C1,2 C2,2 C3,2 C3,3 C4,2 C4,3 C5,2 C5,3 C6,2 C6,3."""
    out = []
    for i, J in enumerate(imbe_amplitude_blocks(L)):
        for k in range(2, J + 1):
            out.append((i, k))
    return tuple(out)


# ------------------------------------------------------- derived: quantizer steps
def imbe_gain_step(bits: int, element: int) -> float:
    """Step size for gain-vector element G_{element+2}, element = 0..4.

    QUANT_STEP[bits] * GAIN_STD_DEV[element]. This GENERATES all 240 of the
    standard's Annex F step sizes rather than tabulating them, which is both
    less data and immune to the L=23 transposition in mbelib's stored copy."""
    if int(bits) <= 0:
        return 0.0
    return float(_T.QUANT_STEP[int(bits)] * _T.GAIN_STD_DEV[int(element)])


def imbe_ho_step(bits: int, k: int) -> float:
    """Step size for a higher-order DCT coefficient: QUANT_STEP[bits] * sigma_k.

    Sigma depends only on the coefficient's position k within its block, not on
    which block it is in (TIA Tables 3 and 4)."""
    if int(bits) <= 0:
        return 0.0
    return float(_T.QUANT_STEP[int(bits)] * _T.HO_STD_DEV[int(k)])


def _dequantize_scalar(index: int, bits: int, step: float) -> float:
    """TIA eq. (68): value = step * (index - 2^(bits-1) + 0.5). 0 bits -> 0.0.

    The index is read as an UNSIGNED integer, MSB first; this is what recentres
    it about zero. Equivalently (index - (2^bits - 1)/2) * step."""
    bits = int(bits)
    if bits <= 0:
        return 0.0
    return (float(index) - (float(2 ** bits) - 1.0) / 2.0) * float(step)


# ------------------------------------------ derived: prediction and warping
def imbe_prediction_coefficient(L: int) -> float:
    """TIA eq. (55): the inter-frame prediction coefficient, a function of L.

        rho = 0.4               L <= 15
            = 0.03*L - 0.05     15 < L <= 24
            = 0.7               L > 24

    The piecewise function is continuous at both breakpoints (0.03*15-0.05 = 0.40
    and 0.03*25-0.05 = 0.70), so the strict-vs-inclusive reading of the
    inequalities cannot change any value — which is why the ambiguity in the
    source scan is harmless here."""
    L = int(L)
    if L <= 15:
        return 0.4
    if L <= 24:
        return 0.03 * L - 0.05
    return 0.7


def imbe_warp_previous(prev_log_mags, L: int) -> np.ndarray:
    """Resample the previous frame's log2 amplitudes onto THIS frame's harmonics.

    The step that exists only because L changes frame to frame. TIA eqs.
    (75)/(76): the warp is driven by the ratio of HARMONIC COUNTS, not of
    fundamental frequencies —

        f_l = l * L(-1) / L(0),   k_l = floor(f_l),   d_l = f_l - k_l
        pred_l = (1 - d_l) * log2M_{k_l}(-1) + d_l * log2M_{k_l+1}(-1)

    Two boundary rules make the indexing total, and both are load-bearing:
      eq. (78) M_0(-1) = 1.0, i.e. log2 = 0 — needed because when L grows,
        f_1 < 1 and k_1 = 0, so index 0 is really read;
      eq. (79) M_l(-1) = M_{L(-1)}(-1) for l > L(-1) — hold the last value,
        needed because f_L = L(-1) makes k_L + 1 run one past the end.

    prev_log_mags is 1-indexed (slot 0 unused) of length L_prev+1. With no
    history the predictor contributes nothing and this returns zeros."""
    L = int(L)
    out = np.zeros(L + 1, dtype=np.float64)
    if prev_log_mags is None:
        return out
    prev = np.asarray(prev_log_mags, dtype=np.float64).ravel()
    L_prev = prev.size - 1
    if L_prev < 1:
        return out
    # P[0] = 0 is eq. (78); the tail repeat is eq. (79). One extra slot covers
    # the k_L + 1 read at the top end.
    P = np.zeros(L_prev + 2, dtype=np.float64)
    P[1:L_prev + 1] = prev[1:L_prev + 1]
    P[L_prev + 1] = prev[L_prev]
    f = np.arange(1, L + 1, dtype=np.float64) * (float(L_prev) / float(L))
    k = np.floor(f).astype(int)
    d = f - k
    k = np.clip(k, 0, L_prev)
    out[1:] = (1.0 - d) * P[k] + d * P[k + 1]
    return out


def imbe_undo_prediction(residual, warped_prev, rho: float) -> np.ndarray:
    """TIA eq. (77): log2M_l = T_l + rho*(pred_l - mean(pred)).

    The subtracted term is a SINGLE SCALAR for the whole frame — (rho/L) times
    the sum of the warped predictions — not a per-harmonic quantity. It has to be
    there because the frame's mean level is carried separately by the gain index;
    leaving it out produces a constant spectral tilt error, which is exactly the
    failure that sounds almost-right and is not."""
    r = np.asarray(residual, dtype=np.float64).ravel()
    p = np.asarray(warped_prev, dtype=np.float64).ravel()
    if r.size != p.size:
        raise ValueError(f"length mismatch: {r.size} vs {p.size}")
    out = np.zeros_like(r)
    if r.size > 1:
        mean_pred = float(np.mean(p[1:]))
        out[1:] = r[1:] + float(rho) * (p[1:] - mean_pred)
    return out


def imbe_prediction_residual(log_mags, warped_prev, rho: float) -> np.ndarray:
    """Encoder direction, the exact inverse of imbe_undo_prediction."""
    a = np.asarray(log_mags, dtype=np.float64).ravel()
    p = np.asarray(warped_prev, dtype=np.float64).ravel()
    if a.size != p.size:
        raise ValueError(f"length mismatch: {a.size} vs {p.size}")
    out = np.zeros_like(a)
    if a.size > 1:
        mean_pred = float(np.mean(p[1:]))
        out[1:] = a[1:] - float(rho) * (p[1:] - mean_pred)
    return out


# ------------------------------------------------- derived: block transform chain
def imbe_amplitude_forward(residual, L: int):
    """Prediction residual (1-indexed, length L+1) -> (gain_vector, higher).

    The encoder-side structure, kept because it makes the decoder-side structure
    provable: split into 6 blocks, DCT each, peel the 6 block means off into the
    gain vector, and keep the within-block coefficients k >= 2 as `higher`."""
    L = int(L)
    r = np.asarray(residual, dtype=np.float64).ravel()
    if r.size != L + 1:
        raise ValueError(f"residual must be 1-indexed length L+1={L + 1}, got {r.size}")
    means, higher, pos = [], [], 1
    for J in imbe_amplitude_blocks(L):
        c = imbe_dct(r[pos:pos + J])
        means.append(c[0])
        higher.append(c[1:].copy())
        pos += J
    return imbe_dct(np.asarray(means, dtype=np.float64)), higher


def imbe_amplitude_inverse(gain_vector, higher, L: int) -> np.ndarray:
    """(gain_vector, higher) -> prediction residual, 1-indexed length L+1.

    6-point IDCT of the gain vector gives the block means R_i (TIA eq. (69)),
    then a per-block IDCT gives the residual segments (eq. (73)), concatenated
    in block order."""
    L = int(L)
    lengths = imbe_amplitude_blocks(L)
    if len(higher) != IMBE_AMPLITUDE_BLOCKS:
        raise ValueError(f"expected {IMBE_AMPLITUDE_BLOCKS} coefficient blocks, "
                         f"got {len(higher)}")
    means = imbe_idct(gain_vector)
    out = np.zeros(L + 1, dtype=np.float64)
    pos = 1
    for i, J in enumerate(lengths):
        h = np.asarray(higher[i], dtype=np.float64).ravel()
        if h.size != J - 1:
            raise ValueError(f"block {i} needs {J - 1} higher coefficients, "
                             f"got {h.size}")
        out[pos:pos + J] = imbe_idct(np.concatenate([[means[i]], h]))
        pos += J
    return out


def imbe_magnitudes_from_coefficients(gain_vector, higher, warped_prev, rho: float,
                                      L: int):
    """Coefficients -> (linear magnitudes, unenhanced log2 magnitudes).

    Both are returned because they have different jobs: the magnitudes go to the
    synthesizer, and the UNENHANCED log2 values are what the next frame predicts
    against (TIA clause 8 is explicit about this)."""
    residual = imbe_amplitude_inverse(gain_vector, higher, L)
    log_mags = imbe_undo_prediction(residual, warped_prev, rho)
    mags = np.zeros(int(L) + 1, dtype=np.float64)
    mags[1:] = np.exp2(log_mags[1:])
    return mags, log_mags


# ----------------------------------------------------- derived: clause 8 enhancement
def imbe_enhance_amplitudes(mags, w0: float, L: int) -> np.ndarray:
    """TIA eqs. (105)-(110): sharpen the spectral envelope before synthesis.

    Weights each amplitude by a function of the first two autocorrelation lags of
    the spectrum, which pulls up formant peaks and pushes down the valleys
    between them, then renormalizes to preserve frame energy exactly. Speech is
    audibly muffled without it, so it is part of the decoder, not a nicety.

    The lowest harmonics (8*l <= L) are left alone, and the weight is clamped to
    [0.5, 1.2] so the enhancement can never invent a peak that was not there."""
    L = int(L)
    m = np.asarray(mags, dtype=np.float64).ravel().copy()
    if L < 1 or w0 <= 0.0:
        return m
    l = np.arange(1, L + 1, dtype=np.float64)
    sq = m[1:L + 1] ** 2
    rm0 = float(np.sum(sq))
    rm1 = float(np.sum(sq * np.cos(w0 * l)))
    denom = w0 * rm0 * (rm0 * rm0 - rm1 * rm1)
    if rm0 <= 0.0 or denom <= 0.0:
        return m                                   # degenerate frame: leave it alone
    num = 0.96 * math.pi * (rm0 * rm0 + rm1 * rm1
                            - 2.0 * rm0 * rm1 * np.cos(w0 * l))
    w = np.sqrt(m[1:L + 1]) * np.power(num / denom, 0.25)
    scale = np.clip(w, 0.5, 1.2)
    scale = np.where(8.0 * l <= L, 1.0, scale)     # leave the low band untouched
    m[1:L + 1] = m[1:L + 1] * scale
    total = float(np.sum(m[1:L + 1] ** 2))
    gamma = math.sqrt(rm0 / total) if total > 0.0 else 1.0
    m[1:L + 1] *= gamma                            # eqs. (109)/(110): energy preserved
    return m


# ------------------------------------------------ sourced: bits -> coefficients
def imbe_amplitude_available(L: int):
    """(ok, reason). True for every legal L now that the tables are complete.

    The L = 42/43/44 gap that used to live here is CLOSED — those rows were
    recovered by parsing mbelib's hoba[] out of a hash-pinned copy of the
    upstream header, and all three satisfy the bit-budget checksum. The guard
    itself is deliberately kept rather than deleted: _T.UNVERIFIED_L is now
    empty, so this is a no-op, but reinstating a gap is a one-line change and
    every caller already handles magnitudes=None correctly."""
    L = int(L)
    if L in _T.UNVERIFIED_L:
        return False, (f"spectral amplitude dequantizer: no verified bit "
                       f"allocation for L={L} — see survey/imbe_tables.py")
    if L not in _T.HIGHER_ORDER_BITS or L not in _T.GAIN_BITS:
        return False, f"spectral amplitude dequantizer: no bit allocation for L={L}"
    return True, ""


def imbe_amplitude_allocation(L: int) -> tuple:
    """The L-1 quantizer widths for this L: 5 gain-vector, then L-6 higher-order."""
    ok, why = imbe_amplitude_available(L)
    if not ok:
        raise ValueError(why)
    return tuple(_T.GAIN_BITS[int(L)]) + tuple(_T.HIGHER_ORDER_BITS[int(L)])


def imbe_amplitude_indices(amp_bits, L: int, allocation=None):
    """Slice the amplitude bit field into the L-1 quantizer indices, MSB-first.

    Validates the allocation against both the quantizer count and the budget, so
    a wrong table fails loudly here rather than desynchronizing the bit reader
    and producing confident nonsense downstream."""
    L = int(L)
    budget = imbe_amplitude_bit_budget(L)
    alloc = [int(b) for b in (imbe_amplitude_allocation(L) if allocation is None
                              else allocation)]
    if len(alloc) != budget["quantizers"]:
        raise ValueError(f"L={L} needs {budget['quantizers']} quantizers, "
                         f"allocation has {len(alloc)}")
    if any(b < 0 for b in alloc):
        raise ValueError("bit allocation must be non-negative")
    if sum(alloc) > budget["bits"]:
        raise ValueError(f"allocation needs {sum(alloc)} bits, budget is "
                         f"{budget['bits']}")
    bits = np.asarray(amp_bits, dtype=np.uint8).ravel() & 1
    if bits.size < sum(alloc):
        raise ValueError(f"need {sum(alloc)} amplitude bits, got {bits.size}")
    out, pos = [], 0
    for b in alloc:
        out.append(bits_to_int(bits[pos:pos + b]) if b else 0)
        pos += b
    return out, alloc


def dequantize_imbe_amplitudes(amp_bits, L: int, gain_index: int,
                               prev_log_mags=None, w0: float = 0.0,
                               enhance: bool = True):
    """Amplitude bits -> (magnitudes, unenhanced log2 magnitudes, note).

    The whole clause-6.4 decode: unpack the quantizer indices, reconstruct the
    gain vector (G_1 from the 6-bit index through the Annex E table, G_2..G_6
    from b3..b7) and the higher-order DCT coefficients, inverse-transform both,
    undo the frequency-warped inter-frame prediction, exponentiate, and enhance.

    Returns (None, None, reason) for the three L values whose bit allocation
    could not be sourced — see imbe_amplitude_available(). It does not guess."""
    L = int(L)
    ok, why = imbe_amplitude_available(L)
    if not ok:
        return None, None, why

    indices, alloc = imbe_amplitude_indices(amp_bits, L)
    n_gain = IMBE_AMPLITUDE_BLOCKS - 1

    gain_vector = np.zeros(IMBE_AMPLITUDE_BLOCKS, dtype=np.float64)
    gain_vector[0] = float(_T.GAIN_LEVELS[int(gain_index) & 0x3F])
    for j in range(n_gain):
        gain_vector[j + 1] = _dequantize_scalar(
            indices[j], alloc[j], imbe_gain_step(alloc[j], j))

    lengths = imbe_amplitude_blocks(L)
    higher = [np.zeros(J - 1, dtype=np.float64) for J in lengths]
    for q, (i, k) in enumerate(imbe_ho_coefficient_map(L)):
        b = alloc[n_gain + q]
        higher[i][k - 2] = _dequantize_scalar(indices[n_gain + q], b,
                                              imbe_ho_step(b, k))

    rho = imbe_prediction_coefficient(L)
    warped = imbe_warp_previous(prev_log_mags, L)
    mags, log_mags = imbe_magnitudes_from_coefficients(gain_vector, higher,
                                                       warped, rho, L)
    if enhance:
        mags = imbe_enhance_amplitudes(mags, w0, L)
    # log_mags is returned UNENHANCED and UNSCALED on purpose — it is the
    # predictor state, and it has to stay in the codec's own log2 domain or the
    # next frame's prediction is offset by log2(IMBE_OUTPUT_SCALE).
    return mags * IMBE_OUTPUT_SCALE, log_mags, "spectral amplitudes decoded"


# --------------------------------------------- derived: the bit priority scan
def imbe_priority_scan(L: int, allocation=None) -> tuple:
    """TIA-102.BABA Fig. 22 — the order the amplitude quantizer bits go on air.

    Figure 22 draws b_3..b_{L+1} as columns, bit 10 (the MSB row) at the top
    down to bit 0 at the bottom, and shades the cells each quantizer actually
    owns. The scan begins at the upper left and reads LEFT TO RIGHT ALONG EACH
    ROW, top row first, taking only shaded cells.

    So the frame is ordered by BIT SIGNIFICANCE across all quantizers at once:
    every quantizer's MSB is transmitted before any quantizer's second bit. That
    is what makes the leading bits the highest-priority ones, and it is why the
    amplitude field cannot be read as a contiguous run of per-quantizer values.

    Returns ((quantizer, bit), ...) of length sum(allocation), with the
    quantizer numbered the standard's way so that the first one is b_3."""
    L = int(L)
    alloc = [int(b) for b in (imbe_amplitude_allocation(L) if allocation is None
                              else allocation)]
    if len(alloc) != L - 1:
        raise ValueError(f"L={L} has quantizers b_3..b_{L + 1} = {L - 1} of them, "
                         f"allocation has {len(alloc)}")
    if any(b < 0 or b > IMBE_MAX_QUANT_BITS for b in alloc):
        raise ValueError(f"allocation entries must be 0..{IMBE_MAX_QUANT_BITS}")
    return tuple((3 + c, r)
                 for r in range(IMBE_MAX_QUANT_BITS - 1, -1, -1)
                 for c in range(L - 1) if alloc[c] > r)


def imbe_control_map(K: int) -> dict:
    """{offset: (quantizer, bit)} for the V/UV and gain bits inside info[6:85].

    Worth isolating because these positions depend ONLY on K — that is, only on
    the pitch index — and not on the spectral amplitude bit allocation. So the
    voicing decisions and the gain index of a frame remain recoverable even for
    a harmonic count whose amplitude allocation is unsourced, which is what lets
    imbe_amplitude_available() stay a magnitudes-only gate instead of making the
    whole frame unreadable.

    Section 7.1 order: gain bits 5,4,3 lead the prioritized stream; 39 amplitude
    scan bits follow; then the whole V/UV field MSB first (its MSB is the LOWEST
    band); then gain bits 2 and 1; the scan resumes; and gain bit 0 is last."""
    K = int(K)
    m = {0: (2, 5), 1: (2, 4), 2: (2, 3)}
    base = IMBE_CONTROL_PREFIX + IMBE_SCAN_BREAK
    for j in range(K):
        m[base + j] = (1, K - 1 - j)
    m[base + K] = (2, 2)
    m[base + K + 1] = (2, 1)
    m[IMBE_SCAN_STOP - IMBE_SCAN_START - 1] = (2, 0)
    return m


def imbe_priority_map(L: int, allocation=None) -> tuple:
    """(quantizer, bit) for each of the 79 prioritized positions info[6:85].

    The control positions from imbe_control_map() with the Fig. 22 amplitude
    scan threaded through everything left over.

    Quantizer 1 is b_1 (the V/UV decisions), quantizer 2 is b_2 (the gain
    index), and 3 and up are the spectral amplitude quantizers b_3..b_{L+1}.

    The 79 = 6 + K + sum(alloc) accounting only closes when sum(alloc) is
    exactly the 73 - K bit budget, so a wrong allocation table cannot quietly
    produce a plausible-looking map — it raises here instead."""
    L = int(L)
    K = imbe_band_count(L)
    n = IMBE_SCAN_STOP - IMBE_SCAN_START
    ctrl = imbe_control_map(K)
    scan = list(imbe_priority_scan(L, allocation))
    free = [p for p in range(n) if p not in ctrl]
    if len(scan) != len(free):
        raise ValueError(f"L={L}: {len(scan)} amplitude bits for {len(free)} free "
                         f"positions — the bit allocation does not sum to the "
                         f"{imbe_amplitude_bit_budget(L)['bits']}-bit budget")
    out = dict(ctrl)
    out.update(zip(free, scan))
    return tuple(out[p] for p in range(n))


def _imbe_gather(bits, L: int):
    """info[6:85] -> (b_1, b_2, quantizer indices or None).

    b_1 and b_2 come out of the control positions and are always recoverable.
    The amplitude quantizers need the bit allocation, so they come back None for
    a harmonic count imbe_amplitude_available() refuses."""
    K = imbe_band_count(L)
    b1 = b2 = 0
    for pos, (m, n) in imbe_control_map(K).items():
        if int(bits[IMBE_SCAN_START + pos]) & 1:
            if m == 1:
                b1 |= 1 << n
            else:
                b2 |= 1 << n
    if not imbe_amplitude_available(L)[0]:
        return b1, b2, None, None
    alloc = imbe_amplitude_allocation(L)
    qval = [0] * (int(L) - 1)
    for pos, (m, n) in enumerate(imbe_priority_map(L, alloc)):
        if m >= 3 and int(bits[IMBE_SCAN_START + pos]) & 1:
            qval[m - 3] |= 1 << n
    return b1, b2, qval, alloc


def _imbe_scatter(out, L: int, b1: int, b2: int, qval) -> None:
    """The exact inverse of _imbe_gather, writing into out[6:85] in place."""
    for pos, (m, n) in enumerate(imbe_priority_map(L)):
        src = b1 if m == 1 else b2 if m == 2 else qval[m - 3]
        out[IMBE_SCAN_START + pos] = (int(src) >> n) & 1


# -------------------------------------------------------- IMBE frame -> parameters
def decode_imbe_params(bits88, prev_log_mags=None, enhance: bool = True) -> dict:
    """88 IMBE information bits -> MBE parameters, per TIA-102.BABA section 7.1.

    The 88 bits are the eight prioritized vectors u_0..u_7 concatenated MSB
    first, exactly as unpack_imbe_frame() returns them. This function undoes the
    priority scan documented at IMBE_PITCH_BITS above — it does NOT read the
    fields as contiguous slices, because on a real radio they are not.

    prev_log_mags is the previous frame's UNENHANCED log2 amplitudes (the
    `log_magnitudes` this function returned last frame), which the inter-frame
    predictor needs; pass None for the first frame of a stream. The returned
    magnitudes are enhanced and ready for synthesis, while log_magnitudes is the
    unenhanced predictor state — feeding the enhanced values back instead causes
    a slow drift that degrades intelligibility over a few seconds.

    `amplitude_bits` is the GATHERED amplitude field: the quantizer values
    b_3..b_{L+1} re-serialized MSB-first in allocation order. That is the form
    the dequantizer consumes and the form worth logging for analysis; it is a
    permutation of bits drawn from all over the frame, not a slice of it.

    magnitudes is None only when the amplitude bit allocation for this L is not
    sourced (see survey/imbe_tables.py); `note` says so. A reserved pitch index
    (>= 208) is a silence / frame repeat code and is reported as such, not
    decoded as a bogus 405 Hz buzz."""
    bits = _as_bits(bits88, IMBE_INFO_BITS, "IMBE information frame")
    # b_0 is split: six bits at the front, two at info[85..86]. See section 7.1.
    b0 = ((bits_to_int(bits[0:IMBE_PITCH_HIGH_BITS])
           << (IMBE_PITCH_BITS - IMBE_PITCH_HIGH_BITS))
          | bits_to_int([bits[i] for i in IMBE_B0_LOW_INDICES]))
    if b0 >= IMBE_PITCH_RESERVED:
        return {"b0": b0, "reserved_pitch": True, "silence": True, "w0": 0.0,
                "f0_hz": 0.0, "L": 0, "K": 0, "voiced_bands": [],
                "voiced": np.zeros(1, dtype=bool), "gain_index": 0,
                "amplitude_bits": np.zeros(0, dtype=np.uint8), "magnitudes": None,
                "log_magnitudes": None, "magnitudes_available": False,
                "note": "reserved pitch index — silence / frame repeat"}

    w0, f0_hz, L = imbe_pitch(b0)
    K = imbe_band_count(L)
    b1, gain_index, qval, alloc = _imbe_gather(bits, L)
    # b_1's MSB is the LOWEST band (mbelib reads bb[1][K-1] for harmonic 1).
    band_flags = [bool((b1 >> (K - 1 - j)) & 1) for j in range(K)]
    if qval is None:
        # Pitch, voicing and gain are recoverable; the amplitudes are not.
        amp_bits = np.zeros(0, dtype=np.uint8)
        mags, log_mags, note = None, None, imbe_amplitude_available(L)[1]
    else:
        amp_bits = np.concatenate(
            [int_to_bits(qval[c], alloc[c]) for c in range(L - 1)]
            + [np.zeros(0, dtype=np.uint8)]).astype(np.uint8)
        mags, log_mags, note = dequantize_imbe_amplitudes(
            amp_bits, L, gain_index, prev_log_mags=prev_log_mags, w0=w0,
            enhance=enhance)
    return {"b0": b0, "reserved_pitch": False, "silence": False, "w0": w0,
            "f0_hz": f0_hz, "L": L, "K": K, "voiced_bands": band_flags,
            "voiced": _expand_voiced(band_flags, L), "gain_index": gain_index,
            "amplitude_bits": amp_bits, "magnitudes": mags,
            "log_magnitudes": log_mags,
            "magnitudes_available": mags is not None,
            "note": note}


def pack_imbe_params(b0: int, voiced_bands, gain_index: int,
                     amplitude_bits=None) -> np.ndarray:
    """Build 88 information bits from parameter fields (test/self-check path).

    The exact inverse of decode_imbe_params, priority scan and all, so the two
    now agree on the STANDARD's layout rather than on a shared local convention.
    Round-tripping through this pair is therefore still not evidence that the
    layout is right — that comes from TIA-102.BABA 7.1 and from the per-entry
    agreement with mbelib's bo[] table, both cited at IMBE_PITCH_BITS.

    amplitude_bits is the gathered field (quantizer values MSB-first in
    allocation order, the same thing decode_imbe_params returns) and pads with
    zeros to fill the frame."""
    b0 = int(b0)
    out = np.zeros(IMBE_INFO_BITS, dtype=np.uint8)
    hi = IMBE_PITCH_BITS - IMBE_PITCH_HIGH_BITS
    out[0:IMBE_PITCH_HIGH_BITS] = int_to_bits(b0 >> hi, IMBE_PITCH_HIGH_BITS)
    for k, idx in enumerate(IMBE_B0_LOW_INDICES):
        out[idx] = (b0 >> (hi - 1 - k)) & 1
    if b0 >= IMBE_PITCH_RESERVED:
        return out
    _, _, L = imbe_pitch(b0)
    K = imbe_band_count(L)
    flags = list(voiced_bands)
    if len(flags) != K:
        raise ValueError(f"b0={b0} implies L={L}, K={K} bands; got {len(flags)} flags")
    b1 = 0
    for j, f in enumerate(flags):
        if f:
            b1 |= 1 << (K - 1 - j)
    n_amp = imbe_amplitude_bit_budget(L)["bits"]
    amp = np.zeros(n_amp, dtype=np.uint8)
    if amplitude_bits is not None:
        supplied = np.asarray(amplitude_bits, dtype=np.uint8).ravel() & 1
        if supplied.size > n_amp:
            raise ValueError(f"amplitude_bits too long: {supplied.size} > {n_amp}")
        amp[:supplied.size] = supplied
    qval, _ = imbe_amplitude_indices(amp, L)
    _imbe_scatter(out, L, b1, int(gain_index) & 0x3F, qval)
    # info[87] is b_{L+2}, which the decoder does not use; left zero.
    return out


# ------------------------------------------------------------ placeholder envelope
def synthetic_envelope(L: int, gain_index: int) -> np.ndarray:
    """A PLACEHOLDER spectral envelope — NOT the IMBE amplitude tables.

    1/l roll-off (-6 dB/octave, a crude glottal-source approximation), scaled by a
    bounded linear map of the 6-bit gain index. Feeding this to the synthesizer
    produces a buzz at the frame's true pitch with the frame's true voicing
    pattern, and NOTHING of the speaker's formants — i.e. you can hear that
    someone is talking and roughly how, but not what.

    Now that the real dequantizer exists this is a FALLBACK, not the main path:
    decode_p25_voice only reaches for it on the L = 42/43/44 frames whose bit
    allocation is unsourced, and never in preference to a real decode. It is
    still never enabled by default, and frames using it are always reported
    intelligible=False.

    Normalized by the SUM of the harmonic amplitudes, not their energy, so that
    even the worst case (all harmonics phase-aligned, which really does happen
    once per pitch period) stays under half scale and the placeholder can never
    be the thing that clips."""
    L = max(1, int(L))
    l = np.arange(1, L + 1, dtype=np.float64)
    env = 1.0 / l
    env /= float(np.sum(env))
    scale = 0.05 + 0.45 * (int(np.clip(gain_index, 0, 63)) / 63.0)
    out = np.zeros(L + 1, dtype=np.float64)
    out[1:] = scale * env
    return out


# ------------------------------------------------------------------ MBE synthesis
class MbeState:
    """Per-stream synthesis state. One per talkgroup/stream, reused frame to frame.

    Carries what MUST NOT reset at a 20 ms boundary: the absolute phase of every
    harmonic (reset it and every frame starts with a click), the previous frame's
    magnitudes/voicing/pitch (the synthesizer cross-fades between frames rather
    than butt-joining them), the unvoiced overlap-add tail, and the noise RNG (so
    a decode is reproducible for tests and for evidentiary replay)."""

    def __init__(self, seed: int = 0, n: int = FRAME_SAMPLES):
        self.n = int(n)
        self.phase = np.zeros(MAX_HARMONICS + 2, dtype=np.float64)
        self.prev_w0 = 0.0
        self.prev_mag = np.zeros(MAX_HARMONICS + 2, dtype=np.float64)
        self.prev_voiced = np.zeros(MAX_HARMONICS + 2, dtype=bool)
        self.tail = np.zeros(self.n, dtype=np.float64)
        self.rng = np.random.default_rng(seed)
        self.window = np.sin(np.pi * (np.arange(2 * self.n) + 0.5) / (2 * self.n))
        # Inter-frame amplitude predictor state: the previous frame's UNENHANCED
        # log2 amplitudes, 1-indexed. The IMBE spec initialises a new stream to
        # M_l = 1.0 for all l (log2 = 0) with L(-1) = 30, so a stream's first
        # frame predicts against flat unity rather than against nothing.
        self.prev_log_mags = np.zeros(IMBE_INIT_PREV_L + 1, dtype=np.float64)

    def reset(self):
        """Hard reset for a new stream (new call / new talkgroup). Not used at
        frame boundaries — that is the whole point of the class."""
        self.phase[:] = 0.0
        self.prev_w0 = 0.0
        self.prev_mag[:] = 0.0
        self.prev_voiced[:] = False
        self.tail[:] = 0.0
        self.prev_log_mags = np.zeros(IMBE_INIT_PREV_L + 1, dtype=np.float64)


def voice_params(f0_hz: float, L: int = None, voiced=True, magnitudes=None) -> dict:
    """Build a synthesizer parameter dict directly from physical quantities.

    The entry point for tests, for callers that obtained magnitudes elsewhere, and
    and for callers that obtained magnitudes elsewhere. voiced may be a bool (applies to
    every harmonic) or a per-harmonic sequence; magnitudes may be a scalar or a
    per-harmonic sequence. Harmonic arrays are 1-indexed (index 0 unused)."""
    w0 = 2.0 * math.pi * float(f0_hz) / SAMPLE_RATE
    if w0 <= 0:
        raise ValueError("f0_hz must be positive")
    if L is None:
        L = int(np.clip(int(0.9254 * math.pi / w0), 1, MAX_HARMONICS))
    L = int(L)
    v = np.zeros(L + 1, dtype=bool)
    if np.isscalar(voiced):
        v[1:] = bool(voiced)
    else:
        seq = list(voiced)
        if len(seq) == L:                 # allow a plain 0..L-1 sequence
            seq = [False] + seq
        v[:len(seq)] = [bool(x) for x in seq[:L + 1]]
    mag = None
    if magnitudes is not None:
        mag = np.zeros(L + 1, dtype=np.float64)
        if np.isscalar(magnitudes):
            mag[1:] = float(magnitudes)
        else:
            seq = list(magnitudes)
            if len(seq) == L:
                seq = [0.0] + seq
            mag[:len(seq)] = [float(x) for x in seq[:L + 1]]
    return {"w0": w0, "f0_hz": float(f0_hz), "L": L, "voiced": v,
            "magnitudes": mag, "silence": False, "gain_index": 0,
            "magnitudes_available": mag is not None}


def _harmonic_arrays(params, n_slots: int):
    """(w0, magnitudes, voiced) as fixed-length arrays, tolerant of a silence frame."""
    mag = np.zeros(n_slots, dtype=np.float64)
    voi = np.zeros(n_slots, dtype=bool)
    if params is None or params.get("silence"):
        return 0.0, mag, voi
    L = int(params.get("L", 0))
    m = params.get("magnitudes")
    if m is not None and L > 0:
        m = np.asarray(m, dtype=np.float64)
        k = min(L + 1, m.size, n_slots)
        mag[:k] = m[:k]
    v = params.get("voiced")
    if v is not None and L > 0:
        v = np.asarray(v, dtype=bool)
        k = min(L + 1, v.size, n_slots)
        voi[:k] = v[:k]
    return float(params.get("w0", 0.0)), mag, voi


def _synth_voiced(w0_prev, w0_cur, mag_prev, mag_cur, voi_prev, voi_cur,
                  state: MbeState, n: int) -> np.ndarray:
    """Sum of harmonic sinusoids with continuous phase across the frame seam.

    Amplitude is linearly cross-faded from the previous frame's value to this
    frame's (a harmonic that switches voiced->unvoiced therefore RAMPS to zero
    instead of stopping dead), and frequency is linearly interpolated, which makes
    the phase quadratic in n: theta(t) = theta0 + w_p*t + (w_c - w_p)*t^2/(2N).
    Carrying theta(N) into the next frame is what keeps a sustained vowel from
    clicking 50 times a second."""
    idx = np.nonzero((mag_prev > 0) & voi_prev | (mag_cur > 0) & voi_cur)[0]
    idx = idx[idx >= 1]
    if idx.size == 0:
        return np.zeros(n, dtype=np.float64)
    ll = idx.astype(np.float64)
    wp = (w0_prev if w0_prev > 0 else w0_cur) * ll
    wc = (w0_cur if w0_cur > 0 else w0_prev) * ll
    # Guard against aliasing: a harmonic pushed past Nyquist by a bad pitch decode
    # would fold back as a phantom low tone, which is exactly the kind of invented
    # audio this module must not produce.
    live = (wp < math.pi) & (wc < math.pi)
    if not np.any(live):
        return np.zeros(n, dtype=np.float64)
    idx, ll, wp, wc = idx[live], ll[live], wp[live], wc[live]

    a_prev = np.where(voi_prev[idx], mag_prev[idx], 0.0)[:, None]
    a_cur = np.where(voi_cur[idx], mag_cur[idx], 0.0)[:, None]
    t = np.arange(n, dtype=np.float64)[None, :]
    ramp = (t + 1.0) / float(n)
    amp = a_prev + (a_cur - a_prev) * ramp
    theta = (state.phase[idx][:, None] + wp[:, None] * t
             + (wc - wp)[:, None] * t * t / (2.0 * n))
    out = np.sum(amp * np.cos(theta), axis=0)
    state.phase[idx] = np.mod(state.phase[idx] + 0.5 * (wp + wc) * n, 2.0 * math.pi)
    return out


def _synth_unvoiced(w0, mag, voi, state: MbeState, n: int) -> np.ndarray:
    """Band-shaped noise for the unvoiced harmonics, overlap-added at 50%.

    Each unvoiced harmonic l owns the spectral band ((l-0.5)*w0, (l+0.5)*w0). We
    fill that band's FFT bins with unit-variance random phase scaled so the band's
    time-domain RMS equals A_l/sqrt(2) — i.e. the same power a voiced sinusoid of
    amplitude A_l would carry, so a band flipping voiced<->unvoiced doesn't jump in
    loudness. The synthesis window is a SINE window, not Hann: sin^2 + cos^2 = 1
    means the two overlapping halves sum to constant POWER, which is what matters
    for noise (a Hann OLA would leave audible 50 Hz amplitude ripple)."""
    m = 2 * n
    y = np.zeros(m, dtype=np.float64)
    if w0 > 0:
        binw = np.arange(n + 1, dtype=np.float64) * math.pi / n   # rad/sample per bin
        spec = np.zeros(n + 1, dtype=np.complex128)
        any_band = False
        for l in range(1, mag.size):
            if not voi[l] and mag[l] > 0:
                lo, hi = (l - 0.5) * w0, (l + 0.5) * w0
                if lo >= math.pi:
                    break
                sel = np.nonzero((binw >= lo) & (binw < min(hi, math.pi)))[0]
                sel = sel[sel > 0]
                if sel.size == 0:
                    continue
                ph = state.rng.uniform(0.0, 2.0 * math.pi, sel.size)
                # |Y_k| = n*A/sqrt(#bins) makes the incoherent sum of the band's
                # bins land at RMS A/sqrt(2) after numpy's irfft scaling.
                spec[sel] = (n * mag[l] / math.sqrt(sel.size)) * np.exp(1j * ph)
                any_band = True
        if any_band:
            y = np.fft.irfft(spec, m) * state.window
    out = y[:n] + state.tail
    state.tail = y[n:].copy()
    return out


def mbe_synthesize(params, state: MbeState = None, n: int = FRAME_SAMPLES):
    """One MBE frame -> n PCM samples (float32, -1..1). Returns (pcm, info).

    params: a dict from voice_params() or decode_imbe_params(). magnitudes=None or
    silence=True synthesizes SILENCE — but still runs the cross-fade and the
    overlap-add, so the previous frame decays smoothly instead of being chopped.
    info reports clipping, which is the only way this function can distort."""
    if state is None:
        state = MbeState(n=n)
    n = int(n)
    slots = MAX_HARMONICS + 2
    w0_cur, mag_cur, voi_cur = _harmonic_arrays(params, slots)
    mag_prev, voi_prev, w0_prev = state.prev_mag, state.prev_voiced, state.prev_w0
    if w0_prev <= 0:
        w0_prev = w0_cur
    if w0_cur <= 0:
        w0_cur = w0_prev

    v = _synth_voiced(w0_prev, w0_cur, mag_prev, mag_cur, voi_prev, voi_cur, state, n)
    u = _synth_unvoiced(w0_cur, mag_cur, voi_cur, state, n)
    y = v + u
    clipped = int(np.count_nonzero(np.abs(y) > 1.0))
    y = np.clip(y, -1.0, 1.0)

    state.prev_mag = mag_cur
    state.prev_voiced = voi_cur
    state.prev_w0 = w0_cur
    return y.astype(np.float32), {"clipped": clipped, "samples": n}


def synthesize_silence(n: int = FRAME_SAMPLES) -> np.ndarray:
    return np.zeros(int(n), dtype=np.float32)


# --------------------------------------------------------------------- top level
def _empty_report(reason: str, **extra) -> dict:
    r = {"fec_ok": False, "errors": 0, "vector_errors": [], "params_available": False,
         "magnitudes_available": False, "intelligible": False, "envelope": None,
         "silent": True, "note": reason}
    r.update(extra)
    return r


def decode_p25_voice(frames, envelope=None, state: MbeState = None,
                     err_limit: int = IMBE_ERR_LIMIT):
    """P25 Phase 1 IMBE frames -> (pcm float32 at 8 kHz, per-frame reports).

    frames: an iterable of bit sequences, each either 144 bits (coded, FEC applied
    here) or 88 bits (already FEC-decoded upstream).
    envelope: None (default) -> the DECODED spectral envelope, i.e. real speech.
    "synthetic" -> the documented placeholder envelope (pitch/voicing
    sonification only), kept because it still answers "is a human talking on this
    channel?" on frames whose amplitude allocation is unsourced.

    `intelligible` in each report is True only when the frame's magnitudes were
    genuinely decoded. It remains an assertion about the DECODE, not about
    audibility, and nothing here has ever been checked against off-air audio —
    see the module docstring before putting this in front of an operator.

    State carries across frames and matters: the amplitude predictor needs the
    previous frame's unenhanced log2 amplitudes, so pass one MbeState per stream
    and do not share it between talkgroups.

    Every frame yields exactly FRAME_SAMPLES samples, so pcm length is always
    160 * len(frames) — a failed frame is silence, never a dropout. "Silence"
    means no NEW excitation: a preceding frame's harmonics are still allowed to
    ramp down across the muted frame, because cutting them dead would click, and
    a decaying tail is audio already emitted rather than audio invented."""
    if envelope not in (None, "synthetic"):
        raise ValueError("envelope must be None or 'synthetic'")
    if state is None:
        state = MbeState()
    out, reports = [], []
    for raw in frames:
        bits = np.asarray(raw, dtype=np.uint8).ravel() & 1
        if bits.size == IMBE_FRAME_BITS:
            fec = unpack_imbe_frame(bits, err_limit=err_limit)
        elif bits.size == IMBE_INFO_BITS:
            fec = {"info_bits": bits, "errors": 0, "vector_errors": [],
                   "fec_ok": True, "note": "pre-decoded information bits"}
        else:
            out.append(synthesize_silence())
            reports.append(_empty_report(
                f"frame must be {IMBE_FRAME_BITS} or {IMBE_INFO_BITS} bits, "
                f"got {bits.size}"))
            continue
        if not fec["fec_ok"]:
            # Hard stop. A frame this damaged descrambles to noise, and noise
            # through an MBE synthesizer is a convincing-sounding lie.
            pcm, _ = mbe_synthesize({"silence": True}, state)
            out.append(pcm)
            reports.append(_empty_report(fec["note"], errors=fec["errors"],
                                         vector_errors=fec["vector_errors"]))
            continue

        params = decode_imbe_params(fec["info_bits"],
                                    prev_log_mags=state.prev_log_mags)
        decoded = params["magnitudes"] is not None
        if decoded:
            # The predictor chains on the UNENHANCED values (TIA clause 8).
            state.prev_log_mags = params["log_magnitudes"]
        if envelope == "synthetic" and not params["silence"] and not decoded:
            params = dict(params)
            params["magnitudes"] = synthetic_envelope(params["L"],
                                                      params["gain_index"])
            params["magnitudes_available"] = True
        pcm, sinfo = mbe_synthesize(params, state)
        out.append(pcm)
        reports.append({
            "fec_ok": True, "errors": fec["errors"],
            "vector_errors": fec["vector_errors"], "params_available": True,
            "magnitudes_available": bool(params.get("magnitudes_available")),
            # True only when this frame's amplitudes were really dequantized.
            # A synthetic-envelope frame is NEVER intelligible, by construction.
            "intelligible": bool(decoded),
            "envelope": envelope, "silent": params["magnitudes"] is None,
            "b0": params["b0"], "f0_hz": params["f0_hz"], "L": params["L"],
            "K": params["K"], "voiced_bands": params["voiced_bands"],
            "gain_index": params["gain_index"],
            "reserved_pitch": params["reserved_pitch"], "clipped": sinfo["clipped"],
            "note": params["note"] if decoded or envelope is None else
                    "synthetic placeholder envelope — pitch/voicing only, NOT speech",
        })
    pcm = (np.concatenate(out).astype(np.float32) if out
           else np.zeros(0, dtype=np.float32))
    return pcm, reports


def decode_dmr_voice(frames, state: MbeState = None,
                     err_limit: int = AMBE_ERR_LIMIT):
    """DMR AMBE+2 frames -> (pcm float32 at 8 kHz, per-frame reports).

    frames: an iterable of bit sequences, each either 72 bits (coded) or 49 bits
    (already FEC-decoded upstream).

    The PCM is ALWAYS silence. The AMBE+2 2450 parameter quantizer is not
    implemented (module docstring), so there are no parameters to synthesize from,
    and this function will not borrow IMBE's quantizer and pretend — they are
    different codecs and the result would be noise wearing a speech costume.

    It is still worth calling: the FEC layer is real, so `fec_ok` and `errors`
    answer the field question 'are these bursts actually AMBE voice frames, or am
    I looking at noise / a different protocol?', and `info_bits` gives the next
    engineer verified payload to develop the quantizer against."""
    if state is None:
        state = MbeState()
    out, reports = [], []
    for raw in frames:
        bits = np.asarray(raw, dtype=np.uint8).ravel() & 1
        if bits.size == AMBE_FRAME_BITS:
            fec = unpack_ambe_frame(bits, err_limit=err_limit)
        elif bits.size == AMBE_INFO_BITS:
            fec = {"info_bits": bits, "errors": 0, "vector_errors": [],
                   "fec_ok": True, "note": "pre-decoded information bits"}
        else:
            out.append(synthesize_silence())
            reports.append(_empty_report(
                f"frame must be {AMBE_FRAME_BITS} or {AMBE_INFO_BITS} bits, "
                f"got {bits.size}"))
            continue
        out.append(synthesize_silence())
        if not fec["fec_ok"]:
            reports.append(_empty_report(fec["note"], errors=fec["errors"],
                                         vector_errors=fec["vector_errors"]))
            continue
        reports.append({
            "fec_ok": True, "errors": fec["errors"],
            "vector_errors": fec["vector_errors"], "params_available": False,
            "magnitudes_available": False, "intelligible": False, "envelope": None,
            "silent": True, "info_bits": fec["info_bits"],
            "note": "AMBE+2 2450 parameter quantizer not implemented — "
                    "FEC verified, no audio",
        })
    pcm = (np.concatenate(out).astype(np.float32) if out
           else np.zeros(0, dtype=np.float32))
    return pcm, reports


def capability() -> dict:
    """Machine-readable statement of what this module can and cannot do.

    Exists so the router/UI/report layer can surface the gap automatically instead
    of relying on somebody having read the docstring. Any caller that renders
    vocoder audio to an operator should render `caveat` next to it."""
    unverified = tuple(_T.UNVERIFIED_L)
    total_L = MAX_HARMONICS - 8                      # L = 9..56
    return {
        "module": "ambe",
        "sample_rate": SAMPLE_RATE,
        # The transport contract, stated where the router can read it.
        "input_bit_order": (
            "IMBE: 144 coded bits in code-word order c_0..c_7, MSB-first — i.e. "
            "ALREADY transport-de-interleaved. survey/p25.py does this per "
            "TIA-102.BAAA-A Table 5-1 before calling here; any other caller "
            "must too."),
        # Two constants that used to be local inventions and now are not. Kept
        # machine-readable because 'we fixed it' is worth nothing without saying
        # what it was checked against.
        "air_interface_conformance": {
            "hamming15_11_generator": {
                "source": "TIA-102.BAAA-A section 5.8, Table 5-4",
                "corroborated_by": ("mbelib ecc_const.h hammingGenerator[4], "
                                    "parsed deterministically (ISC)"),
                "was": ("a CRC on g(x)=x^4+x+1, which generates the same code "
                        "with permuted rows — right distance, right syndromes, "
                        "perfect round-trip, WRONG codewords off-air"),
            },
            "imbe_88_bit_field_order": {
                "source": "TIA-102.BABA section 7.1 (Bit Prioritization), Fig. 22",
                "derived": True,
                "corroborated_by": (
                    "reproduces mbelib's tabulated bo[48][79][2] priority map "
                    "for all 3792 entries, parsed from imbe7200x4400_const.h "
                    "sha256 " + str(_T.MBELIB_SHA256)),
                "was": ("b0=info[0:8] then b1, b2 and a contiguous amplitude "
                        "field — a local convention that decoded a clean "
                        "off-air frame to the wrong pitch, hence the wrong L"),
            },
        },
        "protocols": {
            "p25_phase1_imbe": {"fec": True,
                                "parameters": "pitch + voicing + magnitudes",
                                "magnitudes": True,
                                "audio": "decoded speech envelope"},
            "dmr_ambe2": {"fec": True, "parameters": False, "magnitudes": False,
                          "audio": False},
        },
        "spectral_amplitudes": {
            "implemented": True,
            "derived": ["6-block split", "DCT / inverse DCT",
                        "higher-order coefficient map", "gain-vector step sizes",
                        "prediction coefficient", "frequency warping when L changes",
                        "frame-mean correction", "clause-8 enhancement"],
            "sourced_tables": "survey/imbe_tables.py (mbelib, ISC)",
            "attribution_required": (
                "mbelib ISC notice (Copyright (C) 2010 mbelib Author) — DISCHARGED "
                "in THIRD_PARTY_NOTICES.md at the repo root, which must ship with "
                "any distribution (installer, laptop bundle, source). "
                "tests/test_third_party_notices.py fails if it goes missing."),
            "supported_L": total_L - len(unverified),
            "total_L": total_L,
            "unsupported_L": unverified,
            "unsupported_reason": (
                "none — the former L=42/43/44 gap is closed" if not unverified else
                "higher-order bit allocation for these harmonic counts could not "
                "be sourced reliably; those frames decode to magnitudes=None"),
            # What each sourced table has actually been checked against, stated
            # separately because the two tables do NOT have equal evidence.
            "table_verification": {
                "source_artifact": "szechyjs/mbelib imbe7200x4400_const.h",
                "source_sha256": _T.MBELIB_SHA256,
                "method": ("downloaded verbatim and parsed deterministically; no "
                           "summarising fetch in the loop"),
                "independent_origins": 2,   # GitHub and Software Heritage
                "gain_bits": ("240/240 per-entry — each bit count is confirmed by "
                              "the step size mbelib stores beside it "
                              "(239 exact + the known L=23 typo we generate around)"),
                "higher_order_bits": (
                    "1272/1272 per-entry against the hashed file, row indexing "
                    "cross-checked against the file's own L= labels, locked "
                    "forever by HIGHER_ORDER_FINGERPRINT"),
                "row_checks": ("bit-budget identity 48/48; within-block bit "
                               "monotonicity 48/48, which catches 86% of intra-row "
                               "transpositions where a sum checksum catches 0%"),
                "tia_corroborated_L": _T.TIA_CORROBORATED_L,
            },
            "unverified_assumptions": [
                "no off-air frame has ever been decoded",
                # The honest residual on the tables themselves.
                "for L=18..56 the higher-order allocation rests on mbelib alone; "
                "it is provably what mbelib says, but whether mbelib faithfully "
                "reflects TIA-102.BABA Annex G is corroborated only for "
                "L=9,10,11,12,13,15,16,17 (mbelib is known to contain at least "
                "one real error — the L=23 gain step typo)",
            ],
        },
        "validated_against": "synthetic vectors only — no off-air audio, ever",
        "caveat": (
            "The IMBE spectral amplitude dequantizer is implemented and this "
            "module now decodes a full envelope for ALL {tot} legal harmonic "
            "counts (L=9..56) — the former L=42/43/44 gap is closed. Both "
            "sourced tables have per-entry verification against a hash-pinned "
            "copy of mbelib, the Hamming(15,11) generator is now TIA-102.BAAA-A "
            "Table 5-4's rather than a locally-invented equivalent code, and "
            "the 88-bit field order is now TIA-102.BABA clause 7.1's priority "
            "scan rather than this module's own convention. What remains "
            "unproven is NOT the transcription: it is that NO OFF-AIR FRAME HAS "
            "EVER BEEN DECODED. Stages that are each right can still be wired "
            "together wrongly, so 'intelligible' means the dequantizer ran, not "
            "that a human heard words. Label any audio shown to an operator "
            "accordingly."
        ).format(tot=total_L) if not unverified else (
            "The IMBE spectral amplitude dequantizer is implemented and this "
            "module decodes a full envelope for {ok} of {tot} harmonic counts "
            "(L={bad} are unsupported and return no magnitudes; their pitch, "
            "voicing and gain still decode). The Hamming(15,11) generator and "
            "the 88-bit field order are now the published ones. NO OFF-AIR "
            "FRAME HAS EVER BEEN DECODED, so 'intelligible' means the "
            "dequantizer ran, not that a human heard words. Label any audio "
            "shown to an operator accordingly."
        ).format(ok=total_L - len(unverified), tot=total_L,
                 bad="/".join(str(x) for x in unverified)),
    }
