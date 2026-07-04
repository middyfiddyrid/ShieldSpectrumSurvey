import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from survey.correlation import (  # noqa: E402
    anomaly_score, classify_signal, compute_interior_delta, compute_room_localization,
    explain_anomaly_score)


def rec(lo, hi, **kw):
    row = {"freq_low_mhz": lo, "freq_high_mhz": hi, "confidence": "record",
           "service": "FM", "source": "fcc_fm", "callsign": "KTST",
           "channel_bw_khz": 200.0, "caution": False, "uplink": False,
           "label": "", "licensee": "", "category": "broadcast"}
    row.update(kw)
    return row


BASELINE = [
    rec(98.5, 98.5, callsign="KSPR", service="FM"),
    rec(155.34, 155.34, callsign="WQAB123", service="LMR", channel_bw_khz=25.0,
        source="fcc_uls", category="land_mobile"),
    {"freq_low_mhz": 1930.0, "freq_high_mhz": 1995.0, "confidence": "block",
     "service": "CELL", "label": "PCS downlink (B2/B25)", "licensee": "AT&T",
     "callsign": "", "source": "opencellid", "channel_bw_khz": 5000.0,
     "caution": False, "uplink": False, "category": "cellular"},
    {"freq_low_mhz": 1850.0, "freq_high_mhz": 1915.0, "confidence": "block",
     "service": "CELL", "label": "PCS UPLINK (B2/B25)", "licensee": "AT&T",
     "callsign": "", "source": "opencellid", "channel_bw_khz": 5000.0,
     "caution": True, "uplink": True, "category": "cellular"},
    {"freq_low_mhz": 433.05, "freq_high_mhz": 434.79, "confidence": "allocation",
     "service": "ISM", "label": "433 MHz ISM", "callsign": "", "licensee": "",
     "source": "static", "channel_bw_khz": 100.0, "caution": True, "uplink": False,
     "category": "ism"},
    {"freq_low_mhz": 1565.42, "freq_high_mhz": 1585.42, "confidence": "allocation",
     "service": "GPS", "label": "GPS L1", "callsign": "", "licensee": "",
     "source": "static", "channel_bw_khz": 2000.0, "caution": True, "uplink": False,
     "category": "gps"},
]


class TestClassification(unittest.TestCase):
    def test_fm_verified_within_tolerance(self):
        c = classify_signal(98.45, BASELINE)
        self.assertEqual(c.classification, "VERIFIED")
        self.assertEqual(c.identity_tier, "IDENTIFIED")
        self.assertEqual(c.matched_label, "KSPR")
        self.assertEqual(c.group_key, "broadcast:FM")
        self.assertEqual(c.display_label, "KSPR")

    def test_fm_outside_tolerance_not_verified(self):
        c = classify_signal(98.2, BASELINE)
        self.assertNotEqual(c.classification, "VERIFIED")

    def test_nfm_land_mobile_tight_tolerance(self):
        c = classify_signal(155.345, BASELINE)
        self.assertEqual(c.classification, "VERIFIED")
        self.assertEqual(c.identity_tier, "IDENTIFIED")

    def test_cellular_downlink_block_verified(self):
        c = classify_signal(1960.0, BASELINE)
        self.assertEqual(c.classification, "VERIFIED")
        self.assertEqual(c.identity_tier, "IDENTIFIED")
        self.assertIn("downlink", c.match_reason)

    def test_cellular_uplink_block_is_known_band_caution(self):
        c = classify_signal(1880.0, BASELINE)
        self.assertEqual(c.classification, "PLAUSIBLE")
        self.assertEqual(c.identity_tier, "KNOWN_BAND")
        self.assertTrue(c.caution)
        self.assertIn("UPLINK", c.match_reason)

    def test_ism_known_band_with_caution(self):
        c = classify_signal(433.92, BASELINE)
        self.assertEqual(c.classification, "PLAUSIBLE")
        self.assertEqual(c.identity_tier, "KNOWN_BAND")
        self.assertTrue(c.caution)

    def test_gps_band_energy_flagged(self):
        c = classify_signal(1575.42, BASELINE)
        self.assertEqual(c.identity_tier, "KNOWN_BAND")
        self.assertTrue(c.caution)
        self.assertIn("GPS", c.match_reason.upper())

    def test_anomaly_reports_nearest(self):
        c = classify_signal(412.0, BASELINE)
        self.assertEqual(c.classification, "UNVERIFIED")
        self.assertEqual(c.identity_tier, "ANOMALY")
        self.assertEqual(c.display_label, "ANOMALY")
        self.assertTrue(c.caution)
        self.assertIn("nearest known", c.match_reason)
        self.assertIsNotNone(c.nearest_distance_mhz)


class TestLocalization(unittest.TestCase):
    def test_interior_delta_against_matched_ambient(self):
        sig = {"center_mhz": 433.9, "bw_khz": 200.0, "peak_dbm": -45.0}
        ambient = [{"center_mhz": 433.92, "bw_khz": 200.0, "peak_dbm": -78.0}]
        self.assertAlmostEqual(compute_interior_delta(sig, ambient, -105.0), 33.0)

    def test_interior_delta_absent_outside_uses_floor(self):
        sig = {"center_mhz": 2437.0, "bw_khz": 20000.0, "peak_dbm": -50.0}
        self.assertAlmostEqual(compute_interior_delta(sig, [], -100.0), 50.0)

    def test_room_localization_picks_strongest_room(self):
        by_room = {
            "Office": [{"center_mhz": 433.9, "bw_khz": 200.0, "peak_dbm": -42.0}],
            "Master Bedroom": [{"center_mhz": 433.91, "bw_khz": 200.0, "peak_dbm": -68.0}],
            "Kitchen": [{"center_mhz": 433.9, "bw_khz": 200.0, "peak_dbm": -71.0}],
        }
        groups = compute_room_localization(by_room)
        self.assertEqual(len(groups), 1)
        g = next(iter(groups.values()))
        self.assertEqual(g["strongest_room"], "Office")
        self.assertGreater(g["delta_db"], 20.0)

    def test_uniform_broadcast_signal_low_delta(self):
        by_room = {
            "Office": [{"center_mhz": 98.5, "bw_khz": 200.0, "peak_dbm": -50.0}],
            "Kitchen": [{"center_mhz": 98.5, "bw_khz": 200.0, "peak_dbm": -52.0}],
        }
        g = next(iter(compute_room_localization(by_room).values()))
        self.assertLess(g["delta_db"], 5.0)


class TestScoring(unittest.TestCase):
    def test_verified_uniform_scores_zero_ish(self):
        c = classify_signal(98.5, BASELINE)
        s = anomaly_score(c, persistence=1.0, interior_delta_db=0.0, localization_delta_db=1.0)
        self.assertLessEqual(s, 10.0)

    def test_unverified_local_steady_scores_high(self):
        c = classify_signal(412.0, BASELINE)
        s = anomaly_score(c, persistence=0.95, interior_delta_db=30.0, localization_delta_db=20.0)
        self.assertGreaterEqual(s, 85.0)

    def test_plausible_ism_with_interior_boost_beats_clean_plausible(self):
        c = classify_signal(433.92, BASELINE)
        boosted = anomaly_score(c, 0.9, interior_delta_db=25.0, localization_delta_db=18.0)
        flat = anomaly_score(c, 0.9, interior_delta_db=-2.0, localization_delta_db=0.0)
        self.assertGreater(boosted, flat + 20.0)

    def test_score_capped_at_100(self):
        c = classify_signal(412.0, BASELINE)
        s = anomaly_score(c, 0.05, interior_delta_db=60.0, localization_delta_db=40.0, gps_band=True)
        self.assertLessEqual(s, 100.0)


class TestScoreExplanation(unittest.TestCase):
    def test_score_matches_scalar_entrypoint(self):
        # the explanation must carry the SAME number the legacy scalar returns
        c = classify_signal(412.0, BASELINE)
        for pers, idb, ldb, gps in [(0.95, 30.0, 20.0, False), (1.0, 0.0, 1.0, False),
                                    (0.05, 60.0, 40.0, True), (0.9, None, None, False)]:
            expl = explain_anomaly_score(c, pers, idb, ldb, gps_band=gps)
            self.assertAlmostEqual(expl.score, anomaly_score(c, pers, idb, ldb, gps_band=gps))

    def test_factors_sum_to_score_when_uncapped(self):
        c = classify_signal(412.0, BASELINE)          # anomaly, base 60
        expl = explain_anomaly_score(c, 0.95, 15.0, 14.0)   # 60 + 17.5 + 10.4 + 5 = 92.9
        self.assertLess(expl.score, 100.0)
        self.assertAlmostEqual(sum(f["points"] for f in expl.factors), expl.score, places=1)
        # first factor is always the identification starting point
        self.assertIn("identification", expl.factors[0]["label"].lower())

    def test_every_raising_factor_is_explained(self):
        c = classify_signal(412.0, BASELINE)
        expl = explain_anomaly_score(c, 0.95, 30.0, 20.0)
        labels = " ".join(f["label"].lower() for f in expl.factors)
        for token in ("louder inside", "localizes", "always on"):
            self.assertIn(token, labels)
        # each factor has human-readable detail text
        for f in expl.factors:
            self.assertTrue(f["detail"] and len(f["detail"]) > 10)

    def test_summary_is_plain_language_with_band(self):
        c = classify_signal(412.0, BASELINE)
        hi = explain_anomaly_score(c, 0.95, 30.0, 20.0).summary
        self.assertIn("HIGH", hi)
        self.assertIn("/100", hi)
        lo = explain_anomaly_score(classify_signal(98.5, BASELINE), 1.0, 0.0, 1.0).summary
        self.assertIn("LOW", lo)

    def test_capped_score_is_flagged_in_summary(self):
        c = classify_signal(412.0, BASELINE)
        expl = explain_anomaly_score(c, 0.05, 60.0, 40.0, gps_band=True)
        self.assertEqual(expl.score, 100.0)
        self.assertIn("Capped", expl.summary)

    def test_identified_signal_has_no_behaviour_raisers(self):
        # persistence 0.5 (not "always on"), no interior/localization deltas
        expl = explain_anomaly_score(classify_signal(98.5, BASELINE), 0.5, None, None)
        self.assertEqual(len(expl.factors), 1)       # identification only
        self.assertIn("identification status alone", expl.summary)


if __name__ == "__main__":
    unittest.main()