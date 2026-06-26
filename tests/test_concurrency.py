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


if __name__ == "__main__":
    unittest.main()
