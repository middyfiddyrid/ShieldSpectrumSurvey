"""Unit tests for the software NLJD — pure DSP (harmonic line extraction, the
2f/3f decision rule, baseline subtraction, honesty gate) driven by a SYNTHETIC
radio that fabricates harmonic IQ. No UHD, no hardware.

The synthetic radio models the physics we rely on:
  - an EMPTY box returns only a small fixed self-leakage line at 2f and 3f
    (the SDR's own residual harmonics after the TX filter);
  - a SEMICONDUCTOR object adds a STRONG 2f line and a WEAKER 3f line (even-order
    dominant, asymmetric diode I-V);
  - a CORROSION/MOM object adds a STRONG 3f line and a WEAKER 2f line (odd-order
    dominant, symmetric back-to-back I-V);
  - nothing present adds nothing -> must read NO_JUNCTION.
"""

import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from survey.nljd import (NLJDEngine, classify, extract_line_power,  # noqa: E402
                         F0_HZ, TX_BB_OFFSET_HZ)


class FakeCfg:
    def __init__(self, d):
        self.d = d

    def get(self, *keys, default=None):
        cur = self.d
        for k in keys:
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        return cur

    def set_and_save(self, keys, value):
        cur = self.d
        for k in keys[:-1]:
            cur = cur.setdefault(k, {})
        cur[keys[-1]] = value


FAST = FakeCfg({"nljd": {
    "f0_hz": F0_HZ, "tx_bb_offset_hz": TX_BB_OFFSET_HZ,
    "rx_sample_rate": 2_000_000, "fft_size": 1 << 14,
    "dwell_samples": 1 << 14, "snr_gate_db": 6.0, "averages": 2,
    "ratio_hysteresis_db": 1.0, "require_hw_filters": True,
}})


class FakeNLJDRadio:
    """Synthetic NLJD radio. `profile` sets the per-harmonic line amplitude (linear
    voltage) the object adds ON TOP of a fixed empty-box self-leakage floor."""

    SELF_LEAK = {2: 0.010, 3: 0.010}   # empty-box residual line amplitude

    def __init__(self, cfg, profile=None, hw=True, noise=0.02):
        self.cfg = cfg
        self.profile = profile or {2: 0.0, 3: 0.0}
        self._hw = hw
        self._noise = noise
        self._tx_on = False
        self.fs = cfg.get("nljd", "rx_sample_rate")
        self.nfft = cfg.get("nljd", "fft_size")

    def nljd_hw_filters_attested(self):
        return self._hw

    def nljd_tx_cw(self, f0_hz, bb_offset_hz):
        self._tx_on = True

    def nljd_tx_off(self):
        self._tx_on = False

    def nljd_capture_at(self, rx_lo_hz, fs, n_samples):
        rng = np.random.default_rng(int(rx_lo_hz) % (2**31))
        iq = (rng.normal(0, self._noise, n_samples)
              + 1j * rng.normal(0, self._noise, n_samples)).astype(np.complex64)
        if not self._tx_on:
            return iq
        # which harmonic is this dwell? recover N from rx_lo ~ N*(f0+off)
        emit = F0_HZ + TX_BB_OFFSET_HZ
        n = int(round((rx_lo_hz) / emit))
        bin_hz = self.fs / self.nfft
        want = round((self.fs * 0.10) / bin_hz) * bin_hz
        amp = self.SELF_LEAK.get(n, 0.0) + self.profile.get(n, 0.0)
        t = np.arange(n_samples)
        iq += (amp * np.exp(2j * np.pi * want * t / self.fs)).astype(np.complex64)
        return iq


class TestLineExtraction(unittest.TestCase):
    def test_line_stands_above_noise(self):
        fs, nfft = 2_000_000, 1 << 14
        bin_hz = fs / nfft
        want = round((fs * 0.10) / bin_hz) * bin_hz
        t = np.arange(nfft)
        iq = (0.1 * np.exp(2j * np.pi * want * t / fs)
              + 0.01 * (np.random.default_rng(1).normal(0, 1, nfft)
                        + 1j * np.random.default_rng(2).normal(0, 1, nfft)))
        line_db, floor_db, snr = extract_line_power(iq.astype(np.complex64), fs, want, nfft)
        self.assertGreater(snr, 20.0)   # a clean strong line is well above floor

    def test_empty_capture_low_snr(self):
        fs, nfft = 2_000_000, 1 << 14
        bin_hz = fs / nfft
        want = round((fs * 0.10) / bin_hz) * bin_hz
        rng = np.random.default_rng(3)
        iq = (0.02 * rng.normal(0, 1, nfft) + 1j * 0.02 * rng.normal(0, 1, nfft)).astype(np.complex64)
        _, _, snr = extract_line_power(iq, fs, want, nfft)
        self.assertLess(snr, 10.0)      # noise-only -> no strong line


class TestDecisionRule(unittest.TestCase):
    def test_semiconductor(self):
        v, ratio = classify(delta_2f_db=12.0, delta_3f_db=4.0, confident=True)
        self.assertEqual(v, "SEMICONDUCTOR")
        self.assertGreater(ratio, 0)

    def test_false_junction(self):
        v, ratio = classify(delta_2f_db=3.0, delta_3f_db=11.0, confident=True)
        self.assertEqual(v, "FALSE_JUNCTION")
        self.assertLess(ratio, 0)

    def test_not_confident_is_no_junction(self):
        v, _ = classify(delta_2f_db=12.0, delta_3f_db=4.0, confident=False)
        self.assertEqual(v, "NO_JUNCTION")


class TestEngineEndToEnd(unittest.TestCase):
    """The radio holds only the EMPTY-box self-leakage while the baseline is taken,
    then the object's harmonic profile is inserted before measure() — exactly the
    real workflow: baseline empty box, then place object and measure the delta."""

    def _run(self, object_profile, hw=True):
        radio = FakeNLJDRadio(FAST, profile={2: 0.0, 3: 0.0}, hw=hw)
        eng = NLJDEngine(FAST, radio)
        eng.measure_baseline("empty")     # empty box
        radio.profile = object_profile    # operator places the object
        return eng.measure()

    def test_semiconductor_object(self):
        v = self._run({2: 0.08, 3: 0.02})   # strong 2f, weak 3f
        self.assertEqual(v.verdict, "SEMICONDUCTOR")
        self.assertTrue(v.confident)
        self.assertGreater(v.ratio_db, 0)

    def test_corrosion_object(self):
        v = self._run({2: 0.02, 3: 0.08})   # strong 3f, weak 2f
        self.assertEqual(v.verdict, "FALSE_JUNCTION")
        self.assertLess(v.ratio_db, 0)

    def test_empty_object_no_hallucination(self):
        v = self._run({2: 0.0, 3: 0.0})     # nothing added over baseline
        self.assertEqual(v.verdict, "NO_JUNCTION")
        self.assertFalse(v.confident)

    def test_missing_hw_filters_blocks(self):
        v = self._run({2: 0.08, 3: 0.02}, hw=False)
        self.assertEqual(v.verdict, "BLOCKED")
        self.assertFalse(v.confident)


if __name__ == "__main__":
    unittest.main()
