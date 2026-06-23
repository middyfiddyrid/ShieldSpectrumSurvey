"""Unit tests for the AD936x/Pluto SDR source — the pure DSP (Welch PSD, DC
notch, sub-step planning, stitch) and an end-to-end stepped sweep driven by a
SYNTHETIC radio (no hardware). The hardware path is exercised separately by
tests/pluto_live_check.py against a real Pluto.
"""

import unittest

import numpy as np

from survey.sweep.device import SweepFrame
from survey.sweep.sdr_source import (AD936xSource, notch_dc, plan_substeps,
                                     stitch_psd, welch_psd)


class FakeCfg:
    def __init__(self, d):
        self.d = d

    def get(self, *keys, default=None):
        cur = self.d
        for k in keys:
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        return cur


# small, fast SDR config for synthetic tests
FAST = FakeCfg({"pluto": {
    "sample_rate": 5_000_000, "capture_samples": 8192, "fft_size": 512,
    "sweep_bins": 600, "settle_seconds": 0.0, "keep_fraction": 0.8,
    "overlap": 0.5, "dc_notch_bins": 2, "cal_offset_db": -30.0,
    "freq_min_hz": 325_000_000, "freq_max_hz": 3_800_000_000,
}})


class FakeAD936x(AD936xSource):
    """Synthetic radio: _read_iq() returns noise + a complex exponential for each
    planted RF carrier that falls inside the current tune's instantaneous band,
    plus an optional DC term standing in for LO leakage."""

    def __init__(self, cfg, carriers_hz, dc_leak=0.0, noise=1e-3, seed=0):
        super().__init__(cfg)
        self.carriers = carriers_hz
        self.dc_leak = dc_leak
        self.noise = noise
        self._lo = 0.0
        self._rng = np.random.default_rng(seed)
        self.sdr = object()

    def _open_radio(self):
        self.sdr = object()

    def _set_lo(self, hz):
        self._lo = float(hz)

    def _set_sample_rate(self, hz):
        pass

    def _set_rf_bandwidth(self, hz):
        pass

    def _set_buffer_size(self, n):
        pass

    def _set_gain(self, mode, db):
        pass

    def _flush_buffer(self):
        pass

    def _read_iq(self):
        n = self._nsamp
        t = np.arange(n) / self._fs
        x = self.noise * (self._rng.standard_normal(n)
                          + 1j * self._rng.standard_normal(n))
        x = x + self.dc_leak  # LO leakage sits at baseband DC
        for c in self.carriers:
            off = c - self._lo
            if abs(off) <= self._fs / 2:
                x = x + np.exp(2j * np.pi * off * t)
        return x.astype(np.complex64)


class TestWelchPsd(unittest.TestCase):
    def test_tone_lands_in_right_bin(self):
        fs, n, f0 = 1_000_000.0, 8192, 100_000.0
        t = np.arange(n) / fs
        x = np.exp(2j * np.pi * f0 * t)
        freqs, psd = welch_psd(x, fs, nperseg=512)
        peak_hz = freqs[int(np.argmax(psd))]
        self.assertLess(abs(peak_hz - f0), 2 * fs / 512)

    def test_dc_at_center(self):
        fs, n = 1_000_000.0, 4096
        x = np.ones(n, dtype=np.complex64)  # pure DC
        freqs, psd = welch_psd(x, fs, nperseg=512)
        self.assertEqual(int(np.argmax(psd)), psd.size // 2)


class TestNotchDc(unittest.TestCase):
    def test_spike_removed(self):
        freqs = np.linspace(-0.5, 0.5, 513)
        psd = np.full(513, -100.0)
        c = int(np.argmin(np.abs(freqs)))
        psd[c] = 0.0  # a 100 dB LO spike
        out = notch_dc(freqs, psd, half_bins=2)
        self.assertLess(out[c], -90.0)         # spike gone
        self.assertAlmostEqual(out[0], -100.0)  # edges untouched


class TestPlanSubsteps(unittest.TestCase):
    def test_covers_range_with_overlap(self):
        lo, hi, bw = 2_400e6, 2_500e6, 5e6
        centers = plan_substeps(lo, hi, bw, keep_fraction=0.8, overlap=0.25)
        keep_bw = bw * 0.8
        self.assertLessEqual(centers[0] - keep_bw / 2, lo + 1)   # starts at lo
        self.assertGreaterEqual(centers[-1] + keep_bw / 2, hi)   # reaches hi
        step = centers[1] - centers[0]
        self.assertLess(step, keep_bw)                           # windows overlap

    def test_bad_bandwidth_raises(self):
        with self.assertRaises(ValueError):
            plan_substeps(0, 100e6, 0.0)


class TestStitch(unittest.TestCase):
    def test_overlap_and_gap_fill(self):
        grid = np.linspace(0, 100, 101)
        f1 = np.linspace(0, 60, 61)
        p1 = np.full(61, -100.0)
        p1[30] = -10.0                       # a tone at 30
        f2 = np.linspace(40, 100, 61)
        p2 = np.full(61, -100.0)
        out = stitch_psd(grid, [(f1, p1), (f2, p2)])
        self.assertEqual(out.size, grid.size)
        self.assertFalse(np.any(np.isnan(out)))
        self.assertGreater(out[30], -40.0)   # tone preserved


class TestSteppedSweepEndToEnd(unittest.TestCase):
    def _sweep(self, source, lo_mhz, hi_mhz):
        source.connect()
        source.set_range(lo_mhz, hi_mhz)
        frame = source.read_sweep()
        self.assertIsInstance(frame, SweepFrame)
        return np.asarray(frame.freqs_mhz), np.asarray(frame.amps_dbm)

    def test_carrier_recovered_and_units(self):
        src = FakeAD936x(FAST, carriers_hz=[2_450e6], dc_leak=2.0)
        freqs, amps = self._sweep(src, 2400.0, 2500.0)
        # contiguous trace in MHz, monotonic increasing
        self.assertTrue(np.all(np.diff(freqs) > 0))
        self.assertAlmostEqual(freqs[0], 2400.0, places=3)
        self.assertAlmostEqual(freqs[-1], 2500.0, places=3)
        peak_mhz = freqs[int(np.argmax(amps))]
        self.assertLess(abs(peak_mhz - 2450.0), 1.0)  # carrier within 1 MHz

    def test_no_false_peak_on_lo_leakage(self):
        # strong LO leakage, NO real carriers — the notch + overlap must keep the
        # trace flat (a TSCM sweep must not flag its own LO).
        src = FakeAD936x(FAST, carriers_hz=[], dc_leak=50.0, noise=1e-3)
        freqs, amps = self._sweep(src, 2400.0, 2500.0)
        spread = float(np.max(amps) - np.median(amps))
        self.assertLess(spread, 15.0)  # no tall DC spikes survived

    def test_carrier_exactly_on_a_tune_center_still_seen(self):
        # plant the carrier on a tune center (where its own DC notch sits); the
        # neighbouring overlapped segment must still recover it.
        # mirror the driver's own plan exactly (fs + keep/overlap from FAST)
        fs = FAST.get("pluto", "sample_rate")
        centers = plan_substeps(2400e6, 2500e6, fs, 0.8, 0.5)
        target = centers[len(centers) // 2]
        src = FakeAD936x(FAST, carriers_hz=[target], dc_leak=5.0)
        freqs, amps = self._sweep(src, 2400.0, 2500.0)
        peak_mhz = freqs[int(np.argmax(amps))]
        self.assertLess(abs(peak_mhz - target / 1e6), 1.5)


if __name__ == "__main__":
    unittest.main()
