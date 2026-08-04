"""The window-4 referee must be provable before it grades anything.

Rule under test: content revealed → WIN, else FAIL. `cause` labels every FAIL
(code / physics / no-signal) but NEVER changes the WIN/FAIL score, and a
"physics" cause must be derivable from an objective input, so a code failure
cannot hide behind a physics excuse.
"""

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from survey.verdict import score_signal, tally


def _sig(**over):
    base = dict(
        feats={"signal_present": True, "snr_db": 30.0, "occupied_bw_khz": 12.5},
        routed={"protocol": None, "messages": [], "encrypted": False},
        audio_present=False, image_present=False,
        in_range=True, radio_lo_mhz=70.0, radio_hi_mhz=6000.0,
        center_mhz=462.5, capture_fs_hz=5_000_000.0, capture_error=False,
    )
    base.update(over)
    return base


class ScoreTest(unittest.TestCase):
    def test_capture_error_is_fail_on_code(self):
        v = score_signal(**_sig(capture_error=True))
        self.assertEqual(v["verdict"], "FAIL")
        self.assertEqual(v["cause"], "code")

    def test_out_of_band_is_fail_on_physics(self):
        v = score_signal(**_sig(in_range=False, center_mhz=5800.0,
                                radio_lo_mhz=325.0, radio_hi_mhz=3800.0))
        self.assertEqual(v["verdict"], "FAIL")
        self.assertEqual(v["reason_code"], "out_of_band")
        self.assertEqual(v["cause"], "physics")

    def test_no_radio_is_not_scored(self):
        # monitor not connected / busy: range unknown, no capture attempted -> NA
        v = score_signal(**_sig(in_range=False, radio_lo_mhz=None,
                                radio_hi_mhz=None, feats=None))
        self.assertEqual(v["verdict"], "NA")
        self.assertEqual(v["cause"], "setup")

    def test_empty_channel_is_fail_no_signal(self):
        v = score_signal(**_sig(feats={"signal_present": False, "snr_db": 3.0}))
        self.assertEqual(v["verdict"], "FAIL")
        self.assertEqual(v["cause"], "no-signal")

    def test_video_still_is_win_image(self):
        v = score_signal(**_sig(image_present=True,
                                routed={"protocol": "ANALOG-VIDEO-NTSC",
                                        "messages": [{"x": 1}], "encrypted": False}))
        self.assertEqual(v["verdict"], "WIN")
        self.assertEqual(v["content_kind"], "image")

    def test_analog_audio_is_win_audio(self):
        v = score_signal(**_sig(audio_present=True))
        self.assertEqual(v["verdict"], "WIN")
        self.assertEqual(v["content_kind"], "audio")

    def test_pocsag_text_is_win_text(self):
        v = score_signal(**_sig(routed={"protocol": "POCSAG",
                                        "messages": [{"text": "CALL 5551212"}],
                                        "encrypted": False}))
        self.assertEqual(v["verdict"], "WIN")
        self.assertEqual(v["content_kind"], "text")

    def test_encrypted_voice_is_fail_physics(self):
        v = score_signal(**_sig(routed={"protocol": "DMR",
                                        "messages": [{"talkgroup": 4021,
                                                      "encrypted": True}],
                                        "encrypted": True}))
        self.assertEqual(v["verdict"], "FAIL")
        self.assertEqual(v["reason_code"], "encrypted")
        self.assertEqual(v["cause"], "physics")

    def test_clear_digital_voice_without_audio_is_fail_code(self):
        # identified the call but produced no audio -> content not revealed, on us
        v = score_signal(**_sig(routed={"protocol": "P25",
                                        "messages": [{"talkgroup": 1}],
                                        "encrypted": False},
                                audio_present=False))
        self.assertEqual(v["verdict"], "FAIL")
        self.assertEqual(v["reason_code"], "voice_not_rendered")
        self.assertEqual(v["cause"], "code")

    def test_too_wide_for_capture_is_fail_physics(self):
        # 12 MHz-wide signal into a 5 MHz window (usable 0.8*5 = 4 MHz)
        v = score_signal(**_sig(feats={"signal_present": True, "snr_db": 25.0,
                                       "occupied_bw_khz": 12000.0},
                                capture_fs_hz=5_000_000.0))
        self.assertEqual(v["verdict"], "FAIL")
        self.assertEqual(v["reason_code"], "too_wide")
        self.assertEqual(v["cause"], "physics")

    def test_real_signal_no_decode_narrow_is_fail_code(self):
        # in band, present, fits the window, but nothing came out -> our gap
        v = score_signal(**_sig(feats={"signal_present": True, "snr_db": 20.0,
                                       "occupied_bw_khz": 25.0}))
        self.assertEqual(v["verdict"], "FAIL")
        self.assertEqual(v["reason_code"], "no_content")
        self.assertEqual(v["cause"], "code")


class TallyTest(unittest.TestCase):
    def test_headline_and_breakdown(self):
        vs = [
            score_signal(**_sig(audio_present=True)),                       # WIN
            score_signal(**_sig(routed={"protocol": "POCSAG",
                                        "messages": [{"text": "x"}],
                                        "encrypted": False})),             # WIN
            score_signal(**_sig(routed={"protocol": "DMR",
                                        "messages": [{"encrypted": True}],
                                        "encrypted": True})),              # FAIL physics
            score_signal(**_sig(capture_error=True)),                      # FAIL code
        ]
        t = tally(vs)
        self.assertEqual(t["total"], 4)
        self.assertEqual(t["wins"], 2)
        self.assertEqual(t["fails"], 2)
        self.assertEqual(t["did_job_pct"], 50.0)
        self.assertEqual(t["fails_physics"], 1)
        self.assertEqual(t["fails_code"], 1)

    def test_empty_sweep_is_zero_not_crash(self):
        self.assertEqual(tally([])["did_job_pct"], 0.0)

    def test_na_verdicts_excluded_from_denominator(self):
        vs = [
            score_signal(**_sig(audio_present=True)),                       # WIN
            score_signal(**_sig(in_range=False, radio_lo_mhz=None,
                                radio_hi_mhz=None, feats=None)),           # NA
        ]
        t = tally(vs)
        self.assertEqual(t["total"], 1)        # NA not counted
        self.assertEqual(t["did_job_pct"], 100.0)


if __name__ == "__main__":
    unittest.main()
