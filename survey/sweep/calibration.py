"""CALIBRATION / EXPERIMENT mode.

Run the same segment back-to-back under each capture strategy against a known
test source (cheap 433 MHz keyfob, 2.4 GHz device, ...) and report detection
rate, SNR, and time cost per strategy. The winning per-band configuration is
persisted to config.yaml so the production sweep profile is empirically tuned
to this exact unit, not guessed. Run once at the shop, not at client sites.
"""

import time

from .detector import Accumulator, detect_signals

STRATEGIES = [
    {"name": "normal_raw_x10", "calculator": "normal", "passes": 10, "dwell_s": 0},
    {"name": "average_x10", "calculator": "average", "passes": 10, "dwell_s": 0},
    {"name": "average_x28", "calculator": "average", "passes": 28, "dwell_s": 0},
    {"name": "max_x28", "calculator": "max", "passes": 28, "dwell_s": 0},
    {"name": "maxhold_10s", "calculator": "maxhold", "passes": 0, "dwell_s": 10.0},
    {"name": "maxhold_20s", "calculator": "maxhold", "passes": 0, "dwell_s": 20.0},
]
SPAN_VARIANTS_MHZ = [50.0, 10.0, 2.0]


def run_calibration(controller, cfg, test_freq_mhz: float = 433.92,
                    repeats: int = 3, progress=None) -> dict:
    """Blocking; call on a worker thread. controller: SweepController with a
    connected device. Returns the results table and writes the winner to config."""
    dev = controller.device
    dev.set_rbw_khz(None)   # calibration must run at config RBW, never a leftover override
    threshold_db = float(cfg.get("detection", "threshold_db", default=12.0))
    side = "left" if any(m.side == "left" and m.min_mhz <= test_freq_mhz <= m.max_mhz
                         for m in dev.info.modules) else "right"
    dev.select_module(side)

    results = []
    for span in SPAN_VARIANTS_MHZ:
        lo, hi = test_freq_mhz - span / 2, test_freq_mhz + span / 2
        for strat in STRATEGIES:
            if controller.abort_event.is_set():
                break
            detections, snrs, t_total, sweep_times = 0, [], 0.0, []
            for _ in range(repeats):
                dev.set_range(max(lo, 0.05), hi)
                dev.set_calculator(strat["calculator"])
                first = dev.read_sweep(timeout_s=8.0)
                if first is None:
                    continue
                acc = Accumulator(first.freqs_mhz)
                acc.add_sweep(first.amps_dbm)
                t0 = time.time()

                def on_frame(frame):
                    acc.add_sweep(frame.amps_dbm)
                    sweep_times.append(time.time())

                if strat["passes"]:
                    dev.collect(strat["passes"], on_frame, abort=controller.abort_event)
                else:
                    dev.stream(strat["dwell_s"], on_frame, abort=controller.abort_event)
                elapsed = time.time() - t0
                t_total += elapsed
                floor, sigs = detect_signals(acc.freqs, acc.avg, acc.maxpeak, acc.hits,
                                             acc.passes, threshold_db)
                hit = next((s for s in sigs
                            if abs(s.center_mhz - test_freq_mhz)
                            <= max(span / 20.0, 0.2)), None)
                if hit:
                    detections += 1
                    snrs.append(hit.peak_dbm - floor)
            dev.set_calculator("normal")
            row = {"strategy": strat["name"], "span_mhz": span,
                   "detection_rate": round(detections / repeats, 2),
                   "mean_snr_db": round(sum(snrs) / len(snrs), 1) if snrs else None,
                   "time_cost_s": round(t_total / repeats, 1)}
            results.append(row)
            if progress:
                progress(row)
        if controller.abort_event.is_set():
            break

    # measured raw sweep rate refines seconds_per_sweep for the ETA solver
    raw = next((r for r in results if r["strategy"] == "normal_raw_x10"
                and r["span_mhz"] == 50.0), None)
    if raw and raw["time_cost_s"]:
        cfg.set_and_save(("sweep", "seconds_per_sweep"),
                         round(raw["time_cost_s"] / 10.0, 3))

    # winner = best (detection_rate, snr) per unit time
    def fitness(r):
        snr = r["mean_snr_db"] or 0.0
        return r["detection_rate"] * 100 + snr - r["time_cost_s"] * 0.5

    detected_any = any(r["detection_rate"] > 0 for r in results)
    winner = max(results, key=fitness) if (results and detected_any) else None
    if winner:
        cfg.set_and_save(("calibration",), {
            "test_freq_mhz": test_freq_mhz, "winner": winner,
            "results": results, "calibrated_unit": dev.info.model,
            "calibrated_at": time.strftime("%Y-%m-%d %H:%M"),
        })
    message = "" if detected_any else (
        f"TEST SOURCE NOT DETECTED at {test_freq_mhz} MHz in any strategy — "
        "keep the keyfob/test transmitter keyed during the run. No winner saved.")
    return {"results": results, "winner": winner, "message": message,
            "seconds_per_sweep": cfg.get("sweep", "seconds_per_sweep")}
