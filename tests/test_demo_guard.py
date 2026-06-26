"""Demo gate: the RF-only fused ANALYZE must be reachable in the public demo,
while the PII paths (threat vectors, POI, internal memo, case files) stay 403."""

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from survey.db import Database          # noqa: E402
from survey.config import Config        # noqa: E402
from survey.server import create_app    # noqa: E402
from survey.tones import dtmf_encode    # noqa: E402


class _DtmfStub:
    """SDR stub whose capture always returns a decodable DTMF signal."""
    is_swept_sdr = True

    def __init__(self):
        self.info = types.SimpleNamespace(
            modules=[types.SimpleNamespace(min_mhz=70.0, max_mhz=6000.0, side="right")],
            simulated=True)

    def capture_iq(self, center_hz, fs, n):
        return dtmf_encode("159D", float(fs))


class TestDemoGuard(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = Database(os.path.join(self.dir, "t.db"))
        self.cfg = Config(os.path.join(self.dir, "config.yaml"))
        self.db.create_session("1 A St", 0, 0, "test", 25, 5, "op")
        self.c = create_app(self.db, self.cfg, demo=True).test_client()

    def test_analyze_signal_available_in_demo(self):
        # RF-only, no PII -> must NOT be demo-blocked (no device -> 200 + note)
        r = self.c.post("/api/analyze/signal", json={"center_mhz": 433.92})
        self.assertNotEqual(r.status_code, 403)
        self.assertEqual(r.status_code, 200)

    def test_threat_vectors_blocked_in_demo(self):
        r = self.c.post("/api/analyze/threat_vectors", json={})
        self.assertEqual(r.status_code, 403)

    def test_poi_blocked_in_demo(self):
        self.assertEqual(self.c.get("/api/poi").status_code, 403)

    def test_internal_memo_blocked_in_demo(self):
        self.assertEqual(
            self.c.post("/api/report/internal_memo", json={}).status_code, 403)

    def test_triage_and_novelty_blocked_in_demo(self):
        # operator-authored triage notes + address novelty history are internal
        self.assertEqual(self.c.get("/api/triage/last").status_code, 403)
        self.assertEqual(self.c.get("/api/novelty?center_mhz=915").status_code, 403)

    def test_recordings_blocked_in_demo(self):
        # raw audio of possibly-private comms must not be reachable in the demo
        self.assertEqual(self.c.get("/api/recordings").status_code, 403)
        self.assertEqual(
            self.c.post("/api/record/start", json={"center_mhz": 433.0}).status_code, 403)

    def _analyze_decode(self, demo):
        d = tempfile.mkdtemp()
        db = Database(os.path.join(d, "t.db"))
        cfg = Config(os.path.join(d, "config.yaml"))
        cfg.data["pluto"]["listen_sample_rate"] = 48000
        db.create_session("1 A St", 0, 0, "test", 25, 5, "op")
        app = create_app(db, cfg, demo=demo)
        st = app.config["APP_STATE"]
        st.controller.device = _DtmfStub()
        st.controller.active_source = "pluto"
        return app.test_client().post(
            "/api/analyze/signal", json={"center_mhz": 462.5, "mode": "dtmf"}).get_json()

    def test_decoded_content_present_normally_but_stripped_in_demo(self):
        # normal: the DTMF signal decodes -> decode payload present
        normal = self._analyze_decode(demo=False)
        self.assertIsNotNone(normal["decode"])
        self.assertTrue(normal["decode"]["messages"])
        # demo: same signal, but decoded (intercepted) content is stripped
        demo = self._analyze_decode(demo=True)
        self.assertIsNone(demo["decode"])
        self.assertNotEqual(demo.get("interpretation", {}).get("readable"), "decoded")


if __name__ == "__main__":
    unittest.main()
