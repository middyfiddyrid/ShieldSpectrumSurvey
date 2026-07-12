"""Flask server — serves the single-page ops console and the JSON API."""

import json
import logging
import os
import threading
import time

from flask import (Flask, Response, jsonify, request, send_file,
                   send_from_directory, stream_with_context)

from .baseline.base import DiskCache
from .baseline.manager import build_baseline
from .db import TRIAGE_DECISIONS, ATTRIBUTION_STATUSES
from .correlation import compute_room_localization
from .geocode import geocode
from .report import export_csv, generate_report
from .sweep.calibration import run_calibration
from .sweep.planner import plan_full_sweep
from .allocations import bug_band_ranges
from .sweep.runner import SweepController

log = logging.getLogger("survey.server")
WEB_DIR = os.path.join(os.path.dirname(__file__), "web")


def _dedupe_emitters(rows, tol_mhz=0.25):
    """One row per physical emitter: keep the first row (rows are pre-ordered by
    score) within tol_mhz of an already-kept center; the same emitter seen across
    several room sweeps collapses to its best instance."""
    kept = []
    for r in rows:
        if not any(abs(r["center_mhz"] - k["center_mhz"]) <= tol_mhz for k in kept):
            kept.append(r)
    return kept


class _WidthCfg:
    """Wraps a Config so cfg.get('sweep','segment_width_mhz') returns a per-scan
    override (one scalar applied to subghz/mid/high), leaving every other key
    untouched. Lets a segment-size override flow through the planner (incl. the
    _ProxyCfg it builds) without threading a kwarg through every plan function."""
    def __init__(self, real, width_mhz):
        self._real = real
        self._w = {"subghz": width_mhz, "mid": width_mhz, "high": width_mhz}

    def get(self, *keys, default=None):
        if keys[:2] == ("sweep", "segment_width_mhz"):
            return self._w
        return self._real.get(*keys, default=default)

    def __getattr__(self, name):
        return getattr(self._real, name)


class AppState:
    def __init__(self, db, cfg):
        self.db = db
        self.cfg = cfg
        self.cache = DiskCache(cfg.abs_path(cfg.get("cache", "dir", default="cache")),
                               cfg.get("cache", "max_age_hours", default=720))
        self.controller = SweepController(db, cfg)
        # restore the most recent session so a server restart doesn't strand the
        # operator mid-survey (the UI keeps polling whatever session is open)
        last = db.query_one("SELECT id FROM sessions ORDER BY created_utc DESC LIMIT 1")
        self.session_id = last["id"] if last else None
        self.baseline_status = {"running": False, "result": None, "error": None}
        self.calibration_status = {"running": False, "result": None, "error": None}
        self.reports_dir = cfg.abs_path("reports")

    def session(self):
        if self.session_id is None:
            return None
        return self.db.query_one("SELECT * FROM sessions WHERE id=?", (self.session_id,))


def create_app(db, cfg, demo=False) -> Flask:
    app = Flask(__name__, static_folder=None)
    st = AppState(db, cfg)
    st.demo = demo
    app.config["APP_STATE"] = st

    # hands-free second radio: keep seeking the Pluto so it auto-joins as the
    # monitor the moment it's plugged in (operator ask — never hand-connect it).
    # No-op in the public demo (simulated device) and until a sweeper is connected.
    if not demo:
        st.controller.start_monitor_autoseek()

    def err(message, code=400):
        return jsonify({"error": str(message)}), code

    # In the PUBLIC DEMO, every feature that touches real client PII is blocked
    # at the server, not merely hidden in the UI: Persons of Interest, the
    # internal memo, the multi-AI threat analysis, and case-file upload/serve.
    # NOTE: this is "/api/analyze/threat_vectors" specifically, NOT a bare
    # "/api/analyze" prefix — the fused signal ANALYZE (/api/analyze/signal) stays
    # available in the demo for RF identification, but it strips DECODED content
    # (pager text / DTMF / device bits = intercepted comms) in demo mode itself.
    # /api/record* also covers /api/recordings* (prefix) — raw audio of possibly
    # private comms must never be reachable in the public demo. Triage notes +
    # address-novelty history are operator-internal too.
    DEMO_BLOCKED = ("/api/poi", "/api/report/internal_memo",
                    "/api/analyze/threat_vectors", "/api/case_files", "/case_files",
                    "/api/triage", "/api/novelty", "/api/record",
                    "/api/attribution", "/api/decode/override", "/api/ble/scan",
                    "/api/stimulate")

    @app.before_request
    def _demo_guard():
        if st.demo and request.path.startswith(DEMO_BLOCKED):
            return err("This feature is disabled in the public demo.", 403)

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

    # ---------- engagement / pre-sweep workflow ----------

    DEFAULT_TECH_TASKS = [
        "Confirm engagement type and terms with the client; collect deposit if remote/monitoring.",
        "Record the client's stated concerns and any persons the CLIENT names "
        "(Persons of Interest tab — internal only).",
        "Upload only client-authorized background reports to Case Files; keep inside "
        "permissible-purpose / provider-TOS limits (no dragnet on uninvolved third parties).",
        "Build the expected-spectrum baseline for the address.",
        "Remote engagements: ship technical package + setup instructions after deposit; "
        "arrange device return or set up monitoring billing.",
    ]

    @app.get("/api/engagement")
    def engagement_get():
        session = st.session()
        if not session:
            return err("no session")
        row = db.query_one("SELECT * FROM engagement WHERE session_id=?", (session["id"],))
        return jsonify(row or {"engagement_type": "onsite", "status": "intake",
                               "client_name": "", "client_contact": "",
                               "client_concerns": "", "offsite_location": "",
                               "scheduling_note": ""})

    @app.post("/api/engagement")
    def engagement_save():
        session = st.session()
        if not session:
            return err("create a session first")
        b = request.get_json(force=True)
        db.execute(
            "INSERT INTO engagement (session_id,engagement_type,client_name,client_contact,"
            "client_concerns,offsite_location,scheduling_note,status,updated_utc) "
            "VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(session_id) DO UPDATE SET engagement_type=excluded.engagement_type,"
            "client_name=excluded.client_name,client_contact=excluded.client_contact,"
            "client_concerns=excluded.client_concerns,"
            "offsite_location=excluded.offsite_location,"
            "scheduling_note=excluded.scheduling_note,status=excluded.status,"
            "updated_utc=excluded.updated_utc",
            (session["id"], b.get("engagement_type", "onsite"), b.get("client_name", ""),
             b.get("client_contact", ""), b.get("client_concerns", ""),
             b.get("offsite_location", ""), b.get("scheduling_note", ""),
             b.get("status", "intake"), time.time()))
        # seed technician tasks on first save
        existing = db.query_one("SELECT COUNT(*) n FROM tech_tasks WHERE session_id=?",
                                (session["id"],))
        if not existing["n"]:
            for t in DEFAULT_TECH_TASKS:
                db.execute("INSERT INTO tech_tasks (session_id,text,done,created_utc) "
                           "VALUES (?,?,0,?)", (session["id"], t, time.time()))
        return jsonify({"saved": True})

    @app.get("/api/tech_tasks")
    def tech_tasks_list():
        session = st.session()
        if not session:
            return err("no session")
        return jsonify(db.query("SELECT * FROM tech_tasks WHERE session_id=? "
                                "ORDER BY id", (session["id"],)))

    @app.post("/api/tech_tasks")
    def tech_task_add():
        session = st.session()
        if not session:
            return err("no session")
        text = (request.get_json(force=True).get("text") or "").strip()
        if not text:
            return err("task text required")
        tid = db.execute("INSERT INTO tech_tasks (session_id,text,done,created_utc) "
                         "VALUES (?,?,0,?)", (session["id"], text, time.time()))
        return jsonify({"id": tid})

    @app.post("/api/tech_tasks/<int:tid>/toggle")
    def tech_task_toggle(tid):
        row = db.query_one("SELECT done FROM tech_tasks WHERE id=?", (tid,))
        if not row:
            return err("no such task", 404)
        db.execute("UPDATE tech_tasks SET done=? WHERE id=?",
                   (0 if row["done"] else 1, tid))
        return jsonify({"toggled": True})

    @app.post("/api/report/internal_memo")
    def internal_memo_report():
        from .report import generate_internal_memo
        session = st.session()
        if not session:
            return err("no session")
        try:
            result = generate_internal_memo(db, cfg, session["id"], st.reports_dir)
        except Exception as e:  # noqa: BLE001
            log.exception("internal memo failed")
            return err(e, 500)
        result["filename"] = os.path.basename(result["path"])
        return jsonify(result)

    @app.post("/api/report/presweep")
    def presweep_report():
        from .report import generate_presweep_report
        from .report_html import generate_presweep_html
        session = st.session()
        if not session:
            return err("no session")
        try:
            try:
                result = generate_presweep_html(db, cfg, session["id"], st.reports_dir)
            except Exception:  # noqa: BLE001 - no browser etc.: legacy renderer
                log.exception("HTML pre-sweep failed — falling back to reportlab")
                result = generate_presweep_report(db, cfg, session["id"], st.reports_dir)
        except Exception as e:  # noqa: BLE001
            log.exception("pre-sweep report failed")
            return err(e, 500)
        result["filename"] = os.path.basename(result["path"])
        return jsonify(result)

    # ---------- persons of interest / deconfliction (internal only) ----------

    POI_FIELDS = ("name", "aliases", "relationship", "source", "motive",
                  "access_note", "access_tier", "capability_tier", "capability_basis",
                  "resources_note", "conduct_note", "analyst_notes",
                  "opportunity_vectors", "status")

    @app.post("/api/poi")
    def poi_save():
        session = st.session()
        if not session:
            return err("create a session first")
        body = request.get_json(force=True)
        if not (body.get("name") or "").strip():
            return err("name required")
        # capability tier asserted without an evidence basis is exactly the
        # fake-precision failure mode we avoid — require the basis
        if body.get("capability_tier") in ("low", "medium", "high") \
                and not (body.get("capability_basis") or "").strip():
            return err("set the evidence basis for the capability tier "
                       "(profession, credentials, demonstrated skill) — "
                       "tiers without a factual basis aren't recorded")
        vals = {k: body.get(k, "") for k in POI_FIELDS}
        named = 1 if body.get("named_by_client") else 0
        if body.get("id"):
            sets = ", ".join(f"{k}=?" for k in POI_FIELDS)
            db.execute(f"UPDATE persons_of_interest SET {sets}, named_by_client=?, "
                       "updated_utc=? WHERE id=?",
                       (*[vals[k] for k in POI_FIELDS], named, time.time(), body["id"]))
            pid = body["id"]
        else:
            cols = ",".join(POI_FIELDS)
            ph = ",".join("?" * len(POI_FIELDS))
            pid = db.execute(
                f"INSERT INTO persons_of_interest (session_id,{cols},named_by_client,"
                f"created_utc,updated_utc) VALUES (?,{ph},?,?,?)",
                (session["id"], *[vals[k] for k in POI_FIELDS], named,
                 time.time(), time.time()))
            db.execute(
                "INSERT INTO tech_tasks (session_id,text,done,created_utc) VALUES (?,?,0,?)",
                (session["id"],
                 f"Gather client-authorized background for \"{vals['name'].strip()}\" and "
                 "attach it to their POI record (permissible purpose only — no dragnet on "
                 "uninvolved third parties).", time.time()))
            return jsonify({"id": pid, "saved": True, "task_added": True})
        return jsonify({"id": pid, "saved": True})

    @app.get("/api/poi")
    def poi_list():
        session = st.session()
        if not session:
            return err("no session")
        order = "CASE status WHEN 'concern' THEN 0 WHEN 'open' THEN 1 "\
                "WHEN 'cleared' THEN 2 ELSE 3 END, name"
        return jsonify(db.query(
            f"SELECT * FROM persons_of_interest WHERE session_id=? ORDER BY {order}",
            (session["id"],)))

    @app.post("/api/poi/<int:pid>/delete")
    def poi_delete(pid):
        db.execute("DELETE FROM persons_of_interest WHERE id=?", (pid,))
        return jsonify({"deleted": True})

    @app.post("/api/analyze/threat_vectors")
    def analyze_threat_vectors():
        from .analyze import run_analysis
        session = st.session()
        if not session:
            return err("no session")
        body = request.get_json(force=True) or {}
        try:
            result = run_analysis(db, cfg, session["id"], st.reports_dir,
                                  local_contributions=body.get("local") or None)
        except Exception as e:  # noqa: BLE001
            log.exception("threat-vector analysis failed")
            return err(e, 500)
        result["filename"] = os.path.basename(result["path"])
        return jsonify(result)

    # ---------- case files (session attachments) ----------

    @app.post("/api/case_files")
    def case_file_upload():
        import hashlib
        session = st.session()
        if not session:
            return err("create a session first")
        f = request.files.get("file")
        if not f or not f.filename:
            return err("no file")
        raw = f.read()
        if len(raw) > 50 * 1024 * 1024:
            return err("file exceeds 50 MB")
        digest = hashlib.sha256(raw).hexdigest()
        safe = os.path.basename(f.filename).replace("\\", "_")
        stored = f"s{session['id']}_{int(time.time())}_{safe}"
        case_dir = cfg.abs_path("case_files")
        os.makedirs(case_dir, exist_ok=True)
        with open(os.path.join(case_dir, stored), "wb") as out:
            out.write(raw)
        poi_id = request.form.get("poi_id", type=int)
        db.execute(
            "INSERT INTO case_files (session_id,filename,stored_name,kind,size_bytes,"
            "sha256,uploaded_utc,notes,poi_id) VALUES (?,?,?,?,?,?,?,?,?)",
            (session["id"], safe, stored, request.form.get("kind", ""),
             len(raw), digest, time.time(), request.form.get("notes", ""), poi_id))
        return jsonify({"uploaded": True, "filename": safe, "size_bytes": len(raw),
                        "poi_id": poi_id})

    @app.get("/api/case_files")
    def case_files_list():
        session = st.session()
        if not session:
            return err("no session")
        poi_id = request.args.get("poi_id", type=int)
        sql = ("SELECT cf.id,cf.filename,cf.kind,cf.size_bytes,cf.uploaded_utc,cf.notes,"
               "cf.poi_id,p.name AS poi_name FROM case_files cf "
               "LEFT JOIN persons_of_interest p ON p.id=cf.poi_id "
               "WHERE cf.session_id=?")
        params = [session["id"]]
        if poi_id:
            sql += " AND cf.poi_id=?"
            params.append(poi_id)
        return jsonify(db.query(sql + " ORDER BY cf.uploaded_utc DESC", params))

    @app.get("/case_files/<int:file_id>")
    def case_file_download(file_id):
        row = db.query_one("SELECT * FROM case_files WHERE id=?", (file_id,))
        if not row:
            return err("no such file", 404)
        return send_from_directory(cfg.abs_path("case_files"), row["stored_name"],
                                   as_attachment=True, download_name=row["filename"])

    @app.post("/api/reports/open_local")
    def report_open_local():
        # the embedded preview browser cannot render PDFs (black page), so the
        # UI offers opening the file in the system's default desktop viewer
        name = os.path.basename(request.get_json(force=True).get("filename") or "")
        path = os.path.join(st.reports_dir, name)
        if not name or not os.path.exists(path):
            return err("no such report", 404)
        os.startfile(path)
        return jsonify({"opened": True})

    # ---------- case-study / device library (cross-session) ----------

    CS_FIELDS = ("title", "device_class", "rf_signature", "discovery",
                 "case_history", "lessons")

    @app.get("/api/case_studies")
    def case_studies_list():
        return jsonify(db.query("SELECT * FROM case_studies ORDER BY id"))

    @app.post("/api/case_studies")
    def case_study_save():
        body = request.get_json(force=True)
        if not (body.get("title") or "").strip():
            return err("title required")
        vals = {k: body.get(k, "") for k in CS_FIELDS}
        deid = 1 if body.get("deidentified") else 0
        if body.get("id"):
            sets = ", ".join(f"{k}=?" for k in CS_FIELDS)
            db.execute(f"UPDATE case_studies SET {sets}, deidentified=?, "
                       "updated_utc=? WHERE id=?",
                       (*[vals[k] for k in CS_FIELDS], deid, time.time(), body["id"]))
            cid = body["id"]
        else:
            cols = ",".join(CS_FIELDS)
            ph = ",".join("?" * len(CS_FIELDS))
            cid = db.execute(
                f"INSERT INTO case_studies ({cols},deidentified,created_utc,updated_utc) "
                f"VALUES ({ph},?,?,?)",
                (*[vals[k] for k in CS_FIELDS], deid, time.time(), time.time()))
        return jsonify({"id": cid, "saved": True})

    @app.post("/api/case_studies/<int:cid>/delete")
    def case_study_delete(cid):
        db.execute("DELETE FROM case_studies WHERE id=?", (cid,))
        return jsonify({"deleted": True})

    @app.post("/api/case_studies/<int:cid>/photo")
    def case_study_photo_upload(cid):
        if not db.query_one("SELECT id FROM case_studies WHERE id=?", (cid,)):
            return err("no such case study", 404)
        f = request.files.get("file")
        if not f or not f.filename:
            return err("no file")
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
            return err("image files only")
        d = cfg.abs_path("data/case_study_photos")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"cs{cid}{ext}")
        f.save(path)
        db.execute("UPDATE case_studies SET photo_path=?, updated_utc=? WHERE id=?",
                   (path, time.time(), cid))
        return jsonify({"uploaded": True})

    @app.get("/case_study_photo/<int:cid>")
    def case_study_photo_get(cid):
        row = db.query_one("SELECT photo_path FROM case_studies WHERE id=?", (cid,))
        if not row or not row["photo_path"] or not os.path.exists(row["photo_path"]):
            return err("no photo", 404)
        return send_from_directory(os.path.dirname(row["photo_path"]),
                                   os.path.basename(row["photo_path"]))

    # ---------- overall state (polled by the UI) ----------

    @app.get("/api/state")
    def state():
        session = st.session()
        sweeps = []
        if session:
            sweeps = db.query("SELECT id,phase,module,room,profile,status,started_utc,"
                              "finished_utc,simulated FROM sweeps WHERE session_id=? "
                              "ORDER BY id", (session["id"],))
        # real radio listen state (drives the LIVE indicator) — sourced from the
        # controller flags _audio_blocks sets, so a silently-dropped stream reads idle
        ls = getattr(st.controller, "listen_spectrum", None)
        live = {"listening": bool(getattr(st.controller, "listening", False))}
        if ls:
            live.update({"center_mhz": ls.get("center_mhz"), "mode": ls.get("mode"),
                         "ts": ls.get("ts")})
        return jsonify({
            "session": session,
            "source": (st.controller.active_source
                       or os.environ.get("SWEEP_SOURCE")
                       or cfg.get("device", "source", default="rfexplorer")),
            "device": st.controller.device_info(),
            "roles": st.controller.device_roles(),
            "sweep": st.controller.state,
            "sweeps": sweeps,
            "baseline": st.baseline_status,
            "calibration": st.calibration_status,
            "busy": st.controller.busy(),
            "live": live,
            "demo": st.demo,
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
        body = request.get_json(silent=True) or {}
        source = (body.get("source") or "").strip().lower() or None
        # keep_other (dual-device): connect this radio WITHOUT closing the other,
        # so RFE-sweep + Pluto-monitor can coexist. Only honored when the
        # pluto.concurrent_monitor flag is on; otherwise it's the legacy swap.
        keep_other = bool(body.get("keep_other"))
        try:
            info = st.controller.connect_device(source=source, keep_other=keep_other)
        except Exception as e:  # noqa: BLE001
            return err(e, 502)
        # remember the operator's pick so the next launch defaults to it
        if source:
            try:
                cfg.set_and_save(("device", "source"), source)
            except Exception:  # noqa: BLE001
                log.exception("could not persist device.source")
        # hands-free second radio: right after the sweeper connects, seek the
        # Pluto so it auto-joins as the monitor without a manual connect. Off the
        # request thread so this response returns immediately; the connect can
        # take a few seconds (libiio bring-up) and the background poller keeps
        # trying anyway if the Pluto is plugged in later.
        if (source not in ("pluto", "sdr", "ad936x")
                and st.controller.concurrent_enabled()):
            threading.Thread(target=st.controller.autoseek_monitor,
                             daemon=True).start()
        if st.session_id:
            db.set_device_info(st.session_id, info.to_dict())
        return jsonify(info.to_dict())

    @app.get("/api/device/roles")
    def device_roles():
        """Per-radio roles (sweeper / monitor / idle) + whether concurrent
        dual-device monitoring is enabled — lets the UI show 'RFE sweeping +
        Pluto monitoring' and choose LIVE vs post-hoc triage."""
        return jsonify(st.controller.device_roles())

    @app.post("/api/device/swap")
    def device_swap():
        """Swap sweeper <-> monitor (field 'just switch them around'). Refused
        while a sweep is running."""
        try:
            return jsonify(st.controller.swap_roles())
        except Exception as e:  # noqa: BLE001
            return err(e, 409)

    @app.post("/api/device/roles/assign")
    def device_roles_assign():
        """Explicitly assign which connected radio sweeps and which monitors."""
        body = request.get_json(silent=True) or {}
        try:
            return jsonify(st.controller.set_roles(
                sweeper=(body.get("sweeper") or "").strip().lower() or None,
                monitor=(body.get("monitor") or "").strip().lower() or None))
        except Exception as e:  # noqa: BLE001
            return err(e, 409)

    @app.post("/api/identify")
    def identify_signal():
        """sigidwiki-style ID of an emitter. With a connected Pluto, captures IQ
        at the frequency and matches by modulation features; otherwise matches by
        frequency/bandwidth only. Accepts {center_mhz,bw_khz} or {signal_id} /
        {capture_id}."""
        from .identify import identify, iq_features, load_reference, spectrogram
        body = request.get_json(silent=True) or {}
        center_mhz = body.get("center_mhz")
        bw_khz = body.get("bw_khz")
        for table, key in (("captures", "capture_id"), ("signals", "signal_id")):
            if center_mhz is None and body.get(key):
                row = db.query_one(f"SELECT center_mhz,bw_khz FROM {table} WHERE id=?",
                                   (body[key],))
                if row:
                    center_mhz, bw_khz = row["center_mhz"], row["bw_khz"]
        if center_mhz is None:
            return err("need center_mhz (or a signal_id / capture_id)")
        center_mhz = float(center_mhz)

        ref = load_reference()    # ships with the package (survey/sigidwiki_ref.json)
        feats, spec, note = None, None, ""
        dev = st.controller.device
        is_sdr = dev is not None and hasattr(dev, "capture_iq") and \
            getattr(dev, "is_swept_sdr", False)
        if is_sdr and st.controller.busy():
            note = "a sweep is running — stop it first to capture IQ; matched on frequency only"
        elif is_sdr:
            try:
                sr = int(cfg.get("pluto", "sample_rate", default=20_000_000))
                n = int(cfg.get("pluto", "identify_samples", default=131072))
                reps = max(1, int(cfg.get("pluto", "identify_captures", default=8)))
                # bursty emitters (Wi-Fi/BLE) only show in some snapshots — take
                # the highest-SNR of several captures so we catch a burst, while a
                # genuinely empty channel still reads as noise across all of them.
                best_iq = None
                for _ in range(reps):
                    iq = dev.capture_iq(center_mhz * 1e6, sr, n)
                    fe = iq_features(iq, sr)
                    if feats is None or fe["snr_db"] > feats["snr_db"]:
                        feats, best_iq = fe, iq
                spec = spectrogram(best_iq, sr)
                if not feats.get("signal_present"):
                    note = (f"no clear signal above the noise floor at {center_mhz:.3f} MHz "
                            f"(best SNR {feats['snr_db']} dB over {reps} captures) — likely an "
                            f"empty channel; the candidates are what COULD occupy this band, "
                            f"not a detected emitter")
            except Exception as e:  # noqa: BLE001
                log.exception("identify IQ capture failed")
                note = f"IQ capture failed ({e}); matched on frequency only"
        else:
            note = ("BAND GUESS ONLY — no SDR connected for IQ capture (Pluto or "
                    "B220); matched on frequency/bandwidth alone")
        candidates = identify(center_mhz * 1e6, feats, ref)
        from .pocsag import looks_like_pocsag
        pocsag_likely = bool(feats and looks_like_pocsag(feats))
        # persist onto the originating detection so ID chips survive reloads
        # and show up in Results/Triage. 'band' mode = frequency-only guess.
        id_mode = "iq" if feats else "band"
        persisted = False
        session = st.session()
        if body.get("signal_id") and session:
            try:
                persisted = db.set_signal_identification(
                    int(body["signal_id"]), session["id"], candidates,
                    id_mode) is not None
            except Exception:  # noqa: BLE001 - persistence must not break the ID
                log.exception("could not persist identify result")
        return jsonify({"center_mhz": center_mhz, "bw_khz": bw_khz,
                        "source": st.controller.active_source, "features": feats,
                        "note": note, "candidates": candidates, "spectrogram": spec,
                        "pocsag_likely": pocsag_likely,
                        "id_mode": id_mode, "persisted": persisted})

    def _mcfg(msrc, key, default):
        """Monitor-radio config knob. Every listen/analyze tunable historically
        lived under the `pluto:` section; now that the B220 can be the monitor
        (single-radio mode), read the monitor source's OWN section first —
        critically cal_offset_db, which differs by ~66 dB between the two radios
        — and fall back to the pluto section, then the given default."""
        if msrc and msrc != "pluto":
            v = cfg.get(msrc, key, default=None)
            if v is not None:
                return v
        return cfg.get("pluto", key, default=default)

    @app.post("/api/inspect")
    def inspect_signal():
        """Tune the Pluto to a frequency and return an analyzer trace + waterfall
        + demodulated audio (WAV) so the operator can SEE and HEAR it. Degrades
        cleanly when the radio isn't connected, is busy, or can't reach the freq."""
        import base64
        from .demod import demodulate, pick_mode, wav_bytes
        from .identify import spectrogram
        from .sweep.sdr_source import welch_psd
        body = request.get_json(silent=True) or {}
        if body.get("center_mhz") is None:
            return err("need center_mhz")
        center_mhz = float(body["center_mhz"])
        label = body.get("label", "")
        mode = (body.get("mode") or pick_mode(center_mhz, label)).lower()

        dev, mlock, mstop, msrc = st.controller.ensure_monitor()
        is_sdr = dev is not None and hasattr(dev, "capture_iq") and \
            getattr(dev, "is_swept_sdr", False)
        base = {"center_mhz": center_mhz, "label": label, "mode": mode, "in_range": False}
        if not is_sdr:
            return jsonify({**base, "note": "connect the Pluto (SDR) to tune, view and listen"})
        if st.controller.busy(msrc):
            return jsonify({**base, "note": "the monitor radio is busy — stop it to tune & listen"})
        mods = dev.info.modules
        lo_mhz = min(m.min_mhz for m in mods)
        hi_mhz = max(m.max_mhz for m in mods)
        if not (lo_mhz <= center_mhz <= hi_mhz):
            return jsonify({**base, "note": f"{center_mhz:.3f} MHz is outside the radio's "
                            f"{lo_mhz:.0f}-{hi_mhz:.0f} MHz range — apply the AD9364 extension to reach it"})
        try:
            fs = float(_mcfg(msrc, "listen_sample_rate", 2_400_000))
            # POCSAG needs preamble+batch (~2.2 s at 512 baud) plus alignment slack
            # -> a longer one-shot capture (default 3 s; baud-agnostic, the decoder
            # tries all bauds on the same buffer)
            secs = (float(_mcfg(msrc, "pocsag_seconds", 3.0))
                    if mode == "pocsag"
                    else float(_mcfg(msrc, "listen_seconds", 0.5)))
            n = int(fs * secs)
            mstop.set()                            # preempt any live audio stream on this radio
            with mlock:                            # serialise this radio's access
                iq = dev.capture_iq(center_mhz * 1e6, fs, n)
        except Exception as e:  # noqa: BLE001
            log.exception("inspect capture failed")
            return jsonify({**base, "note": f"capture failed: {e}"})

        cal = float(_mcfg(msrc, "cal_offset_db", -30.0))
        bb, psd_db = welch_psd(iq, fs, nperseg=2048)
        step = max(1, bb.size // 480)
        analyzer = {"freqs_mhz": [round((center_mhz * 1e6 + f) / 1e6, 5) for f in bb[::step]],
                    "power_dbm": [round(float(p) + cal, 1) for p in psd_db[::step]]}
        wf = spectrogram(iq, fs, n_time=90, nperseg=200)
        waterfall = {"freqs_mhz": [round((center_mhz * 1e6 + f) / 1e6, 4) for f in wf["freqs_hz"]],
                     "db": wf["db"], "vmin": wf["vmin"], "vmax": wf["vmax"]}
        audio, decode = None, None
        if mode == "pocsag":
            # decode pages instead of producing (unlistenable) hash audio
            try:
                from . import pocsag
                decode = pocsag.decode_pocsag(
                    iq, fs, correct=bool(cfg.get("pluto", "pocsag_bch_correct", default=True)))
                decode["protocol"] = "POCSAG"
            except Exception:  # noqa: BLE001
                log.exception("pocsag decode failed")
        else:
            try:
                pcm, arate = demodulate(iq, fs, mode)
                audio = "data:audio/wav;base64," + base64.b64encode(wav_bytes(pcm, arate)).decode()
            except Exception:  # noqa: BLE001
                log.exception("demod failed")
        return jsonify({**base, "in_range": True, "analyzer": analyzer,
                        "waterfall": waterfall, "audio": audio, "decode": decode, "note": ""})

    @app.post("/api/analyze/signal")
    def analyze_signal():
        """FUSED per-signal ANALYZE — the monitor/receiver 'real demod' brain.
        ONE IQ capture, then everything at once: modulation features
        (identify.iq_features) + sigidwiki candidates (identify) + a live
        (instantaneous) analyzer (welch_psd) + waterfall (spectrogram) + EITHER
        protocol decode (POCSAG) OR demod audio, PLUS the new-to-address novelty
        read. This is what replaces the audio-only 'bullshit demod': it says WHAT
        the signal IS, not just plays it. Available in the demo for RF
        identification, but DECODED content is stripped in demo mode (it's
        intercepted comms). Degrades cleanly (no radio / busy / out-of-range)
        exactly like /api/inspect."""
        import base64
        from .demod import demodulate, pick_mode, wav_bytes
        from .identify import identify, iq_features, load_reference, spectrogram
        from .pocsag import looks_like_pocsag
        from .decode_router import route_decode, interpret, DATA_MODES, DECODE_MODES
        from .sweep.sdr_source import welch_psd
        body = request.get_json(silent=True) or {}
        center_mhz = body.get("center_mhz")
        bw_khz = body.get("bw_khz")
        for table, key in (("captures", "capture_id"), ("signals", "signal_id")):
            if center_mhz is None and body.get(key):
                row = db.query_one(f"SELECT center_mhz,bw_khz FROM {table} WHERE id=?",
                                   (body[key],))
                if row:
                    center_mhz, bw_khz = row["center_mhz"], row["bw_khz"]
        if center_mhz is None:
            return err("need center_mhz (or a signal_id / capture_id)")
        center_mhz = float(center_mhz)
        label = body.get("label", "")
        mode = (body.get("mode") or pick_mode(center_mhz, label)).lower()

        ref = load_reference()
        feats = spec = analyzer = audio = decode = interpretation = None
        in_range = False
        note = ""
        # Monitor target: in dual-device mode this is the Pluto + its OWN lock, so
        # ANALYZE runs while the B220/RFE keeps sweeping — ensure_monitor even
        # auto-opens the Pluto mid-sweep. In single-device mode it's the active
        # device + the legacy lock and busy(msrc) == busy() — unchanged.
        dev, mlock, mstop, msrc = st.controller.ensure_monitor()
        cal = float(_mcfg(msrc, "cal_offset_db", -30.0))
        is_sdr = dev is not None and hasattr(dev, "capture_iq") and \
            getattr(dev, "is_swept_sdr", False)
        if not is_sdr:
            note = ("connect the Pluto (SDR) to receive & analyze this signal live — "
                    "candidates shown are frequency-only")
        elif st.controller.busy(msrc):
            note = ("the monitor radio is busy — candidates matched on frequency "
                    "only (dual-device mode lets the Pluto monitor while the RFE sweeps)")
        else:
            mods = dev.info.modules
            lo_mhz, hi_mhz = min(m.min_mhz for m in mods), max(m.max_mhz for m in mods)
            if not (lo_mhz <= center_mhz <= hi_mhz):
                note = (f"{center_mhz:.3f} MHz is outside the radio's "
                        f"{lo_mhz:.0f}-{hi_mhz:.0f} MHz range — apply the AD9364 extension")
            else:
                try:
                    fs = float(_mcfg(msrc, "listen_sample_rate", 2_400_000))
                    # ANALYZE is a deliberate "interrogate this" action — capture a
                    # bit longer than the live listen so the decoders have enough
                    # symbols (paging frames, repeated device packets).
                    secs = (float(_mcfg(msrc, "pocsag_seconds", 3.0))
                            if mode in DATA_MODES
                            else float(_mcfg(msrc, "analyze_seconds", 2.0)))
                    n = int(fs * secs)
                    mstop.set()                            # preempt any live stream on this radio
                    with mlock:                            # serialise this radio's access
                        iq = dev.capture_iq(center_mhz * 1e6, fs, n)
                    in_range = True
                    # ---- ONE capture, analyzed every way (the fusion) ----
                    feats = iq_features(iq, fs)
                    wf = spectrogram(iq, fs, n_time=90, nperseg=200)
                    spec = {"freqs_mhz": [round((center_mhz * 1e6 + f) / 1e6, 4)
                                          for f in wf["freqs_hz"]],
                            "db": wf["db"], "vmin": wf["vmin"], "vmax": wf["vmax"]}
                    bb, psd_db = welch_psd(iq, fs, nperseg=2048)
                    step = max(1, bb.size // 480)
                    analyzer = {"freqs_mhz": [round((center_mhz * 1e6 + f) / 1e6, 5)
                                              for f in bb[::step]],
                                "power_dbm": [round(float(p) + cal, 1) for p in psd_db[::step]]}
                    # DECODE: auto-route across protocols (POCSAG/FLEX/OOK-FSK/DTMF/CW).
                    # Each decoder's own honesty gate means a mismatch decodes to
                    # nothing, never a hallucinated page. An explicit mode is tried first.
                    prefer = mode if mode in DECODE_MODES else None
                    routed = route_decode(iq, fs, feats, prefer=prefer)
                    if routed.get("messages"):
                        decode = routed
                    interpretation = interpret(feats, routed.get("protocol"))
                    # AUDIO: analog modes are listenable (hear the room); data modes
                    # would just be hash, so skip audio for them. HONESTY GATE: an
                    # empty channel gets NO audio clip — the one-shot demod path
                    # peak-normalizes, which turns pure noise into full-scale fuzz
                    # that reads as "demod is broken". Say "empty channel" instead.
                    if mode not in DATA_MODES and feats.get("signal_present"):
                        try:
                            pcm, arate = demodulate(iq, fs, mode,
                                                    float(body.get("offset_hz") or 0.0))
                            audio = ("data:audio/wav;base64,"
                                     + base64.b64encode(wav_bytes(pcm, arate)).decode())
                        except Exception:  # noqa: BLE001
                            log.exception("analyze demod failed")
                    if not feats.get("signal_present"):
                        note = (f"no clear signal above the noise floor at {center_mhz:.3f} "
                                f"MHz (SNR {feats['snr_db']} dB) — likely an empty channel; "
                                f"audio muted (nothing to demodulate). If a signal should be "
                                f"here: check the antenna is on the monitor radio's RX port "
                                f"and its gain setting")
                except Exception as e:  # noqa: BLE001
                    log.exception("analyze capture failed")
                    note = f"capture failed: {e}"

        candidates = identify(center_mhz * 1e6, feats, ref)
        pocsag_likely = bool(feats and looks_like_pocsag(feats))
        novelty = None
        session = st.session()
        if session:
            try:
                from .novelty import compute_novelty
                novelty = compute_novelty(db, session["address"], center_mhz,
                                          session["created_utc"], cur_bw_khz=bw_khz)
            except Exception:  # noqa: BLE001
                log.exception("novelty lookup failed")
        if st.demo:
            # Decoded third-party content (pager text, DTMF digits, device bit
            # patterns) is intercepted-comms data — NEVER expose it in the public
            # demo. Keep the safe RF identification (features/candidates); drop the
            # decoded payload and re-derive the verdict without a 'decoded' claim.
            decode = None
            interpretation = interpret(feats, None)
        return jsonify({
            "center_mhz": center_mhz, "label": label, "mode": mode,
            "bw_khz": bw_khz, "in_range": in_range, "note": note,
            "source": st.controller.active_source, "features": feats,
            "candidates": candidates, "analyzer": analyzer, "waterfall": spec,
            "audio": audio, "decode": decode, "interpretation": interpretation,
            "pocsag_likely": pocsag_likely,
            "signal_present": bool(feats and feats.get("signal_present")),
            "novelty": novelty})

    def _validate_listen_request():
        """Shared guard for the audio-stream endpoints. Returns
        (center_mhz, mode, dev, fs, n_block, cal) on success, or an (error
        response, status) tuple the caller returns directly. Keeps the two stream
        endpoints (WAV + raw PCM) identical in their device/range/busy checks."""
        from .demod import pick_mode
        if request.args.get("center_mhz") is None:
            return err("need center_mhz")
        center_mhz = float(request.args["center_mhz"])
        mode = (request.args.get("mode") or pick_mode(center_mhz)).lower()
        # dual mode: listen on the MONITOR radio (the Pluto) so a sweep can keep
        # running on the B220/RFE — ensure_monitor auto-opens the Pluto mid-sweep
        # if it isn't connected yet; single-device behaviour is unchanged.
        dev, _mlock, _mstop, msrc = st.controller.ensure_monitor()
        if not (dev is not None and hasattr(dev, "stream_iq")
                and getattr(dev, "is_swept_sdr", False)):
            return err("connect the Pluto (SDR) to listen", 409)
        if st.controller.busy(msrc):
            return err("the monitor radio is busy — stop its sweep to listen "
                       "(or plug in the Pluto: it takes over monitoring "
                       "automatically while the sweep runs)", 409)
        mods = dev.info.modules
        lo_mhz, hi_mhz = min(m.min_mhz for m in mods), max(m.max_mhz for m in mods)
        if not (lo_mhz <= center_mhz <= hi_mhz):
            return err(f"{center_mhz:.3f} MHz is outside the radio's range", 416)
        ibw = request.args.get("ibw_mhz")
        if ibw is not None:
            try:
                ibw_mhz = max(1.0, min(20.0, float(ibw)))
                fs = ibw_mhz * 1_000_000
            except (TypeError, ValueError):
                fs = float(_mcfg(msrc, "stream_sample_rate", 2_400_000))
        else:
            fs = float(_mcfg(msrc, "stream_sample_rate", 2_400_000))
        block_s = request.args.get("block_s")
        if block_s is not None:
            try:
                block_s = max(0.1, min(1.0, float(block_s)))
            except (TypeError, ValueError):
                block_s = float(_mcfg(msrc, "listen_block_seconds", 0.5))
        else:
            block_s = float(_mcfg(msrc, "listen_block_seconds", 0.5))
        n_block = max(1024, int(fs * block_s))
        cal = float(_mcfg(msrc, "cal_offset_db", -30.0))
        return center_mhz, mode, dev, fs, n_block, cal

    def _audio_blocks(center_mhz, mode, dev, fs, n_block, cal):
        """Core live-audio generator shared by /api/listen and /api/listen_pcm.

        Tunes the radio ONCE (via dev.stream_iq), demodulates each live IQ block,
        and yields (pcm_int16_ndarray, arate). As a side effect it publishes the
        live analyzer + scrolling waterfall to st.controller.listen_spectrum for
        the inspect panel to poll. Owns the full device-serialisation contract:
        preempts any running stream, acquires device_lock, clears/sets listen_stop
        and the `listening` flag, and tears all of that down in finally — so the
        framing wrappers stay trivial and can't deadlock the device."""
        import time as _time
        import numpy as _np
        from .demod import demodulate
        from .identify import waterfall_stft
        from .sweep.sdr_source import welch_psd
        _mdev, mlock, mstop, msrc = st.controller.monitor_target()
        # live-spectrum tunables (read once; the client owns the scroll history)
        an_nperseg = int(_mcfg(msrc, "listen_analyzer_nperseg", 2048))
        an_bins = int(_mcfg(msrc, "listen_analyzer_bins", 512))
        an_ema = float(_mcfg(msrc, "listen_analyzer_ema", 0.4))
        wf_nperseg = int(_mcfg(msrc, "listen_wf_nperseg", 512))
        wf_rows = int(_mcfg(msrc, "listen_wf_rows_per_block", 8))
        fl_ema = float(_mcfg(msrc, "listen_wf_floor_ema", 0.1))
        span_lo = float(_mcfg(msrc, "listen_wf_span_lo_db", -5.0))
        span_hi = float(_mcfg(msrc, "listen_wf_span_hi_db", 40.0))
        psd_ema = nf_ema = None
        wf_seq = 0
        # live demod strengthening (DEMOD_ANALYZE_DISSEMINATE_DESIGN.md §4a): ONE
        # DemodState per stream so squelch/AGC/de-emphasis carry across blocks.
        from .demod import DemodState

        def _dnum(key, d):                    # null-safe: an explicit YAML null -> default
            v = cfg.get("demod", key, default=d)
            return d if v is None else v
        dstate = DemodState(
            agc_target_rms=float(_dnum("agc_target_rms", 0.18)),
            agc_attack_ms=float(_dnum("agc_attack_ms", 8.0)),
            agc_release_ms=float(_dnum("agc_release_ms", 300.0)),
            agc_max_gain=float(_dnum("agc_max_gain", 40.0)),
            squelch_hang_blocks=int(_dnum("squelch_hang_blocks", 3)),
            deemphasis_us=float(_dnum("deemphasis_us", 75.0)))
        sq_default = _mcfg(msrc, "listen_squelch_snr_db", 5.0)
        last_off, last_bw = 0.0, None
        mstop.set()                           # preempt whatever stream is running now
        with mlock:                           # wait for the previous user of THIS radio to release
            mstop.clear()
            st.controller.listening = True
            st.controller.listen_offset_hz = 0.0   # new tune -> demod band center until clicked
            st.controller.listen_chan_bw_hz = None # new tune -> per-mode default channel width
            st.controller.listen_squelch_snr_db = sq_default  # ON by default (5 dB; config listen_squelch_snr_db, null=off)
            st.controller.listen_spectrum = None
            stop = mstop
            try:
                for iq in dev.stream_iq(center_mhz * 1e6, fs, n_block, stop):
                    try:
                        off = float(getattr(st.controller, "listen_offset_hz", 0.0) or 0.0)
                        bw = getattr(st.controller, "listen_chan_bw_hz", None)
                        if off != last_off or bw != last_bw:
                            dstate.reset_continuity()      # passband moved -> don't click
                            last_off, last_bw = off, bw
                        sq = getattr(st.controller, "listen_squelch_snr_db", None)
                        pcm, arate = demodulate(iq, fs, mode, off, bw,
                                                state=dstate, squelch_snr_db=sq)
                    except Exception:  # noqa: BLE001
                        continue
                    yield pcm, arate
                    # live analyzer + scrolling waterfall from the WHOLE block.
                    # The client owns the scroll history: we ship the new rows +
                    # a monotonic seq. db == new_rows here so the JSON stays a
                    # superset of the one-shot /api/inspect (POCSAG) contract.
                    try:
                        # ANALYZER: Welch over the full block, decimated to an_bins
                        # by power-domain mean (a plain stride-skip aliases), then
                        # smoothed across blocks to kill single-FFT jitter.
                        bb, psd = welch_psd(iq, fs, nperseg=an_nperseg)
                        grp = max(1, bb.size // an_bins)
                        keep = (bb.size // grp) * grp
                        lin = (10.0 ** (psd[:keep] / 10.0)).reshape(-1, grp).mean(axis=1)
                        f_dec = bb[:keep].reshape(-1, grp).mean(axis=1)
                        psd_dec = 10.0 * _np.log10(lin + 1e-20)
                        if psd_ema is None or psd_ema.shape != psd_dec.shape:
                            psd_ema = psd_dec
                        else:
                            psd_ema = (1.0 - an_ema) * psd_ema + an_ema * psd_dec
                        af = [round((center_mhz * 1e6 + f) / 1e6, 5) for f in f_dec]
                        ap = [round(float(p) + cal, 1) for p in psd_ema]
                        # WATERFALL: contiguous STFT rows spanning the block + a
                        # stable color floor (EMA of a robust percentile).
                        wf = waterfall_stft(iq, fs, nperseg=wf_nperseg, rows=wf_rows)
                        db = _np.asarray(wf["db"])
                        nf = float(_np.percentile(db, 30))
                        nf_ema = nf if nf_ema is None else (1.0 - fl_ema) * nf_ema + fl_ema * nf
                        wf_seq += 1
                        rows_list = db.round(1).tolist()
                        st.controller.listen_spectrum = {
                            "center_mhz": center_mhz, "mode": mode,
                            "ts": _time.time(), "seq": wf_seq,
                            "analyzer": {"freqs_mhz": af, "power_dbm": ap},
                            "demod": {
                                "snr_db": (round(dstate.last_snr_db, 1)
                                           if dstate.last_snr_db is not None else None),
                                "gated": bool(dstate.gated),
                                "agc_gain_db": round(20.0 * _np.log10(
                                    max(dstate.last_out_gain, 1e-6)), 1),
                                "squelch_db": getattr(st.controller,
                                                      "listen_squelch_snr_db", None)},
                            "waterfall": {
                                "freqs_mhz": [round((center_mhz * 1e6 + f) / 1e6, 4)
                                              for f in wf["freqs_hz"]],
                                "db": rows_list, "new_rows": rows_list, "seq": wf_seq,
                                "vmin": nf_ema + span_lo, "vmax": nf_ema + span_hi}}
                    except Exception:  # noqa: BLE001
                        pass
            except Exception:  # noqa: BLE001 - client disconnect / device error
                pass
            finally:
                st.controller.listening = False
                st.controller.listen_spectrum = None

    @app.get("/api/listen")
    def listen_stream():
        """Open-ended demodulated-audio stream as a continuous WAV (legacy/
        fallback path). Tune the Pluto and keep demodulating live IQ until the
        client disconnects (or a sweep/inspect preempts it). One stream at a time,
        serialised on the radio. A plain <audio> element can play it, but delivery
        pacing on the dev server can cause stutter — prefer /api/listen_pcm with
        the Web Audio client for smooth playback."""
        from .demod import wav_stream_header
        v = _validate_listen_request()
        if not isinstance(v, tuple) or len(v) != 6:
            return v                          # (error_response, status)
        center_mhz, mode, dev, fs, n_block, cal = v

        def gen():
            first = True
            for pcm, arate in _audio_blocks(center_mhz, mode, dev, fs, n_block, cal):
                if first:
                    yield wav_stream_header(arate)
                    first = False
                yield pcm.tobytes()
        return Response(stream_with_context(gen()), mimetype="audio/wav",
                        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @app.get("/api/listen_pcm")
    def listen_pcm_stream():
        """Open-ended RAW little-endian int16 PCM stream for the Web Audio client.

        Emits a one-line self-describing header (sample rate + format) then a
        continuous run of mono '<i2' samples — NO WAV container and no data-size
        lie, so the browser never treats it as a finite seekable file. The client
        (app.js) reads the body as a stream, parses PCM frames, and schedules them
        against the AudioContext clock, which decouples audible playback from the
        dev server's delivery pacing (the cause of the <audio> stutter). Shares all
        device/range/busy/serialisation logic with /api/listen."""
        from .demod import pcm_stream_header
        v = _validate_listen_request()
        if not isinstance(v, tuple) or len(v) != 6:
            return v                          # (error_response, status)
        center_mhz, mode, dev, fs, n_block, cal = v

        def gen():
            first = True
            for pcm, arate in _audio_blocks(center_mhz, mode, dev, fs, n_block, cal):
                if first:
                    yield pcm_stream_header(arate, mode)
                    first = False
                yield pcm.tobytes()
        return Response(stream_with_context(gen()),
                        mimetype="application/octet-stream",
                        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @app.get("/api/listen_spectrum")
    def listen_spectrum_frame():
        """Latest live analyzer + waterfall produced by the active audio stream
        (polled by the inspect panel for moving visuals)."""
        return jsonify(st.controller.listen_spectrum or {"center_mhz": None})

    @app.post("/api/listen/offset")
    def listen_offset():
        """Move the demod tuner WITHIN the live band, with no re-tune: the running
        audio stream demodulates (center + offset_hz) on its next block. Lets the
        operator click a signal in the live spectrum and hear THAT one. Clamped to
        the captured bandwidth so it can't land outside the visible band."""
        body = request.get_json(silent=True) or {}
        try:
            off = float(body.get("offset_hz", 0.0))
        except (TypeError, ValueError):
            return err("offset_hz must be a number")
        _d, _l, _s, _msrc = st.controller.monitor_target()
        fs = float(_mcfg(_msrc, "stream_sample_rate", 2_400_000))
        lim = fs * 0.48                        # stay inside the captured band edges
        st.controller.listen_offset_hz = max(-lim, min(lim, off))
        return jsonify({"offset_hz": st.controller.listen_offset_hz})

    @app.post("/api/listen/bandwidth")
    def listen_bandwidth():
        """Adjust the demodulator CHANNEL WIDTH live — the shaded band on the
        analyzer. Narrow for a tight signal (pager, NFM voice), wide for a
        wireless mic / video subcarrier. 'auto' restores the per-mode default."""
        body = request.get_json(silent=True) or {}
        v = body.get("chan_bw_hz")
        if v in (None, "", "auto"):
            st.controller.listen_chan_bw_hz = None
            return jsonify({"chan_bw_hz": None})
        try:
            bw = float(v)
        except (TypeError, ValueError):
            return err("chan_bw_hz must be a number or 'auto'")
        st.controller.listen_chan_bw_hz = max(2000.0, min(bw, 20_000_000.0))
        return jsonify({"chan_bw_hz": st.controller.listen_chan_bw_hz})

    # ---------- monitor recordings (WAV; client-sensitive, gitignored) ----------

    def _recordings_dir():
        return cfg.abs_path(cfg.get("recordings", "dir", default="recordings"))

    @app.post("/api/record/start")
    def record_start():
        from .demod import pick_mode
        session = st.session()
        if not session:
            return err("no session")
        body = request.get_json(silent=True) or {}
        center_mhz = body.get("center_mhz")
        signal_id = body.get("signal_id")
        if center_mhz is None and signal_id:
            row = db.query_one("SELECT center_mhz FROM signals WHERE id=? AND session_id=?",
                               (signal_id, session["id"]))
            if row:
                center_mhz = row["center_mhz"]
        if center_mhz is None:
            return err("need center_mhz (or a signal_id)")
        center_mhz = float(center_mhz)
        mode = (body.get("mode") or pick_mode(center_mhz, body.get("label", ""))).lower()
        offset_hz = float(body.get("offset_hz") or 0.0)
        kind = (body.get("kind") or "audio").lower()
        if kind not in ("audio", "iq"):
            kind = "audio"
        try:
            rec = st.controller.start_recording(
                session["id"], center_mhz, _recordings_dir(),
                mode=mode, offset_hz=offset_hz, signal_id=signal_id, kind=kind)
        except RuntimeError as e:
            return err(e, 409)
        except Exception as e:  # noqa: BLE001
            log.exception("record start failed")
            return err(e, 502)
        return jsonify({"started": True, "center_mhz": center_mhz, "mode": mode,
                        "kind": kind, "file_path": rec["file_path"],
                        "started_utc": rec["started_utc"]})

    @app.post("/api/record/stop")
    def record_stop():
        rec = st.controller.stop_recording()
        rid = rec.get("recording_id")
        if not rid:
            return jsonify({"recording_id": None, "note": "no audio captured"})
        row = db.query_one("SELECT * FROM recordings WHERE id=?", (rid,))
        return jsonify(row or {"recording_id": rid})

    @app.get("/api/recordings")
    def recordings_list():
        session = st.session()
        if not session:
            return err("no session")
        sid = request.args.get("signal_id")
        return jsonify(db.list_recordings(session["id"], int(sid) if sid else None))

    @app.get("/api/recordings/<int:rec_id>/audio")
    def recording_audio(rec_id):
        session = st.session()
        if not session:
            return err("no session")
        row = db.query_one("SELECT * FROM recordings WHERE id=? AND session_id=?",
                           (rec_id, session["id"]))
        if not row:
            return err("recording not found in this session", 404)
        rec_dir = _recordings_dir()
        path = os.path.join(rec_dir, row["file_path"])
        # defence-in-depth: never serve outside the recordings dir
        if not os.path.realpath(path).startswith(os.path.realpath(rec_dir)):
            return err("invalid path", 400)
        if not os.path.exists(path):
            return err("recording file missing", 404)
        return send_file(path, mimetype="audio/wav", as_attachment=False,
                         download_name=os.path.basename(path))

    @app.post("/api/recordings/purge")
    def recordings_purge():
        """Per-engagement purge: MOVE this session's recording WAVs into an
        _archive/ subdir (Matt's never-delete rule) and re-point the rows. Clears
        client-sensitive audio out of the active recordings area without losing it."""
        import shutil
        session = st.session()
        if not session:
            return err("no session")
        body = request.get_json(silent=True) or {}
        sid = int(body.get("session_id") or session["id"])
        rec_dir = _recordings_dir()
        archive = os.path.join(rec_dir, "_archive", str(sid))
        os.makedirs(archive, exist_ok=True)
        moved = 0
        for r in db.list_recordings(sid):
            src = os.path.join(rec_dir, r["file_path"])
            if not os.path.exists(src):
                continue
            dst_rel = os.path.join("_archive", str(sid), os.path.basename(r["file_path"]))
            try:
                shutil.move(src, os.path.join(rec_dir, dst_rel))
                db.execute("UPDATE recordings SET file_path=?, notes=? WHERE id=?",
                           (dst_rel, "archived", r["id"]))
                moved += 1
            except Exception:  # noqa: BLE001
                log.exception("purge move failed for recording %s", r["id"])
        return jsonify({"moved": moved, "archive": archive})

    # ---------- planning & sweeping ----------

    def _plans(profile, custom_minutes, scan_mode="full", seg_width=None,
               custom_avg_passes=None, custom_maxhold_s=None):
        # auto-(re)connects and lazily reads any module range the connect-time
        # identity pass failed to capture; raises DeviceError if truly absent
        modules = st.controller.module_ranges()
        # a "bug" scan restricts coverage to the known surveillance bands; with
        # the same time budget that means far deeper dwell per band
        targets = bug_band_ranges() if scan_mode == "bug" else None
        pcfg = cfg
        if seg_width:                          # per-scan segment-width override
            try:
                w = float(seg_width)
                if w >= 2.0:                   # planner floors at 2.0 anyway
                    pcfg = _WidthCfg(cfg, w)
            except (TypeError, ValueError):
                pass
        avg_p = custom_avg_passes
        if avg_p is not None:
            try:
                avg_p = int(avg_p)
            except (TypeError, ValueError):
                avg_p = None
        mh = custom_maxhold_s
        if mh is not None:
            try:
                mh = float(mh)
            except (TypeError, ValueError):
                mh = None
        return plan_full_sweep(modules, pcfg, profile, custom_minutes, targets=targets,
                             custom_avg_passes=avg_p, custom_maxhold_s=mh)

    def _plan_rbw(plans, rbw_khz):
        """Annotate each plan dict with the REAL delivered RBW per segment (and
        the requested override): the connected device knows (SDR grid + FFT);
        the RFE falls back to the firmware span/sweep_points. Purely advisory —
        the UI 'RBW' readout was hardcoded to span/112 which is wrong for SDRs."""
        dev = st.controller.device
        out = {}
        for side, p in plans.items():
            d = p.to_dict()
            for s, seg in zip(d["segments"], p.segments):
                # pure preview — never mutates the device (a mutate-and-restore
                # here would briefly distort a LIVE sweep, which re-reads the
                # FFT size every frame while the operator types in the RBW box)
                pred = (dev.predicted_rbw_khz(seg.span_mhz, rbw_khz)
                        if dev is not None else None)
                s["rbw_khz"] = round(pred if pred else seg.rbw_khz(p.sweep_points), 1)
            d["rbw_adjustable"] = bool(dev is not None
                                       and getattr(dev, "is_swept_sdr", False))
            d["rbw_khz_requested"] = rbw_khz
            out[side] = d
        return out

    def _num_or_none(v):
        try:
            f = float(v)
            return f if f > 0 else None
        except (TypeError, ValueError):
            return None

    @app.get("/api/plan")
    def plan_preview():
        profile = request.args.get("profile", "standard")
        minutes = float(request.args.get("custom_minutes", 45))
        scan_mode = request.args.get("scan_mode", "full")
        seg = request.args.get("segment_width_mhz")
        rbw_khz = _num_or_none(request.args.get("rbw_khz"))
        try:
            plans = _plans(profile, minutes, scan_mode, seg,
                           request.args.get("custom_avg_passes"),
                           request.args.get("custom_maxhold_s"))
        except Exception as e:  # noqa: BLE001 - device absent/dead included
            return err(e)
        return jsonify(_plan_rbw(plans, rbw_khz))

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
        scan_mode = body.get("scan_mode", "full")        # full | bug
        seg = body.get("segment_width_mhz")               # optional segment-width override
        # operator RBW override (kHz, SDR only) — clamped to a sane field range
        rbw_khz = _num_or_none(body.get("rbw_khz"))
        if rbw_khz:
            rbw_khz = max(1.0, min(1000.0, rbw_khz))
        # F5: manual segment advance is a custom-mode-only behaviour
        manual_advance = bool(body.get("manual_advance")) and profile == "custom"
        try:
            plans = _plans(profile, minutes, scan_mode, seg,
                           body.get("custom_avg_passes"),
                           body.get("custom_maxhold_s"))
            plan = plans.get(module)
            # F4: single-RX devices (Pluto) report one module; if the requested
            # side has no plan but only one exists, fall back to it rather than erroring.
            if (plan is None or not plan.segments) and len(plans) == 1:
                module, plan = next(iter(plans.items()))
            if plan is None or not plan.segments:
                return err(f"no plan for module {module}")
            sweep_id = st.controller.start_sweep(session["id"], phase, module, room, plan,
                                                 manual_advance=manual_advance,
                                                 rbw_khz=rbw_khz)
        except Exception as e:  # noqa: BLE001 - busy, device dead, plan failure
            log.exception("sweep start failed")
            return err(e, 409)
        if st.controller.device:
            db.set_device_info(session["id"], st.controller.device.info.to_dict())
        return jsonify({"sweep_id": sweep_id, "plan": plan.to_dict()})

    @app.post("/api/sweep/abort")
    def sweep_abort():
        """F7: default = pause the radio, save everything. {'erase': true} is the
        confirmed hard abort — the aborted sweep's data is deleted."""
        body = request.get_json(force=True, silent=True) or {}
        erase = bool(body.get("erase"))
        st.controller.abort(erase=erase)
        return jsonify({"aborted": True, "erase": erase})

    @app.post("/api/sweep/next_segment")
    def sweep_next_segment():
        """F5: release the manual-advance gate so the sweep moves to the next segment."""
        st.controller.advance()
        return jsonify({"ok": True})

    @app.post("/api/sweep/sustain")
    def sweep_sustain():
        """F6: hold the current segment (keep re-sweeping it live) / release it."""
        body = request.get_json(force=True) or {}
        on = bool(body.get("on"))
        if on and not st.controller.state.get("running"):
            return err("no sweep running")
        st.controller.set_sustain(on)
        return jsonify({"ok": True, "sustain": on})

    _catalog_cache = {}

    @app.get("/api/catalog/segment")
    def catalog_segment():
        """F6: covert-device classes from the surveillance catalog whose signature
        bands intersect [low, high] MHz — segment-local intel for the operator."""
        try:
            low = float(request.args.get("low", 0))
            high = float(request.args.get("high", 0))
        except ValueError:
            return err("low/high must be numeric")
        if "data" not in _catalog_cache:
            path = os.path.join(os.path.dirname(__file__), "surveillance_catalog.json")
            try:
                with open(path, encoding="utf-8") as f:
                    _catalog_cache["data"] = json.load(f)
            except OSError:
                _catalog_cache["data"] = {}
        out = []
        for dc in (_catalog_cache["data"].get("device_classes") or []):
            hits = []
            for s in dc.get("signatures", []):
                try:
                    if float(s["freq_min_mhz"]) <= high and float(s["freq_max_mhz"]) >= low:
                        hits.append(s)
                except (KeyError, TypeError, ValueError):
                    continue
            if not hits:
                continue
            out.append({
                "device_class": dc.get("device_class", ""),
                "summary": (dc.get("summary") or "")[:280],
                "confirm_steps": (dc.get("confirm_steps") or [])[:3],
                "false_positive_overlap": (dc.get("false_positive_overlap") or [])[:4],
                "signatures": [{
                    "name": s.get("name") or s.get("signature") or "",
                    "freq_min_mhz": s.get("freq_min_mhz"),
                    "freq_max_mhz": s.get("freq_max_mhz"),
                    "bandwidth_khz": s.get("bandwidth_khz"),
                    "class": s.get("class"), "duty": s.get("duty"),
                } for s in hits[:8]],
            })
        out.sort(key=lambda d: -len(d["signatures"]))
        return jsonify({"low": low, "high": high, "classes": out})

    # ---------- results ----------

    @app.get("/api/signals")
    def signals():
        session = st.session()
        if not session:
            return err("no session")
        phase = request.args.get("phase")
        sweep_id = request.args.get("sweep_id")
        q = "SELECT * FROM signals WHERE session_id=?"
        params = [session["id"]]
        if phase:
            q += " AND phase=?"
            params.append(phase)
        if sweep_id:
            q += " AND sweep_id=?"
            params.append(int(sweep_id))
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
        return jsonify(_dedupe_emitters(rows))

    @app.get("/api/scan/unknowns")
    def scan_unknowns():
        """Ordered, de-duped 'hunt list': the interior signals NOT explained by the
        baseline (the SAME UNVERIFIED-or-caution predicate as /api/anomalies). The
        client tunes the live listen view to the first one and steps through the
        rest. Read-only — no radio, no DB write; run a sweep first to populate it."""
        session = st.session()
        if not session:
            return err("no session")
        rows = db.query(
            "SELECT * FROM signals WHERE session_id=? AND phase='interior' AND "
            "(classification='UNVERIFIED' OR caution=1) ORDER BY anomaly_score DESC",
            (session["id"],))
        return jsonify(_dedupe_emitters(rows))

    # ---------- gated MANUAL triage (post-hoc; no radio) ----------

    @app.get("/api/triage/last")
    def triage_last():
        """The gated MANUAL review list: the not-already-explained detections of
        the most recent interior sweep (or ?sweep_id=N), each with its triage
        decision + novelty. Read-only, no radio — works with only the RFE present
        and never fights a live sweep. The operator must decide every signal; the
        UI blocks completion until remaining_unexplained hits zero."""
        session = st.session()
        if not session:
            return err("no session")
        sweep_id = request.args.get("sweep_id")
        if sweep_id:
            sweep = db.query_one("SELECT * FROM sweeps WHERE id=? AND session_id=?",
                                 (int(sweep_id), session["id"]))
        else:
            sweep = db.query_one(
                "SELECT * FROM sweeps WHERE session_id=? AND phase='interior' "
                "AND status IN ('done','aborted') ORDER BY started_utc DESC LIMIT 1",
                (session["id"],))
        if not sweep:
            return jsonify({"sweep_id": None, "phase": None, "signals": [],
                            "counts": {}, "remaining_unexplained": 0})
        rows = db.query(
            "SELECT id, center_mhz, bw_khz, peak_dbm, persistence, classification, "
            "matched_label, match_reason, caution, anomaly_score, room, module, "
            "is_novel_to_address, novelty_reason, triage_status, triage_notes, "
            "attribution_status, attribution_notes "
            "FROM signals WHERE sweep_id=? AND (classification!='VERIFIED' OR caution=1) "
            "ORDER BY anomaly_score DESC, center_mhz", (sweep["id"],))
        counts = {}
        for r in rows:
            stt = r["triage_status"] or "unreviewed"
            counts[stt] = counts.get(stt, 0) + 1
        remaining = sum(1 for r in rows
                        if (r["triage_status"] or "unreviewed") == "unreviewed")
        return jsonify({"sweep_id": sweep["id"], "phase": sweep["phase"],
                        "signals": rows, "counts": counts,
                        "remaining_unexplained": remaining})

    @app.post("/api/signals/<int:signal_id>/triage")
    def signal_triage(signal_id):
        session = st.session()
        if not session:
            return err("no session")
        body = request.get_json(force=True)
        status = (body.get("status") or "").strip()
        if status not in TRIAGE_DECISIONS:
            return err(f"status must be one of {', '.join(TRIAGE_DECISIONS)}")
        exists = db.query_one("SELECT id FROM signals WHERE id=? AND session_id=?",
                              (signal_id, session["id"]))
        if not exists:
            return err("signal not found in this session", 404)
        row = db.set_signal_triage(signal_id, session["id"], status,
                                   body.get("notes", ""))
        return jsonify(row)

    @app.post("/api/signals/<int:signal_id>/attribution")
    def signal_attribution(signal_id):
        """Record whether this detection captures the CLIENT'S own environment.
        client_space => a bug targeting the client (escalate + IQ evidence).
        third_party / infrastructure => do NOT decode content — move along."""
        session = st.session()
        if not session:
            return err("no session")
        body = request.get_json(force=True)
        status = (body.get("status") or "").strip()
        if status not in ATTRIBUTION_STATUSES:
            return err(f"status must be one of {', '.join(ATTRIBUTION_STATUSES)}")
        exists = db.query_one("SELECT id FROM signals WHERE id=? AND session_id=?",
                              (signal_id, session["id"]))
        if not exists:
            return err("signal not found in this session", 404)
        row = db.set_signal_attribution(signal_id, session["id"], status,
                                        body.get("notes", ""))
        return jsonify(row)

    @app.post("/api/decode/override")
    def decode_override():
        """Log an ECPA force-decode override. Decoding the CONTENT of a signal that
        is NOT attributed to the client is potentially unlawful interception
        (18 U.S.C. 2511); the operator may proceed at their own documented
        discretion, but ONLY with an explicit acknowledgment recorded here. Requires
        ecpa_ack=true + a reason. Returns the immutable audit row."""
        session = st.session()
        if not session:
            return err("no session")
        body = request.get_json(force=True)
        if not body.get("ecpa_ack"):
            return err("ecpa_ack (acknowledgment of the ECPA warning) is required", 400)
        reason = (body.get("reason") or "").strip()
        if not reason:
            return err("a reason for the override is required for the audit log", 400)
        row = db.add_decode_override(
            session["id"], body.get("signal_id"), body.get("center_mhz"),
            (body.get("mode") or "").strip(),
            (body.get("operator") or session.get("operator") or "").strip(), reason)
        return jsonify(row)

    @app.get("/api/decode/overrides")
    def decode_overrides_list():
        """The ECPA override audit log for this session (internal only)."""
        session = st.session()
        if not session:
            return err("no session")
        return jsonify(db.list_decode_overrides(session["id"]))

    @app.post("/api/triage/bulk")
    def triage_bulk():
        """Apply one triage decision to many signals (filtered client-side)."""
        session = st.session()
        if not session:
            return err("no session")
        body = request.get_json(force=True)
        ids = body.get("signal_ids") or []
        if not isinstance(ids, list) or not ids:
            return err("signal_ids required (non-empty list)")
        status = (body.get("status") or "").strip()
        if status not in TRIAGE_DECISIONS:
            return err(f"status must be one of {', '.join(TRIAGE_DECISIONS)}")
        notes = body.get("notes", "")
        # reject IDs outside this session (don't leak existence of other sessions)
        placeholders = ",".join("?" * len(ids))
        owned = db.query(
            f"SELECT id FROM signals WHERE session_id=? AND id IN ({placeholders})",
            (session["id"], *ids))
        owned_ids = [r["id"] for r in owned]
        if not owned_ids:
            return err("no matching signals in this session", 404)
        updated = db.bulk_set_signal_triage(owned_ids, session["id"], status, notes)
        return jsonify({"updated": updated, "status": status})

    @app.get("/api/novelty")
    def novelty_lookup():
        """New-to-environment lookup for a signal/frequency vs this address's
        PRIOR sweep history (Signal Wiki). Read-only, no radio."""
        from .novelty import compute_novelty
        session = st.session()
        if not session:
            return err("no session")
        cur_peak = cur_bw = None
        signal_id = request.args.get("signal_id")
        if signal_id:
            sig = db.query_one("SELECT center_mhz, peak_dbm, bw_khz FROM signals "
                               "WHERE id=? AND session_id=?",
                               (int(signal_id), session["id"]))
            if not sig:
                return err("signal not found in this session", 404)
            center, cur_peak, cur_bw = sig["center_mhz"], sig["peak_dbm"], sig["bw_khz"]
        else:
            center_arg = request.args.get("center_mhz")
            if not center_arg:
                return err("center_mhz or signal_id required")
            center = float(center_arg)
        tol = float(request.args.get("tol_mhz") or 0.25)
        nov = compute_novelty(db, session["address"], center, session["created_utc"],
                              tol_mhz=tol, cur_peak_dbm=cur_peak, cur_bw_khz=cur_bw)
        nov["address"] = session["address"]
        return jsonify(nov)

    @app.get("/api/expected")
    def expected():
        from .expected import expected_vs_found
        session = st.session()
        if not session:
            return err("no session")
        return jsonify(expected_vs_found(db, session["id"]))

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

    # ---------- phase-interferometry DF (B220 dual coherent RX) ----------
    # Receive-only, so not demo-blocked (emanations precedent). Live telemetry
    # rides out through /api/state -> sweep.df; stop uses a DEDICATED event so
    # it can never abort an unrelated sweep.

    @app.post("/api/df/start")
    def df_start():
        session = st.session()
        if not session:
            return err("no session")
        body = request.get_json(force=True)
        try:
            st.controller.start_df(
                session["id"], float(body["center_mhz"]),
                float(body.get("bw_khz") or 100.0), body.get("room", ""),
                spacing_m=(float(body["spacing_m"])
                           if body.get("spacing_m") else None))
        except (RuntimeError, KeyError, ValueError) as e:
            return err(e, 409)
        return jsonify({"started": True})

    @app.post("/api/df/stop")
    def df_stop():
        st.controller.stop_df()
        return jsonify({"stopped": True})

    @app.post("/api/df/calibrate")
    def df_calibrate():
        try:
            st.controller.df_calibrate()
        except RuntimeError as e:
            return err(e, 409)
        return jsonify({"calibrating": True})

    @app.get("/api/df/log")
    def df_log():
        session = st.session()
        if not session:
            return err("no session")
        return jsonify(db.query("SELECT * FROM df_log WHERE session_id=? "
                                "ORDER BY ts_utc", (session["id"],)))

    # ---------- community / Discord ----------

    def _sanitized_anomaly_lines(session_id, limit=15):
        """Anomaly summary safe to share outside the engagement: frequencies and
        signal characteristics ONLY — never the address, client, operator, or
        room names."""
        rows = db.query(
            "SELECT * FROM signals WHERE session_id=? AND phase='interior' AND "
            "(classification='UNVERIFIED' OR caution=1) ORDER BY anomaly_score DESC",
            (session_id,))
        kept = []
        for r in rows:
            if not any(abs(r["center_mhz"] - k["center_mhz"]) <= 0.25 for k in kept):
                kept.append(r)
        lines = []
        for r in kept[:limit]:
            f = f"{r['center_mhz']:.4f} MHz" if r["center_mhz"] < 1000 \
                else f"{r['center_mhz'] / 1000:.4f} GHz"
            lines.append(
                f"• {f} — peak {r['peak_dbm']:.0f} dBm, bw ~{(r['bw_khz'] or 0):.0f} kHz, "
                f"persistence {(r['persistence'] or 0) * 100:.0f}%, {r['classification']}"
                + (f" ({(r['match_reason'] or '')[:90]})" if r["match_reason"] else ""))
        return lines, len(kept)

    @app.get("/api/discord/preview")
    def discord_preview():
        session = st.session()
        if not session:
            return err("no session")
        lines, total = _sanitized_anomaly_lines(session["id"])
        configured = bool(cfg.get("integrations", "discord_webhook", default=""))
        return jsonify({"lines": lines, "total": total, "configured": configured})

    @app.post("/api/discord/post")
    def discord_post():
        import requests as _rq
        session = st.session()
        if not session:
            return err("no session")
        webhook = cfg.get("integrations", "discord_webhook", default="")
        if not webhook:
            return err("No Discord webhook configured — create one in your TSCM "
                       "community server (channel settings → Integrations → Webhooks) "
                       "and set integrations.discord_webhook in config.yaml")
        body = request.get_json(force=True)
        lines, total = _sanitized_anomaly_lines(session["id"])
        if not lines:
            return err("no anomalies to post")
        note = (body.get("note") or "").strip()[:300]
        content = ("**Signal ID help requested** (sweep details withheld)\n"
                   + (note + "\n" if note else "")
                   + "\n".join(lines)
                   + (f"\n…and {total - len(lines)} more." if total > len(lines) else ""))
        try:
            r = _rq.post(webhook, json={"content": content[:1900],
                                        "username": "Shield Spectrum Survey"},
                         timeout=20)
            r.raise_for_status()
        except Exception as e:  # noqa: BLE001
            return err(f"Discord post failed: {e}", 502)
        return jsonify({"posted": True, "anomalies": len(lines)})

    # ---------- wireless inventory ----------

    @app.post("/api/inventory/scan")
    def inventory_scan():
        from .inventory import scan_ble, scan_wifi, upsert_inventory
        session = st.session()
        if not session:
            return err("create a session first")
        kind = (request.get_json(force=True).get("kind") or "wifi").lower()
        devices, message = scan_ble() if kind == "ble" else scan_wifi()
        if devices:
            upsert_inventory(db, session["id"], devices)
        return jsonify({"kind": kind, "found": len(devices), "message": message})

    @app.get("/api/inventory")
    def inventory_get():
        from .inventory import inventory_rows
        session = st.session()
        if not session:
            return err("no session")
        return jsonify(inventory_rows(db, session["id"]))

    @app.post("/api/inventory/mark")
    def inventory_mark():
        body = request.get_json(force=True)
        db.execute("UPDATE inventory SET owned=? WHERE id=?",
                   (int(bool(body.get("owned"))), body.get("id")))
        return jsonify({"saved": True})

    @app.post("/api/ble/scan")
    def ble_scan_detail():
        """Standalone BLE sweep: every advertiser with the full survey.ble dossier
        (MAC address type, vendor, decoded manufacturer/service data, tracker flag,
        plain-language summary). Trackers first, then loudest. Not stored — a live
        look; use /api/inventory/scan kind=ble to persist + mark ownership."""
        seconds = float((request.get_json(silent=True) or {}).get("seconds") or 8.0)
        from .inventory import scan_ble_details
        records, message = scan_ble_details(min(max(seconds, 2.0), 30.0))
        return jsonify({"count": len(records), "message": message,
                        "trackers": sum(1 for r in records if r["is_tracker"]),
                        "devices": records})

    @app.post("/api/stimulate/probe")
    def stimulate_probe():
        """Active-stimulation probe: transmit a swept stimulus across a band and look
        for a device that REACTS. TRANSMITS — demo-blocked, refuses unauthorized bands
        unless override_band is acknowledged. HARDWARE-UNTESTED end-to-end; see
        STIMULATE_HARDWARE_TODO.md before field use.

        Two rigs (config `stimulate.setup`):
          * "single" (default) — one 2TX/2RX B220: TX/RX port out, RX2 port in.
          * "split"  — TX on one radio, RX on another (e.g. Pluto TX + B220 RX) for the
            best isolation. Set `stimulate.tx_source` / `stimulate.rx_source`."""
        import contextlib
        from .stimulate import SplitStimRadio, StimulationEngine
        body = request.get_json(silent=True) or {}
        center = body.get("center_mhz")
        lo = body.get("band_lo_mhz")
        hi = body.get("band_hi_mhz")
        if center is None or lo is None or hi is None:
            return err("need center_mhz, band_lo_mhz, band_hi_mhz")

        setup = str(cfg.get("stimulate", "setup", default="single")).lower()
        locks = []
        if setup == "split":
            tx_src = str(cfg.get("stimulate", "tx_source", default="pluto")).lower()
            rx_src = str(cfg.get("stimulate", "rx_source", default="b220")).lower()
            devs = st.controller.devices
            tx, rx = devs.get(tx_src), devs.get(rx_src)
            if tx is None or rx is None:
                return err(f"split stimulation needs BOTH radios connected — TX={tx_src}, "
                           f"RX={rx_src}. Connect both in the DEVICE card (keep-other).", 409)
            if not hasattr(tx, "start_tx") or not hasattr(rx, "capture_iq"):
                return err(f"{tx_src} can't transmit or {rx_src} can't capture IQ for a "
                           "split rig — check stimulate.tx_source/rx_source", 409)
            if st.controller.busy(tx_src) or st.controller.busy(rx_src):
                return err("a radio is busy — stop the sweep/monitor first", 409)
            radio = SplitStimRadio(cfg, tx, rx)
            for s in (tx_src, rx_src):
                lk = st.controller.device_locks.get(s)
                if lk is not None:
                    locks.append(lk)
        else:
            dev, mlock, mstop, msrc = st.controller.monitor_target()
            if dev is None or not hasattr(dev, "stim_mode"):
                return err("active radio can't transmit — connect a 2TX/2RX radio (B220), "
                           "or set stimulate.setup=split for a Pluto-TX + B220-RX rig", 409)
            if st.controller.busy(msrc):
                return err("radio is busy — stop the current sweep/monitor first", 409)
            radio, mstop_ = dev, mstop
            mstop_.set()
            if mlock is not None:
                locks.append(mlock)
        try:
            eng = StimulationEngine(cfg, radio)
            with contextlib.ExitStack() as stack:
                for lk in locks:
                    stack.enter_context(lk)
                res = eng.probe(float(center) * 1e6, float(lo) * 1e6, float(hi) * 1e6,
                                override_band=bool(body.get("override_band")))
        except Exception as e:  # noqa: BLE001
            log.exception("stimulation probe failed")
            return err(f"stimulation failed: {e}", 500)
        return jsonify(res.to_dict())

    @app.post("/api/emanations/scan")
    def emanations_scan():
        """Passive emanation scan: hunt a hidden device by the clock harmonics it
        LEAKS (a comb of evenly-spaced tones) — catches silent recorders that never
        transmit and won't react to a ping. Receive-only; needs an SDR (raw IQ)."""
        from .emanations import EmanationScanner
        body = request.get_json(silent=True) or {}
        lo, hi = body.get("band_lo_mhz"), body.get("band_hi_mhz")
        if lo is None or hi is None:
            return err("need band_lo_mhz, band_hi_mhz")
        dev, mlock, mstop, msrc = st.controller.monitor_target()
        is_sdr = dev is not None and hasattr(dev, "capture_iq") and \
            getattr(dev, "is_swept_sdr", False)
        if not is_sdr:
            return err("emanation scanning needs raw IQ — connect an SDR (Pluto or "
                       "B220); the RF Explorer can't do this", 409)
        if st.controller.busy(msrc):
            return err("radio is busy — stop the current sweep/monitor first", 409)
        try:
            eng = EmanationScanner(cfg, dev)
            mstop.set()
            with mlock:
                res = eng.scan(float(lo) * 1e6, float(hi) * 1e6)
        except Exception as e:  # noqa: BLE001
            log.exception("emanation scan failed")
            return err(f"emanation scan failed: {e}", 500)
        return jsonify(res.to_dict())

    # ---------- signal wiki (captures) ----------

    @app.post("/api/captures/start")
    def captures_start():
        """The wiki is session-independent: attribution comes from the target
        signal's/capture's own session, so no session needs to be 'open'.
        Only capture-all-anomalies is inherently about the current session."""
        body = request.get_json(force=True)
        session_id = None
        if body.get("recapture"):
            row = db.query_one("SELECT * FROM captures WHERE id=?", (body["recapture"],))
            if not row:
                return err("no such capture", 404)
            session_id = row["session_id"]
            targets = [{"id": None, "center_mhz": row["center_mhz"],
                        "bw_khz": row["bw_khz"], "room": "recapture",
                        "classification": row["classification"],
                        "matched_label": row["matched_label"],
                        "persistence": row["persistence"], "caution": 0}]
        elif body.get("all_anomalies"):
            session = st.session()
            if not session:
                return err("open a session first for capture-all (it is scoped to "
                           "the current sweep)")
            session_id = session["id"]
            rows = db.query(
                "SELECT * FROM signals WHERE session_id=? AND phase='interior' AND "
                "(classification='UNVERIFIED' OR caution=1) ORDER BY anomaly_score DESC",
                (session_id,))
            targets = []
            for r in rows:
                if not any(abs(r["center_mhz"] - k["center_mhz"]) <= 0.25 for k in targets):
                    targets.append(r)
        else:
            ids = body.get("signal_ids") or []
            targets = db.query(
                f"SELECT * FROM signals WHERE id IN ({','.join('?' * len(ids))})",
                ids) if ids else []
            if targets:
                session_id = targets[0]["session_id"]
        if not targets:
            return err("no capture targets")
        sess = db.query_one("SELECT address FROM sessions WHERE id=?", (session_id,))
        try:
            st.controller.start_captures(session_id, targets,
                                         cfg.abs_path("capture_images"),
                                         sess["address"] if sess else "")
        except Exception as e:  # noqa: BLE001
            return err(e, 409)
        return jsonify({"started": True, "targets": len(targets)})

    @app.get("/api/captures")
    def captures_list():
        # Default view groups the library by emitter (one row per frequency,
        # newest observation) so hundreds of auto-archived observations collapse
        # into the distinct signals the analyst cares about. ?flat=1 lists every
        # individual observation/zoom.
        q = "SELECT c.id, c.session_id, c.kind, c.center_mhz, c.bw_khz, c.peak_dbm, "\
            "c.room, c.module, c.captured_utc, c.image_path, c.category, c.label, "\
            "c.status, s.address FROM captures c JOIN sessions s ON c.session_id=s.id"
        params, where = [], []
        if request.args.get("category"):
            where.append("c.category=?")
            params.append(request.args["category"])
        if request.args.get("q"):
            where.append("(c.label LIKE ? OR c.notes LIKE ? OR c.description LIKE ?)")
            like = f"%{request.args['q']}%"
            params += [like, like, like]
        if request.args.get("kind"):
            where.append("c.kind=?")
            params.append(request.args["kind"])
        if where:
            q += " WHERE " + " AND ".join(where)
        q += " ORDER BY c.captured_utc DESC"
        rows = db.query(q, params)
        cats = db.query("SELECT category, COUNT(*) AS n FROM captures "
                        "GROUP BY category ORDER BY n DESC")
        if request.args.get("flat"):
            return jsonify({"rows": rows, "categories": cats, "grouped": False})
        # collapse by frequency (0.25 MHz buckets), keep newest + sighting count
        groups = []
        for r in rows:
            g = next((x for x in groups if abs(x["center_mhz"] - r["center_mhz"]) <= 0.25), None)
            if g is None:
                r = dict(r)
                r["sightings"] = 1
                r["sessions_seen"] = {r["session_id"]}
                groups.append(r)
            else:
                g["sightings"] += 1
                g["sessions_seen"].add(r["session_id"])
        for g in groups:
            g["sessions_seen"] = len(g["sessions_seen"])
        return jsonify({"rows": groups, "categories": cats, "grouped": True})

    @app.get("/api/signal_timeline")
    def signal_timeline_route():
        from .sigcapture import signal_timeline
        center = float(request.args.get("center_mhz", 0))
        return jsonify(signal_timeline(db, center))

    @app.get("/api/captures/<int:cap_id>")
    def capture_get(cap_id):
        from .sigcapture import find_matches
        row = db.query_one(
            "SELECT c.*, s.address FROM captures c JOIN sessions s ON "
            "c.session_id=s.id WHERE c.id=?", (cap_id,))
        if not row:
            return err("no such capture", 404)
        row["matches"] = find_matches(db, row)
        row.pop("data_json", None)   # raw data via dedicated param only
        return jsonify(row)

    @app.post("/api/captures/<int:cap_id>")
    def capture_update(cap_id):
        body = request.get_json(force=True)
        for field in ("label", "category", "status", "notes"):
            if field in body:
                db.execute(f"UPDATE captures SET {field}=? WHERE id=?",
                           (body[field], cap_id))
        return jsonify({"saved": True})

    @app.post("/api/captures/<int:cap_id>/describe")
    def capture_describe(cap_id):
        from .sigcapture import ai_description, rule_based_description, signal_features
        row = db.query_one("SELECT * FROM captures WHERE id=?", (cap_id,))
        if not row:
            return err("no such capture", 404)
        feats = json.loads(row["features_json"] or "{}")
        text, ai_err = ai_description(cfg, row, feats)
        if not text:
            return err(ai_err or "no description produced", 502)
        db.execute("UPDATE captures SET description=? WHERE id=?", (text, cap_id))
        return jsonify({"description": text})

    @app.get("/capture_images/<path:name>")
    def capture_image(name):
        return send_from_directory(cfg.abs_path("capture_images"), name)

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

    @app.get("/api/report_options")
    def report_options_get():
        from .report import DEFAULT_REPORT_OPTIONS
        session = st.session()
        if not session:
            return err("no session")
        row = db.query_one("SELECT options_json FROM report_options WHERE session_id=?",
                           (session["id"],))
        opts = dict(DEFAULT_REPORT_OPTIONS)
        if row and row["options_json"]:
            try:
                opts.update(json.loads(row["options_json"]))
            except (TypeError, ValueError):
                pass
        return jsonify(opts)

    @app.post("/api/report_options")
    def report_options_save():
        from .report import DEFAULT_REPORT_OPTIONS
        session = st.session()
        if not session:
            return err("no session")
        body = request.get_json(force=True)
        opts = {k: bool(body.get(k, False)) for k in DEFAULT_REPORT_OPTIONS}
        db.execute(
            "INSERT INTO report_options (session_id,options_json,updated_utc) VALUES (?,?,?) "
            "ON CONFLICT(session_id) DO UPDATE SET options_json=excluded.options_json,"
            "updated_utc=excluded.updated_utc",
            (session["id"], json.dumps(opts), time.time()))
        return jsonify({"saved": True, "options": opts})

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
