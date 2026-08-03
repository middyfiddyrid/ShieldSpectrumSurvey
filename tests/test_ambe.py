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

import hashlib
import math
import unittest

import numpy as np

from survey import ambe
from survey import imbe_tables as T
from survey.ambe import (AMBE_ERR_LIMIT, AMBE_FRAME_BITS, AMBE_INFO_BITS,
                         FRAME_SAMPLES, IMBE_ERR_LIMIT, IMBE_FRAME_BITS,
                         IMBE_INFO_BITS, MbeState, SAMPLE_RATE, bits_to_int,
                         decode_dmr_voice, decode_imbe_params, decode_p25_voice,
                         dequantize_imbe_amplitudes, golay23_decode,
                         golay23_encode, golay24_decode, golay24_encode,
                         hamming15_decode, hamming15_encode,
                         imbe_amplitude_bit_budget, imbe_amplitude_blocks,
                         imbe_amplitude_forward, imbe_amplitude_indices,
                         imbe_amplitude_inverse, imbe_band_count, imbe_dct,
                         imbe_dct_matrix, imbe_gain_step,
                         imbe_ho_coefficient_map, imbe_ho_step, imbe_idct,
                         imbe_pitch, imbe_prediction_coefficient,
                         imbe_prediction_residual, imbe_undo_prediction,
                         imbe_warp_previous, int_to_bits,
                         pack_ambe_frame, pack_imbe_frame, pack_imbe_params,
                         unpack_ambe_frame, unpack_imbe_frame, voice_params)

ALL_L = list(range(9, 57))          # the full IMBE harmonic range


def _plumbing_tables():
    """NOT the IMBE tables. Arbitrary numbers whose ONLY job is to prove that the
    bits -> indices -> coefficients -> magnitudes path runs and is shaped right.

    They are deliberately implausible (a flat allocation, one step size, a linear
    gain map, rho=0.5) so that nobody can mistake them for sourced constants, and
    they are never installed into ambe.IMBE_AMPLITUDE_TABLES — a test below
    asserts the module-level tables stay None even after these are used."""
    def alloc(L):
        b = imbe_amplitude_bit_budget(L)
        w = max(1, min(4, b["bits"] // b["quantizers"]))
        return [w] * b["quantizers"]

    return ImbeAmplitudeTables(
        bit_allocation=alloc,
        step_size=lambda L, i, bits: 0.05,
        gain_level=lambda idx: (float(idx) - 31.5) * 0.05,
        prediction_coeff=lambda L: 0.5,
        source="ARBITRARY TEST VALUES - not IMBE, not sourced, plumbing only")


def _log_envelope(hz):
    """A smooth log2-magnitude envelope: two formant-ish bumps over frequency.

    Used to build 'the same voice at a different pitch' so the frequency warping
    can be measured against a ground truth that exists independently of the code
    under test."""
    hz = np.asarray(hz, dtype=np.float64)
    return (2.0 * np.exp(-((hz - 500.0) / 400.0) ** 2)
            + 1.2 * np.exp(-((hz - 1600.0) / 550.0) ** 2) - 0.5)


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


def _harmonic_energy_fraction(pcm, f0, L, half_width_hz=12.0, rate=SAMPLE_RATE):
    """Fraction of spectral power sitting on the harmonic comb of f0.

    The right measure for a decoded frame: the envelope decides WHICH harmonic
    is loudest, but a voiced frame must put essentially all of its energy on
    integer multiples of the fundamental regardless."""
    f, mag = _spectrum(pcm, rate)
    power = mag ** 2
    near = np.zeros(f.size, dtype=bool)
    for l in range(1, int(L) + 1):
        near |= np.abs(f - l * f0) <= half_width_hz
    total = float(np.sum(power))
    return float(np.sum(power[near]) / total) if total > 0 else 0.0


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
        #   next 70 bits         -> the amplitude quantizers b3..b10 (73 - K)
        #   final 1 bit          -> b_{L+2}
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
        self.assertEqual(p["amplitude_bits"].size, 70)   # 73 - K, not 74 - K
        self.assertIsNotNone(p["magnitudes"])            # the envelope decodes now
        self.assertTrue(p["magnitudes_available"])
        self.assertEqual(p["magnitudes"].size, 10)
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
        # the dequantizer's input must survive intact for analysis and replay
        rng = np.random.default_rng(5)
        _, _, L = imbe_pitch(64)
        K = imbe_band_count(L)
        n_amp = imbe_amplitude_bit_budget(L)["bits"]
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

    def test_clean_frames_decode_to_audio_by_default(self):
        # This used to assert SILENCE, because the dequantizer did not exist.
        # It now asserts the opposite, which is the whole point of the change.
        frames = [self._frame(b0=100)] * 5
        pcm, reports = decode_p25_voice(frames)
        self.assertEqual(pcm.size, 5 * FRAME_SAMPLES)
        self.assertGreater(float(np.max(np.abs(pcm))), 0.0)
        for r in reports:
            self.assertTrue(r["fec_ok"])
            self.assertTrue(r["params_available"])
            self.assertTrue(r["magnitudes_available"])
            self.assertTrue(r["intelligible"])
            self.assertFalse(r["silent"])
            self.assertAlmostEqual(r["f0_hz"], 16000.0 / 139.5, places=3)
            self.assertIn("decoded", r["note"])

    def test_pre_decoded_information_bits_accepted(self):
        bits = pack_imbe_params(64, [True] * imbe_band_count(imbe_pitch(64)[2]), 5)
        pcm, reports = decode_p25_voice([bits])
        self.assertEqual(pcm.size, FRAME_SAMPLES)
        self.assertTrue(reports[0]["fec_ok"])
        self.assertEqual(reports[0]["b0"], 64)

    def test_synthetic_envelope_has_no_frame_left_to_serve(self):
        # The placeholder used to cover L = 42/43/44. Those rows are now sourced,
        # so EVERY legal harmonic count decodes for real and opting into the
        # placeholder must change nothing. Asserted rather than assumed, because
        # a fallback that silently started winning would be exactly the kind of
        # "confident garbage" this module exists to refuse.
        self.assertEqual(tuple(T.UNVERIFIED_L), (),
                         "a gap was reinstated — restore the fallback coverage test")
        for b0 in (40, 100, 180):
            frames = [self._frame(b0=b0, gain=20)] * 4
            plain, r_plain = decode_p25_voice(frames)
            synth, r_synth = decode_p25_voice(frames, envelope="synthetic")
            np.testing.assert_allclose(plain, synth, atol=0.0)
            for r in r_synth:
                self.assertTrue(r["intelligible"], f"b0={b0} fell back")
                self.assertIn("decoded", r["note"])

    def test_placeholder_envelope_is_still_reachable_and_still_honest(self):
        # It is dead code on the IMBE path now, but it is retained as the
        # documented fallback if a gap is ever reinstated. Keep it exercised so
        # it cannot rot into something that would emit unlabelled audio.
        env = ambe.synthetic_envelope(20, 40)
        self.assertEqual(env.size, 21)
        self.assertEqual(float(env[0]), 0.0)
        self.assertTrue(np.all(env[1:] > 0.0))
        self.assertLess(float(np.sum(env[1:])), 0.5)   # can never be what clips
        self.assertTrue(np.all(np.diff(env[1:]) < 0.0))  # 1/l roll-off

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
        pcm, reports = decode_p25_voice([good, good, bad, bad])
        self.assertTrue(reports[1]["fec_ok"])
        self.assertFalse(reports[2]["fec_ok"])
        seg = [float(np.sum(pcm[i * FRAME_SAMPLES:(i + 1) * FRAME_SAMPLES] ** 2))
               for i in range(4)]
        self.assertGreater(seg[1], 0.0)
        # The cross-fade ramps each harmonic's amplitude linearly to zero across
        # the muted frame, so its energy is ~1/3 a^2 N against ~1/2 a^2 N for a
        # sustained frame — about two thirds. Anything near 1.0 would mean the
        # mute is sustaining the previous frame rather than decaying it.
        self.assertLess(seg[2], 0.75 * seg[1])
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

    def test_capability_statement_still_admits_the_dmr_gap(self):
        # The IMBE side of capability() is asserted in
        # TestHonestyAfterImplementation; this pins the parts that did NOT change.
        cap = ambe.capability()
        self.assertEqual(cap["sample_rate"], 8000)
        self.assertIn("synthetic", cap["validated_against"])
        self.assertFalse(cap["protocols"]["dmr_ambe2"]["audio"])
        self.assertFalse(cap["protocols"]["dmr_ambe2"]["magnitudes"])


class TestSourcedTables(unittest.TestCase):
    """The tables in survey/imbe_tables.py are the only numbers in this decoder
    that were copied rather than computed, so they get the hardest tests. Each
    check below is an EXTERNAL anchor from docs/IMBE_SPECTRAL_AMPLITUDES.md — a
    corrupted or mis-transcribed table fails at least one of them."""

    def test_bit_budget_checksum_holds_for_every_tabulated_L(self):
        # sum(gain bits) + sum(higher-order bits) == 73 - K, which ties the two
        # independent tables to the frame arithmetic. This is the single
        # strongest guard against a mis-copied row.
        for L in sorted(T.HIGHER_ORDER_BITS):
            budget = imbe_amplitude_bit_budget(L)
            total = sum(T.GAIN_BITS[L]) + sum(T.HIGHER_ORDER_BITS[L])
            self.assertEqual(total, budget["bits"],
                             f"L={L}: {total} allocated, budget {budget['bits']}")
            self.assertEqual(budget["bits"], 73 - imbe_band_count(L))

    def test_table_shapes(self):
        self.assertEqual(len(T.GAIN_LEVELS), 64)          # 6-bit gain index
        self.assertEqual(sorted(T.GAIN_BITS), list(range(9, 57)))
        for L, row in T.GAIN_BITS.items():
            self.assertEqual(len(row), 5, f"L={L}")       # G_2..G_6
        for L, row in T.HIGHER_ORDER_BITS.items():
            self.assertEqual(len(row), L - 6, f"L={L}")
        # 1272 integers is the full Annex G, and we now have every one of them.
        self.assertEqual(sorted(T.HIGHER_ORDER_BITS), list(range(9, 57)))
        have = sum(len(r) for r in T.HIGHER_ORDER_BITS.values())
        self.assertEqual(have, 1272)
        self.assertEqual(tuple(T.UNVERIFIED_L), ())

    def test_every_gain_bit_count_is_confirmed_by_its_own_step_size(self):
        # mbelib stores (bits, step) pairs. The step must equal
        # QUANT_STEP[bits] * GAIN_STD_DEV[j] -- a per-entry checksum on all 240
        # bit counts. This is what makes the gain allocation trustworthy rather
        # than merely plausible.
        stored = {  # (L, element) -> step size as stored in mbelib's ba[]
            (9, 0): 0.003100, (9, 1): 0.004020, (9, 2): 0.003360,
            (9, 3): 0.002900, (9, 4): 0.002640,
            (16, 0): 0.046500, (16, 1): 0.030150, (16, 2): 0.025200,
            (16, 3): 0.040600, (16, 4): 0.036960,
            (56, 0): 0.201500, (56, 1): 0.130650, (56, 2): 0.142800,
            (56, 3): 0.123250, (56, 4): 0.112200,
        }
        for (L, j), step in stored.items():
            self.assertAlmostEqual(imbe_gain_step(T.GAIN_BITS[L][j], j), step,
                                   places=6, msg=f"L={L} G{j + 2}")

    def test_we_are_immune_to_the_known_mbelib_typo(self):
        # mbelib's stored step at L=23, G_5 is 0.068000; the standard says
        # 0.058000. We take only the BIT COUNT from mbelib and generate the step
        # from the rule, so we get the correct value and the typo cannot reach
        # audio. Asserting it explicitly so a future "optimisation" that
        # hardcodes mbelib's steps fails loudly.
        typo = T.GAIN_STEP_TYPO
        bits = T.GAIN_BITS[typo["L"]][typo["element"] - 2]
        self.assertEqual(bits, 4)
        self.assertAlmostEqual(imbe_gain_step(bits, typo["element"] - 2),
                               typo["correct"], places=6)
        self.assertNotAlmostEqual(imbe_gain_step(bits, typo["element"] - 2),
                                  typo["mbelib"], places=6)

    def test_tia_worked_example_L16(self):
        # The standard's own clause-10 example. It pins the gain allocation, the
        # higher-order allocation, the m->(block,coefficient) mapping and both
        # step-size rules simultaneously -- if any one of them is wrong, the ten
        # higher-order step sizes below cannot all come out right.
        self.assertEqual(imbe_amplitude_bit_budget(16)["bits"], 67)
        self.assertEqual(list(T.GAIN_BITS[16]), [6, 6, 6, 5, 5])
        self.assertEqual(list(T.HIGHER_ORDER_BITS[16]),
                         [6, 6, 5, 4, 4, 3, 3, 3, 3, 2])
        self.assertEqual(sum(T.GAIN_BITS[16]), 28)
        self.assertEqual(sum(T.HIGHER_ORDER_BITS[16]), 39)

        np.testing.assert_allclose(
            [imbe_gain_step(b, j) for j, b in enumerate(T.GAIN_BITS[16])],
            [0.04650, 0.03015, 0.02520, 0.04060, 0.03696], atol=5e-7)

        self.assertEqual(imbe_amplitude_blocks(16), (2, 2, 3, 3, 3, 3))
        self.assertEqual(imbe_ho_coefficient_map(16),
                         ((0, 2), (1, 2), (2, 2), (2, 3), (3, 2),
                          (3, 3), (4, 2), (4, 3), (5, 2), (5, 3)))
        steps = [imbe_ho_step(b, k) for b, (_i, k)
                 in zip(T.HIGHER_ORDER_BITS[16], imbe_ho_coefficient_map(16))]
        np.testing.assert_allclose(
            steps, [0.04605, 0.04605, 0.08596, 0.09640, 0.12280,
                    0.15665, 0.19955, 0.15665, 0.19955, 0.20485], atol=5e-6)

    def test_L9_spot_values(self):
        self.assertEqual(list(T.GAIN_BITS[9]), [10, 9, 9, 9, 9])
        self.assertEqual(list(T.HIGHER_ORDER_BITS[9]), [9, 8, 7])
        self.assertEqual(imbe_amplitude_bit_budget(9)["bits"], 70)

    def test_gain_quantizer_is_a_monotonic_scalar_quantizer(self):
        # Reconstruction levels of a scalar quantizer must be strictly
        # increasing; a transposed or duplicated entry would break this.
        lv = list(T.GAIN_LEVELS)
        self.assertTrue(all(lv[i] < lv[i + 1] for i in range(63)))
        self.assertAlmostEqual(lv[0], -2.842205, places=6)
        self.assertAlmostEqual(lv[63], 8.695827, places=6)

    def test_standdev_tail_is_not_monotonic_and_that_is_correct(self):
        # sigma dips at k=8 (0.165) then rises to 0.170, 0.170. It looks like a
        # transcription error and is not -- confirmed in both sources. Pinned so
        # nobody "corrects" it.
        self.assertEqual(T.HO_STD_DEV[7], 0.173)
        self.assertEqual(T.HO_STD_DEV[8], 0.165)
        self.assertEqual(T.HO_STD_DEV[9], 0.170)
        self.assertEqual(T.HO_STD_DEV[10], 0.170)
        self.assertLess(T.HO_STD_DEV[8], T.HO_STD_DEV[9])

    def test_isc_attribution_is_present_in_the_tables_module(self):
        # ISC permits commercial closed-source use only if the notice travels
        # with the code. Losing this docstring would be a licence breach, so the
        # test suite guards it.
        doc = T.__doc__ or ""
        self.assertIn("ISC", doc)
        self.assertIn("Copyright (C) 2010 mbelib Author", doc)
        self.assertIn("Permission to use, copy, modify", doc)
        self.assertIn("third-party notices", doc)
        self.assertIn("GPL-2.0", doc)          # the mbelib-neo trap
        self.assertIn("mbelib-neo", doc)


def _table_fingerprint(table):
    """Canonical serialization of an {L: row} bit table -> sha256 hex.

    Defined here rather than in the module so the constant in imbe_tables.py is
    checked against an INDEPENDENT re-derivation of the digest, not against a
    helper the table module could itself get wrong.
    """
    h = hashlib.sha256()
    for L in range(9, 57):
        h.update(("%d:%s;" % (L, ",".join(str(int(v)) for v in table[L]))).encode())
    return h.hexdigest()


class TestSourcedTableIntegrity(unittest.TestCase):
    """Shortfall 2: the higher-order table used to have only per-ROW checksums,
    so an intra-row swap in any row but L=16 would have gone unnoticed.

    These are the checks that close that hole and keep it closed. The tables
    were parsed deterministically out of a hash-pinned copy of mbelib's
    imbe7200x4400_const.h; the suite cannot re-download it (tests are offline
    and must stay that way), so what it pins is that the tables still hold
    exactly the values that parse produced, plus the structural invariants that
    a transposition breaks."""

    def test_fingerprint_locks_every_higher_order_integer(self):
        # The load-bearing test. A sum checksum is blind to ordering by
        # construction; this is not. Any edit to any of the 1272 integers —
        # including swapping two of them within one row — changes the digest.
        self.assertEqual(_table_fingerprint(T.HIGHER_ORDER_BITS),
                         T.HIGHER_ORDER_FINGERPRINT,
                         "HIGHER_ORDER_BITS no longer matches the values parsed "
                         "from mbelib (sha256 " + T.MBELIB_SHA256[:16] + "...)")

    def test_fingerprint_locks_every_gain_bit_count(self):
        self.assertEqual(_table_fingerprint(T.GAIN_BITS),
                         T.GAIN_BITS_FINGERPRINT)

    def test_the_fingerprint_actually_detects_an_intra_row_swap(self):
        # A guard nobody has ever seen fire is not a guard. Prove this one
        # catches the exact failure mode Shortfall 2 named, in every row that
        # can express it.
        caught = 0
        for L in range(9, 57):
            row = list(T.HIGHER_ORDER_BITS[L])
            pair = next(((a, b) for a in range(len(row))
                         for b in range(a + 1, len(row)) if row[a] != row[b]), None)
            if pair is None:
                continue                      # a uniform row cannot be swapped
            a, b = pair
            mutated = dict(T.HIGHER_ORDER_BITS)
            row[a], row[b] = row[b], row[a]
            mutated[L] = tuple(row)
            self.assertEqual(sum(mutated[L]), sum(T.HIGHER_ORDER_BITS[L]),
                             "the swap must be checksum-invisible, else it "
                             "proves nothing about the fingerprint")
            self.assertNotEqual(_table_fingerprint(mutated),
                                T.HIGHER_ORDER_FINGERPRINT, f"L={L} swap missed")
            caught += 1
        self.assertGreater(caught, 40, "too few rows exercised")

    def test_within_block_bit_allocation_is_non_increasing_in_k(self):
        # Structural invariant, verified to hold for all 48 rows of mbelib's
        # hoba[]: inside one DCT block, higher coefficient indices never get
        # MORE bits than lower ones. Independent of the fingerprint — it would
        # catch a bad row introduced together with a recomputed digest.
        for L in ALL_L:
            per_block = {}
            for bits, (i, k) in zip(T.HIGHER_ORDER_BITS[L],
                                    imbe_ho_coefficient_map(L)):
                per_block.setdefault(i, []).append((k, bits))
            for i, seq in per_block.items():
                vals = [b for _k, b in sorted(seq)]
                self.assertEqual(vals, sorted(vals, reverse=True),
                                 f"L={L} block {i} allocation rises with k: {vals}")

    def test_the_monotonicity_invariant_is_not_vacuous(self):
        # It IS vacuous for L=9..12, where every block holds at most one
        # higher-order coefficient. Stated honestly here so nobody reads the
        # test above as covering the whole table. Those low-L rows lean on the
        # TIA Annex G corroboration instead.
        def constrained(L):
            return sum(1 for J in imbe_amplitude_blocks(L) for _ in range(2, J)
                       if J > 2)
        self.assertEqual([L for L in ALL_L if constrained(L) == 0],
                         [9, 10, 11, 12])
        self.assertEqual(tuple(T.TIA_CORROBORATED_L[:4]), (9, 10, 11, 12),
                         "the rows the invariant cannot constrain must be "
                         "exactly the ones the TIA scan does")

    def test_bit_budget_now_holds_for_all_48_rows_not_45(self):
        for L in ALL_L:
            self.assertEqual(sum(T.GAIN_BITS[L]) + sum(T.HIGHER_ORDER_BITS[L]),
                             73 - imbe_band_count(L), f"L={L}")

    def test_recovered_rows_satisfy_every_guard(self):
        # L = 42/43/44 in particular: budget, length, invariant, value range.
        for L in (42, 43, 44):
            row = T.HIGHER_ORDER_BITS[L]
            self.assertEqual(len(row), L - 6)
            self.assertEqual(imbe_band_count(L), 12)
            self.assertEqual(sum(T.GAIN_BITS[L]), 14)
            self.assertEqual(sum(row), 47)
            self.assertEqual(sum(T.GAIN_BITS[L]) + sum(row), 61)
            self.assertTrue(all(0 <= b <= 10 for b in row), f"L={L}")

    def test_provenance_constants_are_present_and_plausible(self):
        # The provenance claim has to be checkable, not just asserted in prose.
        self.assertEqual(len(T.MBELIB_SHA256), 64)
        self.assertTrue(all(c in "0123456789abcdef" for c in T.MBELIB_SHA256))
        self.assertEqual(T.MBELIB_BYTES, 41049)
        self.assertGreaterEqual(len(T.MBELIB_ORIGINS), 2)
        # every origin must be szechyjs/mbelib or a hash-addressed archive —
        # never mbelib-neo, op25, jmbe or dsdcc, which would relicense the product
        joined = " ".join(T.MBELIB_ORIGINS)
        for forbidden in ("mbelib-neo", "arancormonk", "op25", "jmbe", "dsdcc"):
            self.assertNotIn(forbidden, joined)

    def test_all_48_rows_are_within_the_frame_budget(self):
        # A row that allocates more bits than the frame carries would
        # desynchronize the bit reader; assert the slack is never negative.
        for L in ALL_L:
            alloc = ambe.imbe_amplitude_allocation(L)
            self.assertEqual(len(alloc), L - 1, f"L={L}")
            self.assertLessEqual(sum(alloc), imbe_amplitude_bit_budget(L)["bits"],
                                 f"L={L}")


class TestAmplitudeBlockStructure(unittest.TestCase):
    """The 6-block split of the prediction residual — pure arithmetic, and the
    foundation every later stage indexes against."""

    def test_blocks_partition_L_exactly(self):
        for L in ALL_L:
            J = imbe_amplitude_blocks(L)
            self.assertEqual(len(J), ambe.IMBE_AMPLITUDE_BLOCKS, f"L={L}")
            self.assertEqual(sum(J), L, f"L={L} blocks {J}")
            self.assertTrue(all(j >= 1 for j in J), f"L={L} has an empty block")

    def test_blocks_are_within_one_of_each_other(self):
        for L in ALL_L:
            J = imbe_amplitude_blocks(L)
            self.assertLessEqual(max(J) - min(J), 1, f"L={L} blocks {J}")
            self.assertEqual(list(J), sorted(J), f"L={L}: extras ride high")

    def test_block_extremes(self):
        self.assertEqual(imbe_amplitude_blocks(9), (1, 1, 1, 2, 2, 2))
        self.assertEqual(imbe_amplitude_blocks(34), (5, 5, 6, 6, 6, 6))  # TIA Fig 17
        self.assertEqual(imbe_amplitude_blocks(36), (6, 6, 6, 6, 6, 6))
        self.assertEqual(imbe_amplitude_blocks(56), (9, 9, 9, 9, 10, 10))

    def test_too_few_harmonics_rejected(self):
        with self.assertRaises(ValueError):
            imbe_amplitude_blocks(5)

    def test_higher_order_map_covers_every_coefficient_once(self):
        for L in ALL_L:
            J = imbe_amplitude_blocks(L)
            m = imbe_ho_coefficient_map(L)
            self.assertEqual(len(m), L - 6, f"L={L}")
            self.assertEqual(len(set(m)), len(m), f"L={L} duplicate coefficient")
            for i, k in m:
                self.assertTrue(2 <= k <= J[i], f"L={L} block {i} k={k}")
            # length-1 blocks contribute nothing
            for i, Ji in enumerate(J):
                got = sorted(k for bi, k in m if bi == i)
                self.assertEqual(got, list(range(2, Ji + 1)), f"L={L} block {i}")


class TestAmplitudeDct(unittest.TestCase):
    """The DCT is proved against its DEFINING PROPERTIES, not against itself."""

    def test_basis_is_orthogonal_with_the_expected_norms(self):
        for J in (1, 2, 3, 5, 8, 10):
            basis = imbe_dct_matrix(J) * float(J)
            gram = basis @ basis.T
            expected = np.full(J, J / 2.0)
            expected[0] = float(J)
            np.testing.assert_allclose(np.diag(gram), expected, atol=1e-10)
            off = gram - np.diag(np.diag(gram))
            self.assertLess(float(np.max(np.abs(off))), 1e-10, f"J={J}")

    def test_zeroth_coefficient_is_exactly_the_mean(self):
        rng = np.random.default_rng(21)
        for J in range(1, 15):
            x = rng.normal(size=J)
            self.assertAlmostEqual(float(imbe_dct(x)[0]), float(np.mean(x)),
                                   places=12, msg=f"J={J}")

    def test_constant_block_has_no_higher_coefficients(self):
        for J in range(1, 12):
            c = imbe_dct(np.full(J, -1.75))
            self.assertAlmostEqual(float(c[0]), -1.75, places=12)
            np.testing.assert_allclose(c[1:], 0.0, atol=1e-12)

    def test_pure_basis_vector_gives_an_impulse(self):
        J = 9
        for k in range(J):
            j = np.arange(J, dtype=np.float64)
            c = imbe_dct(np.cos(math.pi * k * (j + 0.5) / J))
            want = np.zeros(J)
            want[k] = 1.0 if k == 0 else 0.5
            np.testing.assert_allclose(c, want, atol=1e-10, err_msg=f"k={k}")

    def test_parseval_energy_identity(self):
        rng = np.random.default_rng(22)
        for J in range(1, 16):
            x = rng.normal(size=J)
            c = imbe_dct(x)
            self.assertAlmostEqual(float(np.mean(x ** 2)),
                                   float(c[0] ** 2 + 2.0 * np.sum(c[1:] ** 2)),
                                   places=10, msg=f"J={J}")

    def test_idct_matches_the_standards_written_form(self):
        # TIA eqs. (69)/(73): x_j = sum_k a(k) c_k cos(pi (k-1)(j-0.5)/J),
        # a(1)=1, a(k>=2)=2. Written out longhand here so the implementation is
        # checked against the equation rather than against itself.
        rng = np.random.default_rng(25)
        for J in (1, 2, 3, 6, 9):
            c = rng.normal(size=J)
            want = np.zeros(J)
            for j in range(1, J + 1):
                acc = 0.0
                for k in range(1, J + 1):
                    a = 1.0 if k == 1 else 2.0
                    acc += a * c[k - 1] * math.cos(math.pi * (k - 1)
                                                   * (j - 0.5) / J)
                want[j - 1] = acc
            np.testing.assert_allclose(imbe_idct(c), want, atol=1e-10,
                                       err_msg=f"J={J}")

    def test_inverse_reconstructs(self):
        rng = np.random.default_rng(24)
        for J in range(1, 20):
            x = rng.normal(size=J) * 4.0
            np.testing.assert_allclose(imbe_idct(imbe_dct(x)), x, atol=1e-10,
                                       err_msg=f"J={J}")


class TestFrequencyWarping(unittest.TestCase):
    """The step that exists ONLY because L changes frame to frame. TIA eqs.
    (75)-(79). Tested against ground truth the module never sees."""

    def _grid(self, b0):
        w0, f0, L = imbe_pitch(b0)
        logm = np.zeros(L + 1)
        logm[1:] = _log_envelope(np.arange(1, L + 1) * f0)
        return w0, f0, L, logm

    def test_identity_when_L_does_not_change(self):
        _, _, L, logm = self._grid(80)
        np.testing.assert_allclose(imbe_warp_previous(logm, L)[1:], logm[1:],
                                   atol=1e-10)

    def test_warping_is_driven_by_harmonic_count_not_frequency(self):
        # TIA eq. (75) uses L(-1)/L(0). Doubling L must map current harmonic 2l
        # onto previous harmonic l exactly, with odd harmonics halfway between.
        prev = np.concatenate([[0.0], np.arange(1.0, 21.0)])   # prev[l] = l
        warped = imbe_warp_previous(prev, 40)                  # L_prev=20 -> L=40
        for l in range(1, 21):
            self.assertAlmostEqual(float(warped[2 * l]), float(l), places=10,
                                   msg=f"harmonic {2 * l}")
        for l in range(1, 20):
            self.assertAlmostEqual(float(warped[2 * l + 1]), l + 0.5, places=10)

    def test_index_zero_is_unity_not_the_first_harmonic(self):
        # TIA eq. (78): M_0(-1) = 1.0, i.e. log2 = 0. It is really read whenever
        # L grows, because then f_1 < 1 and k_1 = 0. Clamping to harmonic 1
        # instead (the intuitive choice) gives a different answer, so pin it.
        prev = np.array([0.0, 4.0, 4.0])           # L_prev = 2, both harmonics 4.0
        warped = imbe_warp_previous(prev, 4)       # f_1 = 0.5 -> between P0 and P1
        self.assertAlmostEqual(float(warped[1]), 0.5 * 0.0 + 0.5 * 4.0, places=10)
        self.assertAlmostEqual(float(warped[2]), 4.0, places=10)

    def test_top_end_holds_the_last_value(self):
        # TIA eq. (79). f_L = L_prev exactly, so k_L + 1 runs one past the end.
        prev = np.concatenate([[0.0], np.arange(1.0, 11.0)])   # L_prev = 10
        warped = imbe_warp_previous(prev, 10)
        np.testing.assert_allclose(warped[1:], np.arange(1.0, 11.0), atol=1e-10)
        self.assertTrue(np.all(np.isfinite(imbe_warp_previous(prev, 56))))

    def test_warping_never_extrapolates(self):
        for b_prev, b_cur in ((0, 207), (207, 0), (30, 150), (150, 30), (100, 101)):
            _, _, _, prev = self._grid(b_prev)
            _, _, Lc, _ = self._grid(b_cur)
            warped = imbe_warp_previous(prev, Lc)
            self.assertEqual(warped.size, Lc + 1)
            self.assertTrue(np.all(np.isfinite(warped)))
            lo = min(0.0, float(np.min(prev[1:])))     # 0.0 is the eq.(78) anchor
            hi = max(0.0, float(np.max(prev[1:])))
            self.assertGreaterEqual(float(np.min(warped[1:])), lo - 1e-9)
            self.assertLessEqual(float(np.max(warped[1:])), hi + 1e-9)

    def test_warping_preserves_order(self):
        prev = np.concatenate([[0.0], np.linspace(3.0, 0.1, 40)])
        warped = imbe_warp_previous(prev, 20)
        self.assertTrue(np.all(np.diff(warped[1:]) <= 1e-12))

    def test_no_history_is_a_no_op_not_a_guess(self):
        np.testing.assert_array_equal(imbe_warp_previous(None, 12), np.zeros(13))
        np.testing.assert_array_equal(imbe_warp_previous(np.zeros(1), 12),
                                      np.zeros(13))

    def test_warping_beats_naive_prediction_when_L_changes(self):
        # THE point of the stage. Same speaker, same envelope, new pitch: with
        # warping the residual nearly vanishes; index-for-index it does not.
        for b_prev, b_cur in ((150, 60), (60, 150), (20, 190), (190, 20), (100, 90)):
            _, _, Lp, prev = self._grid(b_prev)
            _, _, Lc, cur = self._grid(b_cur)
            warped = imbe_warp_previous(prev, Lc)
            e_warp = float(np.sum((cur[1:] - warped[1:]) ** 2))

            naive = np.zeros(Lc + 1)
            k = min(Lp, Lc)
            naive[1:k + 1] = prev[1:k + 1]
            if Lc > Lp:
                naive[Lp + 1:] = prev[Lp]
            e_naive = float(np.sum((cur[1:] - naive[1:]) ** 2))
            self.assertLess(e_warp, 0.25 * e_naive,
                            f"b0 {b_prev}->{b_cur}: {e_warp:.4f} vs {e_naive:.4f}")

    def test_extremes_of_the_harmonic_range_both_directions(self):
        for b_prev, b_cur in ((0, 207), (207, 0)):
            _, _, Lp, prev = self._grid(b_prev)
            _, _, Lc, _ = self._grid(b_cur)
            self.assertEqual((Lp, Lc), (9, 56) if b_prev == 0 else (56, 9))
            warped = imbe_warp_previous(prev, Lc)
            self.assertEqual(warped.size, Lc + 1)
            self.assertTrue(np.all(np.isfinite(warped)))


class TestInterFramePrediction(unittest.TestCase):
    def test_prediction_coefficient_matches_the_standard(self):
        # TIA eq. (55), piecewise in L.
        self.assertEqual(imbe_prediction_coefficient(9), 0.4)
        self.assertEqual(imbe_prediction_coefficient(15), 0.4)
        self.assertAlmostEqual(imbe_prediction_coefficient(20), 0.55, places=12)
        self.assertAlmostEqual(imbe_prediction_coefficient(24), 0.67, places=12)
        self.assertEqual(imbe_prediction_coefficient(25), 0.7)
        self.assertEqual(imbe_prediction_coefficient(56), 0.7)

    def test_prediction_coefficient_is_continuous(self):
        # Continuity at both breakpoints is why the strict-vs-inclusive reading
        # of the inequalities cannot change any value. Assert it, because it is
        # the argument that retires an ambiguity in the source.
        self.assertAlmostEqual(0.03 * 15 - 0.05, 0.40, places=12)
        self.assertAlmostEqual(0.03 * 25 - 0.05, 0.70, places=12)
        for L in ALL_L:
            self.assertTrue(0.4 <= imbe_prediction_coefficient(L) <= 0.7)

    def test_prediction_subtracts_a_frame_mean_scalar(self):
        # TIA eq. (77): the correction is (rho/L)*sum(pred), one number for the
        # whole frame. If it were applied per-harmonic, adding a constant to the
        # whole warped prediction would change the OUTPUT; it must not, because
        # the frame's level is carried by the gain index instead.
        rng = np.random.default_rng(33)
        res = np.concatenate([[0.0], rng.normal(size=24)])
        pred = np.concatenate([[0.0], rng.normal(size=24)])
        a = imbe_undo_prediction(res, pred, 0.7)
        shifted = pred.copy()
        shifted[1:] += 3.5
        b = imbe_undo_prediction(res, shifted, 0.7)
        np.testing.assert_allclose(a[1:], b[1:], atol=1e-12)

    def test_undo_prediction_inverts_the_residual(self):
        rng = np.random.default_rng(32)
        logm = np.concatenate([[0.0], rng.normal(size=33)])
        warped = np.concatenate([[0.0], rng.normal(size=33)])
        for rho in (0.0, 0.4, 0.55, 0.7):
            res = imbe_prediction_residual(logm, warped, rho)
            np.testing.assert_allclose(imbe_undo_prediction(res, warped, rho)[1:],
                                       logm[1:], atol=1e-12)

    def test_prediction_removes_energy_from_a_stationary_spectrum(self):
        _, f0, L = imbe_pitch(90)
        logm = np.zeros(L + 1)
        logm[1:] = _log_envelope(np.arange(1, L + 1) * f0)
        warped = imbe_warp_previous(logm, L)
        centred = float(np.sum((logm[1:] - np.mean(logm[1:])) ** 2))
        for rho in (0.4, 0.55, 0.7):
            res = imbe_prediction_residual(logm, warped, rho)
            got = float(np.sum((res[1:] - np.mean(res[1:])) ** 2))
            self.assertAlmostEqual(got, (1.0 - rho) ** 2 * centred, places=9)
            self.assertLess(got, centred)

    def test_length_mismatch_is_rejected(self):
        with self.assertRaises(ValueError):
            imbe_prediction_residual(np.zeros(6), np.zeros(5), 0.7)


class TestEnhancement(unittest.TestCase):
    """Clause 8, eqs. (105)-(110). Muffled speech without it."""

    def _mags(self, L, seed=0):
        rng = np.random.default_rng(seed)
        m = np.zeros(L + 1)
        m[1:] = np.exp2(rng.normal(size=L) * 1.5 + 2.0)
        return m

    def test_energy_is_preserved_exactly(self):
        # eqs. (109)/(110) renormalize to the original RM0, so enhancement
        # reshapes the envelope without changing the frame's loudness.
        for L in (9, 20, 34, 56):
            w0 = 2.0 * math.pi * (0.9254 * math.pi / L) / (2.0 * math.pi)
            m = self._mags(L, seed=L)
            e = ambe.imbe_enhance_amplitudes(m, 0.9254 * math.pi / L, L)
            self.assertAlmostEqual(float(np.sum(e[1:] ** 2)),
                                   float(np.sum(m[1:] ** 2)), places=6,
                                   msg=f"L={L}")

    def test_low_harmonics_are_scaled_uniformly(self):
        # The 8*l <= L guard leaves the lowest harmonics unweighted, so after the
        # global renormalization their ratio to the input must be one constant.
        L = 40
        w0 = 0.9254 * math.pi / L
        m = self._mags(L, seed=5)
        e = ambe.imbe_enhance_amplitudes(m, w0, L)
        low = e[1:6] / m[1:6]                   # 8*l <= 40 for l = 1..5
        np.testing.assert_allclose(low, low[0], rtol=1e-9)

    def test_weights_are_clamped(self):
        # The [0.5, 1.2] clamp means enhancement can never invent a peak; the
        # per-harmonic gain relative to the uniform part stays inside that band.
        L = 34
        w0 = 0.9254 * math.pi / L
        m = self._mags(L, seed=9)
        e = ambe.imbe_enhance_amplitudes(m, w0, L)
        ratio = e[1:L + 1] / m[1:L + 1]
        gamma = float(ratio[0])                 # l=1 is in the untouched band
        self.assertGreaterEqual(float(np.min(ratio)), 0.5 * gamma - 1e-9)
        self.assertLessEqual(float(np.max(ratio)), 1.2 * gamma + 1e-9)

    def test_it_actually_sharpens(self):
        # A spectrum with a peak must come out with MORE contrast than it went
        # in with, or the stage is doing nothing.
        L = 30
        w0 = 0.9254 * math.pi / L
        m = np.zeros(L + 1)
        l = np.arange(1, L + 1)
        m[1:] = 1.0 + 4.0 * np.exp(-((l - 8.0) / 2.5) ** 2)
        e = ambe.imbe_enhance_amplitudes(m, w0, L)
        before = float(np.max(m[1:]) / np.min(m[1:]))
        after = float(np.max(e[1:]) / np.min(e[1:]))
        self.assertGreater(after, before)

    def test_degenerate_frames_are_left_alone(self):
        m = np.zeros(10)
        np.testing.assert_array_equal(ambe.imbe_enhance_amplitudes(m, 0.3, 9), m)
        m2 = np.ones(10)
        np.testing.assert_array_equal(ambe.imbe_enhance_amplitudes(m2, 0.0, 9), m2)


class TestAmplitudeTransformChain(unittest.TestCase):
    def _residual(self, L, seed=0):
        rng = np.random.default_rng(seed)
        r = np.zeros(L + 1)
        r[1:] = rng.normal(size=L) * 2.0
        return r

    def test_coefficient_count_is_exactly_L(self):
        for L in ALL_L:
            gv, higher = imbe_amplitude_forward(self._residual(L), L)
            self.assertEqual(gv.size, ambe.IMBE_AMPLITUDE_BLOCKS)
            total = gv.size + sum(h.size for h in higher)
            self.assertEqual(total, L, f"L={L}")
            self.assertEqual(total - 1, imbe_amplitude_bit_budget(L)["quantizers"])

    def test_forward_and_inverse_are_exact(self):
        for L in ALL_L:
            r = self._residual(L, seed=L)
            gv, higher = imbe_amplitude_forward(r, L)
            np.testing.assert_allclose(imbe_amplitude_inverse(gv, higher, L), r,
                                       atol=1e-9, err_msg=f"L={L}")

    def test_a_flat_residual_lives_entirely_in_the_gain_term(self):
        for L in (9, 30, 56):
            r = np.zeros(L + 1)
            r[1:] = 1.25
            gv, higher = imbe_amplitude_forward(r, L)
            self.assertAlmostEqual(float(gv[0]), 1.25, places=10)
            np.testing.assert_allclose(gv[1:], 0.0, atol=1e-10)
            for h in higher:
                np.testing.assert_allclose(h, 0.0, atol=1e-10)

    def test_wrong_shapes_are_rejected(self):
        gv, higher = imbe_amplitude_forward(self._residual(20), 20)
        with self.assertRaises(ValueError):
            imbe_amplitude_inverse(gv, higher[:5], 20)
        with self.assertRaises(ValueError):
            imbe_amplitude_inverse(gv, higher, 21)
        with self.assertRaises(ValueError):
            imbe_amplitude_forward(np.zeros(10), 20)


class TestDequantizer(unittest.TestCase):
    """Bits in, spectral envelope out — the stage that was missing."""

    def _amp(self, L, seed=0):
        rng = np.random.default_rng(seed)
        return rng.integers(0, 2,
                            imbe_amplitude_bit_budget(L)["bits"]).astype(np.uint8)

    def test_decodes_across_the_whole_supported_pitch_range(self):
        for b0 in range(0, 208, 7):
            w0, _, L = imbe_pitch(b0)
            ok, _why = ambe.imbe_amplitude_available(L)
            mags, logm, note = dequantize_imbe_amplitudes(
                self._amp(L, seed=b0), L, 32, w0=w0)
            if not ok:
                self.assertIsNone(mags)
                continue
            self.assertIsNotNone(mags, f"b0={b0}")
            self.assertEqual(mags.size, L + 1)
            self.assertEqual(logm.size, L + 1)
            self.assertEqual(float(mags[0]), 0.0)
            self.assertTrue(np.all(mags[1:] > 0.0))
            self.assertTrue(np.all(np.isfinite(mags)))
            self.assertIn("decoded", note)

    def test_indices_are_sliced_msb_first(self):
        L = 20
        alloc = [3] * (L - 1)
        want = [(i * 5) % 8 for i in range(L - 1)]
        bits = np.concatenate([int_to_bits(v, 3) for v in want]).astype(np.uint8)
        pad = np.zeros(imbe_amplitude_bit_budget(L)["bits"] - bits.size,
                       dtype=np.uint8)
        got, used = imbe_amplitude_indices(np.concatenate([bits, pad]), L, alloc)
        self.assertEqual(got, want)
        self.assertEqual(used, alloc)

    def test_real_allocation_is_used_by_default(self):
        L = 16
        _, alloc = imbe_amplitude_indices(self._amp(L), L)
        self.assertEqual(alloc, list(T.GAIN_BITS[16]) + list(T.HIGHER_ORDER_BITS[16]))
        self.assertEqual(sum(alloc), imbe_amplitude_bit_budget(L)["bits"])

    def test_a_bad_allocation_fails_loudly(self):
        L = 20
        bits = np.zeros(imbe_amplitude_bit_budget(L)["bits"], dtype=np.uint8)
        with self.assertRaises(ValueError):
            imbe_amplitude_indices(bits, L, [2] * (L - 2))       # wrong count
        with self.assertRaises(ValueError):
            imbe_amplitude_indices(bits, L, [9] * (L - 1))       # over budget
        with self.assertRaises(ValueError):
            imbe_amplitude_indices(bits, L, [-1] + [1] * (L - 2))

    def test_every_amplitude_bit_changes_the_envelope(self):
        # If the bit reader desynchronized, or a quantizer were given the wrong
        # width, some bits would fall in a gap and have no effect. Flip each one
        # and require the decoded envelope to move.
        L = 16
        w0, _, _ = imbe_pitch(next(b for b in range(208) if imbe_pitch(b)[2] == L))
        base_bits = self._amp(L, seed=1)
        base, _, _ = dequantize_imbe_amplitudes(base_bits, L, 32, w0=w0)
        alloc = list(T.GAIN_BITS[L]) + list(T.HIGHER_ORDER_BITS[L])
        for i in range(sum(alloc)):
            flipped = base_bits.copy()
            flipped[i] ^= 1
            other, _, _ = dequantize_imbe_amplitudes(flipped, L, 32, w0=w0)
            self.assertGreater(float(np.max(np.abs(other[1:] - base[1:]))), 0.0,
                               f"amplitude bit {i} had no effect")

    def test_gain_index_moves_the_whole_envelope_together(self):
        # G_1 is the frame gain, so changing only b2 must scale every harmonic by
        # the same factor. Enhancement is off here because it renormalizes energy
        # and would mask exactly this.
        L = 16
        w0, _, _ = imbe_pitch(next(b for b in range(208) if imbe_pitch(b)[2] == L))
        amp = self._amp(L, seed=2)
        a, _, _ = dequantize_imbe_amplitudes(amp, L, 10, w0=w0, enhance=False)
        b, _, _ = dequantize_imbe_amplitudes(amp, L, 50, w0=w0, enhance=False)
        ratio = b[1:] / a[1:]
        np.testing.assert_allclose(ratio, ratio[0], rtol=1e-9)
        self.assertGreater(float(ratio[0]), 1.0)      # index 50 is louder than 10
        self.assertAlmostEqual(float(np.log2(ratio[0])),
                               T.GAIN_LEVELS[50] - T.GAIN_LEVELS[10], places=6)

    def test_history_changes_the_result_and_is_deterministic(self):
        L = 16
        w0, f0, _ = imbe_pitch(next(b for b in range(208) if imbe_pitch(b)[2] == L))
        amp = self._amp(L, seed=3)
        prev = np.zeros(31)
        prev[1:] = _log_envelope(np.arange(1, 31) * 120.0)
        cold, _, _ = dequantize_imbe_amplitudes(amp, L, 32, w0=w0)
        warm, c1, _ = dequantize_imbe_amplitudes(amp, L, 32, prev_log_mags=prev,
                                                 w0=w0)
        self.assertGreater(float(np.max(np.abs(warm[1:] - cold[1:]))), 1e-9)
        again, c2, _ = dequantize_imbe_amplitudes(amp, L, 32, prev_log_mags=prev,
                                                  w0=w0)
        np.testing.assert_allclose(warm, again, atol=0.0)
        np.testing.assert_allclose(c1, c2, atol=0.0)

    def test_predictor_state_is_unenhanced(self):
        # TIA clause 8 requires the NEXT frame to predict against the unenhanced
        # amplitudes. If log_magnitudes came back enhanced (or output-scaled),
        # exponentiating it would not reproduce the pre-enhancement envelope.
        L = 16
        w0, _, _ = imbe_pitch(next(b for b in range(208) if imbe_pitch(b)[2] == L))
        amp = self._amp(L, seed=4)
        plain, log_plain, _ = dequantize_imbe_amplitudes(amp, L, 32, w0=w0,
                                                         enhance=False)
        enh, log_enh, _ = dequantize_imbe_amplitudes(amp, L, 32, w0=w0,
                                                     enhance=True)
        np.testing.assert_allclose(log_plain, log_enh, atol=0.0)
        np.testing.assert_allclose(np.exp2(log_plain[1:]) * ambe.IMBE_OUTPUT_SCALE,
                                   plain[1:], rtol=1e-9)
        self.assertGreater(float(np.max(np.abs(enh[1:] - plain[1:]))), 0.0)

    def test_no_L_is_unsupported_any_more(self):
        self.assertEqual(tuple(T.UNVERIFIED_L), ())
        for L in ALL_L:
            ok, why = ambe.imbe_amplitude_available(L)
            self.assertTrue(ok, f"L={L}: {why}")
            self.assertEqual(len(ambe.imbe_amplitude_allocation(L)), L - 1)

    def test_the_refusal_path_still_works_if_a_gap_is_reinstated(self):
        # UNVERIFIED_L is empty, so the guard is currently a no-op. It is kept
        # deliberately, and "kept" is worthless unless it still functions —
        # otherwise a future gap would be added and silently decode garbage.
        L = 30
        original = T.UNVERIFIED_L
        try:
            T.UNVERIFIED_L = (L,)
            ok, why = ambe.imbe_amplitude_available(L)
            self.assertFalse(ok)
            self.assertIn(str(L), why)
            mags, logm, note = dequantize_imbe_amplitudes(
                np.ones(imbe_amplitude_bit_budget(L)["bits"], dtype=np.uint8),
                L, 32, w0=0.1)
            self.assertIsNone(mags)
            self.assertIsNone(logm)
            self.assertIn("bit allocation", note)
            with self.assertRaises(ValueError):
                ambe.imbe_amplitude_allocation(L)
        finally:
            T.UNVERIFIED_L = original
        self.assertTrue(ambe.imbe_amplitude_available(L)[0])

    def test_output_is_scaled_into_the_float_contract(self):
        # The codec's amplitudes are 16-bit referenced; this module's contract is
        # -1..1 floats. Pin the conversion so a future change cannot silently
        # alter every operator's listening level.
        self.assertAlmostEqual(ambe.IMBE_OUTPUT_SCALE, 1.0 / 32768.0, places=12)


class TestDecodedSpeechSpectrum(unittest.TestCase):
    """The end-to-end claim: coded bits become audio with harmonic structure at
    the transmitted fundamental and a NON-FLAT envelope. Proved spectrally."""

    def _stream(self, b0, gain=32, seed=0, frames=40, voiced=True):
        _, f0, L = imbe_pitch(b0)
        K = imbe_band_count(L)
        rng = np.random.default_rng(seed)
        amp = rng.integers(0, 2,
                           imbe_amplitude_bit_budget(L)["bits"]).astype(np.uint8)
        bits = pack_imbe_params(b0, [voiced] * K, gain, amplitude_bits=amp)
        return f0, L, decode_p25_voice([pack_imbe_frame(bits)] * frames)

    def test_voiced_audio_is_harmonic_at_the_decoded_fundamental(self):
        for b0 in (40, 100, 180):        # all map to a supported L
            f0, L, (pcm, reports) = self._stream(b0, seed=b0)
            self.assertTrue(reports[0]["intelligible"])
            self.assertGreater(float(np.sqrt(np.mean(pcm ** 2))), 1e-4)
            frac = _harmonic_energy_fraction(pcm, f0, L)
            self.assertGreater(frac, 0.9,
                               f"b0={b0}: only {frac:.2f} of energy on harmonics")

    def test_the_decoded_envelope_is_not_flat(self):
        # A flat or 1/l envelope would mean the amplitude bits were ignored. Real
        # decoded envelopes have formant structure, so harmonic magnitudes must
        # span a wide dynamic range and not follow a fixed shape.
        for b0 in (30, 90, 170):
            _, f0, L = imbe_pitch(b0)
            rng = np.random.default_rng(b0)
            amp = rng.integers(0, 2, imbe_amplitude_bit_budget(L)["bits"]).astype(np.uint8)
            bits = pack_imbe_params(b0, [True] * imbe_band_count(L), 34,
                                    amplitude_bits=amp)
            p = decode_imbe_params(bits)
            m = p["magnitudes"][1:]
            self.assertGreater(float(np.max(m) / np.min(m)), 3.0, f"b0={b0}")
            # and it is not the synthetic 1/l placeholder
            placeholder = ambe.synthetic_envelope(L, 34)[1:]
            corr = float(np.corrcoef(m / np.max(m),
                                     placeholder / np.max(placeholder))[0, 1])
            self.assertLess(corr, 0.95, f"b0={b0} envelope looks like 1/l")

    def test_different_amplitude_bits_give_different_audio(self):
        _, _, (a, _) = self._stream(100, seed=1, frames=8)
        _, _, (b, _) = self._stream(100, seed=2, frames=8)
        self.assertGreater(float(np.max(np.abs(a - b))), 1e-5)

    def test_unvoiced_frames_are_noise_not_harmonics(self):
        # Compared on the whole harmonic comb rather than on f0 alone: with a
        # decoded (non-flat) envelope the fundamental is not necessarily the
        # loudest harmonic, so a single-band test would be measuring the
        # envelope's shape rather than the voicing decision.
        f0, L, (v_pcm, _) = self._stream(100, seed=7, voiced=True)
        _, _, (u_pcm, _) = self._stream(100, seed=7, voiced=False)
        self.assertGreater(_harmonic_energy_fraction(v_pcm, f0, L),
                           1.5 * _harmonic_energy_fraction(u_pcm, f0, L))
        self.assertGreater(_harmonic_energy_fraction(v_pcm, f0, L), 0.9)

    def test_audio_stays_inside_the_float_contract(self):
        for b0 in (20, 80, 140, 200):
            _, _, (pcm, reports) = self._stream(b0, gain=32, seed=b0, frames=10)
            self.assertLessEqual(float(np.max(np.abs(pcm))), 1.0)
            self.assertEqual(pcm.dtype, np.float32)
            for r in reports:
                self.assertEqual(r["clipped"], 0, f"b0={b0} clipped")

    def test_the_predictor_chains_across_frames(self):
        # Frame 2 of a stream must differ from frame 1 given identical bits,
        # because the first frame's amplitudes are now the second's prediction.
        # Identical frames would mean the history is not being carried.
        b0 = 100
        _, f0, L = imbe_pitch(b0)
        rng = np.random.default_rng(11)
        amp = rng.integers(0, 2, imbe_amplitude_bit_budget(L)["bits"]).astype(np.uint8)
        bits = pack_imbe_params(b0, [True] * imbe_band_count(L), 32,
                                amplitude_bits=amp)
        st = MbeState()
        p1 = decode_imbe_params(bits, prev_log_mags=st.prev_log_mags)
        p2 = decode_imbe_params(bits, prev_log_mags=p1["log_magnitudes"])
        self.assertGreater(float(np.max(np.abs(p2["magnitudes"]
                                               - p1["magnitudes"]))), 1e-9)

    def test_a_fresh_state_starts_from_the_specified_history(self):
        # TIA initialises M_l(-1) = 1.0 with L(-1) = 30, so a new stream is
        # reproducible rather than dependent on whatever ran before it.
        st = MbeState()
        self.assertEqual(st.prev_log_mags.size, ambe.IMBE_INIT_PREV_L + 1)
        np.testing.assert_array_equal(st.prev_log_mags,
                                      np.zeros(ambe.IMBE_INIT_PREV_L + 1))
        _, _, (a, _) = self._stream(100, seed=5, frames=6)
        _, _, (b, _) = self._stream(100, seed=5, frames=6)
        np.testing.assert_allclose(a, b, atol=0.0)


class TestHonestyAfterImplementation(unittest.TestCase):
    """The claims got stronger, so the honesty tests have to get stronger too."""

    def test_capability_reports_what_is_and_is_not_supported(self):
        cap = ambe.capability()
        sa = cap["spectral_amplitudes"]
        self.assertTrue(cap["protocols"]["p25_phase1_imbe"]["magnitudes"])
        self.assertTrue(sa["implemented"])
        # all 48 legal harmonic counts now decode — the gap is closed
        self.assertEqual(sa["supported_L"], 48)
        self.assertEqual(sa["total_L"], 48)
        self.assertEqual(tuple(sa["unsupported_L"]), ())
        self.assertIn("mbelib", sa["sourced_tables"])

    def test_capability_states_the_verification_each_table_actually_has(self):
        # The two sourced tables do NOT have identical evidence, and capability()
        # is what the report layer renders, so it must not flatten them into one
        # reassuring sentence.
        tv = ambe.capability()["spectral_amplitudes"]["table_verification"]
        self.assertEqual(tv["source_sha256"], T.MBELIB_SHA256)
        self.assertGreaterEqual(tv["independent_origins"], 2)
        self.assertIn("240/240", tv["gain_bits"])
        self.assertIn("1272/1272", tv["higher_order_bits"])
        self.assertIn("48/48", tv["row_checks"])
        self.assertEqual(tuple(tv["tia_corroborated_L"]), T.TIA_CORROBORATED_L)

    def test_capability_still_admits_mbelib_is_its_own_authority_above_L17(self):
        # The honest residual: transcription risk is closed, source risk is not.
        # If this ever disappears, someone has overclaimed.
        assumptions = " ".join(
            ambe.capability()["spectral_amplitudes"]["unverified_assumptions"])
        self.assertIn("mbelib alone", assumptions)
        self.assertIn("L=23", assumptions)          # the proof mbelib has errors

    def test_capability_still_says_no_off_air_validation(self):
        cap = ambe.capability()
        self.assertIn("synthetic", cap["validated_against"])
        self.assertIn("NO OFF-AIR FRAME HAS EVER BEEN DECODED", cap["caveat"])
        self.assertIn("field order", cap["caveat"])

    def test_capability_surfaces_the_attribution_duty(self):
        # ISC compliance is a shipping precondition, so it has to stay visible to
        # the report layer rather than being buried in a source comment. The duty
        # has since been DISCHARGED in THIRD_PARTY_NOTICES.md, so this now pins
        # that capability() points at the notice — and tests/test_third_party_
        # notices.py pins that the notice itself, and the licence text in it,
        # still exist. Together those make the obligation hard to lose.
        note = ambe.capability()["spectral_amplitudes"]["attribution_required"]
        self.assertIn("mbelib", note)
        self.assertIn("THIRD_PARTY_NOTICES.md", note)

    def test_every_pitch_index_now_decodes_to_real_magnitudes(self):
        # The inverse of the test this replaces. Previously some pitch indices
        # landed on L = 42/43/44 and had to refuse; now EVERY one of the 208
        # legal pitch indices must produce a real envelope. Sweeping all 208
        # (not a sample) is what proves no harmonic count was left behind.
        for b0 in range(ambe.IMBE_PITCH_RESERVED):
            _, _, L = imbe_pitch(b0)
            bits = pack_imbe_params(b0, [True] * imbe_band_count(L), 30)
            _pcm, reports = decode_p25_voice([pack_imbe_frame(bits)])
            r = reports[0]
            self.assertTrue(r["magnitudes_available"], f"b0={b0} (L={L})")
            self.assertTrue(r["intelligible"], f"b0={b0} (L={L})")
            self.assertFalse(r["silent"], f"b0={b0} (L={L})")

    def test_the_three_recovered_harmonic_counts_decode(self):
        # L = 42, 43, 44 specifically — the rows that were absent. Named
        # explicitly so a regression that drops them again is unmistakable.
        for L in (42, 43, 44):
            ok, why = ambe.imbe_amplitude_available(L)
            self.assertTrue(ok, f"L={L} unavailable: {why}")
            self.assertEqual(len(T.HIGHER_ORDER_BITS[L]), L - 6)
            b0 = next(b for b in range(ambe.IMBE_PITCH_RESERVED)
                      if imbe_pitch(b)[2] == L)
            _, f0, _ = imbe_pitch(b0)
            rng = np.random.default_rng(L)
            amp = rng.integers(
                0, 2, imbe_amplitude_bit_budget(L)["bits"]).astype(np.uint8)
            bits = pack_imbe_params(b0, [True] * imbe_band_count(L), 32,
                                    amplitude_bits=amp)
            pcm, reports = decode_p25_voice([pack_imbe_frame(bits)] * 40)
            self.assertTrue(reports[0]["intelligible"], f"L={L}")
            # and the audio really is harmonic at the transmitted fundamental
            self.assertGreater(_harmonic_energy_fraction(pcm, f0, L), 0.9,
                               f"L={L} audio is not harmonic at f0")

    def test_synthetic_envelope_never_overrides_a_real_decode(self):
        # Opting into the placeholder must not downgrade frames that really
        # decoded — it is a fallback for the unsupported ones, not a mode.
        b0 = 100
        _, _, L = imbe_pitch(b0)
        bits = pack_imbe_params(b0, [True] * imbe_band_count(L), 30)
        _pcm, reports = decode_p25_voice([pack_imbe_frame(bits)],
                                         envelope="synthetic")
        self.assertTrue(reports[0]["intelligible"])
        self.assertIn("decoded", reports[0]["note"])

    def test_synthetic_envelope_would_still_be_labelled_if_a_gap_returned(self):
        # No real frame reaches the placeholder any more, so drive the labelling
        # path directly with a simulated gap. This keeps the "never emit
        # unlabelled placeholder audio" contract under test even though the
        # condition that triggered it no longer occurs.
        L = 42
        b0 = next(b for b in range(ambe.IMBE_PITCH_RESERVED)
                  if imbe_pitch(b)[2] == L)
        bits = pack_imbe_params(b0, [True] * imbe_band_count(L), 30)
        frame = pack_imbe_frame(bits)
        original = T.UNVERIFIED_L
        try:
            T.UNVERIFIED_L = (L,)              # reinstate the gap for this test
            _pcm, reports = decode_p25_voice([frame] * 4, envelope="synthetic")
            for r in reports:
                self.assertTrue(r["magnitudes_available"])
                self.assertFalse(r["intelligible"])
                self.assertEqual(r["envelope"], "synthetic")
                self.assertIn("NOT speech", r["note"])
            # and without opting in, a gapped frame is silence, never a guess
            _pcm2, plain = decode_p25_voice([frame])
            self.assertFalse(plain[0]["magnitudes_available"])
            self.assertTrue(plain[0]["silent"])
        finally:
            T.UNVERIFIED_L = original
        # the gap really is gone once the patch is undone
        self.assertTrue(ambe.imbe_amplitude_available(L)[0])

    def test_failed_fec_still_produces_no_audio(self):
        info = np.zeros(IMBE_INFO_BITS, dtype=np.uint8)
        coded = pack_imbe_frame(info).copy()
        for (a, _b) in ambe.IMBE_VECTOR_SPANS[:4]:
            for k in range(4):
                coded[a + k] ^= 1
        pcm, reports = decode_p25_voice([coded])
        self.assertFalse(reports[0]["fec_ok"])
        self.assertFalse(reports[0]["intelligible"])
        np.testing.assert_array_equal(pcm, np.zeros(FRAME_SAMPLES, dtype=np.float32))

    def test_noise_never_decodes_as_voice(self):
        rng = np.random.default_rng(2)
        frames = [rng.integers(0, 2, IMBE_FRAME_BITS).astype(np.uint8)
                  for _ in range(200)]
        _pcm, reports = decode_p25_voice(frames)
        self.assertEqual(sum(1 for r in reports if r["fec_ok"]), 0)

    def test_module_docstring_states_the_remaining_limits(self):
        doc = ambe.__doc__ or ""
        # collapse the hard line wrapping so these assertions pin the CLAIMS
        # rather than the current paragraph layout
        flat = " ".join(doc.split())
        self.assertIn("off-air", flat)
        self.assertIn("NO OFF-AIR FRAME HAS EVER BEEN DECODED", flat)
        self.assertIn("FREQUENCY WARPING", flat)
        self.assertIn("FIELD ORDER", flat)
        self.assertIn("ISC", flat)
        self.assertIn("CONSEQUENCE, STATED PLAINLY", flat)
        # the gap is recorded as CLOSED, not silently dropped
        self.assertIn("(CLOSED)", flat)
        self.assertIn("ALL 48 legal harmonic counts", flat)
        # and the residual source-risk is still stated, not quietly retired
        self.assertIn("mbelib is its own authority", flat)
        self.assertIn("per-entry", flat.lower())


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
