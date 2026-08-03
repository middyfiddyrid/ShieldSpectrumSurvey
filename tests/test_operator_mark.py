"""Operator-marked frequencies (POST /api/signals/manual).

The tech can click a frequency straight off the live spectrum/waterfall and send
it to review. The detector's threshold is not the operator's eye: a plainly
visible signal can sit under threshold (or inside a dampened "expected" range)
and never reach the ledger.

The load-bearing rule here is EVIDENTIARY: a frequency a human marked by eye is
NOT an instrument detection, and the two must stay distinguishable forever —
operator_marked=1, classification UNVERIFIED, and it lands in the review queue
rather than in findings.
"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from survey.db import Database                           # noqa: E402
from survey.config import Config                         # noqa: E402
from survey.server import create_app                     # noqa: E402
from survey.report import _freq_cell, export_csv, generate_report   # noqa: E402


class OperatorMarkTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = Database(os.path.join(self.dir, "t.db"))
        self.cfg = Config(os.path.join(self.dir, "config.yaml"))
        self.sid = self.db.create_session("1 A St", 0, 0, "test", 25, 5, "op")
        self.sweep = self.db.execute(
            "INSERT INTO sweeps (session_id,phase,status,started_utc) VALUES (?,?,?,?)",
            (self.sid, "interior", "done", time.time()))
        app = create_app(self.db, self.cfg)
        app.config["TESTING"] = True
        self.c = app.test_client()

    def _mark(self, **body):
        body.setdefault("center_mhz", 433.92)
        return self.c.post("/api/signals/manual", json=body)

    # ---- the evidentiary contract ----
    def test_marked_signal_is_flagged_as_operator_marked(self):
        r = self._mark()
        self.assertEqual(r.status_code, 200)
        row = self.db.query_one("SELECT * FROM signals WHERE id=?", (r.get_json()["id"],))
        self.assertEqual(row["operator_marked"], 1)
        self.assertIsNotNone(row["operator_marked_utc"])

    def test_marked_signal_is_never_presented_as_a_detection(self):
        """UNVERIFIED + a match_reason that says a human did it. If this ever
        regresses, an operator's guess could print as an instrument finding."""
        r = self._mark()
        row = self.db.query_one("SELECT * FROM signals WHERE id=?", (r.get_json()["id"],))
        self.assertEqual(row["classification"], "UNVERIFIED")
        self.assertIn("operator marked", row["match_reason"])

    def test_marked_signal_lands_in_the_review_queue(self):
        r = self._mark()
        row = self.db.query_one("SELECT * FROM signals WHERE id=?", (r.get_json()["id"],))
        self.assertEqual(row["triage_status"], "suspect")

    def test_detector_signals_are_not_flagged(self):
        """The flag must distinguish — a normal insert stays 0."""
        auto = self.db.execute(
            "INSERT INTO signals (sweep_id,session_id,center_mhz,classification,phase) "
            "VALUES (?,?,?,?,?)", (self.sweep, self.sid, 915.0, "UNVERIFIED", "interior"))
        row = self.db.query_one("SELECT operator_marked FROM signals WHERE id=?", (auto,))
        self.assertEqual(row["operator_marked"], 0)

    # ---- it has to reach the operator's lists ----
    def test_marked_signal_appears_in_the_signals_list(self):
        sig_id = self._mark().get_json()["id"]
        listed = self.c.get("/api/signals").get_json()
        self.assertIn(sig_id, [s["id"] for s in listed])

    def test_amplitude_and_note_are_kept(self):
        r = self._mark(peak_dbm=-72.5, note="visible spur, detector missed it")
        row = self.db.query_one("SELECT * FROM signals WHERE id=?", (r.get_json()["id"],))
        self.assertAlmostEqual(row["peak_dbm"], -72.5)
        self.assertIn("detector missed", row["triage_notes"])

    def test_links_to_the_segment_containing_the_frequency(self):
        seg = self.db.execute(
            "INSERT INTO segments (sweep_id,low_mhz,high_mhz,status) VALUES (?,?,?,?)",
            (self.sweep, 400.0, 500.0, "done"))
        r = self._mark(center_mhz=433.92)
        row = self.db.query_one("SELECT segment_id FROM signals WHERE id=?",
                                (r.get_json()["id"],))
        self.assertEqual(row["segment_id"], seg)

    def test_frequency_outside_every_segment_still_marks(self):
        """A mark made mid-segment has no stored segment yet — nullable, not an error."""
        r = self._mark(center_mhz=433.92)
        self.assertEqual(r.status_code, 200)
        row = self.db.query_one("SELECT segment_id FROM signals WHERE id=?",
                                (r.get_json()["id"],))
        self.assertIsNone(row["segment_id"])

    # ---- input handling ----
    def test_missing_frequency_is_rejected(self):
        self.assertEqual(self.c.post("/api/signals/manual", json={}).status_code, 400)

    def test_non_numeric_frequency_is_rejected(self):
        self.assertEqual(self._mark(center_mhz="banana").status_code, 400)

    def test_out_of_range_frequency_is_rejected(self):
        self.assertEqual(self._mark(center_mhz=-5).status_code, 400)

    def test_requires_a_sweep_to_attach_to(self):
        """sweep_id is NOT NULL — a session that never swept has nothing to hang
        the mark on, and must say so rather than 500."""
        db2 = Database(os.path.join(self.dir, "t2.db"))
        cfg2 = Config(os.path.join(self.dir, "c2.yaml"))
        db2.create_session("2 B St", 0, 0, "test", 25, 5, "op")
        app2 = create_app(db2, cfg2)
        app2.config["TESTING"] = True
        r = app2.test_client().post("/api/signals/manual", json={"center_mhz": 433.92})
        self.assertEqual(r.status_code, 400)
        self.assertIn("sweep", r.get_json()["error"].lower())


class OperatorMarkReportTest(unittest.TestCase):
    """The distinction has to survive into the deliverables. A marked frequency
    printed next to real detections without saying so is the failure that matters
    — that is a technician's guess reading as a measurement to counsel."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = Database(os.path.join(self.dir, "t.db"))
        self.cfg = Config(os.path.join(self.dir, "config.yaml"))
        self.sid = self.db.create_session("1 A St", 0, 0, "test", 25, 5, "op")
        self.sweep = self.db.execute(
            "INSERT INTO sweeps (session_id,phase,status,started_utc) VALUES (?,?,?,?)",
            (self.sid, "interior", "done", time.time()))

    def _sig(self, center, **kw):
        cols = {"sweep_id": self.sweep, "session_id": self.sid, "center_mhz": center,
                "phase": "interior", "module": "right", "room": "Office",
                "classification": "UNVERIFIED", "peak_dbm": -70.0, "bw_khz": 25.0,
                "persistence": 0.5, "matched_label": "", "match_reason": "",
                "anomaly_score": 50.0}
        cols.update(kw)
        keys = ",".join(cols)
        qs = ",".join("?" * len(cols))
        return self.db.execute(f"INSERT INTO signals ({keys}) VALUES ({qs})",
                               tuple(cols.values()))

    def test_report_builds_when_a_marked_signal_has_no_amplitude(self):
        """REGRESSION: the amplitude cells used an unguarded format, so a mark
        carrying no measured peak took down the whole PDF build."""
        self._sig(433.92, peak_dbm=None, operator_marked=1)
        out = generate_report(self.db, self.cfg, self.sid, self.dir)
        self.assertTrue(os.path.exists(out["path"]))
        self.assertGreater(os.path.getsize(out["path"]), 1000)

    def test_report_builds_with_a_normal_signal(self):
        self._sig(915.0)
        out = generate_report(self.db, self.cfg, self.sid, self.dir)
        self.assertTrue(os.path.exists(out["path"]))

    def test_csv_export_carries_the_flag(self):
        self._sig(433.92, operator_marked=1)
        self._sig(915.0)
        import csv as _csv
        path = export_csv(self.db, self.sid, self.dir)
        with open(path, encoding="utf-8", newline="") as f:
            rows = list(_csv.DictReader(f))
        # by column NAME, not position — column order changes as the schema grows
        self.assertIn("operator_marked", rows[0])
        by_freq = {r["center_mhz"]: r for r in rows}
        self.assertEqual(by_freq["433.92"]["operator_marked"], "1")
        self.assertEqual(by_freq["915.0"]["operator_marked"], "0")

    def test_freq_cell_tags_marked_and_leaves_detections_alone(self):
        marked = self.db.query_one("SELECT * FROM signals WHERE id=?",
                                   (self._sig(433.92, operator_marked=1),))
        auto = self.db.query_one("SELECT * FROM signals WHERE id=?", (self._sig(915.0),))
        self.assertIn("operator-marked", _freq_cell(marked))
        self.assertNotIn("operator-marked", _freq_cell(auto))


if __name__ == "__main__":
    unittest.main()
