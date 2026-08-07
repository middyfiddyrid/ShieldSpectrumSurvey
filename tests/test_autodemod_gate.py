"""AUTO-DEMOD's content gate — the part where a bug has legal consequences.

Characterizing a signal (modulation, bandwidth, SNR, signature match) is
content-free and runs for everything. Demodulating audio or decoding a payload is
interception of communications CONTENT, and ECPA (18 U.S.C. 2511) rests on the
premises owner's consent — in this app, attribution_status == 'client_space'.

A worker that walks every signal unattended is exactly where "it was easier to
just decode everything" would creep in, so these tests pin the gate shut: it must
default closed, open only for the client's own space, and never open on an
acknowledgment that was not written down.
"""

import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from survey.autodemod import AutoDemod, can_capture_iq   # noqa: E402
from survey.config import Config                          # noqa: E402
from survey.db import Database                            # noqa: E402
from survey.sweep.device import BaseDevice                # noqa: E402


def _worker(demo=False):
    tmp = tempfile.mkdtemp()
    db = Database(str(Path(tmp) / "t.db"))
    cfg = Config(str(Path(tmp) / "config.yaml"))
    controller = types.SimpleNamespace(state={}, listening=False,
                                       active_source="b220")
    w = AutoDemod(controller, db, cfg, demo=demo)
    sid = db.execute(
        "INSERT INTO sessions (address, created_utc, status) VALUES (?,?,?)",
        ("test address", 1.0, "open"))
    w.session_id = sid
    return w, db, sid


class TestContentGate(unittest.TestCase):
    def test_unassessed_signal_is_not_decoded(self):
        w, _, _ = _worker()
        ok, why = w._permitted({"attribution_status": "unassessed"})
        self.assertFalse(ok)
        self.assertIn("not attributed", why)

    def test_third_party_signal_is_not_decoded(self):
        w, _, _ = _worker()
        for status in ("third_party", "infrastructure", "inconclusive", "", None):
            ok, _ = w._permitted({"attribution_status": status})
            self.assertFalse(ok, f"{status!r} must not permit content decode")

    def test_client_space_is_decoded_without_any_override(self):
        """The lawful default path: the client owns the space, so no ECPA
        acknowledgment is needed."""
        w, _, _ = _worker()
        ok, _ = w._permitted({"attribution_status": "client_space"})
        self.assertTrue(ok)
        self.assertFalse(w.status["consent"], "no override should be recorded")

    def test_demo_mode_never_decodes_even_with_consent(self):
        w, _, _ = _worker(demo=True)
        w.status["consent"] = True
        for status in ("client_space", "unassessed"):
            ok, why = w._permitted({"attribution_status": status})
            self.assertFalse(ok, "demo must never decode content")
            self.assertIn("demo", why)


class TestConsentRequiresAnAuditRow(unittest.TestCase):
    def test_consent_without_a_reason_is_refused(self):
        w, db, sid = _worker()
        with self.assertRaises(ValueError):
            w.set_consent(True, reason="")
        with self.assertRaises(ValueError):
            w.set_consent(True, reason="   ")
        self.assertFalse(w.status["consent"])
        self.assertEqual(db.list_decode_overrides(sid), [],
                         "a refused acknowledgment must not write an audit row")

    def test_consent_with_a_reason_writes_the_immutable_audit_row(self):
        w, db, sid = _worker()
        w.set_consent(True, reason="signed engagement, premises owner authorized",
                      operator="matt")
        self.assertTrue(w.status["consent"])
        rows = db.list_decode_overrides(sid)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ecpa_ack"], 1)
        self.assertEqual(rows[0]["operator"], "matt")
        self.assertIn("premises owner", rows[0]["reason"])
        self.assertEqual(rows[0]["mode"], "autodemod")

    def test_consent_opens_the_gate_for_unattributed_signals(self):
        w, _, _ = _worker()
        before, _ = w._permitted({"attribution_status": "unassessed"})
        self.assertFalse(before)
        w.set_consent(True, reason="authorized sweep")
        after, why = w._permitted({"attribution_status": "unassessed"})
        self.assertTrue(after)
        self.assertIn("logged", why)

    def test_consent_can_be_withdrawn(self):
        w, _, _ = _worker()
        w.set_consent(True, reason="authorized sweep")
        w.set_consent(False)
        self.assertFalse(w.status["consent"])
        ok, _ = w._permitted({"attribution_status": "unassessed"})
        self.assertFalse(ok)

    def test_gate_is_closed_on_a_fresh_worker(self):
        w, _, _ = _worker()
        self.assertFalse(w.status["consent"])
        self.assertFalse(w.status["running"])


class TestCapabilityProbe(unittest.TestCase):
    """can_capture_iq must test the real capability, not a label — the simulator
    implements capture_iq without claiming to be a swept SDR, and gating on the
    label alone made the whole feature undemoable."""

    def test_amplitude_only_device_is_rejected(self):
        class RFE(BaseDevice):
            pass
        self.assertFalse(can_capture_iq(RFE()))

    def test_device_that_overrides_capture_iq_is_accepted(self):
        class Sim(BaseDevice):
            def capture_iq(self, center_hz, sample_rate, n_samples):
                return []
        self.assertTrue(can_capture_iq(Sim()))

    def test_swept_sdr_flag_is_still_honoured(self):
        dev = types.SimpleNamespace(is_swept_sdr=True)
        self.assertTrue(can_capture_iq(dev))

    def test_none_is_rejected(self):
        self.assertFalse(can_capture_iq(None))


class TestPauseEtiquette(unittest.TestCase):
    def test_stands_down_while_the_operator_is_listening(self):
        w, _, _ = _worker()
        w.controller.listening = True
        self.assertIn("listening", w._pause_reason())

    def test_stands_down_when_a_sweep_owns_the_only_radio(self):
        w, _, _ = _worker()
        w.controller.state = {"running": True}
        w.controller._current_monitor = lambda: "b220"   # same as the sweeper
        self.assertIn("only radio", w._pause_reason())

    def test_runs_when_a_distinct_monitor_is_free(self):
        w, _, _ = _worker()
        w.controller.state = {"running": True}
        w.controller._current_monitor = lambda: "pluto"  # a second radio
        self.assertEqual(w._pause_reason(), "")


if __name__ == "__main__":
    unittest.main()
