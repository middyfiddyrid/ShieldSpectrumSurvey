"""Subprocess radio-worker isolation (survey/sweep/worker_proxy.py).

The 2026-07-12 "STILL THE REAL FIX": native SDR drivers move into a worker
subprocess so a driver fastfail kills only the worker. These tests run fake
devices INSIDE real spawned workers (tests/workerfakes.py via the env-gated
"test:<file>:<Class>" source) and prove:

  * full BaseDevice round-trip over the pipe (frames, info mutation, RBW)
  * bulk IQ over shared memory (capture, dual capture, live streaming)
  * stop-event translation + generator abandonment leave the worker healthy
  * a worker death mid-call / mid-stream surfaces as DeviceError (the ONLY
    retryable type the runner's reconnect rails accept) and is_alive()->False
  * config snapshot + runtime overrides reach the child
  * the hasattr capability surface mirrors the wrapped concrete class
  * SweepController reconnect + dead-monitor eviction heal crashed workers
"""

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from survey.config import Config  # noqa: E402
from survey.sweep.device import DeviceError, open_device  # noqa: E402
from survey.sweep.worker_proxy import SubprocessDevice  # noqa: E402

FAKES = str(Path(__file__).resolve().parent / "workerfakes.py")


def src(cls_name: str) -> str:
    return f"test:{FAKES}:{cls_name}"


def make_cfg() -> Config:
    cfg = Config(os.path.join(tempfile.mkdtemp(), "config.yaml"))
    cfg.data["device"]["isolate_workers"] = True
    # keep worker failures snappy in tests (prod default is a generous 300 s)
    cfg.data["device"]["worker_call_timeout_s"] = 30.0
    cfg.data["device"]["worker_connect_timeout_s"] = 60.0
    return cfg


class WorkerProxyCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["SWEEP_ALLOW_TEST_DEVICE"] = "1"

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("SWEEP_ALLOW_TEST_DEVICE", None)

    def setUp(self):
        self._devs = []

    def tearDown(self):
        for d in self._devs:
            try:
                d.close()
            except Exception:  # noqa: BLE001
                pass

    def open(self, cls_name: str, cfg=None):
        dev = open_device(cfg or make_cfg(), source=src(cls_name))
        self._devs.append(dev)
        return dev


class TestRoundTrip(WorkerProxyCase):
    def test_open_returns_proxy_with_live_info(self):
        dev = self.open("RoundTrip")
        self.assertIsInstance(dev, SubprocessDevice)
        self.assertTrue(dev.is_alive())
        self.assertEqual(dev.info.model, "RoundTrip")
        self.assertEqual(dev.info.modules[0].side, "right")
        d = dev.info.to_dict()
        self.assertEqual(d["model"], "RoundTrip")

    def test_select_module_mutation_visible_through_info(self):
        dev = self.open("RoundTrip")
        self.assertEqual(dev.info.modules[0].min_mhz, 100.0)
        dev.select_module("left")
        # module_ranges contract: the worker-side info mutation must be
        # observable immediately after the call returns
        self.assertEqual(dev.info.modules[0].min_mhz, 50.0)

    def test_read_sweep_frames_and_rbw(self):
        dev = self.open("RoundTrip")
        with self.assertRaises(DeviceError):
            dev.read_sweep()               # before set_range: device raises,
        self.assertTrue(dev.is_alive())    # ...worker survives a Python error
        self.assertEqual(dev.set_rbw_khz(10), 10.0)
        dev.set_range(300.0, 348.0)
        frame = dev.read_sweep(timeout_s=3.0)
        self.assertEqual(len(frame.freqs_mhz), 16)
        self.assertEqual(frame.freqs_mhz[0], 300.0)
        self.assertEqual(frame.amps_dbm[0], -70.0)   # -80 + rbw 10
        self.assertEqual(dev.info.last_sweep_seconds, 0.01)
        self.assertIsNone(dev.set_rbw_khz(None))

    def test_capture_iq_over_shared_memory(self):
        dev = self.open("RoundTrip")
        iq = dev.capture_iq(433e6, 2.4e6, 4096)
        self.assertEqual(iq.dtype, np.complex64)
        self.assertEqual(len(iq), 4096)
        self.assertAlmostEqual(float(iq[10].real), 10.0, places=4)
        self.assertAlmostEqual(float(iq[0].imag), 433.0, places=3)

    def test_capture_iq_dual_shape_and_rows(self):
        dev = self.open("RoundTrip")
        pair = dev.capture_iq_dual(433e6, 5e6, 1024)
        self.assertEqual(pair.shape, (2, 1024))
        np.testing.assert_allclose(pair[1].real, pair[0].real * 2)

    def test_config_snapshot_and_runtime_override_reach_worker(self):
        cfg = make_cfg()
        cfg.set_runtime_override(("pluto", "gain_db"), 33.5)
        dev = self.open("RoundTrip", cfg)
        self.assertEqual(dev.info.firmware, "gain=33.5")


class TestCapabilitySurface(WorkerProxyCase):
    def test_full_surface_mirrored(self):
        dev = self.open("RoundTrip")
        self.assertTrue(hasattr(dev, "stream_iq"))
        self.assertTrue(hasattr(dev, "capture_iq_dual"))
        self.assertTrue(hasattr(dev, "environment"))
        self.assertTrue(getattr(dev, "is_swept_sdr", False))
        self.assertEqual(getattr(dev, "CFG_SECTION", None), "pluto")
        self.assertEqual(getattr(dev, "channels", 1), 2)
        self.assertEqual(getattr(dev, "_fs", None), 20_000_000.0)

    def test_minimal_surface_not_inflated(self):
        dev = self.open("NoIQ")
        self.assertFalse(hasattr(dev, "stream_iq"))
        self.assertFalse(hasattr(dev, "capture_iq_dual"))
        self.assertFalse(hasattr(dev, "environment"))
        self.assertFalse(getattr(dev, "is_swept_sdr", False))
        # BaseDevice defines capture_iq (vacuously hasattr-True everywhere);
        # a non-overriding device must keep raising NotImplementedError
        self.assertTrue(hasattr(dev, "capture_iq"))
        with self.assertRaises(NotImplementedError):
            dev.capture_iq(100e6, 1e6, 16)
        frame = dev.read_sweep()
        self.assertEqual(frame.freqs_mhz, [1.0])

    def test_environment_setattr_forwards(self):
        dev = self.open("RoundTrip")
        dev.environment = {"phase": "ambient", "room": "kitchen"}
        self.assertEqual(dev.environment["phase"], "ambient")
        self.assertTrue(dev.is_alive())


class TestStreaming(WorkerProxyCase):
    def test_stream_blocks_then_stop(self):
        dev = self.open("RoundTrip")
        stop = threading.Event()
        blocks = []
        for iq in dev.stream_iq(100e6, 2.4e6, 256, stop):
            blocks.append(iq)
            if len(blocks) >= 5:
                stop.set()
        self.assertGreaterEqual(len(blocks), 5)
        for i, b in enumerate(blocks):
            self.assertEqual(b.dtype, np.complex64)
            self.assertEqual(len(b), 256)
            self.assertEqual(float(b[0].real), float(i))   # ordered, no loss
        # worker healthy + its stream finally ran (info piggyback on next RPC)
        dev.set_range(100.0, 200.0)
        self.assertIsNotNone(dev.read_sweep())
        self.assertIn("stream-finalized", dev.info.warnings)

    def test_stream_abandoned_by_break(self):
        dev = self.open("RoundTrip")
        stop = threading.Event()
        got = 0
        for _ in dev.stream_iq(100e6, 2.4e6, 128, stop):
            got += 1
            if got == 2:
                break                       # GeneratorExit path
        self.assertEqual(got, 2)
        dev.set_range(100.0, 200.0)         # pipe must be clean afterwards
        self.assertIsNotNone(dev.read_sweep())

    def test_stop_from_other_thread(self):
        dev = self.open("RoundTrip")
        stop = threading.Event()
        threading.Timer(0.5, stop.set).start()
        n = 0
        t0 = time.time()
        for _ in dev.stream_iq(100e6, 2.4e6, 64, stop):
            n = n + 1
        self.assertGreater(n, 0)
        self.assertLess(time.time() - t0, 10.0)   # ended promptly after stop


class TestCrashRecovery(WorkerProxyCase):
    def test_crash_mid_call_raises_deviceerror(self):
        dev = self.open("CrashOnRead")
        dev.set_range(100.0, 200.0)
        with self.assertRaises(DeviceError):
            dev.read_sweep()
        self.assertFalse(dev.is_alive())
        with self.assertRaises(DeviceError):
            dev.set_range(100.0, 200.0)     # post-mortem calls fail FAST
        dev.close()                          # and close never raises

    def test_crash_mid_stream_raises_deviceerror(self):
        dev = self.open("CrashStream")
        stop = threading.Event()
        blocks = []
        with self.assertRaises(DeviceError):
            for iq in dev.stream_iq(100e6, 2.4e6, 128, stop):
                blocks.append(iq)
        self.assertGreaterEqual(len(blocks), 1)   # delivered before death
        self.assertFalse(dev.is_alive())

    def test_worker_open_failure_is_deviceerror(self):
        with self.assertRaises(DeviceError):
            open_device(make_cfg(), source=src("ConnectFails"))

    def test_fresh_proxy_after_crash_works(self):
        dev = self.open("CrashOnRead")
        dev.set_range(100.0, 200.0)
        with self.assertRaises(DeviceError):
            dev.read_sweep()
        # the app-level recovery: open a NEW handle on the same source
        dev2 = self.open("RoundTrip")
        self.assertTrue(dev2.is_alive())
        dev2.set_range(100.0, 200.0)
        self.assertIsNotNone(dev2.read_sweep())


class TestRunnerIntegration(WorkerProxyCase):
    def _controller(self):
        from survey.sweep.runner import SweepController
        cfg = make_cfg()
        cfg.data["pluto"]["concurrent_monitor"] = True
        c = SweepController(None, cfg)
        # keep the Pluto auto-probe INSIDE its failure cooldown for the whole
        # test: ensure_monitor must never open real hardware from a unit test
        # (this laptop often has the actual Pluto plugged in — it connected!)
        c._pluto_auto_fail_ts = time.time() + 3600.0
        self.addCleanup(lambda: [d.close() for d in list(c.devices.values())])
        return c

    def test_ensure_device_respawns_after_worker_death(self):
        c = self._controller()
        c.connect_device(source=src("RoundTrip"))
        dev1 = c.device
        self.assertIsInstance(dev1, SubprocessDevice)
        self._devs.append(dev1)
        dev1._proc.terminate()              # simulated native crash
        dev1._proc.join(timeout=5.0)
        self.assertFalse(dev1.is_alive())
        c._ensure_device()                  # the existing reconnect rail
        self._devs.append(c.device)
        self.assertIsNot(c.device, dev1)
        self.assertTrue(c.device.is_alive())
        self.assertEqual(c.active_source, src("RoundTrip").lower())

    def test_dead_monitor_evicted_and_target_falls_back(self):
        c = self._controller()
        c.connect_device(source=src("RoundTrip"))
        sweeper = c.device
        self._devs.append(sweeper)
        c.connect_device(source=src("NoIQ"), keep_other=True)
        mon_src = src("NoIQ").lower()
        self._devs.append(c.devices[mon_src])
        # the second radio is the monitor (non-pluto fallback role)
        _, _, _, msrc = c.monitor_target()
        self.assertEqual(msrc, mon_src)
        # kill the monitor's worker; the next monitor ACTION must evict it
        c.devices[mon_src]._proc.terminate()
        c.devices[mon_src]._proc.join(timeout=5.0)
        dev, lock, stop_evt, msrc2 = c.ensure_monitor()
        self.assertNotIn(mon_src, c.devices)
        # degraded-but-honest: falls back to the sweeper
        self.assertEqual(msrc2, c.active_source)
        self.assertIs(dev, sweeper)
        self.assertTrue(sweeper.is_alive())   # eviction never touches it


if __name__ == "__main__":
    unittest.main()
