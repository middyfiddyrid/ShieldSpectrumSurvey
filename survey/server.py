"""Flask server — serves the single-page ops console and the JSON API."""

import logging
import os
import threading
import time

from flask import Flask, jsonify, request, send_file, send_from_directory

from .baseline.base import DiskCache
from .baseline.manager import build_baseline
from .correlation import compute_room_localization
from .geocode import geocode
from .report import export_csv, generate_report
from .sweep.calibration import run_calibration
from .sweep.planner import plan_full_sweep
from .sweep.runner import SweepController

log = logging.getLogger("survey.server")
WEB_DIR = os.path.join(os.path.dirname(__file__), "web")


class AppState:
    def __init__(self, db, cfg):
        self.db = db
        self.cfg = cfg
        self.cache = DiskCache(cfg.abs_path(cfg.get("cache", "dir", default="cache")),
                               cfg.get("cache", "max_age_hours", default=720))
        self.controller = SweepController(db, cfg)
        self.session_id = None
        self.baseline_status = {"running": False, "result": None, "error": None}
        self.calibration_status = {"running": False, "result": None, "error": None}
        self.reports_dir = cfg.abs_path("reports")

    def session(self):
        if self.session_id is None:
            return None
        return self.db.query_one("SELECT * FROM sessions WHERE id=?", (self.session_id,))


def create_app(db, cfg) -> Flask:
    app = Flask(__name__, static_folder=None)
    st = AppState(db, cfg)
    app.config["APP_STATE"] = st

    def err(message, code=400):
        return jsonify({"error": str(message)}), code

    # ---------- static ----------

    @app.get("/")
    def index():
        return send_from_directory(WEB_DIR, "index.html")

    @app.get("/static/<path:name>")
    def static_files(name):
        return send_from_directory(WEB_DIR, name)

    # ---------- session ----------

    @app.post("/api/session")
    def create_session():
        body = request.get_json(force=True)
        address = (body.get("address") or "").strip()
        if not address:
            return err("address required")
        try:
            geo = geocode(address, st.cache)
        except RuntimeError as e:
            return err(e, 502)
        r_b = float(body.get("radius_broadcast")
                    or cfg.get("radius", "broadcast_miles", default=25))
        r_l = float(body.get("radius_lmr")
                    or cfg.get("radius", "land_mobile_miles", default=5))
        operator = body.get("operator") or cfg.get("branding", "operator", default="")
        st.session_id = db.create_session(address, geo["lat"], geo["lon"],
                                          geo["geocoder"], r_b, r_l, operator)
        st.baseline_status = {"running": False, "result": None, "error": None}
        return jsonify({"session_id": st.session_id, "geocode": geo})

    @app.get("/api/sessions")
    def list_sessions():
        return jsonify(db.query(
            "SELECT id, created_utc, address, operator, status FROM sessions "
            "ORDER BY created_utc DESC LIMIT 100"))

    @app.post("/api/session/open")
    def open_session():
        body = request.get_json(force=True)
        row = db.query_one("SELECT id FROM sessions WHERE id=?", (body.get("id"),))
        if not row:
            return err("no such session", 404)
        st.session_id = row["id"]
        return jsonify({"session_id": st.session_id})

    # ---------- overall state (polled by the UI) ----------

    @app.get("/api/state")
    def state():
        session = st.session()
        sweeps = []
        if session:
            sweeps = db.query("SELECT id,phase,module,room,profile,status,started_utc,"
                              "finished_utc,simulated FROM sweeps WHERE session_id=? "
                              "ORDER BY id", (session["id"],))
        return jsonify({
            "session": session,
            "device": st.controller.device_info(),
            "sweep": st.controller.state,
            "sweeps": sweeps,
            "baseline": st.baseline_status,
            "calibration": st.calibration_status,
            "busy": st.controller.busy(),
        })

    # ---------- baseline ----------

    @app.post("/api/baseline/build")
    def baseline_build():
        session = st.session()
        if not session:
            return err("create a session first")
        if st.baseline_status["running"]:
            return err("baseline build already running")
        st.baseline_status = {"running": True, "result": None, "error": None,
                              "progress": []}

        def work():
            try:
                result = build_baseline(
                    db, cfg, st.cache, session,
                    progress_cb=lambda name, status, count:
                        st.baseline_status["progress"].append(
                            {"source": name, "status": status, "count": count}))
                st.baseline_status.update(running=False, result=result)
            except Exception as e:  # noqa: BLE001
                log.exception("baseline build failed")
                st.baseline_status.update(running=False, error=str(e))

        threading.Thread(target=work, daemon=True).start()
        return jsonify({"started": True})

    @app.get("/api/baseline")
    def baseline_get():
        session = st.session()
        if not session:
            return err("no session")
        rows = db.query("SELECT * FROM baseline WHERE session_id=? "
                        "ORDER BY freq_low_mhz", (session["id"],))
        sources = db.query("SELECT * FROM source_status WHERE session_id=?",
                           (session["id"],))
        networked = [s for s in sources if s["source"] != "Static allocations"]
        degraded = bool(sources) and not any(
            s["status"] in ("ok", "cached", "stale") for s in networked)
        return jsonify({"rows": rows, "sources": sources, "degraded": degraded,
                        "count": len(rows)})

    # ---------- device ----------

    @app.post("/api/device/connect")
    def device_connect():
        try:
            info = st.controller.connect_device()
        except Exception as e:  # noqa: BLE001
            return err(e, 502)
        if st.session_id:
            db.set_device_info(st.session_id, info.to_dict())
        return jsonify(info.to_dict())

    # ---------- planning & sweeping ----------

    def _plans(profile, custom_minutes):
        dev = st.controller.device
        if dev is None:
            raise RuntimeError("connect the device first")
        modules = [m.to_dict() for m in dev.info.modules]
        return plan_full_sweep(modules, cfg, profile, custom_minutes)

    @app.get("/api/plan")
    def plan_preview():
        profile = request.args.get("profile", "standard")
        minutes = float(request.args.get("custom_minutes", 45))
        try:
            plans = _plans(profile, minutes)
        except RuntimeError as e:
            return err(e)
        return jsonify({side: p.to_dict() for side, p in plans.items()})

    @app.post("/api/sweep/start")
    def sweep_start():
        session = st.session()
        if not session:
            return err("create a session first")
        body = request.get_json(force=True)
        phase = body.get("phase", "interior")           # ambient | interior
        module = body.get("module", "right")            # left | right
        room = body.get("room", "") or ("exterior" if phase == "ambient" else "")
        profile = body.get("profile", "standard")
        minutes = float(body.get("custom_minutes") or 45)
        try:
            plans = _plans(profile, minutes)
            plan = plans.get(module)
            if plan is None or not plan.segments:
                return err(f"no plan for module {module}")
            sweep_id = st.controller.start_sweep(session["id"], phase, module, room, plan)
        except RuntimeError as e:
            return err(e, 409)
        if st.controller.device:
            db.set_device_info(session["id"], st.controller.device.info.to_dict())
        return jsonify({"sweep_id": sweep_id, "plan": plan.to_dict()})

    @app.post("/api/sweep/abort")
    def sweep_abort():
        st.controller.abort()
        return jsonify({"aborted": True})

    # ---------- results ----------

    @app.get("/api/signals")
    def signals():
        session = st.session()
        if not session:
            return err("no session")
        phase = request.args.get("phase")
        q = "SELECT * FROM signals WHERE session_id=?"
        params = [session["id"]]
        if phase:
            q += " AND phase=?"
            params.append(phase)
        q += " ORDER BY center_mhz"
        return jsonify(db.query(q, params))

    @app.get("/api/anomalies")
    def anomalies():
        session = st.session()
        if not session:
            return err("no session")
        rows = db.query(
            "SELECT * FROM signals WHERE session_id=? AND phase='interior' AND "
            "(classification='UNVERIFIED' OR caution=1) ORDER BY anomaly_score DESC",
            (session["id"],))
        # one row per physical signal: the same emitter detected in several room
        # sweeps keeps only its highest-scoring instance (room grid shows the rest)
        kept = []
        for r in rows:
            if not any(abs(r["center_mhz"] - k["center_mhz"]) <= 0.25 for k in kept):
                kept.append(r)
        return jsonify(kept)

    @app.get("/api/rooms")
    def rooms():
        session = st.session()
        if not session:
            return err("no session")
        sigs = db.query("SELECT * FROM signals WHERE session_id=? AND phase='interior'",
                        (session["id"],))
        by_room = {}
        for s in sigs:
            by_room.setdefault(s["room"] or "unspecified", []).append(s)
        groups = compute_room_localization(by_room)
        return jsonify({
            "rooms": sorted(by_room.keys()),
            "groups": [{"center_mhz": k, **v} for k, v in
                       sorted(groups.items(), key=lambda kv: -kv[1]["delta_db"])]})

    # ---------- directional ----------

    @app.post("/api/directional/start")
    def directional_start():
        session = st.session()
        if not session:
            return err("no session")
        body = request.get_json(force=True)
        try:
            st.controller.start_directional(
                session["id"], float(body["center_mhz"]),
                float(body.get("bw_khz") or 100.0), body.get("room", ""))
        except (RuntimeError, KeyError, ValueError) as e:
            return err(e, 409)
        return jsonify({"started": True})

    @app.post("/api/directional/stop")
    def directional_stop():
        st.controller.abort()
        return jsonify({"stopped": True})

    @app.post("/api/directional/note")
    def directional_note():
        session = st.session()
        if not session:
            return err("no session")
        body = request.get_json(force=True)
        row = db.query_one("SELECT id FROM directional WHERE session_id=? "
                           "ORDER BY ts_utc DESC LIMIT 1", (session["id"],))
        if not row:
            return err("no directional reading to annotate", 404)
        db.execute("UPDATE directional SET notes=? WHERE id=?",
                   (body.get("notes", ""), row["id"]))
        return jsonify({"saved": True})

    @app.get("/api/directional/log")
    def directional_log():
        session = st.session()
        if not session:
            return err("no session")
        return jsonify(db.query("SELECT * FROM directional WHERE session_id=? "
                                "ORDER BY ts_utc", (session["id"],)))

    # ---------- calibration ----------

    @app.post("/api/calibration/start")
    def calibration_start():
        if st.controller.busy() or st.calibration_status["running"]:
            return err("device busy", 409)
        body = request.get_json(force=True)
        freq = float(body.get("test_freq_mhz") or 433.92)
        if st.controller.device is None:
            try:
                st.controller.connect_device()
            except Exception as e:  # noqa: BLE001
                return err(e, 502)
        st.calibration_status = {"running": True, "result": None, "error": None,
                                 "rows": []}
        st.controller.abort_event.clear()

        def work():
            try:
                result = run_calibration(
                    st.controller, cfg, freq,
                    progress=lambda row: st.calibration_status["rows"].append(row))
                st.calibration_status.update(running=False, result=result)
            except Exception as e:  # noqa: BLE001
                log.exception("calibration failed")
                st.calibration_status.update(running=False, error=str(e))

        threading.Thread(target=work, daemon=True).start()
        return jsonify({"started": True})

    # ---------- report / export ----------

    @app.post("/api/report/generate")
    def report_generate():
        session = st.session()
        if not session:
            return err("no session")
        try:
            result = generate_report(db, cfg, session["id"], st.reports_dir)
        except Exception as e:  # noqa: BLE001
            log.exception("report generation failed")
            return err(e, 500)
        result["filename"] = os.path.basename(result["path"])
        return jsonify(result)

    @app.get("/api/reports")
    def reports_list():
        session = st.session()
        if not session:
            return err("no session")
        rows = db.query("SELECT * FROM reports WHERE session_id=? ORDER BY generated_utc DESC",
                        (session["id"],))
        for r in rows:
            r["filename"] = os.path.basename(r["path"])
        return jsonify(rows)

    @app.get("/reports/<path:name>")
    def report_file(name):
        return send_from_directory(st.reports_dir, name)

    @app.get("/api/export/csv")
    def csv_export():
        session = st.session()
        if not session:
            return err("no session")
        path = export_csv(db, session["id"], st.reports_dir)
        return send_file(path, as_attachment=True,
                         download_name=os.path.basename(path))

    return app
