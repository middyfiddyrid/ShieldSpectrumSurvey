"""Regression: a --simulate/--demo session must never persist simulate:true.

The original bug: app.py wrote the CLI override into cfg.data, and any
set_and_save() during that session dumped the whole dict — so config.yaml
ended up with simulate:true and every later NORMAL launch silently ran real
sweeps against the simulator.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml

from survey.config import Config


class TestRuntimeOverride(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "config.yaml")

    def tearDown(self):
        self.tmp.cleanup()

    def test_override_visible_in_session_but_never_persisted(self):
        # session 1: launched with --simulate, then settings get saved
        cfg = Config(self.path)
        cfg.set_runtime_override(("device", "simulate"), True)
        self.assertTrue(cfg.get("device", "simulate", default=False))
        self.assertFalse(cfg.data["device"]["simulate"])

        cfg.set_and_save(("device", "source"), "pluto")
        cfg.set_and_save(("sweep", "seconds_per_sweep"), 0.42)
        self.assertTrue(cfg.get("device", "simulate", default=False))

        # on disk: simulate stays false, the legit saves stick
        with open(self.path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        self.assertFalse(raw["device"]["simulate"])

        # session 2: normal launch reloads a clean config
        cfg2 = Config(self.path)
        self.assertFalse(cfg2.get("device", "simulate", default=False))
        self.assertEqual(cfg2.get("device", "source"), "pluto")
        self.assertEqual(cfg2.get("sweep", "seconds_per_sweep"), 0.42)

    def test_deliberate_config_file_simulate_still_works(self):
        cfg = Config(self.path)
        cfg.set_and_save(("device", "simulate"), True)
        self.assertTrue(Config(self.path).get("device", "simulate", default=False))


if __name__ == "__main__":
    unittest.main()
