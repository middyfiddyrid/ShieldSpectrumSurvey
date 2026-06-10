"""Sweep orchestration.

Runs a planned multi-segment sweep on a worker thread with the layered pass
structure per segment:

  Pass 1 — AVERAGE: device calculator in average mode, N sweeps. Noise floor +
           weak continuous carriers.
  Pass 2 — MAX HOLD: device DSP max-hold engaged for the risk-weighted dwell —
           catches fast transients/bursts the host-side loop would miss.
  Host-side accumulation runs in parallel throughout: running average, max-peak,
  and per-bin hit-count (persistence) computed from the raw realtime stream
  regardless of device mode, all stored.

Serial disconnects are retried (reconnect + resume at the failed segment).
Abort keeps all data collected so far. After the sweep, every detected signal
is classified against the baseline and scored; interior/ambient deltas and
room-to-room localization are recomputed session-wide.
"""

import json
import logging
import threading
import time

from ..correlation import (anomaly_score, classify_signal, compute_interior_delta,
                           compute_room_localization)
from .detector import Accumulator, detect_signals
from .device import DeviceError

log = logging.getLogger("survey.runner")

MAX_SEGMENT_RETRIES = 3


class SweepController:
    """One per app. Owns the device handle and the single active sweep thread."""

    def __init__(self, db, cfg):
        self.db = db
        self.cfg = cfg
        self.device = None
        self._thread = None
        self.abort_event = threading.Event()
        self.state = {"running": False, "phase": None, "module": None, "room": None,
                      "segment_index": 0, "segment_total": 0, "percent": 0.0,
                      "eta_seconds": 0, "signal_count": 0, "message": "idle",
                      "live_trace": None, "current_segment": None, "error": None,
                      "directional": None, "calibration": None}
        self._lock = threading.Lock()

    # ---- device ----

    def connect_device(self):
        from .device import open_device
        old, self.device = self.device, None   # never leave a dead handle assigned
        if old is not None:
            try:
                old.close()
            except Exception:  # noqa: BLE001
                pass
        self.device = open_device(self.cfg)
        return self.device.info

    def _ensure_device(self):
        if self.device is None or not self.device.is_alive():
            log.warning("device missing or dead — reconnecting")
            self.connect_device()

    def module_ranges(self) -> list[dict]:
        """Module list with REAL frequency ranges. The connect-time read of the
        inactive module's range can fail (module boot races); recover here by
        switching to any module whose range is still unknown and reading the
        config the hardware reports."""
        self._ensure_device()
        for m in self.device.info.modules:
            if m.max_mhz <= m.min_mhz:
                log.warning("module %s range unknown — querying device", m.side)
                self.device.select_module(m.side)
        bad = [m.side for m in self.device.info.modules if m.max_mhz <= m.min_mhz]
        if bad:
            raise DeviceError(f"device did not report a frequency range for "
                              f"module(s): {', '.join(bad)}")
        return [m.to_dict() for m in self.device.info.modules]

    def device_info(self):
        return self.device.info.to_dict() if self.device else None

    def _reconnect(self):
        log.warning("Serial problem — attempting reconnect")
        time.sleep(2.0)
        self.connect_device()

    # ---- sweep lifecycle ----

    def busy(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def abort(self):
        self.abort_event.set()
        self._set(message="abort requested — keeping data collected so far")

    def _set(self, **kw):
        with self._lock:
            self.state.update(kw)

    def start_sweep(self, session_id: int, phase: str, module: str, room: str,
                    plan, on_done=None):
        if self.busy():
            raise RuntimeError("a sweep is already running")
        self._ensure_device()
        self.abort_event.clear()
        sweep_id = self.db.execute(
            "INSERT INTO sweeps (session_id,phase,module,room,profile,started_utc,"
            "range_low_mhz,range_high_mhz,status,simulated) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (session_id, phase, module, room, plan.profile, time.time(),
             plan.segments[0].low_mhz if plan.segments else 0,
             plan.segments[-1].high_mhz if plan.segments else 0,
             "running", int(self.device.info.simulated)))

        def work():
            try:
                self._run_plan(session_id, sweep_id, phase, module, room, plan)
                status = "aborted" if self.abort_event.is_set() else "done"
            except Exception as e:  # noqa: BLE001
                log.exception("sweep failed")
                status = "error"
                self._set(error=str(e), message=f"sweep error: {e}")
            self.db.execute("UPDATE sweeps SET status=?, finished_utc=? WHERE id=?",
                            (status, time.time(), sweep_id))
            try:
                postprocess_session(self.db, self.cfg, session_id)
            except Exception:  # noqa: BLE001
                log.exception("postprocess failed")
            self._set(running=False,
                      message=f"{phase} sweep ({module}) {status}; "
                              f"{self.state['signal_count']} signals detected")
            if on_done:
                on_done(status)

        self._thread = threading.Thread(target=work, daemon=True)
        self._set(running=True, phase=phase, module=module, room=room, error=None,
                  segment_index=0, segment_total=len(plan.segments), percent=0.0,
                  signal_count=0, sweep_id=sweep_id,
                  message=f"starting {phase} sweep on {module} module")
        self._thread.start()
        return sweep_id

    def _run_plan(self, session_id, sweep_id, phase, module, room, plan):
        dev = self.device
        if hasattr(dev, "environment"):
            dev.environment = {"phase": phase, "room": room}
        dev.select_module(module if module in ("left", "right") else "right")
        sps = float(self.cfg.get("sweep", "seconds_per_sweep", default=0.35))
        threshold_db = float(self.cfg.get("detection", "threshold_db", default=12.0))
        merge_gap = int(self.cfg.get("detection", "merge_gap_bins", default=2))
        baseline_rows = self.db.query("SELECT * FROM baseline WHERE session_id=?",
                                      (session_id,))
        remaining = sum(s.avg_passes * sps + s.maxhold_dwell_s + 0.9 for s in plan.segments)

        for idx, seg in enumerate(plan.segments):
            if self.abort_event.is_set():
                break
            self._set(segment_index=idx + 1,
                      current_segment={"low_mhz": seg.low_mhz, "high_mhz": seg.high_mhz,
                                       "band_tag": seg.band_tag,
                                       "rbw_khz": round(seg.rbw_khz(plan.sweep_points), 1)},
                      eta_seconds=int(remaining),
                      message=f"segment {idx + 1}/{len(plan.segments)}: "
                              f"{seg.low_mhz:.3f}-{seg.high_mhz:.3f} MHz [{seg.band_tag}]")
            for attempt in range(1, MAX_SEGMENT_RETRIES + 1):
                try:
                    self._sweep_segment(session_id, sweep_id, phase, module, room, seg,
                                        plan, baseline_rows, threshold_db, merge_gap)
                    break
                except (DeviceError, OSError) as e:
                    log.warning("segment %s attempt %d failed: %s", idx + 1, attempt, e)
                    if attempt == MAX_SEGMENT_RETRIES:
                        raise
                    self._set(message=f"serial hiccup — reconnecting (attempt {attempt})")
                    self._reconnect()
                    dev = self.device
                    if hasattr(dev, "environment"):
                        dev.environment = {"phase": phase, "room": room}
                    dev.select_module(module if module in ("left", "right") else "right")
            remaining -= seg.avg_passes * sps + seg.maxhold_dwell_s + 0.9
            self._set(percent=round(100.0 * (idx + 1) / len(plan.segments), 1),
                      eta_seconds=max(int(remaining), 0))

    def _sweep_segment(self, session_id, sweep_id, phase, module, room, seg, plan,
                       baseline_rows, threshold_db, merge_gap):
        dev = self.device
        dev.set_range(seg.low_mhz, seg.high_mhz)
        first = dev.read_sweep(timeout_s=8.0)
        if first is None:
            raise DeviceError("no sweep data after range change")
        acc = Accumulator(first.freqs_mhz)
        acc.add_sweep(first.amps_dbm)

        def on_frame(frame):
            acc.add_sweep(frame.amps_dbm)
            # publish all three traces — realtime, running average, max-hold —
            # so the UI can draw them together like RF Explorer for Windows
            n = len(frame.amps_dbm)
            self._set(live_trace={"freqs": [round(f, 4) for f in frame.freqs_mhz],
                                  "amps": [round(a, 1) for a in frame.amps_dbm],
                                  "avg": [round(a, 1) for a in acc.avg[:n]],
                                  "maxpeak": [round(a, 1) for a in acc.maxpeak[:n]],
                                  "low": seg.low_mhz, "high": seg.high_mhz})

        # Pass 1 — AVERAGE
        dev.set_calculator("average")
        dev.collect(seg.avg_passes, on_frame, abort=self.abort_event)
        # Pass 2 — MAX HOLD (device DSP transient capture)
        dev.set_calculator("maxhold")
        dev.stream(seg.maxhold_dwell_s, on_frame, abort=self.abort_event)
        dev.set_calculator("normal")

        floor, signals = detect_signals(acc.freqs, acc.avg, acc.maxpeak, acc.hits,
                                        acc.passes, threshold_db, merge_gap)
        segment_id = self.db.execute(
            "INSERT INTO segments (sweep_id,low_mhz,high_mhz,noise_floor_dbm,avg_passes,"
            "maxhold_dwell_s,status,data_json) VALUES (?,?,?,?,?,?,?,?)",
            (sweep_id, seg.low_mhz, seg.high_mhz, round(floor, 1), seg.avg_passes,
             seg.maxhold_dwell_s, "done", json.dumps(acc.to_dict())))
        for s in signals:
            cls = classify_signal(s.center_mhz, baseline_rows)
            self.db.execute(
                "INSERT INTO signals (sweep_id,session_id,segment_id,room,module,phase,"
                "center_mhz,bw_khz,peak_dbm,avg_dbm,floor_dbm,persistence,classification,"
                "matched_label,match_reason,caution) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (sweep_id, session_id, segment_id, room, module, phase,
                 s.center_mhz, s.bw_khz, s.peak_dbm, s.avg_dbm, s.floor_dbm,
                 s.persistence, cls.classification, cls.matched_label,
                 cls.match_reason, int(cls.caution)))
        self._set(signal_count=self.state["signal_count"] + len(signals))

    # ---- directional follow-up ----

    def start_directional(self, session_id: int, center_mhz: float, bw_khz: float,
                          room: str):
        if self.busy():
            raise RuntimeError("a sweep is already running")
        self._ensure_device()
        self.abort_event.clear()
        span_mhz = max(2.0, (bw_khz / 1000.0) * 4)
        lo, hi = center_mhz - span_mhz / 2, center_mhz + span_mhz / 2

        def work():
            dev = self.device
            try:
                side = "left" if any(m.side == "left" and m.min_mhz <= center_mhz <= m.max_mhz
                                     for m in dev.info.modules) and center_mhz < 1000 else "right"
                dev.select_module(side)
                dev.set_range(max(lo, 0.05), hi)
                dev.set_calculator("maxhold")
                peak_hold = -200.0
                while not self.abort_event.is_set():
                    frame = dev.read_sweep(timeout_s=3.0)
                    if frame is None:
                        continue
                    cur = max(frame.amps_dbm)
                    peak_hold = max(peak_hold, cur)
                    self._set(directional={
                        "center_mhz": center_mhz, "room": room,
                        "current_dbm": round(cur, 1), "peak_dbm": round(peak_hold, 1),
                        "trace": {"freqs": [round(f, 4) for f in frame.freqs_mhz],
                                  "amps": [round(a, 1) for a in frame.amps_dbm]}})
                dev.set_calculator("normal")
                self.db.execute(
                    "INSERT INTO directional (session_id,center_mhz,room,peak_dbm,notes,ts_utc)"
                    " VALUES (?,?,?,?,?,?)",
                    (session_id, center_mhz, room, round(peak_hold, 1), "", time.time()))
            except Exception as e:  # noqa: BLE001
                log.exception("directional pass failed")
                self._set(error=str(e))
            finally:
                self._set(running=False, directional=None,
                          message=f"directional reading logged for {center_mhz:.4f} MHz")

        self._thread = threading.Thread(target=work, daemon=True)
        self._set(running=True, phase="directional", room=room, error=None,
                  message=f"directional lock on {center_mhz:.4f} MHz — sweep the antenna")
        self._thread.start()


# ---------------------------------------------------------------------------
# Session-wide post-processing (interior/ambient delta, room localization, score)
# ---------------------------------------------------------------------------

def postprocess_session(db, cfg, session_id: int):
    interior_flag = float(cfg.get("detection", "interior_delta_flag_db", default=10.0))
    room_flag = float(cfg.get("detection", "room_delta_flag_db", default=12.0))

    signals = db.query(
        "SELECT s.* FROM signals s JOIN sweeps w ON s.sweep_id=w.id "
        "WHERE s.session_id=? AND w.status IN ('done','aborted')", (session_id,))
    ambient = [s for s in signals if s["phase"] == "ambient"]
    interior = [s for s in signals if s["phase"] == "interior"]

    ambient_floor = None
    floors = db.query(
        "SELECT seg.noise_floor_dbm AS f FROM segments seg JOIN sweeps w ON seg.sweep_id=w.id "
        "WHERE w.session_id=? AND w.phase='ambient'", (session_id,))
    if floors:
        ambient_floor = sum(r["f"] for r in floors) / len(floors)

    by_room: dict[str, list[dict]] = {}
    for s in interior:
        by_room.setdefault(s["room"] or "unspecified", []).append(s)
    localization = compute_room_localization(by_room)

    gps_bands = [(1565.42, 1585.42), (1217.6, 1237.6)]
    baseline_rows = db.query("SELECT * FROM baseline WHERE session_id=?", (session_id,))

    for s in interior:
        delta = compute_interior_delta(s, ambient, ambient_floor) if (ambient or
                                                                      ambient_floor) else None
        loc_delta, strongest = None, None
        for key, g in localization.items():
            if abs(key - s["center_mhz"]) <= max((s["bw_khz"] or 100) / 1000.0, 0.2):
                loc_delta, strongest = g["delta_db"], g["strongest_room"]
                break
        cls = classify_signal(s["center_mhz"], baseline_rows)
        gps_band = any(lo <= s["center_mhz"] <= hi for lo, hi in gps_bands)
        score = anomaly_score(cls, s["persistence"], delta, loc_delta,
                              interior_flag, room_flag, gps_band)
        db.execute(
            "UPDATE signals SET interior_delta_db=?, localization_score=?, "
            "strongest_room=?, anomaly_score=?, classification=?, matched_label=?, "
            "match_reason=?, caution=? WHERE id=?",
            (round(delta, 1) if delta is not None else None,
             loc_delta, strongest, score, cls.classification, cls.matched_label,
             cls.match_reason, int(cls.caution), s["id"]))

    # ambient signals get classified too (no localization boosts)
    for s in ambient:
        cls = classify_signal(s["center_mhz"], baseline_rows)
        score = anomaly_score(cls, s["persistence"], None, None,
                              interior_flag, room_flag)
        db.execute(
            "UPDATE signals SET anomaly_score=?, classification=?, matched_label=?, "
            "match_reason=?, caution=? WHERE id=?",
            (score, cls.classification, cls.matched_label, cls.match_reason,
             int(cls.caution), s["id"]))
