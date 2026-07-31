import os
import tempfile
import unittest
from pathlib import Path

import yaml

from survey.config import Config
from survey.db import Database
from survey.server import create_app


ROOT = Path(__file__).resolve().parents[1]


class TestSentryBuild(unittest.TestCase):
    def test_field_configuration_is_live_b220_and_isolated(self):
        config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
        self.assertEqual(config["app"]["host"], "127.0.0.1")
        self.assertEqual(config["app"]["port"], 8876)
        self.assertFalse(config["device"]["simulate"])
        self.assertEqual(config["device"]["source"], "b220")
        self.assertFalse(config["pluto"]["concurrent_monitor"])
        self.assertFalse(config["stimulate"]["enabled"])
        self.assertTrue(config["catalog_enrichment"]["enabled"])
        self.assertFalse(any(config["api_keys"].values()))

    def test_launcher_never_selects_simulation_or_demo(self):
        launcher = (ROOT / "START Sentry RF Intelligence.bat").read_text(encoding="utf-8")
        self.assertIn("app.py --port 8876", launcher)
        self.assertNotIn("--simulate", launcher)
        self.assertNotIn("--demo", launcher)

    def test_transmit_endpoints_are_disabled_by_server(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = Config(os.path.join(root, "config.yaml"))
            cfg.data.setdefault("stimulate", {})["enabled"] = False
            db = Database(os.path.join(root, "sentry.db"))
            app = create_app(db, cfg)
            client = app.test_client()
            response = client.post("/api/stimulate/probe", json={
                "center_mhz": 915.0, "band_lo_mhz": 902.0, "band_hi_mhz": 928.0,
            })
        self.assertEqual(response.status_code, 403)
        self.assertIn("receive-only", response.get_json()["error"])


if __name__ == "__main__":
    unittest.main()
