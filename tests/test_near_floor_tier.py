"""Near-floor candidate tier.

Field report that forced this: the operator kept marking, by eye, signals sitting
0-2 dB over the segment noise floor while the detector demanded 12 dB — so the
sweep silently discarded exactly the low-power emitters a bug sweep exists to
find. One case (781.249 MHz) missed the gate by 0.7 dB.

The fix must NOT be "lower the threshold": that would promote noise to findings.
Candidates are a second tier — recorded as leads, never as measured findings.
"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from survey.sweep.detector import detect_signals                # noqa: E402
from survey.db import Database                                  # noqa: E402
from survey.config import Config                                # noqa: E402
from survey.report import (_freq_cell, _is_near_floor,           # noqa: E402
                           export_csv, generate_report)


def _trace(n=200, floor=-100.0, spikes=()):
    """Flat floor with (bin, dBm) spikes; hits=passes so persistence is 1.0."""
    avg = [floor] * n
    mx = [floor] * n
    for b, dbm in spikes:
        mx[b] = dbm
    return list(range(n)), avg, mx, [10] * n, 10


class NearFloorDetectorTest(unittest.TestCase):
    def test_single_tier_unchanged_when_disabled(self):
        """Default (None) must behave exactly as before this feature existed."""
        # floor -100: -92 is +8 dB (candidate tier only), -80 is +20 dB (detection)
        f, avg, mx, hits, p = _trace(spikes=[(50, -92.0), (100, -80.0)])
        floor, sigs = detect_signals(f, avg, mx, hits, p, threshold_db=12.0)
        self.assertEqual(len(sigs), 1)                  # only the -80 clears +12
        self.assertEqual(sigs[0].tier, "detection")

    def test_candidate_tier_surfaces_the_weak_signal(self):
        # floor -100: -92 is +8 dB (candidate tier only), -80 is +20 dB (detection)
        f, avg, mx, hits, p = _trace(spikes=[(50, -92.0), (100, -80.0)])
        floor, sigs = detect_signals(f, avg, mx, hits, p, threshold_db=12.0,
                                     candidate_threshold_db=6.0)
        tiers = sorted(s.tier for s in sigs)
        self.assertEqual(tiers, ["candidate", "detection"])

    def test_the_real_world_near_miss(self):
        """781.249 MHz: floor -60.3, peak -49.3 — 11.0 dB over a 12 dB gate."""
        f, avg, mx, hits, p = _trace(floor=-60.3, spikes=[(100, -49.3)])
        _, single = detect_signals(f, avg, mx, hits, p, threshold_db=12.0)
        self.assertEqual(single, [], "precondition: the old detector dropped it")
        _, two = detect_signals(f, avg, mx, hits, p, threshold_db=12.0,
                                candidate_threshold_db=6.0)
        self.assertEqual([s.tier for s in two], ["candidate"])

    def test_strong_signals_never_get_demoted(self):
        """The tier must not weaken anything that already qualified."""
        f, avg, mx, hits, p = _trace(spikes=[(100, -40.0)])
        _, sigs = detect_signals(f, avg, mx, hits, p, threshold_db=12.0,
                                 candidate_threshold_db=6.0)
        self.assertEqual([s.tier for s in sigs], ["detection"])

    def test_candidate_threshold_at_or_above_detection_is_refused(self):
        """Otherwise a misconfiguration would relabel real detections as leads
        and quietly empty the findings table."""
        f, avg, mx, hits, p = _trace(spikes=[(100, -70.0)])
        for bad in (12.0, 20.0):
            _, sigs = detect_signals(f, avg, mx, hits, p, threshold_db=12.0,
                                     candidate_threshold_db=bad)
            self.assertEqual([s.tier for s in sigs], ["detection"],
                             f"candidate_threshold_db={bad} corrupted the tiers")

    def test_pure_noise_still_yields_nothing(self):
        f, avg, mx, hits, p = _trace()
        _, sigs = detect_signals(f, avg, mx, hits, p, threshold_db=12.0,
                                 candidate_threshold_db=6.0)
        self.assertEqual(sigs, [])


class NearFloorReportTest(unittest.TestCase):
    """A lead must never read as a measured finding in a client deliverable."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = Database(os.path.join(self.dir, "t.db"))
        self.cfg = Config(os.path.join(self.dir, "config.yaml"))
        self.sid = self.db.create_session("1 A St", 0, 0, "test", 25, 5, "op")
        self.sweep = self.db.execute(
            "INSERT INTO sweeps (session_id,phase,status,started_utc) VALUES (?,?,?,?)",
            (self.sid, "interior", "done", time.time()))

    def _sig(self, center, **kw):
        cols = {"sweep_id": self.sweep, "session_id": self.sid, "center_mhz": center,
                "phase": "interior", "module": "right", "room": "Office",
                "classification": "UNVERIFIED", "peak_dbm": -70.0, "bw_khz": 25.0,
                "persistence": 0.5, "matched_label": "", "match_reason": "",
                "anomaly_score": 50.0}
        cols.update(kw)
        keys = ",".join(cols)
        return self.db.execute(
            f"INSERT INTO signals ({keys}) VALUES ({','.join('?' * len(cols))})",
            tuple(cols.values()))

    def test_near_floor_row_is_tagged(self):
        row = self.db.query_one("SELECT * FROM signals WHERE id=?",
                                (self._sig(433.92, near_floor=1),))
        self.assertTrue(_is_near_floor(row))
        self.assertIn("near-floor lead", _freq_cell(row))

    def test_normal_detection_is_not_tagged(self):
        row = self.db.query_one("SELECT * FROM signals WHERE id=?", (self._sig(915.0),))
        self.assertNotIn("near-floor", _freq_cell(row))

    def test_report_builds_with_a_near_floor_row(self):
        self._sig(433.92, near_floor=1, peak_dbm=None)
        out = generate_report(self.db, self.cfg, self.sid, self.dir)
        self.assertTrue(os.path.exists(out["path"]))

    def test_csv_carries_the_flag(self):
        self._sig(433.92, near_floor=1)
        with open(export_csv(self.db, self.sid, self.dir), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("near_floor", text.splitlines()[0])


if __name__ == "__main__":
    unittest.main()
