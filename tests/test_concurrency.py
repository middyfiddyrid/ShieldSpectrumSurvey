"""Phase-2 dual-device split (flag pluto.concurrent_monitor).

Flag OFF (default): connect swaps the single active device, busy() is global —
byte-identical to the legacy single-device path. Flag ON: the RFE (sweeper) and
the Pluto (monitor) are held at once with INDEPENDENT per-source locks, and
busy('pluto') is False while a sweep is bound to the RFE — so a monitor can run
while the sweep continues. open_device is stubbed; no hardware.
"""

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import survey.sweep.device as device_mod          # noqa: E402
from survey.db import Database                     # noqa: E402
from survey.config import Config                   # noqa: E402
from survey.sweep.runner import SweepController    # noqa: E402


class FakeDev:
    def __init__(self, source):
        self.source = source
        self.closed = False
        self.is_swept_sdr = (source == "pluto")
        self.info = types.SimpleNamespace(
            to_dict=lambda: {"source": source}, simulated=True,
            modules=[types.SimpleNamespace(min_mhz=70.0, max_mhz=6000.0, side="right")])

    def close(self):
        self.closed = True

    def is_alive(self):
        return not self.closed


class _AliveThread:
    def is_alive(self):
        return True


class TestConcurrency(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = Database(os.path.join(self.dir, "t.db"))
        self.cfg = Config(os.path.join(self.dir, "config.yaml"))
        self._orig = device_mod.open_device
        device_mod.open_device = lambda cfg, source=None: FakeDev(source or "rfexplorer")

    def tearDown(self):
        device_mod.open_device = self._orig

    # ---- flag OFF: legacy single-device behaviour ----

    def test_off_connect_swaps_single_device(self):
        c = SweepController(self.db, self.cfg)
        c.connect_device("rfexplorer")
        self.assertIn("rfexplorer", c.devices)
        c.connect_device("pluto")                      # legacy: closes the RFE
        self.assertNotIn("rfexplorer", c.devices)
        self.assertIn("pluto", c.devices)
        dev, lock, _stop, src = c.monitor_target()
        self.assertIs(dev, c.device)                   # active device
        self.assertIs(lock, c.device_lock)             # legacy lock
        self.assertEqual(src, "pluto")

    def test_off_busy_is_global(self):
        c = SweepController(self.db, self.cfg)
        c.connect_device("rfexplorer")
        c._thread = _AliveThread()
        c._busy_source = "rfexplorer"
        self.assertTrue(c.busy())
        self.assertTrue(c.busy("rfexplorer"))
        self.assertFalse(c.busy("pluto"))              # source-aware even when off
        self.assertFalse(c.concurrent_enabled())

    # ---- flag ON: dual-device concurrent ----

    def _dual(self):
        self.cfg.data["pluto"]["concurrent_monitor"] = True
        c = SweepController(self.db, self.cfg)
        c.connect_device("rfexplorer")                 # sweeper
        c.connect_device("pluto", keep_other=True)     # monitor, keep the RFE open
        return c

    def test_on_both_radios_coexist(self):
        c = self._dual()
        self.assertIn("rfexplorer", c.devices)
        self.assertIn("pluto", c.devices)
        self.assertIs(c.device, c.devices["rfexplorer"])   # sweeper stays the active device
        self.assertFalse(c.devices["rfexplorer"].closed)   # RFE NOT closed

    def test_on_monitor_target_is_pluto_with_own_lock(self):
        c = self._dual()
        dev, lock, _stop, src = c.monitor_target()
        self.assertEqual(src, "pluto")
        self.assertIs(dev, c.devices["pluto"])
        self.assertIs(lock, c.device_locks["pluto"])
        self.assertIsNot(lock, c.device_lock)              # independent of the sweeper lock

    def test_on_per_source_locks_independent(self):
        c = self._dual()
        rfe_lock = c.device_locks["rfexplorer"]
        pluto_lock = c.device_locks["pluto"]
        self.assertIsNot(rfe_lock, pluto_lock)
        self.assertTrue(rfe_lock.acquire(blocking=False))
        try:
            # holding the sweeper's lock must NOT block the monitor's lock
            self.assertTrue(pluto_lock.acquire(blocking=False))
            pluto_lock.release()
        finally:
            rfe_lock.release()

    def test_on_monitor_free_while_sweep_runs(self):
        c = self._dual()
        c._thread = _AliveThread()         # a sweep is running...
        c._busy_source = "rfexplorer"      # ...on the RFE
        self.assertTrue(c.busy())                  # something is running
        self.assertTrue(c.busy("rfexplorer"))      # the sweeper is busy
        self.assertFalse(c.busy("pluto"))          # the monitor radio is FREE
        roles = c.device_roles()
        self.assertTrue(roles["concurrent_enabled"])
        by_src = {r["source"]: r for r in roles["roles"]}
        self.assertEqual(by_src["rfexplorer"]["role"], "sweeper")
        self.assertEqual(by_src["pluto"]["role"], "monitor")

    # ---- ensure_monitor: mid-sweep Pluto auto-connect (B220 sweeper) ----

    def test_ensure_monitor_autoconnects_pluto_mid_sweep(self):
        self.cfg.data["pluto"]["concurrent_monitor"] = True
        c = SweepController(self.db, self.cfg)
        c.connect_device("b220")
        c._thread = _AliveThread()          # a sweep is running on the B220
        c._busy_source = "b220"
        dev, lock, _stop, src = c.ensure_monitor()
        self.assertEqual(src, "pluto")                      # handed off to the Pluto
        self.assertIn("pluto", c.devices)                   # ...which was auto-opened
        self.assertIn("b220", c.devices)                    # sweeper stays open
        self.assertIs(dev, c.devices["pluto"])
        self.assertIsNot(lock, c.device_lock)               # its own lock
        self.assertEqual(c.active_source, "b220")           # sweep untouched

    def test_ensure_monitor_no_autoconnect_when_off(self):
        # flag OFF -> plain monitor_target, never opens hardware
        c = SweepController(self.db, self.cfg)
        c.connect_device("b220")
        _dev, _l, _s, src = c.ensure_monitor()
        self.assertEqual(src, "b220")
        self.assertNotIn("pluto", c.devices)

    def test_ensure_monitor_seeks_pluto_proactively_when_idle(self):
        # operator ask 2026-07-11: the Pluto should auto-join as monitor WITHOUT
        # a sweep running — flag ON + sweeper connected + idle now seeks it.
        self.cfg.data["pluto"]["concurrent_monitor"] = True
        c = SweepController(self.db, self.cfg)
        c.connect_device("b220")
        self.assertNotIn("pluto", c.devices)          # not opened by the connect itself
        _dev, _l, _s, src = c.ensure_monitor()        # the seek happens here
        self.assertEqual(src, "pluto")
        self.assertIn("pluto", c.devices)
        self.assertEqual(c.active_source, "b220")     # sweeper unchanged

    def test_ensure_monitor_failure_is_nonfatal_with_cooldown(self):
        self.cfg.data["pluto"]["concurrent_monitor"] = True
        c = SweepController(self.db, self.cfg)
        c.connect_device("b220")
        c._thread = _AliveThread()
        c._busy_source = "b220"
        calls = {"n": 0}

        def boom(cfg, source=None):
            calls["n"] += 1
            raise RuntimeError("no Pluto plugged in")
        device_mod.open_device = boom
        _dev, _l, _s, src = c.ensure_monitor()      # falls back to the sweeper target
        self.assertEqual(src, "b220")
        c.ensure_monitor()                          # inside the 60 s cooldown
        self.assertEqual(calls["n"], 1)             # -> no second open attempt

    def test_autoseek_monitor_connects_when_present(self):
        self.cfg.data["pluto"]["concurrent_monitor"] = True
        c = SweepController(self.db, self.cfg)
        c.connect_device("b220")
        self.assertTrue(c.autoseek_monitor())         # Pluto present -> joins
        self.assertIn("pluto", c.devices)
        self.assertEqual(c.device_roles()["roles"][-1]["role"], "monitor")

    def test_autoseek_noop_without_sweeper_or_flag(self):
        # no sweeper connected yet -> nothing to seek onto
        self.cfg.data["pluto"]["concurrent_monitor"] = True
        c = SweepController(self.db, self.cfg)
        self.assertFalse(c.autoseek_monitor())
        self.assertNotIn("pluto", c.devices)
        # flag OFF -> never seeks even with a sweeper up
        c2 = SweepController(self.db, self.cfg)
        c2.cfg.data["pluto"]["concurrent_monitor"] = False
        c2.connect_device("b220")
        self.assertFalse(c2.autoseek_monitor())
        self.assertNotIn("pluto", c2.devices)

    def test_b220_sweeper_promotion_regardless_of_connect_order(self):
        # generalized role rule: with the Pluto + ANY other radio open, the other
        # radio is the sweeper — even when the Pluto was connected first.
        self.cfg.data["pluto"]["concurrent_monitor"] = True
        c = SweepController(self.db, self.cfg)
        c.connect_device("pluto")
        c.connect_device("b220", keep_other=True)
        self.assertEqual(c.active_source, "b220")
        _dev, _l, _s, src = c.monitor_target()
        self.assertEqual(src, "pluto")


if __name__ == "__main__":
    unittest.main()
