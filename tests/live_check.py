"""Quick live-hardware sanity check: lock the FM band and confirm real RF streams."""
import time

import requests

B = "http://127.0.0.1:8765"

requests.post(B + "/api/device/connect", json={}, timeout=90).raise_for_status()
# use the operator's most recent session — don't stomp whatever the UI has open
sessions = requests.get(B + "/api/sessions", timeout=30).json()
newest = sessions[0]["id"]
requests.post(B + "/api/session/open", json={"id": newest}, timeout=30).raise_for_status()
bl = requests.get(B + "/api/baseline", timeout=30).json()
print(f"session {newest} ({sessions[0]['address']}) baseline rows:", bl["count"])

requests.post(B + "/api/directional/start",
              json={"center_mhz": 98.1, "bw_khz": 4000, "room": "hardware-check"},
              timeout=30).raise_for_status()
dirstate = None
for i in range(30):           # module switch + range confirm can take ~10s on hardware
    time.sleep(2)
    st = requests.get(B + "/api/state", timeout=30).json()
    dirstate = st["sweep"]["directional"]
    if dirstate or not st["sweep"]["running"]:
        break
if dirstate:
    print("live:", dirstate["current_dbm"], "dBm  peak:", dirstate["peak_dbm"], "dBm")
    tr = dirstate.get("trace")
    if tr:
        amps, freqs = tr["amps"], tr["freqs"]
        print("trace bins:", len(amps), " span:", round(freqs[0], 2), "-",
              round(freqs[-1], 2), "MHz")
        print("min/max amp:", min(amps), "/", max(amps), "dBm")
        top = sorted(range(len(amps)), key=lambda i: -amps[i])[:3]
        print("top bins:", [(round(freqs[i], 2), amps[i]) for i in top])
else:
    print("no directional state — error:", st["sweep"].get("error"))
requests.post(B + "/api/directional/stop", json={}, timeout=30)
