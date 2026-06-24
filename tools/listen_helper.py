#!/usr/bin/env python3
"""
listen_helper.py — quick bridge from Shield TSCM wiki / results to a demod app.

Usage examples:
  python tools/listen_helper.py 433.92
  python tools/listen_helper.py 915.0 --mode NFM
  python tools/listen_helper.py 315.0 --bw 20

Prints exact instructions + suggested settings for SDR#. Optionally tries to
launch common tools (best effort; operator can copy freq anyway).

This is a zero-dependency bridge until we wire native demod into the web UI.
"""

import argparse
import os
import platform
import subprocess
import sys

MODES = {
    "AM": "AM — good for some cheap bugs / carrier with audio subcarrier",
    "NFM": "Narrow FM (default for most voice bugs, <25 kHz BW)",
    "WFM": "Wide FM (broadcast or wide video subcarrier)",
    "LSB": "Lower sideband (very narrow or HF-ish)",
    "USB": "Upper sideband",
    "CW": "CW / Morse / very narrow telemetry",
}

def suggest_mode(bw_khz: float | None) -> str:
    if not bw_khz:
        return "NFM"
    if bw_khz < 15:
        return "NFM"  # or SSB for super narrow
    if bw_khz < 30:
        return "NFM"
    if bw_khz < 200:
        return "AM"
    return "WFM"

def main():
    p = argparse.ArgumentParser(description="Turn wiki freq into listen instructions")
    p.add_argument("freq", type=float, help="Center frequency in MHz (e.g. 433.92)")
    p.add_argument("--mode", choices=list(MODES.keys()), default=None,
                   help="Force demod mode")
    p.add_argument("--bw", type=float, default=None, help="Observed BW in kHz (for suggestion)")
    p.add_argument("--gain", type=int, default=20, help="Rough starting gain (RTL typical 0-50)")
    args = p.parse_args()

    freq = args.freq
    mode = args.mode or suggest_mode(args.bw)
    bw = args.bw or "?"

    print("=" * 60)
    print("SHIELD TSCM — LISTEN HELPER")
    print("=" * 60)
    print(f"Target freq : {freq:.4f} MHz")
    print(f"Suggested   : {mode}  (based on bw~{bw} kHz)")
    print(f"Description : {MODES[mode]}")
    print()
    print("STEP-BY-STEP (RTL-SDR V4 + SDR# on Windows):")
    print("1. Plug RTL-SDR Blog V4 into a USB port (use powered hub if available).")
    print("2. If first time: run Zadig (from rtl-sdr.com), select the device, install WinUSB driver.")
    print("3. Launch SDR# (SDRSharp).")
    print(f"4. In SDR#: set Source = RTL-SDR USB, then click the Play button.")
    print(f"5. Enter frequency: {freq * 1000000:.0f}  (or {freq:.4f} MHz in the box)")
    print(f"6. Set mode to: {mode}")
    print("7. Adjust RF Gain (start ~15-25), turn off RTL AGC if overloaded.")
    print("8. Use your directional antenna. Rotate for strongest signal / clearest audio.")
    print("9. Hit Record (the red circle) to save a WAV next to your sweep session.")
    print()
    print("TIPS:")
    print(" - Narrow BW signals (<25 kHz) almost always NFM for voice bugs.")
    print(" - Continuous strong carrier with voice = classic analog bug.")
    print(" - Bursty / packet sounds = digital or remote. Record + open in URH later.")
    print(" - Very weak: add LNA later or move closer.")
    print(" - Move SMA cable from RF Explorer to SDR when ready to listen (or use switch).")
    print()
    print("After listening: note what you heard (voice? data? silence? broadcast content)")
    print("and any new characteristics in the wiki capture notes.")
    print("=" * 60)

    # Best-effort launch attempts (harmless if nothing happens)
    if platform.system() == "Windows":
        # Try common install locations for SDR#
        candidates = [
            r"C:\Program Files\SDR#\SDRSharp.exe",
            r"C:\SDR#\SDRSharp.exe",
            os.path.expanduser(r"~\SDR#\SDRSharp.exe"),
            r"C:\Program Files (x86)\SDR#\SDRSharp.exe",
        ]
        for c in candidates:
            if os.path.exists(c):
                try:
                    print(f"\n[info] Found SDR# at: {c}")
                    # SDR# doesn't take freq on CLI easily; just launch
                    subprocess.Popen([c], shell=False)
                    print("[info] Launched SDR#. Manually tune to the freq above.")
                    break
                except Exception:
                    pass
        else:
            print("\n[info] SDR# not auto-detected. Launch it manually from your Start menu / folder.")

    print("\nCopy this freq for the app or notes:", freq)
    print("Done. Headphones on, antenna pointed. Go identify it.")

if __name__ == "__main__":
    main()
