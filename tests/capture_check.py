"""Live check: capture a known strong signal (FM broadcast) into the wiki."""
import time

import requests

B = "http://127.0.0.1:8765"

sessions = requests.get(B + "/api/sessions", timeout=30).json()
# use the operator's most recent real session
mine = {"ui-check", "module-switch-check", "smoke-test"}
real = next((s for s in sessions if (s.get("operator") or "") not in mine), sessions[0])
requests.post(B + "/api/session/open", json={"id": real["id"]}, timeout=30).raise_for_status()
print(f"[1] session #{real['id']} {real['address']}")

# capture a synthetic target: strongest local FM (94.9 KC-area) as a wiki test
r = requests.post(B + "/api/captures/start", json={"signal_ids": []}, timeout=30)
print("[2] empty target rejection:", r.status_code, r.json().get("error", "?")[:60])

# fabricate via recapture path? No — insert a real detected signal if one exists
sigs = requests.get(B + "/api/signals", timeout=30).json()
target = next((s for s in sigs if 88 <= s["center_mhz"] <= 108), None) or \
    (sigs[0] if sigs else None)
if not target:
    raise SystemExit("no detected signals in this session to capture — run a sweep first")
print(f"[3] capturing {target['center_mhz']:.4f} MHz ({target['classification']})")
requests.post(B + "/api/captures/start",
              json={"signal_ids": [target["id"]]}, timeout=60).raise_for_status()

for _ in range(60):
    st = requests.get(B + "/api/state", timeout=30).json()
    if not st["busy"]:
        break
    time.sleep(2)
print("[4]", st["sweep"]["message"])

caps = requests.get(B + "/api/captures", timeout=30).json()
print(f"[5] wiki entries: {len(caps['rows'])}, categories: "
      f"{[(c['category'], c['n']) for c in caps['categories']]}")
if caps["rows"]:
    cap = requests.get(B + f"/api/captures/{caps['rows'][0]['id']}", timeout=30).json()
    print(f"[6] entry: {cap['label']} | {cap['category']} | peak {cap['peak_dbm']} dBm "
          f"| img {cap['image_path']}")
    print(f"    description: {(cap['description'] or '')[:180]}")
    img = requests.get(B + "/capture_images/" + cap["image_path"], timeout=30)
    print(f"[7] SVG: {img.status_code}, {len(img.content)} bytes")
print("CAPTURE CHECK DONE")
