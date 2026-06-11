"""Verify auto-archive populates the wiki from existing sweep data, the grouped
view, the cross-session timeline/variance, and case-file upload."""
import io
import os
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from survey.config import Config           # noqa: E402
from survey.db import Database             # noqa: E402
from survey.sigcapture import archive_observations, signal_timeline  # noqa: E402

BASE = Path(__file__).resolve().parents[1]
cfg = Config(str(BASE / "config.yaml"))
db = Database(str(BASE / "data" / "sweeps.db"))

# pick the session with the most detected signals that have segment data
rows = db.query("SELECT s.session_id, COUNT(*) n FROM signals s "
                "JOIN segments seg ON s.segment_id=seg.id GROUP BY s.session_id "
                "ORDER BY n DESC LIMIT 1")
if not rows:
    print("no sessions with segment-backed signals yet — run a real sweep first")
    sys.exit(0)
sid = rows[0]["session_id"]
sess = db.query_one("SELECT address FROM sessions WHERE id=?", (sid,))
print(f"archiving observations for session #{sid} ({sess['address']}), "
      f"{rows[0]['n']} segment-backed signals")
added = archive_observations(db, cfg, sid, str(BASE / "capture_images"),
                             sess["address"])
print(f"[1] archived {added} observations")

total = db.query_one("SELECT COUNT(*) n FROM captures WHERE kind='observation'")["n"]
print(f"[2] total observation captures in wiki: {total}")

# pick a frequency seen and show its timeline/variance
any_obs = db.query_one("SELECT center_mhz FROM captures WHERE kind='observation' "
                       "ORDER BY peak_dbm DESC LIMIT 1")
if any_obs:
    tl = signal_timeline(db, any_obs["center_mhz"])
    print(f"[3] timeline for {any_obs['center_mhz']:.4f} MHz: "
          f"{tl['variance'].get('observations', 0)} sightings, "
          f"variance={tl['variance']}")

# case-file upload via the running API
B = "http://127.0.0.1:8765"
sessions = requests.get(B + "/api/sessions", timeout=30).json()
requests.post(B + "/api/session/open", json={"id": sessions[0]["id"]}, timeout=30)
fd = {"file": ("test_intake.txt", io.BytesIO(b"sample case file - not real PII"),
               "text/plain")}
r = requests.post(B + "/api/case_files", files=fd,
                  data={"kind": "test"}, timeout=30)
print(f"[4] case-file upload: {r.status_code} {r.json()}")
lst = requests.get(B + "/api/case_files", timeout=30).json()
print(f"[5] case files on session: {[f['filename'] for f in lst]}")

caps = requests.get(B + "/api/captures", timeout=30).json()
print(f"[6] grouped wiki: {len(caps['rows'])} distinct signals "
      f"(grouped={caps['grouped']}); top: "
      f"{[(round(g['center_mhz'],3), g.get('sightings')) for g in caps['rows'][:4]]}")
print("WIKI ARCHIVE CHECK DONE")
