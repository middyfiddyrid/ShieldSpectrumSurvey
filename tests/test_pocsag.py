"""Unit tests for the POCSAG paging decoder.

Synthesize a known transmission with pocsag.encode_pocsag() (which shares the
same BCH generator as the decoder, so a wrong polynomial fails loudly), then
round-trip it through decode_pocsag and assert exact recovery across baud x mode
x polarity x frame x BCH-error. All headless — no hardware. Most tests synth at
fs=38400 (dec=1, integer sps) for speed; test_full_chain_through_iq runs at the
real 2.4 MHz capture rate to exercise decimation + fractional symbol timing.
"""

import unittest

import numpy as np

from survey import pocsag
from survey.pocsag import (FSC, FSC_INV, IDLE, BCH_POLY, DEVIATION_HZ, bch_check,
                           encode_codeword, encode_pocsag, decode_pocsag, _bits_to_iq)

FS = 38400.0          # synth rate: dec=1, integer sps (75/32/16)
PRE = 64              # short preamble keeps the synth arrays small


def _msgs(result):
    return result["messages"]


def _only(result):
    msgs = result["messages"]
    assert len(msgs) == 1, f"expected 1 message, got {len(msgs)}: {msgs}"
    return msgs[0]


class TestConstants(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(FSC, 0x7CD215D8)
        self.assertEqual(FSC_INV, 0x832DEA27)
        self.assertEqual(IDLE, 0x7A89C197)
        self.assertEqual(BCH_POLY, 0x769)


class TestBCH(unittest.TestCase):
    def setUp(self):
        self.rng = np.random.default_rng(0)

    def test_bch_roundtrip(self):
        for _ in range(60):
            flag = int(self.rng.integers(0, 2))
            data20 = int(self.rng.integers(0, 1 << 20))
            cw = encode_codeword(flag, data20)
            ok, c, ne = bch_check(cw)
            self.assertTrue(ok); self.assertEqual(ne, 0); self.assertEqual(c, cw)
            # exactly the data we put in (pins the MSB-first field layout)
            self.assertEqual((cw >> 31) & 1, flag)
            self.assertEqual((cw >> 11) & 0xFFFFF, data20)
            # one error -> corrected back to the original
            b = int(self.rng.integers(0, 32))
            ok, c, ne = bch_check(cw ^ (1 << b))
            self.assertTrue(ok); self.assertEqual(ne, 1); self.assertEqual(c, cw)
            # two errors -> corrected back to the original
            b1 = int(self.rng.integers(0, 32))
            b2 = (b1 + 1 + int(self.rng.integers(0, 31))) % 32
            ok, c, ne = bch_check(cw ^ (1 << b1) ^ (1 << b2))
            self.assertTrue(ok); self.assertEqual(ne, 2); self.assertEqual(c, cw)

    def test_three_errors_uncorrectable(self):
        cw = encode_codeword(1, 0xABCDE)
        bad = cw ^ 0b111            # three adjacent bit errors
        ok, c, ne = bch_check(bad)
        self.assertFalse(ok); self.assertEqual(ne, -1)

    def test_detect_only_never_flips(self):
        cw = encode_codeword(0, 0x12345)
        ok, c, ne = bch_check(cw ^ (1 << 5), correct=False)
        self.assertFalse(ok); self.assertEqual(c, cw ^ (1 << 5)); self.assertEqual(ne, -1)


class TestDecode(unittest.TestCase):
    def setUp(self):
        self.rng = np.random.default_rng(0)

    def test_numeric_decode(self):
        iq = encode_pocsag([{"ric": 123456, "mode": "numeric", "text": "1234567890"}],
                           512, FS, preamble_bits=PRE)
        m = _only(decode_pocsag(iq, FS))
        self.assertEqual(m["ric"], 123456)
        self.assertEqual(m["numeric"], "1234567890")

    def test_numeric_spares(self):
        iq = encode_pocsag([{"ric": 200000, "mode": "numeric", "text": "12 U-34"}],
                           1200, FS, preamble_bits=PRE)
        m = _only(decode_pocsag(iq, FS))
        self.assertEqual(m["numeric"], "12 U-34")

    def test_alpha_decode(self):
        iq = encode_pocsag([{"ric": 654321, "mode": "alpha", "text": "TEST 42"}],
                           1200, FS, preamble_bits=PRE)
        m = _only(decode_pocsag(iq, FS))
        self.assertEqual(m["ric"], 654321)
        self.assertEqual(m["alpha"], "TEST 42")
        self.assertEqual(m["type"], "alpha")

    def test_alpha_long_multi_codeword(self):
        text = "EVACUATE BUILDING 7 NOW -- CALL 5551234"   # >30 chars, spans >=3 codewords
        iq = encode_pocsag([{"ric": 1000000, "mode": "alpha", "text": text}],
                           512, FS, preamble_bits=PRE)
        m = _only(decode_pocsag(iq, FS))
        self.assertEqual(m["alpha"], text)

    def test_ric_frame_lowbits(self):
        addr18 = 0x1ABCD
        for frame in range(8):
            ric = (addr18 << 3) | frame
            iq = encode_pocsag([{"ric": ric, "mode": "alpha", "text": "HI"}],
                               1200, FS, preamble_bits=PRE)
            m = _only(decode_pocsag(iq, FS))
            self.assertEqual(m["ric"], ric, f"frame {frame}")
            self.assertEqual(m["frame"], frame)
            self.assertEqual(m["ric"] & 7, frame)

    def test_function_bits(self):
        for fn in range(4):
            iq = encode_pocsag([{"ric": 333333, "function": fn, "mode": "alpha",
                                 "text": "X"}], 1200, FS, preamble_bits=PRE)
            m = _only(decode_pocsag(iq, FS))
            self.assertEqual(m["function"], fn)

    def test_baud_autodetect(self):
        for baud in (512, 1200, 2400):
            iq = encode_pocsag([{"ric": 424242, "mode": "alpha", "text": "BAUD"}],
                               baud, FS, preamble_bits=PRE)
            r = decode_pocsag(iq, FS)
            self.assertEqual(r["baud"], baud, f"baud {baud}")
            self.assertEqual(_only(r)["alpha"], "BAUD")

    def test_polarity_inversion(self):
        iq = encode_pocsag([{"ric": 555000, "mode": "alpha", "text": "FLIP"}],
                           1200, FS, invert=True, preamble_bits=PRE)
        r = decode_pocsag(iq, FS)
        self.assertTrue(r["inverted"])
        self.assertEqual(_only(r)["alpha"], "FLIP")

    def test_idle_skip(self):
        # one page in frame 5; every other slot is IDLE -> exactly one message
        ric = (0x10000 << 3) | 5
        iq = encode_pocsag([{"ric": ric, "mode": "numeric", "text": "911"}],
                           1200, FS, preamble_bits=PRE)
        r = decode_pocsag(iq, FS)
        self.assertEqual(len(_msgs(r)), 1)
        self.assertEqual(_msgs(r)[0]["numeric"], "911")

    def test_two_messages_one_transmission(self):
        msgs = [{"ric": 111111, "mode": "alpha", "text": "FIRST"},
                {"ric": 222222, "mode": "numeric", "text": "456"}]
        iq = encode_pocsag(msgs, 1200, FS, preamble_bits=PRE)
        got = {m["ric"]: m for m in _msgs(decode_pocsag(iq, FS))}
        self.assertIn(111111, got); self.assertIn(222222, got)
        self.assertEqual(got[111111]["alpha"], "FIRST")
        self.assertEqual(got[222222]["numeric"], "456")

    def test_leading_false_lock(self):
        # A decoy FSC + a garbage batch BEFORE the real transmission must not block
        # decode: the true batch origin is a LATER lock, not locks[0]. (regression
        # for the lock-iteration fix)
        rng = np.random.default_rng(7)
        decoy = [1, 0] * 32                                # short preamble
        for k in range(31, -1, -1):
            decoy.append((FSC >> k) & 1)                   # an exact, real FSC
        decoy += list(rng.integers(0, 2, 16 * 32))         # 16 garbage codewords
        decoy_iq = _bits_to_iq(np.array(decoy, dtype=np.uint8), 1200, FS, DEVIATION_HZ)
        real = encode_pocsag([{"ric": 424242, "mode": "alpha", "text": "AFTER DECOY"}],
                             1200, FS, preamble_bits=PRE)
        iq = np.concatenate([decoy_iq, real])
        r = decode_pocsag(iq, FS)
        self.assertTrue(r["sync_count"] >= 2)              # decoy + real FSCs both seen
        self.assertEqual(_only(r)["alpha"], "AFTER DECOY")

    def test_tone_only_page(self):
        iq = encode_pocsag([{"ric": 777777, "mode": "tone", "text": ""}],
                           1200, FS, preamble_bits=PRE)
        m = _only(decode_pocsag(iq, FS))
        self.assertEqual(m["type"], "tone")
        self.assertEqual(m["ric"], 777777)

    def test_full_chain_through_iq(self):
        # the real path: 2.4 MHz capture -> decimate ~62x -> fractional sps
        iq = encode_pocsag([{"ric": 1314159, "mode": "alpha", "text": "REALPATH 9"}],
                           2400, 2_400_000.0, preamble_bits=PRE)
        m = _only(decode_pocsag(iq, 2_400_000.0))
        self.assertEqual(m["ric"], 1314159)
        self.assertEqual(m["alpha"], "REALPATH 9")

    def test_noise_no_false_pages(self):
        rng = np.random.default_rng(1)
        noise = (rng.standard_normal(60000) + 1j * rng.standard_normal(60000)).astype(np.complex64)
        self.assertEqual(_msgs(decode_pocsag(noise, FS)), [])

    def test_pure_tone_empty(self):
        t = np.arange(40000) / FS
        tone = np.exp(1j * 2 * np.pi * 3000.0 * t).astype(np.complex64)
        r = decode_pocsag(tone, FS)
        self.assertEqual(_msgs(r), [])
        self.assertIsNone(r["baud"])

    def test_too_short_raises(self):
        with self.assertRaises(ValueError):
            decode_pocsag(np.zeros(8, dtype=np.complex64), FS)

    def test_bch_correction_in_chain(self):
        # corrupt a few discriminator samples; BCH should still recover the text
        iq = encode_pocsag([{"ric": 888888, "mode": "alpha", "text": "ERRORS OK"}],
                           512, FS, preamble_bits=PRE, noise=0.05, seed=3)
        m = _only(decode_pocsag(iq, FS))
        self.assertEqual(m["alpha"], "ERRORS OK")


class TestGoldenVectors(unittest.TestCase):
    """External anchors NOT produced by encode_pocsag — these pin spec conformance
    (the round-trip tests only prove encoder<->decoder self-consistency)."""

    def test_fsc_and_idle_are_valid_codewords(self):
        # The real standard FSC/IDLE are themselves valid BCH(31,21)+parity
        # codewords, which independently pins BCH_POLY=0x769 and the parity rule.
        for word in (FSC, IDLE):
            ok, corrected, ne = bch_check(word)
            self.assertTrue(ok); self.assertEqual(ne, 0); self.assertEqual(corrected, word)

    def test_numeric_glyph_from_raw_bits(self):
        # digit 5 = 0b0101 sent LSB-first -> [1,0,1,0]; digit 1 = 0b0001 -> [1,0,0,0]
        self.assertEqual(pocsag.numeric_decode([1, 0, 1, 0]), "5")
        self.assertEqual(pocsag.numeric_decode([1, 0, 0, 0]), "1")
        self.assertEqual(pocsag.numeric_decode([0, 0, 1, 1]), "")   # 0xC space -> trimmed

    def test_alpha_char_from_raw_bits(self):
        # 'A' = 0x41 = 1000001, LSB-first -> [1,0,0,0,0,0,1]
        self.assertEqual(pocsag.alpha_decode([1, 0, 0, 0, 0, 0, 1]), "A")
        # 'z' = 0x7A = 1111010, LSB-first -> [0,1,0,1,1,1,1]
        self.assertEqual(pocsag.alpha_decode([0, 1, 0, 1, 1, 1, 1]), "z")

    def test_codeword_field_layout(self):
        # hand-built address codeword (no encoder for the field extraction)
        cw = encode_codeword(0, (0x1ABCD << 2) | 0b10)   # addr18=0x1ABCD, function=2
        self.assertEqual((cw >> 31) & 1, 0)              # address flag
        self.assertEqual((cw >> 13) & 0x3FFFF, 0x1ABCD)  # 18 address bits
        self.assertEqual((cw >> 11) & 0x3, 0b10)         # 2 function bits


class TestGate(unittest.TestCase):
    def test_looks_like_pocsag(self):
        self.assertTrue(pocsag.looks_like_pocsag(
            {"modulation_class": "fsk", "fsk_states": 2, "fsk_shift_khz": 9.0,
             "symbol_rate_kbd": 1.2}))
        self.assertFalse(pocsag.looks_like_pocsag(
            {"modulation_class": "psk", "fsk_states": 1, "fsk_shift_khz": 0.0,
             "symbol_rate_kbd": 0.0}))
        self.assertFalse(pocsag.looks_like_pocsag({}))


if __name__ == "__main__":
    unittest.main()
