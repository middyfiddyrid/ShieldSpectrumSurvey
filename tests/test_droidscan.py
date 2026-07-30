import unittest

from survey import droidscan as ds


class FakeAdb(ds.AdbRunner):
    """Canned adb output — no phone, no subprocess. Mirrors how netscan tests
    inject arp/ping output."""

    def __init__(self, canned):
        super().__init__("adb")
        self.canned = canned
        self.calls = []

    def run(self, *args, timeout=None):
        ds.assert_read_only(args)           # the guard still applies in tests
        self.calls.append(args)
        key = " ".join(args[1:]) if args and args[0] == "shell" else " ".join(args)
        for pat, out in self.canned.items():
            if key.startswith(pat):
                return 0, out, ""
        return 0, "", ""


class TestReadOnlyGuard(unittest.TestCase):
    def test_allows_reads(self):
        for args in (("devices",), ("shell", "getprop", "ro.product.model"),
                     ("shell", "dumpsys", "media_projection"),
                     ("shell", "settings", "get", "secure", "x"),
                     ("shell", "pm", "list", "packages", "-3")):
            ds.assert_read_only(args)

    def test_blocks_state_changes(self):
        bad = [
            ("uninstall", "com.foo"),
            ("shell", "pm", "uninstall", "com.foo"),
            ("shell", "settings", "put", "secure", "x", "1"),
            ("shell", "am", "force-stop", "com.foo"),
            ("push", "a", "b"),
            ("shell", "rm", "-rf", "/sdcard"),
            ("root",),
            ("shell", "cmd", "package", "install"),
        ]
        for args in bad:
            with self.assertRaises(ds.ReadOnlyViolation, msg=f"allowed {args!r}"):
                ds.assert_read_only(args)


class TestParsers(unittest.TestCase):
    def test_parse_devices(self):
        raw = ("List of devices attached\n"
               "R58M12ABCDE\tdevice\n39001FFAZ\tunauthorized\n")
        self.assertEqual(ds.parse_devices(raw), [
            {"serial": "R58M12ABCDE", "state": "device"},
            {"serial": "39001FFAZ", "state": "unauthorized"}])

    def test_parse_devices_empty(self):
        self.assertEqual(ds.parse_devices("List of devices attached\n\n"), [])

    def test_parse_component_list(self):
        raw = "com.spy.app/.WatchSvc:com.google.android.marvin.talkback/.TalkBackService"
        self.assertEqual(ds.parse_component_list(raw),
                         ["com.google.android.marvin.talkback", "com.spy.app"])

    def test_parse_component_list_null(self):
        for raw in ("null", "", "   ", None):
            self.assertEqual(ds.parse_component_list(raw), [])

    def test_parse_packages_installer(self):
        raw = ("package:com.good.app  installer=com.android.vending\n"
               "package:com.side.load  installer=null\n"
               "package:com.other  installer=com.example.dropper\n")
        got = ds.parse_packages(raw)
        self.assertEqual([p["pkg"] for p in got],
                         ["com.good.app", "com.side.load", "com.other"])
        self.assertEqual([p["sideloaded"] for p in got], [False, True, True])

    def test_parse_device_admins(self):
        raw = ("Enabled Device Admins (User 0):\n"
               "  ComponentInfo{com.spy.app/com.spy.app.Admin}\n"
               "  ComponentInfo{com.google.android.gms/.mdm.Receiver}\n")
        self.assertEqual(ds.parse_device_admins(raw),
                         ["com.google.android.gms", "com.spy.app"])

    def test_parse_media_projection_active_and_idle(self):
        active, ev = ds.parse_media_projection(
            "Media Projection\n  session=ProjectionToken{package=com.spy.app} valid=true")
        self.assertTrue(active)
        self.assertTrue(ev)
        idle, _ = ds.parse_media_projection("Media Projection\n  session=null")
        self.assertFalse(idle)

    def test_parse_granted_perms(self):
        raw = ("runtime permissions:\n"
               "  android.permission.RECORD_AUDIO: granted=true\n"
               "  android.permission.CAMERA: granted=true\n"
               "  android.permission.READ_SMS: granted=false\n")
        self.assertEqual(ds.parse_granted_perms(raw), ["camera", "mic"])

    def test_parse_connections_drops_loopback(self):
        raw = ("tcp ESTAB 0 0 10.0.0.5:44112 93.184.216.34:443\n"
               "tcp ESTAB 0 0 127.0.0.1:5555 127.0.0.1:99\n"
               "udp UNCONN 0 0 0.0.0.0:68 0.0.0.0:*\n")
        self.assertEqual(ds.parse_connections(raw), ["93.184.216.34:443"])


class TestScoring(unittest.TestCase):
    def test_stacked_app_outranks_single_flag(self):
        spy = {"accessibility": True, "admin": True, "sideloaded": True,
               "installer": "null", "perms": ["mic", "camera"]}
        plain = {"sideloaded": True, "installer": "null", "perms": []}
        s_spy, stack = ds.score_app(spy)
        s_plain, _ = ds.score_app(plain)
        self.assertGreater(s_spy, s_plain)
        self.assertGreaterEqual(s_spy, 6)
        self.assertTrue(any("accessibility" in x for x in stack))

    def test_clean_store_app_scores_zero(self):
        self.assertEqual(ds.score_app({"sideloaded": False, "perms": []})[0], 0)

    def test_summarize_orders_and_buckets(self):
        apps = [{"pkg": "a", "score": 1}, {"pkg": "b", "score": 8},
                {"pkg": "c", "score": 4}]
        ranked, leads, watch = ds.summarize(apps)
        self.assertEqual([a["pkg"] for a in ranked], ["b", "c", "a"])
        self.assertEqual([a["pkg"] for a in leads], ["b"])
        self.assertEqual([a["pkg"] for a in watch], ["c"])


class TestScan(unittest.TestCase):
    def _canned(self):
        return {
            "getprop ro.product.model": "SM-G991B",
            "getprop ro.product.brand": "samsung",
            "getprop ro.build.version.release": "13",
            "getprop ro.build.version.security_patch": "2024-01-01",
            "dumpsys media_projection": "session=ProjectionToken{package=com.spy.app} valid=true",
            "settings get secure enabled_accessibility_services":
                "com.spy.app/.WatchSvc",
            "settings get secure enabled_notification_listeners": "null",
            "dumpsys device_policy":
                "ComponentInfo{com.spy.app/com.spy.app.Admin}",
            "cmd appops query-op SYSTEM_ALERT_WINDOW allow": "",
            "pm list packages -3 -i":
                ("package:com.spy.app installer=null\n"
                 "package:com.good.app installer=com.android.vending\n"),
            "dumpsys package com.spy.app":
                ("android.permission.RECORD_AUDIO: granted=true\n"
                 "android.permission.CAMERA: granted=true\n"),
            "dumpsys package com.good.app": "",
            "ss -tunH": "tcp ESTAB 0 0 10.0.0.5:4 203.0.113.9:443\n",
        }

    def test_scan_surfaces_the_stacked_app_as_lead(self):
        res = ds.scan(FakeAdb(self._canned()))
        self.assertEqual(res["phone"]["model"], "SM-G991B")
        self.assertIn("com.spy.app", res["leads"])
        self.assertNotIn("com.good.app", res["leads"])
        top = res["apps"][0]
        self.assertEqual(top["pkg"], "com.spy.app")
        self.assertTrue(top["accessibility"])
        self.assertTrue(top["admin"])
        self.assertEqual(sorted(top["perms"]), ["camera", "mic"])

    def test_scan_reports_active_projection_as_high(self):
        res = ds.scan(FakeAdb(self._canned()))
        mp = [f for f in res["findings"] if f["id"] == "media_projection"][0]
        self.assertEqual(mp["severity"], "high")

    def test_scan_never_emits_a_verdict(self):
        """The whole point: no field anywhere says clean/infected/safe."""
        res = ds.scan(FakeAdb(self._canned()))
        blob = repr(res).lower()
        for word in ("infected", "malware detected", "is clean", "no threats",
                     "verdict"):
            self.assertNotIn(word, blob)
        self.assertNotIn("verdict", res)

    def test_quiet_phone_yields_no_leads_but_still_no_clean_claim(self):
        canned = {
            "getprop ro.product.model": "Pixel 7",
            "dumpsys media_projection": "session=null",
            "settings get secure enabled_accessibility_services": "null",
            "settings get secure enabled_notification_listeners": "null",
            "dumpsys device_policy": "",
            "pm list packages -3 -i":
                "package:com.good.app installer=com.android.vending\n",
            "dumpsys package com.good.app": "",
            "ss -tunH": "",
        }
        res = ds.scan(FakeAdb(canned))
        self.assertEqual(res["leads"], [])
        self.assertTrue(any(f["severity"] == "clear" for f in res["findings"]))
        # an unreadable /proc/net must be reported as a LIMIT, not a finding
        self.assertTrue(any("root" in l.lower() for l in res["limits"]))

    def test_scan_makes_only_read_only_calls(self):
        fake = FakeAdb(self._canned())
        ds.scan(fake)
        for args in fake.calls:
            ds.assert_read_only(args)       # raises if scan ever drifts


class TestStatusHints(unittest.TestCase):
    def test_no_adb_gives_install_hint(self):
        real = ds.find_adb
        ds.find_adb = lambda base_dir="": None
        try:
            s = ds.status()
            self.assertFalse(s["ready"])
            self.assertEqual(s["state"], "no_adb")
            self.assertIn("platform-tools", s["hint"])
        finally:
            ds.find_adb = real


if __name__ == "__main__":
    unittest.main()
