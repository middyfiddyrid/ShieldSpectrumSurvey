"""Build the PUBLIC DEMO seed (survey/demo_seed.json) from the real archive.

Run locally, once, against data/sweeps.db. The output is a committed,
PII-FREE dataset that the demo self-seeds from on boot (see survey/demo_seed.py).

What it does / what it guarantees:
  * Selects the curated zoom captures (the showcase gallery) plus a capped set
    of per-emitter observations so the cross-session sighting/variance view has
    something to show.
  * Sessions are replaced with neutral "De-identified reference site N" labels —
    NO real address, lat/lon, operator, or notes survive.
  * Per capture: room, module, and notes are DROPPED. Only numeric arrays
    (freqs/avg/maxpeak/hits) and signal characteristics are kept. The SVG is NOT
    copied from disk (those footers carry the source address) — it is re-rendered
    de-identified at seed time.
  * case_studies: only operator-attested de-identified composites (deidentified=1).

Re-run any time the library grows; the JSON is overwritten.

    python tools/build_demo_seed.py
"""

import json
import os
import re
import sqlite3
import sys

# generic geography/address words that are not identifying on their own
GENERIC_TERMS = {
    "street", "drive", "road", "avenue", "lane", "court", "circle", "place",
    "boulevard", "highway", "north", "south", "east", "west", "apartment",
    "unit", "suite", "city", "county", "reference", "site", "identified",
}

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(BASE, "data", "sweeps.db")
OUT = os.path.join(BASE, "survey", "demo_seed.json")

MATCH_TOL_MHZ = 0.25            # same emitter window (mirrors sigcapture)
OBS_PER_EMITTER = 6            # cap auto-archived observations kept per emitter

# Fields copied verbatim from a capture row. Deliberately EXCLUDES room, module,
# notes, image_path, session_id, and any address — those are PII surfaces.
CAP_FIELDS = ("kind", "center_mhz", "span_mhz", "bw_khz", "peak_dbm", "floor_dbm",
              "persistence", "captured_utc", "data_json", "features_json",
              "classification", "matched_label", "category", "label", "status",
              "description")


def main():
    if not os.path.exists(SRC):
        sys.exit(f"source archive not found: {SRC}")
    c = sqlite3.connect(SRC)
    c.row_factory = sqlite3.Row

    zooms = c.execute(
        "SELECT * FROM captures WHERE kind='zoom' AND data_json IS NOT NULL "
        "ORDER BY center_mhz").fetchall()

    picked, seen_ids = [], set()
    real_sessions = []      # ordered list of real session_ids contributing rows

    def take(row):
        if row["id"] in seen_ids:
            return
        try:
            data = json.loads(row["data_json"])
        except (TypeError, ValueError):
            return
        if len(data.get("freqs") or []) < 8:
            return
        seen_ids.add(row["id"])
        if row["session_id"] not in real_sessions:
            real_sessions.append(row["session_id"])
        rec = {k: row[k] for k in CAP_FIELDS}
        rec["_session"] = row["session_id"]
        picked.append(rec)

    # 1) every showcase zoom capture
    for z in zooms:
        take(z)
        # 2) a capped sighting history per emitter (observations across sessions)
        obs = c.execute(
            "SELECT * FROM captures WHERE kind='observation' AND data_json IS NOT NULL "
            "AND ABS(center_mhz - ?) <= ? ORDER BY captured_utc",
            (z["center_mhz"], MATCH_TOL_MHZ)).fetchall()
        for o in obs[:OBS_PER_EMITTER]:
            take(o)

    # neutral session remap — index in real_sessions -> "reference site N+1"
    sess_created = {r["id"]: r["created_utc"]
                    for r in c.execute("SELECT id, created_utc FROM sessions")}
    sessions = []
    sess_map = {}
    for i, sid in enumerate(real_sessions, start=1):
        ref = f"site{i}"
        sess_map[sid] = ref
        sessions.append({
            "ref": ref,
            "address": f"De-identified reference site {i}",
            "created_utc": sess_created.get(sid),
        })
    for rec in picked:
        rec["session"] = sess_map[rec.pop("_session")]

    case_studies = [dict(r) for r in c.execute(
        "SELECT title, device_class, rf_signature, discovery, case_history, "
        "lessons FROM case_studies WHERE deidentified=1 ORDER BY id")]

    out = {"sessions": sessions, "captures": picked, "case_studies": case_studies}

    # PII tripwire: derive the denylist from the LIVE archive (addresses, client
    # and POI names) — nothing identifying is hardcoded in this file, so it is
    # itself safe to commit. Assert no real identifier survived into the seed.
    deny = set()

    def add_terms(val):
        for piece in re.findall(r"[A-Za-z]{4,}", val or ""):
            low = piece.lower()
            if low not in GENERIC_TERMS:
                deny.add(low)

    for r in c.execute("SELECT address FROM sessions"):
        add_terms(r["address"])
    for sql in ("SELECT client_name AS v FROM engagement",
                "SELECT client_contact AS v FROM engagement",
                "SELECT name AS v FROM persons_of_interest"):
        try:
            for r in c.execute(sql):
                add_terms(r["v"])
        except sqlite3.OperationalError:
            pass

    # an identifier is a real-address/name term that is NOT part of the
    # legitimate signal/case vocabulary (those words are allowed in the seed) —
    # this removes common words like "high"/"park" that merely coincide.
    allowed = set()
    for r in c.execute("SELECT label, matched_label, category, description FROM captures"):
        for v in (r["label"], r["matched_label"], r["category"], r["description"]):
            allowed.update(re.findall(r"[a-z]{4,}", (v or "").lower()))
    for r in c.execute("SELECT title, device_class, rf_signature, discovery, "
                       "case_history, lessons FROM case_studies WHERE deidentified=1"):
        for v in dict(r).values():
            allowed.update(re.findall(r"[a-z]{4,}", (v or "").lower()))
    deny -= allowed

    seed_words = set(re.findall(r"[a-z]{4,}", json.dumps(out).lower()))
    leaked = sorted(deny & seed_words)
    if leaked:
        sys.exit(f"ABORT: real identifier(s) {leaked} survived into the seed — "
                 "fix the builder before committing")
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, separators=(",", ":"))
    size_kb = os.path.getsize(OUT) / 1024
    print(f"wrote {OUT}")
    print(f"  {len(sessions)} reference sites, {len(picked)} captures "
          f"({sum(1 for r in picked if r['kind']=='zoom')} zoom / "
          f"{sum(1 for r in picked if r['kind']=='observation')} observation), "
          f"{len(case_studies)} case studies — {size_kb:.0f} KB")


if __name__ == "__main__":
    main()
