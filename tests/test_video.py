"""Unit tests for the analog composite video (NTSC / PAL) sync detector.

Synthesize a known raster with video.encode_video() — real line periods, real
4.7 us horizontal sync, a real half-line vertical interval — then run it back
through decode_video and assert the standard, the line rate, the field rate and
the recovered still.

The NEGATIVE half of this file matters more than the positive half. A false "camera
confirmed" in a client's bedroom is far worse than a miss, so the negatives here
are deliberately adversarial: noise, an unmodulated carrier, analog voice FM,
and — the nastiest — a 15734 Hz tone and a 15734 Hz squarewave, which have
EXACTLY the NTSC line rate and must still be rejected because their pulses are
the wrong shape. All headless, no hardware.
"""

import base64
import struct
import sys
import unittest
import zlib
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from survey import video                                            # noqa: E402
from survey.video import decode_video, encode_video, looks_like_video   # noqa: E402
from survey.decode_router import route_decode, interpret            # noqa: E402
from survey.ook import ook_encode                                   # noqa: E402

FS = 2_000_000.0          # 2 MHz synth rate (decimates x2 to the 1 MHz working rate)
FIELDS = 6                # ~100 ms — three frames, enough for a field/frame lock
DEV = 200_000.0           # FM peak deviation of the video link


def _only(result):
    msgs = result["messages"]
    assert len(msgs) == 1, f"expected 1 message, got {len(msgs)}: {result['note']}"
    return msgs[0]


def _fm(baseband, fs=FS, deviation=1.0):
    """Real baseband -> constant-envelope FM IQ (the negatives are built with this)."""
    return np.exp(1j * 2 * np.pi * np.cumsum(deviation * baseband) / fs).astype(np.complex64)


def _noise(n, scale=1.0, seed=0):
    rng = np.random.default_rng(seed)
    return (scale * (rng.standard_normal(n) + 1j * rng.standard_normal(n))).astype(np.complex64)


def _read_png(data_url):
    """Parse the emitted PNG back to (width, height, bitdepth, colortype, pixels)."""
    assert data_url.startswith("data:image/png;base64,"), data_url[:40]
    raw = base64.b64decode(data_url.split(",", 1)[1])
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"
    i, chunks = 8, {}
    while i < len(raw):
        ln = struct.unpack(">I", raw[i:i + 4])[0]
        tag, data = raw[i + 4:i + 8], raw[i + 8:i + 8 + ln]
        crc = struct.unpack(">I", raw[i + 8 + ln:i + 12 + ln])[0]
        assert crc == zlib.crc32(tag + data) & 0xFFFFFFFF, f"bad CRC on {tag}"
        chunks[tag] = chunks.get(tag, b"") + data
        i += 12 + ln
    w, h, depth, ctype = struct.unpack(">IIBB", chunks[b"IHDR"][:10])
    rows = np.frombuffer(zlib.decompress(chunks[b"IDAT"]), dtype=np.uint8)
    rows = rows.reshape(h, w + 1)
    assert np.all(rows[:, 0] == 0), "every scanline must carry filter type 0"
    return w, h, depth, ctype, rows[:, 1:].astype(np.float64)


class TestNTSC(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.iq = encode_video("NTSC", FIELDS, FS, deviation_hz=DEV)
        cls.res = decode_video(cls.iq, FS)

    def test_detects_ntsc(self):
        m = _only(self.res)
        self.assertEqual(m["standard"], "NTSC")
        self.assertAlmostEqual(m["line_rate_hz"], video.NTSC_LINE_HZ, delta=8.0)
        self.assertAlmostEqual(m["line_period_us"], 63.556, delta=0.05)

    def test_not_called_pal(self):
        # the whole point of refining the period against a high lag multiple:
        # NTSC and PAL are 0.7 % apart and must never be confused
        self.assertNotEqual(_only(self.res)["standard"], "PAL")

    def test_field_rate(self):
        m = _only(self.res)
        self.assertAlmostEqual(m["field_rate_hz"], 59.94, delta=0.1)
        self.assertEqual(m["lines_per_field"], 262.5)
        self.assertGreater(m["field_lock"], video.FIELD_LOCK_MIN)

    def test_sync_shape(self):
        m = _only(self.res)
        # 4.7 us nominal; the slice level and the decimation smear it a little
        self.assertGreaterEqual(m["sync_width_us"], 3.0)
        self.assertLessEqual(m["sync_width_us"], 8.0)
        self.assertGreater(m["sync_duty"], 0.03)
        self.assertLess(m["sync_duty"], 0.20)
        self.assertGreater(m["line_lock"], 0.5)
        self.assertEqual(m["sync_polarity"], "negative")

    def test_confidence_and_result_shape(self):
        self.assertEqual(self.res["protocol"], "ANALOG-VIDEO")
        self.assertGreaterEqual(self.res["confidence"], 0.75)
        self.assertLessEqual(self.res["confidence"], 0.95)   # never claims certainty
        m = _only(self.res)
        for key in ("standard", "line_rate_hz", "line_period_us", "field_rate_hz",
                    "sync_width_us", "sync_duty", "sync_polarity", "line_lock",
                    "confidence", "evidence", "caveats", "frame_png"):
            self.assertIn(key, m)
        self.assertTrue(m["evidence"])

    def test_verdict_never_claims_a_camera(self):
        m = _only(self.res)
        blob = (self.res["note"] + " " + " ".join(m["evidence"]) + " "
                + " ".join(m["caveats"])).lower()
        self.assertIn("not a confirmed camera", self.res["note"].lower())
        self.assertIn("not proof of a camera", blob)
        self.assertIn("emanation", blob)      # the honest alternative explanation

    def test_inverted_polarity_still_locks(self):
        # a Pluto's LO side flips the discriminator sign — the decoder must not
        # depend on sync tips being the negative excursion
        r = decode_video(encode_video("NTSC", FIELDS, FS, deviation_hz=DEV, invert=True), FS)
        m = _only(r)
        self.assertEqual(m["standard"], "NTSC")
        self.assertEqual(m["sync_polarity"], "positive")

    def test_blank_picture_still_locks(self):
        # sync only, no picture content at all: proves the verdict is read off the
        # SYNC structure, not off correlated image content
        r = decode_video(encode_video("NTSC", FIELDS, FS, deviation_hz=DEV,
                                      blank_picture=True), FS)
        self.assertEqual(_only(r)["standard"], "NTSC")

    def test_survives_light_noise(self):
        r = decode_video(encode_video("NTSC", FIELDS, FS, deviation_hz=DEV,
                                      noise=0.10, seed=7), FS)
        m = _only(r)
        self.assertEqual(m["standard"], "NTSC")
        self.assertAlmostEqual(m["line_rate_hz"], video.NTSC_LINE_HZ, delta=20.0)


class TestPAL(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.res = decode_video(encode_video("PAL", FIELDS, FS, deviation_hz=DEV), FS)

    def test_detects_pal(self):
        m = _only(self.res)
        self.assertEqual(m["standard"], "PAL")
        self.assertAlmostEqual(m["line_rate_hz"], video.PAL_LINE_HZ, delta=8.0)
        self.assertAlmostEqual(m["line_period_us"], 64.0, delta=0.05)

    def test_not_called_ntsc(self):
        self.assertNotEqual(_only(self.res)["standard"], "NTSC")

    def test_field_rate(self):
        m = _only(self.res)
        self.assertAlmostEqual(m["field_rate_hz"], 50.0, delta=0.1)
        self.assertEqual(m["lines_per_field"], 312.5)


class TestFrameRecovery(unittest.TestCase):
    """Phase 2 — the still. A recognizable picture is what turns a sync lock into
    something an operator can put in front of a client."""

    @classmethod
    def setUpClass(cls):
        cls.res = decode_video(encode_video("NTSC", FIELDS, FS, deviation_hz=DEV), FS)
        cls.msg = _only(cls.res)

    def test_png_is_valid_greyscale(self):
        w, h, depth, ctype, img = _read_png(self.msg["frame_png"])
        self.assertEqual((depth, ctype), (8, 0))          # 8-bit greyscale
        self.assertEqual(w, self.msg["frame_cols"])
        self.assertEqual(h, self.msg["frame_rows"])
        self.assertEqual(w, video.FRAME_COLS)
        self.assertGreaterEqual(h, 200)                   # ~a field of NTSC picture lines

    def test_picture_content_recovered(self):
        _w, _h, _d, _c, img = _read_png(self.msg["frame_png"])
        col = img.mean(axis=0)
        # the test card is 8 vertical bars -> 4 light/dark cycles across the row
        spec = np.abs(np.fft.rfft(col - col.mean()))
        self.assertEqual(int(np.argmax(spec)), 4)
        # ...a top-to-bottom luminance ramp...
        rowmean = img.mean(axis=1)
        self.assertGreater(rowmean[-40:].mean(), rowmean[40:80].mean())
        # ...and a bright centre block against its own column's surround
        centre = img[img.shape[0] // 2 - 20:img.shape[0] // 2 + 20,
                     img.shape[1] // 2 - 25:img.shape[1] // 2 + 25].mean()
        above = img[30:70, img.shape[1] // 2 - 25:img.shape[1] // 2 + 25].mean()
        self.assertGreater(centre, above + 60.0)

    def test_frame_can_be_skipped(self):
        # confirmation without the picture — the analyze path may not want the cost
        r = decode_video(encode_video("NTSC", FIELDS, FS, deviation_hz=DEV), FS,
                         frame=False)
        m = _only(r)
        self.assertIsNone(m["frame_png"])
        self.assertEqual(m["standard"], "NTSC")           # Phase 1 is unaffected


class TestNegatives(unittest.TestCase):
    """False positives are the failure that matters. Nothing below is video."""

    N = 200_000

    def _assert_rejected(self, iq, must_mention=None):
        r = decode_video(iq, FS)
        self.assertEqual(r["messages"], [], f"FALSE POSITIVE: {r['note']}")
        self.assertEqual(r["confidence"], 0.0)
        self.assertTrue(r["note"])
        if must_mention:
            self.assertIn(must_mention, r["note"].lower())

    def test_noise_rejected(self):
        self._assert_rejected(_noise(self.N, 1.0, seed=1))

    def test_unmodulated_carrier_rejected(self):
        t = np.arange(self.N) / FS
        self._assert_rejected(np.exp(1j * 2 * np.pi * 50_000.0 * t).astype(np.complex64))

    def test_carrier_with_noise_rejected(self):
        t = np.arange(self.N) / FS
        carrier = np.exp(1j * 2 * np.pi * 50_000.0 * t).astype(np.complex64)
        self._assert_rejected(carrier + _noise(self.N, 0.02, seed=4))

    def test_analog_voice_fm_rejected(self):
        # the signal this app is MOST likely to be pointed at: an audio bug.
        # Its baseband is highly correlated at a 63 us lag but has no PEAK there.
        t = np.arange(self.N) / FS
        audio = (0.6 * np.sin(2 * np.pi * 300.0 * t) + 0.3 * np.sin(2 * np.pi * 900.0 * t)
                 + 0.2 * np.sin(2 * np.pi * 1700.0 * t))
        audio *= 0.5 + 0.5 * np.sin(2 * np.pi * 3.0 * t)      # syllabic envelope
        self._assert_rejected(_fm(audio, FS, 5_000.0), "no video sync found")

    def test_voice_fm_with_noise_rejected(self):
        t = np.arange(self.N) / FS
        audio = 0.8 * np.sin(2 * np.pi * 440.0 * t) + 0.4 * np.sin(2 * np.pi * 1200.0 * t)
        self._assert_rejected(_fm(audio, FS, 5_000.0) + _noise(self.N, 0.02, seed=9))

    def test_line_rate_tone_rejected(self):
        # EXACTLY the NTSC line rate, wrong shape: a 15734 Hz whine (flyback tone,
        # switching supply) must not be promoted to "video" by its period alone.
        t = np.arange(self.N) / FS
        tone = np.sin(2 * np.pi * video.NTSC_LINE_HZ * t)
        r = decode_video(_fm(tone, FS, 30_000.0), FS)
        self.assertEqual(r["messages"], [], f"FALSE POSITIVE: {r['note']}")
        self.assertIn("periodic interference", r["note"])

    def test_line_rate_squarewave_rejected(self):
        t = np.arange(self.N) / FS
        sq = np.sign(np.sin(2 * np.pi * video.NTSC_LINE_HZ * t))
        r = decode_video(_fm(sq, FS, 30_000.0), FS)
        self.assertEqual(r["messages"], [], f"FALSE POSITIVE: {r['note']}")
        self.assertIn("periodic interference", r["note"])

    def test_off_standard_raster_rejected(self):
        # a 14 kHz pulse train: right SHAPE, wrong rate. Neither NTSC nor PAL, so
        # the decoder reports what it measured and refuses to name a standard.
        t = np.arange(self.N) / FS
        pulses = np.where((t * 14_000.0) % 1.0 < 0.074, -1.0, 0.0)
        r = decode_video(_fm(pulses, FS, 100_000.0), FS)
        self.assertEqual(r["messages"], [], f"FALSE POSITIVE: {r['note']}")
        self.assertIn("neither the ntsc", r["note"].lower())

    def test_ook_device_burst_rejected(self):
        # another decoder's signal: a cheap remote's keyed burst is not a raster
        payload = [1, 0, 1, 1, 0, 0, 1, 0, 1, 1, 1, 0]
        self._assert_rejected(ook_encode(payload, "PWM", 300.0, FS, repeats=6))

    def test_short_capture_is_honest(self):
        iq = encode_video("NTSC", 1, FS, deviation_hz=DEV)[:6000]
        r = decode_video(iq, FS)
        self.assertEqual(r["messages"], [])
        self.assertIn("too short", r["note"])

    def test_low_rate_capture_is_honest(self):
        # 48 kHz of baseband can't resolve a 4.7 us pulse — say so, don't guess
        r = decode_video(_noise(50_000, 1.0, seed=2), 48_000.0)
        self.assertEqual(r["messages"], [])
        self.assertIn("capture rate too low", r["note"])

    def test_too_short_raises(self):
        with self.assertRaises(ValueError):
            decode_video(np.zeros(8, dtype=np.complex64), FS)


class TestGate(unittest.TestCase):
    """looks_like_video() — the cheap pre-gate the auto-router uses."""

    @staticmethod
    def _feats(**kw):
        base = {"occupied_bw_khz": 6000.0, "continuity": "continuous",
                "duty_cycle": 1.0, "modulation_class": "analog_video",
                "signal_present": True}
        base.update(kw)
        return base

    def test_accepts_wideband_continuous(self):
        self.assertTrue(looks_like_video(self._feats()))

    def test_accepts_wideband_labelled_ofdm(self):
        # deliberate wide net: classify_modulation() tests its flat/wide/strong
        # OFDM branch BEFORE analog_video, so a real camera can arrive labelled
        # OFDM. decode_video's sync gate is the real filter.
        self.assertTrue(looks_like_video(self._feats(modulation_class="ofdm_wideband",
                                                     occupied_bw_khz=18000.0)))

    def test_rejects_narrowband_bug(self):
        self.assertFalse(looks_like_video(self._feats(occupied_bw_khz=12.0,
                                                      modulation_class="analog_voice")))

    def test_rejects_bursty_remote(self):
        self.assertFalse(looks_like_video(self._feats(occupied_bw_khz=2000.0,
                                                      modulation_class="ook",
                                                      continuity="bursty",
                                                      duty_cycle=0.1)))

    def test_rejects_fsk_telemetry(self):
        self.assertFalse(looks_like_video(self._feats(modulation_class="fsk")))

    def test_rejects_empty_and_absent(self):
        self.assertFalse(looks_like_video({}))
        self.assertFalse(looks_like_video(None))
        self.assertFalse(looks_like_video(self._feats(signal_present=False)))


class TestRouter(unittest.TestCase):
    """Registration in decode_router — the auto path and the explicit mode."""

    @classmethod
    def setUpClass(cls):
        cls.iq = encode_video("NTSC", FIELDS, FS, deviation_hz=DEV)
        cls.feats = {"modulation_class": "analog_video", "occupied_bw_khz": 6000.0,
                     "continuity": "continuous", "duty_cycle": 1.0,
                     "signal_present": True, "fsk_states": 0, "fsk_shift_khz": 0.0,
                     "symbol_rate_kbd": 0.0, "snr_db": 25.0, "distinct_tones": 2,
                     "spectral_flatness": 0.3}

    def test_prefer_video_decodes(self):
        r = route_decode(self.iq, FS, None, prefer="video")
        self.assertEqual(r["protocol"], "ANALOG-VIDEO")
        self.assertTrue(r["messages"])

    def test_auto_gate_routes_video(self):
        r = route_decode(self.iq, FS, self.feats)
        self.assertEqual(r["protocol"], "ANALOG-VIDEO")
        self.assertIn("video", r["tried"])

    def test_auto_gate_holds_on_voice(self):
        # narrowband analog voice never reaches the video decoder
        t = np.arange(120_000) / FS
        iq = _fm(np.sin(2 * np.pi * 800.0 * t), FS, 5_000.0)
        feats = dict(self.feats, modulation_class="analog_voice", occupied_bw_khz=12.0)
        r = route_decode(iq, FS, feats)
        self.assertNotIn("video", r["tried"])
        self.assertEqual(r["messages"], [])

    def test_interpret_reports_raster_not_camera(self):
        v = interpret(self.feats, "ANALOG-VIDEO")
        self.assertEqual(v["readable"], "decoded")
        self.assertIn("not a confirmed camera", v["summary"].lower())

    def test_interpret_unconfirmed_video_stays_a_shape(self):
        # still 'listen' — a wideband ANALOG carrier really does produce audio, so
        # the verdict must not tell the operator listening is pointless. It must
        # also not read as a finding.
        v = interpret(self.feats, None)
        self.assertEqual(v["readable"], "listen")
        self.assertIn("not confirmed", v["summary"].lower())
        self.assertIn("video mode", v["summary"].lower())


if __name__ == "__main__":
    unittest.main()
