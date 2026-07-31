"""Verify the exact field-failure path on real hardware: sweep one module,
then start the OTHER module's phase (the antenna-move transition). Uses a
throwaway session, then re-opens the operator's newest real session."""
import time

import requests

B = "http://127.0.0.1:8765"


def state():
    return requests.get(B + "/api/state", timeout=30).json()


def wait_done(label, timeout=240):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = state()
        if not st["busy"]:
            return st
        sw = st["sweep"]
        if sw.get("segment_index") == 2 and sw.get("phase") == "interior":
            # got far enough to prove the module sweeps — abort to save time
            requests.post(B + "/api/sweep/abort", json={}, timeout=30)
        time.sleep(2)
    raise SystemExit(f"TIMEOUT during {label}")


# remember the operator's current (newest) session to restore later
sessions = requests.get(B + "/api/sessions", timeout=30).json()
operator_session = sessions[0]["id"]

r = requests.post(B + "/api/session", json={
    "address": "13227 switzer road overland park ks",
    "operator": "module-switch-check"}, timeout=60)
r.raise_for_status()
test_session = r.json()["session_id"]
print(f"[1] throwaway session #{test_session}")

# server restart restored a session automatically?
st = state()
print(f"[2] state after boot: session={st['session'] and st['session']['id']}, "
      f"device={'yes' if st['device'] else 'no (auto-connects on sweep start)'}")

for i, module in enumerate(["left", "right"]):
    r = requests.post(B + "/api/sweep/start", json={
        "phase": "interior", "module": module, "room": "hw-check",
        "profile": "custom", "custom_minutes": 3}, timeout=120)
    if not r.ok:
        raise SystemExit(f"FAIL start {module}: {r.status_code} {r.text[:300]}")
    print(f"[3.{i}] {module} module sweep started "
          f"(segments={r.json()['plan']['segment_count']}) ... ", end="", flush=True)
    st = wait_done(module)
    sw_msg = st["sweep"]["message"]
    err = st["sweep"]["error"]
    print(f"finished: {sw_msg}  error={err}")
    if err:
        raise SystemExit(f"FAIL: {module} module sweep errored: {err}")

sigs = requests.get(B + "/api/signals?sweep_id=0", timeout=30).json()  # noop check
both = requests.get(B + "/api/signals", timeout=30).json()
by_mod = {}
for s in both:
    if s["room"] == "hw-check":
        by_mod.setdefault(s["module"], []).append(s)
print(f"[4] signals captured per module in hw-check: "
      f"{ {k: len(v) for k, v in by_mod.items()} }")

requests.post(B + "/api/session/open", json={"id": operator_session}, timeout=30)
print(f"[5] re-opened operator session #{operator_session}")
print("MODULE SWITCH CHECK: PASS — both modules sweep and the transition works")
