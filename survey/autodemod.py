"""AUTO-DEMOD — a background worker that walks every detection in the session,
demodulates it, and keeps doing it.

The operator problem this solves: a sweep leaves a few hundred hits in the
ledger, and answering "what IS that" has been a per-signal manual act — click,
listen, judge, move on. Nobody does that three hundred times. So most hits are
never actually listened to, and the ones that are, are the ones that already
looked interesting — which is exactly backwards for finding something hidden.

This worker inverts it. It cycles the monitor radio through EVERY signal
continuously, so by the time the operator looks at the ledger, every row already
carries a measured answer: what modulation, how strong, what it most resembles,
whether it decoded, and — where lawful — audio to play. Signals that were never
worth a manual click are the ones this catches.

HOW IT SHARES THE RADIO. It is a guest, never an owner:
  * It uses the MONITOR radio (normally the Pluto), never the sweeper, so a sweep
    in flight is untouched. With only one radio connected it runs solely when
    nothing else is, and drops the moment a sweep starts.
  * It registers itself as the monitor worker, so controller.busy(src) reports it
    honestly and other borrowers can see it — a worker that hides from busy()
    gets its radio yanked mid-capture by the next mstop.set().
  * The operator always wins. Manual LISTEN, ANALYZE, DF or recording take the
    radio back instantly; this worker notices and stands down without a fight.

WHAT IT DOES TO EVERY SIGNAL (content-free — always, for all signals):
  occupied bandwidth, duty cycle, symbol rate, SNR, modulation class, the ranked
  signature-ID candidates, and the one-line interpret() verdict. This is RF shape
  measurement — the same content-free characterization the post-sweep pass
  already performs — and it is what fills the ledger in.

WHAT IT DOES ONLY WHEN CONTENT IS PERMITTED:
  demodulating audio you can play, and decoding protocol payloads (pager text and
  the like). That is interception of communications CONTENT, and ECPA
  (18 U.S.C. 2511) draws the line at the premises owner's consent — which in this
  app is `attribution_status == 'client_space'`, the predicate
  nextstep.blinded_cue_for() already computes for every row.

  A signal not attributed to the client is characterized but NOT demodulated, and
  the row says why. The operator can lift that for the session with an explicit
  acknowledgment + reason, which is written to the immutable `decode_overrides`
  audit log — the same gate and the same table the manual force-decode path
  already uses. One acknowledgment, logged, covers the run; there is no silent
  mode where this quietly records everyone's conversations because it was left
  switched on.

WHAT IT IS NOT. It is not a scanner that "hears everything at once" — there is
one monitor radio and it can be on one frequency at a time. It round-robins, so
a 300-signal ledger at ~2 s per signal comes round about every ten minutes. Each
row therefore carries the age of its own result, and a stale row says so instead
of implying it is live.
"""

import base64
import logging
import threading
import time

import numpy as np

from . import demod as demod_mod
from . import identify as identify_mod
from .decode_router import DATA_MODES, interpret, route_decode

log = logging.getLogger("survey.autodemod")

# capture shape per signal. Deliberately modest: this runs hundreds of times.
DEFAULT_SECONDS = 0.35
# iq_features is ~1 s at 16k samples and superlinear (O(n^2) autocorrelation in
# _symbol_rate_hz) — never hand it the whole buffer. 8192 is the same slice the
# live listen path classifies on.
FEATURE_SAMPLES = 8192
# keep playable audio for this many most-recent signals (~70 kB each)
AUDIO_KEEP = 40
AUDIO_SECONDS = 1.5
# a signal this far down is noise; measuring it wastes a radio slot
MIN_SNR_DB = 4.0


def _b64_wav(pcm16, rate):
    return ("data:audio/wav;base64,"
            + base64.b64encode(demod_mod.wav_bytes(pcm16, rate)).decode("ascii"))


class AutoDemod:
    """Owns the background cycle. One instance per app, created by the server."""

    def __init__(self, controller, db, cfg, demo=False):
        self.controller = controller
        self.db = db
        self.cfg = cfg
        self.demo = bool(demo)
        self._thread = None
        self._stop = threading.Event()      # NOT named _stop_flag/Thread._stop —
        self._lock = threading.Lock()       # see the Thread._stop naming trap
        self.session_id = None
        self.results = {}                   # signal_id -> result dict
        self.order = []                     # signal ids, most-recently-done last
        self.status = {
            "running": False, "paused_reason": "", "session_id": None,
            "cycle": 0, "done": 0, "queued": 0, "current": None,
            "consent": False, "consent_reason": "", "consent_utc": 0.0,
            "started_utc": 0.0, "last_utc": 0.0, "error": "",
            "radio": "", "message": "idle",
        }

    # ---- lifecycle ----

    def start(self, session_id, consent=False, consent_reason="", operator=""):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return dict(self.status)
            self.session_id = int(session_id)
            self._stop.clear()
            self.results = {}
            self.order = []
            self.status.update(running=True, session_id=self.session_id, cycle=0,
                               done=0, queued=0, current=None, error="",
                               started_utc=time.time(), message="starting",
                               paused_reason="")
            if consent:
                self._record_consent(consent_reason, operator)
            t = threading.Thread(target=self._run, name="autodemod", daemon=True)
            self._thread = t
            t.start()
            return dict(self.status)

    def stop(self):
        self._stop.set()
        with self._lock:
            self.status.update(running=False, current=None, message="stopped")
        return dict(self.status)

    def _record_consent(self, reason, operator=""):
        """Lift the content gate for this session — logged, never implicit."""
        reason = (reason or "").strip()
        if not reason:
            raise ValueError("a written reason is required to decode content")
        try:
            self.db.add_decode_override(self.session_id, None, None,
                                        "autodemod", operator or "", reason)
        except Exception:  # noqa: BLE001
            log.exception("could not write the ECPA acknowledgment — refusing "
                          "to enable content decode without an audit row")
            raise
        self.status.update(consent=True, consent_reason=reason,
                           consent_utc=time.time())
        log.warning("AUTO-DEMOD content decode enabled for session %s by %r: %s",
                    self.session_id, operator or "operator", reason)

    def set_consent(self, on, reason="", operator=""):
        if on:
            self._record_consent(reason, operator)
        else:
            self.status.update(consent=False, consent_reason="", consent_utc=0.0)
        return dict(self.status)

    # ---- state for the UI ----

    def state(self, limit=400):
        with self._lock:
            st = dict(self.status)
        rows = [self.results[i] for i in self.order if i in self.results]
        rows.sort(key=lambda r: (-(r.get("interest") or 0.0),
                                 r.get("center_mhz") or 0.0))
        st["rows"] = rows[:limit]
        st["total"] = len(rows)
        alive = bool(self._thread and self._thread.is_alive())
        if st["running"] and not alive:
            # the worker died; never keep claiming it is running
            st["running"] = False
            st["message"] = st.get("error") or "auto-demod stopped unexpectedly"
        return st

    # ---- the work queue ----

    def _queue(self):
        """Every detection in the session, never-done first then oldest-first, so
        coverage completes before anything is refreshed."""
        rows = self.db.query(
            "SELECT id, center_mhz, bw_khz, peak_dbm, floor_dbm, matched_label, "
            "display_label, classification, anomaly_score, attribution_status, "
            "caution FROM signals WHERE session_id=? ORDER BY center_mhz",
            (self.session_id,))
        def key(r):
            prev = self.results.get(r["id"])
            done_utc = prev.get("utc", 0.0) if prev else 0.0
            return (done_utc, -(r.get("anomaly_score") or 0.0))
        return sorted(rows, key=key)

    def _permitted(self, row):
        """ECPA: content only where the client owns the space. Everything else is
        characterized, not listened to."""
        if self.demo:
            return False, "demo mode never decodes content"
        if (row.get("attribution_status") or "") == "client_space":
            return True, ""
        if self.status.get("consent"):
            return True, "operator ECPA acknowledgment (logged)"
        return False, ("not attributed to the client's space — characterized "
                       "only; acknowledge to decode content")

    # ---- the loop ----

    def _pause_reason(self):
        c = self.controller
        if getattr(c, "listening", False):
            return "operator is listening — standing down"
        if c.state.get("running"):
            mon = c._current_monitor()
            if not mon or mon == c.active_source:
                return "sweep is using the only radio"
        return ""

    def _run(self):
        try:
            while not self._stop.is_set():
                reason = self._pause_reason()
                if reason:
                    with self._lock:
                        self.status.update(paused_reason=reason, current=None,
                                           message=reason)
                    self._stop.wait(2.0)
                    continue
                queue = self._queue()
                with self._lock:
                    self.status.update(queued=len(queue), paused_reason="",
                                       cycle=self.status["cycle"] + 1)
                if not queue:
                    with self._lock:
                        self.status["message"] = "no detections in this session yet"
                    self._stop.wait(3.0)
                    continue
                for row in queue:
                    if self._stop.is_set():
                        break
                    if self._pause_reason():
                        break
                    self._do_one(row)
                    self._stop.wait(0.05)
        except Exception as e:  # noqa: BLE001
            log.exception("auto-demod loop failed")
            with self._lock:
                self.status.update(error=str(e), message=f"auto-demod error: {e}")
        finally:
            with self._lock:
                self.status.update(running=False, current=None)
                if not self.status.get("message", "").startswith("auto-demod error"):
                    self.status["message"] = "stopped"

    def _borrow(self):
        """(dev, lock, stop, src) for a capture, or (None, reason)."""
        c = self.controller
        dev, mlock, mstop, msrc = c.ensure_monitor()
        if not msrc or dev is None:
            return None, "no monitor radio connected"
        if not (hasattr(dev, "capture_iq") and getattr(dev, "is_swept_sdr", False)):
            return None, "the monitor radio cannot capture IQ"
        if c.state.get("running") and msrc == c.active_source:
            return None, "the sweep owns the only radio"
        return (dev, mlock, mstop, msrc), ""

    def _in_range(self, dev, center_mhz):
        try:
            mods = dev.info.modules or []
        except Exception:  # noqa: BLE001
            return True
        if not mods:
            return True
        return any(m.min_mhz <= center_mhz <= m.max_mhz for m in mods)

    def _do_one(self, row):
        sid = row["id"]
        center_mhz = float(row["center_mhz"] or 0.0)
        borrowed, why = self._borrow()
        if not borrowed:
            with self._lock:
                self.status.update(paused_reason=why, message=why, current=None)
            self._stop.wait(2.0)
            return
        dev, mlock, mstop, msrc = borrowed
        with self._lock:
            self.status.update(radio=msrc, current={"id": sid, "mhz": center_mhz},
                               message=f"measuring {center_mhz:.4f} MHz")
        if not self._in_range(dev, center_mhz):
            self._store(row, {"state": "out_of_range",
                              "note": f"{msrc} cannot tune {center_mhz:.3f} MHz"})
            return

        fs = float(self.cfg.get("pluto", "listen_sample_rate", default=5_000_000))
        secs = float(self.cfg.get("autodemod", "seconds", default=DEFAULT_SECONDS))
        n = int(fs * max(0.1, min(1.0, secs)))
        try:
            mstop.set()                     # preempt any idle stream on this radio
            with mlock:
                mstop.clear()
                iq = dev.capture_iq(center_mhz * 1e6, fs, n)
        except Exception as e:  # noqa: BLE001
            log.warning("auto-demod capture failed at %.4f MHz: %s", center_mhz, e)
            self._store(row, {"state": "capture_failed", "note": str(e)[:160]})
            self._stop.wait(0.5)
            return

        res = self._analyze(row, iq, fs, center_mhz)
        self._store(row, res)

    def _analyze(self, row, iq, fs, center_mhz):
        """Content-free measurement for every signal; content only where allowed."""
        out = {"state": "measured"}
        head = np.asarray(iq)[:FEATURE_SAMPLES]
        try:
            feats = identify_mod.iq_features(head, fs)
        except Exception as e:  # noqa: BLE001
            return {"state": "analysis_failed", "note": str(e)[:160]}
        snr = feats.get("snr_db")
        out.update({
            "modulation": feats.get("modulation_class") or "unknown",
            "snr_db": round(snr, 1) if snr is not None else None,
            "occupied_bw_khz": feats.get("occupied_bw_khz"),
            "symbol_rate_kbd": feats.get("symbol_rate_kbd"),
            "duty_cycle": feats.get("duty_cycle"),
            "present": bool(feats.get("signal_present")),
        })
        if not feats.get("signal_present") or (snr is not None and snr < MIN_SNR_DB):
            out["state"] = "quiet"
            out["note"] = "nothing above the noise floor right now"
            return out

        # what does it most resemble? (content-free signature matching)
        try:
            cands = identify_mod.identify(center_mhz * 1e6, feats,
                                          identify_mod.load_reference(), top_n=3)
            out["candidates"] = [{"name": c.get("name"), "score": c.get("score"),
                                  "device_class": c.get("device_class")}
                                 for c in cands]
            if cands:
                out["best_guess"] = cands[0].get("name")
        except Exception:  # noqa: BLE001
            out["candidates"] = []

        permitted, gate_note = self._permitted(row)
        out["content_permitted"] = permitted
        out["gate_note"] = gate_note

        mode = demod_mod.pick_mode(center_mhz,
                                   row.get("display_label")
                                   or row.get("matched_label") or "")
        out["mode"] = mode

        if not permitted:
            # characterized, deliberately not decoded
            out["verdict"] = interpret(feats, None).get("summary", "")
            out["state"] = "characterized"
            self._persist(row, out.get("verdict", ""), "identify_only", "")
            return out

        decoded = None
        try:
            decoded = route_decode(np.asarray(iq), fs, feats)
            if decoded and decoded.get("messages"):
                out["protocol"] = decoded.get("protocol")
                out["messages"] = decoded["messages"][:8]
        except Exception:  # noqa: BLE001
            log.debug("route_decode failed at %.4f MHz", center_mhz, exc_info=True)

        interp = interpret(feats, (decoded or {}).get("protocol"),
                           bool((decoded or {}).get("encrypted")))
        out["verdict"] = interp.get("summary", "")
        out["readable"] = interp.get("readable", "")

        if mode not in DATA_MODES:
            try:
                keep = int(min(len(iq), fs * AUDIO_SECONDS))
                pcm, arate = demod_mod.demodulate(np.asarray(iq)[:keep], fs,
                                                  mode=mode)
                if pcm is not None and len(pcm):
                    out["audio"] = _b64_wav(pcm, arate)
                    out["audio_rate"] = arate
                    out["audio_rms"] = float(np.sqrt(np.mean(
                        (pcm.astype(np.float32) / 32768.0) ** 2)))
            except Exception:  # noqa: BLE001
                log.debug("demod failed at %.4f MHz", center_mhz, exc_info=True)
        out["state"] = "demodulated"
        self._persist(row, out.get("verdict", ""), out.get("readable", ""),
                      out.get("protocol", ""))
        return out

    def _persist(self, row, summary, readable, protocol):
        """Write the verdict onto the signal so the ledger and the report show it
        without re-running anything. Best-effort, exactly like /api/analyze."""
        if self.demo or not summary:
            return
        try:
            self.db.set_signal_analysis(row["id"], self.session_id, summary,
                                        readable, protocol or "")
        except Exception:  # noqa: BLE001
            log.debug("auto-demod persist failed for signal %s", row["id"],
                      exc_info=True)

    def _store(self, row, res):
        sid = row["id"]
        rec = {
            "id": sid,
            "center_mhz": row.get("center_mhz"),
            "bw_khz": row.get("bw_khz"),
            "peak_dbm": row.get("peak_dbm"),
            "label": (row.get("display_label") or row.get("matched_label") or ""),
            "classification": row.get("classification"),
            "attribution_status": row.get("attribution_status"),
            "anomaly_score": row.get("anomaly_score"),
            "utc": time.time(),
        }
        rec.update(res)
        # what should the operator look at first? measured energy + how odd it is
        interest = float(row.get("anomaly_score") or 0.0)
        if rec.get("state") == "demodulated" and (rec.get("audio_rms") or 0) > 0.02:
            interest += 1.0
        if rec.get("messages"):
            interest += 2.0
        if (rec.get("snr_db") or 0) > 20:
            interest += 0.5
        rec["interest"] = round(interest, 3)
        with self._lock:
            first = sid not in self.results
            self.results[sid] = rec
            if sid in self.order:
                self.order.remove(sid)
            self.order.append(sid)
            if first:
                self.status["done"] = self.status.get("done", 0) + 1
            self.status["last_utc"] = rec["utc"]
            # bound the audio held in memory — the measurements stay forever, the
            # clips age out oldest-first
            for old in self.order[:-AUDIO_KEEP]:
                stale = self.results.get(old)
                if stale is not None:
                    stale.pop("audio", None)
