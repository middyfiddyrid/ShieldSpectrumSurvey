#!/usr/bin/env python3
"""iq_record.py — capture N seconds of raw IQ off the radio into a SigMF pair.

THE JOB. Point the B220 (or the Pluto) at a real P25/DMR signal ONCE and get the
samples onto disk, with enough metadata that the file is still meaningful next
week. Everything after that happens offline against the file — see
tools/iq_replay.py. The one thing this tool must not do is let a bad capture look
like a good one, because you only find out at replay time, by which point the
transmitter has stopped and you are no longer standing next to it.

    py tools/iq_record.py --freq-mhz 851.0125 --seconds 30 --source b220 \
        --sample-rate 2400000 --gain 40 --out recordings/iq/p25_bench \
        --note "county trunked control channel, rooftop whip, LOS"

WHAT IT PRINTS AND WHY EACH LINE MATTERS
  * REQUESTED vs ACHIEVED sample rate. The achieved figure is measured from the
    wall clock over the samples actually delivered. A shortfall means the host
    could not keep up and the driver dropped buffers — the file will have SPLICES
    in it, and a splice inside a frame breaks sync exactly like a bad bit order
    would. You would spend a day chasing the wrong bug.
  * Clipping count. A pinned front end spreads the signal and shuts the C4FM eye.
  * Peak / RMS level. Too low and the symbols are quantiser noise.
  * Sample count, duration and file size, so the file can be sanity-checked
    against what you thought you asked for.

Uses the same device layer as the app (SweepController.connect_device ->
survey/sweep/uhd_source.py or sdr_source.py), so a radio that sweeps in the GUI
records here, with the same worker-subprocess isolation.
"""

import argparse
import os
import sys
import threading
import time

import numpy as np

try:
    sys.stdout.reconfigure(encoding="utf-8")   # Windows console is cp1252 by default
except Exception:                                                  # noqa: BLE001
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from survey.config import Config                                   # noqa: E402
from survey.iqfile import IQMeta, SigMFWriter, DEFAULT_DATATYPE     # noqa: E402
from survey.sweep.runner import SweepController                    # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUT_DIR = os.path.join(ROOT, "recordings", "iq")
# Config section per source; a b220 falls back to the shared pluto listen knobs
# exactly the way server.py's _mcfg does.
_SECTION = {"b220": "b220", "usrp": "b220", "uhd": "b220",
            "pluto": "pluto", "sdr": "pluto", "ad936x": "pluto"}
# Rate shortfall we are willing to call jitter rather than dropped buffers.
RATE_TOLERANCE = 0.02


def _cfg_get(cfg, source, key, default):
    """Per-source knob with the pluto section as the shared fallback."""
    sect = _SECTION.get(source, source)
    v = cfg.get(sect, key, default=None)
    if v is None and sect != "pluto":
        v = cfg.get("pluto", key, default=None)
    return default if v is None else v


def _default_out(freq_mhz: float) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    return os.path.join(DEFAULT_OUT_DIR,
                        f"{stamp}Z_{int(round(freq_mhz * 1000))}khz")


def _bandwidth_advice(fs: float) -> list:
    """Sample-rate sanity, stated before the capture rather than after it.

    C4FM (P25 Phase 1 / DMR) is 4800 Bd in a 12.5 kHz channel. survey/c4fm.py
    decimates to a 48 kHz working rate, so anything at or above that carries the
    signal; the reason to record WIDER is that a hand-tuned LO is never exactly
    on channel and a wide capture lets you correct the offset offline instead of
    driving back out. The reason not to go too wide is throughput — the dropped
    buffers described above."""
    notes = []
    if fs < 48_000:
        notes.append(
            f"sample rate {fs / 1e3:.1f} kS/s is below the 48 kHz working rate the "
            "C4FM front end decimates to — a 4800 Bd signal cannot be recovered "
            "from this file at all")
    elif fs < 200_000:
        notes.append(
            f"sample rate {fs / 1e3:.1f} kS/s covers a 12.5 kHz channel but leaves "
            "little room to correct an LO offset offline; 240 kS/s or more is a "
            "safer bench capture")
    if fs > 4_000_000:
        notes.append(
            f"sample rate {fs / 1e6:.2f} MS/s is high for a 12.5 kHz channel — "
            "it is mostly disk and USB throughput, and throughput is what causes "
            "dropped buffers. Consider 2.4 MS/s or less")
    return notes


def _stream_blocks(dev, center_hz, fs, n_block, want, stop, on_block):
    """Pull from stream_iq until `want` samples are in hand. Returns
    (samples, elapsed_seconds, note). Never raises past the caller — a driver
    that dies mid-capture must still leave a finalised file behind."""
    got, note = 0, ""
    t0 = time.time()
    try:
        for iq in dev.stream_iq(center_hz, fs, int(n_block), stop):
            a = np.asarray(iq, dtype=np.complex64).ravel()
            if a.size == 0:
                continue
            if got + a.size > want:
                a = a[:want - got]
            on_block(a)
            got += a.size
            if got >= want:
                break
    except Exception as e:                                         # noqa: BLE001
        note = f"stream ended early: {type(e).__name__}: {e}"
    finally:
        stop.set()
    return got, time.time() - t0, note


def _capture_blocks(dev, center_hz, fs, n_block, want, on_block):
    """Fallback for a device with no stream_iq: repeated one-shot capture_iq.

    Be clear about what this costs — each capture RETUNES and re-arms, so the
    blocks are NOT contiguous in time. That is fine for measuring a signal's
    shape and useless for frame sync across a block boundary. Only used when the
    device offers nothing better."""
    got, note = 0, ("one-shot capture_iq path: blocks are NOT time-contiguous, "
                    "so frame sync will not cross a block boundary")
    t0 = time.time()
    try:
        while got < want:
            n = min(int(n_block), want - got)
            a = np.asarray(dev.capture_iq(center_hz, fs, n),
                           dtype=np.complex64).ravel()
            if a.size == 0:
                break
            on_block(a)
            got += a.size
    except Exception as e:                                         # noqa: BLE001
        note += f"; capture ended early: {type(e).__name__}: {e}"
    return got, time.time() - t0, note


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Record raw IQ to a SigMF pair for offline decoder work.")
    p.add_argument("--freq-mhz", type=float, required=True,
                   help="RX center frequency in MHz")
    p.add_argument("--seconds", type=float, default=10.0,
                   help="capture length in seconds (default 10)")
    p.add_argument("--sample-rate", type=float, default=None,
                   help="sample rate in S/s (default: the source's "
                        "stream_sample_rate from config.yaml)")
    p.add_argument("--source", default=None,
                   help="b220 | pluto (default: device.source from config.yaml)")
    p.add_argument("--gain", type=float, default=None,
                   help="manual RX gain in dB for THIS capture only "
                        "(never written back to config.yaml)")
    p.add_argument("--out", default=None,
                   help="output base path; .sigmf-data/.sigmf-meta are appended "
                        "(default: recordings/iq/<utc-stamp>_<freq>khz)")
    p.add_argument("--note", default="",
                   help="operator note stored in the sidecar — what you aimed at, "
                        "antenna, position, why")
    p.add_argument("--datatype", default=DEFAULT_DATATYPE,
                   choices=("cf32_le", "ci16_le"),
                   help="on-disk sample format (default cf32_le, exact; ci16_le "
                        "is half the size and lossy)")
    p.add_argument("--block-seconds", type=float, default=None,
                   help="stream block length (default: the source's "
                        "listen_block_seconds)")
    p.add_argument("--simulate", action="store_true",
                   help="rehearse the whole path against the built-in simulated "
                        "device — no radio touched. Use it to prove the tool and "
                        "the output path work BEFORE the field trip; the samples "
                        "are synthetic and validate nothing about real traffic.")
    p.add_argument("--config", default=os.path.join(ROOT, "config.yaml"))
    args = p.parse_args(argv)

    cfg = Config(args.config)
    if args.simulate:
        cfg.set_runtime_override(("device", "simulate"), True)
    source = (args.source or cfg.get("device", "source", default="pluto")).strip().lower()
    sect = _SECTION.get(source, source)
    fs = float(args.sample_rate or _cfg_get(cfg, source, "stream_sample_rate", 2_400_000))
    block_s = float(args.block_seconds or _cfg_get(cfg, source, "listen_block_seconds", 0.25))
    n_block = max(1024, int(fs * block_s))
    want = int(round(fs * float(args.seconds)))
    center_hz = float(args.freq_mhz) * 1e6
    out = args.out or _default_out(args.freq_mhz)

    if args.gain is not None:
        # Runtime-only: get() honours it, set_and_save() never sees it, so a bench
        # gain experiment cannot silently become the app's configured gain.
        cfg.set_runtime_override((sect, "gain_mode"), "manual")
        cfg.set_runtime_override((sect, "gain_db"), float(args.gain))
    gain_db = _cfg_get(cfg, source, "gain_db", None)
    gain_mode = _cfg_get(cfg, source, "gain_mode", "")

    print("=" * 72)
    print("SHIELD TSCM — IQ RECORD (SigMF)")
    print("=" * 72)
    print(f"  source        : {source}")
    print(f"  center        : {args.freq_mhz:.6f} MHz")
    print(f"  sample rate   : {fs:,.0f} S/s (requested)")
    print(f"  gain          : {gain_db} dB ({gain_mode})")
    print(f"  length        : {args.seconds:g} s -> {want:,} samples")
    print(f"  datatype      : {args.datatype}")
    est = want * (8 if args.datatype == "cf32_le" else 4)
    print(f"  est. size     : {est / 1e6:,.1f} MB")
    print(f"  out           : {out}{{.sigmf-data,.sigmf-meta}}")
    for n in _bandwidth_advice(fs):
        print(f"  NOTE          : {n}")
    print()

    ctl = SweepController(None, cfg)          # connect_device needs cfg only
    try:
        info = ctl.connect_device(source=source)
    except Exception as e:                                         # noqa: BLE001
        print(f"  FAILED to open the radio: {type(e).__name__}: {e}")
        return 2
    dev = ctl.device
    hw = f"{info.model} ({info.driver}) {info.port}".strip()
    print(f"  connected     : {hw}")
    for w in getattr(info, "warnings", []) or []:
        print(f"  device warning: {w}")
    if getattr(info, "simulated", False):
        print("  *** SIMULATED DEVICE — these samples are synthetic, not off-air. "
              "Nothing recorded here validates anything about real traffic. ***")

    meta = IQMeta(center_hz=center_hz, sample_rate=fs, datatype=args.datatype,
                  source=source, hardware=hw, gain_db=gain_db,
                  gain_mode=str(gain_mode), note=args.note,
                  recorder="Shield TSCM spectrum_survey tools/iq_record.py")
    writer = SigMFWriter(out, meta)
    stop = threading.Event()
    print(f"  recording ... ({args.seconds:g} s)")
    streamed = callable(getattr(dev, "stream_iq", None))
    try:
        if streamed:
            got, elapsed, note = _stream_blocks(dev, center_hz, fs, n_block, want,
                                                stop, writer.append)
        else:
            got, elapsed, note = _capture_blocks(dev, center_hz, fs, n_block, want,
                                                 writer.append)
    finally:
        summary = writer.close()
        try:
            dev.close()
        except Exception:                                          # noqa: BLE001
            pass

    achieved = got / elapsed if elapsed > 0 else 0.0
    print()
    print("  --- result ---")
    print(f"  samples       : {summary['samples']:,} of {want:,} requested")
    print(f"  duration      : {summary['seconds']:.3f} s of signal time")
    print(f"  file size     : {summary['bytes'] / 1e6:,.2f} MB")
    print(f"  wall clock    : {elapsed:.3f} s")
    if streamed:
        print(f"  ACHIEVED rate : {achieved:,.0f} S/s  (requested {fs:,.0f})")
    else:
        # One-shot capture_iq is not real-time: it retunes and re-arms per block,
        # so this number is generation/transfer throughput and says NOTHING about
        # whether samples were dropped. Do not dress it up as an achieved rate.
        print(f"  throughput    : {achieved:,.0f} S/s — one-shot path, NOT a "
              "real-time rate and not evidence of continuity")
    print(f"  peak |I|,|Q|  : {summary['peak_amplitude']:.4f} of full scale")
    print(f"  RMS           : {summary['rms_amplitude']:.4f}")
    print(f"  clipped       : {summary['clipped_samples']:,} samples "
          f"({summary['clipped_fraction'] * 100:.3f} %)")
    print(f"  sha512        : {summary['sha512'][:32]}...")
    print(f"  data          : {summary['data_path']}")
    print(f"  meta          : {summary['meta_path']}")

    problems = list(summary["warnings"])
    if note:
        problems.append(note)
    if got < want:
        problems.append(
            f"SHORT: {want - got:,} samples missing — the capture ended before the "
            "requested length")
    if streamed and elapsed > 0 and achieved < fs * (1.0 - RATE_TOLERANCE):
        problems.append(
            f"DROPPED BUFFERS: delivered {achieved:,.0f} S/s against a requested "
            f"{fs:,.0f} S/s ({100 * (1 - achieved / fs):.1f} % short). The host did "
            "not keep up, so this file contains SPLICES where samples are missing. "
            "Frame sync will break at every splice and it will look like a decoder "
            "bug. Lower --sample-rate or --block-seconds and record again.")
    print()
    if problems:
        print("  !! CHECK BEFORE YOU LEAVE THE SITE:")
        for w in problems:
            print(f"     - {w}")
    else:
        print("  no clipping, no shortfall, rate held — capture looks usable.")
    print()
    print("  replay it with:")
    print(f"     py tools/iq_replay.py --file {summary['data_path']} --decoder auto")
    return 0 if summary["samples"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
