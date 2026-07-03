"""Attribution ('is this about our client?') + the ECPA force-decode audit log.

Attribution records whether a detection captures the CLIENT'S own environment
(client_space => a bug targeting them) vs a third party / infrastructure (move
along, do NOT decode content). The decode_overrides table is the immutable legal
artifact: any time the operator overrides the attribution gate to decode content
of a non-client signal, it is logged with their ECPA acknowledgment + reason.
"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from survey.db import Database, ATTRIBUTION_STATUSES   # noqa: E402
from survey.config import Config                        # noqa: E402
from survey.server import create_app                    # noqa: E402


class TestAttribution(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = Database(os.path.join(self.dir, "t.db"))
        self.cfg = Config(os.path.join(self.dir, "config.yaml"))
        self.sid = self.db.create_session("1 A St", 0, 0, "test", 25, 5, "op")
        self.sweep = self.db.execute(
            "INSERT INTO sweeps (session_id,phase,status,started_utc) VALUES (?,?,?,?)",
            (self.sid, "interior", "done", time.time()))
        self.sig = self.db.execute(
            "INSERT INTO signals (sweep_id,session_id,center_mhz,classification,phase) "
            "VALUES (?,?,?,?,?)", (self.sweep, self.sid, 915.0, "UNVERIFIED", "interior"))
        app = create_app(self.db, self.cfg)
        app.config["TESTING"] = True
        self.c = app.test_client()

    # ---- DB layer ----
    def test_default_attribution_is_unassessed(self):
        row = self.db.query_one("SELECT attribution_status FROM signals WHERE id=?",
                                (self.sig,))
        self.assertEqual(row["attribution_status"], "unassessed")

    def test_set_and_read_attribution(self):
        row = self.db.set_signal_attribution(self.sig, self.sid, "client_space",
                                             "hear the client's living room on FM demod")
        self.assertEqual(row["attribution_status"], "client_space")
        self.assertIn("living room", row["attribution_notes"])
        self.assertIsNotNone(row["attribution_updated_utc"])

    def test_override_is_logged_immutably(self):
        before = self.db.list_decode_overrides(self.sid)
        self.assertEqual(before, [])
        row = self.db.add_decode_override(self.sid, self.sig, 915.0, "pocsag",
                                          "op", "confirming it's the client's pager")
        self.assertEqual(row["ecpa_ack"], 1)
        self.assertEqual(len(self.db.list_decode_overrides(self.sid)), 1)

    # ---- HTTP layer ----
    def test_attribution_endpoint_validates_enum(self):
        r = self.c.post(f"/api/signals/{self.sig}/attribution",
                        json={"status": "not_a_status"})
        self.assertEqual(r.status_code, 400)
        r = self.c.post(f"/api/signals/{self.sig}/attribution",
                        json={"status": "third_party", "notes": "neighbor wifi"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["attribution_status"], "third_party")

    def test_attribution_session_scoped(self):
        r = self.c.post("/api/signals/999999/attribution",
                        json={"status": "client_space"})
        self.assertEqual(r.status_code, 404)

    def test_override_requires_ack_and_reason(self):
        # missing ack
        r = self.c.post("/api/decode/override",
                        json={"reason": "x", "signal_id": self.sig})
        self.assertEqual(r.status_code, 400)
        # ack but no reason
        r = self.c.post("/api/decode/override",
                        json={"ecpa_ack": True, "signal_id": self.sig})
        self.assertEqual(r.status_code, 400)
        # both -> logged
        r = self.c.post("/api/decode/override",
                        json={"ecpa_ack": True, "reason": "client's own device",
                              "signal_id": self.sig, "center_mhz": 915.0, "mode": "pocsag"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["ecpa_ack"], 1)
        # visible in the audit log
        r = self.c.get("/api/decode/overrides")
        self.assertEqual(len(r.get_json()), 1)

    def test_all_statuses_accepted(self):
        for stt in ATTRIBUTION_STATUSES:
            r = self.c.post(f"/api/signals/{self.sig}/attribution", json={"status": stt})
            self.assertEqual(r.status_code, 200, stt)


if __name__ == "__main__":
    unittest.main()
