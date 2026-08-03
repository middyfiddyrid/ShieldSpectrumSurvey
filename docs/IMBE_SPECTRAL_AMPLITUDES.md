# IMBE 7200x4400 (P25 Phase 1) — Spectral Amplitude Dequantizer

Research findings for implementing the missing spectral amplitude decode stage.
Compiled 2026-08-03. **Research document only — no code was changed to produce it.**

**Bottom line: YES, this is implementable from freely-usable, citable sources.**
No purchase of the TIA standard is strictly required, and no GPL code needs to be
touched. See [§7 Recommendation](#7-recommendation).

---

## 1. Scope and status of each claim

Two independent sources were used and cross-checked against each other:

| ID | Source | Type | How obtained |
|----|--------|------|--------------|
| **[TIA]** | TIA/EIA-102.BABA, *APCO Project 25 Vocoder Description* (the original full-rate IMBE 7200/4400 document) | Primary standard | Scanned copy + OCR at `https://archive.org/details/TIA-102_Series_Documents/TIA-102.BABA_Project_25_IMBE_Vocoder_Description` (see [§6.4](#64-is-the-archiveorg-copy-usable) for the copyright caveat) |
| **[MBE]** | `szechyjs/mbelib`, files `imbe7200x4400.c`, `imbe7200x4400_const.h`, `mbelib.c` | Independent implementation, **ISC licensed** | `https://github.com/szechyjs/mbelib` |

Throughout this document:

- **VERIFIED** = confirmed in **both** [TIA] and [MBE], byte-for-byte or equation-for-equation.
- **VERIFIED [TIA]** = read directly off the standard, not independently corroborated.
- **DERIVED** = my inference, with the evidence for it stated. Not printed as such in any source.
- **UNRESOLVED** = could not establish. Listed in [§5](#5-what-i-could-not-establish).

Equation numbers `(nn)` are TIA/EIA-102.BABA's own numbering.

---

## 2. Decoder algorithm, step by step

All of this is the **decoder** path (clause 6.4 of [TIA]), which is what the tool needs.

### 2.0 Prerequisites from earlier decode stages

**VERIFIED** — [TIA] eqs. (46), (47), (48):

```
w0 = 4*pi / (b0 + 39.5)                    (46)   fundamental frequency, b0 = 8-bit index
L  = floor( 0.9254 * floor(pi/w0 + 0.25) ) (47)   number of harmonics
K  = floor((L + 2)/3)   if L < 36                 number of V/UV decisions
   = 12                 otherwise         (48)
```

Valid range is `9 <= L <= 56` (48 distinct values). `b0` is restricted to `0..207`
by the pitch estimator; the remaining 48 codes of the 8-bit field are reserved.
**VERIFIED** — [TIA] clause 6.1; [MBE] `imbe7200x4400.c` lines 211–218.

**Bit budget — DERIVED, and machine-checked for all 48 values of L:**

```
bits available for b3..b(L+1)  =  73 - K
```

Derivation: the 88-bit frame is `b0` (8) + `b1` (K) + `b2` (6, gain) +
`b3..b(L+1)` + `b(L+2)` (1, sync) = 88. I verified `sum(gain_bits) + sum(higher_order_bits)
== 73 - K` holds for **all 48 values of L** against [MBE]'s allocation tables, and it
reproduces the "67–76 bits to quantize the spectral magnitudes" range stated in
[TIA] Table 1. Use this as an assertion in the implementation — it catches table
indexing errors immediately.

### 2.1 Block lengths J_i — fully algorithmic, no table needed

The L residuals are divided into **6 blocks**. [TIA] eqs. (58)/(59) and (65)/(66)
constrain them as `sum(J_i) = L` and `floor(L/6) <= J_i <= J_(i+1) <= ceil(L/6)`.
That is uniquely satisfied by:

```python
r = L % 6
J = [L // 6] * (6 - r) + [L // 6 + 1] * r     # J[0] = block 1 (lowest frequency)
```

**VERIFIED.** [TIA] Annex J tabulates this for all L; [MBE] stores it as `ImbeJi[48][6]`.
I checked the closed form above against [MBE]'s table for **all 48 values of L — exact match,
zero exceptions**. Annex J and `ImbeJi` are therefore redundant; do not transcribe them.

Sanity check: `L = 34 -> [5,5,6,6,6,6]`, which is [TIA] Figure 17 ("Prediction Residual
Blocks for L = 34"). `L = 9 -> [1,1,1,2,2,2]`. `L = 36 -> [6,6,6,6,6,6]`.

Note blocks with `J_i = 1` have **no** higher-order coefficients.

### 2.2 Decode the gain vector (clause 6.4.1)

1. `b2` (6 bits) indexes directly into a **64-entry non-uniform quantizer table**
   ([TIA] Annex E) to give `G_1`. See [§3.3](#33-annex-e--64-entry-gain-quantizer-table).
2. `b3..b7` reconstruct `G_2..G_6` — **VERIFIED**, [TIA] eq. (68):

   ```
   G_(m-1) = 0                                    if B_m == 0
           = A_m * (b_m - 2^(B_m - 1) + 0.5)      otherwise      for 3 <= m <= 7
   ```

   `B_m` (bit count) and `A_m` (step size) come from [TIA] Annex F as a function of L.
   See [§3.2](#32-annex-f--gain-vector-step-sizes-are-derivable-bit-counts-are-not).
   `b_m` is read as an **unsigned** integer, MSB first.
3. 6-point inverse DCT to recover the gain vector `R_i` (also written `C_(i,1)`) —
   **VERIFIED**, [TIA] eqs. (69)/(70), identical in [MBE] `imbe7200x4400.c`:

   ```
   R_i = sum over m=1..6 of  a(m) * G_m * cos( pi * (m-1) * (i - 0.5) / 6 )
   a(m) = 1 if m == 1 else 2
   ```

   Note the **non-standard normalisation**: `a(m)` is 1/2, not the usual `sqrt` scaling,
   and there is no leading `1/N`. Use exactly this form.

### 2.3 Decode higher-order DCT coefficients (clause 6.4.2)

`b8 .. b(L+1)` map to `[C_1,2 .. C_1,J1, C_2,2 .. C_2,J2, ..., C_6,2 .. C_6,J6]`
in that order — i.e. block-major, ascending k within each block, skipping any block
with `J_i = 1`. **VERIFIED** — [TIA] clause 6.4.2; [MBE] increments a single running
index `m` starting at 8 over exactly this traversal.

The closed form for the index is:

```
m = 6 + k + sum over n=1..i-1 of ( J_n - 1 )        for 2 <= k <= J_i
```

> **Correction — do not copy eq. (72) as the scan renders it.** The OCR of [TIA]
> eqs. (64) and (72) reads `m = 6 + k + sum(J_n)`, i.e. the `-1` inside the summand is
> lost. That form is wrong: it maps `C_2,2 -> m=10` for `L=16`, leaving `m=9`
> unassigned. The standard's **own worked example** (clause 10, Table 7) settles it —
> it lists `b9 -> C_2,2` and `b10 -> C_3,2`, which only the `(J_n - 1)` form produces.
> [MBE]'s sequential traversal agrees. **DERIVED**, but with the standard's own
> validation table as the arbiter, so confidence is high. Simplest safe implementation:
> don't use the closed form at all — just walk the blocks with a running counter.

```
C_(i,k) = 0                                    if B_m == 0
        = A_m * (b_m - 2^(B_m - 1) + 0.5)      otherwise      for 8 <= m <= L+1
```

**The step size is fully derivable** — this is the single most useful finding
(see [§3.1](#31-step-size-rule--the-key-simplification)):

```
A_m = quantstep[B_m] * standdev[k]
```

### 2.4 Inverse DCT per block, then concatenate

**VERIFIED** — [TIA] eqs. (73)/(74), identical in [MBE]:

```
c_(i,j) = sum over k=1..J_i of  a(k) * C_(i,k) * cos( pi * (k-1) * (j - 0.5) / J_i )
a(k) = 1 if k == 1 else 2
```

Concatenate `c_(1,1..J1), c_(2,1..J2), ..., c_(6,1..J6)` into `T_l` for `1 <= l <= L`.
`T_l` is the reconstructed **log2 prediction residual**.

### 2.5 Inter-frame prediction and the frequency warping rule

This is the part most likely to be got wrong, so it is spelled out.

**Prediction coefficient — VERIFIED**, [TIA] eq. (55):

```
rho = 0.4                  if L(0) <= 15
    = 0.03*L(0) - 0.05     if 15 <  L(0) <= 24
    = 0.7                  otherwise
```

The scan's OCR renders the boundaries ambiguously (`<` vs `<=`), but **it does not
matter**: the piecewise function is continuous at both breakpoints
(`0.03*15 - 0.05 = 0.40` and `0.03*25 - 0.05 = 0.70`), so any consistent reading of
the inequalities gives identical values. [MBE] uses `<= 15` / `<= 24`. **DERIVED**
(the continuity argument), but it removes the ambiguity entirely.

**Frequency warping — VERIFIED**, [TIA] eqs. (75)/(76), corroborated by [MBE]:

The warping is driven by the ratio of **harmonic counts**, not by the ratio of
fundamental frequencies:

```
f_l   = l * L(-1) / L(0)          # real-valued
k_l   = floor(f_l)                (75)
d_l   = f_l - k_l                 (76)
```

> **Confidence note.** The OCR of eq. (75) in the scan is degraded — the fraction
> renders as `H- 1 )` over `m`. The surrounding paragraph explicitly establishes the
> `L(0)` / `L(-1)` notation immediately before the equation, and [MBE] independently
> implements `flokl[l] = (prev_mp->L / cur_mp->L) * l`. Two sources agree, so I am
> treating this as VERIFIED, but it is the one place where I am relying on an
> inference from a poor scan plus one implementation rather than on clean primary text.
> If a clean copy of the standard is ever obtained, re-check eq. (75) first.

**Reconstruction — VERIFIED**, [TIA] eq. (77):

```
mean_term = (rho / L(0)) * sum over l=1..L(0) of
                [ (1 - d_l)*log2M_(k_l)(-1) + d_l*log2M_(k_l + 1)(-1) ]

log2M_l(0) = T_l
           + rho * (1 - d_l) * log2M_(k_l)(-1)
           + rho * d_l       * log2M_(k_l + 1)(-1)
           - mean_term

M_l(0) = 2 ** log2M_l(0)
```

Note `mean_term` is a **single scalar** computed over the whole frame and subtracted
from every `l` — it is not per-harmonic. Getting this wrong produces a constant
spectral tilt error, which is exactly the sort of thing that yields unintelligible
output while looking superficially plausible.

**Boundary conditions — VERIFIED**, [TIA] eqs. (78)/(79) and the initialisation note:

```
M_0(-1)  = 1.0                                  (78)   index 0 must exist
M_l(-1)  = M_(L(-1))(-1)   for l > L(-1)        (79)   hold the last value
on init: M_l(-1) = 1.0 for all l, and L(-1) = 30
```

Eq. (79) matters whenever `L` grows between frames — you must extend the previous
frame's array by repeating its final value before indexing `k_l + 1`.

**Critical: the array carried into the next frame must be the UNENHANCED
`log2M_l(0)`**, not the enhanced amplitudes from §2.6. [TIA] clause 8 states this
explicitly ("The unenhanced spectral amplitudes are required by future frames in the
computation of Equation (77)"). [MBE] keeps `prev_mp` and `prev_mp_enhanced` as two
separate structures for exactly this reason. Conflating them causes slow drift and
progressive loss of intelligibility over a few seconds of speech — a classic
symptom worth checking for if output is "almost" right.

### 2.6 Spectral amplitude enhancement (clause 8) — required for intelligibility

This is a separate stage but it is the "gain/scaling constants applied on
reconstruction" and speech is noticeably muffled without it.

**VERIFIED** — [TIA] eqs. (105)–(110), matching [MBE] `mbelib.c: mbe_spectralAmpEnhance`:

```
RM0 = sum over l=1..L of  M_l^2
RM1 = sum over l=1..L of  M_l^2 * cos(w0 * l)                     (105),(106)

W_l = sqrt(M_l) * [ ( 0.96*pi * (RM0^2 + RM1^2 - 2*RM0*RM1*cos(w0*l)) )
                    / ( w0 * RM0 * (RM0^2 - RM1^2) ) ] ** 0.25    (107)

M_l := M_l                if 8*l <= L      # lowest harmonics left alone
     := 1.2 * M_l         else if W_l > 1.2
     := 0.5 * M_l         else if W_l < 0.5
     := W_l * M_l         otherwise        (108)

gamma = sqrt( RM0 / sum(M_l^2 after the above) )                  (109)
M_l  := gamma * M_l                                               (110)
```

Constants: `0.96*pi`, exponent `0.25`, clamps `1.2` and `0.5`, and the `8*l <= L`
guard. All **VERIFIED** in both sources. [MBE] guards `sum == 0 -> gamma = 1.0`;
that guard is an implementation detail, not in the standard, but is worth keeping.

**Energy tracker — VERIFIED**, [TIA] eq. (111). Needed only if adaptive smoothing
(clause 9) is implemented:

```
SE(0) = max( 0.95*SE(-1) + 0.05*RM0 , 10000.0 )
```

---

## 3. The constants

### 3.1 Step size rule — the key simplification

**VERIFIED.** [TIA] Table 3 gives the uniform quantizer step size as a multiple of the
coefficient's long-term standard deviation, and [TIA] Table 4 gives those standard
deviations. Both are short tables and both were confirmed identical in [MBE]
(`quantstep[]`, `standdev[]`):

`quantstep` — step size as a multiple of sigma, indexed by number of bits 1..10:

| bits | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 |
|---|---|---|---|---|---|---|---|---|---|---|
| factor | 1.2 | 0.85 | 0.65 | 0.40 | 0.28 | 0.15 | 0.08 | 0.04 | 0.02 | 0.01 |

`standdev` — sigma of the k-th DCT coefficient within a block, for k = 2..10:

| k | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 |
|---|---|---|---|---|---|---|---|---|---|
| sigma | 0.307 | 0.241 | 0.207 | 0.190 | 0.179 | 0.173 | 0.165 | 0.170 | 0.170 |

Note sigma is **not** monotonic at the tail (`k=8: 0.165`, then `0.170, 0.170`). That
looked like an OCR artifact, so I checked: [MBE] has exactly the same non-monotonic
tail. It is real. Do not "correct" it.

Sigma depends **only on k** (position within the block), not on which block. So:

```
A_m = quantstep[B_m] * standdev[k]
```

Worked example from [TIA] clause 6.3.2 itself: 4 bits on the third DCT coefficient of
any block -> `0.40 * 0.241 = 0.0964`. Confirmed.

### 3.2 Annex F — gain vector step sizes are DERIVABLE, bit counts are not

[TIA] Annex F is printed as an explicit `(bits, step size)` pair for each of
`G_2..G_6` at each of the 48 values of L — 240 rows.

**DERIVED (and this is a genuinely useful result):** the step-size column is *not*
independent data. It is the same rule as §3.1 with a per-element sigma:

```
sigma_gain = [0.310, 0.201, 0.168, 0.145, 0.132]     # for G_2, G_3, G_4, G_5, G_6
A_m = quantstep[B_m] * sigma_gain[m - 2]
```

Evidence: this reproduces **all 240** Annex F step sizes exactly (to the 6 decimal
places printed). These five sigma values are *not* printed anywhere in [TIA] or
present in [MBE] — [MBE] stores the 240 expanded pairs as `ba[48][5][2]`. I recovered
them by dividing the printed step sizes by `quantstep[]`.

Consequence: **only the 240 bit-count integers are irreducible data.** The 240
floats can be generated.

> **Bug found in mbelib — worth reporting upstream.**
> `ba[48][5][2]` at `L = 23`, element `G_5` has step size **0.068**. The standard says
> **0.058**. I confirmed this directly against the page image coordinates of the scan
> (Annex F page, row: `23 | G | 5 | b6 | 4 | 0.058000`), and the derivation rule
> independently gives `quantstep[4] * 0.145 = 0.40 * 0.145 = 0.058`. Every other
> 4-bit `G_5` entry in mbelib (L = 19, 20, 21, 22, 24, 25) is 0.058 — so mbelib is
> also internally inconsistent at this one row. It is a digit transposition.
> **Impact:** 17% oversized quantizer step on one gain element, for frames with
> L = 23 only. Anyone copying mbelib's table inherits it. **Use 0.058.**
> This was the *only* discrepancy found across 240 step sizes.

### 3.3 Annex E — 64-entry gain quantizer table

`b2` (6 bits) indexes a 64-entry non-uniform quantizer for `G_1`, spanning roughly
-2.842205 to +8.695827.

**VERIFIED — all 64 values match exactly between [TIA] Annex E and [MBE] `B2[64]`,
zero mismatches.** This is irreducible data: no generating rule was found, and the
spacing is non-uniform and clearly the output of a trained/optimised quantizer.

Not reproduced here — see [§6.4](#64-is-the-archiveorg-copy-usable). Take it from
[MBE] `imbe7200x4400_const.h`, `B2[64]`, which is ISC-licensed and now
independently confirmed correct against the standard.

### 3.4 Bit allocation — this IS a lookup table, not an algorithm

**This was the specific question asked, so: it is a table.** Findings:

- [TIA] presents the allocation as two lookup annexes — Annex F (bit counts for
  `G_2..G_6`, 48 x 5 = 240 integers) and Annex G (bit counts for the higher-order
  coefficients, 48 rows of `L-6` entries = **1272 integers**). No generating
  procedure is given in the standard. **VERIFIED [TIA].**
- [MBE] likewise stores them as data: `ba[48][5][2]` and `hoba[48][50]`.
- op25's `imbe_vocoder` likewise: `bit_allocation_tbl[]` and
  `bit_allocation_offset_tbl[]` declared in `tbls.h`. Every independent implementation
  uses a table.
- The DVSI patent family describes only the *principle* — allocation "according to
  their relative long-term variances" and weighted to favour perceptually more
  important low-frequency blocks (WO1992010830A1 / US5226084). That is not
  reproducible to exact integers.
- **I tested and rejected the obvious algorithmic hypothesis.** If allocation were
  plain reverse water-filling (greedy: give the next bit to whichever quantizer
  currently has the largest step size `quantstep[b] * sigma`), the resulting step
  sizes would be roughly equalised across coefficients. They are not: for `L = 16` the
  realised step sizes run monotonically from 0.0252 up to 0.20485, a factor of 8.
  Also, three coefficients with *identical* sigma get 9, 8 and 7 bits at `L = 9`. The
  allocation carries a deliberate low-frequency perceptual weighting that is not
  published. **Do not try to generate this table — take the values.**

**Conclusion: 1512 integers (240 + 1272) are irreducible and must be obtained, not
computed.** They are available under ISC from [MBE] (`ba`, `hoba`). Everything else in
the dequantizer is derivable from the ~25 constants given above.

### 3.5 Summary — what is data vs. what is code

| Item | Count | Status |
|---|---|---|
| Block lengths `J_i` | — | **Algorithmic**, closed form, verified for all 48 L |
| `quantstep[1..10]` | 10 floats | Reproduced above, verified in 2 sources |
| `standdev[k=2..10]` | 9 floats | Reproduced above, verified in 2 sources |
| `sigma_gain[G2..G6]` | 5 floats | **Derived above**, reproduces all 240 Annex F steps |
| Annex F step sizes | 240 floats | **Generated** from `sigma_gain` + `quantstep` |
| Annex E gain quantizer | 64 floats | **Irreducible.** Take from [MBE] `B2[64]` (ISC) |
| Annex F bit counts | 240 ints | **Irreducible.** Take from [MBE] `ba` (ISC) |
| Annex G bit counts | 1272 ints | **Irreducible.** Take from [MBE] `hoba` (ISC) |
| Enhancement constants | ~6 | Reproduced above, verified in 2 sources |
| Prediction / warping | — | Algorithmic, given above |

---

## 4. Validation vectors

Use these to prove the table indexing is right before debugging audio.

**From [TIA]'s own worked example (clause 10, Tables 6 and 7), for `L = 16`.**
This example is in the standard body, and I confirmed it agrees with [MBE]'s tables:

```
L = 16  ->  K = 6,  budget = 73 - 6 = 67 bits
J = [2, 2, 3, 3, 3, 3]

gain vector bit counts  B(G2..G6) = [6, 6, 6, 5, 5]                      (28 bits)
gain vector step sizes  A(G2..G6) = [0.04650, 0.03015, 0.02520, 0.04060, 0.03696]

higher-order bit counts b8..b17   = [6, 6, 5, 4, 4, 3, 3, 3, 3, 2]       (39 bits)
higher-order mapping    b8..b17  -> C1,2  C2,2  C3,2  C3,3  C4,2
                                    C4,3  C5,2  C5,3  C6,2  C6,3
higher-order step sizes           = [0.04605, 0.04605, 0.08596, 0.09640, 0.12280,
                                     0.15665, 0.19955, 0.15665, 0.19955, 0.20485]

28 + 39 = 67  ✓
```

Every step size above is `quantstep[bits] * standdev[k]` — e.g. `b13 -> C4,3`,
3 bits, k=3: `0.65 * 0.241 = 0.15665`. Confirms both the rule and the m->(i,k) mapping.

**Other spot values, verified in both sources:**

```
L = 9  : J = [1,1,1,2,2,2], K = 3, budget = 70
         gain bits [10,9,9,9,9] = 46,  higher-order b8..b10 = [9,8,7] = 24
L = 34 : J = [5,5,6,6,6,6]        (this is TIA Figure 17)
L = 36 : J = [6,6,6,6,6,6]
L = 56 : J = [9,9,9,9,10,10], K = 12, budget = 61
```

**Global assertion to build into the code:**

```
sum(gain_bits[L]) + sum(higher_order_bits[L]) == 73 - K(L)     for all L in 9..56
```

I machine-checked this against [MBE]'s tables for all 48 values of L — it holds with
no exceptions. It is a strong, cheap guard against off-by-one table indexing.

---

## 5. What I could NOT establish

Stated plainly, since the point of this exercise was to stop guessing.

1. **The generating rule behind the bit allocation tables.** Confirmed absent from
   [TIA], absent from every implementation examined, and only described qualitatively
   in the DVSI patents. I actively tested the standard reverse-water-filling
   hypothesis and disproved it (§3.4). The 1512 integers must be taken as data.
   *This is a finding, not a gap — no further searching will help.*
2. **A clean machine-readable transcription of Annex G from the standard itself.**
   The archive.org scan's OCR degrades badly on the dense Annex G pages. My
   coordinate-based reparse reproduced [MBE]'s `hoba` **exactly** for 8 values of L
   (L = 9, 10, 11, 12, 13, 15, 16, 17 — the sparse, low-L pages) and corroborated it
   partially elsewhere, but column drift makes the scan unusable as a sole source for
   the full table. It is good enough to *corroborate* [MBE], not to *replace* it.
   A purchased copy would close this.
3. **Eq. (75) read directly from clean primary text.** See the confidence note in
   §2.5. Two-source agreement, but degraded OCR on the primary. Note that eq. (72)
   has the *same* problem and I did resolve that one (§2.3) using the standard's own
   worked example — but it is a reminder that the scan drops terms from equations,
   silently. Every equation in §2 was therefore checked against [MBE] rather than
   trusted from the OCR alone.
4. **Whether TIA-102.BABA-A (2014) changed any of these constants.** The 2014 revision
   merges the full-rate and half-rate documents. I found no indication that full-rate
   IMBE constants changed — that would break interoperability with every deployed P25
   radio — but I did not read the revision, so I am not asserting it.
5. **Provenance of mbelib's tables.** The author is pseudonymous ("mbelib Author",
   identified only by a GPG key). Whether the tables were transcribed from the
   standard, reverse-engineered, or derived from DVSI code cannot be established.
   See §6.2 — this is a due-diligence point, not a technical one.
6. **The exact `<` vs `<=` boundaries in eq. (55).** Resolved as immaterial by the
   continuity argument in §2.5, not by reading clean text.

---

## 6. Licensing — per source

**I am not a lawyer and this is not legal advice.** Licenses are quoted by name and
checked against the actual license files, not assumed. The commercial question —
"can this ship in a closed-source product?" — should get counsel sign-off before
release. Flagging per standing preference on legal exposure.

### 6.1 Summary table

| Source | License (verified) | Closed-source commercial use? |
|---|---|---|
| **szechyjs/mbelib** | **ISC License** (verbatim ISC text, in a file named `COPYRIGHT`) | **YES** — permissive; retain the copyright + permission notice |
| **op25 `imbe_vocoder`** | **GNU GPL v3 or later** | **NO** — would force the entire product under GPLv3 |
| **arancormonk/mbelib-neo** | **GNU GPL v2.0** | **NO** — relicensed fork; see warning below |
| **jmbe (DSheirer)** | **GNU GPL v3** | **NO** |
| **dsdcc (f4exb)** | No license declared in repo metadata | **NO** — absent a grant, no license = no rights |
| **codec2 / FreeDV** | LGPL v2.1 | Moot — see §6.3 |
| **TIA-102.BABA** | Copyrighted TIA standard, purchasable | Text is copyrighted; see §6.4 |

### 6.2 mbelib — the usable one, with caveats

The license file is named `COPYRIGHT` (not `LICENSE`), which is why GitHub's license
detector reports "NOASSERTION" / "Other" for the repo. Its contents are the **ISC
License, verbatim**:

> "Permission to use, copy, modify, and/or distribute this software for any purpose
> with or without fee is hereby granted, provided that the above copyright notice
> and this permission notice appear in all copies."

Copyright line: `Copyright (C) 2010 mbelib Author`.

ISC is a permissive, OSI-approved license, functionally equivalent to 2-clause BSD /
MIT. It permits use in a closed-source commercial product provided the copyright
notice and permission notice are retained (typically in a third-party notices file).
There is no copyleft and no source-disclosure obligation.

**Three caveats to raise with counsel, none of which are license terms but all of
which are real:**

1. **The README carries a "PATENT NOTICE" stating the code "is provided for
   educational purposes only."** That phrase is in the README, not in the license
   grant, and the ISC grant itself is unconditional and says "for any purpose."
   But a commercial user should be aware the author framed the work that way. The
   full notice: *"This source code is provided for educational purposes only... Executable
   objects compiled or derived from this package may be covered by one or more patents.
   Readers are strongly advised to check for any patent restrictions or licencing
   requirements before compiling or using this source code."*
2. **The copyright holder is pseudonymous** and the repo has not been pushed since
   2020-10-31. There is no identifiable counterparty, no warranty, and no way to seek
   clarification or an indemnity.
3. **Provenance of the tables is unverifiable** (see §5.5). If it matters, the clean
   path is to buy TIA-102.BABA-A and transcribe from it, using mbelib only as a
   cross-check. That converts an unverifiable provenance into a documented one for
   about $271.

**Warning on the fork:** `arancormonk/mbelib-neo` is the actively-maintained, more
performant fork and is the one a developer is most likely to reach for — but it is
**GPL-2.0 licensed**, confirmed from the repository's `LICENSE` file and GitHub's
license metadata (`spdx_id: GPL-2.0`). A permissive upstream can be relicensed by a
downstream fork, and here it was. **Do not pull mbelib-neo into this product.** Use
`szechyjs/mbelib`.

### 6.3 codec2 / FreeDV — not applicable

codec2 is LGPL v2.1 and is explicitly designed to be patent-free, but it is **not an
IMBE implementation**. It is a different harmonic-sinusoidal codec with a single
voicing bit, developed specifically to *avoid* the MBE patent family. It contains
none of the IMBE quantizer constants and is of no use for this task. Ruled out on
technical grounds before licensing becomes relevant.

### 6.4 Is the archive.org copy usable?

`archive.org/details/TIA-102_Series_Documents` hosts scans of TIA-102 standards
including TIA-102.BABA. These are **copyrighted TIA documents uploaded by a third
party**, not an authorised TIA release. Reading it for research is one thing;
building a commercial product's constant tables by transcribing it is a different
risk posture.

Two distinct questions, which should not be conflated:

- **The document text, figures and layout** are unambiguously copyrighted by TIA.
  Do not copy prose or reproduce annex pages.
- **The numeric table values themselves** are a harder question. Under *Feist
  Publications v. Rural Telephone Service*, 499 U.S. 340 (1991), facts are not
  copyrightable, though a compilation's selection and arrangement can be. Quantizer
  tables sit awkwardly between "fact" and "creative engineering expression," and
  TIA-102 is not incorporated into law, so the public-access arguments from
  *ASTM v. Public.Resource.Org* do not transfer. **This is genuinely unsettled and I
  am not going to guess at it.**

**This ambiguity is entirely avoidable and that is the practical point:** mbelib's ISC
grant is an *express written license* to use, copy, modify and distribute the same
values. Taking the numbers from mbelib rather than from the scan sidesteps the
question completely. That is the recommended path.

**Purchase route, if a documented clean provenance is wanted:**
ANSI/TIA-102.BABA-A, *Project 25 Vocoder Description*, approved 2014-01-27, is the
current active revision (it merges the original TIA-102.BABA full-rate document with
the TIA-102.BABA-1 half-rate addendum). Listed at **from $271.00** at the TIA store
(Accuris): `https://store.accuristech.com/standards/tia-ansi-tia-102-baba-a?product_id=2594105`.
Also listed via IHS/GlobalSpec. Purchasing gives a licensed copy but note that
standards licenses typically restrict redistribution — it licenses *your* use of the
document, it does not grant rights to republish the tables.

### 6.5 Patent status — the IMBE patents have expired

This is the risk everyone worries about with MBE-family vocoders, so it was checked
directly rather than assumed. All from Google Patents / USPTO records:

| Patent | Title | Assignee | Priority | Status |
|---|---|---|---|---|
| **US 5,226,084** | Methods for speech quantization and error correction | Digital Voice Systems Inc | 1990-12-05 | **Expired — Lifetime.** Anticipated expiration **2010-12-05** |
| **US 5,247,579** | Methods for speech transmission | Digital Voice Systems Inc | 1990-12-05 | **Expired — Lifetime.** Anticipated expiration **2010-12-05** |
| **US 5,870,405** | Digital transmission of acoustic signals over a noisy communication channel | Digital Voice Systems Inc | 1992-11-30 | **Expired — Lifetime.** Anticipated expiration **2012-11-30** |

Inventors on all three: John C. Hardwick and Jae S. Lim. US 5,226,084 is the core
spectral-amplitude quantization patent — the one that actually covers the algorithm
in this document — and it expired in **2010**.

Notes and limits on that finding:

- Expiry dates are as reported by Google Patents' "Anticipated expiration" field;
  for a commercial release, have counsel pull the certified file histories and
  confirm no continuations or reissues are live.
- This covers the **IMBE / P25 Phase 1** patent generation only. **AMBE+2, used in
  P25 Phase 2, DMR, D-STAR and NXDN, is a later and separately-patented codec** and
  nothing here should be read as applying to it. If the product ever needs Phase 2,
  that is a fresh analysis.
- mbelib's own patent notice (quoted in §6.2) predates these expirations and is now
  largely stale for IMBE specifically — but it is the author's warning, not a legal
  determination, and it is not license text.

---

## 7. Recommendation

**Yes — implementable now, from freely-usable and citable sources, with no GPL
contamination and no mandatory purchase.**

Concretely:

1. **Implement the algorithm from this document.** §2 is complete enough to code
   against. It was written from the primary standard and corroborated line-by-line
   against an independent implementation.
2. **Generate, don't transcribe, everything that is derivable:** block lengths `J_i`
   (closed form, §2.1), all 240 Annex F step sizes (from `sigma_gain` + `quantstep`,
   §3.2). That removes 240 floats and an entire annex from the data burden and
   eliminates a whole class of transcription bugs.
3. **Take the 1512 irreducible integers and the 64-entry gain table from
   `szechyjs/mbelib` under ISC** — `ba` (bit counts only), `hoba`, `B2`. Retain the
   ISC copyright and permission notice in the product's third-party notices.
   **Apply the L=23 / G5 correction from §3.2 — mbelib's value is wrong.**
4. **Do not touch op25's `imbe_vocoder`, jmbe, mbelib-neo, or dsdcc.** All are GPL or
   unlicensed. Using any of them would force the whole TSCM product's licensing —
   which is the outcome this research existed to prevent.
5. **Optional, ~$271, recommended if this ships to clients:** buy ANSI/TIA-102.BABA-A
   and re-derive the 1512 integers from it. That replaces "an anonymous GitHub
   author's tables" with "the standard we licensed" in the provenance story, and
   makes the mbelib dependency a cross-check rather than a source. Cheap insurance
   for a commercial product; not a blocker for getting audio working now.
6. **Legal review before release**, on two narrow points only: (a) the ISC attribution
   obligation is satisfied, and (b) confirmation that the DVSI patents in §6.5 are
   expired with no live continuations. Neither is expected to be a problem.

**Blocked? No. Requires purchasing the standard? No — recommended, not required.**

The single thing that would have blocked this is if the bit-allocation tables existed
only in GPL code. They do not: mbelib is ISC, and that is the load-bearing finding in
this entire document.

---

## 8. Appendix — reproducing the verification

Everything above was checked mechanically. To repeat it:

```
# primary standard, OCR text and word-coordinate XML
https://archive.org/download/TIA-102_Series_Documents/TIA-102.BABA_Project_25_IMBE_Vocoder_Description_djvu.txt
https://archive.org/download/TIA-102_Series_Documents/TIA-102.BABA_Project_25_IMBE_Vocoder_Description_djvu.xml
https://archive.org/download/TIA-102_Series_Documents/TIA-102.BABA_Project_25_IMBE_Vocoder_Description.pdf

# independent implementation (ISC)
https://raw.githubusercontent.com/szechyjs/mbelib/master/imbe7200x4400_const.h
https://raw.githubusercontent.com/szechyjs/mbelib/master/imbe7200x4400.c
https://raw.githubusercontent.com/szechyjs/mbelib/master/mbelib.c
https://raw.githubusercontent.com/szechyjs/mbelib/master/COPYRIGHT
```

Relevant clauses in the standard: 6.1 (fundamental / L / K), 6.3 (encoding — useful
because it states the quantizer forms), **6.4 (decoding — the target)**, clause 8
(enhancement), clause 9 (adaptive smoothing). Tables 3 and 4 are in clause 6.3.2.
Worked example with validation vectors is clause 10, Tables 6 and 7. Annexes: E (gain
quantizer levels), F (gain vector allocation + step size), G (higher-order
allocation), J (block lengths).

In the `_djvu.xml`, `<WORD coords="x0,y1,x1,y0">` allows reconstructing table rows by
clustering on `y1` then sorting by `x0`. That is how the L=23 mbelib bug was
confirmed against the page image rather than against the flat OCR dump. The flat
`_djvu.txt` emits one token per line with the two-column table layout interleaved, and
is not reliable for the dense annexes.

Checks performed:

- Closed-form `J_i` vs [MBE] `ImbeJi`: **48/48 exact.**
- `quantstep`, `standdev` [TIA] vs [MBE]: **exact, including the non-monotonic tail.**
- Annex E vs [MBE] `B2[64]`: **64/64 exact, zero mismatches.**
- `sigma_gain` derivation vs Annex F step sizes: **240/240 exact** (the one apparent
  failure was mbelib's typo, confirmed against the scan).
- Bit budget `sum(gain) + sum(higher) == 73 - K`: **48/48 exact.**
- Annex G OCR vs [MBE] `hoba`: exact for L = 9, 10, 11, 12, 13, 15, 16, 17; degraded
  by OCR column drift at higher L (see §5.2).
- [TIA] clause 10 worked example (L=16) vs [MBE] tables: **exact.**
