"""Persistence detection — finding a carrier that sits AT the noise floor.

The operator was marking, by eye, signals 0-2 dB over the segment floor (one
segment: floor -67.3, marks from -69.2 to -65.1). No amplitude threshold can
reach those without also admitting noise, so amplitude was the wrong axis.

Consistency is the right one: thermal noise crossing floor+0.5 dB lands in a
different bin every pass, while a real emitter does it in the SAME bin every
pass. That is also why the operator could see them — an accumulating max-hold
display shows persistence, and the eye reads shape.

The measurements that set the defaults are in test_persistence_adds_no_false_
positives and test_amplitude_tier_is_the_noisy_one below.
"""

import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from survey.sweep.detector import Accumulator, detect_signals   # noqa: E402

FLOOR = -67.3          # the operator's real segment 3614 noise floor
SIGMA = 1.6            # per-pass noise spread
BINS = 112             # what an RF Explorer segment actually returns
PASSES = 40


def _sweep(seed, carrier_dbm=None, carrier_bin=50, bins=BINS, passes=PASSES):
    random.seed(seed)
    freqs = [300.0 + i * 0.42 for i in range(bins)]
    acc = Accumulator(freqs)
    for _ in range(passes):
        amps = [random.gauss(FLOOR, SIGMA) for _ in range(bins)]
        if carrier_dbm is not None:
            amps[carrier_bin] = carrier_dbm + random.gauss(0, 0.25)
        acc.add_sweep(amps)
    return acc


def _detect(acc, persistence=True, cand=8.0, frac=0.85):
    kw = {"threshold_db": 12.0, "candidate_threshold_db": cand,
          "persistence_min_frac": frac}
    if persistence:
        kw["low_hits"] = acc.low_hits
    return detect_signals(acc.freqs, acc.avg, acc.maxpeak, acc.hits,
                          acc.passes, **kw)[1]


class PersistenceTest(unittest.TestCase):
    def test_amplitude_alone_cannot_see_a_carrier_at_the_floor(self):
        """Precondition for everything below: this is why the axis had to change."""
        for s in range(20):
            self.assertEqual(_detect(_sweep(s, carrier_dbm=FLOOR + 1.0),
                                     persistence=False), [])

    def test_detection_rate_near_the_floor(self):
        """Characterises real sensitivity rather than cherry-picking a seed.
        At +1 dB it is good but NOT certain (~80%); by +2 dB it is reliable.
        Recorded here so nobody later mistakes this for a guarantee."""
        def rate(over_db):
            return sum(1 for s in range(20)
                       if _detect(_sweep(s, carrier_dbm=FLOOR + over_db)))
        self.assertGreaterEqual(rate(1.0), 14, "+1 dB sensitivity regressed")
        self.assertEqual(rate(2.0), 20, "+2 dB must be reliable")

    def test_found_carrier_is_at_the_right_frequency(self):
        acc = _sweep(0, carrier_dbm=FLOOR + 2.0)
        found = _detect(acc)
        self.assertTrue(found)
        self.assertAlmostEqual(found[0].center_mhz, acc.freqs[50], delta=0.5)

    def test_weak_finds_are_leads_never_findings(self):
        """Evidentiary: a near-floor find must never print as a measurement."""
        found = _detect(_sweep(0, carrier_dbm=FLOOR + 2.0))
        self.assertTrue(found)
        for s in found:
            self.assertEqual(s.tier, "candidate")

    def test_persistence_adds_no_false_positives(self):
        """Measured, not assumed: persistence must not invent emitters from
        noise. This is what makes a near-floor tier safe to ship at all."""
        base = sum(len(_detect(_sweep(s), persistence=False)) for s in range(40))
        withp = sum(len(_detect(_sweep(s))) for s in range(40))
        self.assertEqual(withp, base,
                         "persistence manufactured signals out of pure noise")

    def test_amplitude_tier_is_the_noisy_one(self):
        """Pins WHY candidate_threshold_db is 8.0 and not 6.0: max-hold over 40
        passes routinely peaks 6 dB over the floor on noise alone."""
        at6 = sum(len(_detect(_sweep(s), cand=6.0)) for s in range(40))
        at8 = sum(len(_detect(_sweep(s), cand=8.0)) for s in range(40))
        self.assertGreater(at6, 0, "6 dB was expected to be noisy")
        self.assertEqual(at8, 0, "8 dB should be clean on pure noise")

    def test_pure_noise_yields_nothing(self):
        for s in range(15):
            self.assertEqual(_detect(_sweep(s)), [])

    def test_strong_signal_still_reads_as_a_detection(self):
        found = _detect(_sweep(7, carrier_dbm=FLOOR + 25.0))
        self.assertTrue(found)
        self.assertEqual(found[0].tier, "detection")

    def test_needs_enough_passes_before_trusting_persistence(self):
        """Three passes cannot establish consistency; it must not guess early."""
        acc = _sweep(0, carrier_dbm=FLOOR + 2.0, passes=3)
        self.assertEqual(_detect(acc), [])

    def test_low_hits_are_counted_separately_from_hits(self):
        acc = _sweep(0, carrier_dbm=FLOOR + 2.0)
        self.assertGreater(acc.low_hits[50], acc.hits[50])
        self.assertIn("low_hits", acc.to_dict())


if __name__ == "__main__":
    unittest.main()
