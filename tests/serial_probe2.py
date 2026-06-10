"""Probe C2-F variants to find what fw 03.45 on the 6G Combo PLUS honors,
and validate $S frame alignment. Run with the app server stopped."""
import sys
import time

import serial

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM3"
ser = serial.Serial(PORT, 500000, timeout=0.3)
time.sleep(0.5)
ser.reset_input_buffer()
BUF = b""


def send(cmd: bytes):
    ser.write(b"#" + bytes([len(cmd) + 2]) + cmd)


def read_config(timeout=5.0):
    """Send C0 and parse the #C2-F reply. Returns dict or None."""
    global BUF
    send(b"C0")
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
            line = BUF[i:j]
            BUF = BUF[j + 2:]
            p = line[6:].decode(errors="replace").split(",")
            try:
                start, step_hz, points = float(p[0]), float(p[1]), int(p[4])
                end = start + step_hz * (points - 1) / 1000.0
                return {"start_khz": start, "step_hz": step_hz, "points": points,
                        "end_khz": round(end, 1), "raw": line.decode(errors='replace')[:90]}
            except (ValueError, IndexError):
                return {"raw": line.decode(errors='replace')[:90]}
    return None


print("current:", read_config())

variants = [
    (b"C2-F:0090100,0106100", "7-digit kHz pair (classic)"),
    (b"C2-F:0090100,0106100,0112", "kHz pair + points"),
    (b"C3-F:0090100,0106100", "C3-F variant"),
]
for cmd, label in variants:
    send(cmd)
    time.sleep(1.0)
    cfg = read_config()
    print(f"{label}: -> {cfg}")

# frame alignment check on whatever range is now set
BUF = b""
ser.reset_input_buffer()
good = bad = 0
t_end = time.time() + 5
while time.time() < t_end:
    chunk = ser.read(4096)
    if not chunk:
        continue
    BUF += chunk
    while True:
        i = BUF.find(b"$S")
        if i < 0 or len(BUF) < i + 3:
            break
        n = BUF[i + 2]
        if len(BUF) < i + 3 + n + 2:
            break
        if BUF[i + 3 + n:i + 3 + n + 2] == b"\r\n":
            frame = BUF[i + 3:i + 3 + n]
            good += 1
            if good == 3:
                amps = [-(b / 2.0) for b in frame]
                print(f"frame sample: n={n} min={min(amps)} max={max(amps)} dBm")
            BUF = BUF[i + 3 + n + 2:]
        else:
            bad += 1
            BUF = BUF[i + 2:]
print(f"frames: {good} aligned, {bad} false-start rejected")
ser.close()
