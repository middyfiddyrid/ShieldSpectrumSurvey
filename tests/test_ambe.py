"""Unit tests for the AMBE / IMBE vocoder frame decoder.

Same discipline as tests/test_pocsag.py: synthesize known frames with the
module's OWN packers (which share the FEC and the scrambler with the unpackers,
so a wrong polynomial or a wrong bit order fails loudly), round-trip them, and
separately pin the pieces that a round-trip CANNOT prove:

  - TestFecStructure enumerates every codeword and every low-weight error pattern
    to assert minimum distance and syndrome-table perfection. That is what proves
    these really are Golay(23,12) / Golay(24,12) / Hamming(15,11) rather than
    some self-consistent code of my own invention.
  - TestPitchTable pins the pitch/harmonic-count formula at both ends of the IMBE
    range against the documented limits (405 Hz / L=9 .. 64.9 Hz / L=56).
  - TestSynthesis asserts the synthesizer's OUTPUT SPECTRUM — an FFT peak on the
    requested fundamental, energy in the right place for voiced vs unvoiced, and
    the right absolute noise level — rather than trusting the arithmetic.
  - TestHonesty asserts the module refuses to emit audio it cannot justify.

All headless, no hardware, no audio fixtures.
"""

import math
import unittest

import numpy as np

from survey import ambe
from survey.ambe import (AMBE_ERR_LIMIT, AMBE_FRAME_BITS, AMBE_INFO_BITS,
                         FRAME_SAMPLES, IMBE_ERR_LIMIT, IMBE_FRAME_BITS,
                         IMBE_INFO_BITS, MbeState, SAMPLE_RATE, bits_to_int,
                         decode_dmr_voice, decode_imbe_params, decode_p25_voice,
                         golay23_decode, golay23_encode, golay24_decode,
                         golay24_encode, hamming15_decode, hamming15_encode,
                         imbe_band_count, imbe_pitch, int_to_bits,
                         pack_ambe_frame, pack_imbe_frame, pack_imbe_params,
                         unpack_ambe_frame, unpack_imbe_frame, voice_params)


def _spectrum(pcm, rate=SAMPLE_RATE):
    """(freqs, magnitude) of a Hann-windowed FFT — the ear on every synthesis test."""
    x = np.asarray(pcm, dtype=np.float64)
    w = np.hanning(x.size)
    mag = np.abs(np.fft.rfft(x * w))
    return np.fft.rfftfreq(x.size, 1.0 / rate), mag


def _peak_hz(pcm, rate=SAMPLE_RATE):
    f, mag = _spectrum(pcm, rate)
    return float(f[int(np.argmax(mag))])


def _band_energy_fraction(pcm, center_hz, half_width_hz=8.0, rate=SAMPLE_RATE):
    f, mag = _spectrum(pcm, rate)
    p = mag ** 2
    sel = np.abs(f - center_hz) <= half_width_hz
    total = float(np.sum(p))
    return float(np.sum(p[sel]) / total) if total > 0 else 0.0


def _synth_run(params, frames=32, seed=0):
    st = MbeState(seed=seed)
    out = [ambe.mbe_synthesize(params, st)[0] for _ in range(frames)]
    return np.concatenate(out)


class TestFecStructure(unittest.TestCase):
    """External anchors: these hold for the REAL named codes and would fail for a
    merely self-consistent one."""

    def test_golay23_minimum_distance_7(self):
        weights = [bin(golay23_encode(d)).count("1") for d in range(1, 4096)]
        self.assertEqual(min(weights), 7)
        self.assertEqual(bin(golay23_encode(0)).count("1"), 0)

    def test_golay24_minimum_distance_8(self):
        weights = [bin(golay24_encode(d)).count("1") for d in range(1, 4096)]
        self.assertEqual(min(weights), 8)
        # extended code is even-weight by construction
        self.assertTrue(all(w % 2 == 0 for w in weights))

    def test_hamming15_minimum_distance_3(self):
        weights = [bin(hamming15_encode(d)).count("1") for d in range(1, 2048)]
        self.assertEqual(min(weights), 3)

    def test_golay23_is_a_perfect_code(self):
        # 1 + 23 + 253 + 1771 = 2048 = 2^11 error patterns of weight <=3, and
        # exactly 2^11 syndromes. A bijection is only possible for the real Golay.
        self.assertEqual(len(ambe._GOLAY23_SYN), 2048)
        self.assertEqual(set(ambe._GOLAY23_SYN.keys()), set(range(2048)))

    def test_hamming15_is_a_perfect_code(self):
        self.assertEqual(len(ambe._HAMMING15_SYN), 16)
        self.assertEqual(set(ambe._HAMMING15_SYN.keys()), set(range(16)))


class TestFecCorrection(unittest.TestCase):
    def test_golay23_clean_roundtrip(self):
        for d in (0, 1, 0x555, 0xABC, 0xFFF):
            cw = golay23_encode(d)
            data, nerr, ok = golay23_decode(cw)
            self.assertTrue(ok); self.assertEqual(nerr, 0); self.assertEqual(data, d)

    def test_golay23_every_single_bit_error(self):
        for d in (0x000, 0x123, 0xFFF):
            cw = golay23_encode(d)
            for b in range(23):
                data, nerr, ok = golay23_decode(cw ^ (1 << b))
                self.assertTrue(ok)
                self.assertEqual(nerr, 1, f"bit {b}")
                self.assertEqual(data, d, f"bit {b}")

    def test_golay23_every_double_and_triple_error(self):
        d = 0x8A5
        cw = golay23_encode(d)
        for b1 in range(23):
            for b2 in range(b1 + 1, 23):
                data, nerr, _ = golay23_decode(cw ^ (1 << b1) ^ (1 << b2))
                self.assertEqual((data, nerr), (d, 2))
                for b3 in range(b2 + 1, 23):
                    data, nerr, _ = golay23_decode(
                        cw ^ (1 << b1) ^ (1 << b2) ^ (1 << b3))
                    self.assertEqual((data, nerr), (d, 3))

    def test_golay24_corrects_up_to_three(self):
        d = 0x6C3
        cw = golay24_encode(d)
        for b1 in range(24):
            data, nerr, ok = golay24_decode(cw ^ (1 << b1))
            self.assertTrue(ok); self.assertEqual((data, nerr), (d, 1))
            for b2 in range(b1 + 1, 24):
                data, nerr, ok = golay24_decode(cw ^ (1 << b1) ^ (1 << b2))
                self.assertTrue(ok); self.assertEqual((data, nerr), (d, 2))

    def test_golay24_detects_four_errors(self):
        # d=8 means weight-4 patterns are detectable but NOT correctable. Every
        # one of them must be refused rather than silently mis-decoded — this is
        # the only genuine per-vector failure signal either codec gives us.
        d = 0x137
        cw = golay24_encode(d)
        rng = np.random.default_rng(0)
        for _ in range(400):
            bits = rng.choice(24, size=4, replace=False)
            bad = cw
            for b in bits:
                bad ^= (1 << int(b))
            _, nerr, ok = golay24_decode(bad)
            self.assertFalse(ok, f"weight-4 pattern {sorted(bits)} not detected")
            self.assertEqual(nerr, 4)

    def test_hamming15_every_single_bit_error(self):
        for d in (0x000, 0x2AA, 0x7FF):
            cw = hamming15_encode(d)
            data, nerr, ok = hamming15_decode(cw)
            self.assertTrue(ok); self.assertEqual((data, nerr), (d, 0))
            for b in range(15):
                data, nerr, ok = hamming15_decode(cw ^ (1 << b))
                self.assertTrue(ok); self.assertEqual((data, nerr), (d, 1))


class TestScrambler(unittest.TestCase):
    def test_pn_is_deterministic_and_seed_dependent(self):
        a = ambe.pn_bits(0x123, 114)
        b = ambe.pn_bits(0x123, 114)
        c = ambe.pn_bits(0x124, 114)
        self.assertEqual(a.size, 114)
        np.testing.assert_array_equal(a, b)
        self.assertTrue(np.any(a != c), "different seeds must give different masks")
        self.assertTrue(set(np.unique(a)) <= {0, 1})

    def test_pn_prefix_property(self):
        # a shorter request is the prefix of a longer one (the LCG is a stream)
        np.testing.assert_array_equal(ambe.pn_bits(0x7AB, 23),
                                      ambe.pn_bits(0x7AB, 114)[:23])


class TestFrameUnpack(unittest.TestCase):
    def setUp(self):
        self.rng = np.random.default_rng(11)

    def test_imbe_frame_roundtrip(self):
        for _ in range(40):
            info = self.rng.integers(0, 2, IMBE_INFO_BITS).astype(np.uint8)
            coded = pack_imbe_frame(info)
            self.assertEqual(coded.size, IMBE_FRAME_BITS)
            r = unpack_imbe_frame(coded)
            self.assertTrue(r["fec_ok"])
            self.assertEqual(r["errors"], 0)
            np.testing.assert_array_equal(r["info_bits"], info)

    def test_imbe_frame_corrects_one_bit_per_vector(self):
        info = self.rng.integers(0, 2, IMBE_INFO_BITS).astype(np.uint8)
        coded = pack_imbe_frame(info)
        # one flip inside five of the coded vectors, including u_0 whose Golay
        # word seeds the scrambler for all the rest (the bare u_7 has no FEC, so
        # leave it alone — a flip there is genuinely uncorrectable)
        hit = coded.copy()
        for (a, _b) in ambe.IMBE_VECTOR_SPANS[:5]:
            hit[a + 3] ^= 1
        r = unpack_imbe_frame(hit)
        self.assertTrue(r["fec_ok"])
        self.assertEqual(r["errors"], 5)
        np.testing.assert_array_equal(r["info_bits"], info)

    def test_error_limit_gates_a_frame_the_fec_still_recovered(self):
        # 7 corrections in 144 bits is ~5% BER. The FEC does recover the payload,
        # and unpack_imbe_frame still hands it back — but fec_ok goes False,
        # because at that error rate the NEXT frame is a coin flip and audio
        # built on it would be confident noise. The gate is a trust threshold,
        # not a correctness threshold, and the distinction is asserted here so
        # nobody "fixes" it by raising the limit.
        info = self.rng.integers(0, 2, IMBE_INFO_BITS).astype(np.uint8)
        hit = pack_imbe_frame(info).copy()
        for (a, _b) in ambe.IMBE_VECTOR_SPANS[:7]:
            hit[a + 3] ^= 1
        r = unpack_imbe_frame(hit)
        np.testing.assert_array_equal(r["info_bits"], info)   # payload recovered
        self.assertEqual(r["errors"], 7)
        self.assertFalse(r["fec_ok"])                          # but not trusted
        self.assertIn("exceeds limit", r["note"])

    def test_imbe_unprotected_vector_is_honest(self):
        # u_7 rides bare by design; a flip there survives into the output and the
        # FEC layer reports zero errors. Documented, not a bug — assert it so
        # nobody later assumes the whole frame is protected.
        info = np.zeros(IMBE_INFO_BITS, dtype=np.uint8)
        coded = pack_imbe_frame(info)
        coded[IMBE_FRAME_BITS - 1] ^= 1
        r = unpack_imbe_frame(coded)
        self.assertEqual(r["errors"], 0)
        self.assertEqual(int(r["info_bits"][IMBE_INFO_BITS - 1]), 1)

    def test_ambe_frame_roundtrip(self):
        for _ in range(40):
            info = self.rng.integers(0, 2, AMBE_INFO_BITS).astype(np.uint8)
            coded = pack_ambe_frame(info)
            self.assertEqual(coded.size, AMBE_FRAME_BITS)
            r = unpack_ambe_frame(coded)
            self.assertTrue(r["fec_ok"])
            self.assertEqual(r["errors"], 0)
            np.testing.assert_array_equal(r["info_bits"], info)

    def test_ambe_frame_corrects_errors(self):
        info = self.rng.integers(0, 2, AMBE_INFO_BITS).astype(np.uint8)
        coded = pack_ambe_frame(info)
        hit = coded.copy()
        hit[2] ^= 1; hit[9] ^= 1          # 2 errors in the extended-Golay u_0
        hit[30] ^= 1                       # 1 error in the Golay(23,12) u_1
        r = unpack_ambe_frame(hit)
        self.assertTrue(r["fec_ok"])
        self.assertEqual(r["errors"], 3)
        np.testing.assert_array_equal(r["info_bits"], info)

    def test_ambe_seed_vector_failure_is_detected(self):
        # u_0 seeds the scrambler, so a bad u_0 poisons the whole frame. The
        # extended Golay is there precisely to catch it.
        info = self.rng.integers(0, 2, AMBE_INFO_BITS).astype(np.uint8)
        coded = pack_ambe_frame(info)
        hit = coded.copy()
        for b in (1, 4, 9, 17):
            hit[b] ^= 1
        r = unpack_ambe_frame(hit)
        self.assertFalse(r["fec_ok"])
        self.assertIn("uncorrectable", r["note"])

    def test_wrong_length_raises(self):
        with self.assertRaises(ValueError):
            unpack_imbe_frame(np.zeros(100, dtype=np.uint8))
        with self.assertRaises(ValueError):
            unpack_ambe_frame(np.zeros(64, dtype=np.uint8))


class TestPitchTable(unittest.TestCase):
    """Anchors the pitch formula against the documented IMBE limits."""

    def test_pitch_endpoints(self):
        w0, f0, L = imbe_pitch(0)
        self.assertAlmostEqual(f0, 16000.0 / 39.5, places=3)   # 405.06 Hz
        self.assertAlmostEqual(f0, 405.063, places=2)
        self.assertEqual(L, 9)
        w0, f0, L = imbe_pitch(207)
        self.assertAlmostEqual(f0, 16000.0 / 246.5, places=3)  # 64.91 Hz
        self.assertAlmostEqual(f0, 64.909, places=2)
        self.assertEqual(L, 56)

    def test_pitch_is_monotonic_and_bounded(self):
        prev = None
        for b0 in range(208):
            w0, f0, L = imbe_pitch(b0)
            self.assertGreater(f0, 0)
            self.assertTrue(9 <= L <= 56)
            self.assertLess(L * w0, math.pi, f"harmonic L alias at b0={b0}")
            if prev is not None:
                self.assertLess(f0, prev)          # higher index = lower pitch
            prev = f0

    def test_band_count(self):
        self.assertEqual(imbe_band_count(9), 3)
        self.assertEqual(imbe_band_count(12), 4)
        self.assertEqual(imbe_band_count(34), 12)
        self.assertEqual(imbe_band_count(56), 12)   # capped: b1 is at most 12 bits


class TestParameterUnpack(unittest.TestCase):
    def test_hand_built_frame(self):
        # 88 bits assembled by hand, no packer involved:
        #   b0 = 00000000        -> index 0  -> 405.06 Hz, L=9, K=3
        #   b1 = 1 0 1           -> bands voiced / unvoiced / voiced
        #   b2 = 101010          -> gain index 42
        #   remaining 71 bits    -> undecoded amplitude bits
        bits = np.array([0] * 8 + [1, 0, 1] + [1, 0, 1, 0, 1, 0] + [0] * 71,
                        dtype=np.uint8)
        self.assertEqual(bits.size, IMBE_INFO_BITS)
        p = decode_imbe_params(bits)
        self.assertEqual(p["b0"], 0)
        self.assertAlmostEqual(p["f0_hz"], 405.063, places=2)
        self.assertEqual(p["L"], 9)
        self.assertEqual(p["K"], 3)
        self.assertEqual(p["voiced_bands"], [True, False, True])
        self.assertEqual(p["gain_index"], 42)
        self.assertEqual(p["amplitude_bits"].size, 71)
        self.assertIsNone(p["magnitudes"])
        self.assertFalse(p["magnitudes_available"])
        # 3 harmonics per band: 1-3 voiced, 4-6 unvoiced, 7-9 voiced
        self.assertEqual([bool(x) for x in p["voiced"][1:]],
                         [True] * 3 + [False] * 3 + [True] * 3)

    def test_field_roundtrip_through_packer(self):
        for b0 in (0, 1, 37, 100, 150, 207):
            _, _, L = imbe_pitch(b0)
            K = imbe_band_count(L)
            flags = [(i % 3) != 1 for i in range(K)]
            bits = pack_imbe_params(b0, flags, gain_index=(b0 % 64))
            p = decode_imbe_params(bits)
            self.assertEqual(p["b0"], b0)
            self.assertEqual(p["L"], L)
            self.assertEqual(p["K"], K)
            self.assertEqual(p["voiced_bands"], flags)
            self.assertEqual(p["gain_index"], b0 % 64)

    def test_params_survive_the_full_fec_chain(self):
        K = imbe_band_count(imbe_pitch(90)[2])
        bits = pack_imbe_params(90, [True] * K, gain_index=17)
        coded = pack_imbe_frame(bits).copy()
        coded[5] ^= 1                       # a real channel bit error
        r = unpack_imbe_frame(coded)
        self.assertTrue(r["fec_ok"])
        self.assertEqual(r["errors"], 1)
        p = decode_imbe_params(r["info_bits"])
        self.assertEqual(p["b0"], 90)
        self.assertEqual(p["gain_index"], 17)
        self.assertEqual(p["voiced_bands"], [True] * K)

    def test_wrong_band_count_rejected(self):
        with self.assertRaises(ValueError):
            pack_imbe_params(90, [True] * 3, gain_index=0)

    def test_reserved_pitch_is_silence_not_a_bogus_buzz(self):
        bits = pack_imbe_params(220, [], 0)
        p = decode_imbe_params(bits)
        self.assertTrue(p["reserved_pitch"])
        self.assertTrue(p["silence"])
        self.assertEqual(p["f0_hz"], 0.0)
        self.assertIn("reserved", p["note"])

    def test_amplitude_bits_are_returned_verbatim(self):
        # the missing dequantizer's input must survive intact so it can be bolted on
        rng = np.random.default_rng(5)
        _, _, L = imbe_pitch(64)
        K = imbe_band_count(L)
        n_amp = IMBE_INFO_BITS - 8 - K - 6
        amp = rng.integers(0, 2, n_amp).astype(np.uint8)
        bits = pack_imbe_params(64, [True] * K, 9, amplitude_bits=amp)
        p = decode_imbe_params(bits)
        np.testing.assert_array_equal(p["amplitude_bits"], amp)


class TestSynthesis(unittest.TestCase):
    def test_voiced_fundamental_lands_on_the_pitch(self):
        for f0 in (100.0, 150.0, 200.0, 312.5, 400.0):
            mags = [1.0 / l for l in range(1, 10)]
            p = voice_params(f0, L=9, voiced=True, magnitudes=mags)
            pcm = _synth_run(p, frames=32)
            peak = _peak_hz(pcm)
            self.assertLess(abs(peak - f0), 8.0, f"f0={f0} peaked at {peak}")

    def test_voiced_harmonics_are_where_they_should_be(self):
        f0 = 220.0
        p = voice_params(f0, L=6, voiced=True, magnitudes=[1.0] * 6)
        pcm = _synth_run(p, frames=40)
        f, mag = _spectrum(pcm)
        for l in range(1, 7):
            sel = np.abs(f - l * f0) <= 8.0
            others = np.abs(f - (l * f0 + 0.5 * f0)) <= 8.0   # between harmonics
            self.assertGreater(float(np.max(mag[sel])),
                               10.0 * float(np.max(mag[others])),
                               f"harmonic {l} not dominant")

    def test_pitch_from_a_decoded_frame(self):
        # the parameter path and the synthesis path agree end to end
        for b0 in (10, 80, 160):
            _, f0, L = imbe_pitch(b0)
            p = voice_params(f0, L=L, voiced=True,
                             magnitudes=[1.0 / l for l in range(1, L + 1)])
            pcm = _synth_run(p, frames=48)
            self.assertLess(abs(_peak_hz(pcm) - f0), 6.0, f"b0={b0}")

    def test_phase_is_continuous_across_frames(self):
        # A steady single harmonic must come out as a near-pure tone. A phase
        # reset at each 20 ms seam would splatter sidebands at +/-50 Hz, so
        # spectral purity IS the continuity test.
        p = voice_params(400.0, L=1, voiced=True, magnitudes=[0.5])
        pcm = _synth_run(p, frames=40)
        self.assertGreater(_band_energy_fraction(pcm, 400.0, 10.0), 0.99)
        self.assertLess(_band_energy_fraction(pcm, 350.0, 10.0), 1e-4)

    def test_unvoiced_is_noise_not_a_harmonic_line(self):
        f0 = 200.0
        mags = [1.0 / l for l in range(1, 10)]
        voiced = voice_params(f0, L=9, voiced=True, magnitudes=mags)
        unvoiced = voice_params(f0, L=9, voiced=False, magnitudes=mags)
        v_pcm = _synth_run(voiced, frames=32, seed=3)
        u_pcm = _synth_run(unvoiced, frames=32, seed=3)
        self.assertGreater(_band_energy_fraction(v_pcm, f0), 0.5)
        self.assertLess(_band_energy_fraction(u_pcm, f0), 0.2)
        self.assertGreater(float(np.sqrt(np.mean(u_pcm ** 2))), 0.01)

    def test_unvoiced_level_matches_the_voiced_convention(self):
        # An unvoiced band must carry the same power a voiced sinusoid of the same
        # magnitude would, or voicing flips would audibly jump in loudness.
        a, L = 0.1, 9
        p = voice_params(200.0, L=L, voiced=False, magnitudes=a)
        pcm = _synth_run(p, frames=64, seed=7)[FRAME_SAMPLES:]   # skip OLA warm-up
        expected = math.sqrt(L * a * a / 2.0)
        rms = float(np.sqrt(np.mean(pcm ** 2)))
        self.assertGreater(rms, 0.75 * expected)
        self.assertLess(rms, 1.30 * expected)

    def test_mixed_voicing(self):
        f0 = 250.0
        p = voice_params(f0, L=6, voiced=[True, True, False, False, False, False],
                         magnitudes=[0.30, 0.15, 0.15, 0.15, 0.15, 0.15])
        pcm = _synth_run(p, frames=32, seed=1)
        self.assertLess(abs(_peak_hz(pcm) - f0), 8.0)   # the voiced line still wins
        self.assertGreater(float(np.sqrt(np.mean(pcm ** 2))), 0.1)

    def test_output_contract(self):
        p = voice_params(200.0, L=9, voiced=True, magnitudes=0.05)
        st = MbeState()
        pcm, info = ambe.mbe_synthesize(p, st)
        self.assertEqual(pcm.dtype, np.float32)
        self.assertEqual(pcm.size, FRAME_SAMPLES)
        self.assertLessEqual(float(np.max(np.abs(pcm))), 1.0)
        self.assertEqual(info["clipped"], 0)

    def test_clipping_is_reported_not_hidden(self):
        p = voice_params(200.0, L=9, voiced=True, magnitudes=1.0)
        pcm, info = ambe.mbe_synthesize(p, MbeState())
        self.assertGreater(info["clipped"], 0)
        self.assertLessEqual(float(np.max(np.abs(pcm))), 1.0)

    def test_silence_params_give_silence(self):
        pcm, _ = ambe.mbe_synthesize({"silence": True}, MbeState())
        np.testing.assert_array_equal(pcm, np.zeros(FRAME_SAMPLES, dtype=np.float32))

    def test_no_magnitudes_gives_silence(self):
        # decode_imbe_params always returns magnitudes=None today; the synthesizer
        # must treat that as silence rather than as unit gain.
        p = voice_params(200.0, L=9, voiced=True)
        self.assertIsNone(p["magnitudes"])
        pcm, _ = ambe.mbe_synthesize(p, MbeState())
        np.testing.assert_array_equal(pcm, np.zeros(FRAME_SAMPLES, dtype=np.float32))


class TestP25Stream(unittest.TestCase):
    def _frame(self, b0=100, gain=32, voiced=True):
        _, _, L = imbe_pitch(b0)
        K = imbe_band_count(L)
        bits = pack_imbe_params(b0, [voiced] * K, gain)
        return pack_imbe_frame(bits)

    def test_clean_frames_report_pitch_but_stay_silent_by_default(self):
        frames = [self._frame(b0=100)] * 5
        pcm, reports = decode_p25_voice(frames)
        self.assertEqual(pcm.size, 5 * FRAME_SAMPLES)
        np.testing.assert_array_equal(pcm, np.zeros(pcm.size, dtype=np.float32))
        for r in reports:
            self.assertTrue(r["fec_ok"])
            self.assertTrue(r["params_available"])
            self.assertFalse(r["magnitudes_available"])
            self.assertFalse(r["intelligible"])
            self.assertTrue(r["silent"])
            self.assertAlmostEqual(r["f0_hz"], 16000.0 / 139.5, places=3)
            self.assertIn("dequantizer", r["note"])

    def test_pre_decoded_information_bits_accepted(self):
        bits = pack_imbe_params(64, [True] * imbe_band_count(imbe_pitch(64)[2]), 5)
        pcm, reports = decode_p25_voice([bits])
        self.assertEqual(pcm.size, FRAME_SAMPLES)
        self.assertTrue(reports[0]["fec_ok"])
        self.assertEqual(reports[0]["b0"], 64)

    def test_synthetic_envelope_is_opt_in_and_labelled(self):
        b0 = 100
        _, f0, _ = imbe_pitch(b0)
        frames = [self._frame(b0=b0, gain=20)] * 40
        pcm, reports = decode_p25_voice(frames, envelope="synthetic")
        self.assertGreater(float(np.sqrt(np.mean(pcm ** 2))), 0.01)
        self.assertLess(abs(_peak_hz(pcm) - f0), 8.0)
        for r in reports:
            self.assertTrue(r["magnitudes_available"])
            self.assertFalse(r["intelligible"])     # never, until the tables exist
            self.assertEqual(r["envelope"], "synthetic")
            self.assertIn("NOT speech", r["note"])

    def test_bad_envelope_rejected(self):
        with self.assertRaises(ValueError):
            decode_p25_voice([self._frame()], envelope="real")

    def test_frame_count_is_preserved(self):
        frames = [self._frame(), np.zeros(IMBE_FRAME_BITS, dtype=np.uint8),
                  np.zeros(7, dtype=np.uint8)]
        pcm, reports = decode_p25_voice(frames, envelope="synthetic")
        self.assertEqual(len(reports), 3)
        self.assertEqual(pcm.size, 3 * FRAME_SAMPLES)
        self.assertFalse(reports[2]["fec_ok"])
        self.assertIn("bits", reports[2]["note"])

    def test_empty_input(self):
        pcm, reports = decode_p25_voice([])
        self.assertEqual(pcm.size, 0)
        self.assertEqual(reports, [])

    def test_err_limit_loosens_as_well_as_tightens(self):
        # The gate is a judgement call, so it has to be tunable in BOTH
        # directions — an err_limit that could only ever tighten would be a trap
        # for whoever tries to raise it for a noisy site survey.
        _, _, L = imbe_pitch(100)
        frame = pack_imbe_frame(pack_imbe_params(100, [True] * imbe_band_count(L), 30))
        hit = frame.copy()
        for (a, _b) in ambe.IMBE_VECTOR_SPANS[:7]:
            hit[a + 3] ^= 1                       # 7 corrections, default limit 6
        _, tight = decode_p25_voice([hit])
        self.assertFalse(tight[0]["fec_ok"])
        _, loose = decode_p25_voice([hit], err_limit=8)
        self.assertTrue(loose[0]["fec_ok"])
        self.assertEqual(loose[0]["b0"], 100)
        _, strict = decode_p25_voice([frame], err_limit=0)
        self.assertTrue(strict[0]["fec_ok"])       # a clean frame needs 0 anyway


class TestHonesty(unittest.TestCase):
    """The module must never emit audio it cannot justify."""

    def test_failed_fec_frame_is_silence_plus_flag(self):
        info = np.zeros(IMBE_INFO_BITS, dtype=np.uint8)
        coded = pack_imbe_frame(info).copy()
        # 4 flips inside a Golay(23,12) vector ALWAYS alias to a weight-3
        # "correction" (min nonzero Golay weight is 7 = 4+3), so this is a
        # deterministic way to burn the error budget, not a lucky corruption.
        for (a, _b) in ambe.IMBE_VECTOR_SPANS[:4]:
            for k in range(4):
                coded[a + k] ^= 1
        pcm, reports = decode_p25_voice([coded], envelope="synthetic")
        self.assertFalse(reports[0]["fec_ok"])
        self.assertGreater(reports[0]["errors"], IMBE_ERR_LIMIT)
        self.assertTrue(reports[0]["silent"])
        self.assertFalse(reports[0]["intelligible"])
        np.testing.assert_array_equal(pcm, np.zeros(FRAME_SAMPLES, dtype=np.float32))

    def test_random_bits_never_decode_as_voice(self):
        rng = np.random.default_rng(2)
        frames = [rng.integers(0, 2, IMBE_FRAME_BITS).astype(np.uint8)
                  for _ in range(200)]
        _, reports = decode_p25_voice(frames, envelope="synthetic")
        accepted = sum(1 for r in reports if r["fec_ok"])
        self.assertEqual(accepted, 0, f"{accepted}/200 noise frames passed the gate")

    def test_muted_frame_decays_instead_of_clicking(self):
        # A mute is allowed to let the PREVIOUS frame's harmonics ramp down —
        # that is audio already emitted, not audio invented — but it must end at
        # silence and carry far less energy than a live frame.
        _, _, L = imbe_pitch(100)
        good = pack_imbe_frame(pack_imbe_params(100, [True] * imbe_band_count(L), 60))
        bad = good.copy()
        for (a, _b) in ambe.IMBE_VECTOR_SPANS[:4]:
            for k in range(4):
                bad[a + k] ^= 1
        pcm, reports = decode_p25_voice([good, good, bad, bad], envelope="synthetic")
        self.assertTrue(reports[1]["fec_ok"])
        self.assertFalse(reports[2]["fec_ok"])
        seg = [float(np.sum(pcm[i * FRAME_SAMPLES:(i + 1) * FRAME_SAMPLES] ** 2))
               for i in range(4)]
        self.assertGreater(seg[1], 0.0)
        self.assertLess(seg[2], 0.6 * seg[1])          # ramping down, not sustaining
        self.assertEqual(seg[3], 0.0)                  # the decay is finite
        np.testing.assert_array_equal(pcm[3 * FRAME_SAMPLES:],
                                      np.zeros(FRAME_SAMPLES, dtype=np.float32))

    def test_dmr_is_fec_only_and_says_so(self):
        rng = np.random.default_rng(4)
        info = rng.integers(0, 2, AMBE_INFO_BITS).astype(np.uint8)
        coded = pack_ambe_frame(info)
        pcm, reports = decode_dmr_voice([coded] * 3)
        self.assertEqual(pcm.size, 3 * FRAME_SAMPLES)
        np.testing.assert_array_equal(pcm, np.zeros(pcm.size, dtype=np.float32))
        for r in reports:
            self.assertTrue(r["fec_ok"])
            self.assertFalse(r["params_available"])
            self.assertFalse(r["intelligible"])
            self.assertIn("not implemented", r["note"])
            np.testing.assert_array_equal(r["info_bits"], info)

    def test_dmr_rejects_most_noise(self):
        # DMR's 72-bit frame carries far less redundancy than P25's 144-bit one
        # (one extended Golay + one Golay vs four Golay + three Hamming), so its
        # noise rejection is genuinely weaker. Measured over 12 seeds x 200
        # random frames: P25 accepts 0, DMR accepts 1.5-3.5%. Asserting the real
        # number rather than a comfortable one — never trust a lone DMR frame,
        # gate on a run of them.
        rng = np.random.default_rng(6)
        frames = [rng.integers(0, 2, AMBE_FRAME_BITS).astype(np.uint8)
                  for _ in range(200)]
        _, reports = decode_dmr_voice(frames)
        accepted = sum(1 for r in reports if r["fec_ok"])
        self.assertLess(accepted, 30, f"{accepted}/200 noise frames passed the gate")

    def test_capability_statement_admits_the_gap(self):
        cap = ambe.capability()
        self.assertEqual(cap["sample_rate"], 8000)
        self.assertIn("synthetic", cap["validated_against"])
        self.assertFalse(cap["protocols"]["p25_phase1_imbe"]["magnitudes"])
        self.assertFalse(cap["protocols"]["dmr_ambe2"]["audio"])
        self.assertIn("not implemented", cap["caveat"])

    def test_module_docstring_states_the_limits(self):
        # The docstring is the contract other engineers read before wiring this
        # into anything an operator hears. Pin the disclosures so a future edit
        # cannot quietly delete them.
        doc = ambe.__doc__ or ""
        self.assertIn("off-air", doc)
        self.assertIn("NOT** IMPLEMENTED", doc)
        self.assertIn("DEQUANTIZER", doc)
        self.assertIn("cannot produce", doc)
        self.assertIn("intelligible speech", doc)
        self.assertIn("CONSEQUENCE, STATED PLAINLY", doc)


class TestBitHelpers(unittest.TestCase):
    def test_bits_int_roundtrip(self):
        for v, n in ((0, 8), (255, 8), (0xABC, 12), (1, 23), (0x7FFFFF, 23)):
            self.assertEqual(bits_to_int(int_to_bits(v, n)), v)

    def test_msb_first_ordering(self):
        np.testing.assert_array_equal(int_to_bits(0b1011, 4),
                                      np.array([1, 0, 1, 1], dtype=np.uint8))
        self.assertEqual(bits_to_int([1, 0, 0, 0]), 8)


if __name__ == "__main__":
    unittest.main()
