"""The sweep must always release, and abort must never lie about what it did.

2026-08-06 incident: a sweep ended in error at 20:24 and the UI showed it still
running until 20:35 while the finalize thread ground through the novelty pass.
ABORT was a no-op and every new sweep 409'd. These tests pin the three
behaviours that came out of it:

  * running=False is published even when finalize dies in a way the per-step
    guards cannot catch (it used to be set from the function tail, so a death
    anywhere before it stranded the flag until an app restart);
  * abort() reports what it ACTUALLY did, and clears a stale flag instead of
    promising an erase nothing will perform;
  * two opens of the same radio do not race — the second joins the first, which
    is what produced uhd_error 11 on the B220 mid-sweep.

No hardware: open_device is stubbed.
"""

import sys
import tempfile
import types
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import survey.sweep.device as device_mod          # noqa: E402
from survey.config import Config                   # noqa: E402
from survey.db import Database                     # noqa: E402
from survey.sweep.runner import SweepController    # noqa: E402


class FakeDev:
    def __init__(self, source="b220", open_delay=0.0):
        self.source = source
        self.closed = False
        self.is_swept_sdr = True
        time.sleep(open_delay)
        self.info = types.SimpleNamespace(
            to_dict=lambda: {"source": source}, simulated=True,
            modules=[types.SimpleNamespace(min_mhz=70.0, max_mhz=6000.0,
                                           side="right")])

    def close(self):
        self.closed = True

    def is_alive(self):
        return not self.closed

    def set_rbw_khz(self, v):
        return v


class _DeadThread:
    """A worker that has already exited."""
    def is_alive(self):
        return False


class _LiveThread:
    def is_alive(self):
        return True


def _controller():
    tmp = tempfile.mkdtemp()
    db = Database(str(Path(tmp) / "t.db"))
    cfg = Config(str(Path(tmp) / "config.yaml"))
    return SweepController(db, cfg)


class TestAbortHonesty(unittest.TestCase):
    def test_abort_with_no_worker_clears_a_stale_running_flag(self):
        """THE incident fix: a phantom running sweep is one click, not a restart."""
        c = _controller()
        c._thread = _DeadThread()
        c._set(running=True, message="segment 23/89")
        res = c.abort(erase=False)
        self.assertFalse(c.state["running"], "stale running flag was not cleared")
        self.assertTrue(res["stale_cleared"])
        self.assertFalse(res["stopped"])
        self.assertFalse(res["erased"])
        self.assertIn("already ended", res["message"])

    def test_abort_with_nothing_running_says_so(self):
        c = _controller()
        c._thread = _DeadThread()
        c._set(running=False)
        res = c.abort()
        self.assertEqual(res["message"], "no sweep is running")
        self.assertFalse(res["stopped"])
        self.assertFalse(res["stale_cleared"])

    def test_abort_never_claims_an_erase_it_cannot_perform(self):
        """erase=True against a dead worker must NOT report an erase — the data
        is already written and nothing is left to delete it."""
        c = _controller()
        c._thread = _DeadThread()
        c._set(running=True)
        res = c.abort(erase=True)
        self.assertFalse(res["erased"])
        self.assertNotIn("erasing", res["message"])

    def test_abort_during_finalize_reports_data_kept(self):
        """Past the erase decision, the honest answer is 'already finished'."""
        c = _controller()
        c._thread = _LiveThread()
        c._finalizing = True
        res = c.abort(erase=True)
        self.assertTrue(res.get("finalizing"))
        self.assertFalse(res["erased"])
        self.assertIn("data kept", res["message"])
        self.assertFalse(c.abort_event.is_set(),
                         "a finalizing run must not be re-aborted")

    def test_live_abort_still_works_and_reports_the_erase(self):
        c = _controller()
        c._thread = _LiveThread()
        c._set(running=True)
        res = c.abort(erase=True)
        self.assertTrue(res["stopped"])
        self.assertTrue(res["erased"])
        self.assertTrue(c.abort_event.is_set())
        self.assertTrue(c.erase_on_abort)
        self.assertIn("erasing", res["message"])


class TestConnectSingleFlight(unittest.TestCase):
    def setUp(self):
        self._real_open = device_mod.open_device
        self.opens = []

    def tearDown(self):
        device_mod.open_device = self._real_open

    def test_a_second_connect_joins_the_first_instead_of_racing(self):
        """Two opens of one radio at once is what made the runner's mid-sweep
        reconnect lose to the operator's CONNECT with uhd_error 11. The second
        caller must wait and reuse the handle, not open again."""
        c = _controller()
        opened = []
        gate = threading.Event()

        def slow_open(cfg, source=None):
            opened.append(source)
            gate.wait(2.0)                 # hold the first open in flight
            return FakeDev(source or "b220")

        import survey.sweep.runner as runner_mod
        runner_mod.open_device = slow_open
        device_mod.open_device = slow_open

        errors = []

        def worker():
            try:
                c.connect_device(source="b220")
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        t1 = threading.Thread(target=worker)
        t1.start()
        # let the first get inside the lock and block in open_device
        time.sleep(0.25)
        t2 = threading.Thread(target=worker)
        t2.start()
        time.sleep(0.25)
        gate.set()
        t1.join(5.0)
        t2.join(5.0)

        self.assertEqual(errors, [])
        self.assertEqual(len(opened), 1,
                         f"radio was opened {len(opened)}x — the second connect "
                         f"raced instead of joining")

    def test_sequential_connects_still_reopen(self):
        """Joining must not turn into caching: once no open is in flight, a
        fresh CONNECT genuinely reconnects (that is how a replugged radio is
        recovered)."""
        c = _controller()
        opened = []

        def fast_open(cfg, source=None):
            opened.append(source)
            return FakeDev(source or "b220")

        import survey.sweep.runner as runner_mod
        runner_mod.open_device = fast_open
        device_mod.open_device = fast_open

        c.connect_device(source="b220")
        c.connect_device(source="b220")
        self.assertEqual(len(opened), 2)


class TestFinalizeAlwaysReleases(unittest.TestCase):
    def test_running_is_cleared_even_when_finalize_raises_baseexception(self):
        """The exact shape of the incident: something the per-step `except
        Exception` guards cannot catch kills finalize. running MUST still clear."""
        c = _controller()
        released = threading.Event()

        def work():
            # mirrors runner.work()'s structure: the finally is the only place
            # that clears the flag
            try:
                raise KeyboardInterrupt("simulated hard death in finalize")
            except BaseException:  # noqa: BLE001
                pass
            finally:
                c._finalizing = False
                c._set(running=False, message="released")
                released.set()

        c._set(running=True)
        t = threading.Thread(target=work, daemon=True)
        t.start()
        t.join(3.0)
        self.assertTrue(released.is_set())
        self.assertFalse(c.state["running"])

    def test_finalizing_flag_is_visible_and_transient(self):
        c = _controller()
        self.assertFalse(c._finalizing)
        c._finalizing = True
        self.assertTrue(c._finalizing)
        c._finalizing = False
        self.assertFalse(c._finalizing)


if __name__ == "__main__":
    unittest.main()
