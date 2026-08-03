"""LISTEN must say WHAT the signal is — and whether audio is even possible.

pick_mode() defaults to NFM everywhere outside the broadcast bands, so pressing
LISTEN on a data link served the operator hash with nothing on screen to explain
it (a lab session went that way). The live audio stream now publishes the SAME
verdict ANALYZE gives — identify.iq_features + decode_router.interpret, no second
classifier — into listen_spectrum.demod.

The load-bearing rules here:
  1. the verdict actually reaches the operator (it is in the published frame);
  2. it is THROTTLED — this runs inside the real-time audio loop the operator
     already reported as choppy, so it must NOT be recomputed every block;
  3. a classifier failure costs the verdict, never the audio.
"""

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from survey import identify                    # noqa: E402  (patched per-test)
from survey.db import Database                 # noqa: E402
from survey.config import Config               # noqa: E402
from survey.server import create_app           # noqa: E402

BLOCKS = 6                                     # finite stream -> the test ends


class StubSDR:
    """Minimal swept-SDR stand-in with a FINITE stream_iq: a steady narrowband
    carrier, which iq_features classifies without needing a real radio."""
    is_swept_sdr = True

    def __init__(self):
        self.blocks = 0
        self.info = types.SimpleNamespace(
            modules=[types.SimpleNamespace(min_mhz=70.0, max_mhz=6000.0, side="right")],
            simulated=True)

    def is_alive(self):
        return True

    def stream_iq(self, center_hz, fs, n_block, stop):
        for _ in range(BLOCKS):
            if stop.is_set():
                break
            self.blocks += 1
            t = np.arange(int(n_block)) / float(fs)
            yield (np.exp(2j * np.pi * 1200.0 * t)
                   + 0.001 * (np.random.randn(int(n_block))
                              + 1j * np.random.randn(int(n_block)))).astype(np.complex64)


class ListenClassifyTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = Database(os.path.join(self.dir, "t.db"))
        self.cfg = Config(os.path.join(self.dir, "config.yaml"))
        # tiny blocks so the whole stream runs in well under one throttle window
        self.cfg.data["pluto"]["stream_sample_rate"] = 48000
        self.cfg.data["pluto"]["listen_block_seconds"] = 0.05
        self.app = create_app(self.db, self.cfg)
        self.st = self.app.config["APP_STATE"]
        self.stub = StubSDR()
        self.st.controller.device = self.stub
        self.st.controller.active_source = "pluto"
        self.c = self.app.test_client()
        self._real_feats = identify.iq_features
        self.calls = 0

    def tearDown(self):
        identify.iq_features = self._real_feats

    def _count_calls(self):
        """Wrap the REAL classifier so the payload stays honest while we count."""
        def counted(iq, fs):
            self.calls += 1
            return self._real_feats(iq, fs)
        identify.iq_features = counted

    def _stream(self):
        """Run one live-audio stream to completion, snapshotting the published
        listen_spectrum frame after every audio chunk (the generator clears it in
        finally, so it has to be read while the stream is alive). Returns
        (frames, audio_bytes)."""
        frames, audio = [], 0
        r = self.c.get("/api/listen_pcm?center_mhz=433.92&mode=nfm")
        try:
            for chunk in r.response:
                audio += len(chunk)
                f = self.st.controller.listen_spectrum
                if f:
                    frames.append(f)
        finally:
            r.close()
        return frames, audio

    # ---- 1. the verdict reaches the operator ----
    def test_live_frame_carries_the_signal_class(self):
        frames, _ = self._stream()
        self.assertTrue(frames, "the stream published no listen_spectrum frame")
        dm = frames[-1]["demod"]
        self.assertIn("modulation_class", dm)
        self.assertIn("class_summary", dm)
        self.assertIn("readable", dm)
        # the summary is interpret()'s wording, not a re-implementation
        self.assertTrue(dm["class_summary"])
        self.assertIn(dm["readable"],
                      ("decoded", "listen", "maybe_decode", "encrypted",
                       "identify_only", "none", "unknown"))
        # the existing demod telemetry is untouched by the addition
        self.assertIn("snr_db", dm)
        self.assertIn("gated", dm)

    def test_class_matches_what_analyze_would_say(self):
        """Same IQ, same pair of functions -> the live verdict cannot drift from
        the ANALYZE verdict. If this fails, two classifiers exist."""
        from survey.decode_router import interpret
        frames, _ = self._stream()
        dm = frames[-1]["demod"]
        fs = float(self.cfg.data["pluto"]["stream_sample_rate"])
        n = int(fs * self.cfg.data["pluto"]["listen_block_seconds"])
        t = np.arange(n) / fs
        iq = np.exp(2j * np.pi * 1200.0 * t).astype(np.complex64)
        feats = self._real_feats(iq[:8192], fs)
        self.assertEqual(dm["modulation_class"], feats["modulation_class"])
        self.assertEqual(dm["readable"], interpret(feats)["readable"])

    # ---- 2. throttled: it does NOT run every block ----
    def test_classification_is_throttled_not_per_block(self):
        self._count_calls()
        frames, _ = self._stream()
        self.assertGreaterEqual(self.stub.blocks, 4, "not enough blocks to prove it")
        self.assertLess(self.calls, self.stub.blocks,
                        "the classifier ran on every audio block — it is O(n^2) and "
                        "this is the real-time audio loop")
        self.assertEqual(self.calls, 1,          # 6 tiny blocks << the 2 s window
                         "one throttle window should yield exactly one pass")
        # ...and the verdict is REUSED in between, not dropped
        self.assertTrue(all("modulation_class" in f["demod"] for f in frames))

    def test_throttle_interval_is_what_limits_it(self):
        """Same run with the interval off classifies every block — proving the
        limiter above is the throttle, not a one-shot."""
        self.cfg.data["pluto"]["listen_classify_interval_s"] = 0.0
        self._count_calls()
        self._stream()
        self.assertEqual(self.calls, self.stub.blocks)

    def test_disabled_by_zero_samples(self):
        self.cfg.data["pluto"]["listen_classify_samples"] = 0
        self._count_calls()
        frames, audio = self._stream()
        self.assertEqual(self.calls, 0)
        self.assertGreater(audio, 0)
        self.assertNotIn("modulation_class", frames[-1]["demod"])

    # ---- 3. a classifier failure never touches the audio ----
    def test_classifier_exception_does_not_break_the_stream(self):
        def boom(iq, fs):
            self.calls += 1
            raise RuntimeError("synthetic classifier failure")
        identify.iq_features = boom
        frames, audio = self._stream()
        self.assertEqual(self.stub.blocks, BLOCKS, "the audio stream died")
        self.assertGreater(audio, 0, "no PCM was delivered")
        self.assertTrue(frames, "the analyzer/waterfall frame was lost with it")
        # the verdict is dropped — a stale or invented one would be worse
        self.assertNotIn("modulation_class", frames[-1]["demod"])
        self.assertIn("snr_db", frames[-1]["demod"])
        self.assertIsNotNone(frames[-1]["waterfall"])


if __name__ == "__main__":
    unittest.main()
