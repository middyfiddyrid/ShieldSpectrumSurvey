"""Demo gate: the RF-only fused ANALYZE must be reachable in the public demo,
while the PII paths (threat vectors, POI, internal memo, case files) stay 403."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from survey.db import Database          # noqa: E402
from survey.config import Config        # noqa: E402
from survey.server import create_app    # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
