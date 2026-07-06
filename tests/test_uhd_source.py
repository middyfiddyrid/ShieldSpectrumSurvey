"""Unit tests for the B220 Mini / UHD source — ctypes struct layout, the
B220Source radio-binding hooks, dual-channel module mapping, and an
end-to-end stepped sweep driven by a SYNTHETIC UHDRadio (no hardware, no
uhd.dll). The hardware path is exercised separately by
tests/b220_live_check.py against the real unit.
"""

import ctypes
import threading
import unittest

import numpy as np

from survey.sweep.device import DeviceError
from survey.sweep.uhd_source import (B220Source, _StreamArgs, _StreamCmd,
                                     _TuneRequest, _TuneResult)


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


B220_FAST = {
    "sample_rate": 5_000_000, "capture_samples": 8192, "fft_size": 512,
    "sweep_bins": 600, "settle_seconds": 0.0, "keep_fraction": 0.8,
    "overlap": 0.5, "dc_notch_bins": 2, "cal_offset_db": 36.0,
    "freq_min_hz": 70_000_000, "freq_max_hz": 6_000_000_000,
    "gain_mode": "manual", "gain_db": 60.0, "antenna": "RX2",
}
# a DIFFERENT pluto section proves the CFG_SECTION split: b220 must never
# read these.
CFG = FakeCfg({"b220": dict(B220_FAST),
               "pluto": {"sample_rate": 999, "cal_offset_db": -30.0}})


class FakeUHDRadio:
    """Stands in for uhd_source.UHDRadio: tracks config calls and synthesises
    noise + planted carriers around the current LO."""

    def __init__(self, carriers_hz=(), noise=1e-3, gain_max=76.0,
                 num_channels=2, seed=0):
        self.carriers = list(carriers_hz)
        self.noise = noise
        self.gain_max = gain_max
        self.n_chan = num_channels
        self.h = ctypes.c_void_p(1)
        self.lo = {0: 0.0, 1: 0.0}
        self.rate = {0: 0.0, 1: 0.0}
        self.bw = {0: 0.0, 1: 0.0}
        self.gain = {0: None, 1: None}
        self.agc = {0: None, 1: None}
        self.antenna = {0: None, 1: None}
        self.calls = []                 # (method, chan) audit trail
        self.streaming = False
        self._rng = np.random.default_rng(seed)

    def mboard_name(self):
        return "B210"

    def num_rx_channels(self):
        return self.n_chan

    def set_rate(self, hz, chan):
        self.rate[chan] = float(hz)
        self.calls.append(("set_rate", chan))

    def set_bandwidth(self, hz, chan):
        self.bw[chan] = float(hz)

    def set_gain(self, db, chan):
        if db > self.gain_max:
            raise DeviceError(f"gain {db} out of range")
        self.gain[chan] = float(db)
        self.calls.append(("set_gain", chan))

    def set_agc(self, enable, chan):
        self.agc[chan] = bool(enable)

    def set_antenna(self, name, chan):
        self.antenna[chan] = name

    def set_freq(self, hz, chan):
        if not (50e6 <= hz <= 6.1e9):
            raise DeviceError("tune out of range")
        self.lo[chan] = float(hz)
        return float(hz)

    def _make_iq(self, n, chan):
        fs = self.rate[chan] or 5e6
        t = np.arange(n) / fs
        x = self.noise * (self._rng.standard_normal(n)
                          + 1j * self._rng.standard_normal(n))
        for c in self.carriers:
            off = c - self.lo[chan]
            if abs(off) < fs / 2:
                x = x + 0.5 * np.exp(2j * np.pi * off * t)
        return x.astype(np.complex64)

    def capture(self, nsamps, chan, timeout_s=5.0):
        self.calls.append(("capture", chan))
        return self._make_iq(int(nsamps), chan)

    def start_continuous(self, chan):
        self.streaming = True
        self.calls.append(("start_continuous", chan))

    def read_continuous(self, n_block, timeout_s=2.0):
        return self._make_iq(int(n_block), 0)

    def stop_continuous(self):
        self.streaming = False
        self.calls.append(("stop_continuous", None))

    def close(self):
        self.h = ctypes.c_void_p()


class FakeB220(B220Source):
    def __init__(self, cfg, radio=None):
        super().__init__(cfg)
        self._radio = radio or FakeUHDRadio()

    def _open_radio(self):
        self.sdr = self._radio
        self.info.model = f"{self.sdr.mboard_name()} (B220 Mini, 2RX)"
        self._apply_channel_count(self.sdr.num_rx_channels())
        for ch in range(self.channels):
            self.sdr.set_antenna(self._antenna, ch)


class TestStructLayout(unittest.TestCase):
    """The C-ABI structs must match uhd's C API exactly — a silent field/
    padding drift corrupts every call. Sizes pinned for 64-bit MSVC/SysV."""

    def test_tune_request_size(self):
        self.assertEqual(ctypes.sizeof(_TuneRequest), 48)

    def test_tune_result_size(self):
        self.assertEqual(ctypes.sizeof(_TuneResult), 40)

    def test_stream_args_size(self):
        self.assertEqual(ctypes.sizeof(_StreamArgs), 40)

    def test_stream_cmd_size(self):
        self.assertEqual(ctypes.sizeof(_StreamCmd), 40)

    def test_tune_request_fields(self):
        req = _TuneRequest(target_freq=1e9, rf_freq_policy=65, rf_freq=0.0,
                           dsp_freq_policy=65, dsp_freq=0.0, args=None)
        self.assertEqual(req.target_freq, 1e9)
        self.assertEqual(req.rf_freq_policy, 65)


class TestConfigSection(unittest.TestCase):
    def test_reads_b220_section_not_pluto(self):
        src = FakeB220(CFG)
        self.assertEqual(src._fs, 5_000_000)          # b220 value, not pluto's 999
        self.assertEqual(src._cal_offset, 36.0)       # not pluto's -30
        self.assertEqual(src.CFG_SECTION, "b220")

    def test_pluto_section_untouched(self):
        from survey.sweep.sdr_source import AD936xSource
        self.assertEqual(AD936xSource.CFG_SECTION, "pluto")


class TestModuleMapping(unittest.TestCase):
    def setUp(self):
        self.src = FakeB220(CFG)
        self.src.connect()

    def test_reports_two_modules(self):
        sides = [m.side for m in self.src.info.modules]
        self.assertEqual(sides, ["right", "left"])
        for m in self.src.info.modules:
            self.assertLessEqual(m.min_mhz, 71)
            self.assertGreaterEqual(m.max_mhz, 5999)

    def test_select_module_maps_sides_to_channels(self):
        self.src.select_module("right")
        self.assertEqual(self.src._chan, 0)
        self.src.select_module("left")
        self.assertEqual(self.src._chan, 1)
        self.src.select_module("nonsense")
        self.assertEqual(self.src._chan, 0)

    def test_channel_switch_reasserts_config(self):
        self.src.select_module("left")
        radio = self.src._radio
        self.assertEqual(radio.rate[1], self.src._fs)
        self.assertEqual(radio.gain[1], 60.0)

    def test_antenna_set_on_both_channels(self):
        radio = self.src._radio
        self.assertEqual(radio.antenna[0], "RX2")
        self.assertEqual(radio.antenna[1], "RX2")

    def test_single_channel_radio_downgrades(self):
        radio = FakeUHDRadio(num_channels=1)
        src = FakeB220(CFG, radio)
        try:
            src.connect()
            self.assertEqual(len(src.info.modules), 1)
            self.assertTrue(any("RX channel" in w for w in src.info.warnings))
        finally:
            type(src).channels = 2   # class attr — restore for other tests


class TestSweepAndStream(unittest.TestCase):
    def test_read_sweep_finds_carrier_on_each_channel(self):
        radio = FakeUHDRadio(carriers_hz=[433.9e6])
        src = FakeB220(CFG, radio)
        src.connect()
        for side, chan in (("right", 0), ("left", 1)):
            src.select_module(side)
            src.set_range(430.0, 438.0)
            frame = src.read_sweep()
            freqs = np.array(frame.freqs_mhz)
            amps = np.array(frame.amps_dbm)
            peak_mhz = freqs[int(np.argmax(amps))]
            self.assertAlmostEqual(peak_mhz, 433.9, delta=0.2)
            # every capture in this sweep must have used the selected channel
            caps = [c for c in radio.calls if c[0] == "capture"]
            self.assertEqual(caps[-1][1], chan)

    def test_capture_iq_normalised(self):
        radio = FakeUHDRadio(carriers_hz=[100e6])
        src = FakeB220(CFG, radio)
        src.connect()
        iq = src.capture_iq(100e6, 2e6, 4096)
        self.assertEqual(iq.dtype, np.complex64)
        # fc32 is already ±1: a 0.5-amplitude carrier must NOT get re-divided
        self.assertGreater(float(np.max(np.abs(iq))), 0.3)

    def test_stream_iq_lifecycle(self):
        radio = FakeUHDRadio(carriers_hz=[100e6])
        src = FakeB220(CFG, radio)
        src.connect()
        stop = threading.Event()
        blocks = []
        for blk in src.stream_iq(100e6, 1e6, 2048, stop):
            blocks.append(blk)
            if len(blocks) >= 3:
                stop.set()
        self.assertEqual(len(blocks), 3)
        self.assertTrue(any(c[0] == "start_continuous" for c in radio.calls))
        self.assertTrue(any(c[0] == "stop_continuous" for c in radio.calls))
        self.assertFalse(radio.streaming)
        # sweep config restored after the stream ends
        self.assertEqual(radio.rate[0], src._fs)


class TestGainPolicy(unittest.TestCase):
    def test_gain_backoff_never_kills_connect(self):
        radio = FakeUHDRadio(gain_max=55.0)   # requested 60 is out of range
        src = FakeB220(CFG, radio)
        src.connect()                          # must not raise
        self.assertEqual(radio.gain[0], 50.0)  # backed off to a safe value

    def test_agc_mode(self):
        cfg = FakeCfg({"b220": dict(B220_FAST, gain_mode="agc")})
        radio = FakeUHDRadio()
        src = FakeB220(cfg, radio)
        src.connect()
        self.assertTrue(radio.agc[0])


class TestFactory(unittest.TestCase):
    def test_open_device_routes_b220(self):
        """open_device('b220') must reach B220Source (import path sanity —
        a real connect needs uhd.dll, so expect DeviceError w/o hardware,
        never ImportError/AttributeError)."""
        from survey.sweep.device import open_device
        cfg = FakeCfg({"b220": dict(B220_FAST, uhd_dir=r"Z:\nonexistent"),
                       "device": {}})
        with self.assertRaises(DeviceError):
            open_device(cfg, source="b220")


if __name__ == "__main__":
    unittest.main()
