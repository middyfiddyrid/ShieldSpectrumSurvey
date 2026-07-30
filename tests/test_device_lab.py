"""Device Capture Lab — catalog matching and package lifecycle (no radio)."""

import math
import os
import tempfile
import time
import unittest

from survey.device_lab import (
    match_catalog, populate_from_catalog, new_package, save_package,
    load_package, next_instruction, advance_after_distance, ANTENNA_TYPES,
    save_photo_b64, add_stop, exercise_states_for, fuse_ident, stop_order,
    walk_score, save_video_b64, DEVICE_CATALOG,
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
        # near-field FIRST (find + prove), then out; ladder ends in states
        pkg = {}
        self.assertEqual(advance_after_distance(pkg, "near_field"), "capture_ten_ft")
        self.assertEqual(advance_after_distance(pkg, "ten_ft"), "capture_next_room")
        self.assertEqual(advance_after_distance(pkg, "next_room"), "states")

    def test_advance_with_custom_stops(self):
        pkg = new_package({})
        stop = add_stop(pkg, "Down the hall")
        self.assertEqual(advance_after_distance(pkg, "next_room"),
                         "capture_stop:" + stop["id"])
        self.assertEqual(advance_after_distance(pkg, stop["id"]), "states")
        self.assertIn(stop["id"], stop_order(pkg))
        with self.assertRaises(ValueError):
            add_stop(pkg, "Down the hall")
        g = next_instruction({**pkg, "step": "capture_stop:" + stop["id"]})
        self.assertIn("Down the hall", " ".join(g["do"]))

    def test_exercise_states(self):
        cam = exercise_states_for("wifi_camera")
        self.assertTrue(any(s["id"] == "record" for s in cam))
        self.assertTrue(any(s["id"] == "off" for s in cam))
        default = exercise_states_for("nope_no_such")
        self.assertTrue(any(s["id"] == "off" for s in default))
        for ent in DEVICE_CATALOG:
            states = exercise_states_for(ent["id"])
            self.assertTrue(states, ent["id"])
            self.assertTrue(all(s.get("instruction") for s in states))

    def test_walk_score_tracks(self):
        t0, period = 1000.0, 8.0
        trace = []
        for i in range(160):
            t = t0 + i * 0.125
            ref = -math.cos(2 * math.pi * (t - t0) / period)
            trace.append((t, -60.0 + 10.0 * ref))
        res = walk_score(trace, t0, period)
        self.assertEqual(res["verdict"], "tracks_motion")
        self.assertGreater(res["r"], 0.9)

    def test_walk_score_flat_and_random(self):
        t0 = 1000.0
        flat = [(t0 + i * 0.125, -80.0) for i in range(160)]
        self.assertEqual(walk_score(flat, t0, 8.0)["verdict"], "flat")
        import random
        rng = random.Random(7)
        noisy = [(t0 + i * 0.125, -60 + rng.uniform(-8, 8)) for i in range(160)]
        self.assertIn(walk_score(noisy, t0, 8.0)["verdict"],
                      ("no_track", "weak_track"))
        short = [(t0 + i * 0.125, -60.0 - i) for i in range(20)]
        self.assertEqual(walk_score(short, t0, 8.0)["verdict"], "keep_going")

    def test_fuse_ident_ladder(self):
        pkg = new_package({})
        self.assertEqual(fuse_ident(pkg)["status"], "unknown")
        pkg["device_name"] = "Wyze cam"
        pkg["device_description"] = "wifi camera cube"
        self.assertEqual(fuse_ident(pkg)["status"], "visual_only")
        cand = [{"name": "WiFi OFDM", "score": 40}]
        pkg["captures"] = {"near_field": {"candidates": cand},
                           "ten_ft": {"candidates": cand}}
        self.assertEqual(fuse_ident(pkg)["status"], "rf_confirmed")
        pkg["walk_test"] = {"verdict": "tracks_motion", "r": 0.9}
        fused = fuse_ident(pkg)
        self.assertEqual(fused["status"], "identified")
        self.assertTrue(fused["attributed"])
        self.assertEqual(fused["rf"][0]["seen"], 2)
        self.assertIn("IDENTIFIED", fused["summary"])

    def test_video_save(self):
        td = tempfile.mkdtemp()
        import base64
        raw = b"\x1aE\xdf\xa3" + b"\x00" * 2000
        url = "data:video/webm;base64," + base64.b64encode(raw).decode()
        path = save_video_b64(td, "abc123", url)
        self.assertTrue(path.endswith(".webm"))
        self.assertTrue(os.path.isfile(os.path.join(td, path.replace("/", os.sep))))
        with self.assertRaises(ValueError):
            save_video_b64(td, "abc123", "data:video/webm;base64,AAAA")

    def test_new_package_has_live_fields(self):
        pkg = new_package({})
        for k in ("stops", "states", "walk_test", "videos", "ident"):
            self.assertIn(k, pkg)

    def test_next_instruction_all_steps(self):
        pkg = new_package({})
        for step in ("photo", "identify", "antenna", "freq_confirm",
                     "capture_near_field", "capture_ten_ft",
                     "capture_next_room", "states", "stimulate", "demod",
                     "ready_to_commit", "committed"):
            g = next_instruction({**pkg, "step": step})
            self.assertTrue(g["title"], step)
            self.assertTrue(g["do"], step)

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
