import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from survey.sweep.detector import Accumulator, detect_signals  # noqa: E402


def make_trace(n=112, lo=430.0, hi=440.0, floor=-105.0, carriers=()):
    """carriers: list of (freq_mhz, peak_dbm, half_width_bins)"""
    freqs = [lo + (hi - lo) * i / (n - 1) for i in range(n)]
    rng = random.Random(42)
    amps = [floor + rng.uniform(-1.5, 1.5) for _ in range(n)]
    for f, p, hw in carriers:
        ci = min(range(n), key=lambda i: abs(freqs[i] - f))
        for j in range(max(0, ci - hw), min(n, ci + hw + 1)):
            roll = p - 6.0 * abs(j - ci)
            amps[j] = max(amps[j], roll)
    return freqs, amps


class TestDetector(unittest.TestCase):
    def test_clean_floor_no_signals(self):
        freqs, amps = make_trace()
        floor, sigs = detect_signals(freqs, amps, amps, [0] * len(freqs), passes=10)
        self.assertEqual(sigs, [])
        self.assertAlmostEqual(floor, -105.0, delta=2.0)

    def test_single_carrier_detected_with_center_and_bw(self):
        freqs, amps = make_trace(carriers=[(433.92, -60.0, 2)])
        hits = [10 if a > -95 else 0 for a in amps]
        floor, sigs = detect_signals(freqs, amps, amps, hits, passes=10)
        self.assertEqual(len(sigs), 1)
        s = sigs[0]
        self.assertAlmostEqual(s.center_mhz, 433.92, delta=0.15)
        self.assertGreaterEqual(s.peak_dbm, -61.0)
        self.assertAlmostEqual(s.persistence, 1.0)
        self.assertGreater(s.bw_khz, 0)

    def test_two_carriers_not_merged(self):
        freqs, amps = make_trace(carriers=[(431.5, -55.0, 1), (438.5, -58.0, 1)])
        hits = [5 if a > -95 else 0 for a in amps]
        _, sigs = detect_signals(freqs, amps, amps, hits, passes=5)
        self.assertEqual(len(sigs), 2)

    def test_adjacent_bins_merge_into_one(self):
        freqs, amps = make_trace(carriers=[(434.0, -50.0, 4)])
        hits = [8 if a > -95 else 0 for a in amps]
        _, sigs = detect_signals(freqs, amps, amps, hits, passes=8)
        self.assertEqual(len(sigs), 1)

    def test_transient_in_maxhold_only(self):
        """A burst present in max-peak but not the average must still be detected,
        with low persistence."""
        freqs, avg = make_trace()
        _, maxp = make_trace(carriers=[(436.0, -55.0, 1)])
        hits = [1 if m > -95 else 0 for m in maxp]
        _, sigs = detect_signals(freqs, avg, maxp, hits, passes=20)
        self.assertEqual(len(sigs), 1)
        self.assertLessEqual(sigs[0].persistence, 0.1)

    def test_threshold_respected(self):
        freqs, amps = make_trace(carriers=[(434.0, -96.0, 1)])  # only ~9 dB over floor
        _, sigs = detect_signals(freqs, amps, amps, [0] * len(freqs), passes=1,
                                 threshold_db=12.0)
        self.assertEqual(sigs, [])


class TestAccumulator(unittest.TestCase):
    def test_running_stats(self):
        freqs, base = make_trace()
        acc = Accumulator(freqs)
        _, hot = make_trace(carriers=[(433.92, -60.0, 2)])
        for i in range(10):
            acc.add_sweep(hot if i % 2 == 0 else base)   # signal present half the time
        d = acc.to_dict()
        self.assertEqual(d["passes"], 10)
        peak_bin = max(range(len(freqs)), key=lambda i: d["maxpeak"][i])
        self.assertAlmostEqual(freqs[peak_bin], 433.92, delta=0.15)
        self.assertAlmostEqual(d["hits"][peak_bin], 5, delta=1)
        # average sits between floor and peak for an intermittent signal
        self.assertLess(d["avg"][peak_bin], d["maxpeak"][peak_bin] - 2)
        self.assertGreater(d["avg"][peak_bin], -105.0)


if __name__ == "__main__":
    unittest.main()
