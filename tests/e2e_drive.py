"""End-to-end smoke drive against a running app instance (simulator mode).
Not a unit test — exercises session -> baseline -> ambient -> two rooms ->
anomalies -> report via the HTTP API, printing checkpoints."""

import sys
import time

import requests

BASE = "http://127.0.0.1:8765"


def post(path, body=None):
    r = requests.post(BASE + path, json=body or {}, timeout=120)
    if not r.ok:
        raise SystemExit(f"FAIL {path}: {r.status_code} {r.text[:300]}")
    return r.json()


def get(path):
    r = requests.get(BASE + path, timeout=60)
    r.raise_for_status()
    return r.json()


def wait_idle(what, timeout=600):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = get("/api/state")
        if not st["busy"] and not st["baseline"]["running"]:
            return st
        time.sleep(2)
    raise SystemExit(f"TIMEOUT waiting for {what}")


def main():
    dev = post("/api/device/connect")
    print(f"[1] device: {dev['model']} fw {dev['firmware']} "
          f"modules={[(m['side'], m['min_mhz'], m['max_mhz']) for m in dev['modules']]}")

    s = post("/api/session", {"address": "300 E High St, Jefferson City, MO 65101",
                              "operator": "smoke-test"})
    print(f"[2] session #{s['session_id']} via {s['geocode']['geocoder']}")

    post("/api/baseline/build")
    st = wait_idle("baseline")
    bl = get("/api/baseline")
    by_src = {x["source"]: (x["status"], x["count"]) for x in bl["sources"]}
    print(f"[3] baseline: {bl['count']} rows, degraded={bl['degraded']}, sources={by_src}")
    assert by_src.get("FCC TV", ("", 0))[1] > 0, "FCC TV should return stations now"

    for phase, module, room in [("ambient", "left", "exterior"),
                                ("interior", "left", "Office"),
                                ("interior", "left", "Master Bedroom")]:
        r = post("/api/sweep/start", {"phase": phase, "module": module, "room": room,
                                      "profile": "custom", "custom_minutes": 2})
        print(f"[4] {phase}/{module}/{room}: sweep #{r['sweep_id']} "
              f"segments={r['plan']['segment_count']} "
              f"est={r['plan']['estimated_minutes']}min ... ", end="", flush=True)
        st = wait_idle(phase)
        print("done:", st["sweep"]["message"])

    anomalies = get("/api/anomalies")
    print(f"[5] anomalies: {len(anomalies)}")
    for a in anomalies[:6]:
        print(f"    {a['center_mhz']:.3f} MHz score={a['anomaly_score']} "
              f"class={a['classification']} room={a['strongest_room']} "
              f"in/out={a['interior_delta_db']} loc={a['localization_score']}")
    assert any(abs(a["center_mhz"] - 611.0) < 1.0 and a["classification"] == "UNVERIFIED"
               for a in anomalies[:3]), \
        "planted 611.0 MHz unverified carrier should rank in top anomalies"
    assert any(abs(a["center_mhz"] - 433.92) < 0.6 for a in anomalies), \
        "planted 433.92 MHz ISM device should be flagged"
    office = [a for a in anomalies if a["strongest_room"] == "Office"]
    assert office, "localization should point at the Office"

    rooms = get("/api/rooms")
    print(f"[6] room groups: {len(rooms['groups'])} across rooms {rooms['rooms']}")

    rep = post("/api/report/generate")
    print(f"[7] report: {rep['kind']} -> {rep['filename']} (simulated={rep['simulated']})")
    csv = requests.get(BASE + "/api/export/csv", timeout=60)
    print(f"[8] csv export: {csv.status_code}, {len(csv.content)} bytes")
    print("E2E SMOKE: ALL CHECKS PASSED")


if __name__ == "__main__":
    sys.exit(main())
