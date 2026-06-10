"""Quick live inventory scan check against the running app."""
import requests

B = "http://127.0.0.1:8765"
sessions = requests.get(B + "/api/sessions", timeout=30).json()
requests.post(B + "/api/session/open", json={"id": sessions[0]["id"]},
              timeout=30).raise_for_status()
w = requests.post(B + "/api/inventory/scan", json={"kind": "wifi"}, timeout=60).json()
print("wifi:", w)
b = requests.post(B + "/api/inventory/scan", json={"kind": "ble"}, timeout=120).json()
print("ble:", b)
inv = requests.get(B + "/api/inventory", timeout=30).json()
for d in inv[:14]:
    flag = "LOUD+UNOWNED" if d["loud_unowned"] else ""
    name = (d["name"] or "")[:28]
    print(f"  {d['kind']:4} {str(d['rssi_dbm']):>6} dBm  {d['identifier']}  {name:28} {flag}")
print("total devices:", len(inv))
