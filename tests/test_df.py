"""Phase-interferometry DF DSP tests (survey/df.py) — synthetic coherent
wavefronts, no hardware. A plane wave arriving at angle theta on a 2-element
array of spacing d shows up as an inter-channel phase 2*pi*d*sin(theta)/lambda;
we plant exactly that and assert measure() recovers theta (and its mirror)."""

import unittest

import numpy as np

from survey import df

C = df.C_LIGHT


def wavefront(fs, n, baseband_hz, phase_deg, snr_db=30.0, seed=0):
    """A coherent (iq_a, iq_b) pair. `phase_deg` is the cross-spectrum phase the
    pair should exhibit — angle(A·conj(B)) = φ_A − φ_B — so B carries −phase_deg,
    matching how measure() reads the wavefront direction."""
    rng = np.random.default_rng(seed)
    t = np.arange(n) / fs
    tone = np.exp(2j * np.pi * baseband_hz * t)
    amp = 10.0 ** (snr_db / 20.0)
    a = amp * tone + (rng.standard_normal(n) + 1j * rng.standard_normal(n))
    b = amp * tone * np.exp(-1j * np.radians(phase_deg)) \
        + (rng.standard_normal(n) + 1j * rng.standard_normal(n))
    return a.astype(np.complex64), b.astype(np.complex64)


def phase_for_bearing(bearing_deg, spacing_m, rf_hz):
    lam = C / rf_hz
    return np.degrees(2 * np.pi * spacing_m * np.sin(np.radians(bearing_deg)) / lam)


class TestCrossSpectrum(unittest.TestCase):
    def test_recovers_planted_phase(self):
        fs = 4e6
        a, b = wavefront(fs, 1 << 16, baseband_hz=200e3, phase_deg=45.0)
        cs = df.cross_spectrum(a, b, fs, nperseg=4096)
        i = int(np.argmax(cs["sxx"] + cs["syy"]))
        got = np.degrees(np.angle(cs["sxy"][i]))
        self.assertAlmostEqual(df._wrap_deg(got - 45.0), 0.0, delta=3.0)

    def test_rejects_short_capture(self):
        with self.assertRaises(ValueError):
            df.cross_spectrum(np.zeros(100, np.complex64),
                              np.zeros(100, np.complex64), 4e6)


class TestBearing(unittest.TestCase):
    def setUp(self):
        self.fs = 4e6
        self.rf = 433.9e6
        self.spacing = 0.30           # lambda/2 at ~500 MHz, unambiguous at 433.9

    def _measure(self, bearing_deg, **kw):
        ph = phase_for_bearing(bearing_deg, self.spacing, self.rf)
        a, b = wavefront(self.fs, 1 << 16, 150e3, ph, **kw)
        # calibrated: broadside (0 deg) has been zeroed, so cal offset = 0
        return df.measure(a, b, self.fs, self.rf, self.spacing,
                          cal_offset_deg=0.0)

    def test_broadside_is_zero(self):
        m = self._measure(0.0)
        self.assertEqual(m.verdict, df.BEARING_OK)
        self.assertAlmostEqual(m.bearing_deg, 0.0, delta=5.0)

    def test_positive_bearing(self):
        m = self._measure(30.0)
        self.assertEqual(m.verdict, df.BEARING_OK)
        self.assertAlmostEqual(m.bearing_deg, 30.0, delta=6.0)

    def test_negative_bearing(self):
        m = self._measure(-45.0)
        self.assertAlmostEqual(m.bearing_deg, -45.0, delta=7.0)

    def test_mirror_is_reported(self):
        m = self._measure(30.0)
        self.assertIsNotNone(m.mirror_deg)
        # front/back twin of +30 is +150
        self.assertAlmostEqual(abs(m.mirror_deg), 150.0, delta=8.0)

    def test_high_coherence_on_clean_signal(self):
        m = self._measure(20.0)
        self.assertGreater(m.coherence, 0.8)


class TestHonestyGates(unittest.TestCase):
    def setUp(self):
        self.fs = 4e6

    def test_no_signal_when_only_noise(self):
        rng = np.random.default_rng(1)
        n = 1 << 16
        a = (rng.standard_normal(n) + 1j * rng.standard_normal(n)).astype(np.complex64)
        b = (rng.standard_normal(n) + 1j * rng.standard_normal(n)).astype(np.complex64)
        m = df.measure(a, b, self.fs, 433e6, 0.3, cal_offset_deg=0.0)
        self.assertEqual(m.verdict, df.NO_SIGNAL)
        self.assertIsNone(m.bearing_deg)

    def test_low_coherence_when_channels_uncorrelated(self):
        # two DIFFERENT tones (independent emitters / multipath) => low coherence
        fs, n = self.fs, 1 << 16
        t = np.arange(n) / fs
        amp = 30.0
        rng = np.random.default_rng(2)
        a = (amp * np.exp(2j * np.pi * 150e3 * t)
             + rng.standard_normal(n) + 1j * rng.standard_normal(n)).astype(np.complex64)
        b = (amp * np.exp(2j * np.pi * 150e3 * t) * np.exp(1j * np.cumsum(
             rng.standard_normal(n)) * 0.5)      # phase-scrambled B
             + rng.standard_normal(n) + 1j * rng.standard_normal(n)).astype(np.complex64)
        m = df.measure(a, b, fs, 433e6, 0.3, cal_offset_deg=0.0)
        self.assertIn(m.verdict, (df.LOW_COHERENCE, df.NO_SIGNAL))
        self.assertIsNone(m.bearing_deg)

    def test_uncalibrated_is_flagged(self):
        ph = phase_for_bearing(20.0, 0.3, 433.9e6)
        a, b = wavefront(self.fs, 1 << 16, 150e3, ph)
        m = df.measure(a, b, self.fs, 433.9e6, 0.3, cal_offset_deg=None)
        self.assertFalse(m.calibrated)
        self.assertTrue(any("UNCALIBRATED" in w for w in m.warnings))

    def test_wide_spacing_flags_ambiguous(self):
        # 0.5 m spacing is wider than lambda/2 at 2.4 GHz -> aliased angle
        ph = phase_for_bearing(15.0, 0.5, 2.4e9)
        a, b = wavefront(self.fs, 1 << 16, 150e3, ph)
        m = df.measure(a, b, self.fs, 2.4e9, 0.5, cal_offset_deg=0.0)
        self.assertEqual(m.verdict, df.AMBIGUOUS_SPACING)
        self.assertTrue(any("aliases" in w for w in m.warnings))

    def test_plain_language_present(self):
        m = self._one()
        self.assertTrue(m.plain)
        self.assertIn("mirror", m.plain.lower())

    def _one(self):
        ph = phase_for_bearing(25.0, 0.3, 433.9e6)
        a, b = wavefront(self.fs, 1 << 16, 150e3, ph)
        return df.measure(a, b, self.fs, 433.9e6, 0.3, cal_offset_deg=0.0)


class TestToDict(unittest.TestCase):
    def test_serialisable_shape(self):
        ph = phase_for_bearing(10.0, 0.3, 433.9e6)
        a, b = wavefront(4e6, 1 << 16, 150e3, ph)
        d = df.measure(a, b, 4e6, 433.9e6, 0.3, cal_offset_deg=0.0).to_dict()
        for k in ("verdict", "plain", "bearing_deg", "mirror_deg", "coherence",
                  "calibrated", "max_unambiguous_mhz", "warnings"):
            self.assertIn(k, d)


if __name__ == "__main__":
    unittest.main()
