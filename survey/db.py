"""SQLite session archive. One database file holds every sweep session:
address, baseline data, raw sweep accumulators, detected signals, anomaly
classifications, directional readings, and generated reports.
"""

import json
import os
import sqlite3
import threading
import time

_LOCK = threading.Lock()

# Valid per-detection triage decisions for gated MANUAL review.
# 'unreviewed' is the default state; the rest are operator decisions.
TRIAGE_STATUSES = ("unreviewed", "cleared", "suspect", "flagged", "ask_client")
TRIAGE_DECISIONS = ("cleared", "suspect", "flagged", "ask_client")
# Attribution: is a detection capturing THE CLIENT'S OWN environment (their space,
# their conversations)? That is the legal line for demodulating content (ECPA safe
# harbor = premises-owner consent). third_party/infrastructure => do NOT decode content.
ATTRIBUTION_STATUSES = ("unassessed", "client_space", "third_party",
                        "infrastructure", "inconclusive")

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY,
    created_utc REAL NOT NULL,
    address TEXT NOT NULL,
    lat REAL, lon REAL,
    geocoder TEXT,
    radius_broadcast_mi REAL, radius_lmr_mi REAL,
    operator TEXT,
    status TEXT DEFAULT 'open',
    device_info TEXT,            -- JSON: model, firmware, module ranges as reported
    notes TEXT,
    kind TEXT DEFAULT 'field'    -- field = real client engagement (report/portal
                                 -- eligible) | lab = bench/test work, NEVER
                                 -- delivered to a client
);
CREATE TABLE IF NOT EXISTS source_status (
    session_id INTEGER, source TEXT, status TEXT, count INTEGER,
    fetched_utc REAL, message TEXT,
    PRIMARY KEY (session_id, source)
);
CREATE TABLE IF NOT EXISTS baseline (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    freq_low_mhz REAL NOT NULL, freq_high_mhz REAL NOT NULL,
    service TEXT, label TEXT, callsign TEXT, licensee TEXT,
    erp TEXT, distance_mi REAL, bearing_deg REAL,
    confidence TEXT, channel_bw_khz REAL, category TEXT,
    caution INTEGER DEFAULT 0, uplink INTEGER DEFAULT 0,
    notes TEXT, raw_json TEXT
);
CREATE TABLE IF NOT EXISTS sweeps (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL,
    phase TEXT NOT NULL,         -- ambient | interior | calibration | directional
    module TEXT,                 -- left | right | single
    room TEXT,
    profile TEXT,
    started_utc REAL, finished_utc REAL,
    range_low_mhz REAL, range_high_mhz REAL,
    status TEXT DEFAULT 'running',   -- running | done | aborted | error
    simulated INTEGER DEFAULT 0,
    meta TEXT
);
CREATE TABLE IF NOT EXISTS segments (
    id INTEGER PRIMARY KEY,
    sweep_id INTEGER NOT NULL,
    low_mhz REAL, high_mhz REAL,
    noise_floor_dbm REAL,
    avg_passes INTEGER, maxhold_dwell_s REAL,
    status TEXT DEFAULT 'pending',
    data_json TEXT               -- JSON: freqs[], avg[], maxpeak[], hits[], passes
);
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY,
    sweep_id INTEGER NOT NULL,
    session_id INTEGER NOT NULL,
    segment_id INTEGER,
    room TEXT, module TEXT, phase TEXT,
    center_mhz REAL NOT NULL, bw_khz REAL,
    peak_dbm REAL, avg_dbm REAL, floor_dbm REAL,
    persistence REAL,            -- fraction of passes the bin was hot
    classification TEXT,         -- VERIFIED | PLAUSIBLE | UNVERIFIED
    matched_label TEXT, match_reason TEXT,
    caution INTEGER DEFAULT 0,
    anomaly_score REAL DEFAULT 0,
    interior_delta_db REAL,      -- interior minus ambient amplitude (NULL until ambient exists)
    localization_score REAL, strongest_room TEXT,
    detected_utc REAL            -- when this detection was recorded (report date)
);
CREATE TABLE IF NOT EXISTS directional (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL,
    center_mhz REAL NOT NULL,
    room TEXT, peak_dbm REAL, notes TEXT, ts_utc REAL
);
CREATE TABLE IF NOT EXISTS inventory (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL,
    kind TEXT NOT NULL,            -- wifi | ble
    identifier TEXT NOT NULL,      -- BSSID / MAC
    name TEXT, rssi_dbm REAL, channel TEXT, freq_mhz REAL, extra TEXT,
    owned INTEGER DEFAULT 0,       -- operator: client accounts for this device
    first_seen REAL, last_seen REAL,
    UNIQUE(session_id, kind, identifier)
);
CREATE TABLE IF NOT EXISTS captures (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL,
    signal_id INTEGER,           -- originating detection, if any
    kind TEXT DEFAULT 'zoom',    -- zoom (high-iteration manual) | observation (auto-archived per sweep)
    center_mhz REAL NOT NULL, span_mhz REAL, bw_khz REAL,
    peak_dbm REAL, floor_dbm REAL, persistence REAL,
    room TEXT, module TEXT,
    captured_utc REAL,
    image_path TEXT,             -- SVG fingerprint
    data_json TEXT,              -- raw accumulator (freqs/avg/maxpeak/hits/passes)
    features_json TEXT,          -- numeric shape profile
    classification TEXT, matched_label TEXT,
    category TEXT,               -- wiki section (editable)
    label TEXT,                  -- wiki title (editable)
    status TEXT DEFAULT 'unconfirmed',   -- unconfirmed | identified | threat
    notes TEXT DEFAULT '',
    description TEXT DEFAULT ''  -- auto/AI description
);
CREATE INDEX IF NOT EXISTS idx_captures_freq ON captures(center_mhz);
CREATE TABLE IF NOT EXISTS report_options (
    session_id INTEGER PRIMARY KEY,
    options_json TEXT, updated_utc REAL
);
CREATE TABLE IF NOT EXISTS engagement (
    session_id INTEGER PRIMARY KEY,
    engagement_type TEXT DEFAULT 'onsite',   -- onsite | remote | monitoring
    client_name TEXT DEFAULT '', client_contact TEXT DEFAULT '',
    client_concerns TEXT DEFAULT '',          -- the client's OWN stated concerns (client-facing)
    offsite_location TEXT DEFAULT '',          -- neutral intake-meeting venue (client-facing)
    scheduling_note TEXT DEFAULT '',           -- availability / time-on-site (client-facing)
    status TEXT DEFAULT 'intake',
    updated_utc REAL
);
CREATE TABLE IF NOT EXISTS tech_tasks (
    id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL,
    text TEXT NOT NULL, done INTEGER DEFAULT 0, created_utc REAL
);
CREATE TABLE IF NOT EXISTS persons_of_interest (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL,
    name TEXT NOT NULL, aliases TEXT DEFAULT '',
    relationship TEXT DEFAULT '',      -- to the client (ex-spouse, partner, neighbor…)
    source TEXT DEFAULT '',            -- where the assessment facts came from (client, court record…)
    motive TEXT DEFAULT '',            -- client-stated dispute / reason
    access_note TEXT DEFAULT '',       -- could they physically reach the premises
    access_tier TEXT DEFAULT 'unknown',        -- none | low | high | unknown
    capability_tier TEXT DEFAULT 'unknown',    -- technical capability: low | medium | high | unknown
    capability_basis TEXT DEFAULT '',  -- REQUIRED evidence: profession, credentials, demonstrated skill
    resources_note TEXT DEFAULT '',    -- means to commission surveillance (relevant assets only)
    conduct_note TEXT DEFAULT '',      -- documented harassment / litigation / restraining orders
    analyst_notes TEXT DEFAULT '',     -- investigator's own subjective judgment
    opportunity_vectors TEXT DEFAULT '', -- social/institutional areas warranting observation or de-confliction
    named_by_client INTEGER DEFAULT 0, -- client provided this name -> may appear by name in the client pre-sweep
    status TEXT DEFAULT 'open',        -- cleared | open | concern  (deconfliction state)
    created_utc REAL, updated_utc REAL
);
CREATE TABLE IF NOT EXISTS case_files (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL,
    filename TEXT NOT NULL, stored_name TEXT NOT NULL,
    kind TEXT, size_bytes INTEGER, sha256 TEXT,
    uploaded_utc REAL, notes TEXT DEFAULT '',
    poi_id INTEGER                     -- optional link to persons_of_interest
);
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL,
    path TEXT NOT NULL, kind TEXT, generated_utc REAL
);
CREATE TABLE IF NOT EXISTS case_studies (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    device_class TEXT DEFAULT '',      -- e.g. consumer GPS tracker, audio transmitter
    rf_signature TEXT DEFAULT '',      -- generic signal description, no case specifics
    discovery TEXT DEFAULT '',         -- how the device was located
    case_history TEXT DEFAULT '',      -- de-identified COMPOSITE narrative only
    lessons TEXT DEFAULT '',           -- what the example teaches a client
    photo_path TEXT DEFAULT '',        -- optional device photo (local file)
    deidentified INTEGER DEFAULT 0,    -- operator attests composite is de-identified;
                                       -- only attested studies render in reports
    created_utc REAL, updated_utc REAL
);
CREATE TABLE IF NOT EXISTS recordings (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL,
    signal_id INTEGER,                 -- originating detection, if any
    center_mhz REAL NOT NULL,
    offset_hz REAL DEFAULT 0,          -- demod tuner offset at time of capture
    mode TEXT DEFAULT '',              -- nfm | wfm | am | usb | lsb | cw
    duration_s REAL,
    sample_rate INTEGER,
    file_path TEXT NOT NULL,           -- relative path under recordings/ (gitignored)
    kind TEXT DEFAULT 'audio',         -- audio (WAV) | iq (follow-on)
    created_utc REAL,
    notes TEXT DEFAULT ''
);
-- ECPA force-decode audit log: every time the operator overrides the attribution
-- gate to decode a signal NOT attributed to the client, it is recorded here with
-- their acknowledgment + reason. This is the legal artifact; it is never deleted.
CREATE TABLE IF NOT EXISTS decode_overrides (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL,
    signal_id INTEGER,
    center_mhz REAL,
    mode TEXT DEFAULT '',
    operator TEXT DEFAULT '',
    ecpa_ack INTEGER DEFAULT 0,        -- 1 = operator acknowledged the ECPA warning
    reason TEXT DEFAULT '',
    created_utc REAL
);
-- Signed engagement authorization — the sign gate. Receiving, analyzing,
-- decoding or recording CONTENT requires an active (unrevoked) row for the
-- session; passive spectrum view never does, so the operator can eyeball the
-- band before committing. Scope is the whole engagement, not per signal: it is
-- signed once at the start rather than interrupting a sweep on a client site.
-- Immutable — never UPDATE except to set revoked_utc (soft revoke).
CREATE TABLE IF NOT EXISTS session_authorizations (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL,
    operator TEXT DEFAULT '',
    client_name TEXT DEFAULT '',
    scope_text TEXT DEFAULT '',
    authorized_area TEXT DEFAULT '',
    attestations_json TEXT DEFAULT '{}',   -- all four must be true to insert
    signature_name TEXT NOT NULL,
    signature_kind TEXT DEFAULT 'typed',
    signature_blob TEXT DEFAULT '',
    signature_utc REAL NOT NULL,
    revoked_utc REAL,                      -- NULL = active
    created_utc REAL
);
-- LAN device access-control audit. Blocking a device is an ACTIVE change to the
-- client's network, so every block/unblock is operator-confirmed and recorded
-- here with who/when/why. Append-only artifact; never automatic.
CREATE TABLE IF NOT EXISTS net_blocks (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL,
    inventory_id INTEGER,
    mac TEXT,
    ip TEXT,
    action TEXT,                       -- block | unblock
    operator TEXT DEFAULT '',
    reason TEXT DEFAULT '',
    created_utc REAL
);
-- Phase-interferometry DF (B220 dual-RX): best bearing per lock, logged when
-- the loop stops. Receive-only; bearings are relative to the antenna bar.
CREATE TABLE IF NOT EXISTS df_log (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL,
    center_mhz REAL NOT NULL,
    room TEXT DEFAULT '',
    bearing_deg REAL,                  -- from broadside; NULL if never resolved
    mirror_deg REAL,                   -- the front/back twin (2-element ambiguity)
    phase_deg REAL,                    -- calibrated cross-spectrum phase
    coherence REAL,                    -- 0..1 same-wavefront confidence
    snr_db REAL,
    spacing_m REAL,
    calibrated INTEGER DEFAULT 0,
    notes TEXT DEFAULT '',
    ts_utc REAL
);
CREATE INDEX IF NOT EXISTS idx_df_log_session ON df_log(session_id);
CREATE INDEX IF NOT EXISTS idx_baseline_session ON baseline(session_id);
CREATE INDEX IF NOT EXISTS idx_signals_session ON signals(session_id);
CREATE INDEX IF NOT EXISTS idx_segments_sweep ON segments(sweep_id);
CREATE INDEX IF NOT EXISTS idx_recordings_session ON recordings(session_id);
CREATE INDEX IF NOT EXISTS idx_recordings_freq ON recordings(center_mhz);
CREATE INDEX IF NOT EXISTS idx_signals_session_phase ON signals(session_id, phase);
"""


class Database:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)
        self._migrate()

    def _migrate(self):
        """Additive column migrations for databases created by older versions."""
        with self._conn() as c:
            # FIELD vs LAB. A bench/test session must never be mistaken for
            # client work or leave the building in a report. Existing rows all
            # carry real client addresses, so 'field' is the correct backfill —
            # the operator can retag any bench session afterwards.
            cols = {r["name"] for r in c.execute("PRAGMA table_info(sessions)")}
            if "kind" not in cols:
                c.execute("ALTER TABLE sessions ADD COLUMN kind TEXT DEFAULT 'field'")
                c.execute("UPDATE sessions SET kind='field' WHERE kind IS NULL")
            cols = {r["name"] for r in c.execute("PRAGMA table_info(captures)")}
            if "kind" not in cols:
                c.execute("ALTER TABLE captures ADD COLUMN kind TEXT DEFAULT 'zoom'")
            cols = {r["name"] for r in c.execute("PRAGMA table_info(case_files)")}
            if "poi_id" not in cols:
                c.execute("ALTER TABLE case_files ADD COLUMN poi_id INTEGER")
            cols = {r["name"] for r in c.execute("PRAGMA table_info(persons_of_interest)")}
            if "opportunity_vectors" not in cols:
                c.execute("ALTER TABLE persons_of_interest "
                          "ADD COLUMN opportunity_vectors TEXT DEFAULT ''")
            if "named_by_client" not in cols:
                c.execute("ALTER TABLE persons_of_interest "
                          "ADD COLUMN named_by_client INTEGER DEFAULT 0")
            cols = {r["name"] for r in c.execute("PRAGMA table_info(engagement)")}
            if "offsite_location" not in cols:
                c.execute("ALTER TABLE engagement "
                          "ADD COLUMN offsite_location TEXT DEFAULT ''")
            if "scheduling_note" not in cols:
                c.execute("ALTER TABLE engagement "
                          "ADD COLUMN scheduling_note TEXT DEFAULT ''")
            # Per-detection triage state (gated MANUAL review) + novelty flags.
            cols = {r["name"] for r in c.execute("PRAGMA table_info(signals)")}
            if "triage_status" not in cols:
                c.execute("ALTER TABLE signals ADD COLUMN "
                          "triage_status TEXT DEFAULT 'unreviewed'")
                # unreviewed | cleared | suspect | flagged | ask_client
            if "triage_notes" not in cols:
                c.execute("ALTER TABLE signals ADD COLUMN triage_notes TEXT DEFAULT ''")
            if "triage_updated_utc" not in cols:
                c.execute("ALTER TABLE signals ADD COLUMN triage_updated_utc REAL")
            if "is_novel_to_address" not in cols:
                c.execute("ALTER TABLE signals ADD COLUMN "
                          "is_novel_to_address INTEGER DEFAULT 0")
            if "novelty_reason" not in cols:
                c.execute("ALTER TABLE signals ADD COLUMN novelty_reason TEXT DEFAULT ''")
            if "identity_tier" not in cols:
                c.execute("ALTER TABLE signals ADD COLUMN identity_tier TEXT DEFAULT ''")
            if "display_label" not in cols:
                c.execute("ALTER TABLE signals ADD COLUMN display_label TEXT DEFAULT ''")
            if "group_key" not in cols:
                c.execute("ALTER TABLE signals ADD COLUMN group_key TEXT DEFAULT ''")
            if "group_label" not in cols:
                c.execute("ALTER TABLE signals ADD COLUMN group_label TEXT DEFAULT ''")
            if "attribution_status" not in cols:
                c.execute("ALTER TABLE signals ADD COLUMN "
                          "attribution_status TEXT DEFAULT 'unassessed'")
            if "attribution_notes" not in cols:
                c.execute("ALTER TABLE signals ADD COLUMN attribution_notes TEXT DEFAULT ''")
            if "attribution_updated_utc" not in cols:
                c.execute("ALTER TABLE signals ADD COLUMN attribution_updated_utc REAL")
            # Plain-language explanation of how anomaly_score was reached.
            if "score_reason" not in cols:
                c.execute("ALTER TABLE signals ADD COLUMN score_reason TEXT DEFAULT ''")
            # WHEN this detection was recorded. Every hit on a client report has
            # to carry a date/time; previously a signal's only time reference was
            # its parent sweep. Historic rows are backfilled from that parent
            # sweep's start, so old sweeps still date correctly on a report.
            if "detected_utc" not in cols:
                c.execute("ALTER TABLE signals ADD COLUMN detected_utc REAL")
                c.execute("UPDATE signals SET detected_utc = "
                          "(SELECT started_utc FROM sweeps WHERE sweeps.id = "
                          "signals.sweep_id) WHERE detected_utc IS NULL")
            # Persisted Signal ID (sigidwiki/catalog matcher) results. Candidates
            # only — never merged into matched_label/classification, which the
            # baseline correlation path owns. id_mode records match quality:
            # 'iq' = live modulation features, 'band' = frequency-only guess.
            if "id_top_name" not in cols:
                c.execute("ALTER TABLE signals ADD COLUMN id_top_name TEXT DEFAULT ''")
            if "id_top_score" not in cols:
                c.execute("ALTER TABLE signals ADD COLUMN id_top_score REAL")
            if "id_device_class" not in cols:
                c.execute("ALTER TABLE signals ADD COLUMN id_device_class TEXT DEFAULT ''")
            if "id_mode" not in cols:
                c.execute("ALTER TABLE signals ADD COLUMN id_mode TEXT DEFAULT ''")
            if "id_candidates_json" not in cols:
                c.execute("ALTER TABLE signals ADD COLUMN id_candidates_json TEXT DEFAULT ''")
            if "id_updated_utc" not in cols:
                c.execute("ALTER TABLE signals ADD COLUMN id_updated_utc REAL")
            # Bin indices of the detection within its segment's stored arrays
            # (segments.data_json). The detector always computed these but they
            # were dropped at insert time — persisting them lets the ledger crop
            # the exact hit window for thumbnails/highlights instead of guessing
            # from center±bw/2 (the power-weighted center can sit off-middle).
            if "bin_start" not in cols:
                c.execute("ALTER TABLE signals ADD COLUMN bin_start INTEGER")
            if "bin_end" not in cols:
                c.execute("ALTER TABLE signals ADD COLUMN bin_end INTEGER")
            # One-line ANALYZE verdict (decode_router.interpret) persisted per
            # detection, so "what is this" survives into the live ledger and
            # results lists instead of living only in one HTTP response.
            if "analysis_summary" not in cols:
                c.execute("ALTER TABLE signals ADD COLUMN analysis_summary TEXT DEFAULT ''")
            if "analysis_readable" not in cols:
                c.execute("ALTER TABLE signals ADD COLUMN analysis_readable TEXT DEFAULT ''")
            if "analysis_protocol" not in cols:
                c.execute("ALTER TABLE signals ADD COLUMN analysis_protocol TEXT DEFAULT ''")
            if "analysis_utc" not in cols:
                c.execute("ALTER TABLE signals ADD COLUMN analysis_utc REAL")
            # Guided field experiments (phone test, inventory subtract, …) as
            # JSON list of outcome records — accuracy without content demod.
            if "experiments_json" not in cols:
                c.execute("ALTER TABLE signals ADD COLUMN "
                          "experiments_json TEXT DEFAULT ''")
            # OPERATOR-MARKED detections (tech clicked the spectrum/waterfall and
            # said "this one"). EVIDENTIARY: a human-marked frequency is NOT an
            # instrument detection and must never be presentable as one — the
            # report has to be able to tell them apart. 0 = detector found it,
            # 1 = the operator marked it by eye.
            if "operator_marked" not in cols:
                c.execute("ALTER TABLE signals ADD COLUMN "
                          "operator_marked INTEGER DEFAULT 0")
            if "operator_marked_utc" not in cols:
                c.execute("ALTER TABLE signals ADD COLUMN operator_marked_utc REAL")
            # NEAR-FLOOR candidates: cleared the low tier but not the detection
            # threshold. A bug sweep's whole point is low-power emitters, and a
            # single 12 dB gate was dropping signals the operator could plainly
            # see (0-2 dB over the floor). These are LEADS for review — same
            # evidentiary rule as operator_marked: never printed as findings.
            if "near_floor" not in cols:
                c.execute("ALTER TABLE signals ADD COLUMN near_floor INTEGER DEFAULT 0")
            # LAN device checker: IP/vendor enrichment + operator block state on
            # the shared inventory table (kind='lan'). blocked is set ONLY by an
            # operator-confirmed action, never by discovery.
            cols = {r["name"] for r in c.execute("PRAGMA table_info(inventory)")}
            if "ip" not in cols:
                c.execute("ALTER TABLE inventory ADD COLUMN ip TEXT DEFAULT ''")
            if "vendor" not in cols:
                c.execute("ALTER TABLE inventory ADD COLUMN vendor TEXT DEFAULT ''")
            if "blocked" not in cols:
                c.execute("ALTER TABLE inventory ADD COLUMN blocked INTEGER DEFAULT 0")
            if "block_utc" not in cols:
                c.execute("ALTER TABLE inventory ADD COLUMN block_utc REAL")
            if "block_reason" not in cols:
                c.execute("ALTER TABLE inventory ADD COLUMN block_reason TEXT DEFAULT ''")

    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def execute(self, sql, params=()):
        with _LOCK, self._conn() as c:
            cur = c.execute(sql, params)
            return cur.lastrowid

    def executemany(self, sql, rows):
        with _LOCK, self._conn() as c:
            c.executemany(sql, rows)

    def query(self, sql, params=()):
        with self._conn() as c:
            return [dict(r) for r in c.execute(sql, params).fetchall()]

    def query_one(self, sql, params=()):
        rows = self.query(sql, params)
        return rows[0] if rows else None

    # --- convenience helpers ---

    def create_session(self, address, lat, lon, geocoder, r_bcast, r_lmr, operator,
                       kind="field"):
        return self.execute(
            "INSERT INTO sessions (created_utc,address,lat,lon,geocoder,"
            "radius_broadcast_mi,radius_lmr_mi,operator,kind) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (time.time(), address, lat, lon, geocoder, r_bcast, r_lmr, operator,
             "lab" if kind == "lab" else "field"))

    def set_device_info(self, session_id, info: dict):
        self.execute("UPDATE sessions SET device_info=? WHERE id=?",
                     (json.dumps(info), session_id))

    def update_sweep_metadata(self, sweep_id, session_id, phase, room, notes=""):
        """Correct historical sweep metadata without changing raw measurements.

        ``phase`` and ``room`` are duplicated onto signal rows for fast reporting,
        so both tables must change together.  Capture rows linked to those signals
        also inherit the corrected room.  The sweep's JSON metadata keeps an audit
        trail of every edit and the operator's current note.

        Returns the updated sweep, or ``None`` when the sweep is not part of the
        requested session.  Callers are responsible for running session-wide
        post-processing after this transaction commits.
        """
        now = time.time()
        with _LOCK, self._conn() as c:
            row = c.execute(
                "SELECT * FROM sweeps WHERE id=? AND session_id=?",
                (sweep_id, session_id)).fetchone()
            if not row:
                return None

            raw_meta = row["meta"]
            try:
                meta = json.loads(raw_meta) if raw_meta else {}
            except (TypeError, ValueError):
                meta = {"legacy_meta": raw_meta}
            if not isinstance(meta, dict):
                meta = {"legacy_meta": meta}
            history = meta.get("operator_edits")
            if not isinstance(history, list):
                history = []
            history.append({
                "edited_utc": now,
                "prior_phase": row["phase"],
                "phase": phase,
                "prior_room": row["room"] or "",
                "room": room,
                "notes": notes,
            })
            meta["operator_notes"] = notes
            meta["operator_edits"] = history[-50:]

            c.execute(
                "UPDATE sweeps SET phase=?, room=?, meta=? "
                "WHERE id=? AND session_id=?",
                (phase, room, json.dumps(meta), sweep_id, session_id))
            c.execute(
                "UPDATE signals SET phase=?, room=? "
                "WHERE sweep_id=? AND session_id=?",
                (phase, room, sweep_id, session_id))
            c.execute(
                "UPDATE captures SET room=? WHERE session_id=? AND signal_id IN "
                "(SELECT id FROM signals WHERE sweep_id=? AND session_id=?)",
                (room, session_id, sweep_id, session_id))

        return self.query_one(
            "SELECT * FROM sweeps WHERE id=? AND session_id=?",
            (sweep_id, session_id))

    def set_source_status(self, session_id, source, status, count, message=""):
        self.execute(
            "INSERT INTO source_status (session_id,source,status,count,fetched_utc,message) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(session_id,source) DO UPDATE SET "
            "status=excluded.status,count=excluded.count,"
            "fetched_utc=excluded.fetched_utc,message=excluded.message",
            (session_id, source, status, count, time.time(), message))

    def add_baseline_rows(self, session_id, rows: list[dict]):
        self.executemany(
            "INSERT INTO baseline (session_id,source,freq_low_mhz,freq_high_mhz,service,"
            "label,callsign,licensee,erp,distance_mi,bearing_deg,confidence,channel_bw_khz,"
            "category,caution,uplink,notes,raw_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(session_id, r["source"], r["freq_low_mhz"], r["freq_high_mhz"],
              r.get("service"), r.get("label"), r.get("callsign"), r.get("licensee"),
              r.get("erp"), r.get("distance_mi"), r.get("bearing_deg"),
              r.get("confidence", "allocation"), r.get("channel_bw_khz", 25.0),
              r.get("category", "other"), int(bool(r.get("caution"))),
              int(bool(r.get("uplink"))), r.get("notes", ""),
              json.dumps(r.get("raw", {}))) for r in rows])

    def clear_baseline(self, session_id):
        self.execute("DELETE FROM baseline WHERE session_id=?", (session_id,))

    # --- triage (gated MANUAL review) ---

    def set_signal_triage(self, signal_id, session_id, status, notes=""):
        """Set a detection's triage decision. Scoped to session to block
        cross-session writes. Returns the updated row, or None if not found."""
        self.execute(
            "UPDATE signals SET triage_status=?, triage_notes=?, triage_updated_utc=? "
            "WHERE id=? AND session_id=?",
            (status, notes, time.time(), signal_id, session_id))
        return self.query_one(
            "SELECT id, triage_status, triage_notes, triage_updated_utc "
            "FROM signals WHERE id=? AND session_id=?", (signal_id, session_id))

    def set_signal_attribution(self, signal_id, session_id, status, notes=""):
        """Record whether a detection is capturing the CLIENT'S space (a threat) vs a
        third party / infrastructure (move along). Session-scoped. Returns the row."""
        self.execute(
            "UPDATE signals SET attribution_status=?, attribution_notes=?, "
            "attribution_updated_utc=? WHERE id=? AND session_id=?",
            (status, notes, time.time(), signal_id, session_id))
        return self.query_one(
            "SELECT id, attribution_status, attribution_notes, attribution_updated_utc "
            "FROM signals WHERE id=? AND session_id=?", (signal_id, session_id))

    def set_signal_identification(self, signal_id, session_id, candidates,
                                  mode):
        """Persist Signal ID matcher results on a detection. `candidates` is the
        identify() list (may be empty — stored as '[]' to distinguish 'ran, no
        match' from 'never ran'); mode is 'iq' (live modulation features) or
        'band' (frequency-only guess, RF Explorer). Session-scoped; returns the
        updated row or None. Deliberately does NOT touch matched_label/
        classification — those belong to the baseline correlation path."""
        top = candidates[0] if candidates else {}
        self.execute(
            "UPDATE signals SET id_top_name=?, id_top_score=?, id_device_class=?, "
            "id_mode=?, id_candidates_json=?, id_updated_utc=? "
            "WHERE id=? AND session_id=?",
            (top.get("name", ""), top.get("score"),
             top.get("device_class") or "", mode,
             json.dumps(candidates), time.time(), signal_id, session_id))
        return self.query_one(
            "SELECT id, id_top_name, id_top_score, id_device_class, id_mode, "
            "id_updated_utc FROM signals WHERE id=? AND session_id=?",
            (signal_id, session_id))

    def append_signal_experiment(self, signal_id, session_id, rec: dict):
        """Append one field-experiment outcome to signals.experiments_json."""
        from .experiments import append_experiment_json, summarize_outcome
        row = self.query_one(
            "SELECT experiments_json FROM signals WHERE id=? AND session_id=?",
            (signal_id, session_id))
        if not row:
            return None
        new_json = append_experiment_json(row.get("experiments_json") or "", rec)
        self.execute(
            "UPDATE signals SET experiments_json=? WHERE id=? AND session_id=?",
            (new_json, signal_id, session_id))
        return self.query_one(
            "SELECT * FROM signals WHERE id=? AND session_id=?",
            (signal_id, session_id))

    def set_signal_analysis(self, signal_id, session_id, summary, readable,
                            protocol=""):
        """Persist the one-line ANALYZE verdict (decode_router.interpret) on a
        detection so the ledger/results can show it without re-analyzing.
        Session-scoped like the other per-signal writers; returns the row."""
        self.execute(
            "UPDATE signals SET analysis_summary=?, analysis_readable=?, "
            "analysis_protocol=?, analysis_utc=? WHERE id=? AND session_id=?",
            (summary or "", readable or "", protocol or "", time.time(),
             signal_id, session_id))
        return self.query_one(
            "SELECT id, analysis_summary, analysis_readable, analysis_protocol, "
            "analysis_utc FROM signals WHERE id=? AND session_id=?",
            (signal_id, session_id))

    # --- LAN device checker (inventory kind='lan') ---

    def upsert_lan_devices(self, session_id: int, devices: list[dict]):
        """Store/refresh discovered LAN hosts on the inventory table, keyed by
        (session, kind='lan', MAC). Never touches `owned` or `blocked` — those
        are operator decisions, preserved across rescans. Returns rows touched."""
        now = time.time()
        n = 0
        for d in devices:
            self.execute(
                "INSERT INTO inventory (session_id,kind,identifier,name,ip,vendor,"
                "rssi_dbm,channel,freq_mhz,extra,first_seen,last_seen) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(session_id,kind,identifier) DO UPDATE SET "
                "name=CASE WHEN excluded.name!='' THEN excluded.name ELSE inventory.name END, "
                "ip=excluded.ip, vendor=excluded.vendor, extra=excluded.extra, "
                "last_seen=excluded.last_seen",
                (session_id, "lan", d["identifier"], d.get("name", ""),
                 d.get("ip", ""), d.get("vendor", ""), None,
                 d.get("channel", ""), d.get("freq_mhz"), d.get("extra", ""),
                 now, now))
            n += 1
        return n

    def lan_devices(self, session_id: int) -> list[dict]:
        return self.query(
            "SELECT * FROM inventory WHERE session_id=? AND kind='lan' "
            "ORDER BY blocked DESC, owned ASC, last_seen DESC", (session_id,))

    def set_inventory_owned(self, inventory_id, session_id, owned: bool):
        """Operator allowlist toggle, session-scoped. Returns the row."""
        self.execute(
            "UPDATE inventory SET owned=? WHERE id=? AND session_id=?",
            (int(bool(owned)), inventory_id, session_id))
        return self.query_one(
            "SELECT * FROM inventory WHERE id=? AND session_id=?",
            (inventory_id, session_id))

    def set_inventory_block(self, inventory_id, session_id, blocked: bool,
                            operator="", reason=""):
        """Operator-confirmed block/unblock of a LAN device. Session-scoped.
        Writes the inventory flag AND an append-only net_blocks audit row.
        NEVER called automatically — only from the confirmed endpoint. Returns
        the updated inventory row, or None if not in this session."""
        row = self.query_one(
            "SELECT * FROM inventory WHERE id=? AND session_id=?",
            (inventory_id, session_id))
        if not row:
            return None
        now = time.time()
        self.execute(
            "UPDATE inventory SET blocked=?, block_utc=?, block_reason=? "
            "WHERE id=? AND session_id=?",
            (int(bool(blocked)), now if blocked else None,
             reason if blocked else "", inventory_id, session_id))
        self.execute(
            "INSERT INTO net_blocks (session_id,inventory_id,mac,ip,action,"
            "operator,reason,created_utc) VALUES (?,?,?,?,?,?,?,?)",
            (session_id, inventory_id, row["identifier"], row.get("ip") or "",
             "block" if blocked else "unblock", operator, reason, now))
        return self.query_one(
            "SELECT * FROM inventory WHERE id=? AND session_id=?",
            (inventory_id, session_id))

    def list_net_blocks(self, session_id):
        return self.query(
            "SELECT * FROM net_blocks WHERE session_id=? ORDER BY created_utc DESC",
            (session_id,))

    def add_decode_override(self, session_id, signal_id, center_mhz, mode,
                            operator, reason):
        """Append an ECPA force-decode acknowledgment to the immutable audit log."""
        rid = self.execute(
            "INSERT INTO decode_overrides (session_id, signal_id, center_mhz, mode, "
            "operator, ecpa_ack, reason, created_utc) VALUES (?,?,?,?,?,1,?,?)",
            (session_id, signal_id, center_mhz, mode, operator, reason, time.time()))
        return self.query_one("SELECT * FROM decode_overrides WHERE id=?", (rid,))

    def list_decode_overrides(self, session_id):
        return self.query(
            "SELECT * FROM decode_overrides WHERE session_id=? ORDER BY created_utc DESC",
            (session_id,))

    def add_session_authorization(self, session_id, signature_name, operator="",
                                  client_name="", scope_text="", authorized_area="",
                                  attestations=None, signature_kind="typed",
                                  signature_blob=""):
        """Record the signed engagement authorization. Immutable once written —
        the caller validates the attestations; this only stores them."""
        now = time.time()
        rid = self.execute(
            "INSERT INTO session_authorizations (session_id, operator, client_name, "
            "scope_text, authorized_area, attestations_json, signature_name, "
            "signature_kind, signature_blob, signature_utc, created_utc) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (session_id, operator, client_name, scope_text, authorized_area,
             json.dumps(attestations or {}), signature_name, signature_kind,
             signature_blob, now, now))
        return self.query_one("SELECT * FROM session_authorizations WHERE id=?", (rid,))

    def active_authorization(self, session_id):
        """Newest unrevoked authorization for the session, or None. This is THE
        predicate the receive/decode gate reads — never infer authorization from
        attribution, session kind, or anything else."""
        return self.query_one(
            "SELECT * FROM session_authorizations WHERE session_id=? "
            "AND revoked_utc IS NULL ORDER BY signature_utc DESC, id DESC LIMIT 1",
            (session_id,))

    def revoke_authorization(self, auth_id, session_id):
        """Soft revoke — the row itself is the legal artifact and never leaves."""
        self.execute(
            "UPDATE session_authorizations SET revoked_utc=? WHERE id=? "
            "AND session_id=? AND revoked_utc IS NULL",
            (time.time(), auth_id, session_id))
        return self.query_one(
            "SELECT * FROM session_authorizations WHERE id=? AND session_id=?",
            (auth_id, session_id))

    def list_session_authorizations(self, session_id):
        return self.query(
            "SELECT * FROM session_authorizations WHERE session_id=? "
            "ORDER BY created_utc DESC", (session_id,))

    def bulk_set_signal_triage(self, signal_ids, session_id, status, notes=""):
        """Apply the same triage decision to many signals in one session."""
        ids = [int(i) for i in signal_ids if i is not None]
        if not ids:
            return 0
        now = time.time()
        placeholders = ",".join("?" * len(ids))
        self.execute(
            f"UPDATE signals SET triage_status=?, triage_notes=?, triage_updated_utc=? "
            f"WHERE session_id=? AND id IN ({placeholders})",
            (status, notes, now, session_id, *ids))
        row = self.query_one(
            f"SELECT COUNT(*) AS n FROM signals WHERE session_id=? AND id IN ({placeholders})",
            (session_id, *ids))
        return int(row["n"]) if row else 0

    # --- recordings (monitor captures; gitignored, client-sensitive) ---

    def add_recording(self, session_id, center_mhz, file_path, *, signal_id=None,
                      offset_hz=0.0, mode="", duration_s=None, sample_rate=None,
                      kind="audio", notes=""):
        return self.execute(
            "INSERT INTO recordings (session_id,signal_id,center_mhz,offset_hz,mode,"
            "duration_s,sample_rate,file_path,kind,created_utc,notes) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (session_id, signal_id, center_mhz, offset_hz, mode, duration_s,
             sample_rate, file_path, kind, time.time(), notes))

    def list_recordings(self, session_id, signal_id=None):
        if signal_id is not None:
            return self.query(
                "SELECT * FROM recordings WHERE session_id=? AND signal_id=? "
                "ORDER BY created_utc DESC", (session_id, signal_id))
        return self.query(
            "SELECT * FROM recordings WHERE session_id=? ORDER BY created_utc DESC",
            (session_id,))
