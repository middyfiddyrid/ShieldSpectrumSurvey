"""Simulated RF Explorer for development and demos WITHOUT hardware.

Never used silently: only when device.simulate is true in config (or --simulate
on the command line). Everything it produces is marked SIMULATED in the UI, the
database, and any report.

The scenario plants a classic TSCM finding: a 433.92 MHz device that is weak
outside (ambient), present inside, and loud in the Office — plus normal FM /
NOAA / cellular / WiFi environment so the correlation engine has real work.
"""

import random
import time

from .device import BaseDevice, DeviceInfo, ModuleInfo, SweepFrame


class SimulatedDevice(BaseDevice):
    def __init__(self):
        self.info = DeviceInfo(model="6G Combo PLUS (SIMULATED)", firmware="03.45-SIM",
                               port="SIM", simulated=True, driver="simulator")
        self.info.modules = [
            ModuleInfo("left", "WSUB1G_PLUS (sim)", 0.05, 960.0),
            ModuleInfo("right", "6G_PLUS (sim)", 240.0, 6100.0),
        ]
        self._range = (430.0, 440.0)
        self._side = "right"
        self._rng = random.Random()
        self._points = 112
        # set by the runner so the scenario can vary by phase/room
        self.environment = {"phase": "interior", "room": ""}

    def connect(self) -> DeviceInfo:
        return self.info

    def select_module(self, side: str):
        self._side = side

    def set_range(self, start_mhz: float, end_mhz: float):
        self._range = (start_mhz, end_mhz)

    def set_calculator(self, mode_name: str):
        pass  # host-side accumulation covers the simulation

    def _carriers(self):
        phase = self.environment.get("phase", "interior")
        room = self.environment.get("room", "") or ""
        ambient = (phase == "ambient")
        carriers = [
            (98.5, -48.0, 0.2), (95.1, -55.0, 0.2), (104.7, -60.0, 0.2),   # FM
            (162.45, -72.0, 0.03),                                          # NOAA
            (155.34, -80.0, 0.02),                                          # county LMR
            (575.0, -65.0, 6.0),                                            # TV UHF
            (734.0, -70.0, 5.0), (1962.5, -68.0, 5.0), (2125.0, -71.0, 5.0),  # cell DL
        ]
        # WiFi: bursty — handled via burst probability below
        carriers.append((2437.0, -52.0 if not ambient else -60.0, 11.0))
        carriers.append((5785.0, -62.0, 18.0))
        # THE PLANT: 433.92 MHz device, local to the Office
        if ambient:
            bug_amp = -88.0
        elif "office" in room.lower():
            bug_amp = -44.0
        else:
            bug_amp = -62.0
        carriers.append((433.92, bug_amp, 0.15))
        # second plant: 611.0 MHz — inside the post-repack TV gap above ch 36,
        # matches no allocation -> a true UNVERIFIED anomaly. Interior only.
        if not ambient:
            carriers.append((611.0, -58.0 if "office" in room.lower() else -70.0, 0.05))
        return carriers

    def read_sweep(self, timeout_s: float = 5.0):
        time.sleep(0.12)  # simulated sweep time (faster than real hardware)
        lo, hi = self._range
        n = self._points
        freqs = [lo + (hi - lo) * i / (n - 1) for i in range(n)]
        amps = [-105.0 + self._rng.uniform(-2.0, 2.0) for _ in range(n)]
        step = (hi - lo) / (n - 1)
        for f, p, half_bw in self._carriers():
            if not (lo - half_bw <= f <= hi + half_bw):
                continue
            bursty = f in (2437.0,)
            if bursty and self._rng.random() < 0.55:
                continue
            # a real analyzer integrates power across the RBW: a carrier narrower
            # than one bin still lands fully in its nearest bin
            eff_bw = max(half_bw, step / 2)
            for i, fr in enumerate(freqs):
                d = abs(fr - f)
                if d <= eff_bw:
                    amps[i] = max(amps[i], p + self._rng.uniform(-1.5, 1.5))
                elif d <= eff_bw * 2:
                    amps[i] = max(amps[i], p - 12.0 + self._rng.uniform(-2, 2))
        return SweepFrame(freqs, amps, time.time())

    def close(self):
        pass
