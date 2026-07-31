"""Self-seed the PUBLIC DEMO database from the committed PII-free seed.

The demo runs on its own empty demo.db (data/ and capture_images/ are git- and
docker-ignored, so nothing real ever ships). On boot, app.py calls seed_demo()
which — if the Signal Wiki is empty — populates it from survey/demo_seed.json:
de-identified reference captures + attested case-study composites.

Every spectrograph is RE-RENDERED here with deidentified=True, so even though
the seed carries only numeric arrays, the published SVGs cannot leak a source
client's address/room/session. Idempotent: it no-ops once captures exist.
"""

import json
import os
import time

from .sigcapture import render_capture_svg

SEED_PATH = os.path.join(os.path.dirname(__file__), "demo_seed.json")


def seed_demo(db, cfg) -> int:
    """Populate an empty demo DB. Returns the number of captures seeded."""
    if db.query_one("SELECT 1 FROM captures LIMIT 1"):
        return 0  # already seeded — don't duplicate
    if not os.path.exists(SEED_PATH):
        return 0
    with open(SEED_PATH, encoding="utf-8") as f:
        seed = json.load(f)

    images_dir = cfg.abs_path("capture_images")
    os.makedirs(images_dir, exist_ok=True)

    # neutral reference-site sessions (no address/lat/lon/operator)
    sess_map = {}
    for s in seed.get("sessions", []):
        sid = db.execute(
            "INSERT INTO sessions (created_utc, address, status, operator) "
            "VALUES (?,?,?,?)",
            (s.get("created_utc") or time.time(), s["address"], "demo", "Demo"))
        sess_map[s["ref"]] = sid

    n = 0
    for c in seed.get("captures", []):
        try:
            data = json.loads(c["data_json"])
        except (TypeError, ValueError):
            continue
        meta = {"center_mhz": c.get("center_mhz"), "peak_dbm": c.get("peak_dbm"),
                "floor_dbm": c.get("floor_dbm"), "bw_khz": c.get("bw_khz"),
                "persistence": c.get("persistence"), "label": c.get("label")}
        svg = render_capture_svg(data, meta, deidentified=True)
        fname = f"demo_{n:04d}_{int((c.get('center_mhz') or 0) * 10000)}.svg"
        with open(os.path.join(images_dir, fname), "w", encoding="utf-8") as f:
            f.write(svg)
        # room / module / notes are intentionally blank — no provenance in the demo
        db.execute(
            "INSERT INTO captures (session_id, kind, center_mhz, span_mhz, bw_khz, "
            "peak_dbm, floor_dbm, persistence, room, module, captured_utc, "
            "image_path, data_json, features_json, classification, matched_label, "
            "category, label, status, notes, description) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sess_map.get(c.get("session")), c.get("kind", "zoom"),
             c.get("center_mhz"), c.get("span_mhz"), c.get("bw_khz"),
             c.get("peak_dbm"), c.get("floor_dbm"), c.get("persistence"),
             "", "", c.get("captured_utc") or time.time(), fname,
             json.dumps(data), c.get("features_json") or "{}",
             c.get("classification") or "", c.get("matched_label") or "",
             c.get("category") or "", c.get("label") or "",
             c.get("status") or "unconfirmed", "", c.get("description") or ""))
        n += 1

    now = time.time()
    for cs in seed.get("case_studies", []):
        db.execute(
            "INSERT INTO case_studies (title, device_class, rf_signature, discovery, "
            "case_history, lessons, deidentified, created_utc, updated_utc) "
            "VALUES (?,?,?,?,?,?,1,?,?)",
            (cs.get("title", ""), cs.get("device_class", ""),
             cs.get("rf_signature", ""), cs.get("discovery", ""),
             cs.get("case_history", ""), cs.get("lessons", ""), now, now))
    return n
