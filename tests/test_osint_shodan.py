import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from survey.baseline.base import DiskCache  # noqa: E402
from survey.osint_shodan import (  # noqa: E402
    ShodanExposure, classify_exposed)


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


_MATCHES = {
    "matches": [
        {"ip_str": "203.0.113.5", "port": 554, "transport": "tcp",
         "product": "Hikvision IP Camera", "org": "Example ISP",
         "location": {"latitude": 38.6, "longitude": -91.5, "city": "Rolla"},
         "data": "RTSP/1.0 200 OK", "tags": ["camera"], "timestamp": "2026-07-01"},
        {"ip_str": "203.0.113.9", "port": 80, "transport": "tcp",
         "product": "nginx", "org": "Example ISP",
         "location": {"latitude": 38.6, "longitude": -91.5, "city": "Rolla"},
         "data": "HTTP/1.1 200", "tags": []},
        # duplicate ip:port should be de-duped
        {"ip_str": "203.0.113.5", "port": 554, "transport": "tcp",
         "product": "Hikvision IP Camera", "location": {}, "data": ""},
    ]
}


class ClassifyTests(unittest.TestCase):
    def test_camera_wins_over_generic(self):
        self.assertEqual(classify_exposed("Hikvision IP Camera", "", []), "camera")

    def test_banner_and_tags_considered(self):
        self.assertEqual(classify_exposed("", "Synology DiskStation", []), "nas")
        self.assertEqual(classify_exposed("", "", ["router"]), "router")

    def test_unknown_when_no_match(self):
        self.assertEqual(classify_exposed("nginx", "HTTP/1.1 200", []), "unknown")


class ShodanExposureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(__file__).resolve().parent / "_shodan_cache_tmp"
        self.cache = DiskCache(str(self.tmp))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_not_configured_without_key(self):
        res = ShodanExposure(self.cache, "").fetch(38.6, -91.5, 1.0)
        self.assertEqual(res.status, "not_configured")
        self.assertEqual(res.count, 0)

    def test_ok_parses_dedupes_and_sorts(self):
        with patch("survey.osint_shodan.requests.get", return_value=_Resp(200, _MATCHES)):
            res = ShodanExposure(self.cache, "KEY").fetch(38.6, -91.5, 1.0)
        self.assertEqual(res.status, "ok")
        self.assertEqual(res.count, 2)                       # duplicate dropped
        self.assertEqual(res.hosts[0]["device_class"], "camera")  # camera sorted first
        self.assertIn("camera/DVR", res.message)
        self.assertIn("Shodan", res.attribution)

    def test_denied_on_403(self):
        with patch("survey.osint_shodan.requests.get", return_value=_Resp(403)):
            res = ShodanExposure(self.cache, "BADKEY").fetch(38.6, -91.5, 1.0)
        self.assertEqual(res.status, "denied")

    def test_empty_when_no_matches(self):
        with patch("survey.osint_shodan.requests.get",
                   return_value=_Resp(200, {"matches": []})):
            res = ShodanExposure(self.cache, "KEY").fetch(38.6, -91.5, 1.0)
        self.assertEqual(res.status, "empty")

    def test_network_failure_degrades_to_cache(self):
        src = ShodanExposure(self.cache, "KEY")
        with patch("survey.osint_shodan.requests.get", return_value=_Resp(200, _MATCHES)):
            src.fetch(38.6, -91.5, 1.0)                       # warms cache
        with patch("survey.osint_shodan.requests.get", side_effect=OSError("no net")):
            res = src.fetch(38.6, -91.5, 1.0)                 # same params -> cache hit
        self.assertEqual(res.status, "ok")
        self.assertEqual(res.count, 2)
        self.assertIn("live query failed", res.message)

    def test_radius_capped_km(self):
        # 100 mi request must be clamped to RADIUS_CAP_MI before km conversion
        self.assertLessEqual(ShodanExposure(self.cache, "K")._radius_km(100.0),
                             2.0 * 1.609344 + 0.001)


if __name__ == "__main__":
    unittest.main()
