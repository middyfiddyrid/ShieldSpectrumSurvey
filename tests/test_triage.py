"""Gated MANUAL triage endpoints (post-hoc, no radio). First Flask-client test.

Seeds a session + an interior sweep with a mix of explained (VERIFIED) and
unexplained (UNVERIFIED / caution) detections, then exercises the review list,
the per-signal decision, validation, session-scoping, and the completion gate.
"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from survey.db import Database          # noqa: E402
from survey.config import Config        # noqa: E402
from survey.server import create_app    # noqa: E402


def _signal(db, sweep, session, center, *, classification="UNVERIFIED", caution=0,
            anomaly=0.0):
    return db.execute(
        "INSERT INTO signals (sweep_id,session_id,center_mhz,classification,caution,"
        "anomaly_score,phase) VALUES (?,?,?,?,?,?,?)",
        (sweep, session, center, classification, caution, anomaly, "interior"))


class TestTriage(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = Database(os.path.join(self.dir, "t.db"))
        self.cfg = Config(os.path.join(self.dir, "config.yaml"))
        self.sid = self.db.create_session("1 A St", 0, 0, "test", 25, 5, "op")
        self.sweep = self.db.execute(
            "INSERT INTO sweeps (session_id,phase,status,started_utc) VALUES (?,?,?,?)",
            (self.sid, "interior", "done", time.time()))
        # explained: VERIFIED, no caution -> must be EXCLUDED from the triage list
        _signal(self.db, self.sweep, self.sid, 100.0,
                classification="VERIFIED", caution=0, anomaly=1.0)
        # unexplained: high-anomaly UNVERIFIED + a VERIFIED-but-caution
        self.u1 = _signal(self.db, self.sweep, self.sid, 433.92,
                          classification="UNVERIFIED", anomaly=9.0)
        self.u2 = _signal(self.db, self.sweep, self.sid, 915.0,
                          classification="VERIFIED", caution=1, anomaly=5.0)
        self.app = create_app(self.db, self.cfg)
        self.c = self.app.test_client()

    def test_list_only_unexplained_ordered_by_anomaly(self):
        data = self.c.get("/api/triage/last").get_json()
        self.assertEqual(data["sweep_id"], self.sweep)
        ids = [s["id"] for s in data["signals"]]
        self.assertEqual(set(ids), {self.u1, self.u2})       # VERIFIED/no-caution excluded
        self.assertEqual(ids[0], self.u1)                    # 9.0 before 5.0
        self.assertEqual(data["remaining_unexplained"], 2)
        self.assertEqual(data["counts"]["unreviewed"], 2)

    def test_set_decision_updates_and_decrements_remaining(self):
        r = self.c.post(f"/api/signals/{self.u1}/triage",
                        json={"status": "flagged", "notes": "recheck on site"})
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body["triage_status"], "flagged")
        self.assertEqual(body["triage_notes"], "recheck on site")   # notes round-trip
        data = self.c.get("/api/triage/last").get_json()
        self.assertEqual(data["remaining_unexplained"], 1)
        self.assertEqual(data["counts"].get("flagged"), 1)

    def test_invalid_status_400(self):
        r = self.c.post(f"/api/signals/{self.u1}/triage", json={"status": "bogus"})
        self.assertEqual(r.status_code, 400)

    def test_unreviewed_not_an_operator_decision(self):
        # 'unreviewed' is the default state, not a settable decision
        r = self.c.post(f"/api/signals/{self.u1}/triage", json={"status": "unreviewed"})
        self.assertEqual(r.status_code, 400)

    def test_cross_session_signal_404(self):
        other = self.db.create_session("2 B St", 0, 0, "test", 25, 5, "op")
        osw = self.db.execute(
            "INSERT INTO sweeps (session_id,phase,status,started_utc) VALUES (?,?,?,?)",
            (other, "interior", "done", time.time()))
        osig = _signal(self.db, osw, other, 200.0)
        # current open session is still self.sid (set at create_app time)
        r = self.c.post(f"/api/signals/{osig}/triage", json={"status": "cleared"})
        self.assertEqual(r.status_code, 404)

    def test_bulk_triage_updates_many(self):
        r = self.c.post("/api/triage/bulk",
                        json={"signal_ids": [self.u1, self.u2], "status": "cleared",
                              "notes": "bulk test"})
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body["updated"], 2)
        data = self.c.get("/api/triage/last").get_json()
        self.assertEqual(data["remaining_unexplained"], 0)
        self.assertEqual(data["counts"].get("cleared"), 2)


if __name__ == "__main__":
    unittest.main()
