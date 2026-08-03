"""P25 must actually be reachable through the router.

decode_p25() and looks_like_p25() existed, were tested, and were never called:
P25 was absent from _decoders(), _ORDER and DECODE_MODES. ANALYZE on a real P25
signal therefore answered "no decodable protocol matched its signature" — which
reads as "not P25" when the truth was "P25 was never tried". A decoder nothing
calls is not a feature.
"""

import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from survey import decode_router as dr                          # noqa: E402


class P25RegistrationTest(unittest.TestCase):
    def test_p25_is_a_declared_decode_mode(self):
        self.assertIn("p25", dr.DECODE_MODES)

    def test_p25_is_in_the_try_order(self):
        self.assertIn("p25", dr._ORDER)

    def test_p25_is_constructible_from_the_registry(self):
        decs = dr._decoders()
        self.assertIn("p25", decs)
        run, gate = decs["p25"]
        self.assertTrue(callable(run) and callable(gate))

    def test_every_ordered_decoder_exists_in_the_registry(self):
        """Guards the class of bug this test file exists for: a name in one
        place and not the other."""
        decs = dr._decoders()
        for name in dr._ORDER:
            self.assertIn(name, decs, f"{name} is ordered but not registered")
        for name in dr.DECODE_MODES:
            self.assertIn(name, decs, f"{name} is offered to the UI but not registered")


class P25RoutingTest(unittest.TestCase):
    """Registration is only half of it — it has to route without wrecking
    everything else."""

    def _p25_iq(self, fs=48000.0, frames=8):
        """Several LDU1 frames with VARYING payloads.

        Identical repeated frames are not a realistic test signal: the repetition
        puts line spectra in the PSD and the occupied-bandwidth measurement
        collapses to ~0.1 kHz, which is a property of the fixture rather than of
        C4FM. Real traffic varies frame to frame, and measures ~5 kHz.
        """
        from survey import c4fm, p25
        rng = np.random.default_rng(3)
        dibits = np.concatenate([
            p25.encode_frame_dibits(                       # DUID 0x5 = LDU1, NAC 0x293
                0x5, 0x293,
                lc_bits=list(rng.integers(0, 2, 72)),
                voice=[list(rng.integers(0, 2, 144)) for _ in range(9)])
            for _ in range(frames)])
        return c4fm.encode_c4fm(dibits, fs), fs

    def test_the_gate_does_not_veto_a_real_p25_signal(self):
        """The bug this file was written for: decode_p25 worked on a signal that
        looks_like_p25 rejected, so the router never called it. iq_features
        cannot see RRC-shaped C4FM as FSK — the gate must not require that."""
        from survey import p25
        from survey.identify import iq_features
        iq, fs = self._p25_iq()
        self.assertTrue(p25.looks_like_p25(iq_features(iq, fs)))

    def test_a_real_p25_signal_routes_to_p25(self):
        iq, fs = self._p25_iq()
        from survey.identify import iq_features
        feats = iq_features(iq, fs)
        r = dr.route_decode(iq, fs, feats)
        self.assertEqual(r.get("protocol"), "P25",
                         f"P25 signal did not route to P25 (got {r.get('protocol')})")

    def test_noise_does_not_route_to_p25(self):
        """The false positive that would matter most — inventing a police
        radio system out of noise in a client report."""
        rng = np.random.default_rng(11)
        for seed_shift in range(6):
            rng = np.random.default_rng(11 + seed_shift)
            n = 48000
            iq = (rng.standard_normal(n) + 1j * rng.standard_normal(n)).astype(np.complex64)
            from survey.identify import iq_features
            feats = iq_features(iq, 48000.0)
            r = dr.route_decode(iq, 48000.0, feats)
            self.assertNotEqual(r.get("protocol"), "P25",
                                "noise was decoded as a P25 call")

    def test_unmodulated_carrier_does_not_route_to_p25(self):
        fs, n = 48000.0, 48000
        t = np.arange(n) / fs
        iq = np.exp(2j * np.pi * 1000.0 * t).astype(np.complex64)
        from survey.identify import iq_features
        feats = iq_features(iq, fs)
        r = dr.route_decode(iq, fs, feats)
        self.assertNotEqual(r.get("protocol"), "P25")


if __name__ == "__main__":
    unittest.main()
