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
VERIFICATION — see tests/test_ambe.py, which re-runs all of this on every run
------------------------------------------------------------------------------
  * All 240 GAIN_BITS entries are confirmed by an independent check: the step
    size mbelib stores alongside each one must equal QUANT_STEP[bits] *
    GAIN_STD_DEV[j]. 239/240 match exactly. The single exception is a known
    mbelib typo at L=23, G5 (see GAIN_STEP_TYPO below).
  * The bit-budget identity sum(GAIN_BITS[L]) + sum(HIGHER_ORDER_BITS[L])
    == 73 - K(L) holds for all 45 tabulated values of L.
  * The standard's own worked example (L=16) is reproduced exactly, including
    the derived higher-order step sizes.

------------------------------------------------------------------------------
KNOWN GAP — L = 42, 43, 44
------------------------------------------------------------------------------
HIGHER_ORDER_BITS omits L = 42, 43 and 44. Those three rows could not be read
out of the source reliably: repeated reads disagreed with each other and every
candidate failed the bit-budget checksum. Rather than guess a row that sums
correctly, they are ABSENT, and survey/ambe.py refuses to decode those three
harmonic counts (returning magnitudes=None with a reason) instead of emitting
audio built on a fabricated allocation.

TO CLOSE IT: read hoba rows L=42..44 from a local checkout of
szechyjs/mbelib imbe7200x4400_const.h (or from a purchased copy of
TIA-102.BABA Annex G), add them below, and the guard in ambe.py lifts itself —
the checksum test will confirm them automatically.
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

# L values whose higher-order allocation could not be established — see the
# KNOWN GAP note above. ambe.py refuses to decode these.
UNVERIFIED_L = (42, 43, 44)

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
