"""Auto-router + interpretation. The router orchestrates the real decoders
(unit-tested in their own files); here we prove routing + the plain-English
verdict, not the DSP."""

import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from survey.decode_router import route_decode, interpret      # noqa: E402
from survey.identify import classify_modulation               # noqa: E402
from survey.ook import ook_encode                              # noqa: E402
from survey.tones import dtmf_encode                           # noqa: E402


def _feats(**kw):
    base = {"occupied_bw_khz": 12, "snr_db": 20.0, "continuity": "continuous",
            "fsk_states": 0, "fsk_shift_khz": 0.0, "distinct_tones": 1,
            "spectral_flatness": 0.1}
    base.update(kw)
    return base


class TestClassifyModulation(unittest.TestCase):
    def test_weak_bursty_keyed_is_ook_not_wideband(self):
        # the 72.02 MHz failure: weak + bursty + smeared-wide BW MUST be OOK, never OFDM
        f = _feats(snr_db=9.7, continuity="bursty", occupied_bw_khz=2177,
                   spectral_flatness=0.5)
        self.assertEqual(classify_modulation(f), "ook")

    def test_very_weak_is_unknown(self):
        self.assertEqual(classify_modulation(_feats(snr_db=4.0)), "unknown")

    def test_strong_wide_flat_continuous_is_ofdm(self):
        f = _feats(snr_db=25, continuity="continuous", occupied_bw_khz=18000,
                   spectral_flatness=0.6)
        self.assertEqual(classify_modulation(f), "ofdm_wideband")

    def test_weak_wide_is_not_promoted_to_ofdm(self):
        # same shape but weak -> must NOT claim OFDM
        f = _feats(snr_db=9.0, continuity="continuous", occupied_bw_khz=18000,
                   spectral_flatness=0.6)
        self.assertNotEqual(classify_modulation(f), "ofdm_wideband")

    def test_narrow_continuous_is_analog_voice(self):
        self.assertEqual(
            classify_modulation(_feats(snr_db=20, occupied_bw_khz=12)), "analog_voice")


class TestRouteDecode(unittest.TestCase):
    def test_prefer_ook_decodes(self):
        payload = [1, 0, 1, 1, 0, 0, 1, 0, 1, 1, 1, 0,
                   0, 1, 0, 1, 0, 0, 1, 1, 1, 0, 0, 1]
        iq = ook_encode(payload, "PWM", 300.0, 1_000_000.0, repeats=5, modulation="OOK/ASK")
        r = route_decode(iq, 1_000_000.0, None, prefer="ook")
        self.assertTrue(r["messages"])
        self.assertTrue((r["protocol"] or "").upper().startswith("OOK"))

    def test_prefer_dtmf_decodes(self):
        iq = dtmf_encode("123A", 8000.0)
        r = route_decode(iq, 8000.0, None, prefer="dtmf")
        self.assertTrue(r["messages"])
        self.assertIn("1", r["messages"][0].get("digits", ""))

    def test_noise_decodes_to_nothing(self):
        rng = np.random.default_rng(3)
        n = 200_000
        iq = (rng.standard_normal(n) + 1j * rng.standard_normal(n)).astype(np.complex64)
        r = route_decode(iq, 1_000_000.0, None, prefer="ook")
        self.assertEqual(r["messages"], [])     # honesty gate held
        self.assertIsNone(r["protocol"])

    def test_auto_gate_no_match_on_wideband(self):
        # an OFDM/wideband-digital feature set matches no decoder's signature
        feats = {"modulation_class": "ofdm_wideband", "occupied_bw_khz": 20000,
                 "fsk_states": 0, "fsk_shift_khz": 0, "continuity": "continuous",
                 "duty_cycle": 1.0, "symbol_rate_kbd": 0, "signal_present": True,
                 "snr_db": 30, "distinct_tones": 50, "spectral_flatness": 0.8}
        r = route_decode(np.zeros(8192, dtype=np.complex64), 1_000_000.0, feats)
        self.assertEqual(r["messages"], [])


class TestInterpret(unittest.TestCase):
    def test_no_feats(self):
        self.assertEqual(interpret(None)["readable"], "unknown")

    def test_noise(self):
        self.assertEqual(interpret({"signal_present": False})["readable"], "none")

    def test_decoded_wins(self):
        v = interpret({"signal_present": True, "modulation_class": "fsk"}, "POCSAG")
        self.assertEqual(v["readable"], "decoded")

    def test_analog_voice_says_listen(self):
        v = interpret({"signal_present": True, "modulation_class": "analog_voice",
                       "occupied_bw_khz": 12})
        self.assertEqual(v["readable"], "listen")
        self.assertIn("LISTEN", v["summary"])

    def test_wideband_digital_says_encrypted(self):
        v = interpret({"signal_present": True, "modulation_class": "ofdm_wideband",
                       "occupied_bw_khz": 20000})
        self.assertEqual(v["readable"], "encrypted")


if __name__ == "__main__":
    unittest.main()
