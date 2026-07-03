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
    notes TEXT
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
    localization_score REAL, strongest_room TEXT
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

    def create_session(self, address, lat, lon, geocoder, r_bcast, r_lmr, operator):
        return self.execute(
            "INSERT INTO sessions (created_utc,address,lat,lon,geocoder,"
            "radius_broadcast_mi,radius_lmr_mi,operator) VALUES (?,?,?,?,?,?,?,?)",
            (time.time(), address, lat, lon, geocoder, r_bcast, r_lmr, operator))

    def set_device_info(self, session_id, info: dict):
        self.execute("UPDATE sessions SET device_info=? WHERE id=?",
                     (json.dumps(info), session_id))

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
