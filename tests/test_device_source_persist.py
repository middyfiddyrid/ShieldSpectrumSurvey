"""device.source persistence rule (POST /api/device/connect).

The endpoint remembers the operator's pick so the next launch defaults to it —
but ONLY when that radio actually became the SWEEPER. Adding the Pluto as the
concurrent monitor (keep_other=true) must NOT rewrite the boot default: that
regression persisted device.source: pluto, so the next launch made the Pluto
the sweeper — no monitor, no auto-connected B220, dual-radio split dead on
arrival. open_device is stubbed; no hardware.
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
from survey.server import create_app               # noqa: E402


class FakeDev:
    def __init__(self, source):
        self.source = source
        self.closed = False
        self.is_swept_sdr = source in ("pluto", "b220")
        self.info = types.SimpleNamespace(
            to_dict=lambda: {"source": source}, simulated=True,
            modules=[types.SimpleNamespace(min_mhz=70.0, max_mhz=6000.0, side="right")])

    def close(self):
        self.closed = True

    def is_alive(self):
        return not self.closed


class TestDeviceSourcePersist(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = Database(os.path.join(self.dir, "t.db"))
        self.cfg = Config(os.path.join(self.dir, "config.yaml"))
        self.cfg.data["pluto"]["concurrent_monitor"] = True
        self._orig = device_mod.open_device
        device_mod.open_device = lambda cfg, source=None: FakeDev(source or "rfexplorer")
        app = create_app(self.db, self.cfg, demo=False)
        app.testing = True
        self.client = app.test_client()

    def tearDown(self):
        device_mod.open_device = self._orig

    def test_sweeper_connect_persists_source(self):
        r = self.client.post("/api/device/connect", json={"source": "b220"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.cfg.get("device", "source"), "b220")

    def test_monitor_connect_does_not_persist_source(self):
        self.client.post("/api/device/connect", json={"source": "b220"})
        r = self.client.post("/api/device/connect",
                             json={"source": "pluto", "keep_other": True})
        self.assertEqual(r.status_code, 200)
        # the Pluto joined as MONITOR (B220 stays sweeper) -> boot default unchanged
        self.assertEqual(self.cfg.get("device", "source"), "b220")

    def test_legacy_swap_still_persists(self):
        # concurrent OFF: connecting the Pluto IS a sweeper swap -> persist it
        self.cfg.data["pluto"]["concurrent_monitor"] = False
        self.client.post("/api/device/connect", json={"source": "rfexplorer"})
        self.client.post("/api/device/connect", json={"source": "pluto"})
        self.assertEqual(self.cfg.get("device", "source"), "pluto")


if __name__ == "__main__":
    unittest.main()
