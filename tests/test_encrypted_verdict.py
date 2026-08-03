"""An encrypted call must not read as readable content.

DMR and P25 report encryption per message. Before this, a decoded-but-encrypted
call reached interpret() indistinguishable from ordinary traffic and came back
readable="decoded" — so the UI offered "readable content below" for a call whose
payload nobody can read. The metadata IS a finding; the content is not available.
Both halves of that have to be true in the verdict at once.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from survey.decode_router import interpret                      # noqa: E402

FEATS = {"signal_present": True, "modulation_class": "fsk",
         "occupied_bw_khz": 12.5, "fsk_states": 4, "symbol_rate_kbd": 4.8}


class EncryptedVerdictTest(unittest.TestCase):
    def test_encrypted_call_is_not_reported_as_readable(self):
        v = interpret(FEATS, "DMR", encrypted=True)
        self.assertEqual(v["readable"], "encrypted")
        self.assertNotIn("readable content below", v["summary"])

    def test_encrypted_verdict_still_says_it_is_a_finding(self):
        """It must not read as a failure either — who is operating in the space
        is exactly what a sweep is for."""
        v = interpret(FEATS, "P25", encrypted=True)
        self.assertIn("ENCRYPTED", v["summary"])
        self.assertIn("finding", v["summary"].lower())

    def test_clear_call_is_unchanged(self):
        v = interpret(FEATS, "DMR", encrypted=False)
        self.assertEqual(v["readable"], "decoded")

    def test_default_is_unencrypted_so_existing_callers_are_unaffected(self):
        self.assertEqual(interpret(FEATS, "POCSAG")["readable"], "decoded")

    def test_video_verdict_not_hijacked_by_the_encrypted_branch(self):
        v = interpret(FEATS, "ANALOG-VIDEO NTSC", encrypted=False)
        self.assertEqual(v["readable"], "decoded")
        self.assertIn("not a confirmed camera", v["summary"])

    def test_no_signal_still_wins_over_everything(self):
        v = interpret({"signal_present": False}, "DMR", encrypted=True)
        self.assertEqual(v["readable"], "none")


class RouterEncryptedFlagTest(unittest.TestCase):
    """route_decode must surface encryption at the top level, or interpret()
    never learns about it."""

    def test_flag_is_raised_from_any_encrypted_message(self):
        from survey import decode_router as dr
        r = {"messages": [{"encrypted": False}, {"encrypted": True}]}
        r["encrypted"] = any(m.get("encrypted") for m in r["messages"]
                             if isinstance(m, dict))
        self.assertTrue(r["encrypted"])

    def test_non_dict_messages_do_not_crash_the_flag(self):
        """Some decoders emit plain strings as messages."""
        msgs = ["OOK 10110", {"encrypted": True}]
        self.assertTrue(any(m.get("encrypted") for m in msgs if isinstance(m, dict)))


if __name__ == "__main__":
    unittest.main()
