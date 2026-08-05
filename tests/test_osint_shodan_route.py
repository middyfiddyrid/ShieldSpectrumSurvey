"""Route wiring for the internal-only Shodan exposure check
(/api/osint/shodan/run) — mirrors the netscan route pattern.

Proves: no key degrades cleanly to 'not_configured' (never a 500), a key +
mocked HTTP returns classified hosts, and the demo build blocks the route."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from survey.db import Database          # noqa: E402
from survey.config import Config        # noqa: E402
from survey.server import create_app    # noqa: E402


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


_MATCHES = {"matches": [
    {"ip_str": "203.0.113.5", "port": 554, "product": "Hikvision IP Camera",
     "org": "Example ISP", "location": {"city": "Rolla"},
     "data": "RTSP/1.0 200 OK", "tags": ["camera"], "timestamp": "2026-07-01T00:00:00"},
]}


def _app(demo=False, key=""):
    d = tempfile.mkdtemp()
    db = Database(os.path.join(d, "t.db"))
    cfg = Config(os.path.join(d, "config.yaml"))
    if key:
        cfg.data.setdefault("api_keys", {})["shodan"] = key
    db.create_session("1 A St", 38.6, -91.5, "test", 25, 5, "op")
    return create_app(db, cfg, demo=demo).test_client()


class ExposureRouteTests(unittest.TestCase):
    def test_not_configured_without_key(self):
        r = _app().post("/api/osint/shodan/run", json={})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["status"], "not_configured")

    def test_ok_with_key_and_mocked_http(self):
        c = _app(key="KEY")
        with patch("survey.osint_shodan.requests.get", return_value=_Resp(200, _MATCHES)):
            r = c.post("/api/osint/shodan/run", json={})
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["hosts"][0]["device_class"], "camera")
        self.assertIn("Shodan", body["attribution"])

    def test_demo_build_blocks_route(self):
        r = _app(demo=True).post("/api/osint/shodan/run", json={})
        self.assertEqual(r.status_code, 403)


if __name__ == "__main__":
    unittest.main()
