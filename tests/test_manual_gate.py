"""F5 manual-advance gate keeps SWEEPING (2026-07-11 ask).

Before: the between-segment manual gate idled the radio until NEXT SEGMENT.
Now it reuses the F6 sustain live-watch loop, so the current segment keeps
re-sweeping (live_trace keeps moving) until the operator advances or aborts.
"""

import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from survey.db import Database                     # noqa: E402
from survey.config import Config                   # noqa: E402
from survey.sweep.device import BaseDevice, SweepFrame   # noqa: E402
from survey.sweep.planner import Segment, SweepPlan      # noqa: E402
from survey.sweep.runner import SweepController          # noqa: E402


class GateDev(BaseDevice):
    """Minimal sweepable fake: flat -80 dBm frames, counts read_sweep calls."""

    def __init__(self):
        self.reads = 0
        self.rbw_calls = []
        self.info = types.SimpleNamespace(
            simulated=True, to_dict=lambda: {"source": "fake"},
            modules=[types.SimpleNamespace(min_mhz=70.0, max_mhz=6000.0,
                                           side="right")])
        self._range = (100.0, 101.0)

    def set_rbw_khz(self, rbw_khz):
        self.rbw_calls.append(rbw_khz)
        return float(rbw_khz) if rbw_khz else None

    def is_alive(self):
        return True

    def connect(self):
        return self.info

    def select_module(self, side):
        pass

    def set_range(self, lo, hi):
        self._range = (lo, hi)

    def set_calculator(self, mode):
        pass

    def read_sweep(self, timeout_s: float = 5.0):
        self.reads += 1
        time.sleep(0.01)                    # don't busy-spin the test machine
        lo, hi = self._range
        freqs = [lo + i * (hi - lo) / 9 for i in range(10)]
        return SweepFrame(freqs, [-80.0] * 10, time.time())

    def close(self):
        pass


def _wait(pred, timeout=8.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.05)
    return False


def _plan():
    return SweepPlan(segments=[
        Segment("right", 100.0, 101.0, "other", 1.0, avg_passes=1,
                maxhold_dwell_s=0.05),
        Segment("right", 101.0, 102.0, "other", 1.0, avg_passes=1,
                maxhold_dwell_s=0.05)],
        profile="custom", sweep_points=112)


class TestManualGateKeepsSweeping(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = Database(os.path.join(self.dir, "t.db"))
        self.cfg = Config(os.path.join(self.dir, "config.yaml"))

    def _controller(self):
        c = SweepController(self.db, self.cfg)
        c.device = GateDev()
        c.active_source = "fake"
        return c

    def _session_id(self):
        return self.db.create_session("1 A St", 0, 0, "test", 25, 5, "op")

    def test_gate_keeps_resweeping_until_advance(self):
        c = self._controller()
        sid = self._session_id()
        c.start_sweep(sid, "interior", "right", "room", _plan(),
                      manual_advance=True)
        # gate opens after segment 1
        self.assertTrue(_wait(lambda: c.state["awaiting_advance"]),
                        "gate never opened")
        # the radio must KEEP sweeping while gated: read count grows and
        # live_trace timestamps keep moving
        r0 = c.device.reads
        ts0 = (c.state.get("live_trace") or {}).get("ts")
        self.assertTrue(_wait(lambda: c.device.reads > r0 + 3),
                        "radio idle during the manual gate")
        self.assertTrue(_wait(lambda: (c.state.get("live_trace") or {}).get("ts")
                              not in (None, ts0)),
                        "live_trace frozen during the manual gate")
        # the gate is NOT the F6 sustain UI
        self.assertFalse(c.state["sustaining"])
        # NEXT releases it and the sweep finishes
        c.advance()
        self.assertTrue(_wait(lambda: not c.busy(), timeout=15.0),
                        "sweep never finished after advance()")
        self.assertFalse(c.state["awaiting_advance"])

    def test_rbw_override_cleared_when_sweep_ends(self):
        # adversarial-review MAJOR: the per-sweep RBW override leaked into
        # captures/directional/calibration after the sweep — the worker's exit
        # path must clear it (set_rbw_khz(None))
        c = self._controller()
        sid = self._session_id()
        c.start_sweep(sid, "interior", "right", "room", _plan(),
                      manual_advance=False, rbw_khz=25.0)
        self.assertTrue(_wait(lambda: not c.busy(), timeout=20.0))
        calls = c.device.rbw_calls
        self.assertIn(25.0, calls)                 # override applied for the sweep
        self.assertIsNone(calls[-1])               # ...and CLEARED on exit

    def test_live_rbw_change_applies_on_next_segment(self):
        # operator ask 2026-07-11: turn the RBW knob DURING the sweep; it must
        # re-tune on the next segment (set_live_rbw -> _run_plan re-applies)
        c = self._controller()
        sid = self._session_id()
        c.start_sweep(sid, "interior", "right", "room", _plan(),
                      manual_advance=True, rbw_khz=None)
        self.assertTrue(_wait(lambda: c.state["awaiting_advance"]))  # seg 1 done
        c.set_live_rbw(50.0)                        # turn the knob mid-sweep
        self.assertEqual(c.rbw_khz, 50.0)
        c.advance()                                # -> segment 2 applies it
        self.assertTrue(_wait(lambda: not c.busy(), timeout=15.0))
        self.assertIn(50.0, c.device.rbw_calls)    # the device got the new RBW
        self.assertIsNone(c.device.rbw_calls[-1])  # ...and it's cleared on exit

    def test_abort_during_gate_stops_promptly(self):
        c = self._controller()
        sid = self._session_id()
        c.start_sweep(sid, "interior", "right", "room", _plan(),
                      manual_advance=True)
        self.assertTrue(_wait(lambda: c.state["awaiting_advance"]))
        c.abort()
        self.assertTrue(_wait(lambda: not c.busy(), timeout=15.0),
                        "abort during the live gate did not stop the sweep")


if __name__ == "__main__":
    unittest.main()
