import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from survey.ble import (  # noqa: E402
    appearance_label, classify_mac, decode_manufacturer, rssi_proximity,
    service_names, summarize_advertisement)


class TestClassifyMac(unittest.TestCase):
    def test_public_oui_resolves_vendor(self):
        r = classify_mac("B8:27:EB:11:22:33")          # Raspberry Pi OUI, top2=10
        self.assertEqual(r["address_kind"], "public")
        self.assertFalse(r["is_random"])
        self.assertEqual(r["oui"], "B827EB")
        self.assertEqual(r["oui_vendor"], "Raspberry Pi Foundation")

    def test_public_unknown_oui_is_honest(self):
        r = classify_mac("00:11:22:33:44:55", address_type="public")
        self.assertEqual(r["address_kind"], "public")
        self.assertIsNone(r["oui_vendor"])
        self.assertIn("not in our curated", r["note"])

    def test_resolvable_private_is_rotating(self):
        r = classify_mac("7A:11:22:33:44:55")          # top2 = 01
        self.assertEqual(r["address_kind"], "random_resolvable")
        self.assertTrue(r["rotating"])
        self.assertIsNone(r["oui_vendor"])

    def test_nonresolvable_private(self):
        r = classify_mac("12:34:56:78:9A:BC")          # top2 = 00, U/L bit set
        self.assertEqual(r["address_kind"], "random_nonresolvable")
        self.assertTrue(r["rotating"])

    def test_static_random_needs_hint(self):
        # top2 = 11 is ambiguous with a public OUI unless the platform says 'random'
        r = classify_mac("F5:11:22:33:44:55", address_type="random")
        self.assertEqual(r["address_kind"], "random_static")
        self.assertFalse(r["rotating"])

    def test_hint_overrides_inference(self):
        self.assertFalse(classify_mac("7A:11:22:33:44:55", "public")["is_random"])
        self.assertTrue(classify_mac("7A:11:22:33:44:55")["is_random"])   # inferred

    def test_bad_mac_raises(self):
        with self.assertRaises(ValueError):
            classify_mac("not-a-mac")


class TestDecode(unittest.TestCase):
    def test_apple_findmy_flags_tracker(self):
        d = decode_manufacturer(0x004C, bytes([0x12, 0x00]))
        self.assertTrue(d["is_tracker"])
        self.assertEqual(d["company"], "Apple")
        self.assertTrue(any("Find My" in h for h in d["hints"]))

    def test_apple_airpods_not_tracker(self):
        d = decode_manufacturer(0x004C, bytes([0x07, 0x00]))
        self.assertFalse(d["is_tracker"])
        self.assertTrue(any("AirPods" in h for h in d["hints"]))

    def test_tile_company_is_tracker(self):
        self.assertTrue(decode_manufacturer(0x0157, b"")["is_tracker"])

    def test_unknown_company_is_honest(self):
        d = decode_manufacturer(0xABCD, b"")
        self.assertIn("unrecognized", d["company"])
        self.assertIn("0xABCD", d["company"])

    def test_service_names_and_tracker(self):
        svcs = service_names(["FD44", "180F", "0000feed-0000-1000-8000-00805f9b34fb"])
        by = {s["uuid"]: s for s in svcs}
        self.assertEqual(by["FD44"]["name"], "Apple (Find My / AirTag)")
        self.assertTrue(by["FD44"]["tracker"])
        self.assertEqual(by["180F"]["name"], "Battery")
        self.assertTrue(by["FEED"]["tracker"])

    def test_appearance_and_proximity(self):
        self.assertEqual(appearance_label(1 << 6), "Phone")
        self.assertIsNone(appearance_label(None))
        self.assertIn("very close", rssi_proximity(-40))
        self.assertEqual(rssi_proximity(None), "unknown")


class TestSummarize(unittest.TestCase):
    def test_airtag_full_dossier(self):
        adv = {"mac": "7A:AB:CD:EF:01:02", "rssi_dbm": -45,
               "manufacturer_data": {0x004C: bytes([0x12, 0x19, 0x00])},
               "service_uuids": ["FD44"]}
        r = summarize_advertisement(adv)
        self.assertTrue(r["is_tracker"])
        self.assertTrue(r["rotating"])
        self.assertIn("TRACKER", r["info"])
        self.assertIn("TRACKER", r["tscm_note"])
        self.assertIn("Rotating", r["tscm_note"])
        self.assertIn("very close", r["proximity"])

    def test_public_device_names_vendor(self):
        adv = {"mac": "B8:27:EB:11:22:33", "name": "hub", "rssi_dbm": -72}
        r = summarize_advertisement(adv)
        self.assertEqual(r["vendor"], "Raspberry Pi Foundation")
        self.assertFalse(r["is_tracker"])
        self.assertIn("Raspberry Pi", r["summary"])

    def test_vendor_from_mfr_id_when_address_random(self):
        # a phone with a rotating MAC still tells us "Apple" via the company id
        adv = {"mac": "7A:11:22:33:44:55", "rssi_dbm": -60,
               "manufacturer_data": {0x004C: bytes([0x10, 0x05])}}
        r = summarize_advertisement(adv)
        self.assertEqual(r["vendor"], "Apple")
        self.assertTrue(r["rotating"])
        self.assertFalse(r["is_tracker"])

    def test_loud_unnamed_flagged(self):
        adv = {"mac": "12:34:56:78:9A:BC", "rssi_dbm": -55}
        r = summarize_advertisement(adv)
        self.assertIn("un-named", r["tscm_note"])


if __name__ == "__main__":
    unittest.main()
