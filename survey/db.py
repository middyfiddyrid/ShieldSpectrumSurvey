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
CREATE INDEX IF NOT EXISTS idx_baseline_session ON baseline(session_id);
CREATE INDEX IF NOT EXISTS idx_signals_session ON signals(session_id);
CREATE INDEX IF NOT EXISTS idx_segments_sweep ON segments(sweep_id);
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
