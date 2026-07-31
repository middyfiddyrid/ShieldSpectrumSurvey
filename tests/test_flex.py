"""Unit tests for the FLEX paging decoder.

Synthesize a known FLEX transmission with flex.flex_encode() (which shares the
SAME BCH(31,21) generator as the decoder, so a wrong polynomial fails loudly),
then round-trip it through decode_flex and assert exact recovery. Also pin the
block interleaver as its own inverse, the frame-info checksum gate, and — the
honesty requirement — that noise / a pure tone / a false comma lock yield ZERO
pages. Headless; no hardware. Synth at fs=32000 (dec=1, integer sps) for speed,
plus one run at the real 2.4 MHz capture rate.
"""

import unittest

import numpy as np

from survey import flex
from survey.flex import (A_WORD, A_WORD_INV, BCH_POLY, bch_check, encode_codeword,
                         codeword_data, interleave_block, deinterleave_block,
                         encode_fiw, decode_fiw, flex_encode, decode_flex)

FS = 32000.0          # synth rate: dec=1, integer sps (20 @1600, 10 @3200)


def _msgs(r):
    return r["messages"]


def _only(r):
    msgs = r["messages"]
    assert len(msgs) == 1, f"expected 1 message, got {len(msgs)}: {msgs}"
    return msgs[0]


class TestConstants(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(BCH_POLY, 0x769)
        self.assertEqual(A_WORD_INV, A_WORD ^ 0xFFFFFFFF)


class TestBCH(unittest.TestCase):
    def setUp(self):
        self.rng = np.random.default_rng(0)

    def test_bch_roundtrip(self):
        for _ in range(60):
            data21 = int(self.rng.integers(0, 1 << 21))
            cw = encode_codeword(data21)
            ok, c, ne = bch_check(cw)
            self.assertTrue(ok); self.assertEqual(ne, 0); self.assertEqual(c, cw)
            self.assertEqual(codeword_data(cw), data21)
            b = int(self.rng.integers(0, 32))
            ok, c, ne = bch_check(cw ^ (1 << b))
            self.assertTrue(ok); self.assertEqual(ne, 1); self.assertEqual(c, cw)
            b1 = int(self.rng.integers(0, 32))
            b2 = (b1 + 1 + int(self.rng.integers(0, 31))) % 32
            ok, c, ne = bch_check(cw ^ (1 << b1) ^ (1 << b2))
            self.assertTrue(ok); self.assertEqual(ne, 2); self.assertEqual(c, cw)

    def test_three_errors_uncorrectable(self):
        cw = encode_codeword(0xABCDE)
        ok, c, ne = bch_check(cw ^ 0b111)
        self.assertFalse(ok); self.assertEqual(ne, -1)


class TestInterleaver(unittest.TestCase):
    def setUp(self):
        self.rng = np.random.default_rng(2)

    def test_interleave_is_invertible(self):
        for _ in range(20):
            words = [int(self.rng.integers(0, 1 << 32)) for _ in range(8)]
            bits = interleave_block(words)
            self.assertEqual(bits.size, 256)
            back = deinterleave_block(bits)
            self.assertEqual(back, words)

    def test_interleave_spreads_burst(self):
        # A contiguous burst of bit errors in the interleaved stream must land in
        # DISTINCT words after de-interleave (so each is BCH-correctable). With 8
        # words, 8 consecutive stream bits hit 8 different words, one bit each.
        words = [encode_codeword(i * 13) for i in range(8)]
        bits = interleave_block(words).copy()
        bits[10:18] ^= 1                      # 8-bit burst
        back = deinterleave_block(bits)
        # every word recovers with at most a single-bit correction
        for w in back:
            ok, c, ne = bch_check(w)
            self.assertTrue(ok)
            self.assertLessEqual(ne, 1)


class TestFIW(unittest.TestCase):
    def test_fiw_roundtrip(self):
        for cycle in range(0, 16, 3):
            for frame in range(0, 128, 17):
                for rate in (0, 1, 2):
                    fiw = encode_fiw(cycle, frame, rate)
                    ok, c, f, r = decode_fiw(fiw)
                    self.assertTrue(ok)
                    self.assertEqual((c, f, r), (cycle, frame, rate))

    def test_fiw_rejects_garbage(self):
        # a random 32-bit word that happens to be BCH-clean must still usually fail
        # the checksum; assert that pure noise words are overwhelmingly rejected.
        rng = np.random.default_rng(5)
        passed = 0
        for _ in range(500):
            w = int(rng.integers(0, 1 << 32))
            ok, *_ = decode_fiw(w)
            if ok:
                passed += 1
        self.assertLess(passed, 25)            # <5% slip-through


class TestDecode1600(unittest.TestCase):
    def test_alpha_single(self):
        iq = flex_encode([{"capcode": 1234567, "mode": "alpha", "text": "TEST 42"}],
                         1600, FS)
        m = _only(decode_flex(iq, FS))
        self.assertEqual(m["capcode"], 1234567)
        self.assertEqual(m["text"], "TEST 42")
        self.assertEqual(m["type"], "alpha")

    def test_alpha_long_multiword(self):
        text = "EVACUATE BUILDING 7 NOW -- CALL 5551234"
        iq = flex_encode([{"capcode": 2000000, "mode": "alpha", "text": text}],
                         1600, FS)
        m = _only(decode_flex(iq, FS))
        self.assertEqual(m["text"], text)

    def test_numeric_single(self):
        iq = flex_encode([{"capcode": 555000, "mode": "numeric", "text": "1234567890"}],
                         1600, FS)
        m = _only(decode_flex(iq, FS))
        self.assertEqual(m["text"], "1234567890")
        self.assertEqual(m["type"], "numeric")

    def test_two_pages(self):
        pages = [{"capcode": 111111, "mode": "alpha", "text": "FIRST"},
                 {"capcode": 222222, "mode": "numeric", "text": "456"}]
        iq = flex_encode(pages, 1600, FS)
        got = {m["capcode"]: m for m in _msgs(decode_flex(iq, FS))}
        self.assertEqual(got[111111]["text"], "FIRST")
        self.assertEqual(got[222222]["text"], "456")

    def test_stats(self):
        iq = flex_encode([{"capcode": 42, "mode": "alpha", "text": "HI"}], 1600, FS)
        r = decode_flex(iq, FS)
        self.assertTrue(r["stats"]["sync"])
        self.assertEqual(r["stats"]["bps"], 1600)
        self.assertEqual(r["stats"]["level"], 2)

    def test_polarity_inversion(self):
        iq = flex_encode([{"capcode": 99999, "mode": "alpha", "text": "FLIP"}],
                         1600, FS, invert=True)
        m = _only(decode_flex(iq, FS))
        self.assertEqual(m["text"], "FLIP")

    def test_full_chain_through_iq(self):
        iq = flex_encode([{"capcode": 1314159, "mode": "alpha", "text": "REALPATH 9"}],
                         1600, 2_400_000.0)
        m = _only(decode_flex(iq, 2_400_000.0))
        self.assertEqual(m["capcode"], 1314159)
        self.assertEqual(m["text"], "REALPATH 9")

    def test_bch_correction_in_chain(self):
        iq = flex_encode([{"capcode": 888888, "mode": "alpha", "text": "ERRORS OK"}],
                         1600, FS, noise=0.04, seed=3)
        m = _only(decode_flex(iq, FS))
        self.assertEqual(m["text"], "ERRORS OK")


class TestDecode3200(unittest.TestCase):
    def test_alpha_4fsk(self):
        iq = flex_encode([{"capcode": 314159, "mode": "alpha", "text": "FOURLEVEL"}],
                         3200, FS)
        r = decode_flex(iq, FS)
        m = _only(r)
        self.assertEqual(m["text"], "FOURLEVEL")
        self.assertEqual(r["stats"]["bps"], 3200)
        self.assertEqual(r["stats"]["level"], 4)

    def test_numeric_4fsk(self):
        iq = flex_encode([{"capcode": 271828, "mode": "numeric", "text": "98765"}],
                         3200, FS)
        m = _only(decode_flex(iq, FS))
        self.assertEqual(m["text"], "98765")


class TestHonestyGate(unittest.TestCase):
    def test_noise_no_false_pages(self):
        rng = np.random.default_rng(1)
        noise = (rng.standard_normal(60000) + 1j * rng.standard_normal(60000)).astype(np.complex64)
        r = decode_flex(noise, FS)
        self.assertEqual(_msgs(r), [])
        self.assertFalse(r["stats"]["sync"])

    def test_pure_tone_empty(self):
        t = np.arange(40000) / FS
        tone = np.exp(1j * 2 * np.pi * 3000.0 * t).astype(np.complex64)
        r = decode_flex(tone, FS)
        self.assertEqual(_msgs(r), [])

    def test_empty_zeros_empty(self):
        z = np.zeros(40000, dtype=np.complex64)
        r = decode_flex(z, FS)
        self.assertEqual(_msgs(r), [])

    def test_too_short_raises(self):
        with self.assertRaises(ValueError):
            decode_flex(np.zeros(8, dtype=np.complex64), FS)

    def test_false_comma_lock_no_pages(self):
        # Plant a real A / ~A SYNC1 pair followed by GARBAGE (no valid BIW). The
        # sync may lock, but the BIW BCH+marker gate must reject the frame -> no
        # hallucinated pages. (This is the POCSAG false-lock bug class.)
        rng = np.random.default_rng(9)
        bits = []
        pb = 1
        for _ in range(64):
            bits.append(pb); pb ^= 1
        for k in range(31, -1, -1):
            bits.append((A_WORD >> k) & 1)
        for k in range(31, -1, -1):
            bits.append((0xAAAAAAAA >> k) & 1)
        for k in range(31, -1, -1):
            bits.append((A_WORD_INV >> k) & 1)
        bits += list(rng.integers(0, 2, 40 * 32))     # garbage where FIW+blocks go
        from survey.flex import _bits_to_iq_2fsk
        iq = _bits_to_iq_2fsk(np.array(bits, dtype=np.uint8), 1600, FS)
        r = decode_flex(iq, FS)
        self.assertEqual(_msgs(r), [])


class TestGate(unittest.TestCase):
    def test_looks_like_flex(self):
        self.assertTrue(flex.looks_like_flex(
            {"modulation_class": "fsk", "fsk_states": 2, "fsk_shift_khz": 9.0,
             "symbol_rate_kbd": 1.6}))
        self.assertTrue(flex.looks_like_flex(
            {"modulation_class": "fsk", "fsk_states": 4, "fsk_shift_khz": 10.0,
             "symbol_rate_kbd": 3.2}))
        self.assertFalse(flex.looks_like_flex(
            {"modulation_class": "psk", "fsk_states": 1, "fsk_shift_khz": 0.0,
             "symbol_rate_kbd": 0.0}))
        self.assertFalse(flex.looks_like_flex({}))


if __name__ == "__main__":
    unittest.main()
