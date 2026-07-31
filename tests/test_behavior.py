"""Behavioral classifiers + RF↔inventory matching (no radio)."""

import unittest

from survey.behavior import classify_behavior, behavior_from_identify
from survey.rf_match import match_wifi, match_ble, match_inventory, summarize_matches
from survey.nextstep import next_step_for, enrich_signal_rows, device_hint_for


class TestBehavior(unittest.TestCase):
    def test_uplink(self):
        b = classify_behavior({"center_mhz": 1712.5, "bw_khz": 5000,
                               "persistence": 0.2})
        self.assertEqual(b["class_id"], "cellular_uplink")
        self.assertIn("phone", b["phrase"].lower())

    def test_video_like_continuous_wide_24(self):
        b = classify_behavior({"center_mhz": 2437.0, "bw_khz": 18000,
                               "persistence": 0.95})
        self.assertEqual(b["class_id"], "video_like")
        self.assertIn("consistent with", b["phrase"].lower())

    def test_wifi_data_bursty_wide(self):
        b = classify_behavior({"center_mhz": 5180.0, "bw_khz": 20000,
                               "persistence": 0.2})
        self.assertEqual(b["class_id"], "wifi_data")

    def test_keyfob_sub_ghz_bursty(self):
        b = classify_behavior({"center_mhz": 433.92, "bw_khz": 25,
                               "persistence": 0.05})
        self.assertEqual(b["class_id"], "keyfob_like")

    def test_analog_audio_narrow(self):
        b = classify_behavior({"center_mhz": 450.5, "bw_khz": 12,
                               "persistence": 0.8})
        self.assertEqual(b["class_id"], "analog_audio_bug")

    def test_iq_feats_boost_video(self):
        b = classify_behavior(
            {"center_mhz": 5800.0, "bw_khz": 8000, "persistence": 0.9},
            {"duty_cycle": 0.95, "continuity": "continuous",
             "modulation_class": "analog_video", "snr_db": 25})
        self.assertEqual(b["class_id"], "video_like")
        self.assertGreaterEqual(b["confidence"], 0.7)

    def test_behavior_from_identify_wrapper(self):
        b = behavior_from_identify(433.92, 15, {
            "duty_cycle": 0.1, "continuity": "bursty",
            "modulation_class": "ook", "snr_db": 18})
        self.assertEqual(b["class_id"], "keyfob_like")

    def test_never_claims_brand(self):
        b = classify_behavior({"center_mhz": 2437.0, "bw_khz": 20000,
                               "persistence": 0.9})
        lower = b["phrase"].lower()
        self.assertNotIn("samsung", lower)
        self.assertNotIn("detected hidden", lower)


class TestRfMatch(unittest.TestCase):
    def setUp(self):
        self.inv = [
            {"kind": "wifi", "identifier": "aa:bb:cc:dd:ee:01",
             "name": "LivingRoom-TV", "freq_mhz": 2437.0, "channel": "6",
             "rssi_dbm": -48.0, "owned": 0, "extra": "2.4 GHz"},
            {"kind": "wifi", "identifier": "aa:bb:cc:dd:ee:02",
             "name": "Office-AP", "freq_mhz": 5180.0, "channel": "36",
             "rssi_dbm": -55.0, "owned": 1, "extra": "5 GHz"},
            {"kind": "ble", "identifier": "11:22:33:44:55:66",
             "name": "Tile", "freq_mhz": 2440.0, "rssi_dbm": -52.0,
             "owned": 0, "extra": "⚑ TRACKER · Tile"},
            {"kind": "ble", "identifier": "aa:11:22:33:44:55",
             "name": "Phone", "freq_mhz": 2440.0, "rssi_dbm": -70.0,
             "owned": 1, "extra": "Apple"},
        ]

    def test_wifi_channel_hit(self):
        m = match_wifi(2437.0, 20000, self.inv)
        self.assertTrue(m)
        self.assertEqual(m[0]["name"], "LivingRoom-TV")
        self.assertGreater(m[0]["score"], 0.8)

    def test_wifi_wrong_band_no_match(self):
        m = match_wifi(100.1, 200, self.inv)
        self.assertEqual(m, [])

    def test_ble_soft_match_in_24(self):
        m = match_ble(2440.0, self.inv)
        self.assertGreaterEqual(len(m), 2)
        # trackers first
        self.assertTrue(m[0].get("is_tracker"))

    def test_ble_out_of_band_empty(self):
        self.assertEqual(match_ble(450.0, self.inv), [])

    def test_match_inventory_combines(self):
        m = match_inventory(2437.0, 18000, self.inv)
        kinds = {x["kind"] for x in m}
        self.assertIn("wifi", kinds)
        self.assertIn("ble", kinds)
        self.assertTrue(summarize_matches(m))

    def test_owned_wifi_flagged_in_phrase(self):
        m = match_wifi(5180.0, 20000, self.inv)
        self.assertTrue(m[0]["owned"])
        self.assertIn("owned", m[0]["phrase"].lower())


class TestEnrichAndNext(unittest.TestCase):
    def test_enrich_attaches_behavior(self):
        rows = enrich_signal_rows([{
            "center_mhz": 433.92, "bw_khz": 12, "persistence": 0.1,
            "peak_dbm": -50, "floor_dbm": -100, "classification": "UNVERIFIED",
            "identity_tier": "ANOMALY", "triage_status": "unreviewed",
        }])
        self.assertEqual(rows[0]["behavior"]["class_id"], "keyfob_like")
        self.assertEqual(rows[0]["snr_db"], 50.0)
        self.assertEqual(rows[0]["next_step"]["kind"], "identify")

    def test_enrich_with_inventory_match(self):
        inv = [{"kind": "wifi", "identifier": "aa:bb:cc:00:00:01",
                "name": "CamAP", "freq_mhz": 2437.0, "channel": "6",
                "rssi_dbm": -40.0, "owned": 0, "extra": ""}]
        rows = enrich_signal_rows([{
            "center_mhz": 2437.0, "bw_khz": 20000, "persistence": 0.9,
            "peak_dbm": -40, "floor_dbm": -95, "classification": "UNVERIFIED",
            "identity_tier": "ANOMALY", "triage_status": "unreviewed",
        }], inventory=inv)
        self.assertTrue(rows[0]["inventory_matches"])
        self.assertEqual(rows[0]["inventory_matches"][0]["name"], "CamAP")
        self.assertEqual(rows[0]["next_step"]["kind"], "inventory")
        self.assertIn("CamAP", rows[0]["next_step"]["detail"])

    def test_device_hint_uses_behavior(self):
        rows = enrich_signal_rows([{
            "center_mhz": 2437.0, "bw_khz": 18000, "persistence": 0.95,
            "peak_dbm": -35, "floor_dbm": -95, "classification": "UNVERIFIED",
            "identity_tier": "ANOMALY", "triage_status": "unreviewed",
        }])
        self.assertIn("Video", rows[0]["device_hint"])

    def test_id_device_class_wins_hint(self):
        h = device_hint_for({
            "id_device_class": "Confirmed X",
            "behavior": {"class_id": "video_like", "label": "Video"},
            "center_mhz": 2437.0,
        })
        self.assertEqual(h, "Confirmed X")

    def test_tracker_next_step(self):
        inv = [{"kind": "ble", "identifier": "aa:bb:cc:dd:ee:ff",
                "name": "AirTag", "freq_mhz": 2440.0, "rssi_dbm": -45.0,
                "owned": 0, "extra": "⚑ TRACKER"}]
        # No hard Wi-Fi channel hit — only BLE soft match with tracker
        rows = enrich_signal_rows([{
            "center_mhz": 2441.0, "bw_khz": 1000, "persistence": 0.15,
            "peak_dbm": -55, "floor_dbm": -95, "classification": "UNVERIFIED",
            "identity_tier": "ANOMALY", "triage_status": "unreviewed",
        }], inventory=inv)
        self.assertEqual(rows[0]["next_step"]["kind"], "inventory")
        self.assertIn("TRACKER", rows[0]["next_step"]["action"])


if __name__ == "__main__":
    unittest.main()
