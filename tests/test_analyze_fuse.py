"""Fused per-signal ANALYZE (/api/analyze/signal).

The win is ONE radio capture that drives features + candidates + analyzer +
waterfall + (decode | audio) + novelty. A stub SDR with a capture counter proves
the single-capture fusion; the route degrades cleanly with no radio.
"""

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from survey.db import Database          # noqa: E402
from survey.config import Config        # noqa: E402
from survey.server import create_app    # noqa: E402


def _synth_fsk(n, fs):
    t = np.arange(n) / fs
    keying = (np.sin(2 * np.pi * 80.0 * t) > 0).astype(float)
    inst = np.where(keying > 0, 3000.0, -3000.0)
    phase = 2 * np.pi * np.cumsum(inst) / fs
    iq = np.exp(1j * phase) + 0.01 * (np.random.randn(n) + 1j * np.random.randn(n))
    return iq.astype(np.complex64)


class StubSDR:
    """Minimal swept-SDR stand-in: enough surface for /api/analyze/signal."""
    is_swept_sdr = True

    def __init__(self):
        self.capture_calls = 0
        self.info = types.SimpleNamespace(
            modules=[types.SimpleNamespace(min_mhz=70.0, max_mhz=6000.0, side="right")],
            simulated=True)

    def capture_iq(self, center_hz, fs, n):
        self.capture_calls += 1
        return _synth_fsk(int(n), float(fs))


class TestAnalyzeFuse(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = Database(os.path.join(self.dir, "t.db"))
        self.cfg = Config(os.path.join(self.dir, "config.yaml"))
        # tiny capture so the test is fast
        self.cfg.data["pluto"]["listen_sample_rate"] = 48000
        self.cfg.data["pluto"]["listen_seconds"] = 0.2
        self.cfg.data["pluto"]["pocsag_seconds"] = 0.2
        self.db.create_session("1 A St", 0, 0, "test", 25, 5, "op")
        self.app = create_app(self.db, self.cfg)
        self.st = self.app.config["APP_STATE"]
        self.stub = StubSDR()
        self.st.controller.device = self.stub
        self.st.controller.active_source = "pluto"
        self.c = self.app.test_client()

    def test_nfm_fuses_everything_in_one_capture(self):
        r = self.c.post("/api/analyze/signal",
                        json={"center_mhz": 433.92, "mode": "nfm"})
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertTrue(d["in_range"])
        self.assertEqual(self.stub.capture_calls, 1)            # ONE capture
        self.assertIn("modulation_class", d["features"])
        self.assertIsInstance(d["candidates"], list)
        self.assertIsNotNone(d["analyzer"])
        self.assertIsNotNone(d["waterfall"])
        self.assertTrue(str(d["audio"]).startswith("data:audio/wav;base64,"))
        self.assertIsNone(d["decode"])
        self.assertIsInstance(d["pocsag_likely"], bool)
        self.assertIsNotNone(d["novelty"])                      # session is open

    def test_pocsag_decodes_instead_of_audio(self):
        r = self.c.post("/api/analyze/signal",
                        json={"center_mhz": 152.0, "mode": "pocsag"})
        d = r.get_json()
        self.assertEqual(self.stub.capture_calls, 1)
        self.assertIsNone(d["audio"])                           # no hash audio for digital
        self.assertIsInstance(d["decode"], dict)
        self.assertEqual(d["decode"].get("protocol"), "POCSAG")

    def test_no_radio_degrades_to_frequency_only(self):
        self.st.controller.device = None
        r = self.c.post("/api/analyze/signal", json={"center_mhz": 433.92})
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertFalse(d["in_range"])
        self.assertIsNone(d["features"])
        self.assertIsInstance(d["candidates"], list)            # freq-only candidates
        self.assertIn("Pluto", d["note"])

    def test_requires_center(self):
        self.assertEqual(self.c.post("/api/analyze/signal", json={}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
