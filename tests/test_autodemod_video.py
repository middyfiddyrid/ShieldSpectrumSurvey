"""AUTO-DEMOD must recover a hidden camera, which means capturing WIDE.

The question this answers: if the app cannot decode a TV station, how is it
supposed to decode a camera feed in a client's house?

They are not the same problem. Broadcast TV is ATSC 8VSB — trellis + Reed-Solomon
+ an adaptive equalizer + an MPEG transport demux, the hardest video chain in the
band. A covert analog camera is FM-modulated composite video: discriminate the
carrier, slice the sync tips, read the raster. The app already does the second
one (survey/video.py) and these tests prove the auto-demod path reaches it.

The bug they were written for is real and was shipped: auto-demod captured at a
FIXED narrow sample rate, so a wideband video sender overflowed the capture, its
sync tips aliased, and a camera read as ordinary wideband FM. video.py's own
limitations say "capture as wide as the radio allows" — these tests hold the
worker to that.
"""

import sys
import tempfile
import types
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from survey import identify as identify_mod          # noqa: E402
from survey.autodemod import AutoDemod, WIDE_BW_KHZ  # noqa: E402
from survey.config import Config                      # noqa: E402
from survey.db import Database                        # noqa: E402
from survey.video import decode_video, encode_video   # noqa: E402

# Deviation chosen by MEASUREMENT, not guess: at 1 MHz a 20 MHz capture recovers
# a picture and a 5 MHz capture does not, which is exactly the boundary these
# tests exist to hold. (Swept 1/2/4/6 MHz on 2026-08-07.)
WIDE_DEV_HZ = 1_000_000.0
WIDE_FS = 20_000_000.0
NARROW_FS = 5_000_000.0


def _worker(demo=False):
    tmp = tempfile.mkdtemp()
    db = Database(str(Path(tmp) / "t.db"))
    cfg = Config(str(Path(tmp) / "config.yaml"))
    controller = types.SimpleNamespace(state={}, listening=False,
                                       active_source="b220")
    w = AutoDemod(controller, db, cfg, demo=demo)
    w.session_id = db.execute(
        "INSERT INTO sessions (address, created_utc, status) VALUES (?,?,?)",
        ("test", 1.0, "open"))
    return w


def _camera(fs, seconds=0.14, deviation_hz=WIDE_DEV_HZ):
    fields = max(4, int(seconds * 60))
    return encode_video(standard="NTSC", fields=fields, fs=fs,
                        deviation_hz=deviation_hz, noise=0.01, seed=3)


class TestTheDecoderItselfWorks(unittest.TestCase):
    """Baseline: given a wide enough capture, a picture really does come back."""

    def test_wide_capture_recovers_the_raster_and_a_still(self):
        iq = _camera(WIDE_FS)
        res = decode_video(iq, WIDE_FS)
        self.assertTrue(res["messages"], "no video lock on a synthetic camera")
        m = res["messages"][0]
        self.assertEqual(m["standard"], "NTSC")
        self.assertAlmostEqual(m["line_rate_hz"], 15734.264, delta=60.0)
        self.assertTrue(m["frame_png"].startswith("data:image/png;base64,"),
                        "no still recovered from a clean wide capture")

    def test_a_too_narrow_capture_loses_the_camera(self):
        """THE failure mode. The signal IS a camera; capture it too narrow and
        the raster does not survive. This is why capture width is chosen per
        signal instead of being a constant."""
        wide = _camera(WIDE_FS)
        # decimate to a narrow span the way a 5 MHz capture would see it
        step = int(WIDE_FS // NARROW_FS)
        narrow = wide[::step]
        res = decode_video(narrow, NARROW_FS)
        got_picture = bool(res.get("messages")
                           and res["messages"][0].get("frame_png"))
        self.assertFalse(got_picture,
                         "narrow capture unexpectedly recovered a still — the "
                         "premise of the width fix needs re-checking")


class TestCaptureWidthIsChosenPerSignal(unittest.TestCase):
    def test_wideband_signal_gets_a_wide_capture(self):
        w = _worker()
        fs, secs = w._capture_shape({"bw_khz": 6000.0}, "pluto")
        self.assertGreater(fs, NARROW_FS,
                           "a 6 MHz-wide carrier was captured at the narrow "
                           "default — a camera would alias and be missed")
        self.assertGreaterEqual(secs, 0.09, "too short for a field lock")

    def test_narrowband_signal_keeps_the_cheap_capture(self):
        """The wide grab must not become the default — it costs radio time on
        every one of a few hundred signals."""
        w = _worker()
        fs, _ = w._capture_shape({"bw_khz": 16.0}, "pluto")
        self.assertLessEqual(fs, NARROW_FS)

    def test_sample_budget_stays_bounded(self):
        from survey.autodemod import MAX_CAPTURE_SAMPLES
        w = _worker()
        for bw in (1000.0, 6000.0, 20000.0, 80000.0):
            fs, secs = w._capture_shape({"bw_khz": bw}, "pluto")
            self.assertLessEqual(int(fs * secs), MAX_CAPTURE_SAMPLES,
                                 f"unbounded capture at bw={bw} kHz")

    def test_missing_bandwidth_falls_back_safely(self):
        w = _worker()
        for row in ({}, {"bw_khz": None}, {"bw_khz": 0}):
            fs, secs = w._capture_shape(row, "pluto")
            self.assertGreater(fs, 0)
            self.assertGreater(secs, 0)

    def test_threshold_is_the_documented_one(self):
        w = _worker()
        below, _ = w._capture_shape({"bw_khz": WIDE_BW_KHZ - 1}, "pluto")
        above, _ = w._capture_shape({"bw_khz": WIDE_BW_KHZ * 4}, "pluto")
        self.assertLess(below, above)


class TestWorkerSurfacesTheCamera(unittest.TestCase):
    def _feats(self, iq, fs):
        return identify_mod.iq_features(np.asarray(iq)[:8192], fs)

    def test_raster_is_confirmed_even_when_content_is_locked(self):
        """A camera must never go unreported because attribution has not been
        set. The RASTER is the detection; only the picture is content."""
        w = _worker()
        iq = _camera(WIDE_FS)
        feats = self._feats(iq, WIDE_FS)
        got = w._video_check(iq, WIDE_FS, feats, permitted=False, bw_khz_hint=6000.0)
        self.assertIsNotNone(got, "video raster not reported while locked")
        self.assertTrue(got["video_confirmed"])
        self.assertEqual(got["video_standard"], "NTSC")
        self.assertIsNone(got["picture"],
                          "a still was rendered without content permission")

    def test_picture_is_returned_once_content_is_permitted(self):
        w = _worker()
        iq = _camera(WIDE_FS)
        feats = self._feats(iq, WIDE_FS)
        got = w._video_check(iq, WIDE_FS, feats, permitted=True, bw_khz_hint=6000.0)
        self.assertIsNotNone(got)
        self.assertTrue(got["picture"].startswith("data:image/png;base64,"))

    def test_a_voice_carrier_is_not_called_video(self):
        """The gate has to reject ordinary narrowband FM, or every radio in the
        building becomes a 'camera'."""
        w = _worker()
        fs = 1_000_000.0
        t = np.arange(int(fs * 0.12)) / fs
        tone = np.sin(2 * np.pi * 1000.0 * t)
        phase = np.cumsum(2 * np.pi * 5000.0 * tone / fs)
        iq = np.exp(1j * phase).astype(np.complex64)
        feats = self._feats(iq, fs)
        self.assertIsNone(w._video_check(iq, fs, feats, permitted=True, bw_khz_hint=16.0))

    def test_confirmed_video_carries_the_not_a_camera_id_caveat(self):
        """A raster lock is evidence of a video source, NOT proof of a camera —
        a monitor emanation or a video sender relaying a TV looks identical.
        That caveat has to travel with the finding."""
        w = _worker()
        iq = _camera(WIDE_FS)
        feats = self._feats(iq, WIDE_FS)
        got = w._video_check(iq, WIDE_FS, feats, permitted=True, bw_khz_hint=6000.0)
        self.assertTrue(any("not proof of a camera" in c.lower()
                            for c in got["video_caveats"]),
                        "the not-a-camera-ID caveat did not survive")


class TestRanking(unittest.TestCase):
    def test_video_outranks_everything_in_the_ledger(self):
        w = _worker()
        w._store({"id": 1, "center_mhz": 915.0, "anomaly_score": 0.2},
                 {"state": "characterized", "video_confirmed": True})
        w._store({"id": 2, "center_mhz": 462.0, "anomaly_score": 3.0},
                 {"state": "demodulated", "messages": ["hello"]})
        rows = w.state()["rows"]
        self.assertEqual(rows[0]["id"], 1,
                         "a confirmed video raster must sort to the top")


if __name__ == "__main__":
    unittest.main()
