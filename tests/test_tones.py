"""Unit tests for the tone decoders — DTMF and CW/Morse.

Synthesize a known signal with the encoders in survey.tones (which share the same
DTMF tone table / Morse table as the decoders, so a wrong table fails loudly), then
round-trip through decode_dtmf / decode_cw and assert EXACT recovery of the payload.
Plus honesty-gate tests: noise and an empty channel must decode to NOTHING. All
headless, pure numpy. fs=8000 keeps the synth arrays small (voiceband is well under
the Nyquist for DTMF's 1633 Hz and a 700 Hz CW sidetone).
"""

import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from survey import tones
from survey.tones import (decode_dtmf, decode_cw, dtmf_encode, cw_encode,
                          looks_like_dtmf, looks_like_cw)

FS = 8000.0


# ===================================================================== DTMF =====
class TestDTMF(unittest.TestCase):
    def _digits(self, result):
        msgs = result["messages"]
        self.assertEqual(len(msgs), 1, f"expected 1 message, got {msgs}")
        return msgs[0]["digits"]

    def test_simple_string(self):
        iq = dtmf_encode("1234567890", FS)
        self.assertEqual(self._digits(decode_dtmf(iq, FS)), "1234567890")

    def test_letters_and_symbols(self):
        iq = dtmf_encode("ABCD*#", FS)
        self.assertEqual(self._digits(decode_dtmf(iq, FS)), "ABCD*#")

    def test_repeated_digits_kept_separate(self):
        # the silence gap between presses must split equal glyphs (not collapse 111)
        iq = dtmf_encode("11122", FS)
        self.assertEqual(self._digits(decode_dtmf(iq, FS)), "11122")

    def test_phone_number(self):
        iq = dtmf_encode("18005551234", FS)
        self.assertEqual(self._digits(decode_dtmf(iq, FS)), "18005551234")

    def test_each_glyph_individually(self):
        for g in "0123456789ABCD*#":
            iq = dtmf_encode(g, FS)
            self.assertEqual(self._digits(decode_dtmf(iq, FS)), g, f"glyph {g}")

    def test_survives_noise(self):
        iq = dtmf_encode("5551212", FS, noise=0.05, seed=4)
        self.assertEqual(self._digits(decode_dtmf(iq, FS)), "5551212")

    def test_noise_only_empty(self):
        rng = np.random.default_rng(1)
        n = 16000
        noise = (rng.standard_normal(n) + 1j * rng.standard_normal(n)).astype(np.complex64)
        self.assertEqual(decode_dtmf(noise, FS)["messages"], [])

    def test_silence_empty(self):
        iq = np.ones(16000, dtype=np.complex64)        # steady carrier, no tones
        self.assertEqual(decode_dtmf(iq, FS)["messages"], [])

    def test_single_tone_no_pair_empty(self):
        # one low tone alone is NOT a valid DTMF symbol -> nothing
        t = np.arange(16000) / FS
        audio = 0.5 * np.sin(2 * np.pi * 770.0 * t)
        iq = (1.0 + audio).astype(np.complex64)
        self.assertEqual(decode_dtmf(iq, FS)["messages"], [])

    def test_result_shape(self):
        r = decode_dtmf(dtmf_encode("42", FS), FS)
        self.assertEqual(r["protocol"], "DTMF")
        self.assertIn("note", r)
        self.assertIsInstance(r["messages"][0], dict)


# ================================================================== CW / MORSE ==
class TestCW(unittest.TestCase):
    def _msg(self, result):
        msgs = result["messages"]
        self.assertEqual(len(msgs), 1, f"expected 1 message, got {msgs}")
        return msgs[0]

    def test_short_word(self):
        iq = cw_encode("CQ", 20, FS)
        m = self._msg(decode_cw(iq, FS))
        self.assertEqual(m["text"], "CQ")

    def test_sos(self):
        iq = cw_encode("SOS", 25, FS)
        self.assertEqual(self._msg(decode_cw(iq, FS))["text"], "SOS")

    def test_two_words(self):
        iq = cw_encode("HELLO WORLD", 18, FS)
        self.assertEqual(self._msg(decode_cw(iq, FS))["text"], "HELLO WORLD")

    def test_digits_and_punct(self):
        iq = cw_encode("DE W1AW", 22, FS)
        self.assertEqual(self._msg(decode_cw(iq, FS))["text"], "DE W1AW")

    def test_wpm_estimate(self):
        iq = cw_encode("PARIS", 20, FS)
        m = self._msg(decode_cw(iq, FS))
        self.assertEqual(m["text"], "PARIS")
        self.assertLess(abs(m["wpm"] - 20.0), 3.0)     # within a few wpm of the truth

    def test_survives_noise(self):
        iq = cw_encode("TEST", 18, FS, noise=0.03, seed=2)
        self.assertEqual(self._msg(decode_cw(iq, FS))["text"], "TEST")

    def test_noise_only_empty(self):
        rng = np.random.default_rng(3)
        n = 40000
        noise = (rng.standard_normal(n) + 1j * rng.standard_normal(n)).astype(np.complex64)
        self.assertEqual(decode_cw(noise, FS)["messages"], [])

    def test_steady_carrier_empty(self):
        # a continuous unmodulated carrier (no keying) -> no Morse
        iq = np.ones(40000, dtype=np.complex64)
        self.assertEqual(decode_cw(iq, FS)["messages"], [])

    def test_result_shape(self):
        r = decode_cw(cw_encode("OK", 20, FS), FS)
        self.assertEqual(r["protocol"], "CW/Morse")
        self.assertIn("note", r)
        self.assertIn("wpm", r["messages"][0])


# ===================================================================== GATES ====
class TestGates(unittest.TestCase):
    def test_looks_like_dtmf(self):
        self.assertTrue(looks_like_dtmf(
            {"occupied_bw_khz": 3.0, "distinct_tones": 4, "modulation_class": "unknown",
             "continuity": "intermittent"}))
        self.assertFalse(looks_like_dtmf(
            {"occupied_bw_khz": 20000.0, "distinct_tones": 1,
             "modulation_class": "ofdm_wideband", "continuity": "continuous"}))
        self.assertFalse(looks_like_dtmf({}))

    def test_looks_like_cw(self):
        self.assertTrue(looks_like_cw(
            {"occupied_bw_khz": 0.3, "modulation_class": "ook", "continuity": "bursty"}))
        self.assertFalse(looks_like_cw(
            {"occupied_bw_khz": 200.0, "modulation_class": "fsk", "continuity": "continuous"}))
        self.assertFalse(looks_like_cw({}))


if __name__ == "__main__":
    unittest.main()
