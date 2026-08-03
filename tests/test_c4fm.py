"""Unit tests for the C4FM / 4-level FSK symbol recovery front end.

Synthesize a known dibit stream with c4fm.encode_c4fm(), push it back through
demod_c4fm(), and assert EXACT recovery — every dibit, not a similarity score —
across sample rate x pulse shape x polarity x carrier offset x clock error.

Two structural notes that the tests pin deliberately:
  * The FM discriminator consumes one sample, so the very first symbol of a
    capture is lost and the recovered stream starts at the ORIGINAL stream's
    symbol 1. _aligned() finds that offset instead of hiding it, and
    test_leading_symbol_offset_is_at_most_one pins it to <= 1 symbol so a
    downstream sync search never has to hunt.
  * Polarity inversion is dibit ^ 0b10, NOT a bit-stream complement. That is
    easy to get wrong and silently fatal, so it has its own test.

Negatives matter as much as positives here: noise, an unmodulated carrier,
analog FM and 2-FSK must all come back with a LOW quality number, because the
protocol layers above use it to decide whether a symbol stream is worth
believing. All headless — no hardware.
"""

import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from survey import c4fm                                            # noqa: E402
from survey.c4fm import (SYMBOL_RATE, LEVELS, DIBIT_FOR_LEVEL, LEVEL_FOR_DIBIT,
                         demod_c4fm, encode_c4fm, dibits_to_bits, bits_to_dibits,
                         invert_dibits, find_pattern, rrc_taps)  # noqa: E402
from survey.pocsag import encode_pocsag                            # noqa: E402

FS = 48000.0          # 10 samples/symbol at 4800 Bd — dec=1, cheap synth
N_SYM = 600


def _stream(n=N_SYM, seed=0):
    return np.random.default_rng(seed).integers(0, 4, n).astype(np.uint8)


def _aligned(got, want):
    """(offset, match_fraction) for the best whole-symbol alignment of a
    recovered stream against the transmitted one. `offset` is positive when the
    recovered stream has extra leading symbols and negative when it is missing
    leading symbols (the discriminator's one-sample bite)."""
    best = (0, 0.0)
    for off in range(-8, 8):
        g = got[off:] if off >= 0 else got
        w = want if off >= 0 else want[-off:]
        n = min(g.size, w.size)
        if n < 64:
            continue
        m = float(np.mean(g[:n] == w[:n]))
        if m > best[1]:
            best = (off, m)
    return best


def _assert_exact(self, got, want, label=""):
    off, m = _aligned(got, want)
    self.assertEqual(m, 1.0, "%s: %.4f of symbols matched at offset %d"
                     % (label, m, off))
    return off


class TestConstants(unittest.TestCase):
    def test_symbol_rate(self):
        self.assertEqual(SYMBOL_RATE, 4800.0)   # DMR and P25 Phase 1 alike

    def test_rail_dibit_table_is_the_tia_mapping(self):
        # outer+ = 01, inner+ = 00, inner- = 10, outer- = 11
        self.assertEqual(DIBIT_FOR_LEVEL[3], 0b01)
        self.assertEqual(DIBIT_FOR_LEVEL[1], 0b00)
        self.assertEqual(DIBIT_FOR_LEVEL[-1], 0b10)
        self.assertEqual(DIBIT_FOR_LEVEL[-3], 0b11)
        self.assertEqual(LEVELS, (3, 1, -1, -3))
        for lvl, d in DIBIT_FOR_LEVEL.items():          # the tables are inverses
            self.assertEqual(LEVEL_FOR_DIBIT[d], lvl)

    def test_rrc_taps_are_symmetric_unit_energy_and_odd(self):
        h = rrc_taps(10.0)
        self.assertEqual(h.size % 2, 1)                 # odd -> integer group delay
        self.assertTrue(np.allclose(h, h[::-1]))
        self.assertAlmostEqual(float(np.sum(h * h)), 1.0, places=9)

    def test_rrc_cascade_is_nyquist_at_symbol_instants(self):
        # RRC (x) RRC = raised cosine, which must be ~zero at every non-zero
        # multiple of the symbol period. This is what makes the synthetic round
        # trip ISI-free, and it is why RRC_SPAN is 12 rather than 8.
        h = rrc_taps(10.0)
        c = np.correlate(h, h, "full")
        mid = c.size // 2
        for k in range(1, 5):
            self.assertLess(abs(c[mid + k * 10] / c[mid]), 0.005, "ISI at lag %d" % k)


class TestBitPlumbing(unittest.TestCase):
    def test_dibit_bit_roundtrip(self):
        d = _stream(200, seed=4)
        self.assertTrue(np.array_equal(bits_to_dibits(dibits_to_bits(d)), d))

    def test_bits_are_msb_first(self):
        # dibit 0b10 -> bits (1, 0)
        self.assertTrue(np.array_equal(dibits_to_bits(np.array([0b10], dtype=np.uint8)),
                                       np.array([1, 0], dtype=np.uint8)))
        self.assertTrue(np.array_equal(dibits_to_bits(np.array([0b01], dtype=np.uint8)),
                                       np.array([0, 1], dtype=np.uint8)))

    def test_inversion_is_xor_2_not_bit_complement(self):
        d = _stream(64, seed=5)
        self.assertTrue(np.array_equal(invert_dibits(invert_dibits(d)), d))
        # negating the rails maps each dibit to the dibit of the negated rail
        for dib in range(4):
            lvl = LEVEL_FOR_DIBIT[dib]
            self.assertEqual(int(invert_dibits(np.array([dib], dtype=np.uint8))[0]),
                             DIBIT_FOR_LEVEL[-lvl])
        # and it is NOT the complement of the bit stream (only the MSB flips)
        self.assertFalse(np.array_equal(dibits_to_bits(invert_dibits(d)),
                                        dibits_to_bits(d) ^ 1))


class TestPatternSearch(unittest.TestCase):
    def test_finds_an_embedded_pattern_on_dibit_boundaries(self):
        rng = np.random.default_rng(9)
        pat_d = rng.integers(0, 4, 24).astype(np.uint8)
        pattern = 0
        for x in pat_d:
            pattern = (pattern << 2) | int(x)
        stream = np.concatenate([rng.integers(0, 4, 100).astype(np.uint8), pat_d,
                                 rng.integers(0, 4, 100).astype(np.uint8)])
        hits = find_pattern(stream, pattern, 24, max_err=0, allow_inverted=False)
        self.assertIn(100, [h[0] for h in hits])

    def test_finds_the_inverted_copy_and_flags_it(self):
        rng = np.random.default_rng(10)
        pat_d = rng.integers(0, 4, 24).astype(np.uint8)
        pattern = 0
        for x in pat_d:
            pattern = (pattern << 2) | int(x)
        stream = np.concatenate([rng.integers(0, 4, 40).astype(np.uint8),
                                 invert_dibits(pat_d),
                                 rng.integers(0, 4, 40).astype(np.uint8)])
        hits = [h for h in find_pattern(stream, pattern, 24, max_err=0) if h[0] == 40]
        self.assertEqual(len(hits), 1)
        self.assertTrue(hits[0][1])              # reported as inverted
        self.assertEqual(hits[0][2], 0)

    def test_tolerates_up_to_max_err_bits(self):
        rng = np.random.default_rng(11)
        pat_d = rng.integers(0, 4, 24).astype(np.uint8)
        pattern = 0
        for x in pat_d:
            pattern = (pattern << 2) | int(x)
        corrupt = pat_d.copy()
        corrupt[3] ^= 0b01                       # one bit error
        stream = np.concatenate([rng.integers(0, 4, 20).astype(np.uint8), corrupt,
                                 rng.integers(0, 4, 20).astype(np.uint8)])
        self.assertEqual([h[0] for h in find_pattern(stream, pattern, 24, 0, False)], [])
        self.assertIn(20, [h[0] for h in find_pattern(stream, pattern, 24, 1, False)])

    def test_too_short_stream_returns_nothing(self):
        self.assertEqual(find_pattern(np.zeros(4, dtype=np.uint8), 0, 24), [])


class TestRoundTrip(unittest.TestCase):
    def test_exact_recovery_at_working_rate(self):
        d = _stream()
        r = demod_c4fm(encode_c4fm(d, FS), FS)
        _assert_exact(self, r["dibits"], d, "48 kHz")
        self.assertGreater(r["quality"], 0.9)

    def test_exact_recovery_through_decimation(self):
        # the real path: a 2.4 MHz Pluto capture -> decimate 50x -> 10 sps
        d = _stream(400, seed=1)
        r = demod_c4fm(encode_c4fm(d, 2_400_000.0), 2_400_000.0)
        _assert_exact(self, r["dibits"], d, "2.4 MHz")
        self.assertGreater(r["quality"], 0.9)

    def test_exact_recovery_at_a_non_integer_sps(self):
        # 1.0 MHz / 48 kHz target -> dec 21 -> 47619 Hz -> 9.92 samples/symbol
        d = _stream(400, seed=2)
        fs = 1_000_000.0
        r = demod_c4fm(encode_c4fm(d, fs), fs)
        self.assertNotAlmostEqual(r["sps"], round(r["sps"]), places=3)
        _assert_exact(self, r["dibits"], d, "1 MHz")

    def test_leading_symbol_offset_is_at_most_one(self):
        # The discriminator eats one sample, so the stream may start one symbol
        # in. Anything larger would mean a timing bug, and would force every
        # downstream sync search to slide further than it should.
        for seed in range(4):
            d = _stream(300, seed=seed)
            off = _assert_exact(self, demod_c4fm(encode_c4fm(d, FS), FS)["dibits"],
                                d, "seed %d" % seed)
            self.assertLessEqual(abs(off), 1)

    def test_bits_match_the_dibits(self):
        d = _stream(200, seed=6)
        r = demod_c4fm(encode_c4fm(d, FS), FS)
        self.assertTrue(np.array_equal(bits_to_dibits(r["bits"]), r["dibits"]))

    def test_polarity_inversion_recovers_after_invert_dibits(self):
        d = _stream()
        r = demod_c4fm(encode_c4fm(d, FS, invert=True), FS)
        _assert_exact(self, invert_dibits(r["dibits"]), d, "inverted")
        self.assertGreater(r["quality"], 0.9)

    def test_invert_argument_matches_invert_dibits(self):
        d = _stream(300, seed=7)
        iq = encode_c4fm(d, FS, invert=True)
        self.assertTrue(np.array_equal(demod_c4fm(iq, FS, invert=True)["dibits"],
                                       invert_dibits(demod_c4fm(iq, FS)["dibits"])))

    def test_carrier_offset_is_absorbed(self):
        # a 900 Hz LO error lands as a DC bias on the discriminator, half the
        # rail spacing — the (dc, spacing) fit has to take it out
        d = _stream()
        r = demod_c4fm(encode_c4fm(d, FS, freq_offset=900.0), FS)
        _assert_exact(self, r["dibits"], d, "900 Hz offset")
        self.assertGreater(r["quality"], 0.9)

    def test_symbol_clock_error_is_tracked(self):
        # +/- 500 ppm walks the sampling instant a full symbol across the burst
        # unless the timing loop tracks the RATE, not just the phase
        d = _stream()
        for ppm in (500, -500):
            rate = SYMBOL_RATE * (1.0 + ppm / 1e6)
            r = demod_c4fm(encode_c4fm(d, FS, symbol_rate=rate), FS)
            _assert_exact(self, r["dibits"], d, "%+d ppm" % ppm)

    def test_rectangular_shaping_still_recovers(self):
        # proves the receiver is not merely matched to its own transmit filter:
        # hard NRZ rails, no pulse shaping at all
        d = _stream()
        r = demod_c4fm(encode_c4fm(d, FS, shaping="rect"), FS)
        _assert_exact(self, r["dibits"], d, "rect")

    def test_noise_degrades_quality_monotonically_before_it_breaks_symbols(self):
        d = _stream()
        prev = 1.1
        for nz in (0.0, 0.05, 0.10, 0.20):
            r = demod_c4fm(encode_c4fm(d, FS, noise=nz, seed=3), FS)
            _assert_exact(self, r["dibits"], d, "noise %.2f" % nz)
            self.assertLess(r["quality"], prev, "quality did not fall at %.2f" % nz)
            prev = r["quality"]

    def test_deviation_scale_is_irrelevant(self):
        # the slicer FITS the rail spacing, so DMR's 1944 Hz and P25's 1800 Hz
        # (and a badly-gained receiver) all decode identically
        d = _stream(300, seed=8)
        for dev in (600.0, 1800.0, 1944.0, 5000.0):
            r = demod_c4fm(encode_c4fm(d, FS, deviation=dev), FS)
            _assert_exact(self, r["dibits"], d, "deviation %g" % dev)


class TestNegatives(unittest.TestCase):
    """Nothing here may produce a confident symbol stream. quality is what the
    DMR/P25 layers read to decide whether to bother, so a false high number is
    exactly the failure mode this codebase refuses to ship."""

    def test_noise_scores_zero(self):
        rng = np.random.default_rng(1)
        n = 200_000
        iq = (rng.standard_normal(n) + 1j * rng.standard_normal(n)).astype(np.complex64)
        self.assertLess(demod_c4fm(iq, 2_400_000.0)["quality"], 0.2)

    def test_analog_fm_voice_scores_near_zero(self):
        t = np.arange(240_000) / 2_400_000.0
        dev = 3000.0 * np.sin(2 * np.pi * 1000.0 * t)
        iq = np.exp(1j * 2 * np.pi * np.cumsum(dev) / 2_400_000.0).astype(np.complex64)
        self.assertLess(demod_c4fm(iq, 2_400_000.0)["quality"], 0.2)

    def test_unmodulated_carrier_scores_zero(self):
        # A dead carrier parks every symbol on ONE rail and therefore fits that
        # rail perfectly. Tightness alone would score it 1.0; rail coverage is
        # what makes it 0.0.
        t = np.arange(96_000) / FS
        iq = np.exp(1j * 2 * np.pi * 1200.0 * t).astype(np.complex64)
        r = demod_c4fm(iq, FS)
        self.assertLessEqual(r["rails_used"], 1)
        self.assertLess(r["quality"], 0.05)

    def test_two_level_fsk_scores_poorly(self):
        # a real POCSAG page: 2-FSK fits two of the four rails perfectly
        iq = encode_pocsag([{"ric": 123456, "mode": "alpha", "text": "HELLO"}],
                           1200, FS, preamble_bits=64)
        r = demod_c4fm(iq, FS)
        self.assertLessEqual(r["rails_used"], 2)
        self.assertLess(r["quality"], 0.4)

    def test_dc_only_returns_an_empty_result(self):
        r = demod_c4fm(np.ones(20_000, dtype=np.complex64), FS)
        self.assertEqual(r["dibits"].size, 0)
        self.assertEqual(r["quality"], 0.0)
        self.assertTrue(r["note"])

    def test_too_short_raises(self):
        with self.assertRaises(ValueError):
            demod_c4fm(np.zeros(8, dtype=np.complex64), FS)

    def test_sample_rate_below_the_symbol_rate_is_reported_not_guessed(self):
        d = _stream(100, seed=12)
        r = demod_c4fm(encode_c4fm(d, 9600.0), 9600.0)
        self.assertEqual(r["dibits"].size, 0)
        self.assertIn("sample rate", r["note"])


class TestGate(unittest.TestCase):
    def test_looks_like_c4fm(self):
        self.assertTrue(c4fm.looks_like_c4fm(
            {"modulation_class": "fsk", "fsk_states": 4, "symbol_rate_kbd": 4.8,
             "occupied_bw_khz": 12.5}))
        self.assertFalse(c4fm.looks_like_c4fm(
            {"modulation_class": "ofdm_wideband", "fsk_states": 0,
             "symbol_rate_kbd": 0.0, "occupied_bw_khz": 20000}))
        self.assertFalse(c4fm.looks_like_c4fm(
            {"modulation_class": "fsk", "fsk_states": 2, "symbol_rate_kbd": 1.2,
             "occupied_bw_khz": 12.0}))          # POCSAG shape, not C4FM
        self.assertFalse(c4fm.looks_like_c4fm({}))


if __name__ == "__main__":
    unittest.main()
