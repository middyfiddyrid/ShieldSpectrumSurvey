"""The Pluto seek runs on operator ACTIONS only — never on a background timer.

Field report: listening was choppy with no Pluto attached. The seek spawns a
worker PROCESS and enumerates USB on every probe — seconds of work. A background
poller used to do that every 20 s for the life of the process, so with no Pluto
present it retried forever: audio chopped about once a minute, a worker respawned
every ~63 s, and the log grew ~5 MB an hour. The poller was removed 2026-08-03.

These tests lock in BOTH halves of the replacement contract:
  * nothing probes the USB bus unless the operator did something, and
  * the operator-action path still brings the Pluto up hands-free.
"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from survey.db import Database                           # noqa: E402
from survey.config import Config                         # noqa: E402
from survey.sweep.runner import SweepController          # noqa: E402


class AutoseekQuietTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = Database(os.path.join(self.dir, "t.db"))
        self.cfg = Config(os.path.join(self.dir, "config.yaml"))
        self.c = SweepController(self.db, self.cfg)
        self.probes = []
        self.c._try_open_pluto_monitor = lambda: self.probes.append(time.time())
        self.c.concurrent_enabled = lambda: True
        self.c.active_source = "rfexplorer"

    def test_no_background_seek_thread_exists(self):
        """The load-bearing assertion: there is no timer that can probe on its
        own. If a background seek is ever reintroduced, it must not be a bare
        forever-loop — this test is the tripwire."""
        self.assertFalse(hasattr(self.c, "start_monitor_autoseek"),
                         "a background Pluto autoseek was reintroduced")

    def test_nothing_probes_without_an_operator_action(self):
        """Construct a controller, wait, and confirm the USB bus stayed quiet."""
        time.sleep(0.35)
        self.assertEqual(self.probes, [],
                         "something probed the USB bus with no operator action")

    def test_monitor_action_still_seeks_the_pluto(self):
        """The hands-free seek must survive on the action path — pressing
        LISTEN / ANALYZE / INSPECT / RECORD still brings the Pluto up with no
        manual connect."""
        self.c.ensure_monitor()
        self.assertEqual(len(self.probes), 1,
                         "a monitor action no longer seeks the Pluto")

    def test_explicit_autoseek_call_still_works(self):
        """The DEVICE-card connect path calls autoseek_monitor() directly."""
        self.c.autoseek_monitor()
        self.assertEqual(len(self.probes), 1,
                         "the operator connect path no longer seeks the Pluto")


if __name__ == "__main__":
    unittest.main()
