"""Device-lab live loop — session lifecycle against a fake radio (no hardware)."""

import threading
import time
import unittest

import numpy as np

from survey.lab_live import (LabLiveError, LiveSession, WideFindSession,
                             group_harmonics)


class FakeIQDevice:
    """IQ-capable fake: strong tone at 433.92 MHz, silence elsewhere."""

    def __init__(self):
        self.selected = []

    def select_module(self, side):
        self.selected.append(side)

    def capture_iq(self, center_hz, sample_rate, n_samples):
        n = int(n_samples)
        rng = np.random.default_rng(42)
        iq = (rng.normal(0, 1e-4, n) + 1j * rng.normal(0, 1e-4, n)) \
            .astype(np.complex64)
        off = 433.92e6 - center_hz
        if abs(off) < sample_rate / 2 * 0.9:
            t = np.arange(n, dtype=np.float32) / sample_rate
            iq += (0.05 * np.exp(2j * np.pi * off * t)).astype(np.complex64)
        return iq


class FakeAmplitudeOnly:
    def capture_iq(self, *a):
        raise NotImplementedError("amplitude-only")


class FakeController:
    def __init__(self, dev):
        self._dev = dev
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def monitor_target(self):
        return self._dev, self._lock, self._stop, "pluto"


def _wait(cond, timeout=6.0):
    t_end = time.time() + timeout
    while time.time() < t_end:
        if cond():
            return True
        time.sleep(0.05)
    return False


class TestLiveSession(unittest.TestCase):
    def test_needs_freq(self):
        with self.assertRaises(LabLiveError):
            LiveSession(FakeController(FakeIQDevice()), "p1", None, [],
                        "right", 2_000_000)

    def test_loop_accumulates_and_harvests(self):
        dev = FakeIQDevice()
        s = LiveSession(FakeController(dev), "p1", 433.92, None,
                        "left", 2_000_000, n_samples=16384)
        s.start()
        try:
            self.assertTrue(_wait(lambda: s.grabs >= 5), s.error)
            snap = s.snapshot()
            self.assertTrue(snap["running"])
            self.assertGreaterEqual(len(snap["trace"]), 5)
            self.assertGreater(snap["snr_now"], 10)
            self.assertIn("peakhold", snap)
            rec = s.harvest()
            self.assertTrue(rec["live_loop"])
            self.assertGreater(rec["spectrum"]["snr_db"], 10)
            self.assertEqual(rec["center_mhz"], 433.92)
            # accumulators reset but the loop keeps running
            self.assertTrue(_wait(lambda: s.grabs >= 3), s.error)
        finally:
            s.stop()
        self.assertTrue(s.ended)
        self.assertIn("left", dev.selected)
        self.assertEqual(dev.selected[-1], "right")

    def test_scan_locks_on_energy(self):
        s = LiveSession(FakeController(FakeIQDevice()), "p1", None,
                        [315.0, 433.92], "right", 2_000_000, n_samples=16384)
        s.start()
        try:
            self.assertTrue(_wait(lambda: not s.scanning, timeout=10), s.error)
            self.assertEqual(s.locked_mhz, 433.92)
        finally:
            s.stop()

    def test_walk_requires_lock_then_scores(self):
        s = LiveSession(FakeController(FakeIQDevice()), "p1", 433.92, None,
                        "right", 2_000_000, n_samples=16384)
        s.start()
        try:
            self.assertTrue(_wait(lambda: s.grabs >= 3), s.error)
            s.start_walk(period_s=4.0)
            snap = s.snapshot()
            self.assertTrue(snap["walk"]["active"])
            self.assertIn(snap["walk"]["phase"], ("in", "out"))
            res = s.end_walk()
            self.assertIn("verdict", res)
            with self.assertRaises(LabLiveError):
                s.end_walk()
        finally:
            s.stop()

    def test_amplitude_only_errors(self):
        s = LiveSession(FakeController(FakeAmplitudeOnly()), "p1", 433.92,
                        None, "right", 2_000_000, n_samples=16384)
        s.start()
        self.assertTrue(_wait(lambda: s.ended))
        self.assertIn("amplitude-only", s.error or "")


class FakeHotDevice(FakeIQDevice):
    """Signal so strong it clips until the gain comes down. Amplitude scales
    with gain like a real front end; hard-clips at full scale ±1."""

    def __init__(self):
        super().__init__()
        self.gain_db = 64.0

    def set_gain(self, mode=None, db=None):
        if db is not None:
            self.gain_db = float(db)

    def capture_iq(self, center_hz, sample_rate, n_samples):
        n = int(n_samples)
        rng = np.random.default_rng(1)
        iq = (rng.normal(0, 1e-4, n) + 1j * rng.normal(0, 1e-4, n)) \
            .astype(np.complex64)
        t = np.arange(n, dtype=np.float32) / sample_rate
        off = 433.92e6 - center_hz
        if abs(off) < sample_rate / 2 * 0.9:
            amp = 0.5 * 10 ** ((self.gain_db - 40.0) / 20.0)
            iq += (amp * np.exp(2j * np.pi * off * t)).astype(np.complex64)
        return (np.clip(iq.real, -1, 1) + 1j * np.clip(iq.imag, -1, 1)) \
            .astype(np.complex64)


class FakeAlwaysHot:
    """Clips no matter what and offers no gain control → needs the pad."""

    def capture_iq(self, center_hz, sample_rate, n_samples):
        n = int(n_samples)
        return np.full(n, 0.99 + 0.99j, dtype=np.complex64)


class TestOverloadProtection(unittest.TestCase):
    def test_auto_gain_steps_down_until_clean(self):
        dev = FakeHotDevice()
        s = LiveSession(FakeController(dev), "p1", 433.92, None, "right",
                        2_000_000, n_samples=16384, initial_gain_db=64.0)
        s.start()
        try:
            self.assertTrue(_wait(lambda: s.grabs >= 4 and not s.clipping,
                                  timeout=10), s.error)
            self.assertLess(s.gain_db, 64.0)
            self.assertFalse(s.needs_pad)
            self.assertTrue(s.gain_events)
            snap = s.snapshot()
            self.assertIn("gain_db", snap)
            rec = s.harvest()
            self.assertTrue(rec["gain_stepped"])
            self.assertLess(rec["gain_db"], 64.0)
        finally:
            s.stop()
        # radio handed back at the configured gain, not the lab's stepped one
        self.assertEqual(dev.gain_db, 64.0)

    def test_needs_pad_when_uncontrollable(self):
        s = LiveSession(FakeController(FakeAlwaysHot()), "p1", 433.92, None,
                        "right", 2_000_000, n_samples=8192)
        s.start()
        try:
            self.assertTrue(_wait(lambda: s.needs_pad, timeout=8), s.error)
            with self.assertRaises(LabLiveError):
                s.start_walk()  # refuse the walk while overloaded
        finally:
            s.stop()


class FakeOnOffDevice:
    """Carrier at 433.92 only when dut_on — drives the wide-find passes."""

    def __init__(self):
        self.dut_on = False

    def select_module(self, side):
        pass

    def capture_iq(self, center_hz, sample_rate, n_samples):
        n = int(n_samples)
        rng = np.random.default_rng(3)
        iq = (rng.normal(0, 1e-4, n) + 1j * rng.normal(0, 1e-4, n)) \
            .astype(np.complex64)
        off = 433.92e6 - center_hz
        if self.dut_on and abs(off) < sample_rate / 2 * 0.9:
            t = np.arange(n, dtype=np.float32) / sample_rate
            iq += (0.03 * np.exp(2j * np.pi * off * t)).astype(np.complex64)
        return iq


class TestWideFind(unittest.TestCase):
    def test_group_harmonics(self):
        peaks = [{"freq_mhz": 433.92, "delta_db": 30.0},
                 {"freq_mhz": 867.85, "delta_db": 40.0},
                 {"freq_mhz": 1301.7, "delta_db": 12.0},
                 {"freq_mhz": 915.0, "delta_db": 20.0}]
        out = group_harmonics(peaks)
        by_f = {p["freq_mhz"]: p for p in out}
        self.assertTrue(by_f[433.92]["fundamental"])
        self.assertEqual(by_f[867.85]["harmonic_of_mhz"], 433.92)
        self.assertEqual(by_f[867.85]["n"], 2)
        self.assertEqual(by_f[1301.7]["n"], 3)
        self.assertTrue(by_f[915.0]["fundamental"])
        # sorted by delta desc, but the 2x harmonic is NOT called fundamental
        self.assertEqual(out[0]["freq_mhz"], 867.85)
        self.assertFalse(out[0]["fundamental"])

    def test_off_on_differential_finds_the_dut(self):
        dev = FakeOnOffDevice()
        dev.dut_on = True          # DUT still transmitting when START is pressed
        s = WideFindSession(FakeController(dev), "p1", (420.0, 450.0),
                            side="right", sample_rate=2_000_000,
                            n_samples=8192, k_grabs=1)
        s.start()
        try:
            # GATE 1: the run must PARK before measuring anything. It used to
            # start the baseline the instant START was pressed, which recorded
            # the reference while the device was still powered on and poisoned
            # the whole differential.
            self.assertTrue(_wait(lambda: s.phase == "await_off", timeout=30),
                            s.error)
            time.sleep(0.5)
            self.assertEqual(s.phase, "await_off",
                             "must keep waiting until the operator confirms")
            dev.dut_on = False     # operator powers it down, then confirms
            s.confirm_off()
            self.assertTrue(_wait(lambda: s.phase == "await_on", timeout=30),
                            s.error)
            # GATE 2: unchanged — wait for power-on
            dev.dut_on = True
            s.confirm_on()
            self.assertTrue(_wait(lambda: s.ended, timeout=30), s.error)
            self.assertIsNone(s.error)
            self.assertTrue(s.candidates)
            top = s.candidates[0]
            self.assertAlmostEqual(top["freq_mhz"], 433.92, delta=0.2)
            self.assertGreater(top["delta_db"], 8.0)
            self.assertTrue(top["fundamental"])
        finally:
            s.stop()

    def test_operator_gate_has_no_timeout(self):
        """The tech decides when it advances. A gate must never expire — an
        iPhone can take minutes to shut down and boot."""
        dev = FakeOnOffDevice()
        s = WideFindSession(FakeController(dev), "p1", (420.0, 450.0),
                            side="right", sample_rate=2_000_000,
                            n_samples=8192, k_grabs=1)
        s.start()
        try:
            self.assertTrue(_wait(lambda: s.phase == "await_off", timeout=30))
            time.sleep(2.0)
            self.assertEqual(s.phase, "await_off")
            self.assertIsNone(s.error, "a gate must not time the session out")
            snap = s.snapshot()
            self.assertTrue(snap["awaiting_operator"])
            self.assertIn("POWER THE DEVICE OFF", snap["title"])
            self.assertTrue(snap["action_label"])
        finally:
            s.stop()

    def test_pause_holds_the_run_without_losing_it(self):
        dev = FakeOnOffDevice()
        s = WideFindSession(FakeController(dev), "p1", (70.0, 1000.0),
                            side="right", sample_rate=2_000_000, n_samples=8192)
        s.start()
        try:
            self.assertTrue(_wait(lambda: s.phase == "await_off", timeout=30))
            s.confirm_off()
            self.assertTrue(_wait(lambda: s.phase == "pass_off", timeout=30))
            s.pause()
            self.assertTrue(_wait(lambda: s.snapshot()["paused"], timeout=10))
            # paused means still ALIVE — stop() is the destructive one
            time.sleep(0.8)
            self.assertFalse(s.ended, "pause must not end the run")
            self.assertIsNone(s.error)
            s.resume()
            self.assertTrue(_wait(lambda: not s.snapshot()["paused"], timeout=10))
        finally:
            s.stop()

    def test_stop_mid_pass(self):
        s = WideFindSession(FakeController(FakeOnOffDevice()), "p1",
                            (70.0, 1000.0), side="right",
                            sample_rate=2_000_000, n_samples=8192)
        s.start()
        time.sleep(0.6)
        s.stop()
        self.assertTrue(s.ended)


class TestSimulatorIQ(unittest.TestCase):
    def test_capture_iq_has_planted_carrier(self):
        from survey.sweep.simulator import SimulatedDevice
        from survey.sweep.sdr_source import welch_psd
        dev = SimulatedDevice()
        iq = dev.capture_iq(433.92e6, 2_000_000, 32768)
        self.assertEqual(len(iq), 32768)
        f_bb, psd = welch_psd(np.asarray(iq), 2_000_000, nperseg=1024)
        snr = float(np.max(psd) - np.median(psd))
        self.assertGreater(snr, 8.0)

    def test_capture_iq_quiet_off_band(self):
        from survey.sweep.simulator import SimulatedDevice
        from survey.sweep.sdr_source import welch_psd
        dev = SimulatedDevice()
        iq = dev.capture_iq(1234.0e6, 2_000_000, 32768)
        f_bb, psd = welch_psd(np.asarray(iq), 2_000_000, nperseg=1024)
        snr = float(np.max(psd) - np.median(psd))
        self.assertLess(snr, 8.0)


if __name__ == "__main__":
    unittest.main()
