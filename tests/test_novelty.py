"""New-to-environment (novelty) detection.

Seeds a PRIOR session at an address with a Wiki capture, then a current session.
A signal at the previously-seen frequency is NOT novel; one at a fresh frequency
IS. Covers compute_novelty (pure), mark_session_novelty (column write), and the
/api/novelty route.
"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from survey.db import Database                                      # noqa: E402
from survey.config import Config                                    # noqa: E402
from survey.novelty import compute_novelty, mark_session_novelty    # noqa: E402
from survey.server import create_app                                # noqa: E402


def _capture(db, session_id, center_mhz, captured_utc, peak=-60, bw=20):
    db.execute(
        "INSERT INTO captures (session_id,kind,center_mhz,bw_khz,peak_dbm,floor_dbm,"
        "captured_utc) VALUES (?,?,?,?,?,?,?)",
        (session_id, "observation", center_mhz, bw, peak, -110, captured_utc))


class TestNovelty(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = Database(os.path.join(self.dir, "t.db"))
        self.cfg = Config(os.path.join(self.dir, "config.yaml"))
        self.addr = "123 Main St"
        past = time.time() - 100000          # clearly before the current session
        self.prior = self.db.create_session(self.addr, 0, 0, "test", 25, 5, "op")
        _capture(self.db, self.prior, 915.000, past, peak=-60, bw=20)
        # current session is created last -> AppState selects it as the open one
        self.cur = self.db.create_session(self.addr, 0, 0, "test", 25, 5, "op")
        self.cur_created = self.db.query_one(
            "SELECT created_utc FROM sessions WHERE id=?", (self.cur,))["created_utc"]

    def test_seen_before_not_novel(self):
        nov = compute_novelty(self.db, self.addr, 915.01, self.cur_created)
        self.assertFalse(nov["is_novel_to_address"])
        self.assertGreaterEqual(nov["prior_sessions"], 1)
        self.assertGreaterEqual(nov["variant_count"], 1)

    def test_new_frequency_is_novel(self):
        nov = compute_novelty(self.db, self.addr, 2412.0, self.cur_created)
        self.assertTrue(nov["is_novel_to_address"])
        self.assertIn("new frequency", nov["novelty_reason"])
        self.assertTrue(nov["differences"]["new_freq"])

    def test_different_address_is_novel(self):
        # same frequency, but a DIFFERENT address has no prior sighting
        nov = compute_novelty(self.db, "999 Other Rd", 915.0, time.time())
        self.assertTrue(nov["is_novel_to_address"])

    def test_mark_session_sets_flags(self):
        sweep = self.db.execute(
            "INSERT INTO sweeps (session_id,phase,status,started_utc) VALUES (?,?,?,?)",
            (self.cur, "interior", "done", time.time()))
        novel_sig = self.db.execute(
            "INSERT INTO signals (sweep_id,session_id,center_mhz,peak_dbm,bw_khz,phase) "
            "VALUES (?,?,?,?,?,?)", (sweep, self.cur, 2412.0, -55, 20, "interior"))
        seen_sig = self.db.execute(
            "INSERT INTO signals (sweep_id,session_id,center_mhz,peak_dbm,bw_khz,phase) "
            "VALUES (?,?,?,?,?,?)", (sweep, self.cur, 915.0, -58, 20, "interior"))
        n = mark_session_novelty(self.db, self.cur)
        self.assertEqual(n, 1)
        self.assertEqual(self.db.query_one(
            "SELECT is_novel_to_address FROM signals WHERE id=?",
            (novel_sig,))["is_novel_to_address"], 1)
        self.assertEqual(self.db.query_one(
            "SELECT is_novel_to_address FROM signals WHERE id=?",
            (seen_sig,))["is_novel_to_address"], 0)

    def test_api_novelty_route(self):
        c = create_app(self.db, self.cfg).test_client()
        novel = c.get("/api/novelty?center_mhz=2412.0").get_json()
        self.assertTrue(novel["is_novel_to_address"])
        self.assertEqual(novel["address"], self.addr)
        seen = c.get("/api/novelty?center_mhz=915.01").get_json()
        self.assertFalse(seen["is_novel_to_address"])

    def test_api_novelty_requires_arg(self):
        c = create_app(self.db, self.cfg).test_client()
        self.assertEqual(c.get("/api/novelty").status_code, 400)


if __name__ == "__main__":
    unittest.main()
