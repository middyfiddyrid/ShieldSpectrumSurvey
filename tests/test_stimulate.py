import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from survey.stimulate import StimulationEngine, find_responders, make_chirp  # noqa: E402


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


class ConcurrentRadio(Radio):
    """A 2T2R-style radio that keys TX once per burst via stim_tx_on/off (the B220
    path). Records the lifecycle so the test can prove the engine used it."""
    def __init__(self, **kw):
        super().__init__(**kw)
        self.tx_on_calls = 0
        self.tx_off_calls = 0
        self.tx_live = False

    def stim_mode(self):
        return "concurrent"

    def stim_tx_on(self, lo_hz, hi_hz):
        self.tx_on_calls += 1
        self.tx_live = True

    def stim_tx_off(self):
        self.tx_off_calls += 1
        self.tx_live = False

    def stim_capture_stimulated(self, center_hz, lo_hz, hi_hz, fs, n):
        assert self.tx_live, "engine must key TX before a stimulated capture"
        return super().stim_capture_stimulated(center_hz, lo_hz, hi_hz, fs, n)


class TestChirp(unittest.TestCase):
    def test_chirp_sweeps_the_band(self):
        fs, span, n = 4_000_000.0, 1_000_000.0, 1 << 16
        w = make_chirp(fs, span, n)
        self.assertEqual(w.dtype, np.complex64)
        self.assertLessEqual(float(np.max(np.abs(w))), 1.0)     # DAC headroom
        f_inst = np.diff(np.unwrap(np.angle(w))) * fs / (2 * np.pi)
        # ignore sawtooth wrap glitches via percentiles; energy spans ~±span/2
        self.assertLess(np.percentile(f_inst, 2), -span / 2 * 0.7)
        self.assertGreater(np.percentile(f_inst, 98), span / 2 * 0.7)

    def test_span_clamped_to_nyquist(self):
        w = make_chirp(2_000_000.0, 50_000_000.0, 4096)         # span >> fs
        self.assertEqual(w.size, 4096)
        self.assertLessEqual(float(np.max(np.abs(w))), 1.0)

    def test_zero_length(self):
        self.assertEqual(make_chirp(4e6, 1e6, 0).size, 0)


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

    def test_concurrent_radio_keys_tx_once_per_burst(self):
        radio = ConcurrentRadio(response="line")
        res = StimulationEngine(self.cfg, radio).probe(self.center, *self.band)
        self.assertEqual(res.verdict, "RESPONSE_DETECTED")
        self.assertEqual(res.mode, "concurrent")
        self.assertEqual(radio.tx_on_calls, 1)       # keyed ONCE for the whole burst
        self.assertEqual(radio.tx_off_calls, 1)      # and always turned back off
        self.assertFalse(radio.tx_live)

    def test_concurrent_tx_off_even_on_block(self):
        radio = ConcurrentRadio(permitted=((2400.0, 2483.5),))
        res = StimulationEngine(self.cfg, radio).probe(self.center, *self.band)
        self.assertEqual(res.verdict, "BLOCKED_BAND")
        self.assertFalse(radio.tx_live)              # never keyed TX out of band


if __name__ == "__main__":
    unittest.main()
