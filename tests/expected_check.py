"""Live check of /api/expected against the operator's real session data."""
import requests

B = "http://127.0.0.1:8765"

sessions = requests.get(B + "/api/sessions", timeout=30).json()
mine = {"ui-check", "module-switch-check", "smoke-test"}
real = next((s for s in sessions if (s.get("operator") or "") not in mine), sessions[0])
requests.post(B + "/api/session/open", json={"id": real["id"]}, timeout=30).raise_for_status()
print(f"session #{real['id']} {real['address']}")

r = requests.get(B + "/api/expected", timeout=60).json()
c = r["counts"]
print(f"counts: {c}")
missing_strong = [x for x in r["rows"] if not x["found"] and x["expectation"] == "strong"]
print(f"\nstrongly-expected but MISSING ({len(missing_strong)}):")
for x in missing_strong[:8]:
    print(f"  {x['freq_low_mhz']:>9.4f} MHz {x['callsign'] or x['label']:<10} "
          f"{x['distance_mi'] or 0:>4.0f} mi -> {x['reason'][:90]}")
found = [x for x in r["rows"] if x["found"]]
print(f"\nreceived as predicted ({len(found)}), first 5:")
for x in found[:5]:
    print(f"  {x['freq_low_mhz']:>9.4f} MHz {x['callsign'] or x['label']:<10} -> {x['reason'][:70]}")
