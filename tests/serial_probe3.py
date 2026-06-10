"""Probe: does 4-field C2-F apply, and how long after a module switch does the
module accept commands? Run with the app server stopped."""
import sys
import time

import serial

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM3"
ser = serial.Serial(PORT, 500000, timeout=0.3)
time.sleep(0.5)
ser.reset_input_buffer()
BUF = b""


def send(cmd: str):
    ser.write(b"#" + bytes([len(cmd) + 2]) + cmd.encode("latin-1"))


def get_config(timeout=4.0, request=True):
    global BUF
    if request:
        send("C0")
    t_end = time.time() + timeout
    while time.time() < t_end:
        chunk = ser.read(4096)
        if chunk:
            BUF += chunk
        i = BUF.find(b"#C2-F:")
        if i >= 0:
            j = BUF.find(b"\r\n", i)
            if j < 0:
                continue
            line, BUF = BUF[i:j], BUF[j + 2:]
            p = line[6:].decode(errors="replace").split(",")
            try:
                start, step, pts = float(p[0]), float(p[1]), int(p[4])
                exp = p[5].strip()
                return {"start": start, "end": round(start + step * (pts - 1) / 1000, 1),
                        "pts": pts, "exp_active": exp}
            except (ValueError, IndexError):
                return {"raw": line[:80]}
    return None


print("0. current config:", get_config())

print("1. switch to mainboard (CM 0) ...")
send("CM" + "\x00")
time.sleep(4)            # generous boot settle
BUF = b""
ser.reset_input_buffer()
print("   after switch:", get_config())

print("2. send 4-field C2-F 90.1-106.1 ...")
send("C2-F:0090100,0106100,0000,-120")
for k in range(8):
    time.sleep(1.0)
    cfg = get_config()
    print(f"   t+{k + 1}s:", cfg)
    if cfg and cfg.get("start") == 90100.0 and abs(cfg.get("end", 0) - 106100) < 200:
        print("   -> APPLIED")
        break

print("3. send another 4-field C2-F 430-440 (no switch) ...")
send("C2-F:0430000,0440000,0000,-120")
time.sleep(1.5)
print("   result:", get_config())

ser.close()
