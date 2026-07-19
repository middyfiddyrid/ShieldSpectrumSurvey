"""In-WORKER fake devices for tests/test_worker_proxy.py.

Loaded by the radio worker subprocess via the env-gated
"test:<this file>:<ClassName>" source (survey/sweep/worker_proxy.py
_open_real), so everything here executes in the CHILD process. os._exit
calls below simulate a native driver fastfail: the worker dies with no
reply, no cleanup — exactly what a 0xc0000409 in uhd.dll looks like from
the parent's side of the pipe."""

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from survey.sweep.device import (BaseDevice, DeviceError, DeviceInfo,  # noqa: E402
                                 ModuleInfo, SweepFrame)


class RoundTrip(BaseDevice):
    """Full-surface fake: exercises every proxy path (frames, IQ capture,
    dual capture, streaming, environment attr, info mutation)."""

    is_swept_sdr = True
    CFG_SECTION = "pluto"
    channels = 2
    _fs = 20_000_000.0

    def __init__(self, cfg):
        self.cfg = cfg
        self.info = DeviceInfo(driver="fake", model="RoundTrip")
        self.info.modules = [ModuleInfo("right", "Fake", 100.0, 6000.0)]
        # smuggle a config value out through info so tests can verify the
        # snapshot + runtime overrides really reached the worker process
        self.info.firmware = f"gain={cfg.get('pluto', 'gain_db', default=0)}"
        self.environment = {"phase": "interior", "room": ""}
        self._rbw = None
        self._range = None
        self._closed = False

    def connect(self):
        return self.info

    def is_alive(self):
        return not self._closed

    def select_module(self, side: str):
        # module_ranges contract: select_module mutates info in place and the
        # caller re-reads it immediately
        self.info.modules[0].min_mhz = 50.0 if side == "left" else 100.0

    def set_range(self, start_mhz, end_mhz):
        self._range = (float(start_mhz), float(end_mhz))

    def set_calculator(self, mode_name):
        return None

    def set_rbw_khz(self, rbw_khz):
        self._rbw = float(rbw_khz) if rbw_khz else None
        return self._rbw

    def predicted_rbw_khz(self, span_mhz, rbw_khz=None):
        return float(rbw_khz or self._rbw or 42.0)

    def read_sweep(self, timeout_s: float = 5.0):
        if self._range is None:
            raise DeviceError("read_sweep before set_range")
        lo, hi = self._range
        freqs = [float(f) for f in np.linspace(lo, hi, 16)]
        amps = [-80.0 + (self._rbw or 0.0)] * 16
        self.info.last_sweep_seconds = 0.01
        return SweepFrame(freqs, amps, time.time())

    def capture_iq(self, center_hz, sample_rate, n_samples):
        n = int(n_samples)
        return (np.arange(n, dtype=np.float32)
                + 1j * np.float32(center_hz / 1e6)).astype(np.complex64)

    def capture_iq_dual(self, center_hz, sample_rate, n_samples):
        a = np.arange(int(n_samples), dtype=np.float32).astype(np.complex64)
        return np.stack([a, a * 2])

    def stream_iq(self, center_hz, sample_rate, n_block, stop):
        try:
            i = 0
            while not stop.is_set():
                yield np.full(int(n_block), float(i),
                              dtype=np.complex64)
                i += 1
        finally:
            # worker-side finalize (stands in for the drivers' restore-sweep-
            # config finally); visible to the parent via the info piggyback
            self.info.warnings.append("stream-finalized")

    def close(self):
        self._closed = True


class NoIQ(BaseDevice):
    """Minimal fake WITHOUT stream_iq / real capture_iq / environment /
    is_swept_sdr — proves the proxy's hasattr surface mirrors the concrete
    class instead of advertising everything."""

    def __init__(self, cfg):
        self.info = DeviceInfo(driver="fake", model="NoIQ")
        self.info.modules = [ModuleInfo("right", "Fake", 100.0, 200.0)]

    def connect(self):
        return self.info

    def read_sweep(self, timeout_s: float = 5.0):
        return SweepFrame([1.0], [-80.0], time.time())

    def close(self):
        pass


class ConnectFails(RoundTrip):
    def connect(self):
        raise DeviceError("no such radio (fake)")


class CrashOnRead(RoundTrip):
    def read_sweep(self, timeout_s: float = 5.0):
        os._exit(57)          # simulated native fastfail mid-call


class CrashStream(RoundTrip):
    def stream_iq(self, center_hz, sample_rate, n_block, stop):
        yield np.zeros(int(n_block), dtype=np.complex64)
        yield np.ones(int(n_block), dtype=np.complex64)
        os._exit(58)          # simulated native fastfail mid-stream
