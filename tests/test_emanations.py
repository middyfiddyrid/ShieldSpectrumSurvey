import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from survey.emanations import (  # noqa: E402
    EmanationScanner, Spike, average_psd_db, detect_spikes, find_harmonic_combs,
    smooth_floor_db, subtract_baseline_db)


def _spikes(mhz_list, prom=12.0):
    return [Spike(freq_mhz=f, prominence_db=prom) for f in mhz_list]


class TestDSP(unittest.TestCase):
    def test_average_pulls_tone_out_of_noise(self):
        rng = np.random.default_rng(0)
        n = 512
        tone = np.full(n, -100.0)
        tone[100] = -80.0
        psds = [tone + rng.standard_normal(n) * 6 for _ in range(64)]
        avg = average_psd_db(psds)
        # the tone bin stands clearly above its neighbours after averaging
        self.assertGreater(avg[100] - np.median(avg), 12.0)

    def test_smooth_floor_ignores_spikes(self):
        psd = np.full(201, -100.0)
        psd[100] = -60.0                      # a big spike
        floor = smooth_floor_db(psd, win=51)
        self.assertLess(floor[100], -95.0)    # median floor unmoved by the spike

    def test_detect_spikes_above_floor(self):
        freqs = np.arange(201) * 1e6
        psd = np.full(201, -100.0)
        psd[50] = -85.0                       # +15 dB
        psd[150] = -98.0                      # +2 dB, below gate
        floor = smooth_floor_db(psd)
        sp = detect_spikes(freqs, psd, floor, prominence_db=8.0)
        self.assertEqual(len(sp), 1)
        self.assertAlmostEqual(sp[0].freq_mhz, 50.0)

    def test_baseline_subtraction_removes_common_signal(self):
        base = np.array([-100.0, -60.0, -100.0])      # ambient tone at bin 1
        test = np.array([-100.0, -60.0, -70.0])       # same tone + a NEW one at bin 2
        resid = subtract_baseline_db(test, base)
        self.assertLess(resid[1] - resid[0], 3.0)     # common tone cancelled
        self.assertGreater(resid[2] - resid[0], 20.0) # new tone survives


class TestHarmonicComb(unittest.TestCase):
    def test_clean_comb_found_with_clock(self):
        combs = find_harmonic_combs(_spikes([24, 48, 72, 96, 120]))
        self.assertEqual(len(combs), 1)
        self.assertAlmostEqual(combs[0]["clock_hz"] / 1e6, 24.0, delta=0.1)
        self.assertEqual(combs[0]["n"], 5)

    def test_comb_with_one_missing_harmonic(self):
        combs = find_harmonic_combs(_spikes([24, 48, 96, 120]))   # 72 missing
        self.assertTrue(combs)
        self.assertAlmostEqual(combs[0]["clock_hz"] / 1e6, 24.0, delta=0.2)

    def test_random_tones_are_not_a_comb(self):
        combs = find_harmonic_combs(_spikes([13.3, 88.1, 201.7, 455.9]))
        self.assertEqual(combs, [])

    def test_two_devices_two_combs(self):
        combs = find_harmonic_combs(_spikes([24, 48, 72, 96,        # 24 MHz device
                                             27, 54, 81, 108]))     # 27 MHz device
        clocks = sorted(round(c["clock_hz"] / 1e6) for c in combs)
        self.assertEqual(clocks, [24, 27])

    def test_two_spikes_is_not_enough(self):
        self.assertEqual(find_harmonic_combs(_spikes([24, 48])), [])


class SyntheticRadio:
    """Emits IQ = noise + optional evenly-spaced emanation tones. `clock_hz` set ->
    a comb of tones at multiples of the clock within each captured window."""
    def __init__(self, clock_hz=None, tone_amp=0.6):
        self.clock_hz = clock_hz
        self.tone_amp = tone_amp

    def capture_iq(self, center_hz, fs, n):
        rng = np.random.default_rng()
        iq = (rng.standard_normal(n) + 1j * rng.standard_normal(n)).astype(np.complex64)
        if self.clock_hz:
            t = np.arange(n)
            for k in range(1, 60):
                f = k * self.clock_hz
                off = f - center_hz                      # baseband offset in this window
                if -fs / 2 < off < fs / 2:
                    iq += (self.tone_amp * np.exp(2j * np.pi * off * t / fs)).astype(np.complex64)
        return iq


class FakeCfg:
    _v = {("emanations", "sample_rate"): 40_000_000,
          ("emanations", "fft_size"): 2048,
          ("emanations", "dwell_samples"): 1 << 14,
          ("emanations", "averages"): 4,
          ("emanations", "prominence_gate_db"): 8.0,
          ("emanations", "min_harmonics"): 3,
          ("emanations", "floor_window_bins"): 41}

    def get(self, s, k, default=None):
        return self._v.get((s, k), default)


class TestScanner(unittest.TestCase):
    def setUp(self):
        self.cfg = FakeCfg()

    def test_emitter_with_comb_is_flagged(self):
        eng = EmanationScanner(self.cfg, SyntheticRadio(clock_hz=24_000_000))
        res = eng.scan(60_000_000, 200_000_000)          # covers 72/96/120/144/168/192
        self.assertEqual(res.verdict, "SUSPECTED_EMITTER")
        self.assertTrue(res.combs)
        self.assertAlmostEqual(res.combs[0].clock_mhz, 24.0, delta=0.5)
        self.assertIn("camera", res.combs[0].device_hint.lower())

    def test_quiet_room_is_none_but_honest(self):
        eng = EmanationScanner(self.cfg, SyntheticRadio(clock_hz=None))
        res = eng.scan(60_000_000, 140_000_000)
        self.assertEqual(res.verdict, "NONE")
        self.assertIn("not proof", res.plain)

    def test_result_serializes(self):
        eng = EmanationScanner(self.cfg, SyntheticRadio(clock_hz=24_000_000))
        d = eng.scan(60_000_000, 160_000_000).to_dict()
        self.assertIn("combs", d)
        self.assertIsInstance(d["combs"], list)


if __name__ == "__main__":
    unittest.main()
