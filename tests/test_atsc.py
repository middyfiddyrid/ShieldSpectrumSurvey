"""ATSC 8VSB signature detector — the point is DISCRIMINATION, so every
positive test is paired with a negative that must NOT confirm."""

import unittest

import numpy as np

from survey import atsc

CH34_LOW = 585_000_000.0          # US TV RF channel 34 = 585-591 MHz
CH34_CENTRE = CH34_LOW + atsc.CHANNEL_BW_HZ / 2.0
FS = 8_000_000.0


class TestChannelMath(unittest.TestCase):
    def test_channel_for_frequency_round_trips(self):
        from survey.baseline.fcc_broadcast import tv_channel_to_range
        for ch in (2, 4, 5, 6, 7, 13, 14, 34, 36):
            lo, hi = tv_channel_to_range(ch)
            mid = (lo + hi) / 2.0
            got = atsc.channel_for_frequency(mid)
            self.assertIsNotNone(got, f"ch {ch} centre {mid} not resolved")
            self.assertEqual(got[0], ch)
            self.assertAlmostEqual(got[1], lo, places=6)

    def test_frequency_outside_tv_bands_is_none(self):
        for f in (88.5, 162.4, 462.5, 915.0, 2437.0):
            self.assertIsNone(atsc.channel_for_frequency(f))

    def test_pilot_offset_matches_the_standard(self):
        # 309.440559 kHz above the lower edge, ~2.69 MHz below channel centre
        self.assertAlmostEqual(atsc.PILOT_OFFSET_HZ, 309_440.559, places=3)
        below_centre = atsc.CHANNEL_BW_HZ / 2.0 - atsc.PILOT_OFFSET_HZ
        self.assertAlmostEqual(below_centre, 2_690_559.441, places=3)


class TestConfirmsRealSignature(unittest.TestCase):
    def test_synthesized_8vsb_is_confirmed(self):
        iq = atsc.encode_atsc(fs=FS, seconds=0.05, channel_low_hz=CH34_LOW,
                              capture_center_hz=CH34_CENTRE, seed=1)
        res = atsc.atsc_signature(iq, FS, CH34_CENTRE, CH34_LOW)
        self.assertTrue(res["confirmed"], res["summary"])
        self.assertGreaterEqual(res["confidence"], 0.9)
        self.assertIn("confirmed", res["summary"].lower())

    def test_pilot_is_found_at_the_standard_offset(self):
        iq = atsc.encode_atsc(fs=FS, seconds=0.05, channel_low_hz=CH34_LOW,
                              capture_center_hz=CH34_CENTRE, seed=2)
        res = atsc.atsc_signature(iq, FS, CH34_CENTRE, CH34_LOW)
        pilot = res["pilot"]
        self.assertIsNotNone(pilot)
        # located within a couple of PSD bins of nominal
        self.assertLess(abs(pilot["offset_error_hz"]), 2_000.0)
        self.assertGreater(pilot["prominence_db"], atsc.PILOT_MIN_PROM_DB)
        expect_mhz = (CH34_LOW + atsc.PILOT_OFFSET_HZ) / 1e6
        self.assertAlmostEqual(pilot["mhz"], expect_mhz, places=2)

    def test_plateau_width_is_about_the_occupied_bandwidth(self):
        iq = atsc.encode_atsc(fs=FS, seconds=0.05, channel_low_hz=CH34_LOW,
                              capture_center_hz=CH34_CENTRE, seed=3)
        res = atsc.atsc_signature(iq, FS, CH34_CENTRE, CH34_LOW)
        w = res["plateau"]["width_hz"]
        self.assertGreater(w, atsc.OCCUPIED_BW_HZ * 0.8)
        self.assertLess(w, atsc.CHANNEL_BW_HZ)

    def test_offset_capture_still_confirms(self):
        """Capture centred on the channel centre is ideal, but a tuner parked a
        bit off must still find the pilot as long as it is inside the span."""
        off = CH34_CENTRE + 700_000.0
        iq = atsc.encode_atsc(fs=FS, seconds=0.05, channel_low_hz=CH34_LOW,
                              capture_center_hz=off, seed=4)
        res = atsc.atsc_signature(iq, FS, off, CH34_LOW)
        self.assertTrue(res["confirmed"], res["summary"])


class TestRejectsImpostors(unittest.TestCase):
    def test_pure_noise_is_not_confirmed(self):
        rng = np.random.default_rng(7)
        n = int(FS * 0.05)
        iq = ((rng.standard_normal(n) + 1j * rng.standard_normal(n))
              / np.sqrt(2.0)).astype(np.complex64)
        res = atsc.atsc_signature(iq, FS, CH34_CENTRE, CH34_LOW)
        self.assertFalse(res["confirmed"], res["summary"])
        self.assertIn("no atsc", res["summary"].lower())

    def test_channel_filling_carrier_without_a_pilot_is_flagged_not_confirmed(self):
        """The dangerous case: something wide enough to look like DTV on a
        waterfall. Must NOT confirm, and must say the pilot is missing."""
        iq = atsc.encode_atsc(fs=FS, seconds=0.05, channel_low_hz=CH34_LOW,
                              capture_center_hz=CH34_CENTRE, pilot_db=-80.0, seed=5)
        res = atsc.atsc_signature(iq, FS, CH34_CENTRE, CH34_LOW)
        self.assertFalse(res["confirmed"], res["summary"])
        self.assertIn("no 8vsb pilot", res["summary"].lower())
        self.assertTrue(any("NOT ATSC" in c for c in res["caveats"]))

    def test_narrowband_carrier_in_the_channel_is_not_confirmed(self):
        """A single strong CW/FM carrier parked mid-channel has energy but no
        plateau — the flatness test is what rejects it."""
        n = int(FS * 0.05)
        t = np.arange(n) / FS
        rng = np.random.default_rng(9)
        iq = (2.0 * np.exp(2j * np.pi * 120_000.0 * t)
              + 0.02 * (rng.standard_normal(n) + 1j * rng.standard_normal(n)))
        res = atsc.atsc_signature(iq.astype(np.complex64), FS, CH34_CENTRE, CH34_LOW)
        self.assertFalse(res["confirmed"], res["summary"])

    def test_wrong_channel_does_not_confirm(self):
        """Signal really is on ch 34; ask about ch 35 and it must say no."""
        iq = atsc.encode_atsc(fs=FS, seconds=0.05, channel_low_hz=CH34_LOW,
                              capture_center_hz=CH34_CENTRE, seed=6)
        res = atsc.atsc_signature(iq, FS, CH34_CENTRE, CH34_LOW + 6_000_000.0)
        self.assertFalse(res["confirmed"], res["summary"])


class TestHonestyAboutLimits(unittest.TestCase):
    def test_pilot_outside_capture_says_so_instead_of_denying(self):
        """A too-narrow capture must NOT report 'no station' — that would be a
        false negative dressed as a measurement."""
        narrow_fs = 1_000_000.0
        iq = atsc.encode_atsc(fs=narrow_fs, seconds=0.05, channel_low_hz=CH34_LOW,
                              capture_center_hz=CH34_CENTRE, seed=8)
        res = atsc.atsc_signature(iq, narrow_fs, CH34_CENTRE, CH34_LOW)
        self.assertFalse(res["confirmed"])
        self.assertIn("outside the captured span", res["summary"])
        self.assertTrue(any("inconclusive" in c for c in res["caveats"]))

    def test_never_claims_a_picture(self):
        iq = atsc.encode_atsc(fs=FS, seconds=0.05, channel_low_hz=CH34_LOW,
                              capture_center_hz=CH34_CENTRE, seed=10)
        res = atsc.atsc_signature(iq, FS, CH34_CENTRE, CH34_LOW)
        self.assertFalse(res["picture"])
        self.assertTrue(any("not decoded" in c for c in res["caveats"]))

    def test_confirmation_carries_the_hiding_caveat(self):
        """A confirmed station must still warn that a weak emitter can hide
        under the data floor — the whole reason this check exists."""
        iq = atsc.encode_atsc(fs=FS, seconds=0.05, channel_low_hz=CH34_LOW,
                              capture_center_hz=CH34_CENTRE, seed=11)
        res = atsc.atsc_signature(iq, FS, CH34_CENTRE, CH34_LOW)
        self.assertTrue(res["confirmed"])
        self.assertTrue(any("hiding under the data floor" in c
                            for c in res["caveats"]))

    def test_short_capture_degrades_gracefully(self):
        iq = np.zeros(64, dtype=np.complex64)
        res = atsc.atsc_signature(iq, FS, CH34_CENTRE, CH34_LOW)
        self.assertFalse(res["confirmed"])
        self.assertIn("too short", res["summary"])


if __name__ == "__main__":
    unittest.main()
