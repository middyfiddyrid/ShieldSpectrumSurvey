"""Flask server — serves the single-page ops console and the JSON API."""

import json
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
from .allocations import bug_band_ranges
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

    def err(message, code=400):
        return jsonify({"error": str(message)}), code

    # In the PUBLIC DEMO, every feature that touches real client PII is blocked
    # at the server, not merely hidden in the UI: Persons of Interest, the
    # internal memo, the multi-AI threat analysis, and case-file upload/serve.
    DEMO_BLOCKED = ("/api/poi", "/api/report/internal_memo", "/api/analyze",
                    "/api/case_files", "/case_files")

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
        return jsonify({
            "session": session,
            "source": (st.controller.active_source
                       or os.environ.get("SWEEP_SOURCE")
                       or cfg.get("device", "source", default="rfexplorer")),
            "device": st.controller.device_info(),
            "sweep": st.controller.state,
            "sweeps": sweeps,
            "baseline": st.baseline_status,
            "calibration": st.calibration_status,
            "busy": st.controller.busy(),
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
        try:
            info = st.controller.connect_device(source=source)
        except Exception as e:  # noqa: BLE001
            return err(e, 502)
        # remember the operator's pick so the next launch defaults to it
        if source:
            try:
                cfg.set_and_save(("device", "source"), source)
            except Exception:  # noqa: BLE001
                log.exception("could not persist device.source")
        if st.session_id:
            db.set_device_info(st.session_id, info.to_dict())
        return jsonify(info.to_dict())

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
            note = ("connect the Pluto (SDR) for modulation-based identification — "
                    "matched on frequency/bandwidth only")
        candidates = identify(center_mhz * 1e6, feats, ref)
        return jsonify({"center_mhz": center_mhz, "bw_khz": bw_khz,
                        "source": st.controller.active_source, "features": feats,
                        "note": note, "candidates": candidates, "spectrogram": spec})

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

        dev = st.controller.device
        is_sdr = dev is not None and hasattr(dev, "capture_iq") and \
            getattr(dev, "is_swept_sdr", False)
        base = {"center_mhz": center_mhz, "label": label, "mode": mode, "in_range": False}
        if not is_sdr:
            return jsonify({**base, "note": "connect the Pluto (SDR) to tune, view and listen"})
        if st.controller.busy():
            return jsonify({**base, "note": "a sweep is running — stop it to tune & listen"})
        mods = dev.info.modules
        lo_mhz = min(m.min_mhz for m in mods)
        hi_mhz = max(m.max_mhz for m in mods)
        if not (lo_mhz <= center_mhz <= hi_mhz):
            return jsonify({**base, "note": f"{center_mhz:.3f} MHz is outside the radio's "
                            f"{lo_mhz:.0f}-{hi_mhz:.0f} MHz range — apply the AD9364 extension to reach it"})
        try:
            fs = float(cfg.get("pluto", "listen_sample_rate", default=2_400_000))
            n = int(fs * float(cfg.get("pluto", "listen_seconds", default=0.5)))
            iq = dev.capture_iq(center_mhz * 1e6, fs, n)
        except Exception as e:  # noqa: BLE001
            log.exception("inspect capture failed")
            return jsonify({**base, "note": f"capture failed: {e}"})

        cal = float(cfg.get("pluto", "cal_offset_db", default=-30.0))
        bb, psd_db = welch_psd(iq, fs, nperseg=2048)
        step = max(1, bb.size // 480)
        analyzer = {"freqs_mhz": [round((center_mhz * 1e6 + f) / 1e6, 5) for f in bb[::step]],
                    "power_dbm": [round(float(p) + cal, 1) for p in psd_db[::step]]}
        wf = spectrogram(iq, fs, n_time=90, nperseg=200)
        waterfall = {"freqs_mhz": [round((center_mhz * 1e6 + f) / 1e6, 4) for f in wf["freqs_hz"]],
                     "db": wf["db"], "vmin": wf["vmin"], "vmax": wf["vmax"]}
        audio = None
        try:
            pcm, arate = demodulate(iq, fs, mode)
            audio = "data:audio/wav;base64," + base64.b64encode(wav_bytes(pcm, arate)).decode()
        except Exception:  # noqa: BLE001
            log.exception("demod failed")
        return jsonify({**base, "in_range": True, "analyzer": analyzer,
                        "waterfall": waterfall, "audio": audio, "note": ""})

    # ---------- planning & sweeping ----------

    def _plans(profile, custom_minutes, scan_mode="full"):
        # auto-(re)connects and lazily reads any module range the connect-time
        # identity pass failed to capture; raises DeviceError if truly absent
        modules = st.controller.module_ranges()
        # a "bug" scan restricts coverage to the known surveillance bands; with
        # the same time budget that means far deeper dwell per band
        targets = bug_band_ranges() if scan_mode == "bug" else None
        return plan_full_sweep(modules, cfg, profile, custom_minutes, targets=targets)

    @app.get("/api/plan")
    def plan_preview():
        profile = request.args.get("profile", "standard")
        minutes = float(request.args.get("custom_minutes", 45))
        scan_mode = request.args.get("scan_mode", "full")
        try:
            plans = _plans(profile, minutes, scan_mode)
        except Exception as e:  # noqa: BLE001 - device absent/dead included
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
        scan_mode = body.get("scan_mode", "full")        # full | bug
        try:
            plans = _plans(profile, minutes, scan_mode)
            plan = plans.get(module)
            if plan is None or not plan.segments:
                return err(f"no plan for module {module}")
            sweep_id = st.controller.start_sweep(session["id"], phase, module, room, plan)
        except Exception as e:  # noqa: BLE001 - busy, device dead, plan failure
            log.exception("sweep start failed")
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
        # one row per physical signal: the same emitter detected in several room
        # sweeps keeps only its highest-scoring instance (room grid shows the rest)
        kept = []
        for r in rows:
            if not any(abs(r["center_mhz"] - k["center_mhz"]) <= 0.25 for k in kept):
                kept.append(r)
        return jsonify(kept)

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
