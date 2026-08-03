"""The Pluto autoseek must go quiet while live audio is running.

Field report: listening was choppy with no Pluto attached. The background
autoseek spawns a worker PROCESS and enumerates USB on every retry — seconds of
work — and with no Pluto present it retried forever. Doing that underneath a
real-time demod stream starves the audio thread, and the operator hears it.
"""

import os
import sys
import tempfile
import threading
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
        self.seeks = []
        self.c.autoseek_monitor = lambda: self.seeks.append(time.time())
        self.c.concurrent_enabled = lambda: True
        self.c.active_source = "rfexplorer"

    def tearDown(self):
        stop = getattr(self.c, "_autoseek_stop", None)
        if stop:
            stop.set()

    def _run_loop_briefly(self, listening, seconds=0.35):
        self.c.listening = listening
        self.c.start_monitor_autoseek(interval_s=0.05)
        time.sleep(seconds)
        self.c._autoseek_stop.set()

    def test_does_not_probe_while_listening(self):
        """The load-bearing assertion: zero USB probes during live audio."""
        self._run_loop_briefly(listening=True)
        self.assertEqual(self.seeks, [],
                         "autoseek probed the USB bus while audio was streaming")

    def test_still_probes_when_not_listening(self):
        """The hands-free seek must survive — this is a pause, not a removal."""
        self._run_loop_briefly(listening=False)
        self.assertGreater(len(self.seeks), 0,
                           "autoseek stopped working when it should still run")

    def test_resumes_after_listening_stops(self):
        self.c.listening = True
        self.c.start_monitor_autoseek(interval_s=0.05)
        time.sleep(0.2)
        self.assertEqual(self.seeks, [])
        self.c.listening = False          # stream ended
        time.sleep(0.25)
        self.c._autoseek_stop.set()
        self.assertGreater(len(self.seeks), 0,
                           "autoseek never resumed after the stream ended")


if __name__ == "__main__":
    unittest.main()
