"""Unit tests for sigidwiki-style identification — synthetic IQ feature
extraction and candidate matching (no hardware)."""

import os
import unittest

import numpy as np

from survey.identify import (classify_modulation, identify, iq_features,
                             load_reference)

REF_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "survey", "sigidwiki_ref.json")


def cw(fs, n, f0=50e3, noise=1e-3):
    t = np.arange(n) / fs
    return (np.exp(2j * np.pi * f0 * t)
            + noise * (np.random.randn(n) + 1j * np.random.randn(n))).astype(np.complex64)


def fsk(fs, n, shift=80e3, sym=120, noise=1e-3):
    t = np.arange(n) / fs
    phase = np.zeros(n)
    f = np.zeros(n)
    for i in range(0, n, sym):
        f[i:i + sym] = (shift / 2) if (i // sym) % 2 else (-shift / 2)
    phase = 2 * np.pi * np.cumsum(f) / fs
    return (np.exp(1j * phase)
            + noise * (np.random.randn(n) + 1j * np.random.randn(n))).astype(np.complex64)


def ook(fs, n, f0=200e3, on=150, off=450, noise=1e-3):
    t = np.arange(n) / fs
    car = np.exp(2j * np.pi * f0 * t)
    gate = np.zeros(n)
    for i in range(0, n, on + off):
        gate[i:i + on] = 1.0
    return (car * gate
            + noise * (np.random.randn(n) + 1j * np.random.randn(n))).astype(np.complex64)


def wideband_noise(fs, n):
    return (np.random.randn(n) + 1j * np.random.randn(n)).astype(np.complex64)


class TestFeatureExtraction(unittest.TestCase):
    def setUp(self):
        np.random.seed(0)
        self.fs = 4e6
        self.n = 65536

    def test_cw_is_continuous_narrow(self):
        f = iq_features(cw(self.fs, self.n), self.fs)
        self.assertEqual(f["continuity"], "continuous")
        self.assertLess(f["occupied_bw_khz"], 300)
        self.assertEqual(f["modulation_class"], "analog_voice")

    def test_fsk_detected(self):
        f = iq_features(fsk(self.fs, self.n), self.fs)
        self.assertGreaterEqual(f["fsk_states"], 2)
        self.assertGreater(f["fsk_shift_khz"], 20)
        self.assertEqual(f["modulation_class"], "fsk")

    def test_ook_is_bursty(self):
        f = iq_features(ook(self.fs, self.n), self.fs)
        self.assertEqual(f["continuity"], "bursty")
        self.assertEqual(f["modulation_class"], "ook")

    def test_pure_noise_is_not_a_signal(self):
        # an empty channel (flat noise) must be reported as noise_floor, NOT
        # confidently classified as a wideband OFDM signal.
        f = iq_features(wideband_noise(10e6, self.n), 10e6)
        self.assertGreater(f["spectral_flatness"], 0.4)
        self.assertFalse(f["signal_present"])
        self.assertEqual(f["modulation_class"], "noise_floor")

    def test_too_few_samples_raises(self):
        with self.assertRaises(ValueError):
            iq_features(np.zeros(10, dtype=np.complex64), 1e6)


class TestClassify(unittest.TestCase):
    def test_ofdm_class(self):
        self.assertEqual(classify_modulation(
            {"occupied_bw_khz": 20000, "spectral_flatness": 0.8, "distinct_tones": 30,
             "fsk_states": 0, "fsk_shift_khz": 0, "continuity": "bursty"}), "ofdm_wideband")


class TestIdentify(unittest.TestCase):
    def setUp(self):
        self.ref = load_reference(REF_PATH)

    def test_reference_loads(self):
        self.assertGreater(len(self.ref), 10)
        for r in self.ref:
            self.assertIn("freq_min_mhz", r)
            self.assertIn("class", r)
            self.assertTrue(r["url"].startswith("https://"))

    def test_wifi_ranks_for_wide_flat_24ghz(self):
        feats = {"occupied_bw_khz": 18000, "duty_cycle": 0.4, "continuity": "bursty",
                 "spectral_flatness": 0.8, "distinct_tones": 30, "fsk_states": 0,
                 "fsk_shift_khz": 0, "symbol_rate_kbd": 0, "modulation_class": "ofdm_wideband"}
        cands = identify(2_440e6, feats, self.ref)
        self.assertTrue(any("Wi-Fi" in c["name"] and "2.4" in c["name"] for c in cands[:2]))
        self.assertTrue(cands[0]["in_band"])

    def test_ook_remote_ranks_for_bursty_433(self):
        feats = {"occupied_bw_khz": 30, "duty_cycle": 0.2, "continuity": "bursty",
                 "spectral_flatness": 0.05, "distinct_tones": 1, "fsk_states": 1,
                 "fsk_shift_khz": 0, "symbol_rate_kbd": 5, "modulation_class": "ook"}
        cands = identify(433.92e6, feats, self.ref)
        self.assertTrue(any("OOK" in c["name"] for c in cands[:3]))

    def test_frequency_only_mode_without_iq(self):
        cands = identify(2_440e6, None, self.ref)
        self.assertTrue(cands and cands[0]["in_band"])
        self.assertTrue(any("frequency/bandwidth only" in r for r in cands[0]["reasons"]))


if __name__ == "__main__":
    unittest.main()
