"""Device Capture Lab — catalog matching and package lifecycle (no radio)."""

import os
import tempfile
import unittest

from survey.device_lab import (
    match_catalog, populate_from_catalog, new_package, save_package,
    load_package, next_instruction, advance_after_distance, ANTENNA_TYPES,
    save_photo_b64,
)


class TestDeviceLab(unittest.TestCase):
    def test_match_keyfob(self):
        hits = match_catalog("Toyota key fob remote")
        self.assertTrue(hits)
        self.assertIn("fob", hits[0]["id"] + hits[0]["name"].lower())

    def test_match_wyze(self):
        hits = match_catalog("Wyze security cam")
        self.assertTrue(any("camera" in h["name"].lower() or "wifi" in h["id"]
                            for h in hits))

    def test_populate_sets_freq(self):
        pkg = new_package({"model": "B210"})
        populate_from_catalog(pkg, "keyfob_433")
        self.assertEqual(pkg["device_type_id"], "keyfob_433")
        self.assertTrue(pkg["center_mhz"])
        self.assertTrue(pkg["freq_guesses_mhz"])

    def test_package_roundtrip(self):
        td = tempfile.mkdtemp()
        pkg = new_package({"driver": "b220"})
        pkg["device_name"] = "test"
        save_package(td, pkg)
        loaded = load_package(td, pkg["id"])
        self.assertEqual(loaded["device_name"], "test")
        g = next_instruction(loaded)
        self.assertIn("Photo", g["title"])

    def test_advance_distances(self):
        pkg = {}
        self.assertEqual(advance_after_distance(pkg, "ten_ft"), "capture_next_room")
        self.assertEqual(advance_after_distance(pkg, "near_field"), "stimulate")

    def test_antennas_nonempty(self):
        self.assertGreater(len(ANTENNA_TYPES), 5)

    def test_photo_save(self):
        td = tempfile.mkdtemp()
        # minimal 1x1 jpeg base64
        import base64
        # tiny valid-ish jpeg header + padding
        raw = b"\xff\xd8\xff\xd9" + b"\x00" * 200
        url = "data:image/jpeg;base64," + base64.b64encode(raw).decode()
        path = save_photo_b64(td, "abc123", url)
        self.assertTrue(path.endswith(".jpg"))
        self.assertTrue(os.path.isfile(os.path.join(td, path.replace("/", os.sep))))


if __name__ == "__main__":
    unittest.main()
