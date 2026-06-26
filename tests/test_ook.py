"""Unit tests for the generic OOK/ASK + 2-FSK cheap-device decoder.

Synthesize a known burst with ook.ook_encode() (which shares the SAME line-code
timing model, _bits_to_runs, as the decoder's slicers — so a broken slicer fails
loudly), then round-trip it through decode_ook and assert exact bit recovery
across encoding x modulation x repeat count. The integrity test these protocols
have is REPEAT AGREEMENT, not a CRC, so the honesty-gate tests assert that a
noise-only / single-glitch / pure-tone capture decodes to NOTHING.

All headless, no hardware. Synth at fs=1.0 MHz (a convenient Pluto-ish rate where
a few-hundred-us symbol gives plenty of samples/symbol).
"""

import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from survey import ook
from survey.ook import decode_ook, ook_encode

FS = 1_000_000.0          # 1 MHz synth rate
PULSE = 300.0             # 300 us base symbol unit (typical cheap remote)


def _only(result):
    msgs = result["messages"]
    assert len(msgs) == 1, f"expected 1 message, got {len(msgs)}: {msgs}"
    return msgs[0]


class TestRoundTrip(unittest.TestCase):
    def setUp(self):
        # a recognizable 24-bit payload
        self.payload = [1, 0, 1, 1, 0, 0, 1, 0, 1, 1, 1, 0,
                        0, 1, 0, 1, 0, 0, 1, 1, 1, 0, 0, 1]

    def test_pwm_ook_roundtrip(self):
        iq = ook_encode(self.payload, "PWM", PULSE, FS, repeats=5,
                        modulation="OOK/ASK")
        m = _only(decode_ook(iq, FS))
        self.assertEqual(m["encoding"], "PWM")
        self.assertEqual(m["modulation"], "OOK/ASK")
        self.assertEqual([int(c) for c in m["bits"]], self.payload)
        self.assertGreaterEqual(m["repeats"], 2)

    def test_ppm_ook_roundtrip(self):
        iq = ook_encode(self.payload, "PPM", PULSE, FS, repeats=4,
                        modulation="OOK/ASK")
        m = _only(decode_ook(iq, FS))
        self.assertEqual(m["encoding"], "PPM")
        self.assertEqual([int(c) for c in m["bits"]], self.payload)

    def test_manchester_ook_roundtrip(self):
        iq = ook_encode(self.payload, "Manchester", PULSE, FS, repeats=4,
                        modulation="OOK/ASK")
        m = _only(decode_ook(iq, FS))
        self.assertEqual(m["encoding"], "Manchester")
        self.assertEqual([int(c) for c in m["bits"]], self.payload)

    def test_pwm_fsk_roundtrip(self):
        iq = ook_encode(self.payload, "PWM", PULSE, FS, repeats=5,
                        modulation="2-FSK", deviation_hz=20000.0)
        m = _only(decode_ook(iq, FS))
        self.assertEqual(m["modulation"], "2-FSK")
        self.assertEqual([int(c) for c in m["bits"]], self.payload)

    def test_nrz_fsk_roundtrip(self):
        # NRZ over FSK is the classic continuous-2FSK keying
        payload = [1, 1, 0, 0, 1, 0, 1, 1, 1, 0, 0, 0, 1, 0, 1, 0]
        iq = ook_encode(payload, "NRZ", PULSE, FS, repeats=4,
                        modulation="2-FSK", deviation_hz=25000.0)
        m = _only(decode_ook(iq, FS))
        self.assertEqual(m["modulation"], "2-FSK")
        self.assertEqual([int(c) for c in m["bits"]], payload)

    def test_hex_matches_bits(self):
        iq = ook_encode([1, 0, 1, 0, 1, 1, 0, 0], "PWM", PULSE, FS, repeats=4)
        m = _only(decode_ook(iq, FS))
        # 10101100 = 0xAC
        self.assertEqual(m["hex"], "ac")


class TestRepeats(unittest.TestCase):
    def test_repeat_count_reported(self):
        iq = ook_encode([1, 0, 0, 1, 1, 0, 1, 0], "PWM", PULSE, FS, repeats=6)
        m = _only(decode_ook(iq, FS))
        # not every burst always survives edge effects, but most should agree
        self.assertGreaterEqual(m["repeats"], 2)
        self.assertGreaterEqual(m["total_bursts"], m["repeats"])

    def test_single_repeat_no_preamble_rejected(self):
        # ONE packet, no preamble -> fails the agreement gate -> no message
        iq = ook_encode([1, 0, 1, 1, 0, 0, 1, 0], "PWM", PULSE, FS, repeats=1)
        r = decode_ook(iq, FS)
        self.assertEqual(r["messages"], [])

    def test_single_repeat_with_preamble_accepted(self):
        # ONE packet but a long alternating preamble -> the alternate honesty anchor
        iq = ook_encode([1, 1, 0, 0, 1, 0, 1, 1], "NRZ", PULSE, FS, repeats=1,
                        modulation="2-FSK", preamble_bits=24)
        r = decode_ook(iq, FS)
        self.assertEqual(len(r["messages"]), 1)
        self.assertTrue(r["messages"][0]["preamble"])


class TestHonestyGate(unittest.TestCase):
    def test_noise_decodes_to_nothing(self):
        rng = np.random.default_rng(1)
        noise = (rng.standard_normal(200000)
                 + 1j * rng.standard_normal(200000)).astype(np.complex64)
        r = decode_ook(noise, FS)
        self.assertEqual(r["messages"], [])

    def test_pure_carrier_decodes_to_nothing(self):
        # a steady unmodulated carrier: flat envelope, flat IF -> no pulse train
        t = np.arange(200000) / FS
        carrier = np.exp(1j * 2 * np.pi * 5000.0 * t).astype(np.complex64)
        r = decode_ook(carrier, FS)
        self.assertEqual(r["messages"], [])

    def test_single_glitch_decodes_to_nothing(self):
        # one lonely pulse in a sea of silence -> not enough edges, no repeats
        env = np.full(100000, 0.05, dtype=np.float64)
        env[40000:40300] = 1.0
        iq = env.astype(np.complex64)
        r = decode_ook(iq, FS)
        self.assertEqual(r["messages"], [])

    def test_too_short_raises(self):
        with self.assertRaises(ValueError):
            decode_ook(np.zeros(8, dtype=np.complex64), FS)

    def test_result_shape(self):
        iq = ook_encode([1, 0, 1, 0, 1, 0, 1, 1], "PWM", PULSE, FS, repeats=4)
        r = decode_ook(iq, FS)
        self.assertEqual(r["protocol"], "OOK/FSK-DEVICE")
        self.assertIn("note", r)
        self.assertIn("stats", r)
        m = r["messages"][0]
        for key in ("encoding", "modulation", "n_bits", "bits", "pulse_us",
                    "gap_us", "repeats", "device_guess"):
            self.assertIn(key, m)


class TestNoiseTolerance(unittest.TestCase):
    def test_decode_survives_light_noise(self):
        payload = [1, 0, 1, 1, 0, 0, 1, 0, 1, 1, 1, 0]
        iq = ook_encode(payload, "PWM", PULSE, FS, repeats=6,
                        modulation="2-FSK", deviation_hz=30000.0,
                        noise=0.05, seed=3)
        m = _only(decode_ook(iq, FS))
        self.assertEqual([int(c) for c in m["bits"]], payload)


class TestDeviceGuess(unittest.TestCase):
    def test_24bit_remote_guess(self):
        payload = [1, 0] * 12          # 24 bits
        iq = ook_encode(payload, "PWM", 350.0, FS, repeats=5)
        m = _only(decode_ook(iq, FS))
        self.assertIn("remote", m["device_guess"])


class TestGate(unittest.TestCase):
    def test_looks_like_ook_accepts_bursty_ook(self):
        self.assertTrue(ook.looks_like_ook(
            {"occupied_bw_khz": 60.0, "continuity": "bursty",
             "modulation_class": "ook", "fsk_states": 1, "fsk_shift_khz": 0.0,
             "duty_cycle": 0.1, "symbol_rate_kbd": 3.0}))

    def test_looks_like_ook_accepts_bursty_fsk(self):
        self.assertTrue(ook.looks_like_ook(
            {"occupied_bw_khz": 80.0, "continuity": "intermittent",
             "modulation_class": "fsk", "fsk_states": 2, "fsk_shift_khz": 5.0,
             "duty_cycle": 0.3, "symbol_rate_kbd": 4.0}))

    def test_looks_like_ook_rejects_wideband_continuous(self):
        # Wi-Fi-ish: wide and continuous -> not a cheap ISM remote
        self.assertFalse(ook.looks_like_ook(
            {"occupied_bw_khz": 20000.0, "continuity": "continuous",
             "modulation_class": "psk", "fsk_states": 1, "fsk_shift_khz": 0.0,
             "duty_cycle": 1.0, "symbol_rate_kbd": 1000.0}))

    def test_looks_like_ook_rejects_empty(self):
        self.assertFalse(ook.looks_like_ook({}))


if __name__ == "__main__":
    unittest.main()
