"""The sign gate's storage layer: engagement-scoped signed authorization.

Scope is the SESSION, not the signal — it is signed once at the start of an
engagement so a sweep on a client site is never interrupted mid-run to
authorize an individual hit. `active_authorization()` is the only predicate the
server gate may read; these tests pin its semantics.

NOTE: these cover storage only. Enforcement at the receive/decode seams is a
separate change — until that lands, nothing is actually gated.
"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from survey.db import Database  # noqa: E402

# A database from before the sign gate existed — one legacy table, no
# session_authorizations. Opening it must add the table without touching data.
OLD_SCHEMA = """
CREATE TABLE decode_overrides (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL,
    signal_id INTEGER,
    center_mhz REAL,
    mode TEXT DEFAULT '',
    operator TEXT DEFAULT '',
    ecpa_ack INTEGER DEFAULT 0,
    reason TEXT DEFAULT '',
    created_utc REAL
);
"""

ATTEST = {"receive_only": True, "authorized_area": True,
          "client_consent": True, "no_third_party_content": True}


class TestAuthorizationMigration(unittest.TestCase):
    """The live field database is upgraded in place, never rebuilt."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "old.db")
        conn = sqlite3.connect(self.path)
        conn.executescript(OLD_SCHEMA)
        conn.execute("INSERT INTO decode_overrides (session_id, reason, created_utc) "
                     "VALUES (7, 'legacy row', 1.0)")
        conn.commit()
        conn.close()

    def _tables(self):
        conn = sqlite3.connect(self.path)
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        return names

    def test_table_added_to_existing_database(self):
        self.assertNotIn("session_authorizations", self._tables())
        Database(self.path)
        self.assertIn("session_authorizations", self._tables())

    def test_legacy_rows_survive(self):
        Database(self.path)
        conn = sqlite3.connect(self.path)
        rows = conn.execute("SELECT reason FROM decode_overrides").fetchall()
        conn.close()
        self.assertEqual([r[0] for r in rows], ["legacy row"])

    def test_reopening_is_idempotent(self):
        db = Database(self.path)
        db.add_session_authorization(1, "M. Ford", attestations=ATTEST)
        Database(self.path)                       # reopen, as a restart would
        self.assertIsNotNone(Database(self.path).active_authorization(1))


class TestAuthorizationSemantics(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = Database(os.path.join(self.dir, "s.db"))

    def test_unsigned_session_has_no_authorization(self):
        self.assertIsNone(self.db.active_authorization(1))

    def test_signing_authorizes_the_whole_session(self):
        self.db.add_session_authorization(
            1, "M. Ford", operator="matt", client_name="Acme",
            authorized_area="3rd floor suite", attestations=ATTEST)
        got = self.db.active_authorization(1)
        self.assertIsNotNone(got)
        self.assertEqual(got["signature_name"], "M. Ford")
        self.assertEqual(json.loads(got["attestations_json"]), ATTEST)
        self.assertIsNone(got["revoked_utc"])

    def test_authorization_does_not_leak_between_sessions(self):
        """A signed engagement must never authorize the next client's sweep."""
        self.db.add_session_authorization(1, "M. Ford", attestations=ATTEST)
        self.assertIsNone(self.db.active_authorization(2))

    def test_revoke_makes_it_inactive_but_keeps_the_record(self):
        row = self.db.add_session_authorization(1, "M. Ford", attestations=ATTEST)
        self.db.revoke_authorization(row["id"], 1)
        self.assertIsNone(self.db.active_authorization(1))
        self.assertEqual(len(self.db.list_session_authorizations(1)), 1,
                         "the signed artifact must survive revocation")

    def test_revoke_is_session_scoped(self):
        row = self.db.add_session_authorization(1, "M. Ford", attestations=ATTEST)
        self.db.revoke_authorization(row["id"], 999)      # wrong session
        self.assertIsNotNone(self.db.active_authorization(1))

    def test_newest_unrevoked_wins(self):
        first = self.db.add_session_authorization(1, "First", attestations=ATTEST)
        self.db.add_session_authorization(1, "Second", attestations=ATTEST)
        self.db.revoke_authorization(first["id"], 1)
        self.assertEqual(self.db.active_authorization(1)["signature_name"], "Second")

    def test_resigning_after_revocation_restores_authorization(self):
        row = self.db.add_session_authorization(1, "M. Ford", attestations=ATTEST)
        self.db.revoke_authorization(row["id"], 1)
        self.db.add_session_authorization(1, "M. Ford", attestations=ATTEST)
        self.assertIsNotNone(self.db.active_authorization(1))
        self.assertEqual(len(self.db.list_session_authorizations(1)), 2)


if __name__ == "__main__":
    unittest.main()
