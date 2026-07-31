"""LIVE hardware check for coherent dual-RX DF on the B220 (NOT a unit test —
excluded from discovery by the missing test_ prefix). Run with the B220
connected and both antennas attached.

The decisive coherence proof: at a FIXED tune, the inter-channel phase of a
real emitter must be STABLE across repeated captures. Two independent radios
re-randomize phase every capture; genuinely coherent channels (shared LO, one
streamer) hold it to a few degrees.

Usage:  python tests/df_live_check.py [center_mhz]   (default 100.0 = FM band)
"""

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from survey.config import Config
from survey import df
from survey.sweep.uhd_source import B220Source


def main():
    center_mhz = float(sys.argv[1]) if len(sys.argv) > 1 else 100.0
    fs = 5e6                       # 4 MHz fails the AD9361 VCO search on this clone
    n = 1 << 17
    spacing = 0.35

    cfg = Config("config.yaml")
    dev = B220Source(cfg)
    print("connecting B220...", flush=True)
    info = dev.connect()
    print(f"  {info.model}, channels={dev.channels}, "
          f"range {info.modules[0].min_mhz:.0f}-{info.modules[0].max_mhz:.0f} MHz")
    if dev.channels < 2:
        print("FAIL: radio did not enumerate 2 RX channels")
        return 1

    print(f"\ncoherent dual capture @ {center_mhz} MHz, {n} samples x2 ...", flush=True)
    phases = []
    for i in range(6):
        pair = dev.capture_iq_dual(center_mhz * 1e6, fs, n)
        assert pair.shape[0] == 2, "not a 2-channel capture"
        m = df.measure(pair[0], pair[1], fs, center_mhz * 1e6, spacing,
                       cal_offset_deg=0.0)
        phases.append(m.phase_deg)
        print(f"  [{i}] verdict={m.verdict:18s} phase={m.phase_deg:+7.1f}°  "
              f"coh={m.coherence:.2f}  snrA/B={m.snr_a_db:.0f}/{m.snr_b_db:.0f}dB",
              flush=True)

    # circular std-dev of the inter-channel phase = the coherence proof
    ang = np.radians(phases)
    r = np.abs(np.mean(np.exp(1j * ang)))
    circ_std_deg = np.degrees(np.sqrt(-2.0 * np.log(max(r, 1e-9))))
    print(f"\ninter-channel phase circular-std across captures: {circ_std_deg:.1f}°")
    if circ_std_deg < 20.0:
        print("PASS: phase is STABLE across captures — channels are coherent.")
    else:
        print("INCONCLUSIVE: phase wandered — either no strong emitter at this "
              "tune (try an FM station or 2.4 GHz), or channels are not coherent.")
    dev.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
