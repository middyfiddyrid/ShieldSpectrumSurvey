import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from survey.expected import (expectation_level, expected_for_range,  # noqa: E402
                             expected_vs_found, parse_erp_kw)


class FakeDB:
    """Minimal stand-in returning canned rows per table keyword."""

    def __init__(self, baseline, segments, signals):
        self.baseline, self.segments, self.signals = baseline, segments, signals

    def query(self, sql, params=()):
        if "FROM baseline" in sql:
            return self.baseline
        if "FROM segments" in sql:
            return self.segments
        if "FROM signals" in sql:
            return self.signals
        raise AssertionError(sql)


def rec(f, service="FM", dist=10.0, callsign="KTST", bw=200.0):
    return {"freq_low_mhz": f, "freq_high_mhz": f, "service": service,
            "callsign": callsign, "label": f"{service} {f}", "licensee": "",
            "distance_mi": dist, "source": "fcc", "confidence": "record",
            "channel_bw_khz": bw, "caution": 0, "uplink": 0, "category": "broadcast"}


class TestExpectationLevels(unittest.TestCase):
    def test_close_fm_strong(self):
        self.assertEqual(expectation_level(rec(98.5, "FM", 12))[0], "strong")

    def test_fringe_fm_medium(self):
        self.assertEqual(expectation_level(rec(98.5, "FM", 42))[0], "medium")

    def test_far_fm_weak(self):
        self.assertEqual(expectation_level(rec(98.5, "FM", 80))[0], "weak")

    def test_lmr_always_weak(self):
        self.assertEqual(expectation_level(rec(155.34, "LMR", 2))[0], "weak")

    def test_am_never_strong(self):
        self.assertNotEqual(expectation_level(rec(0.98, "AM", 5))[0], "strong")


class TestPowerAwareExpectation(unittest.TestCase):
    def _rec(self, erp, dist=13.0):
        r = rec(98.5, "FM", dist)
        r["erp"] = erp
        return r

    def test_parse_erp_variants(self):
        self.assertAlmostEqual(parse_erp_kw({"erp": "0.432  kW"}), 0.432)
        self.assertAlmostEqual(parse_erp_kw({"erp": "8.3 kW"}), 8.3)
        self.assertAlmostEqual(parse_erp_kw({"erp": 50.0}), 50.0)
        self.assertIsNone(parse_erp_kw({"erp": ""}))
        self.assertIsNone(parse_erp_kw({"erp": None}))

    def test_low_power_translator_not_strong(self):
        # a 50-watt translator 13 mi out must NOT be a 'strong — investigate' item
        self.assertEqual(expectation_level(self._rec("0.05 kW", 13))[0], "weak")

    def test_low_power_close_is_medium_not_strong(self):
        self.assertEqual(expectation_level(self._rec("0.432 kW", 6))[0], "medium")

    def test_full_power_close_stays_strong(self):
        self.assertEqual(expectation_level(self._rec("8.3 kW", 13))[0], "strong")

    def test_moderate_power_downgraded(self):
        self.assertIn(expectation_level(self._rec("3 kW", 20))[0], ("weak", "medium"))

    def test_unknown_erp_falls_back_to_distance(self):
        r = rec(98.5, "FM", 12)          # no erp key -> distance-only, close -> strong
        self.assertEqual(expectation_level(r)[0], "strong")


class TestExpectedVsFound(unittest.TestCase):
    def make(self, signals, segments=None):
        baseline = [rec(98.5, "FM", 10, "KFND"),       # found
                    rec(101.1, "FM", 10, "KMISS"),     # missing, swept, strong
                    rec(155.34, "LMR", 3, "WQAB"),     # missing, swept, weak
                    rec(650.0, "TV", 12, "KGAP")]      # not swept
        segments = segments or [{"low_mhz": 88.0, "high_mhz": 160.0}]
        return expected_vs_found(FakeDB(baseline, segments, signals), 1)

    def test_found_and_missing_classified(self):
        out = self.make([{"center_mhz": 98.51, "peak_dbm": -50.0,
                          "phase": "interior", "room": "Office"}])
        by_call = {r["callsign"]: r for r in out["rows"]}
        self.assertTrue(by_call["KFND"]["found"])
        self.assertIn("received at -50", by_call["KFND"]["reason"])
        self.assertFalse(by_call["KMISS"]["found"])
        self.assertTrue(by_call["KMISS"]["swept"])
        self.assertIn("NARROW sweep", by_call["KMISS"]["reason"])
        self.assertFalse(by_call["WQAB"]["found"])
        self.assertIn("intermittent", by_call["WQAB"]["reason"])
        self.assertFalse(by_call["KGAP"]["swept"])
        self.assertIn("NOT covered", by_call["KGAP"]["reason"])

    def test_counts(self):
        out = self.make([{"center_mhz": 98.5, "peak_dbm": -50.0,
                          "phase": "ambient", "room": ""}])
        c = out["counts"]
        self.assertEqual(c["found"], 1)
        self.assertEqual(c["missing_strong"], 1)
        self.assertEqual(c["missing_weak"], 1)
        self.assertEqual(c["not_swept"], 1)
        self.assertEqual(c["total_records"], 4)

    def test_missing_strong_sorts_first(self):
        out = self.make([{"center_mhz": 98.5, "peak_dbm": -50.0,
                          "phase": "ambient", "room": ""}])
        first = out["rows"][0]
        self.assertFalse(first["found"])
        self.assertEqual(first["expectation"], "strong")

    def test_fm_tolerance_respected(self):
        # 98.5 record vs detection at 98.8 — outside ±100 kHz, must not count
        out = self.make([{"center_mhz": 98.8, "peak_dbm": -50.0,
                          "phase": "interior", "room": ""}])
        by_call = {r["callsign"]: r for r in out["rows"]}
        self.assertFalse(by_call["KFND"]["found"])


class TestExpectedForRange(unittest.TestCase):
    def setUp(self):
        # records + a band allocation (must be ignored — record-only)
        self.rows = [rec(98.5, "FM", 12, "KFM"),
                     rec(101.1, "FM", 70, "KFAR"),
                     rec(155.34, "LMR", 3, "WLMR"),
                     {"freq_low_mhz": 2400.0, "freq_high_mhz": 2483.5, "service": "ISM",
                      "confidence": "allocation", "label": "ISM 2.4G"}]

    def test_only_overlapping_records(self):
        out = expected_for_range(self.rows, 88.0, 108.0)
        calls = {e["callsign"] for e in out}
        self.assertEqual(calls, {"KFM", "KFAR"})          # FM band only
        self.assertTrue(all(e["status"] == "looking" for e in out))

    def test_allocation_excluded(self):
        out = expected_for_range(self.rows, 2400.0, 2483.5)
        self.assertEqual(out, [])                          # band rows are not 'records'

    def test_sorted_strongest_first(self):
        out = expected_for_range(self.rows, 88.0, 200.0)
        self.assertEqual(out[0]["expectation"], "strong")  # KFM (12 mi) leads
        self.assertIn("center_mhz", out[0])

    def test_carries_match_fields(self):
        out = expected_for_range(self.rows, 98.0, 99.0)
        self.assertEqual(len(out), 1)
        e = out[0]
        for k in ("freq_low_mhz", "freq_high_mhz", "service", "channel_bw_khz"):
            self.assertIn(k, e)


if __name__ == "__main__":
    unittest.main()
