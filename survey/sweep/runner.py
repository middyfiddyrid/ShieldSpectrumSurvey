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
import os
import threading
import time

from ..correlation import (_matches, anomaly_score, classify_signal,
                           compute_interior_delta, compute_room_localization)
from ..expected import expected_for_range
from .detector import Accumulator, detect_signals
from .device import DeviceError

log = logging.getLogger("survey.runner")

MAX_SEGMENT_RETRIES = 3


class SweepController:
    """One per app. Owns the device handle and the single active sweep thread."""

    def __init__(self, db, cfg):
        self.db = db
        self.cfg = cfg
        self.device = None               # the ACTIVE/sweeper device (legacy single-device handle)
        self.active_source = None        # which spectrum source is connected (rfexplorer|pluto)
        # Phase-2 dual-device (flag pluto.concurrent_monitor, default off): hold
        # >1 radio at once, each with its OWN lock + listen-stop, so an RFE sweep
        # and a Pluto monitor never contend. When the flag is off these dicts hold
        # a single entry and everything routes through self.device/device_lock
        # exactly as before — behaviour is byte-identical.
        self.devices = {}                # source -> handle
        self.device_locks = {}           # source -> threading.Lock
        self.listen_stops = {}           # source -> threading.Event
        self._thread = None              # the sweep/capture/directional worker
        self._monitor_thread = None      # the concurrent monitor (recording) worker
        self._busy_source = None         # source the sweep worker is bound to
        self._monitor_source = None      # source the monitor worker is bound to
        self.abort_event = threading.Event()
        # serialise radio access across sweep / snapshot-capture / live audio stream
        self.device_lock = threading.Lock()
        self.listen_stop = threading.Event()   # set to stop the active audio stream
        self.listening = False
        self.listen_spectrum = None            # latest live analyzer+waterfall from the audio stream
        self.record_stop = threading.Event()   # set to stop the active monitor recording
        self.recording = None                  # active/last recording descriptor
        # F5 manual segment advance (custom mode): when on, the worker pauses
        # between segments until advance_event fires (still honouring abort_event).
        self.manual_advance = False
        self.advance_event = threading.Event()
        # F2 anomaly accumulator — persists across segments for the whole sweep.
        self._anomalies = []
        self.state = {"running": False, "phase": None, "module": None, "room": None,
                      "segment_index": 0, "segment_total": 0, "percent": 0.0,
                      "eta_seconds": 0, "signal_count": 0, "message": "idle",
                      "live_trace": None, "current_segment": None, "error": None,
                      "directional": None, "calibration": None, "capture": None,
                      "awaiting_advance": False, "anomalies_found": []}
        self._lock = threading.Lock()

    # ---- device ----

    def concurrent_enabled(self) -> bool:
        return bool(self.cfg.get("pluto", "concurrent_monitor", default=False))

    def connect_device(self, source=None, keep_other=False):
        from .device import open_device
        src = (source or os.environ.get("SWEEP_SOURCE")
               or self.cfg.get("device", "source", default="rfexplorer")).strip().lower()
        if keep_other and self.concurrent_enabled():
            # DUAL: replace only a same-source handle; keep the OTHER radio open
            # so it can run concurrently (RFE sweep + Pluto monitor).
            ex = self.devices.get(src)
            if ex is not None:
                try:
                    ex.close()
                except Exception:  # noqa: BLE001
                    pass
        else:
            # LEGACY single-device: close everything currently open (today's behavior).
            old, self.device = self.device, None
            if old is not None:
                try:
                    old.close()
                except Exception:  # noqa: BLE001
                    pass
            for s in list(self.devices):
                d = self.devices.pop(s)
                if d is not old:
                    try:
                        d.close()
                    except Exception:  # noqa: BLE001
                        pass
        dev = open_device(self.cfg, source=src)
        self.devices[src] = dev
        self.device_locks.setdefault(src, threading.Lock())
        self.listen_stops.setdefault(src, threading.Event())
        # self.device is the SWEEPER: in dual mode keep the existing sweeper so a
        # sweep stays on the RFE while the just-added Pluto becomes the monitor.
        if not (keep_other and self.concurrent_enabled()) or self.device is None:
            self.device = dev
            self.active_source = src
        return dev.info

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

    def busy(self, source=None) -> bool:
        """No-arg: is ANY worker (sweep or monitor) running — the legacy meaning,
        byte-identical when concurrent mode is off (monitor thread is unused then).
        With `source`: is a worker bound to THAT radio running? Lets a Pluto
        monitor proceed while an RFE sweep is in flight on a different radio."""
        sweep_alive = bool(self._thread and self._thread.is_alive())
        mon_alive = bool(self._monitor_thread and self._monitor_thread.is_alive())
        if source is None:
            return sweep_alive or mon_alive
        return ((sweep_alive and self._busy_source == source)
                or (mon_alive and self._monitor_source == source))

    def monitor_target(self):
        """(device, lock, listen_stop, source) for live monitor / analyze /
        record. In dual mode with a Pluto open, returns the Pluto + its OWN lock
        so it never blocks on the sweeper's lock; otherwise the active device +
        the legacy lock (so single-device behaviour is unchanged)."""
        if self.concurrent_enabled() and "pluto" in self.devices:
            src = "pluto"
            return (self.devices[src], self.device_locks.setdefault(src, threading.Lock()),
                    self.listen_stops.setdefault(src, threading.Event()), src)
        return self.device, self.device_lock, self.listen_stop, self.active_source

    def device_roles(self) -> dict:
        """What each connected radio is doing — for the UI to show 'RFE sweeping +
        Pluto monitoring' and decide live vs post-hoc triage."""
        _, _, _, mon_src = self.monitor_target()
        roles = []
        for src, dev in self.devices.items():
            if src == self.active_source:
                role = "sweeper"
            elif src == mon_src:
                role = "monitor"
            else:
                role = "idle"
            roles.append({"source": src, "role": role, "busy": self.busy(src)})
        return {"roles": roles, "concurrent_enabled": self.concurrent_enabled(),
                "active_source": self.active_source}

    def abort(self):
        self.abort_event.set()
        self.advance_event.set()   # wake a manually-paused sweep so it can stop
        self._set(message="abort requested — keeping data collected so far")

    def advance(self):
        """F5: operator tapped NEXT SEGMENT — release the manual-advance gate."""
        self.advance_event.set()

    def _set(self, **kw):
        with self._lock:
            self.state.update(kw)

    def start_sweep(self, session_id: int, phase: str, module: str, room: str,
                    plan, manual_advance: bool = False, on_done=None):
        if self.busy():
            raise RuntimeError("a sweep is already running")
        self.listen_stop.set()           # stop any live audio stream and let it release the radio
        time.sleep(0.4)
        self._ensure_device()
        self.abort_event.clear()
        self.manual_advance = bool(manual_advance)
        self.advance_event.clear()
        self._anomalies = []
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
            try:
                from ..sigcapture import archive_observations
                sess = self.db.query_one("SELECT address FROM sessions WHERE id=?",
                                         (session_id,))
                added = archive_observations(
                    self.db, self.cfg, session_id,
                    self.cfg.abs_path("capture_images"),
                    sess["address"] if sess else "")
                log.info("archived %d signal observations to the wiki", added)
            except Exception:  # noqa: BLE001
                log.exception("observation archive failed")
            try:
                from ..novelty import mark_session_novelty
                n_novel = mark_session_novelty(self.db, session_id)
                log.info("novelty: %d signals new to this address", n_novel)
            except Exception:  # noqa: BLE001
                log.exception("novelty marking failed")
            self._set(running=False,
                      message=f"{phase} sweep ({module}) {status}; "
                              f"{self.state['signal_count']} signals detected")
            if on_done:
                on_done(status)

        self._thread = threading.Thread(target=work, daemon=True)
        self._busy_source = self.active_source
        self._set(running=True, phase=phase, module=module, room=room, error=None,
                  segment_index=0, segment_total=len(plan.segments), percent=0.0,
                  signal_count=0, sweep_id=sweep_id, awaiting_advance=False,
                  anomalies_found=[],
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
            # F2: what licensed records do we EXPECT in this segment's range —
            # published as status='looking', flipped to 'found' in _sweep_segment.
            seg_expected = expected_for_range(baseline_rows, seg.low_mhz, seg.high_mhz)
            self._set(segment_index=idx + 1,
                      current_segment={"low_mhz": seg.low_mhz, "high_mhz": seg.high_mhz,
                                       "band_tag": seg.band_tag,
                                       "rbw_khz": round(seg.rbw_khz(plan.sweep_points), 1),
                                       "expected": seg_expected},
                      eta_seconds=int(remaining),
                      message=f"segment {idx + 1}/{len(plan.segments)}: "
                              f"{seg.low_mhz:.3f}-{seg.high_mhz:.3f} MHz [{seg.band_tag}]")
            for attempt in range(1, MAX_SEGMENT_RETRIES + 1):
                try:
                    self._sweep_segment(session_id, sweep_id, phase, module, room, seg,
                                        plan, baseline_rows, threshold_db, merge_gap,
                                        seg_expected)
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
            # F5: in custom manual mode, hold here until the operator advances.
            # Still wakes on abort (abort() sets advance_event). Last segment never waits.
            if (self.manual_advance and idx < len(plan.segments) - 1
                    and not self.abort_event.is_set()):
                self.advance_event.clear()
                self._set(awaiting_advance=True,
                          message=f"segment {idx + 1}/{len(plan.segments)} done — "
                                  "tap NEXT SEGMENT to continue")
                while not self.advance_event.is_set() and not self.abort_event.is_set():
                    self.advance_event.wait(0.25)
                self._set(awaiting_advance=False)

    def _sweep_segment(self, session_id, sweep_id, phase, module, room, seg, plan,
                       baseline_rows, threshold_db, merge_gap, seg_expected=None):
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
                                  "low": seg.low_mhz, "high": seg.high_mhz,
                                  "ts": frame.ts})

        # An SDR frame is ALREADY a fully-averaged stepped sweep (each PSD bin
        # averages ~hundreds of FFTs internally), so the RF Explorer's many host
        # AVERAGE passes are redundant — cap those. DO NOT cap the MAX-HOLD
        # dwell: dwell is real time spent watching each band for intermittent /
        # bursty / frequency-hopping emitters, which is the entire point of a
        # TSCM sweep. Dwell stays governed by the operator's profile (Quick /
        # Standard / Deep) and the per-band risk weighting (more on ISM bug bands).
        avg_passes, dwell = seg.avg_passes, seg.maxhold_dwell_s
        if getattr(dev, "is_swept_sdr", False):
            avg_passes = min(avg_passes,
                             int(self.cfg.get("pluto", "sweep_avg_passes", default=2)))
        # Pass 1 — AVERAGE
        dev.set_calculator("average")
        dev.collect(avg_passes, on_frame, abort=self.abort_event)
        # Pass 2 — MAX HOLD (device DSP transient capture)
        dev.set_calculator("maxhold")
        dev.stream(dwell, on_frame, abort=self.abort_event)
        dev.set_calculator("normal")

        floor, signals = detect_signals(acc.freqs, acc.avg, acc.maxpeak, acc.hits,
                                        acc.passes, threshold_db, merge_gap)
        # low-frequency hygiene: conducted noise / analyzer LF artifacts pile up
        # at the bottom of the range; ignore below the floor frequency and demand
        # extra SNR in the HF region
        ignore_below = float(self.cfg.get("detection", "ignore_below_mhz", default=0.15))
        lf_extra_db = float(self.cfg.get("detection", "lf_extra_threshold_db", default=6.0))
        lf_region = float(self.cfg.get("detection", "lf_extra_below_mhz", default=50.0))
        signals = [s for s in signals
                   if s.center_mhz >= ignore_below
                   and (s.center_mhz >= lf_region
                        or s.peak_dbm >= floor + threshold_db + lf_extra_db)]
        segment_id = self.db.execute(
            "INSERT INTO segments (sweep_id,low_mhz,high_mhz,noise_floor_dbm,avg_passes,"
            "maxhold_dwell_s,status,data_json) VALUES (?,?,?,?,?,?,?,?)",
            (sweep_id, seg.low_mhz, seg.high_mhz, round(floor, 1), seg.avg_passes,
             seg.maxhold_dwell_s, "done", json.dumps(acc.to_dict())))
        detected = []
        for s in signals:
            cls = classify_signal(s.center_mhz, baseline_rows)
            detected.append((s, cls))
            self.db.execute(
                "INSERT INTO signals (sweep_id,session_id,segment_id,room,module,phase,"
                "center_mhz,bw_khz,peak_dbm,avg_dbm,floor_dbm,persistence,classification,"
                "matched_label,match_reason,caution,identity_tier,display_label,"
                "group_key,group_label) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (sweep_id, session_id, segment_id, room, module, phase,
                 s.center_mhz, s.bw_khz, s.peak_dbm, s.avg_dbm, s.floor_dbm,
                 s.persistence, cls.classification, cls.matched_label,
                 cls.match_reason, int(cls.caution), cls.identity_tier,
                 cls.display_label, cls.group_key, cls.group_label))

        # F2: flip expected records found, accumulate anomalies for the side panel.
        # Same _matches rule the report uses, so live panel and PDF agree.
        if seg_expected:
            updated = [{**e, "status": "found"} if any(_matches(e, s.center_mhz)
                                                       for s, _ in detected)
                       else e for e in seg_expected]
            cur = dict(self.state.get("current_segment") or {})
            cur["expected"] = updated
            self._set(current_segment=cur)
        new_anoms = [{"center_mhz": s.center_mhz, "peak_dbm": s.peak_dbm,
                      "bw_khz": s.bw_khz,
                      "label": cls.display_label or cls.matched_label or "",
                      "reason": cls.match_reason or ""}
                     for s, cls in detected
                     if cls.identity_tier == "ANOMALY" or cls.caution]
        if new_anoms:
            self._anomalies.extend(new_anoms)
        self._set(signal_count=self.state["signal_count"] + len(signals),
                  anomalies_found=list(self._anomalies))

    # ---- signal capture (wiki fingerprints) ----

    def start_captures(self, session_id: int, targets: list[dict], images_dir: str,
                       address: str = ""):
        """Zoom onto each target signal and record a high-iteration fingerprint.
        targets: signal rows (need center_mhz, bw_khz; room/module/etc. optional)."""
        if self.busy():
            raise RuntimeError("a sweep is already running")
        self.listen_stop.set()           # stop any live audio stream and let it release the radio
        time.sleep(0.4)
        self._ensure_device()
        self.abort_event.clear()
        os.makedirs(images_dir, exist_ok=True)
        cfg = self.cfg
        avg_passes = int(cfg.get("capture", "avg_passes", default=20))
        dwell_s = float(cfg.get("capture", "maxhold_dwell_s", default=12.0))

        def work():
            from ..sigcapture import (ai_description, auto_category, find_matches,
                                      render_capture_svg, rule_based_description,
                                      signal_features)
            results = []
            try:
                for i, t in enumerate(targets):
                    if self.abort_event.is_set():
                        break
                    self._set(capture={"running": True, "index": i + 1,
                                       "total": len(targets),
                                       "center_mhz": t["center_mhz"], "done": results},
                              message=f"capturing {t['center_mhz']:.4f} MHz "
                                      f"({i + 1}/{len(targets)})")
                    try:
                        cap_id, matches = self._capture_one(
                            session_id, t, images_dir, address, avg_passes, dwell_s,
                            auto_category, render_capture_svg, signal_features,
                            rule_based_description, ai_description, find_matches)
                        results.append({"capture_id": cap_id,
                                        "center_mhz": t["center_mhz"],
                                        "matches": matches})
                    except Exception as e:  # noqa: BLE001 - keep capturing the rest
                        log.exception("capture failed for %s", t.get("center_mhz"))
                        results.append({"center_mhz": t.get("center_mhz"),
                                        "error": str(e)})
            finally:
                match_count = sum(len(r.get("matches", [])) for r in results)
                self._set(running=False,
                          capture={"running": False, "done": results,
                                   "total": len(targets)},
                          message=f"capture run finished: {len(results)} signals "
                                  f"documented, {match_count} library matches")

        self._thread = threading.Thread(target=work, daemon=True)
        self._busy_source = self.active_source
        self._set(running=True, phase="capture", error=None,
                  message=f"signal capture: {len(targets)} targets")
        self._thread.start()

    def _capture_one(self, session_id, t, images_dir, address, avg_passes, dwell_s,
                     auto_category, render_capture_svg, signal_features,
                     rule_based_description, ai_description, find_matches):
        from .detector import Accumulator, detect_signals
        dev = self.device
        center = float(t["center_mhz"])
        bw_khz = float(t.get("bw_khz") or 100.0)
        span = min(max(0.5, bw_khz / 1000.0 * 8), 20.0)
        covering = [m for m in dev.info.modules
                    if m.min_mhz <= center <= m.max_mhz and m.max_mhz > m.min_mhz]
        side = min(covering or dev.info.modules,
                   key=lambda m: m.max_mhz - m.min_mhz).side
        dev.select_module(side)
        lo = max(center - span / 2, dev.info.modules[0].min_mhz, 0.05)
        dev.set_range(lo, lo + span)
        first = dev.read_sweep(timeout_s=10.0)
        if first is None:
            raise DeviceError("no sweep data in capture zoom")
        acc = Accumulator(first.freqs_mhz)
        acc.add_sweep(first.amps_dbm)

        def on_frame(frame):
            acc.add_sweep(frame.amps_dbm)
            self._set(live_trace={"freqs": [round(f, 5) for f in frame.freqs_mhz],
                                  "amps": [round(a, 1) for a in frame.amps_dbm],
                                  "avg": [round(a, 1) for a in acc.avg[:len(frame.amps_dbm)]],
                                  "maxpeak": [round(a, 1) for a in acc.maxpeak[:len(frame.amps_dbm)]],
                                  "low": lo, "high": lo + span, "ts": frame.ts})

        dev.set_calculator("average")
        dev.collect(avg_passes, on_frame, abort=self.abort_event)
        dev.set_calculator("maxhold")
        dev.stream(dwell_s, on_frame, abort=self.abort_event)
        dev.set_calculator("normal")

        floor, sigs = detect_signals(acc.freqs, acc.avg, acc.maxpeak, acc.hits,
                                     acc.passes, threshold_db=9.0)
        refined = min(sigs, key=lambda s: abs(s.center_mhz - center)) if sigs else None
        peak = refined.peak_dbm if refined else max(acc.maxpeak)
        r_center = refined.center_mhz if refined else center
        r_bw = refined.bw_khz if refined else bw_khz
        persistence = refined.persistence if refined else t.get("persistence")

        category = auto_category(t.get("classification", ""), t.get("matched_label", ""),
                                 r_center, bool(t.get("caution")))
        label = (t.get("matched_label") or f"{r_center:.4f} MHz "
                 + ("unverified signal" if t.get("classification") == "UNVERIFIED"
                    else "signal"))
        captured = time.strftime("%Y-%m-%d %H:%M")
        data = acc.to_dict()
        feats = signal_features(data, floor)
        meta = {"center_mhz": r_center, "peak_dbm": peak, "floor_dbm": floor,
                "bw_khz": r_bw, "persistence": persistence, "label": label,
                "session_id": session_id, "address": address,
                "room": t.get("room") or "", "module": side, "captured": captured}
        svg = render_capture_svg(data, meta)
        fname = f"cap_s{session_id}_{int(r_center * 10000)}_{int(time.time())}.svg"
        path = os.path.join(images_dir, fname)
        with open(path, "w", encoding="utf-8") as f:
            f.write(svg)

        cap_row = {"center_mhz": r_center, "bw_khz": r_bw, "peak_dbm": peak,
                   "floor_dbm": floor, "persistence": persistence,
                   "classification": t.get("classification", ""),
                   "matched_label": t.get("matched_label", "")}
        description = rule_based_description(cap_row, feats)
        ai_text, ai_err = ai_description(self.cfg, cap_row, feats)
        if ai_text:
            description = ai_text
        elif ai_err:
            log.info("AI description unavailable: %s", ai_err)

        status = "identified" if t.get("classification") == "VERIFIED" else "unconfirmed"
        cap_id = self.db.execute(
            "INSERT INTO captures (session_id,signal_id,center_mhz,span_mhz,bw_khz,"
            "peak_dbm,floor_dbm,persistence,room,module,captured_utc,image_path,"
            "data_json,features_json,classification,matched_label,category,label,"
            "status,description) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (session_id, t.get("id"), round(r_center, 5), span, round(r_bw, 1),
             round(peak, 1), round(floor, 1), persistence,
             t.get("room") or "", side, time.time(), fname,
             json.dumps(data), json.dumps(feats),
             t.get("classification", ""), t.get("matched_label", ""),
             category, label, status, description))
        matches = find_matches(self.db, {"id": cap_id, "center_mhz": r_center,
                                         "bw_khz": r_bw},
                               exclude_session=session_id)
        return cap_id, matches

    # ---- directional follow-up ----

    def start_directional(self, session_id: int, center_mhz: float, bw_khz: float,
                          room: str):
        if self.busy():
            raise RuntimeError("a sweep is already running")
        self.listen_stop.set()           # stop any live audio stream and let it release the radio
        time.sleep(0.4)
        self._ensure_device()
        self.abort_event.clear()
        span_mhz = max(2.0, (bw_khz / 1000.0) * 4)
        lo, hi = center_mhz - span_mhz / 2, center_mhz + span_mhz / 2

        def work():
            dev = self.device
            try:
                # use the narrowest module covering the frequency (best RBW)
                covering = [m for m in dev.info.modules
                            if m.min_mhz <= center_mhz <= m.max_mhz and m.max_mhz > m.min_mhz]
                pool = covering or dev.info.modules
                side = min(pool, key=lambda m: m.max_mhz - m.min_mhz).side
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
        self._busy_source = self.active_source
        self._set(running=True, phase="directional", room=room, error=None,
                  message=f"directional lock on {center_mhz:.4f} MHz — sweep the antenna")
        self._thread.start()

    # ---- monitor recording (WAV capture of one signal) ----

    def start_recording(self, session_id: int, center_mhz: float, recordings_dir: str,
                        mode: str = "nfm", offset_hz: float = 0.0, signal_id=None,
                        kind: str = "audio"):
        """Record ONE monitored signal under recordings/<session_id>/ (gitignored,
        client-sensitive). kind='audio' writes demodulated WAV (listen back);
        kind='iq' writes the RAW complex64 IQ (.cf32) — forensic evidence of an
        undecodable/encrypted intruder to hand to authorities (you can't read
        encrypted content, but the raw capture + identification + location is the
        evidence package). Worker mirrors start_directional: a daemon thread holds
        the radio lock and streams live IQ until stop_recording() or max length."""
        dev, lock, stop_evt, src = self.monitor_target()
        concurrent = self.concurrent_enabled() and src == "pluto"
        if concurrent:
            # dual-device: only the monitor radio must be free; the RFE may sweep
            if self.busy(src):
                raise RuntimeError("the monitor radio is already busy")
        elif self.busy():
            raise RuntimeError("a sweep is running — stop it to record")
        if self.recording and self.recording.get("active"):
            raise RuntimeError("a recording is already in progress")
        stop_evt.set()                   # free THIS radio from any live audio stream
        time.sleep(0.4)
        if not concurrent:
            self._ensure_device()
            dev = self.device
        if not (dev is not None and hasattr(dev, "stream_iq")
                and getattr(dev, "is_swept_sdr", False)):
            raise RuntimeError("connect the Pluto (SDR) to record")
        fs = float(self.cfg.get("pluto", "stream_sample_rate", default=2_400_000))
        n_block = int(fs * float(self.cfg.get("pluto", "listen_block_seconds", default=0.5)))
        max_secs = float(self.cfg.get("recordings", "max_seconds", default=120.0))
        sess_dir = os.path.join(recordings_dir, str(session_id))
        os.makedirs(sess_dir, exist_ok=True)
        is_iq = (kind == "iq")
        ext = "cf32" if is_iq else "wav"
        tag = "IQ" if is_iq else mode
        fname = f"{int(time.time())}_{int(round(center_mhz * 1000))}khz_{tag}.{ext}"
        rel_path = os.path.join(str(session_id), fname)
        abs_path = os.path.join(recordings_dir, rel_path)
        self.record_stop.clear()
        rec = {"active": True, "center_mhz": center_mhz, "mode": mode, "kind": kind,
               "started_utc": time.time(), "file_path": rel_path,
               "signal_id": signal_id, "offset_hz": offset_hz, "recording_id": None}
        self.recording = rec

        def work():
            import numpy as _np
            from ..demod import demodulate, wav_bytes
            chunks, arate, iq_samples = [], None, 0
            try:
                with lock:               # per-source in dual mode; legacy lock otherwise
                    for iq in dev.stream_iq(center_mhz * 1e6, fs, n_block, self.record_stop):
                        if is_iq:
                            chunks.append(_np.asarray(iq, dtype=_np.complex64))
                            iq_samples += len(iq)
                        else:
                            try:
                                pcm, arate = demodulate(iq, fs, mode, offset_hz)
                            except Exception:  # noqa: BLE001
                                continue
                            chunks.append(pcm)
                        if (time.time() - rec["started_utc"]) >= max_secs:
                            break
            except Exception:  # noqa: BLE001
                log.exception("recording worker failed")
            finally:
                try:
                    if is_iq and chunks:
                        # raw complex64 evidence; metadata (freq/time/session) lives
                        # in the recordings row for chain-of-custody.
                        _np.concatenate(chunks).astype(_np.complex64).tofile(abs_path)
                        dur = iq_samples / float(fs)
                        rid = self.db.add_recording(
                            session_id, round(center_mhz, 5), rel_path,
                            signal_id=signal_id, offset_hz=offset_hz, mode="iq",
                            duration_s=round(dur, 2), sample_rate=int(fs), kind="iq",
                            notes="raw IQ evidence (complex64 @ fs)")
                        rec["recording_id"] = rid
                        rec["duration_s"] = round(dur, 2)
                    elif chunks and arate:
                        pcm_all = _np.concatenate(chunks)
                        with open(abs_path, "wb") as f:
                            f.write(wav_bytes(pcm_all, arate))
                        dur = len(pcm_all) / float(arate)
                        rid = self.db.add_recording(
                            session_id, round(center_mhz, 5), rel_path,
                            signal_id=signal_id, offset_hz=offset_hz, mode=mode,
                            duration_s=round(dur, 2), sample_rate=int(arate),
                            kind="audio")
                        rec["recording_id"] = rid
                        rec["duration_s"] = round(dur, 2)
                except Exception:  # noqa: BLE001
                    # never-delete policy: keep the written file, just flag it as an
                    # orphan (filename embeds session/freq/time for later recovery).
                    log.exception("recording finalize failed; file kept at %s with NO "
                                  "DB row (orphaned evidence — recover manually)", abs_path)
                rec["active"] = False
                # In dual mode the sweep owns the global running flag — don't
                # clobber it from the concurrent monitor thread.
                if not concurrent:
                    self._set(running=False,
                              message=f"recording saved for {center_mhz:.4f} MHz"
                                      if rec.get("recording_id")
                                      else "recording produced no audio")

        t = threading.Thread(target=work, daemon=True)
        if concurrent:
            self._monitor_thread = t
            self._monitor_source = src
        else:
            self._thread = t
            self._busy_source = self.active_source
            self._set(running=True, phase="recording", error=None,
                      message=f"recording {center_mhz:.4f} MHz ({mode})")
        t.start()
        return rec

    def stop_recording(self):
        """Signal the record worker to finish, wait for it to flush the WAV +
        insert the row, and return the recording descriptor. No-op-safe; joins
        whichever slot (sweep-thread in legacy mode, monitor-thread in dual)."""
        self.record_stop.set()
        for t in (self._monitor_thread, self._thread):
            if t and t.is_alive():
                t.join(timeout=8.0)
        return self.recording or {}


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
            "match_reason=?, caution=?, identity_tier=?, display_label=?, "
            "group_key=?, group_label=? WHERE id=?",
            (round(delta, 1) if delta is not None else None,
             loc_delta, strongest, score, cls.classification, cls.matched_label,
             cls.match_reason, int(cls.caution), cls.identity_tier,
             cls.display_label, cls.group_key, cls.group_label, s["id"]))

    # ambient signals get classified too (no localization boosts)
    for s in ambient:
        cls = classify_signal(s["center_mhz"], baseline_rows)
        score = anomaly_score(cls, s["persistence"], None, None,
                              interior_flag, room_flag)
        db.execute(
            "UPDATE signals SET anomaly_score=?, classification=?, matched_label=?, "
            "match_reason=?, caution=?, identity_tier=?, display_label=?, "
            "group_key=?, group_label=? WHERE id=?",
            (score, cls.classification, cls.matched_label, cls.match_reason,
             int(cls.caution), cls.identity_tier, cls.display_label,
             cls.group_key, cls.group_label, s["id"]))
