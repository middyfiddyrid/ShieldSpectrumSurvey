import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from survey.stimulate import StimulationEngine, find_responders  # noqa: E402


class FakeCfg:
    """Minimal cfg.get(section, key, default=...) shim; small dwell for fast tests."""
    _vals = {("stimulate", "rx_sample_rate"): 4_000_000,
             ("stimulate", "fft_size"): 2048,
             ("stimulate", "dwell_samples"): 1 << 15,
             ("stimulate", "averages"): 3}

    def get(self, section, key, default=None):
        return self._vals.get((section, key), default)


def _noise(n, sigma=1.0):
    rng = np.random.default_rng()
    return (rng.standard_normal(n) + 1j * rng.standard_normal(n)).astype(np.complex64) * sigma


def _tone(n, fs, f_off_hz, amp=2.5):
    t = np.arange(n)
    return (amp * np.exp(2j * np.pi * f_off_hz * t / fs)).astype(np.complex64)


class Radio:
    """Synthetic stimulation radio. `stim_response` controls what the stimulated
    capture adds: 'line' = a device line at +500 kHz, 'floor' = a uniform +10 dB
    floor rise (no line), 'none' = nothing, 'always' = a line present when quiet too."""
    def __init__(self, permitted=((900.0, 930.0),), response="line"):
        self._permitted = permitted
        self.response = response
        self.fs = 4_000_000

    def stim_mode(self):
        return "interleaved"

    def stim_permitted_bands(self):
        return list(self._permitted)

    def stim_capture_quiet(self, center_hz, fs, n):
        iq = _noise(n)
        if self.response == "always":
            iq = iq + _tone(n, fs, 500_000)
        return iq

    def stim_capture_stimulated(self, center_hz, lo_hz, hi_hz, fs, n):
        if self.response == "none":
            return _noise(n)
        if self.response == "floor":
            return _noise(n, sigma=3.16)              # +10 dB, but flat (no line)
        return _noise(n) + _tone(n, fs, 500_000)      # 'line' and 'always'


class TestFindResponders(unittest.TestCase):
    def test_uniform_floor_rise_is_not_a_responder(self):
        n, fs = 1 << 15, 4_000_000
        freqs = np.fft.fftshift(np.fft.fftfreq(2048, 1 / fs))
        quiet = np.full(2048, -100.0)
        stim = quiet + 10.0                           # whole band up 10 dB, no line
        self.assertEqual(find_responders(freqs, quiet, stim), [])

    def test_new_line_is_a_responder(self):
        freqs = np.fft.fftshift(np.fft.fftfreq(2048, 1 / 4_000_000))
        quiet = np.full(2048, -100.0)
        stim = quiet.copy()
        stim[1200] = -70.0                            # a prominent new spike
        r = find_responders(freqs, quiet, stim)
        self.assertEqual(len(r), 1)
        self.assertGreaterEqual(r[0].delta_db, 6.0)

    def test_line_present_when_quiet_is_not_a_response(self):
        freqs = np.fft.fftshift(np.fft.fftfreq(2048, 1 / 4_000_000))
        quiet = np.full(2048, -100.0)
        quiet[1200] = -70.0                           # already there when quiet
        stim = quiet.copy()
        stim[1200] = -68.0                            # barely higher, but pre-existing
        self.assertEqual(find_responders(freqs, quiet, stim), [])


class TestEngine(unittest.TestCase):
    def setUp(self):
        self.cfg = FakeCfg()
        self.center = 915_000_000
        self.band = (902_000_000, 928_000_000)

    def test_device_response_detected(self):
        eng = StimulationEngine(self.cfg, Radio(response="line"))
        res = eng.probe(self.center, *self.band)
        self.assertEqual(res.verdict, "RESPONSE_DETECTED")
        self.assertTrue(res.confident)
        self.assertTrue(res.responders)
        # responder near center + 500 kHz
        self.assertAlmostEqual(res.responders[0].freq_mhz, 915.5, delta=0.1)

    def test_no_response_is_honest_about_null(self):
        eng = StimulationEngine(self.cfg, Radio(response="none"))
        res = eng.probe(self.center, *self.band)
        self.assertEqual(res.verdict, "NO_RESPONSE")
        self.assertFalse(res.confident)
        self.assertIn("not a clean bill", res.plain)

    def test_flat_floor_rise_does_not_false_trigger(self):
        eng = StimulationEngine(self.cfg, Radio(response="floor"))
        res = eng.probe(self.center, *self.band)
        self.assertEqual(res.verdict, "NO_RESPONSE")

    def test_always_on_line_not_flagged(self):
        eng = StimulationEngine(self.cfg, Radio(response="always"))
        res = eng.probe(self.center, *self.band)
        self.assertEqual(res.verdict, "NO_RESPONSE")

    def test_out_of_band_is_blocked_without_override(self):
        eng = StimulationEngine(self.cfg, Radio(permitted=((2400.0, 2483.5),)))
        res = eng.probe(self.center, *self.band)          # 915 not in 2.4 GHz permit
        self.assertEqual(res.verdict, "BLOCKED_BAND")
        self.assertFalse(res.band_permitted)
        self.assertIn("authorize", res.plain.lower())

    def test_override_transmits_out_of_band_with_warning(self):
        eng = StimulationEngine(self.cfg, Radio(permitted=((2400.0, 2483.5),),
                                                response="none"))
        res = eng.probe(self.center, *self.band, override_band=True)
        self.assertNotEqual(res.verdict, "BLOCKED_BAND")
        self.assertTrue(any("OVERRIDE" in w for w in res.warnings))


if __name__ == "__main__":
    unittest.main()
