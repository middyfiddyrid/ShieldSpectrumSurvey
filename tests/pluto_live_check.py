"""LIVE hardware check for the Pluto SDR source — requires a real ADALM-Pluto.

Not part of the unittest suite (no hardware in CI). Run manually:
    set PLUTO_URI=ip:192.168.2.1   (optional)
    python tests/pluto_live_check.py

Exercises the real tune -> IQ -> PSD -> stitch path and checks the acceptance
criteria: one contiguous (freq, power) trace, no center-bin LO spikes, seams
within a documented threshold, plus a capture_iq() round-trip.
"""

import os
import sys
import time

import numpy as np

try:
    sys.stdout.reconfigure(encoding="utf-8")  # Windows console is cp1252 by default
except Exception:  # noqa: BLE001
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from survey.config import Config
from survey.sweep.sdr_source import PlutoSource, plan_substeps

SEAM_DB = 6.0  # documented max acceptable residual seam/center artifact above local floor


def main():
    cfg = Config(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "config.yaml"))
    src = PlutoSource(cfg)
    info = src.connect()
    print(f"connected: {info.model} via {info.port}  driver={info.driver}")
    print(f"tunable module range: {info.modules[0].min_mhz:.1f}-{info.modules[0].max_mhz:.1f} MHz")
    for w in info.warnings:
        print(f"  warning: {w}")

    lo, hi = 2400.0, 2483.5   # 2.4 GHz ISM — within stock range, real Wi-Fi/BLE energy
    print(f"\nstepped sweep {lo}-{hi} MHz ...")
    t0 = time.time()
    src.set_range(lo, hi)
    frame = src.read_sweep()
    dur = time.time() - t0
    freqs = np.asarray(frame.freqs_mhz)
    amps = np.asarray(frame.amps_dbm)
    centers = plan_substeps(lo * 1e6, hi * 1e6, src._fs, src._keep, src._overlap)
    print(f"  {len(centers)} tune steps, {len(freqs)} bins, "
          f"sweep took {dur:.2f}s (info.last_sweep_seconds={info.last_sweep_seconds})")

    ok = True
    # 1) contiguous + monotonic
    mono = bool(np.all(np.diff(freqs) > 0))
    span_ok = abs(freqs[0] - lo) < 0.1 and abs(freqs[-1] - hi) < 0.1
    print(f"  [{'PASS' if mono and span_ok else 'FAIL'}] contiguous monotonic "
          f"{freqs[0]:.2f}-{freqs[-1]:.2f} MHz, no NaN={not np.any(np.isnan(amps))}")
    ok &= mono and span_ok and not np.any(np.isnan(amps))

    # 2) no center-bin (LO leakage) spike. A real LO artifact appears at EVERY
    #    tune center identically; a real on-air signal contaminates only the few
    #    centers near it. So the systematic-artifact metric is the MEDIAN center
    #    excess over its local floor across all centers (robust to live signals);
    #    the max is reported for info only (it tracks whatever Wi-Fi/BLE is up).
    excesses = []
    worst, worst_mhz = -99.0, 0.0
    for c in centers:
        cmhz = c / 1e6
        if cmhz < freqs[0] or cmhz > freqs[-1]:
            continue
        i = int(np.argmin(np.abs(freqs - cmhz)))
        win = (np.abs(freqs - cmhz) <= 1.0) & (np.abs(freqs - cmhz) >= 0.2)
        if not np.any(win):
            continue
        excess = amps[i] - np.median(amps[win])
        excesses.append(excess)
        if excess > worst:
            worst, worst_mhz = excess, cmhz
    med = float(np.median(excesses)) if excesses else 0.0
    print(f"  [{'PASS' if med <= SEAM_DB else 'FAIL'}] median center-bin excess "
          f"= {med:.1f} dB (<= {SEAM_DB} dB)  [max {worst:.1f} dB @ {worst_mhz:.1f} "
          f"MHz tracks live signals, info only]")
    ok &= med <= SEAM_DB

    # 3) seam continuity: median absolute bin-to-bin step should be small; report
    #    the distribution so real signals (legitimately large steps) are obvious.
    d = np.abs(np.diff(amps))
    print(f"  [info] adjacent-bin abs-step: median={np.median(d):.2f} dB, "
          f"p99={np.percentile(d, 99):.2f} dB, max={np.max(d):.2f} dB "
          f"(large max is fine if it's a real carrier edge)")
    print(f"  [info] floor≈{np.median(amps):.1f} dBm(approx), "
          f"peak={np.max(amps):.1f} dBm(approx) @ {freqs[int(np.argmax(amps))]:.3f} MHz")

    # 4) capture_iq round-trip
    iq = src.capture_iq(2_437e6, 4_000_000, 16384)
    iq_ok = iq.dtype == np.complex64 and iq.size == 16384
    print(f"  [{'PASS' if iq_ok else 'FAIL'}] capture_iq @2437 MHz -> {iq.shape} "
          f"{iq.dtype}, mean|IQ|={np.mean(np.abs(iq)):.3f}")
    ok &= iq_ok

    src.close()
    print(f"\n{'ALL CHECKS PASSED' if ok else 'SOME CHECKS FAILED'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
