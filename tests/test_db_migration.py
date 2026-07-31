"""Migration tests: opening a Database on an OLD schema (signals without the
triage/novelty columns, no recordings table) must additively upgrade it, apply
DEFAULTs to existing rows, and be safe to run repeatedly (idempotent).
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from survey.db import Database, TRIAGE_STATUSES  # noqa: E402

# A pre-migration signals table: the columns that existed before this build.
OLD_SIGNALS = """
CREATE TABLE signals (
    id INTEGER PRIMARY KEY,
    sweep_id INTEGER NOT NULL,
    session_id INTEGER NOT NULL,
    segment_id INTEGER,
    room TEXT, module TEXT, phase TEXT,
    center_mhz REAL NOT NULL, bw_khz REAL,
    peak_dbm REAL, avg_dbm REAL, floor_dbm REAL,
    persistence REAL,
    classification TEXT,
    matched_label TEXT, match_reason TEXT,
    caution INTEGER DEFAULT 0,
    anomaly_score REAL DEFAULT 0,
    interior_delta_db REAL,
    localization_score REAL, strongest_room TEXT
);
"""


class TestMigration(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "old.db")
        # Seed an old database by hand: just the legacy signals table + one row.
        conn = sqlite3.connect(self.path)
        conn.executescript(OLD_SIGNALS)
        conn.execute(
            "INSERT INTO signals (sweep_id,session_id,center_mhz) VALUES (1,1,915.0)")
        conn.commit()
        conn.close()

    def _cols(self, table):
        conn = sqlite3.connect(self.path)
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        conn.close()
        return cols

    def test_signals_gains_triage_and_novelty_columns(self):
        Database(self.path)  # __init__ runs SCHEMA then _migrate()
        cols = self._cols("signals")
        for col in ("triage_status", "triage_notes", "triage_updated_utc",
                    "is_novel_to_address", "novelty_reason"):
            self.assertIn(col, cols, f"migration did not add signals.{col}")

    def test_default_applied_to_existing_row(self):
        db = Database(self.path)
        row = db.query_one("SELECT triage_status, is_novel_to_address, novelty_reason "
                           "FROM signals WHERE center_mhz=915.0")
        self.assertEqual(row["triage_status"], "unreviewed")
        self.assertEqual(row["is_novel_to_address"], 0)
        self.assertEqual(row["novelty_reason"], "")

    def test_recordings_table_created(self):
        Database(self.path)
        self.assertIn("recordings", self._table_names())
        cols = self._cols("recordings")
        for col in ("session_id", "center_mhz", "file_path", "kind", "duration_s"):
            self.assertIn(col, cols)

    def test_signals_gains_identify_columns(self):
        db = Database(self.path)
        cols = self._cols("signals")
        for col in ("id_top_name", "id_top_score", "id_device_class",
                    "id_mode", "id_candidates_json", "id_updated_utc"):
            self.assertIn(col, cols, f"migration did not add signals.{col}")
        # existing legacy row gets the TEXT default, not NULL
        row = db.query_one("SELECT id_top_name, id_mode FROM signals "
                           "WHERE center_mhz=915.0")
        self.assertEqual(row["id_top_name"], "")
        self.assertEqual(row["id_mode"], "")

    def test_df_log_table_created(self):
        Database(self.path)
        self.assertIn("df_log", self._table_names())
        cols = self._cols("df_log")
        for col in ("session_id", "center_mhz", "bearing_deg", "mirror_deg",
                    "coherence", "spacing_m", "calibrated", "ts_utc"):
            self.assertIn(col, cols)

    def test_set_signal_identification_roundtrip(self):
        db = Database(self.path)
        row = db.query_one("SELECT id FROM signals WHERE center_mhz=915.0")
        cands = [{"name": "BLE tracker", "score": 88.0,
                  "device_class": "ble_tracker", "reasons": ["in band"]}]
        updated = db.set_signal_identification(row["id"], 1, cands, "iq")
        self.assertEqual(updated["id_top_name"], "BLE tracker")
        self.assertEqual(updated["id_device_class"], "ble_tracker")
        self.assertEqual(updated["id_mode"], "iq")
        # empty candidate list persists as '[]', not NULL
        db.set_signal_identification(row["id"], 1, [], "band")
        raw = db.query_one("SELECT id_candidates_json FROM signals WHERE id=?",
                           (row["id"],))
        self.assertEqual(raw["id_candidates_json"], "[]")

    def test_idempotent_reopen(self):
        Database(self.path)
        # Re-open twice more; must not raise (duplicate-column / table errors).
        Database(self.path)
        Database(self.path)
        cols = self._cols("signals")
        self.assertIn("triage_status", cols)

    def test_triage_status_constant(self):
        self.assertEqual(TRIAGE_STATUSES[0], "unreviewed")
        for s in ("cleared", "suspect", "flagged", "ask_client"):
            self.assertIn(s, TRIAGE_STATUSES)

    def _table_names(self):
        conn = sqlite3.connect(self.path)
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        return names


if __name__ == "__main__":
    unittest.main()
