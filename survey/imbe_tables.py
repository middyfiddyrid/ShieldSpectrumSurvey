"""IMBE 7200x4400 spectral amplitude constants — THE SOURCED DATA, ISOLATED.

Every number in this file is data that CANNOT be derived, together with where it
came from. Nothing here is inferred, remembered or reconstructed. Anything that
can be computed lives in survey/ambe.py instead, deliberately: block lengths, the
DCT pair, the prediction/warping arithmetic and all 240 gain-vector step sizes
are generated, not tabulated, which removes them from the transcription-risk
surface entirely.

------------------------------------------------------------------------------
ATTRIBUTION — REQUIRED IN ANY DISTRIBUTION OF THIS PRODUCT
------------------------------------------------------------------------------
GAIN_LEVELS, GAIN_BITS and HIGHER_ORDER_BITS are taken from mbelib
(https://github.com/szechyjs/mbelib), file imbe7200x4400_const.h, arrays
B2[64], ba[48][5][2] and hoba[48][50]. mbelib is ISC licensed — the licence text
is in a file named COPYRIGHT (which is why GitHub mislabels the repo
"NOASSERTION"). ISC is permissive and imposes no copyleft, so this may ship in a
closed-source commercial product PROVIDED the notice below travels with it:

    Copyright (C) 2010 mbelib Author

    Permission to use, copy, modify, and/or distribute this software for any
    purpose with or without fee is hereby granted, provided that the above
    copyright notice and this permission notice appear in all copies.

ACTION REQUIRED: add that notice to the product's third-party notices before
release. It is not satisfied by this comment alone.

DO NOT take these tables from arancormonk/mbelib-neo (the actively-maintained
fork — it was relicensed to GPL-2.0), from op25's imbe_vocoder (GPLv3+), from
jmbe (GPLv3), or from dsdcc (no licence declared). Any of those would force this
entire product's licensing. The ISC upstream is the only usable source.

The values are corroborated against TIA/EIA-102.BABA (the P25 IMBE Vocoder
Description) as documented in docs/IMBE_SPECTRAL_AMPLITUDES.md, but they are
TAKEN FROM mbelib under its express licence — not transcribed from the
standard — because the accessible copy of the standard is an unauthorised scan.

------------------------------------------------------------------------------
HOW THESE NUMBERS GOT HERE — the provenance that makes them trustworthy
------------------------------------------------------------------------------
Every integer below was obtained by DETERMINISTICALLY PARSING the upstream C
header, not by a model reading it. The file was downloaded verbatim, its hash
recorded (MBELIB_SHA256), and the arrays tokenized by regex. This matters
because the earlier attempt used a summarising fetch, which once returned a
wrong array with confident formatting — a failure mode that per-row checksums
alone could not catch.

The exact upstream artifact:
    file   : imbe7200x4400_const.h  (szechyjs/mbelib, master)
    size   : 41049 bytes
    sha256 : 62982451d7c23d3d6d255bf1dff34c4bdcf9c40e0aa04b7f5f40393705924737

Confirmed byte-for-byte identical from FOUR fetches across TWO independent
origins — GitHub (raw.githubusercontent.com, cdn.jsdelivr.net, and the
codeload.github.com repo tarball) and the Software Heritage archive, which
crawled and stored the content separately in 2023. Independent origins, so a
compromised or rewritten single host would have been caught.

------------------------------------------------------------------------------
VERIFICATION — see tests/test_ambe.py, which re-runs all of this on every run
------------------------------------------------------------------------------
PER-ENTRY, all 240 GAIN_BITS:
  * The step size mbelib stores alongside each bit count must equal
    QUANT_STEP[bits] * GAIN_STD_DEV[j]. Checked over the whole ba[48][5][2]
    array: 239/240 match exactly. The single exception is the known mbelib typo
    at L=23, G5 (see GAIN_STEP_TYPO below) — which we are immune to, because we
    generate the steps and take only the bit counts.

PER-ENTRY, all 1272 HIGHER_ORDER_BITS:
  * Parsed from the hashed file above, so each integer is verified against the
    actual bytes rather than against a reading of them.
  * Row indexing is cross-checked against the file's OWN `// L=NN` comments for
    all 48 rows, so a row read at the wrong offset is caught, not assumed.
  * mbelib's ImbeJi[48][6] block-length table reproduces our closed-form
    imbe_amplitude_blocks() for all 48 rows — an independent anchor confirming
    the file's rows really run L=9..56 in order.
  * Every row's padding beyond its first L-6 entries is all zeros (48/48),
    confirming L-6 is the correct slice length.
  * HIGHER_ORDER_FINGERPRINT locks all 1272 integers. The test recomputes it
    from the live table, so ANY later edit — including an intra-row swap, which
    is invisible to a sum checksum — fails immediately.

PER-ROW:
  * The bit-budget identity sum(GAIN_BITS[L]) + sum(HIGHER_ORDER_BITS[L])
    == 73 - K(L) now holds for ALL 48 values of L.
  * Within each DCT block, the allocated bits are non-increasing as the
    coefficient index k rises. Holds 48/48. This is the guard the bit-budget
    checksum cannot be: a transposition preserves the sum, so the checksum is
    blind to ordering by construction. Measured against every possible
    distinct-valued intra-row transposition, this invariant catches 86% of them
    (93% of adjacent swaps).
  * The standard's own worked example (L=16) is reproduced exactly, including
    the derived higher-order step sizes.

WHAT IS STILL NOT PROVEN — read before trusting the envelope
  * TRANSCRIPTION risk is closed: these values are provably what mbelib says.
  * SOURCE risk is NOT fully closed: whether mbelib itself faithfully reflects
    TIA-102.BABA Annex G is independently corroborated only for L = 9, 10, 11,
    12, 13, 15, 16, 17 (the sparse low-L pages the archive.org scan OCRs
    cleanly, per docs/IMBE_SPECTRAL_AMPLITUDES.md section 5.2) plus the L=16
    worked example. For L = 18..56 the allocation rests on mbelib alone,
    constrained but not pinned by the checksum and monotonicity invariants.
    This is not theoretical: the L=23 gain typo proves mbelib contains at least
    one real error. Buying ANSI/TIA-102.BABA-A (~$271) and re-deriving Annex G
    is what would close it.
  * The block-monotonicity invariant is VACUOUS for L = 9..12, where every DCT
    block holds at most one higher-order coefficient and there is no within-block
    ordering to violate. Those four rows lean on the TIA scan corroboration
    above instead — the two checks cover for each other.
"""

# ---- derivable step-size machinery (TIA Tables 3 and 4; see docs/) ----
# Quantizer step size as a multiple of the coefficient standard deviation,
# indexed by the number of bits. Index 0 is unused (a 0-bit quantizer has no
# step). Confirmed identical in TIA Table 3 and mbelib quantstep[].
QUANT_STEP = (None, 1.2, 0.85, 0.65, 0.40, 0.28, 0.15, 0.08, 0.04, 0.02, 0.01)

# Standard deviation of the k-th DCT coefficient within a block, k = 2..10.
# TIA Table 4 / mbelib standdev[]. The tail is NOT monotonic (k=8 is 0.165, then
# 0.170, 0.170). That is real and confirmed in both sources — do not "fix" it.
HO_STD_DEV = {2: 0.307, 3: 0.241, 4: 0.207, 5: 0.190, 6: 0.179,
              7: 0.173, 8: 0.165, 9: 0.170, 10: 0.170}

# Standard deviations of the five quantized gain-vector elements G_2..G_6.
# These are NOT printed in the standard and are NOT in mbelib; they were
# recovered by dividing mbelib's 240 stored step sizes by QUANT_STEP, and they
# reproduce all 240 exactly (bar the L=23 typo). Documented in
# docs/IMBE_SPECTRAL_AMPLITUDES.md section 3.2. This is why we generate the
# step sizes instead of tabulating 240 more floats.
GAIN_STD_DEV = (0.310, 0.201, 0.168, 0.145, 0.132)

# mbelib bug, diagnosed in docs/IMBE_SPECTRAL_AMPLITUDES.md section 3.2:
# ba[] at L=23, element G_5 stores step size 0.068000. The standard says
# 0.058000, and the derivation rule agrees (QUANT_STEP[4] * 0.145 = 0.058).
# Every other 4-bit G_5 row in mbelib is 0.058, so mbelib is self-inconsistent
# at this one row — a digit transposition. We take only the BIT COUNTS from
# mbelib and generate the steps, so the typo cannot reach us; this constant
# exists so the test suite can assert we are immune to it.
GAIN_STEP_TYPO = {"L": 23, "element": 5, "mbelib": 0.068000, "correct": 0.058000}

# The exact upstream artifact these tables were parsed from. Recorded so the
# provenance claim is checkable rather than asserted: re-download
# imbe7200x4400_const.h from szechyjs/mbelib and this is the hash you must get.
MBELIB_SHA256 = "62982451d7c23d3d6d255bf1dff34c4bdcf9c40e0aa04b7f5f40393705924737"
MBELIB_BYTES = 41049
MBELIB_ORIGINS = (
    "https://raw.githubusercontent.com/szechyjs/mbelib/master/imbe7200x4400_const.h",
    "https://cdn.jsdelivr.net/gh/szechyjs/mbelib@master/imbe7200x4400_const.h",
    "https://codeload.github.com/szechyjs/mbelib/tar.gz/refs/heads/master",
    "https://archive.softwareheritage.org/api/1/content/"
    "sha1_git:0b6c308f15e35d0f96a12c2d557187d3db34f23b/raw/",
)

# Fingerprints over the FULL tables, computed at parse time from the hashed file
# above. tests/test_ambe.py recomputes them from the live tables on every run,
# so any later edit — including an intra-row transposition, which no sum
# checksum can see — fails the suite. Serialization is defined in the test.
HIGHER_ORDER_FINGERPRINT = \
    "7a826c07e0cf71b56997ad5958f92c038416aa1c33c7008918326b10bddbe152"
GAIN_BITS_FINGERPRINT = \
    "4c7a8b76411840407f42d70ac3b2fc58829be55f7d8530c96c3ea3d44ce0000a"

# L values whose higher-order allocation could not be established. NOW EMPTY:
# the former gap at L = 42, 43, 44 was closed by parsing mbelib's hoba[] rows
# directly out of the hashed file (all three satisfy the bit-budget checksum,
# 14 + 47 == 61 == 73 - 12). The name is kept — ambe.py and the test suite
# branch on it — so that a future gap can be reinstated in one line.
UNVERIFIED_L = ()

# The L values whose higher-order allocation is independently corroborated
# against TIA-102.BABA Annex G, not just against mbelib. See the docstring:
# these are the sparse low-L pages the archive.org scan OCRs cleanly. Recorded
# because they are exactly the rows where the block-monotonicity invariant is
# vacuous, so the two forms of evidence complement each other.
TIA_CORROBORATED_L = (9, 10, 11, 12, 13, 15, 16, 17)

GAIN_LEVELS = (
    -2.842205, -2.694235, -2.558260, -2.382850, -2.221042, -2.095574,
    -1.980845, -1.836058, -1.645556, -1.417658, -1.261301, -1.125631,
    -0.958207, -0.781591, -0.555837, -0.346976, -0.147249, 0.027755,
    0.211495, 0.388380, 0.552873, 0.737223, 0.932197, 1.139032,
    1.320955, 1.483433, 1.648297, 1.801447, 1.942731, 2.118613,
    2.321486, 2.504443, 2.653909, 2.780654, 2.925355, 3.076390,
    3.220825, 3.402869, 3.585096, 3.784606, 3.955521, 4.155636,
    4.314009, 4.444150, 4.577542, 4.735552, 4.909493, 5.085264,
    5.254767, 5.411894, 5.568094, 5.738523, 5.919215, 6.087701,
    6.280685, 6.464201, 6.647736, 6.834672, 7.022583, 7.211777,
    7.471016, 7.738948, 8.124863, 8.695827,
)

GAIN_BITS = {
    9: (10, 9, 9, 9, 9),
    10: (9, 9, 8, 8, 8),
    11: (8, 8, 8, 7, 7),
    12: (8, 7, 7, 7, 7),
    13: (7, 7, 7, 6, 6),
    14: (7, 6, 6, 6, 6),
    15: (7, 6, 6, 6, 5),
    16: (6, 6, 6, 5, 5),
    17: (6, 6, 5, 5, 5),
    18: (6, 5, 5, 5, 5),
    19: (6, 5, 5, 4, 4),
    20: (6, 5, 5, 4, 4),
    21: (5, 5, 5, 4, 4),
    22: (5, 5, 4, 4, 4),
    23: (5, 4, 4, 4, 4),
    24: (5, 4, 4, 4, 4),
    25: (5, 4, 4, 4, 3),
    26: (5, 4, 4, 3, 3),
    27: (5, 4, 4, 3, 3),
    28: (4, 4, 4, 3, 3),
    29: (4, 4, 4, 3, 3),
    30: (4, 4, 4, 3, 3),
    31: (4, 4, 3, 3, 3),
    32: (4, 4, 3, 3, 3),
    33: (4, 3, 3, 3, 3),
    34: (4, 3, 3, 3, 3),
    35: (4, 3, 3, 3, 3),
    36: (4, 3, 3, 3, 3),
    37: (4, 3, 3, 3, 2),
    38: (4, 3, 3, 3, 2),
    39: (4, 3, 3, 3, 2),
    40: (4, 3, 3, 3, 2),
    41: (4, 3, 3, 2, 2),
    42: (4, 3, 3, 2, 2),
    43: (4, 3, 3, 2, 2),
    44: (4, 3, 3, 2, 2),
    45: (4, 3, 3, 2, 2),
    46: (3, 3, 3, 2, 2),
    47: (3, 3, 3, 2, 2),
    48: (3, 3, 3, 2, 2),
    49: (3, 3, 3, 2, 2),
    50: (3, 3, 3, 2, 2),
    51: (3, 3, 3, 2, 2),
    52: (3, 3, 2, 2, 2),
    53: (3, 3, 2, 2, 2),
    54: (3, 3, 2, 2, 2),
    55: (3, 3, 2, 2, 2),
    56: (3, 3, 2, 2, 2),
}

HIGHER_ORDER_BITS = {
    9: (
        9, 8, 7,
    ),
    10: (
        9, 7, 6, 5,
    ),
    11: (
        9, 7, 6, 5, 4,
    ),
    12: (
        8, 7, 6, 5, 4, 3,
    ),
    13: (
        7, 7, 6, 5, 4, 3, 3,
    ),
    14: (
        7, 7, 5, 4, 4, 3, 4, 3,
    ),
    15: (
        6, 7, 5, 4, 4, 3, 3, 3, 3,
    ),
    16: (
        6, 6, 5, 4, 4, 3, 3, 3, 3, 2,
    ),
    17: (
        5, 5, 5, 4, 4, 4, 3, 3, 2, 3, 2,
    ),
    18: (
        5, 4, 5, 5, 4, 3, 3, 3, 3, 2, 2, 2,
    ),
    19: (
        5, 4, 5, 4, 4, 3, 3, 3, 3, 2, 3, 2, 1,
    ),
    20: (
        5, 4, 5, 4, 4, 3, 3, 2, 3, 2, 1, 3, 2, 1,
    ),
    21: (
        4, 4, 5, 4, 4, 3, 3, 2, 2, 3, 2, 1, 3, 2, 1,
    ),
    22: (
        4, 4, 4, 4, 4, 3, 2, 3, 2, 2, 3, 2, 1, 2, 2, 1,
    ),
    23: (
        4, 3, 4, 4, 3, 4, 3, 2, 3, 2, 2, 2, 2, 1, 2, 2, 1,
    ),
    24: (
        4, 3, 3, 4, 3, 3, 3, 3, 2, 3, 2, 1, 2, 2, 1, 2, 2, 1,
    ),
    25: (
        4, 3, 3, 4, 3, 3, 3, 3, 2, 3, 2, 1, 2, 2, 1, 2, 1, 1,
        1,
    ),
    26: (
        4, 3, 3, 4, 3, 3, 3, 2, 2, 3, 2, 1, 2, 2, 1, 1, 2, 2,
        1, 1,
    ),
    27: (
        4, 3, 2, 4, 3, 2, 3, 2, 2, 3, 2, 2, 1, 2, 2, 1, 1, 2,
        2, 1, 1,
    ),
    28: (
        4, 3, 2, 4, 3, 2, 3, 2, 2, 2, 3, 2, 1, 1, 2, 2, 1, 1,
        2, 1, 1, 1,
    ),
    29: (
        3, 3, 2, 4, 3, 2, 2, 3, 2, 2, 2, 3, 2, 1, 1, 2, 1, 1,
        1, 2, 1, 1, 1,
    ),
    30: (
        3, 3, 2, 2, 3, 3, 2, 2, 3, 2, 2, 1, 3, 2, 1, 1, 2, 1,
        1, 1, 2, 1, 1, 1,
    ),
    31: (
        3, 3, 2, 2, 3, 3, 2, 2, 3, 2, 2, 1, 2, 2, 1, 1, 2, 1,
        1, 1, 2, 1, 1, 1, 1,
    ),
    32: (
        3, 3, 2, 2, 3, 3, 2, 2, 3, 2, 2, 1, 2, 2, 1, 1, 2, 1,
        1, 1, 1, 2, 1, 1, 1, 0,
    ),
    33: (
        3, 3, 2, 2, 3, 3, 2, 2, 3, 2, 1, 1, 2, 2, 1, 1, 1, 2,
        1, 1, 1, 1, 2, 1, 1, 1, 1,
    ),
    34: (
        3, 2, 2, 2, 3, 2, 2, 2, 3, 2, 2, 1, 1, 2, 2, 1, 1, 1,
        2, 1, 1, 1, 1, 2, 1, 1, 1, 0,
    ),
    35: (
        3, 2, 2, 2, 3, 2, 2, 2, 2, 3, 2, 1, 1, 1, 2, 2, 1, 1,
        1, 2, 1, 1, 1, 0, 2, 1, 1, 1, 0,
    ),
    36: (
        3, 2, 2, 2, 1, 3, 2, 2, 2, 1, 3, 2, 1, 1, 1, 2, 2, 1,
        1, 1, 2, 1, 1, 1, 0, 2, 1, 1, 1, 0,
    ),
    37: (
        3, 2, 2, 2, 1, 3, 2, 2, 2, 2, 3, 2, 1, 1, 1, 2, 1, 1,
        1, 1, 2, 1, 1, 1, 0, 2, 1, 1, 1, 1, 0,
    ),
    38: (
        3, 2, 2, 2, 1, 3, 2, 2, 2, 1, 3, 2, 1, 1, 1, 2, 1, 1,
        1, 1, 2, 1, 1, 1, 1, 0, 2, 1, 1, 1, 1, 0,
    ),
    39: (
        3, 2, 2, 2, 1, 3, 2, 2, 2, 1, 3, 2, 1, 1, 1, 2, 2, 1,
        1, 1, 0, 2, 1, 1, 1, 1, 0, 2, 1, 1, 1, 0, 0,
    ),
    40: (
        3, 2, 2, 2, 1, 3, 2, 2, 1, 1, 3, 2, 1, 1, 1, 1, 2, 2,
        1, 1, 1, 0, 2, 1, 1, 1, 1, 0, 2, 1, 1, 1, 0, 0,
    ),
    41: (
        3, 2, 2, 1, 1, 3, 2, 2, 2, 1, 1, 3, 2, 1, 1, 1, 1, 2,
        2, 1, 1, 1, 0, 2, 1, 1, 1, 1, 0, 2, 1, 1, 1, 0, 0,
    ),
    # L = 42, 43, 44 — the rows that were absent until they could be parsed
    # deterministically out of mbelib's hoba[] (see the provenance block above).
    # Each satisfies the bit-budget checksum: 14 gain bits + 47 higher-order
    # bits == 61 == 73 - K, with K = 12 for all three.
    42: (
        3, 2, 2, 2, 1, 1, 3, 2, 2, 2, 1, 1, 2, 2, 1, 1, 1, 1,
        2, 2, 1, 1, 1, 0, 2, 1, 1, 1, 0, 0, 2, 1, 1, 1, 0, 0,
    ),
    43: (
        3, 2, 2, 2, 1, 1, 3, 2, 2, 2, 1, 1, 2, 2, 1, 1, 1, 1,
        2, 1, 1, 1, 1, 0, 2, 1, 1, 1, 0, 0, 2, 1, 1, 1, 1, 0,
        0,
    ),
    44: (
        3, 2, 2, 1, 1, 1, 3, 2, 2, 2, 1, 1, 2, 2, 1, 1, 1, 1,
        2, 1, 1, 1, 1, 0, 2, 1, 1, 1, 1, 0, 0, 2, 1, 1, 1, 1,
        0, 0,
    ),
    45: (
        3, 2, 2, 1, 1, 1, 3, 2, 2, 1, 1, 1, 2, 2, 1, 1, 1, 1,
        2, 2, 1, 1, 1, 0, 0, 2, 1, 1, 1, 1, 0, 0, 2, 1, 1, 1,
        1, 0, 0,
    ),
    46: (
        3, 2, 2, 1, 1, 1, 3, 2, 2, 1, 1, 1, 2, 2, 1, 1, 1, 1,
        1, 2, 2, 1, 1, 1, 0, 0, 2, 1, 1, 1, 1, 0, 0, 2, 1, 1,
        1, 1, 0, 0,
    ),
    47: (
        3, 2, 2, 1, 1, 1, 3, 2, 2, 1, 1, 1, 1, 2, 2, 1, 1, 1,
        1, 1, 2, 2, 1, 1, 1, 0, 0, 2, 1, 1, 1, 1, 0, 0, 2, 1,
        1, 1, 0, 0, 0,
    ),
    48: (
        3, 2, 2, 1, 1, 1, 1, 3, 2, 2, 1, 1, 1, 1, 2, 2, 1, 1,
        1, 1, 1, 2, 2, 1, 1, 1, 0, 0, 2, 1, 1, 1, 0, 0, 0, 2,
        1, 1, 1, 0, 0, 0,
    ),
    49: (
        3, 2, 2, 1, 1, 1, 1, 3, 2, 2, 1, 1, 1, 1, 2, 2, 1, 1,
        1, 1, 0, 2, 2, 1, 1, 1, 0, 0, 2, 1, 1, 1, 0, 0, 0, 2,
        1, 1, 1, 1, 0, 0, 0,
    ),
    50: (
        3, 2, 2, 1, 1, 1, 1, 3, 2, 2, 1, 1, 1, 1, 2, 2, 1, 1,
        1, 1, 0, 2, 2, 1, 1, 1, 0, 0, 2, 1, 1, 1, 1, 0, 0, 0,
        2, 1, 1, 1, 0, 0, 0, 0,
    ),
    51: (
        3, 2, 2, 1, 1, 1, 1, 3, 2, 1, 1, 1, 1, 1, 2, 2, 1, 1,
        1, 1, 0, 2, 2, 1, 1, 1, 0, 0, 0, 2, 1, 1, 1, 1, 0, 0,
        0, 2, 1, 1, 1, 1, 0, 0, 0,
    ),
    52: (
        3, 2, 1, 1, 1, 1, 1, 3, 2, 2, 1, 1, 1, 1, 2, 2, 1, 1,
        1, 1, 1, 0, 2, 2, 1, 1, 1, 0, 0, 0, 2, 1, 1, 1, 1, 0,
        0, 0, 2, 1, 1, 1, 1, 0, 0, 0,
    ),
    53: (
        3, 2, 1, 1, 1, 1, 1, 3, 2, 2, 1, 1, 1, 1, 1, 2, 2, 1,
        1, 1, 1, 1, 0, 2, 2, 1, 1, 1, 0, 0, 0, 2, 1, 1, 1, 1,
        0, 0, 0, 2, 1, 1, 1, 0, 0, 0, 0,
    ),
    54: (
        3, 2, 2, 1, 1, 1, 1, 0, 3, 2, 2, 1, 1, 1, 1, 0, 2, 2,
        1, 1, 1, 1, 1, 0, 2, 2, 1, 1, 1, 0, 0, 0, 2, 1, 1, 1,
        1, 0, 0, 0, 2, 1, 1, 1, 0, 0, 0, 0,
    ),
    55: (
        3, 2, 2, 1, 1, 1, 1, 0, 3, 2, 2, 1, 1, 1, 1, 0, 2, 2,
        1, 1, 1, 1, 1, 0, 2, 2, 1, 1, 1, 0, 0, 0, 2, 1, 1, 1,
        0, 0, 0, 0, 2, 1, 1, 1, 1, 0, 0, 0, 0,
    ),
    56: (
        3, 2, 2, 1, 1, 1, 1, 0, 3, 2, 2, 1, 1, 1, 1, 0, 2, 2,
        1, 1, 1, 1, 1, 0, 2, 2, 1, 1, 1, 0, 0, 0, 2, 1, 1, 1,
        1, 0, 0, 0, 0, 2, 1, 1, 1, 0, 0, 0, 0, 0,
    ),
}
