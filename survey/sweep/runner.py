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

from ..correlation import (_matches, classify_signal, compute_interior_delta,
                           compute_room_localization, explain_anomaly_score)
from ..expected import expected_for_range
from .detector import Accumulator, detect_signals
from .device import DeviceError

log = logging.getLogger("survey.runner")

MAX_SEGMENT_RETRIES = 3


def directional_target_peak(freqs_mhz, amps_dbm, center_mhz, bw_khz):
    """Peak inside the target carrier window plus the displayed window width."""
    target_half_mhz = max(0.025, (float(bw_khz) / 1000.0) * 0.75)
    target_amps = [a for f, a in zip(freqs_mhz, amps_dbm)
                   if abs(f - float(center_mhz)) <= target_half_mhz]
    return max(target_amps or amps_dbm), target_half_mhz * 2


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
        # serialise the Pluto auto-monitor probe (background poller vs a request)
        self._monitor_probe_lock = threading.Lock()
        self._pluto_auto_fail_ts = 0.0
        # operator role override (swap_roles/set_roles): which source is the
        # MONITOR. None = auto (Pluto-preferred). The sweeper is always active_source.
        self._monitor_override = None
        # serialise radio access across sweep / snapshot-capture / live audio stream
        self.device_lock = threading.Lock()
        self.listen_stop = threading.Event()   # set to stop the active audio stream
        self.listening = False
        self.listen_spectrum = None            # latest live analyzer+waterfall from the audio stream
        self.listen_error = None               # why the last audio stream died, for the UI to show
        self.record_stop = threading.Event()   # set to stop the active monitor recording
        self.recording = None                  # active/last recording descriptor
        # F8 phase-interferometry DF (B220 dual-RX): dedicated stop event so
        # /api/df/stop can't kill an unrelated sweep (abort() still sets it —
        # the global panic button keeps working). Calibration is per-tune:
        # df_cal holds {freq_hz, offset_deg} and any retune invalidates it.
        self.df_stop = threading.Event()
        self.df_cal = None
        self.df_cal_request = False
        # F5 manual segment advance (custom mode): when on, the worker pauses
        # between segments until advance_event fires (still honouring abort_event).
        self.manual_advance = False
        self.advance_event = threading.Event()
        # F6 sustain: operator holds the CURRENT segment — after its passes finish
        # the worker keeps re-sweeping the same span live (view-only, no re-detect)
        # until RESUME or abort. Works on every sweep kind, any profile.
        self.sustain_flag = False
        # F7: confirmed hard abort — erase the aborted sweep's data (vs the
        # default pause-and-save). Set via abort(erase=True), applied in work().
        self.erase_on_abort = False
        # F2 anomaly accumulator — persists across segments for the whole sweep.
        self._anomalies = []
        self.state = {"running": False, "phase": None, "module": None, "room": None,
                      "segment_index": 0, "segment_total": 0, "percent": 0.0,
                      "eta_seconds": 0, "signal_count": 0, "message": "idle",
                      "live_trace": None, "current_segment": None, "error": None,
                      "directional": None, "calibration": None, "capture": None,
                      "awaiting_advance": False, "anomalies_found": [],
                      "sustain_requested": False, "sustaining": False,
                      "df": None}
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
            # a full close-all resets any operator role override — the fresh
            # connect starts from the auto default (Pluto-preferred monitor)
            self._monitor_override = None
        dev = open_device(self.cfg, source=src)
        self.devices[src] = dev
        self.device_locks.setdefault(src, threading.Lock())
        self.listen_stops.setdefault(src, threading.Event())
        # self.device is the SWEEPER: in dual mode keep the existing sweeper so a
        # sweep stays on the RFE while the just-added Pluto becomes the monitor.
        # src == active_source: a same-source RECONNECT of the sweeper must take
        # over the handle, or self.device keeps pointing at the dead radio.
        if (not (keep_other and self.concurrent_enabled()) or self.device is None
                or src == self.active_source):
            self.device = dev
            self.active_source = src
        # dual mode: default the Pluto to monitor — so once the Pluto plus ANY
        # other radio (RFE or B220) are open, make the other radio the sweeper
        # regardless of connect order. SKIPPED when the operator has explicitly
        # assigned roles (swap_roles/set_roles) so a reconnect can't stomp their
        # choice; never yanks a sweep in flight.
        if (self.concurrent_enabled() and not self.busy()
                and self._monitor_override is None
                and "pluto" in self.devices and len(self.devices) > 1):
            sweeper = next(s for s in self.devices if s != "pluto")
            self.device = self.devices[sweeper]
            self.active_source = sweeper
        return dev.info

    def _ensure_device(self):
        if self.device is None or not self.device.is_alive():
            log.warning("device missing or dead — reconnecting")
            # reconnect the radio we were actually using, NOT the config-file
            # default (roles may differ from device.source after a promotion or
            # swap), and keep the concurrent monitor open instead of closing
            # every radio for a sweeper-only hiccup
            self.connect_device(source=self.active_source,
                                keep_other=self.active_source is not None)

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
        # same rule as _ensure_device: mid-sweep reconnects go to the radio the
        # sweep was running on and never tear down the other radio's stream
        self.connect_device(source=self.active_source,
                            keep_other=self.active_source is not None)

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

    def _current_monitor(self):
        """Which connected radio is the MONITOR (live listen / analyze / record),
        or None in single-device mode. Precedence: the operator's explicit role
        override (set via swap_roles / set_roles) if that radio is connected and
        isn't the sweeper; else the auto default — the Pluto when it isn't the
        sweeper, otherwise any other connected radio. The SWEEPER is always
        active_source, so the monitor is 'some other radio than the sweeper'."""
        if not self.concurrent_enabled():
            return None
        ov = self._monitor_override
        if ov and ov in self.devices and ov != self.active_source:
            return ov
        if "pluto" in self.devices and self.active_source != "pluto":
            return "pluto"
        return next((s for s in self.devices if s != self.active_source), None)

    def monitor_target(self):
        """(device, lock, listen_stop, source) for live monitor / analyze /
        record. In dual mode returns the MONITOR radio + its OWN lock so it never
        blocks on the sweeper's lock; otherwise the active device + the legacy
        lock (so single-device behaviour is unchanged)."""
        mon = self._current_monitor()
        if mon:
            return (self.devices[mon], self.device_locks.setdefault(mon, threading.Lock()),
                    self.listen_stops.setdefault(mon, threading.Event()), mon)
        return self.device, self.device_lock, self.listen_stop, self.active_source

    def set_roles(self, sweeper=None, monitor=None):
        """Operator-assigned radio roles (field resilience — either radio can
        sweep or monitor). Both must be connected and distinct; refused while a
        sweep is running (the sweeper is actively streaming). Persists until
        changed or a radio drops (a stale override self-heals via _current_monitor)."""
        if self.busy():
            raise RuntimeError("stop the sweep before changing radio roles")
        if len(self.devices) < 2:
            raise RuntimeError("connect both radios before assigning roles")
        for r in (sweeper, monitor):
            if r and r not in self.devices:
                raise RuntimeError(f"{r} is not connected")
        if sweeper and monitor and sweeper == monitor:
            raise RuntimeError("the sweeper and monitor must be different radios")
        if sweeper:
            self.device = self.devices[sweeper]
            self.active_source = sweeper
        if monitor:
            self._monitor_override = monitor
        return self.device_roles()

    def swap_roles(self):
        """Swap sweeper <-> monitor — the field 'just switch them around' button
        for when the preferred monitor radio can't do the job (bad USB, weak
        antenna, etc.)."""
        if self.busy():
            raise RuntimeError("stop the sweep before swapping radio roles")
        if len(self.devices) < 2:
            raise RuntimeError("connect both radios to swap roles")
        new_sweeper = self._current_monitor()
        new_monitor = self.active_source
        if not new_sweeper or new_sweeper == new_monitor:
            raise RuntimeError("need two distinct radios to swap")
        return self.set_roles(sweeper=new_sweeper, monitor=new_monitor)

    def _evict_dead_monitor(self):
        """Drop a DEAD monitor handle from self.devices so the autoseek /
        ensure_monitor machinery can reopen it. Without this a crashed monitor
        (worker death or USB drop) lingers in the dict forever: the membership
        checks below keep 'finding' it and every monitor action fails until a
        manual reconnect. Never touches the sweeper (its recovery is
        _ensure_device / the segment-retry loop)."""
        mon = self._current_monitor()
        if not mon or mon == self.active_source:
            return
        d = self.devices.get(mon)
        try:
            dead = d is not None and not d.is_alive()
        except Exception:  # noqa: BLE001 — a broken handle counts as dead
            dead = True
        if not dead:
            return
        with self._monitor_probe_lock:
            d2 = self.devices.get(mon)
            if d2 is not d:                 # raced a reconnect — leave it be
                return
            self.devices.pop(mon, None)
            try:
                d.close()
            except Exception:  # noqa: BLE001
                pass
            log.warning("monitor radio %s is dead — evicted; autoseek/"
                        "ensure_monitor will reopen it", mon)

    def _try_open_pluto_monitor(self) -> bool:
        """Probe for a Pluto and, if present, open it as the concurrent monitor
        WITHOUT disturbing the sweeper. Non-fatal + cooldown-guarded so a missing
        or faulting Pluto (unplugged, bad USB, driver) is retried at most once per
        60 s instead of stalling every call. Returns True if the Pluto is now
        connected. Serialised so the background poller and a request thread can't
        both open it at once."""
        self._evict_dead_monitor()          # a dead handle must not block reopen
        if not self.concurrent_enabled() or "pluto" in self.devices:
            return "pluto" in self.devices
        if self.active_source in (None, "pluto"):
            return False   # no primary yet, or the Pluto IS the primary
        if time.time() - getattr(self, "_pluto_auto_fail_ts", 0.0) <= 60.0:
            return False   # inside the failure cooldown
        with self._monitor_probe_lock:
            if "pluto" in self.devices:
                return True
            try:
                from .device import open_device
                dev = open_device(self.cfg, source="pluto")
                self.devices["pluto"] = dev
                self.device_locks.setdefault("pluto", threading.Lock())
                self.listen_stops.setdefault("pluto", threading.Event())
                self._pluto_auto_fail_ts = 0.0
                log.info("auto-connected the Pluto as the concurrent monitor "
                         "(sweeper stays on %s)", self.active_source)
                return True
            except Exception as e:  # noqa: BLE001
                self._pluto_auto_fail_ts = time.time()
                log.warning("could not auto-connect the Pluto monitor: %s", e)
                return False

    def ensure_monitor(self):
        """Monitor-ACTION entry point (inspect / analyze / listen / record): like
        monitor_target(), but proactively opens the Pluto as the concurrent
        monitor if it's present and not open yet — so the operator never
        hand-connects the second radio. Failure is non-fatal (cooldown-guarded).
        Read-only pollers (device_roles, /api/state) keep calling monitor_target()
        so a status poll can never spin up hardware."""
        self._try_open_pluto_monitor()
        return self.monitor_target()

    def autoseek_monitor(self):
        """Proactive seek — ONE probe, on an operator action: bring the Pluto up
        as the monitor the moment it's detected, no manual connect required
        (operator ask 2026-07-11: 'it should seek out the Pluto and automatically
        connect'). Called from the DEVICE-card connect and from ensure_monitor()
        on every monitor action (inspect / analyze / listen / record).

        THERE IS NO BACKGROUND SEEK. A polling thread used to run this every 20 s
        for the life of the process; with no Pluto attached it spawned a worker
        PROCESS and enumerated USB forever — chopping live audio roughly once a
        minute, respawning a worker every ~63 s, and writing ~5 MB of log an hour
        (removed 2026-08-03, operator call). The cost of that is the ONLY thing
        lost: plugging the Pluto in mid-session is now noticed on your next
        click instead of on its own."""
        return self._try_open_pluto_monitor()

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

    def abort(self, erase: bool = False):
        """Stop the active worker. erase=False (default) is 'pause the radio,
        save the session' — everything recorded so far is kept. erase=True is a
        confirmed hard abort: this sweep's rows (signals/segments/sweep) are
        deleted once the worker stops. Baseline and other sweeps are untouched."""
        self.erase_on_abort = bool(erase)
        self.abort_event.set()
        self.advance_event.set()   # wake a manually-paused sweep so it can stop
        self.df_stop.set()         # the global panic button stops DF too
        self.sustain_flag = False  # a sustained segment must not outlive the sweep
        self._set(message="abort requested — erasing this sweep's data" if erase
                          else "abort requested — keeping data collected so far",
                  sustain_requested=False)

    def advance(self):
        """F5: operator tapped NEXT SEGMENT — release the manual-advance gate."""
        self.advance_event.set()

    def set_sustain(self, on: bool):
        """F6: hold/release the current segment. Turning it on mid-segment takes
        effect when the segment's passes finish; turning it off releases the
        live-watch loop so the sweep moves on."""
        self.sustain_flag = bool(on)
        self._set(sustain_requested=self.sustain_flag)

    def set_live_rbw(self, rbw_khz):
        """Turn the RBW knob (SDR only) — mid-sweep it re-tunes resolution on the
        NEXT segment (_run_plan re-applies self.rbw_khz when it changes); with no
        sweep running it just primes the value for the next start_sweep. Returns
        the achieved analysis RBW (kHz) if a device is connected, else None."""
        self.rbw_khz = float(rbw_khz) if rbw_khz else None
        self._set(rbw_khz=self.rbw_khz)
        dev = self.device
        if dev is not None and hasattr(dev, "predicted_rbw_khz"):
            try:
                # report what the CURRENT segment span would deliver (advisory)
                cs = self.state.get("current_segment") or {}
                span = (cs.get("high_mhz", 0) - cs.get("low_mhz", 0)) or 10.0
                return dev.predicted_rbw_khz(span, self.rbw_khz)
            except Exception:  # noqa: BLE001
                return None
        return None

    def _set(self, **kw):
        with self._lock:
            self.state.update(kw)

    def _assert_sweep_startable(self):
        """A sweep needs the SWEEP radio free. A monitor worker (record / DF) on
        the OTHER radio must not block it — 'one radio sweeping, the other honing
        in' is the whole point of dual mode. It still blocks in legacy single-
        device mode or when the worker is bound to the sweeper itself."""
        if self._thread and self._thread.is_alive():
            raise RuntimeError("a sweep is already running")
        if self._monitor_thread and self._monitor_thread.is_alive():
            if (not self.concurrent_enabled()
                    or self._monitor_source == self.active_source):
                raise RuntimeError("the monitor is recording on the sweep radio — "
                                   "stop it before sweeping")

    def start_sweep(self, session_id: int, phase: str, module: str, room: str,
                    plan, manual_advance: bool = False, rbw_khz=None, on_done=None):
        self._assert_sweep_startable()
        self.listen_stop.set()           # stop any live audio stream and let it release the radio
        time.sleep(0.4)
        self._ensure_device()
        self.abort_event.clear()
        self.manual_advance = bool(manual_advance)
        # operator RBW override for THIS sweep (SDR only; None restores config
        # defaults so a previous sweep's override never leaks into the next)
        self.rbw_khz = float(rbw_khz) if rbw_khz else None
        self.advance_event.clear()
        self.sustain_flag = False
        self.erase_on_abort = False
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
            finally:
                # the RBW override is PER-SWEEP: clear it the moment the worker
                # exits so it can't leak into signal captures, directional
                # passes, or calibration on the same device handle (adversarial
                # review 2026-07-11: a leaked 500 kHz override collapsed a
                # 0.8 MHz fingerprint zoom to 16 grid points)
                try:
                    if self.device is not None:
                        self.device.set_rbw_khz(None)
                except Exception:  # noqa: BLE001
                    pass
            # F7: confirmed hard abort — this sweep's data is erased instead of
            # finalized. Baseline, other sweeps, wiki captures and the session
            # itself are untouched; postprocess still runs so session-wide
            # deltas/localization recompute without the erased rows.
            erased = self.erase_on_abort and status in ("aborted", "error")
            if erased:
                try:
                    self.db.execute("DELETE FROM signals WHERE sweep_id=?", (sweep_id,))
                    self.db.execute("DELETE FROM segments WHERE sweep_id=?", (sweep_id,))
                    self.db.execute("DELETE FROM sweeps WHERE id=?", (sweep_id,))
                    log.info("sweep %s aborted — data erased at operator request", sweep_id)
                except Exception:  # noqa: BLE001
                    log.exception("sweep-data erase failed — rows kept")
                    erased = False
            if not erased:
                self.db.execute("UPDATE sweeps SET status=?, finished_utc=? WHERE id=?",
                                (status, time.time(), sweep_id))
            try:
                postprocess_session(self.db, self.cfg, session_id)
            except Exception:  # noqa: BLE001
                log.exception("postprocess failed")
            if not erased:
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
                      message=f"{phase} sweep ({module}) aborted — data erased" if erased
                              else f"{phase} sweep ({module}) {status}; "
                                   f"{self.state['signal_count']} signals detected")
            if on_done:
                on_done(status)

        self._thread = threading.Thread(target=work, daemon=True)
        self._busy_source = self.active_source
        self._set(running=True, phase=phase, module=module, room=room, error=None,
                  segment_index=0, segment_total=len(plan.segments), percent=0.0,
                  signal_count=0, sweep_id=sweep_id, awaiting_advance=False,
                  anomalies_found=[],
                  sustain_requested=False, sustaining=False,
                  message=f"starting {phase} sweep on {module} module")
        self._thread.start()
        return sweep_id

    def _run_plan(self, session_id, sweep_id, phase, module, room, plan):
        dev = self.device
        if hasattr(dev, "environment"):
            dev.environment = {"phase": phase, "room": room}
        dev.select_module(module if module in ("left", "right") else "right")
        # Operator RBW override (SDR only). ALWAYS called: None restores the
        # config defaults so the previous sweep's setting can't leak in. The
        # operator can also turn the RBW knob MID-SWEEP (set_live_rbw) — the loop
        # below re-applies self.rbw_khz whenever it changes, effective next segment.
        rbw_req = getattr(self, "rbw_khz", None)
        analysis_rbw = dev.set_rbw_khz(rbw_req)
        applied_rbw = rbw_req
        if rbw_req and analysis_rbw is None:
            self._set(message="RBW is fixed on the RF Explorer (span/112) — "
                              "use the Seg MHz control instead")
        sps = float(self.cfg.get("sweep", "seconds_per_sweep", default=0.35))
        threshold_db = float(self.cfg.get("detection", "threshold_db", default=12.0))
        merge_gap = int(self.cfg.get("detection", "merge_gap_bins", default=2))
        baseline_rows = self.db.query("SELECT * FROM baseline WHERE session_id=?",
                                      (session_id,))
        remaining = sum(s.avg_passes * sps + s.maxhold_dwell_s + 0.9 for s in plan.segments)

        for idx, seg in enumerate(plan.segments):
            if self.abort_event.is_set():
                break
            # LIVE RBW: the operator turned the knob mid-sweep — re-apply on this
            # (the next) segment. set_rbw_khz is cheap + idempotent; the seg_rbw
            # display and merge-gap scaling below then reflect the new value.
            cur_rbw = getattr(self, "rbw_khz", None)
            if cur_rbw != applied_rbw:
                analysis_rbw = dev.set_rbw_khz(cur_rbw)
                applied_rbw = rbw_req = cur_rbw
            # F2: what licensed records do we EXPECT in this segment's range —
            # published as status='looking', flipped to 'found' in _sweep_segment.
            seg_expected = expected_for_range(baseline_rows, seg.low_mhz, seg.high_mhz)
            # real delivered RBW: the SDR path knows (grid + FFT under any
            # override); the RFE falls back to the firmware span/112
            dev_rbw = dev.predicted_rbw_khz(seg.span_mhz)
            seg_rbw = dev_rbw if dev_rbw else seg.rbw_khz(plan.sweep_points)
            # merge_gap_bins is denominated in GRID BINS: at a finer-than-default
            # grid the same Hz gap spans more bins, so one modulated signal would
            # split into several detections — scale the gap to keep it constant
            # in Hz relative to the default grid.
            seg_gap = merge_gap
            if rbw_req and hasattr(dev, "grid_points") and analysis_rbw is not None:
                span_hz = seg.span_mhz * 1e6
                n_now = dev.grid_points(span_hz)
                sweep_bins = int(self.cfg.get(
                    getattr(dev, "CFG_SECTION", "pluto"), "sweep_bins", default=2048))
                n_default = max(16, min(sweep_bins, 1 + int(span_hz / 1e3)))
                if n_now > n_default:
                    seg_gap = max(merge_gap,
                                  int(round(merge_gap * (n_now - 1) / (n_default - 1))))
            self._set(segment_index=idx + 1,
                      current_segment={"low_mhz": seg.low_mhz, "high_mhz": seg.high_mhz,
                                       "band_tag": seg.band_tag,
                                       "rbw_khz": round(seg_rbw, 1),
                                       "expected": seg_expected},
                      eta_seconds=int(remaining),
                      message=f"segment {idx + 1}/{len(plan.segments)}: "
                              f"{seg.low_mhz:.3f}-{seg.high_mhz:.3f} MHz [{seg.band_tag}]"
                              + (f" · RBW {seg_rbw:.1f} kHz" if dev_rbw else ""))
            for attempt in range(1, MAX_SEGMENT_RETRIES + 1):
                try:
                    self._sweep_segment(session_id, sweep_id, phase, module, room, seg,
                                        plan, baseline_rows, threshold_db, seg_gap,
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
                    dev.set_rbw_khz(rbw_req)     # a fresh handle loses the override
            remaining -= seg.avg_passes * sps + seg.maxhold_dwell_s + 0.9
            self._set(percent=round(100.0 * (idx + 1) / len(plan.segments), 1),
                      eta_seconds=max(int(remaining), 0))
            # F6: operator sustained this segment — keep watching it live until
            # RESUME. View-only: detection/storage for the segment already ran.
            if self.sustain_flag and not self.abort_event.is_set():
                self._sustain_segment(idx, len(plan.segments), seg)
            # F5: in custom manual mode, hold here until the operator advances —
            # but KEEP SWEEPING the finished segment live (operator ask
            # 2026-07-11: "it just continues to sweep until the NEXT button is
            # hit"). Reuses the sustain live-watch loop with an advance-gated
            # predicate. Still wakes on abort (abort() sets advance_event too).
            # Last segment never waits.
            if (self.manual_advance and idx < len(plan.segments) - 1
                    and not self.abort_event.is_set()):
                self.advance_event.clear()
                self._set(awaiting_advance=True,
                          message=f"segment {idx + 1}/{len(plan.segments)} done — "
                                  f"live on {seg.low_mhz:.3f}-{seg.high_mhz:.3f} MHz; "
                                  "tap NEXT SEGMENT to continue")
                self._sustain_segment(
                    idx, len(plan.segments), seg,
                    keep=lambda: not self.advance_event.is_set(),
                    set_sustaining=False)
                # A device hiccup ends the live watch, but it must NOT advance
                # past the operator's gate — fall back to the idle wait.
                while not self.advance_event.is_set() and not self.abort_event.is_set():
                    self.advance_event.wait(0.25)
                self._set(awaiting_advance=False)

    def _sustain_segment(self, idx, total, seg, keep=None, set_sustaining=True):
        """F6: re-sweep the sustained segment continuously, publishing live_trace
        (realtime + running avg + max-hold) until the operator resumes. The device
        is still tuned to this segment from _sweep_segment. Never raises — a
        serial hiccup here just ends the hold and lets the sweep continue.

        `keep` generalizes the hold predicate so the F5 manual-advance gate can
        reuse this exact loop (keep = "NEXT not pressed yet"); the F6 default is
        the sustain flag. `set_sustaining=False` keeps the F6 UI (RESUME button,
        info tabs) out of the manual gate, which is driven by awaiting_advance."""
        dev = self.device
        acc = None
        if keep is None:
            keep = lambda: self.sustain_flag                       # noqa: E731
        # LIVE RETUNE: while watching this segment, an RBW change should take
        # effect on the NEXT FRAME (~sub-second), not wait for the next segment.
        applied_rbw = getattr(self, "rbw_khz", None)
        if set_sustaining:
            self._set(sustaining=True,
                      message=f"segment {idx + 1}/{total} SUSTAINED — live on "
                              f"{seg.low_mhz:.3f}–{seg.high_mhz:.3f} MHz "
                              "(view-only; RESUME to continue the sweep)")
        try:
            dev.set_calculator("maxhold")
            while keep() and not self.abort_event.is_set():
                # operator turned the RBW knob mid-segment -> re-tune the grid now
                cur_rbw = getattr(self, "rbw_khz", None)
                if cur_rbw != applied_rbw:
                    try:
                        dev.set_rbw_khz(cur_rbw)
                        dev.set_range(seg.low_mhz, seg.high_mhz)   # rebuild grid at new RBW
                        acc = None                                 # Accumulator resets to the new size
                    except (DeviceError, OSError):
                        pass
                    applied_rbw = cur_rbw
                frame = dev.read_sweep(timeout_s=3.0)
                if frame is None:
                    continue
                if acc is None or len(acc.freqs) != len(frame.freqs_mhz):
                    acc = Accumulator(frame.freqs_mhz)
                acc.add_sweep(frame.amps_dbm)
                n = len(frame.amps_dbm)
                self._set(live_trace={
                    "freqs": [round(f, 4) for f in frame.freqs_mhz],
                    "amps": [round(a, 1) for a in frame.amps_dbm],
                    "avg": [round(a, 1) for a in acc.avg[:n]],
                    "maxpeak": [round(a, 1) for a in acc.maxpeak[:n]],
                    "low": seg.low_mhz, "high": seg.high_mhz, "ts": frame.ts})
        except (DeviceError, OSError) as e:
            log.warning("sustain loop ended by device error: %s", e)
        finally:
            try:
                dev.set_calculator("normal")
            except Exception:  # noqa: BLE001
                pass
            if set_sustaining:
                self._set(sustaining=False)

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
                             int(self.cfg.get(getattr(dev, "CFG_SECTION", "pluto"),
                                              "sweep_avg_passes", default=2)))
        # Pass 1 — AVERAGE
        dev.set_calculator("average")
        dev.collect(avg_passes, on_frame, abort=self.abort_event)
        # Pass 2 — MAX HOLD (device DSP transient capture)
        dev.set_calculator("maxhold")
        dev.stream(dwell, on_frame, abort=self.abort_event)
        dev.set_calculator("normal")

        # Near-floor tier: a bug sweep exists to find LOW-POWER emitters, and a
        # single detection threshold was dropping signals the operator could
        # plainly see on the display. Anything between this and threshold_db
        # comes back tagged tier="candidate" — a lead for review, never a
        # finding. null/absent disables the tier and restores single-tier.
        # 8.0, not 6.0: config.yaml is gitignored, so THIS default is what ships.
        # Measured over 120 synthetic noise-only segments, 6 dB produced ~0.42
        # false candidates per segment (a 40-pass max-hold routinely peaks 6 dB
        # over the floor on noise alone); 8 dB produced zero.
        cand_db = self.cfg.get("detection", "candidate_threshold_db", default=8.0)
        cand_db = float(cand_db) if cand_db is not None else None
        pers_frac = float(self.cfg.get("detection", "persistence_min_frac",
                                       default=0.85))
        floor, signals = detect_signals(acc.freqs, acc.avg, acc.maxpeak, acc.hits,
                                        acc.passes, threshold_db, merge_gap,
                                        candidate_threshold_db=cand_db,
                                        low_hits=acc.low_hits,
                                        persistence_min_frac=pers_frac)
        # low-frequency hygiene: conducted noise / analyzer LF artifacts pile up
        # at the bottom of the range; ignore below the floor frequency and demand
        # extra SNR in the HF region
        ignore_below = float(self.cfg.get("detection", "ignore_below_mhz", default=0.15))
        lf_extra_db = float(self.cfg.get("detection", "lf_extra_threshold_db", default=6.0))
        lf_region = float(self.cfg.get("detection", "lf_extra_below_mhz", default=50.0))
        # LF hygiene applies to each signal against ITS OWN tier's threshold —
        # holding a near-floor candidate to the detection threshold would filter
        # out every candidate in the LF region, which is the opposite of the point.
        def _lf_ok(s):
            base = threshold_db if s.tier == "detection" else (cand_db or threshold_db)
            return (s.center_mhz >= lf_region
                    or s.peak_dbm >= floor + base + lf_extra_db)
        signals = [s for s in signals
                   if s.center_mhz >= ignore_below and _lf_ok(s)]
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
                "group_key,group_label,bin_start,bin_end,detected_utc,near_floor) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (sweep_id, session_id, segment_id, room, module, phase,
                 s.center_mhz, s.bw_khz, s.peak_dbm, s.avg_dbm, s.floor_dbm,
                 s.persistence, cls.classification, cls.matched_label,
                 cls.match_reason, int(cls.caution), cls.identity_tier,
                 cls.display_label, cls.group_key, cls.group_label,
                 s.bin_start, s.bin_end, time.time(),
                 1 if s.tier == "candidate" else 0))

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
        self.device.set_rbw_khz(None)    # fingerprints always run at config RBW
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
        self.device.set_rbw_khz(None)    # directional always runs at config RBW
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
                    # Track the requested carrier, not a stronger neighbor in
                    # the display span, so warmer/colder guidance points right.
                    cur, target_window_mhz = directional_target_peak(
                        frame.freqs_mhz, frame.amps_dbm, center_mhz, bw_khz)
                    peak_hold = max(peak_hold, cur)
                    self._set(directional={
                        "center_mhz": center_mhz, "room": room,
                        "current_dbm": round(cur, 1), "peak_dbm": round(peak_hold, 1),
                        "target_window_mhz": round(target_window_mhz, 4),
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

    # ---- phase-interferometry DF (B220 dual-RX) ----

    def df_calibrate(self):
        """Flag the DF worker to take its NEXT good measurement as the broadside
        zero. Field procedure: hold the antenna bar broadside to the emitter
        (both antennas equidistant from it) and tap CALIBRATE."""
        if not (self.state.get("df") or {}).get("running"):
            raise RuntimeError("start the phase DF loop first")
        self.df_cal_request = True

    def stop_df(self):
        self.df_stop.set()

    def start_df(self, session_id: int, center_mhz: float, bw_khz: float,
                 room: str, spacing_m: float | None = None):
        """Continuous dual-RX bearing loop on the B220. Receive-only. Publishes
        one atomic state dict per measurement into state['df']; logs a summary
        row into df_log on exit. Needs a radio with capture_iq_dual (B220)."""
        from ..df import measure

        if self.busy():
            raise RuntimeError("a sweep is already running")
        # the B220 is the only dual-RX radio; prefer it wherever it's connected
        dev = None
        src = None
        for s, d in self.devices.items():
            if hasattr(d, "capture_iq_dual"):
                dev, src = d, s
                break
        if dev is None and hasattr(self.device, "capture_iq_dual"):
            dev, src = self.device, self.active_source
        if dev is None:
            raise RuntimeError("phase DF needs the B220 (dual coherent RX) — "
                               "connect it as the device source first")
        if getattr(dev, "channels", 1) < 2:
            raise RuntimeError("the connected radio reports only one RX channel "
                               "— phase DF needs both")
        lock = self.device_locks.setdefault(src, threading.Lock()) \
            if src in self.devices else self.device_lock
        stop_stream = self.listen_stops.get(src, self.listen_stop)
        stop_stream.set()               # free the radio from any live audio stream
        time.sleep(0.4)

        cfg = self.cfg
        spacing = float(spacing_m or cfg.get("df", "spacing_m", default=0.35))
        fs = float(cfg.get("df", "sample_rate", default=5_000_000))
        n = int(cfg.get("df", "capture_samples", default=1 << 17))
        nperseg = int(cfg.get("df", "nperseg", default=4096))
        snr_gate = float(cfg.get("df", "snr_gate_db", default=6.0))
        coh_gate = float(cfg.get("df", "coherence_gate", default=0.5))
        center_hz = center_mhz * 1e6

        # a retune always invalidates the previous calibration
        if self.df_cal and abs(self.df_cal.get("freq_hz", 0) - center_hz) > 1.0:
            self.df_cal = None
        self.df_cal_request = False
        self.df_stop.clear()

        def work():
            best = None
            count = 0
            try:
                with lock:
                    while not self.df_stop.is_set():
                        try:
                            pair = dev.capture_iq_dual(center_hz, fs, n)
                        except Exception as e:  # noqa: BLE001 - keep trying briefly
                            log.warning("DF capture failed: %s", e)
                            self._set(df={"running": True, "center_mhz": center_mhz,
                                          "room": room, "error": str(e)})
                            if self.df_stop.wait(1.0):
                                break
                            continue
                        cal = self.df_cal["offset_deg"] if self.df_cal else None
                        m = measure(pair[0], pair[1], fs, center_hz, spacing,
                                    cal_offset_deg=cal, nperseg=nperseg,
                                    snr_gate_db=snr_gate, coherence_gate=coh_gate)
                        # operator tapped CALIBRATE: the next good phase is zero
                        if self.df_cal_request and m.verdict != "NO_SIGNAL":
                            self.df_cal = {"freq_hz": center_hz,
                                           "offset_deg": m.phase_deg}
                            self.df_cal_request = False
                            m.warnings.append("calibrated — broadside is now 0°")
                        count += 1
                        d = m.to_dict()
                        d.update({"running": True, "center_mhz": center_mhz,
                                  "room": room, "count": count,
                                  "spacing_m": spacing})
                        self._set(df=d)
                        if m.verdict == "BEARING_OK" and (
                                best is None or m.coherence > best["coherence"]):
                            best = {"bearing_deg": m.bearing_deg,
                                    "mirror_deg": m.mirror_deg,
                                    "coherence": m.coherence,
                                    "phase_deg": m.phase_cal_deg,
                                    "snr_db": min(m.snr_a_db, m.snr_b_db)}
                if best is not None:
                    self.db.execute(
                        "INSERT INTO df_log (session_id,center_mhz,room,bearing_deg,"
                        "mirror_deg,phase_deg,coherence,snr_db,spacing_m,calibrated,"
                        "notes,ts_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (session_id, center_mhz, room, round(best["bearing_deg"], 1),
                         round(best["mirror_deg"], 1), round(best["phase_deg"], 1),
                         round(best["coherence"], 3), round(best["snr_db"], 1),
                         spacing, int(self.df_cal is not None), "", time.time()))
            except Exception as e:  # noqa: BLE001
                log.exception("DF loop failed")
                self._set(error=str(e))
            finally:
                self._set(running=False, df=None,
                          message=(f"phase DF ended for {center_mhz:.4f} MHz"
                                   + (" — best bearing logged" if best else
                                      " — no reliable bearing to log")))

        self._thread = threading.Thread(target=work, daemon=True)
        self._busy_source = src
        self._set(running=True, phase="df", room=room, error=None,
                  df={"running": True, "center_mhz": center_mhz, "room": room},
                  message=f"phase DF on {center_mhz:.4f} MHz — spacing {spacing:.2f} m")
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
        dev, lock, stop_evt, src = self.ensure_monitor()
        concurrent = self.concurrent_enabled() and src == "pluto"
        if concurrent:
            # dual-device: only the monitor radio must be free; the sweeper may sweep
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
        sect = src if (src and self.cfg.get(src, "stream_sample_rate", default=None)
                       is not None) else "pluto"
        fs = float(self.cfg.get(sect, "stream_sample_rate", default=2_400_000))
        n_block = int(fs * float(self.cfg.get(sect, "listen_block_seconds", default=0.5)))
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
        expl = explain_anomaly_score(cls, s["persistence"], delta, loc_delta,
                                     interior_flag, room_flag, gps_band)
        db.execute(
            "UPDATE signals SET interior_delta_db=?, localization_score=?, "
            "strongest_room=?, anomaly_score=?, score_reason=?, classification=?, "
            "matched_label=?, match_reason=?, caution=?, identity_tier=?, "
            "display_label=?, group_key=?, group_label=? WHERE id=?",
            (round(delta, 1) if delta is not None else None,
             loc_delta, strongest, expl.score, expl.summary, cls.classification,
             cls.matched_label, cls.match_reason, int(cls.caution), cls.identity_tier,
             cls.display_label, cls.group_key, cls.group_label, s["id"]))

    # ambient signals get classified too (no localization boosts)
    for s in ambient:
        cls = classify_signal(s["center_mhz"], baseline_rows)
        expl = explain_anomaly_score(cls, s["persistence"], None, None,
                                     interior_flag, room_flag)
        db.execute(
            "UPDATE signals SET interior_delta_db=NULL, localization_score=NULL, "
            "strongest_room=NULL, anomaly_score=?, score_reason=?, classification=?, "
            "matched_label=?, match_reason=?, caution=?, identity_tier=?, "
            "display_label=?, group_key=?, group_label=? WHERE id=?",
            (expl.score, expl.summary, cls.classification, cls.matched_label,
             cls.match_reason, int(cls.caution), cls.identity_tier, cls.display_label,
             cls.group_key, cls.group_label, s["id"]))
