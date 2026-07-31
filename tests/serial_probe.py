"""Direct serial probe: see exactly what the unit sends after C0 / C2-F / C+.
Run with the app server STOPPED (COM port is exclusive)."""
import sys
import time

import serial

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM3"
ser = serial.Serial(PORT, 500000, timeout=0.3)
time.sleep(0.5)
ser.reset_input_buffer()


def send(cmd: str):
    payload = b"#" + bytes([len(cmd) + 2]) + cmd.encode("latin-1")
    ser.write(payload)
    print(f"\n>>> sent {cmd!r} as {payload!r}")


def dump(seconds: float, label: str):
    print(f"--- listening {seconds}s ({label}) ---")
    t_end = time.time() + seconds
    buf = b""
    text_lines, sweep_frames = [], 0
    while time.time() < t_end:
        chunk = ser.read(4096)
        if not chunk:
            continue
        buf += chunk
        # pull out $S frames so they don't drown the text
        while True:
            i = buf.find(b"$S")
            if i < 0 or len(buf) < i + 3:
                break
            n = buf[i + 2]
            if len(buf) < i + 3 + n + 2:
                break
            pre = buf[:i]
            if pre.strip():
                text_lines.append(pre)
            sweep_frames += 1
            buf = buf[i + 3 + n + 2:]
        # also surface complete text lines
        while b"\r\n" in buf and not buf.startswith(b"$S"):
            line, buf = buf.split(b"\r\n", 1)
            if line.strip():
                text_lines.append(line)
    for ln in text_lines[:25]:
        print("TEXT:", ln[:160])
    print(f"(+ {sweep_frames} sweep $S frames)")


send("C0")
dump(4, "after C0 config request")
send("C2-F:0090100,0106100")
dump(6, "after C2-F range 90.1-106.1 MHz")
send("C+" + "\x02")
dump(3, "after C+ calculator avg")
send("C+" + "\x00")
dump(2, "after C+ normal")
ser.close()
print("done")
