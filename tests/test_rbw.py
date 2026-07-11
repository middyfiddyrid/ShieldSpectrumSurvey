"""Operator-adjustable RBW (2026-07-11 ask).

The SDR sources implement set_rbw_khz/grid_points/predicted_rbw_khz: the Welch
FFT length (analysis bin = fs/nperseg) and the output-grid density move
together, and the effective RBW is the coarser of the two. The RF Explorer
inherits the BaseDevice no-op (its 112-point sweep is firmware-fixed).
"""

import unittest

import numpy as np

from survey.sweep.device import BaseDevice
from survey.sweep.sdr_source import AD936xSource, stitch_psd


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


CFG = FakeCfg({"pluto": {
    "sample_rate": 5_000_000, "capture_samples": 8192, "fft_size": 512,
    "sweep_bins": 600, "settle_seconds": 0.0, "keep_fraction": 0.8,
    "overlap": 0.5, "dc_notch_bins": 2, "cal_offset_db": -30.0,
}})


class NoRadio(AD936xSource):
    """DSP-only instance: never opens hardware (set_range/grid math only)."""

    def _open_radio(self):
        pass


class TestRbw(unittest.TestCase):
    def setUp(self):
        self.dev = NoRadio(CFG)

    def test_default_grid_and_prediction(self):
        # no override: legacy 1 point/kHz capped at sweep_bins
        self.assertEqual(self.dev.grid_points(10e6), 600)
        # effective RBW = coarser of fs/fft_size (9.77 kHz) and grid (16.7 kHz)
        self.assertAlmostEqual(self.dev.predicted_rbw_khz(10.0),
                               10e6 / 599 / 1e3, places=1)

    def test_override_sets_fft_and_grid(self):
        achieved = self.dev.set_rbw_khz(10.0)
        # nperseg = nearest pow2 to fs/rbw = 2**round(log2(500)) = 512
        self.assertEqual(self.dev._nperseg, 512)
        self.assertAlmostEqual(achieved, 5e6 / 512 / 1e3, places=2)
        # grid now 1 point per RBW step, no longer capped at sweep_bins
        self.assertEqual(self.dev.grid_points(10e6), 1001)
        self.assertAlmostEqual(self.dev.predicted_rbw_khz(10.0), 10.0, places=1)
        # set_range builds the denser grid
        self.dev.set_range(100.0, 110.0)
        self.assertEqual(self.dev._grid_hz.size, 1001)

    def test_override_clamps_to_averaging_floor(self):
        # absurdly fine request: nperseg clamps to capture_samples//4 so every
        # capture still yields >=4 Welch segments
        self.dev.set_rbw_khz(0.001)
        self.assertLessEqual(self.dev._nperseg, 8192 // 4)

    def test_none_restores_config_default(self):
        self.dev.set_rbw_khz(5.0)
        self.assertIsNone(self.dev.set_rbw_khz(None))
        self.assertEqual(self.dev._nperseg, 512)          # cfg fft_size
        self.assertIsNone(self.dev._rbw_hz)
        self.assertEqual(self.dev.grid_points(10e6), 600)  # legacy grid again

    def test_grid_cost_cap(self):
        # a fine RBW over a huge span must not explode host cost
        self.dev.set_rbw_khz(1.0)
        self.assertLessEqual(self.dev.grid_points(200e6), self.dev._RBW_GRID_MAX)

    def test_rfe_is_a_noop(self):
        base = BaseDevice()
        self.assertIsNone(base.set_rbw_khz(25.0))
        self.assertIsNone(base.predicted_rbw_khz(75.0))
        self.assertIsNone(base.predicted_rbw_khz(75.0, rbw_khz=10.0))

    def test_coarse_grid_never_hides_a_narrow_carrier(self):
        # adversarial-review CRITICAL: with the grid coarser than the analysis
        # bins, np.interp point-sampling made a strong CW carrier vanish when it
        # fell between grid points. The peak-preserving stitch must keep it.
        fine = np.arange(100e6, 106e6, 5e3)          # 5 kHz analysis bins
        psd = np.full(fine.size, -100.0)
        k = int(0.4999 * fine.size)                  # a carrier mid-span
        psd[k] = -30.0
        # coarse 500 kHz grid deliberately MISALIGNED with the carrier bin
        grid = np.linspace(100e6 + 137e3, 106e6 - 137e3, 12)
        out = stitch_psd(grid, [(fine, psd)])
        self.assertGreater(float(out.max()), -40.0,
                           "narrow carrier lost by coarse-grid decimation")
        # and the floor is still a floor (max-of-noise, not the carrier smeared)
        self.assertLess(float(np.median(out)), -90.0)

    def test_fine_grid_keeps_interp_floor(self):
        # grid finer than the analysis bins -> the old interp path (honest floor)
        fine = np.arange(100e6, 101e6, 20e3)
        psd = np.full(fine.size, -100.0)
        grid = np.linspace(100e6, 101e6, 1001)       # 1 kHz grid
        out = stitch_psd(grid, [(fine, psd)])
        self.assertAlmostEqual(float(np.median(out)), -100.0, places=1)

    def test_hypothetical_preview_never_mutates(self):
        # /api/plan previews an override on the LIVE device while a sweep may be
        # running — it must not touch _nperseg/_rbw_hz (read_sweep re-reads them
        # every frame)
        before = (self.dev._nperseg, self.dev._rbw_hz)
        pred = self.dev.predicted_rbw_khz(10.0, rbw_khz=10.0)
        self.assertAlmostEqual(pred, 10.0, places=1)
        self.assertEqual((self.dev._nperseg, self.dev._rbw_hz), before)


if __name__ == "__main__":
    unittest.main()
