"""Monitor recording: WAV capture worker + list + purge (move-not-delete).

A stub SDR with a finite stream_iq lets the record worker complete quickly and
deterministically (join the daemon thread); no real radio, no sleeps-as-sync.
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


class StubSDR:
    is_swept_sdr = True

    def __init__(self):
        self.info = types.SimpleNamespace(
            modules=[types.SimpleNamespace(min_mhz=70.0, max_mhz=6000.0, side="right")],
            simulated=True)

    def is_alive(self):
        return True

    def stream_iq(self, center_hz, fs, n_block, stop):
        # finite: yields a few blocks then ends, so the worker finalizes promptly
        for _ in range(3):
            if stop.is_set():
                break
            t = np.arange(int(n_block)) / float(fs)
            yield (np.exp(1j * 2 * np.pi * 1000.0 * t)
                   + 0.01 * np.random.randn(int(n_block))).astype(np.complex64)


class TestRecordings(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = Database(os.path.join(self.dir, "t.db"))
        self.cfg = Config(os.path.join(self.dir, "config.yaml"))
        self.cfg.data["pluto"]["stream_sample_rate"] = 48000
        self.cfg.data["pluto"]["listen_block_seconds"] = 0.05
        self.sid = self.db.create_session("1 A St", 0, 0, "test", 25, 5, "op")
        self.app = create_app(self.db, self.cfg)
        self.st = self.app.config["APP_STATE"]
        self.st.controller.device = StubSDR()
        self.st.controller.active_source = "pluto"
        self.c = self.app.test_client()
        self.rec_dir = self.cfg.abs_path("recordings")

    def _record_once(self, **body):
        body.setdefault("center_mhz", 433.0)
        r = self.c.post("/api/record/start", json=body)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        t = self.st.controller._thread
        if t:
            t.join(timeout=8)
        return r.get_json()

    def test_record_writes_wav_and_row(self):
        self._record_once(mode="nfm")
        recs = self.c.get("/api/recordings").get_json()
        self.assertEqual(len(recs), 1)
        rec = recs[0]
        self.assertGreater(rec["duration_s"], 0)
        self.assertEqual(rec["kind"], "audio")
        path = os.path.join(self.rec_dir, rec["file_path"])
        self.assertTrue(os.path.exists(path))
        with open(path, "rb") as f:
            self.assertEqual(f.read(4), b"RIFF")          # valid WAV

    def test_recording_namespaced_by_session(self):
        self._record_once()
        rec = self.c.get("/api/recordings").get_json()[0]
        self.assertTrue(rec["file_path"].startswith(f"{self.sid}{os.sep}")
                        or rec["file_path"].startswith(f"{self.sid}/"))

    def test_audio_endpoint_serves_wav(self):
        self._record_once()
        rid = self.c.get("/api/recordings").get_json()[0]["id"]
        r = self.c.get(f"/api/recordings/{rid}/audio")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data[:4], b"RIFF")

    def test_purge_moves_not_deletes(self):
        self._record_once()
        rec = self.c.get("/api/recordings").get_json()[0]
        orig = os.path.join(self.rec_dir, rec["file_path"])
        self.assertTrue(os.path.exists(orig))
        pr = self.c.post("/api/recordings/purge", json={})
        self.assertEqual(pr.status_code, 200)
        self.assertEqual(pr.get_json()["moved"], 1)
        self.assertFalse(os.path.exists(orig))            # gone from working area
        moved = self.c.get("/api/recordings").get_json()[0]
        self.assertIn("_archive", moved["file_path"])
        self.assertTrue(os.path.exists(os.path.join(self.rec_dir, moved["file_path"])))

    def test_record_requires_session(self):
        d = tempfile.mkdtemp()
        db = Database(os.path.join(d, "empty.db"))
        cfg = Config(os.path.join(d, "config.yaml"))
        c = create_app(db, cfg).test_client()          # no session in this DB
        self.assertEqual(c.post("/api/record/start",
                                json={"center_mhz": 433.0}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
