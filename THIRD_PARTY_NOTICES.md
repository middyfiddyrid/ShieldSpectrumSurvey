# Third-party notices

Shield TSCM Spectrum Survey incorporates third-party material. The notices below
must travel with any distribution of this product — source, binary, installer,
or laptop bundle.

---

## mbelib — ISC License

**Used for:** the IMBE 7200x4400 spectral amplitude quantizer constants in
`survey/imbe_tables.py` (`GAIN_LEVELS`, `GAIN_BITS`, `HIGHER_ORDER_BITS`, taken
from `imbe7200x4400_const.h` arrays `B2[64]`, `ba[48][5][2]`, `hoba[48][50]`).
These are the bit-allocation and gain-quantizer tables that let P25 Phase 1
voice frames be decoded to a speech envelope. They cannot be derived — every
other constant in the IMBE chain is computed rather than tabulated.

**Source:** https://github.com/szechyjs/mbelib

**License:** ISC. The licence text lives in a file named `COPYRIGHT`, which is
why GitHub reports the repository as "NOASSERTION". ISC is permissive and
imposes no copyleft, so this material may ship in a closed-source commercial
product provided the notice below appears in all copies.

```
Copyright (C) 2010 mbelib Author

Permission to use, copy, modify, and/or distribute this software for any
purpose with or without fee is hereby granted, provided that the above
copyright notice and this permission notice appear in all copies.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES WITH
REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY AND
FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY SPECIAL, DIRECT,
INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER RESULTING FROM
LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE OR
OTHER TORTIOUS ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE OR
PERFORMANCE OF THIS SOFTWARE.
```

### Forks that must NOT be used

These carry copyleft terms that would force this entire product's licensing.
Recorded here because the first of them is the one a developer would naturally
reach for, being the actively-maintained fork:

| Project | License | Usable here |
|---|---|---|
| `szechyjs/mbelib` | ISC | **yes** — the source used |
| `arancormonk/mbelib-neo` | GPL-2.0 | no — relicensed fork |
| op25 `imbe_vocoder` | GPLv3+ | no |
| `jmbe` | GPLv3 | no |
| `dsdcc` | none declared | no |

---

## Notes on the underlying standard

The constants above are corroborated against TIA/EIA-102.BABA (the P25 IMBE
Vocoder Description), but they are **taken from mbelib under its express ISC
grant**, not transcribed from the standard. The freely accessible copy of that
standard is an unauthorised scan, and reproducing tables out of it into a
commercial product is a different risk posture from reading it for research.
Sourcing from mbelib avoids the question entirely.

For documented provenance in client-facing or litigation-support work, the
purchasable standard is ANSI/TIA-102.BABA-A (~$271). That is optional and is not
a licensing requirement — see `docs/IMBE_SPECTRAL_AMPLITUDES.md`.

The core DVSI spectral-amplitude quantization patents (US 5,226,084 and
US 5,247,579, Hardwick & Lim; US 5,870,405) have expired.

Nothing here is legal advice.
