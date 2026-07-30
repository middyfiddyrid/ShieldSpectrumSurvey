"""Device-lab LIVE loop — continuous short IQ grabs with peak-hold.

The old lab capture was one 6.5 ms grab per button press; bursty DUTs (fobs,
Wi-Fi) were usually missed and the tech had to walk back to the laptop. This
loop owns the monitor radio (same lock the listen/analyze paths use) and keeps
grabbing until stopped, maintaining:

- a rolling power trace [(t, peak_db, floor_db, snr_db)] — feeds the walk test
- a peak-hold spectrum — bursts are kept, not missed
- the best-SNR feature set (survey.identify.iq_features) for signature ID
- an optional frequency SCAN over the catalog guesses until energy locks
- a walk-test window scored against the commanded move-in/out cadence

One session at a time per app. harvest() snapshots the accumulators into a
capture record and RESETS them — the loop keeps running so the tech walks the
whole range ladder without touching the laptop.
"""

from __future__ import annotations

import threading
import time
from collections import deque

from .device_lab import walk_score

SNR_FOUND_DB = 8.0      # scan locks when a grab clears this
SNR_STABLE_DB = 6.0     # stability window needs at least this
STABLE_TOL_DB = 3.0     # ...and peak within +/- this of the tail median
SCAN_DWELL_S = 1.4      # per guess frequency before hopping
MAX_SESSION_S = 600.0   # hard stop — never hold the radio forever
TRACE_MAXLEN = 2400     # ~5 min at 8 Hz

# Overload protection. All IQ sources normalize capture_iq to full scale ±1
# (Pluto /2048, UHD fc32, simulator clips at 1.0). A compressed front end
# fabricates harmonics/IMD — the scan would lock false peaks and a clipped
# power reading saturates, killing the walk test's motion correlation.
CLIP_LEVEL = 0.90       # |I| or |Q| above this fraction of full scale
CLIP_FRAC_TRIP = 0.002  # >0.2 % of samples clipped in a grab = overload
GAIN_STEP_DB = 8.0      # back off this much per overloaded grab
GAIN_MIN_DB = 0.0       # AD936x manual floor; still hot here → needs a pad

FEATS_MIN_INTERVAL_S = 2.0  # iq_features costs ~1 s — never run it per-grab


class LabLiveError(RuntimeError):
    pass


class LiveSession(threading.Thread):
    """Owns the monitor radio until stop() — start via start(), poll snapshot()."""

    def __init__(self, controller, pkg_id: str, center_mhz: float | None,
                 scan_mhz: list[float] | None, side: str,
                 sample_rate: int, n_samples: int = 65536,
                 initial_gain_db: float = 64.0):
        super().__init__(daemon=True, name="lab-live")
        self.controller = controller
        self.pkg_id = pkg_id
        self.center_mhz = float(center_mhz) if center_mhz else None
        self.scan_mhz = [float(f) for f in (scan_mhz or []) if f]
        if not self.center_mhz and not self.scan_mhz:
            raise LabLiveError("need center_mhz or a scan list")
        self.side = side
        self.sample_rate = int(sample_rate)
        self.n_samples = int(n_samples)
        # overload protection state
        self.gain_db: float = float(initial_gain_db)
        self._gain_restore: float = float(initial_gain_db)
        self.clipping = False
        self.needs_pad = False
        self.walk_clipped = False
        self.clip_frac = 0.0
        self.gain_events: list = []
        self._last_feats_t = 0.0
        self._halt = threading.Event()
        self._lk = threading.Lock()
        self._t0 = time.time()
        self.trace: deque = deque(maxlen=TRACE_MAXLEN)
        self._ph_freqs = None      # baseband Hz grid
        self._ph_psd = None        # peak-hold dB
        self.best_feats: dict | None = None
        self.best_snr: float | None = None
        self.grabs = 0
        self.error: str | None = None
        self.scanning = bool(self.scan_mhz) and not self.center_mhz
        self.locked_mhz: float | None = self.center_mhz
        self.walk_t0: float | None = None
        self.walk_period_s: float = 8.0
        self.started_utc = self._t0
        self.ended = False

    # ---- control -----------------------------------------------------------

    def stop(self, join_s: float = 4.0):
        self._halt.set()
        if self.is_alive():
            self.join(timeout=join_s)

    def start_walk(self, period_s: float = 8.0):
        with self._lk:
            if self.scanning:
                raise LabLiveError("frequency not locked yet — scan still running")
            if self.needs_pad or self.clipping:
                raise LabLiveError(
                    "front end is overloaded — fit the attenuator pad (or let "
                    "the gain settle) before running the walk test")
            self.walk_clipped = False
            self.walk_t0 = time.time()
            self.walk_period_s = max(4.0, min(20.0, float(period_s or 8.0)))

    def end_walk(self) -> dict:
        with self._lk:
            if self.walk_t0 is None:
                raise LabLiveError("walk test was not started")
            res = walk_score([(t, p) for (t, p, _f, _s) in self.trace],
                             self.walk_t0, self.walk_period_s)
            res["period_s"] = self.walk_period_s
            res["started_utc"] = self.walk_t0
            res["ended_utc"] = time.time()
            res["gain_db"] = self.gain_db
            if self.walk_clipped:
                res["clipped"] = True
                res["verdict"] = "clipped"
            self.walk_t0 = None
            return res

    def retune(self, center_mhz: float):
        with self._lk:
            self.locked_mhz = float(center_mhz)
            self.scanning = False
            self._reset_accum_locked()

    # ---- read side ---------------------------------------------------------

    def _reset_accum_locked(self):
        self._ph_freqs = None
        self._ph_psd = None
        self.best_feats = None
        self.best_snr = None

    def _stable_s_locked(self) -> float:
        """Seconds the tail of the trace has held SNR >= threshold with the
        peak inside +/- STABLE_TOL_DB of the tail median."""
        if len(self.trace) < 4:
            return 0.0
        tail = list(self.trace)[-80:]
        peaks = sorted(p for (_t, p, _f, s) in tail if s is not None)
        if not peaks:
            return 0.0
        med = peaks[len(peaks) // 2]
        t_stable = None
        for (t, p, _f, s) in tail:
            ok = (s is not None and s >= SNR_STABLE_DB
                  and abs(p - med) <= STABLE_TOL_DB)
            if ok:
                if t_stable is None:
                    t_stable = t
            else:
                t_stable = None
        return round(time.time() - t_stable, 1) if t_stable else 0.0

    def snapshot(self, trace_tail: int = 240, max_bins: int = 256) -> dict:
        import numpy as np
        with self._lk:
            tail = list(self.trace)[-trace_tail:]
            snr_now = tail[-1][3] if tail else None
            peak_now = tail[-1][1] if tail else None
            out = {
                "running": self.is_alive() and not self._halt.is_set(),
                "error": self.error,
                "pkg_id": self.pkg_id,
                "t_now": time.time(),
                "started_utc": self.started_utc,
                "grabs": self.grabs,
                "scanning": self.scanning,
                "center_mhz": self.locked_mhz,
                "scan_mhz": self.scan_mhz,
                "snr_now": snr_now,
                "peak_now": peak_now,
                "stable_s": self._stable_s_locked(),
                "gain_db": self.gain_db,
                "clipping": self.clipping,
                "needs_pad": self.needs_pad,
                "walk_clipped": self.walk_clipped,
                "clip_frac": self.clip_frac,
                "trace": [[round(t, 2), round(p, 1),
                           None if s is None else round(s, 1)]
                          for (t, p, _f, s) in tail],
                "walk": None,
            }
            if self.walk_t0 is not None:
                elapsed = time.time() - self.walk_t0
                phase = "in" if (elapsed % self.walk_period_s) < \
                    self.walk_period_s / 2 else "out"
                w = {"active": True, "t0": self.walk_t0,
                     "period_s": self.walk_period_s,
                     "elapsed_s": round(elapsed, 1), "phase": phase}
                if elapsed >= self.walk_period_s * 1.5:
                    w["score"] = walk_score(
                        [(t, p) for (t, p, _f, _s) in self.trace],
                        self.walk_t0, self.walk_period_s)
                out["walk"] = w
            if self._ph_psd is not None and self._ph_freqs is not None:
                step = max(1, len(self._ph_psd) // max_bins)
                f = self._ph_freqs[::step]
                p = self._ph_psd[::step]
                out["peakhold"] = {
                    "freqs_mhz": [round(float(x) / 1e6, 4) for x in f],
                    "power_db": [round(float(x), 1) for x in p],
                    "peak_db": round(float(np.max(self._ph_psd)), 1),
                    "floor_db": round(float(np.median(self._ph_psd)), 1),
                }
            return out

    def harvest(self) -> dict:
        """Capture record from the accumulators, then reset them (loop keeps
        running for the next stop/state). Peak-hold based — bursts included."""
        import numpy as np
        with self._lk:
            if self._ph_psd is None or not self.trace:
                raise LabLiveError("nothing accumulated yet — let it run a moment")
            ph_peak = float(np.max(self._ph_psd))
            ph_floor = float(np.median(self._ph_psd))
            step = max(1, len(self._ph_psd) // 256)
            spec = {
                "freqs_mhz": [round(float(x) / 1e6, 4)
                              for x in self._ph_freqs[::step]],
                "power_db": [round(float(x), 1) for x in self._ph_psd[::step]],
                "peak_db": round(ph_peak, 1),
                "floor_db": round(ph_floor, 1),
                "snr_db": round(ph_peak - ph_floor, 1),
                "peak_hold": True,
            }
            snrs = [s for (_t, _p, _f, s) in self.trace if s is not None]
            rec = {
                "utc": time.time(),
                "center_mhz": self.locked_mhz,
                "sample_rate": self.sample_rate,
                "n_samples": self.n_samples,
                "grabs": self.grabs,
                "live_loop": True,
                "gain_db": self.gain_db,
                "gain_stepped": bool(self.gain_events),
                "duration_s": round(time.time() - self.started_utc, 1),
                "features": dict(self.best_feats or {}),
                "spectrum": spec,
                "snr_stats": {
                    "max": round(max(snrs), 1) if snrs else None,
                    "median": round(sorted(snrs)[len(snrs) // 2], 1)
                    if snrs else None,
                    "n": len(snrs),
                },
            }
            self._reset_accum_locked()
            self.grabs = 0
            self.started_utc = time.time()
            return rec

    # ---- the loop ----------------------------------------------------------

    def run(self):
        import numpy as np
        from .identify import iq_features
        from .sweep.sdr_source import welch_psd
        try:
            dev, mlock, mstop, _msrc = self.controller.monitor_target()
            if dev is None or not callable(getattr(dev, "capture_iq", None)):
                raise LabLiveError("no IQ-capable radio connected")
            mstop.set()
            scan_i = 0
            scan_t = 0.0
            errors = 0
            with mlock:
                if hasattr(dev, "select_module"):
                    try:
                        dev.select_module(self.side)
                    except Exception:  # noqa: BLE001
                        pass
                try:
                    while not self._halt.is_set() and \
                            time.time() - self._t0 < MAX_SESSION_S:
                        with self._lk:
                            if self.scanning:
                                now = time.time()
                                if now - scan_t >= SCAN_DWELL_S:
                                    scan_t = now
                                    self.locked_mhz = self.scan_mhz[
                                        scan_i % len(self.scan_mhz)]
                                    scan_i += 1
                                    self._reset_accum_locked()
                            center = self.locked_mhz
                        try:
                            iq = dev.capture_iq(center * 1e6,
                                                self.sample_rate,
                                                self.n_samples)
                            errors = 0
                        except NotImplementedError:
                            raise LabLiveError(
                                "connected radio is amplitude-only — "
                                "connect the B220 or Pluto")
                        except Exception as e:  # noqa: BLE001
                            errors += 1
                            if errors >= 5:
                                raise LabLiveError(f"radio failing: {e}")
                            time.sleep(0.4)
                            continue
                        iq = np.asarray(iq, dtype=np.complex64)
                        if iq.size < 256:
                            time.sleep(0.1)
                            continue
                        # ---- overload check BEFORE trusting the spectrum ----
                        clip_frac = float(np.mean(
                            (np.abs(iq.real) >= CLIP_LEVEL) |
                            (np.abs(iq.imag) >= CLIP_LEVEL)))
                        with self._lk:
                            self.clip_frac = round(clip_frac, 4)
                            walk_running = self.walk_t0 is not None
                        if clip_frac >= CLIP_FRAC_TRIP:
                            self.clipping = True
                            if walk_running:
                                # NEVER step gain mid-walk — a gain change
                                # breaks power-vs-motion continuity. Flag it;
                                # the walk must be rerun after levels settle.
                                self.walk_clipped = True
                                continue
                            stepped = False
                            if self.gain_db > GAIN_MIN_DB and \
                                    callable(getattr(dev, "set_gain", None)):
                                new_g = max(GAIN_MIN_DB,
                                            self.gain_db - GAIN_STEP_DB)
                                try:
                                    dev.set_gain("manual", new_g)
                                    self.gain_db = new_g
                                    stepped = True
                                    with self._lk:
                                        self.gain_events.append(
                                            {"utc": time.time(),
                                             "gain_db": new_g,
                                             "clip_frac": clip_frac})
                                        # levels shifted — old peak-hold lies
                                        self._reset_accum_locked()
                                except Exception:  # noqa: BLE001
                                    pass
                            if not stepped:
                                self.needs_pad = True
                            continue  # clipped data is garbage — don't record
                        else:
                            self.clipping = False
                            self.needs_pad = False
                        nper = min(1024, max(64, iq.size // 4))
                        f_bb, psd = welch_psd(iq, self.sample_rate,
                                              nperseg=nper)
                        peak = float(np.max(psd))
                        floor = float(np.median(psd))
                        snr = peak - floor
                        # iq_features is EXPENSIVE (~1 s at 16k samples): the
                        # dense power trace must not stall on it, so compute
                        # only when this grab would become the new best —
                        # immediately if we have none, else throttled.
                        feats = None
                        beats = (self.best_snr is None or snr > self.best_snr)
                        if beats and (
                                self.best_feats is None or
                                time.time() - self._last_feats_t
                                >= FEATS_MIN_INTERVAL_S):
                            try:
                                feats = iq_features(iq, self.sample_rate)
                            except Exception:  # noqa: BLE001
                                feats = None
                            self._last_feats_t = time.time()
                        with self._lk:
                            self.grabs += 1
                            self.trace.append(
                                (time.time(), peak, floor, snr))
                            if self._ph_psd is None or \
                                    len(psd) != len(self._ph_psd):
                                self._ph_freqs = f_bb
                                self._ph_psd = psd.astype(float)
                            else:
                                self._ph_psd = np.maximum(self._ph_psd, psd)
                            if feats is not None and \
                                    (self.best_snr is None or
                                     snr > self.best_snr):
                                self.best_snr = snr
                                self.best_feats = feats
                            if self.scanning and snr >= SNR_FOUND_DB:
                                self.scanning = False
                        time.sleep(0.06)
                finally:
                    # hand the radio back the way we found it — the lab's
                    # auto-gain must never leak into sweep/monitor baselines
                    if self.gain_db != self._gain_restore and \
                            callable(getattr(dev, "set_gain", None)):
                        try:
                            dev.set_gain("manual", self._gain_restore)
                        except Exception:  # noqa: BLE001
                            pass
                    if hasattr(dev, "select_module"):
                        try:
                            dev.select_module("right")
                        except Exception:  # noqa: BLE001
                            pass
        except LabLiveError as e:
            self.error = str(e)
        except Exception as e:  # noqa: BLE001
            self.error = f"live loop died: {e}"
        finally:
            self.ended = True


# ---------------------------------------------------------------------------
# WIDE FIND — two-pass stepped max-hold sweep: DUT OFF baseline, DUT ON, diff.
# Finds the device's ACTUAL frequencies with no guess list: new energy in the
# ON pass is the DUT, wherever it lives. Peak-hold across K grabs per step
# keeps bursts and accumulates hop sets.
# ---------------------------------------------------------------------------

WF_SPANS = {
    "subghz": (70.0, 1000.0),
    "ism24": (2300.0, 2500.0),
    "wifi5": (5150.0, 5900.0),
    "full": (70.0, 6000.0),
}
WF_DIFF_DB = 8.0        # ON must beat OFF by this to count
# Hard stop for the RADIO time only. Operator gates and pauses are subtracted,
# so a tech taking four minutes to power an iPhone down can never burn the
# session's budget (see WideFindSession._elapsed).
WF_MAX_S = 900.0

# The operator decides when each pass starts — there is NO timeout on a gate.
# This used to be a 300 s countdown on the power-ON gate only, and there was no
# gate at all before the baseline pass: pressing START began measuring
# immediately, which assumed the DUT was already off. An iPhone takes ~30 s to
# power down and far longer to boot, so the baseline was recorded WHILE the
# phone was still transmitting — poisoning the very reference the ON/OFF diff
# depends on, and reading as "it gave me two seconds then said baseline
# established".
WF_PHASE_TEXT = {
    "await_off": {
        "title": "STEP 1 of 4 — POWER THE DEVICE OFF",
        "body": "Fully power the device OFF now (not sleep / not standby — a "
                "phone must be shut down, and airplane mode is NOT off). Wait "
                "until the screen is completely dark. Take as long as you need: "
                "nothing is being measured yet. Press I'VE POWERED IT OFF when "
                "it is fully down.",
        "action": "I'VE POWERED IT OFF — START BASELINE",
    },
    "pass_off": {
        "title": "STEP 2 of 4 — RECORDING BASELINE (device OFF)",
        "body": "Measuring the room with the device off. LEAVE THE DEVICE OFF "
                "and keep still. This is the reference every later comparison "
                "is made against.",
        "action": "",
    },
    "await_on": {
        "title": "STEP 3 of 4 — POWER THE DEVICE ON",
        "body": "Baseline recorded. Now power the device ON and let it finish "
                "starting up — a phone needs to boot AND reconnect to Wi-Fi and "
                "cellular, which can take a couple of minutes. Nothing is being "
                "measured while you wait. Press I'VE POWERED IT ON only once it "
                "is fully up and connected.",
        "action": "I'VE POWERED IT ON — START COMPARISON",
    },
    "pass_on": {
        "title": "STEP 4 of 4 — COMPARING (device ON)",
        "body": "Measuring again with the device on. Keep the device powered up "
                "and in place. Any frequency that is louder now than it was in "
                "the baseline is a candidate.",
        "action": "",
    },
    "paused": {
        "title": "PAUSED",
        "body": "The sweep is paused and the radio is idle. Nothing is lost — "
                "press RESUME to continue from where you stopped.",
        "action": "RESUME",
    },
    "done": {
        "title": "DONE — candidates below",
        "body": "Frequencies louder with the device ON than OFF are listed "
                "strongest first. Near-field coupling makes harmonics loud, so "
                "for a tagged family the FUNDAMENTAL is the lowest member.",
        "action": "",
    },
}


def group_harmonics(peaks: list[dict], tol: float = 0.004) -> list[dict]:
    """Tag integer-related peaks. Near-field couples harmonics almost as hard
    as the fundamental, so the loudest diff peak is NOT automatically the
    transmit frequency — the lowest member of an integer family is."""
    out = sorted((dict(p) for p in peaks), key=lambda p: p["freq_mhz"])
    for i, p in enumerate(out):
        p.setdefault("harmonic_of_mhz", None)
        p.setdefault("n", 1)
        for q in out[:i]:
            if q.get("harmonic_of_mhz"):
                continue
            ratio = p["freq_mhz"] / q["freq_mhz"]
            n = round(ratio)
            if n >= 2 and abs(ratio - n) <= tol * n:
                p["harmonic_of_mhz"] = q["freq_mhz"]
                p["n"] = n
                break
    fundamentals = {p["freq_mhz"] for p in out if not p["harmonic_of_mhz"]}
    for p in out:
        p["fundamental"] = p["freq_mhz"] in fundamentals
    return sorted(out, key=lambda p: -p["delta_db"])


class WideFindSession(threading.Thread):
    """Owns the monitor radio for both passes. Operator flow:
    pass_off (DUT powered off) → await_on (operator powers DUT, confirms) →
    pass_on → done (candidates ready)."""

    def __init__(self, controller, pkg_id: str, span: tuple[float, float],
                 side: str, sample_rate: int, n_samples: int = 32768,
                 k_grabs: int = 2, initial_gain_db: float = 64.0):
        super().__init__(daemon=True, name="lab-widefind")
        self.controller = controller
        self.pkg_id = pkg_id
        self.start_mhz, self.stop_mhz = float(span[0]), float(span[1])
        if self.stop_mhz <= self.start_mhz:
            raise LabLiveError("bad span")
        self.side = side
        self.sample_rate = int(sample_rate)
        self.n_samples = int(n_samples)
        self.k_grabs = max(1, int(k_grabs))
        self.gain_db = float(initial_gain_db)
        step = self.sample_rate * 0.8 / 1e6
        self.centers = []
        f = self.start_mhz + step / 2
        while f < self.stop_mhz + step / 2:
            self.centers.append(round(f, 3))
            f += step
        # START now parks on the power-OFF gate instead of measuring immediately
        self.phase = "await_off"
        self.progress = 0.0
        self.current_mhz: float | None = None
        self.clip_steps = 0
        self.candidates: list[dict] | None = None
        self.error: str | None = None
        self.ended = False
        self._halt = threading.Event()
        self._go_off = threading.Event()   # operator: "it is powered OFF"
        self._go_on = threading.Event()    # operator: "it is powered ON"
        self._resume = threading.Event()   # cleared = paused
        self._resume.set()
        self._paused = False
        self._wait_s = 0.0                 # operator gate + pause time, not radio time
        self._lk = threading.Lock()
        self._passes: dict[str, dict] = {"off": {}, "on": {}}

    def stop(self, join_s: float = 5.0):
        self._halt.set()
        self._go_off.set()
        self._go_on.set()
        self._resume.set()                 # a paused thread must be able to exit
        if self.is_alive():
            self.join(timeout=join_s)

    def pause(self):
        """Hold the sweep without losing it. STOP used to be the only control,
        so a tech who needed a moment had to destroy the run and start over."""
        with self._lk:
            self._paused = True
        self._resume.clear()

    def resume(self):
        with self._lk:
            self._paused = False
        self._resume.set()

    def confirm_off(self):
        if self.phase != "await_off":
            raise LabLiveError("not waiting for power-off")
        self._go_off.set()

    def confirm_on(self):
        if self.phase != "await_on":
            raise LabLiveError("not waiting — pass one still running")
        self._go_on.set()

    def _elapsed(self) -> float:
        """Radio time only — operator gates and pauses do not count against the
        session budget, so taking your time can never time the sweep out."""
        return time.time() - self._t0 - self._wait_s

    def snapshot(self) -> dict:
        with self._lk:
            phase = "paused" if self._paused and self.phase in ("pass_off", "pass_on") \
                else self.phase
            text = WF_PHASE_TEXT.get(phase, {})
            return {
                "running": self.is_alive() and not self.ended,
                "phase": self.phase,
                "paused": self._paused,
                # written, on-screen instructions: what to do, right now
                "title": text.get("title", ""),
                "instruction": text.get("body", ""),
                "action_label": text.get("action", ""),
                "awaiting_operator": self.phase in ("await_off", "await_on"),
                "progress": round(self.progress, 3),
                "current_mhz": self.current_mhz,
                "span": [self.start_mhz, self.stop_mhz],
                "steps": len(self.centers),
                "clip_steps": self.clip_steps,
                "candidates": self.candidates,
                "error": self.error,
                "pkg_id": self.pkg_id,
            }

    def _one_pass(self, dev, key: str, t0: float):
        import numpy as np
        from .sweep.sdr_source import welch_psd
        store = self._passes[key]
        n_c = len(self.centers)
        for i, c in enumerate(self.centers):
            self._pause_gate()          # PAUSE holds here without losing the run
            if self._halt.is_set():
                raise LabLiveError("wide find stopped")
            if self._elapsed() > WF_MAX_S:
                raise LabLiveError("wide find timed out")
            with self._lk:
                self.current_mhz = c
                self.progress = (i / n_c) if key == "off" else 0.5 + i / n_c / 2
            ph = None
            f_bb = None
            for _k in range(self.k_grabs):
                try:
                    iq = np.asarray(
                        dev.capture_iq(c * 1e6, self.sample_rate,
                                       self.n_samples), dtype=np.complex64)
                except NotImplementedError:
                    raise LabLiveError("connected radio is amplitude-only")
                except Exception:  # noqa: BLE001
                    continue
                if iq.size < 256:
                    continue
                clip = float(np.mean((np.abs(iq.real) >= CLIP_LEVEL) |
                                     (np.abs(iq.imag) >= CLIP_LEVEL)))
                if clip >= CLIP_FRAC_TRIP:
                    with self._lk:
                        self.clip_steps += 1
                nper = min(1024, max(64, iq.size // 4))
                fb, psd = welch_psd(iq, self.sample_rate, nperseg=nper)
                ph = psd if ph is None else np.maximum(ph, psd)
                f_bb = fb
            if ph is not None:
                store[c] = (f_bb, ph)

    def _diff_candidates(self) -> list[dict]:
        import numpy as np
        peaks = []
        for c, (f_bb, on) in self._passes["on"].items():
            off_ent = self._passes["off"].get(c)
            if off_ent is None or len(off_ent[1]) != len(on):
                continue
            off = off_ent[1]
            d = np.asarray(on) - np.asarray(off)
            floor = float(np.median(on))
            mask = (d >= WF_DIFF_DB) & (np.asarray(on) >= floor + 6.0)
            if not mask.any():
                continue
            idx = np.where(mask)[0]
            runs = np.split(idx, np.where(np.diff(idx) > 2)[0] + 1)
            for run in runs:
                if run.size == 0:
                    continue
                j = run[int(np.argmax(np.asarray(d)[run]))]
                fr = c + float(f_bb[j]) / 1e6
                bw_khz = run.size * abs(float(f_bb[1] - f_bb[0])) / 1e3 \
                    if len(f_bb) > 1 else 0.0
                peaks.append({"freq_mhz": round(fr, 4),
                              "delta_db": round(float(d[j]), 1),
                              "on_db": round(float(np.asarray(on)[j]), 1),
                              "bw_khz": round(bw_khz, 1)})
        # dedupe peaks that landed in overlapping windows
        peaks.sort(key=lambda p: p["freq_mhz"])
        merged: list[dict] = []
        for p in peaks:
            if merged and abs(p["freq_mhz"] - merged[-1]["freq_mhz"]) < 0.2:
                if p["delta_db"] > merged[-1]["delta_db"]:
                    merged[-1] = p
            else:
                merged.append(p)
        merged.sort(key=lambda p: -p["delta_db"])
        return group_harmonics(merged[:12])

    def _await_operator(self, ev):
        """Block until the operator confirms this step. NO TIMEOUT — the tech
        decides when the run advances, because only they can see whether the
        device is actually off, or actually booted and reconnected. Time spent
        here is excluded from the session budget."""
        start = time.time()
        try:
            while not ev.wait(0.25):
                if self._halt.is_set():
                    raise LabLiveError("stopped")
        finally:
            self._wait_s += time.time() - start
        if self._halt.is_set():
            raise LabLiveError("stopped")

    def _pause_gate(self):
        """Honour PAUSE between steps; paused time is not radio time."""
        if self._resume.is_set():
            return
        start = time.time()
        try:
            while not self._resume.wait(0.25):
                if self._halt.is_set():
                    raise LabLiveError("stopped")
        finally:
            self._wait_s += time.time() - start

    def run(self):
        t0 = self._t0 = time.time()
        try:
            dev, mlock, mstop, _msrc = self.controller.monitor_target()
            if dev is None or not callable(getattr(dev, "capture_iq", None)):
                raise LabLiveError("no IQ-capable radio connected")
            mstop.set()
            with mlock:
                if hasattr(dev, "select_module"):
                    try:
                        dev.select_module(self.side)
                    except Exception:  # noqa: BLE001
                        pass
                try:
                    # GATE 1 — the operator powers the DUT down. Nothing is
                    # measured until they say so; the baseline is worthless if
                    # the device was still transmitting while it was recorded.
                    self._await_operator(self._go_off)
                    with self._lk:
                        self.phase = "pass_off"
                    self._one_pass(dev, "off", t0)
                    # GATE 2 — the operator powers it back up and lets it settle
                    with self._lk:
                        self.phase = "await_on"
                        self.progress = 0.5
                    self._await_operator(self._go_on)
                    with self._lk:
                        self.phase = "pass_on"
                    self._one_pass(dev, "on", t0)
                    cands = self._diff_candidates()
                    with self._lk:
                        self.candidates = cands
                        self.phase = "done"
                        self.progress = 1.0
                finally:
                    if hasattr(dev, "select_module"):
                        try:
                            dev.select_module("right")
                        except Exception:  # noqa: BLE001
                            pass
        except LabLiveError as e:
            self.error = str(e)
        except Exception as e:  # noqa: BLE001
            self.error = f"wide find died: {e}"
        finally:
            self.ended = True
