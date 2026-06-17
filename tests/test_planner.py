import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from survey.config import Config  # noqa: E402
from survey.sweep.planner import plan_full_sweep, plan_module_sweep, split_range  # noqa: E402
from survey.allocations import bug_band_ranges  # noqa: E402


class FakeCfg(Config):
    def __init__(self):  # defaults only, no file
        import copy
        from survey.config import DEFAULTS
        self.path = "<memory>"
        self.data = copy.deepcopy(DEFAULTS)

    def set_and_save(self, keys, value):
        pass


CFG = FakeCfg()
MODULES = [{"side": "left", "min_mhz": 0.05, "max_mhz": 960.0},
           {"side": "right", "min_mhz": 240.0, "max_mhz": 6100.0}]


class TestPlanner(unittest.TestCase):
    def test_segments_cover_range_without_gaps(self):
        segs = split_range("left", 0.05, 960.0, {"subghz": 75, "mid": 100, "high": 200},
                           1.0, {"default": 1.0})
        self.assertAlmostEqual(segs[0].low_mhz, 0.05)
        self.assertAlmostEqual(segs[-1].high_mhz, 960.0)
        for a, b in zip(segs, segs[1:]):
            self.assertAlmostEqual(a.high_mhz, b.low_mhz)

    def test_risk_bands_get_more_dwell(self):
        plan = plan_module_sweep("left", 0.05, 960.0, CFG, "standard")
        ism433 = [s for s in plan.segments if s.band_tag == "ism_433"]
        broadcast = [s for s in plan.segments if s.band_tag == "broadcast"]
        self.assertTrue(ism433, "433 ISM segment should exist below 1 GHz")
        self.assertTrue(broadcast, "broadcast-only segments should exist")
        self.assertGreater(min(s.maxhold_dwell_s for s in ism433),
                           max(s.maxhold_dwell_s for s in broadcast))

    def test_profiles_scale_time(self):
        quick = plan_module_sweep("right", 240.0, 6100.0, CFG, "quick")
        deep = plan_module_sweep("right", 240.0, 6100.0, CFG, "deep")
        self.assertLess(quick.estimated_seconds, deep.estimated_seconds)
        # quick uses wider spans -> fewer segments
        self.assertLess(len(quick.segments), len(deep.segments))

    def test_custom_budget_roughly_respected(self):
        plans = plan_full_sweep(MODULES, CFG, "custom", custom_minutes=30)
        total = sum(p.estimated_seconds for p in plans.values())
        self.assertLess(abs(total - 30 * 60) / (30 * 60), 0.5,
                        f"estimate {total / 60:.1f} min should be within 50% of 30 min budget")

    def test_full_sweep_covers_both_modules(self):
        plans = plan_full_sweep(MODULES, CFG, "standard")
        self.assertIn("left", plans)
        self.assertIn("right", plans)
        self.assertAlmostEqual(plans["left"].segments[0].low_mhz, 0.05)
        self.assertAlmostEqual(plans["right"].segments[-1].high_mhz, 6100.0)

    def test_minimums_enforced(self):
        plans = plan_full_sweep(MODULES, CFG, "custom", custom_minutes=3)
        for p in plans.values():
            for s in p.segments:
                self.assertGreaterEqual(s.avg_passes, 3)
                self.assertGreaterEqual(s.maxhold_dwell_s, 2.0)

    def test_bug_scan_only_covers_known_bug_bands(self):
        targets = bug_band_ranges()
        self.assertTrue(targets, "there should be known bug bands")
        plans = plan_full_sweep(MODULES, CFG, "standard", targets=targets)
        segs = [s for p in plans.values() for s in p.segments]
        self.assertTrue(segs, "bug scan should produce segments")
        for s in segs:
            inside = any(s.low_mhz >= lo - 1e-6 and s.high_mhz <= hi + 1e-6
                         for lo, hi in targets)
            self.assertTrue(inside, f"segment {s.low_mhz}-{s.high_mhz} is outside the bug bands")
        for p in plans.values():
            self.assertEqual(p.scan_mode, "bug")

    def test_bug_scan_skips_broadcast(self):
        plans = plan_full_sweep(MODULES, CFG, "standard", targets=bug_band_ranges())
        segs = [s for p in plans.values() for s in p.segments]
        # FM broadcast (88-108) is not a bug band and must be skipped entirely
        fm = [s for s in segs if s.low_mhz < 108.0 and s.high_mhz > 88.0]
        self.assertFalse(fm, "FM broadcast must not be swept in a bug-frequency scan")
        # but the 433 MHz ISM band (a classic bug band) must be covered
        ism433 = [s for s in segs if s.low_mhz < 434.79 and s.high_mhz > 433.05]
        self.assertTrue(ism433, "433 MHz ISM must be covered in a bug-frequency scan")


if __name__ == "__main__":
    unittest.main()
