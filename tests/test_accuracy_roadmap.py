"""Roadmap accuracy features: Wi-Fi OUI, experiments, sig library, could_also_be."""

import os
import tempfile
import unittest

from survey.wifi_oui import vendor_for_bssid, annotate_wifi_device
from survey.behavior import classify_behavior, COULD_ALSO_BE
from survey.experiments import build_record, summarize_outcome, KINDS, next_step_hint
from survey.sig_library import (
    make_ref_from_signal, add_ref, load_index, match_local, delete_ref)
from survey.rf_match import match_wifi
from survey.identify import iq_features
import numpy as np


class TestWifiOui(unittest.TestCase):
    def test_wyze(self):
        v = vendor_for_bssid("d0:03:9b:01:02:03")
        self.assertIsNotNone(v)
        self.assertIn("Wyze", v)

    def test_unknown_none(self):
        self.assertIsNone(vendor_for_bssid("ff:ff:ff:00:00:01"))

    def test_annotate(self):
        d = annotate_wifi_device({
            "identifier": "7c:78:b2:aa:bb:cc", "name": "cam", "extra": ""})
        self.assertIn("Reolink", d["vendor"])


class TestCouldAlsoBe(unittest.TestCase):
    def test_all_classes_have_list(self):
        for cid in COULD_ALSO_BE:
            self.assertTrue(COULD_ALSO_BE[cid])

    def test_behavior_includes_false_friends(self):
        b = classify_behavior({
            "center_mhz": 2437.0, "bw_khz": 18000, "persistence": 0.95})
        self.assertEqual(b["class_id"], "video_like")
        self.assertTrue(b.get("could_also_be"))
        self.assertTrue(any("Wi-Fi" in x or "camera" in x.lower()
                            for x in b["could_also_be"]))


class TestExperiments(unittest.TestCase):
    def test_kinds(self):
        self.assertIn("phone_test", KINDS)

    def test_build_and_summary(self):
        rec = build_record("phone_test", {
            "outcome": "energy_remains", "notes": "still hot"})
        s = summarize_outcome(rec)
        self.assertIn("silent", s.lower())
        self.assertTrue(next_step_hint("phone_test")["steps"])

    def test_bad_kind(self):
        with self.assertRaises(ValueError):
            build_record("jam_everything", {})


class TestSigLibrary(unittest.TestCase):
    def test_roundtrip_match(self):
        td = tempfile.mkdtemp()
        ref = make_ref_from_signal({
            "center_mhz": 433.92, "bw_khz": 25, "persistence": 0.05,
            "behavior": {"class_id": "keyfob_like"},
        }, label="front door fob", site_type="residential")
        add_ref(td, ref)
        self.assertEqual(len(load_index(td)), 1)
        m = match_local(433.9, 25, "keyfob_like", load_index(td))
        self.assertTrue(m)
        self.assertEqual(m[0]["label"], "front door fob")
        self.assertTrue(delete_ref(td, ref["id"]))


class TestRfMatchVendor(unittest.TestCase):
    def test_phrase_includes_vendor(self):
        inv = [{
            "kind": "wifi", "identifier": "d0:03:9b:11:22:33",
            "name": "CamAP", "freq_mhz": 2437.0, "channel": "6",
            "rssi_dbm": -40, "owned": 0, "extra": "", "vendor": "Wyze (camera/IoT)",
        }]
        m = match_wifi(2437.0, 20000, inv)
        self.assertTrue(m)
        self.assertIn("Wyze", m[0]["phrase"])


class TestBurstPeriodFeature(unittest.TestCase):
    def test_ook_has_burst_period_field(self):
        fs, n = 1e6, 32768
        t = np.arange(n) / fs
        car = np.exp(2j * np.pi * 50e3 * t)
        gate = np.zeros(n)
        on, off = 200, 1800
        for i in range(0, n, on + off):
            gate[i:i + on] = 1.0
        iq = (car * gate + 1e-3 * (np.random.randn(n) + 1j * np.random.randn(n))
              ).astype(np.complex64)
        np.random.seed(1)
        f = iq_features(iq, fs)
        self.assertIn("burst_period_ms", f)
        self.assertEqual(f["modulation_class"], "ook")


if __name__ == "__main__":
    unittest.main()
