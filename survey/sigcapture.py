"""Signal capture library ("the wiki").

For every signal worth documenting, the capture engine zooms the analyzer onto
it (narrow span = maximum RBW), runs a high-iteration AVERAGE pass plus a long
MAX HOLD dwell, and saves a high-resolution SVG fingerprint of the signal along
with the raw accumulator data, provenance (session/address/room/time), and
characteristics. Entries are categorized, browsable across sessions, and
matched: when a new capture lands within tolerance of an entry documented in a
previous session, the match is surfaced ("seen before at ...").

Descriptions: rule-based always; optionally enriched by the Claude API when
api_keys.anthropic is configured (the numeric signal profile is sent — never
client or address data).
"""

import json
import logging
import os
import time

log = logging.getLogger("survey.sigcapture")

MATCH_TOL_MHZ = 0.25
MATCH_BW_RATIO = 3.0      # bandwidths within 3x of each other can be the same emitter

CARRIERS = ("AT&T", "T-Mobile", "Verizon", "FirstNet", "US Cellular", "DISH")


def auto_category(classification: str, matched_label: str, center_mhz: float,
                  caution: bool) -> str:
    label = (matched_label or "").lower()
    for carrier in CARRIERS:
        if carrier.lower() in label:
            return f"Cellular — {carrier}"
    if "uplink" in label or "downlink" in label or "tdd" in label:
        return "Cellular — unattributed"
    if any(k in label for k in ("fm", "am ", "tv", "broadcast", "noaa")):
        return "Broadcast"
    if "dect" in label:
        return "DECT"
    if "gps" in label:
        return "GPS band (suspect)"
    if any(k in label for k in ("wifi", "u-nii", "ism", "bluetooth", "2.4", "keyfob")):
        return "ISM / WiFi / Bluetooth"
    if any(k in label for k in ("land mobile", "marine", "murs", "frs", "gmrs",
                                "paging", "public safety", "federal")):
        return "Land mobile / public safety"
    if "ham" in label or "amateur" in label:
        return "Amateur"
    if classification == "VERIFIED":
        return "Verified other"
    return "Unknown — unconfirmed"


# ---------------------------------------------------------------------------
# SVG fingerprint rendering (pure python, vector = unlimited zoom)
# ---------------------------------------------------------------------------

def render_capture_svg(data: dict, meta: dict, width=1200, height=560) -> str:
    """data: {freqs, avg, maxpeak, hits, passes}; meta: annotation fields."""
    freqs, avg, maxp = data["freqs"], data["avg"], data["maxpeak"]
    n = len(freqs)
    if n < 2:
        return "<svg xmlns='http://www.w3.org/2000/svg'/>"
    plot_x, plot_y, plot_w, plot_h = 70, 70, width - 100, height - 170
    a_min, a_max = -120.0, -10.0

    def X(i):
        return plot_x + plot_w * i / (n - 1)

    def Y(v):
        v = max(a_min, min(a_max, v))
        return plot_y + plot_h * (1 - (v - a_min) / (a_max - a_min))

    def path(arr):
        return "M " + " L ".join(f"{X(i):.1f} {Y(arr[i]):.1f}" for i in range(len(arr)))

    grid = []
    for g in range(-110, -19, 10):
        y = Y(g)
        grid.append(f"<line x1='{plot_x}' y1='{y:.1f}' x2='{plot_x + plot_w}' y2='{y:.1f}' "
                    f"stroke='#26303d' stroke-dasharray='2,5'/>"
                    f"<text x='{plot_x - 8}' y='{y + 3:.1f}' fill='#6b7b90' font-size='11' "
                    f"text-anchor='end'>{g}</text>")
    for k in range(11):
        x = plot_x + plot_w * k / 10
        f_mhz = freqs[0] + (freqs[-1] - freqs[0]) * k / 10
        grid.append(f"<line x1='{x:.1f}' y1='{plot_y}' x2='{x:.1f}' y2='{plot_y + plot_h}' "
                    f"stroke='#26303d' stroke-dasharray='2,5'/>"
                    f"<text x='{x:.1f}' y='{plot_y + plot_h + 16}' fill='#6b7b90' "
                    f"font-size='11' text-anchor='middle'>{f_mhz:.4f}</text>")

    # filled realtime-style area under max trace + center marker
    fill_path = path(maxp) + f" L {X(n - 1):.1f} {Y(a_min):.1f} L {X(0):.1f} {Y(a_min):.1f} Z"
    center = meta.get("center_mhz")
    cx = None
    if center is not None and freqs[0] <= center <= freqs[-1]:
        cx = plot_x + plot_w * (center - freqs[0]) / (freqs[-1] - freqs[0])

    info_left = (f"{meta.get('center_mhz', 0):.4f} MHz · span {freqs[0]:.4f}–{freqs[-1]:.4f} MHz "
                 f"· RBW {(freqs[1] - freqs[0]) * 1000:.1f} kHz · {data.get('passes', 0)} passes")
    info_right = (f"peak {meta.get('peak_dbm', 0):.1f} dBm · floor {meta.get('floor_dbm', 0):.1f} dBm "
                  f"· bw ~{meta.get('bw_khz', 0):.0f} kHz · persistence "
                  f"{(meta.get('persistence') or 0) * 100:.0f}%")
    prov = (f"{meta.get('captured', '')} · session #{meta.get('session_id', '?')} "
            f"· {meta.get('address', '')} · room: {meta.get('room', '?')} "
            f"· module: {meta.get('module', '?')}")

    return f"""<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 {width} {height}'
     font-family='Consolas,monospace'>
  <rect width='{width}' height='{height}' fill='#04070c'/>
  <text x='{plot_x}' y='28' fill='#e8eef5' font-size='17' font-weight='bold'>{meta.get('label', 'SIGNAL CAPTURE')}</text>
  <text x='{plot_x}' y='48' fill='#8fa0b8' font-size='12'>{info_left}</text>
  <text x='{plot_x + plot_w}' y='48' fill='#8fa0b8' font-size='12' text-anchor='end'>{info_right}</text>
  <rect x='{plot_x}' y='{plot_y}' width='{plot_w}' height='{plot_h}' fill='#000208' stroke='#26303d'/>
  {''.join(grid)}
  <path d='{fill_path}' fill='rgba(255,208,0,0.10)'/>
  <path d='{path(maxp)}' fill='none' stroke='#ffd000' stroke-width='1.4'/>
  <path d='{path(avg)}' fill='none' stroke='#00c8ff' stroke-width='1.4'/>
  {f"<line x1='{cx:.1f}' y1='{plot_y}' x2='{cx:.1f}' y2='{plot_y + plot_h}' stroke='#e74c3c' stroke-width='1' stroke-dasharray='6,4'/>" if cx else ''}
  <rect x='{plot_x + plot_w - 200}' y='{plot_y + 10}' width='190' height='40' fill='rgba(4,7,12,0.8)'/>
  <line x1='{plot_x + plot_w - 190}' y1='{plot_y + 24}' x2='{plot_x + plot_w - 168}' y2='{plot_y + 24}' stroke='#ffd000' stroke-width='2'/>
  <text x='{plot_x + plot_w - 162}' y='{plot_y + 28}' fill='#ffd000' font-size='11'>MAX HOLD</text>
  <line x1='{plot_x + plot_w - 190}' y1='{plot_y + 40}' x2='{plot_x + plot_w - 168}' y2='{plot_y + 40}' stroke='#00c8ff' stroke-width='2'/>
  <text x='{plot_x + plot_w - 162}' y='{plot_y + 44}' fill='#00c8ff' font-size='11'>AVERAGE</text>
  <text x='{plot_x}' y='{height - 44}' fill='#6b7b90' font-size='11'>{prov}</text>
  <text x='{plot_x}' y='{height - 26}' fill='#6b7b90' font-size='11'>Shield TSCM internal signal library — high-iteration zoom capture (vector; zoom freely)</text>
</svg>"""


# ---------------------------------------------------------------------------
# Characteristics + descriptions
# ---------------------------------------------------------------------------

def signal_features(data: dict, floor_dbm: float) -> dict:
    """Numeric shape profile used for descriptions and matching."""
    maxp, avg, hits = data["maxpeak"], data["avg"], data["hits"]
    passes = max(data.get("passes", 1), 1)
    n = len(maxp)
    hot = [i for i in range(n) if maxp[i] >= floor_dbm + 9]
    # count distinct peaks (local maxima above threshold, separated by >2 bins)
    peaks = []
    for i in hot:
        if (i == 0 or maxp[i] >= maxp[i - 1]) and (i == n - 1 or maxp[i] >= maxp[i + 1]):
            if not peaks or i - peaks[-1] > 2:
                peaks.append(i)
    avg_max_gap = (sum(maxp[i] - avg[i] for i in hot) / len(hot)) if hot else 0.0
    duty = (sum(hits[i] for i in hot) / (len(hot) * passes)) if hot else 0.0
    return {
        "snr_db": round((max(maxp) - floor_dbm), 1) if maxp else 0,
        "distinct_peaks": len(peaks),
        "occupied_fraction": round(len(hot) / n, 3),
        "avg_vs_maxhold_gap_db": round(avg_max_gap, 1),
        "duty_cycle": round(duty, 3),
    }


def rule_based_description(cap: dict, feats: dict) -> str:
    bits = []
    duty = feats.get("duty_cycle", 0)
    gap = feats.get("avg_vs_maxhold_gap_db", 0)
    if duty > 0.8 and gap < 6:
        bits.append("Continuous carrier — present on essentially every sweep with the "
                    "average tracking max-hold closely; consistent with a steady "
                    "transmitter (analog bug, beacon, telemetry, or broadcast).")
    elif duty < 0.25 or gap > 12:
        bits.append("Bursty/intermittent — max-hold captures energy the average doesn't, "
                    "consistent with packetized digital traffic, a frequency hopper, or a "
                    "duty-cycled (VOX/timer) transmitter.")
    else:
        bits.append("Intermittent carrier with moderate duty cycle.")
    if feats.get("distinct_peaks", 1) > 2:
        bits.append(f"{feats['distinct_peaks']} distinct peaks in the zoom span — possible "
                    "multi-carrier signal, modulation sidebands, or several emitters.")
    bw = cap.get("bw_khz") or 0
    if bw < 25:
        bits.append("Very narrowband (<25 kHz) — NFM voice or low-rate telemetry class.")
    elif bw > 1000:
        bits.append("Wideband (>1 MHz) — digital/video class emission.")
    bits.append(f"SNR {feats.get('snr_db', 0):.0f} dB above the local floor at capture time.")
    return " ".join(bits)


def ai_description(cfg, cap: dict, feats: dict) -> tuple[str, str]:
    """Returns (description, error). Uses the Claude API when configured; the
    payload is the numeric signal profile only — no client/address data."""
    api_key = cfg.get("api_keys", "anthropic", default="") or os.environ.get(
        "ANTHROPIC_API_KEY", "")
    if not api_key:
        return "", ("No Anthropic API key configured (api_keys.anthropic) — "
                    "using rule-based description")
    try:
        import anthropic
    except ImportError:
        return "", "anthropic SDK not installed (pip install anthropic)"

    profile = {
        "center_mhz": cap["center_mhz"], "bandwidth_khz": cap.get("bw_khz"),
        "peak_dbm": cap.get("peak_dbm"), "floor_dbm": cap.get("floor_dbm"),
        "persistence": cap.get("persistence"),
        "classification": cap.get("classification"),
        "nearest_known": cap.get("matched_label"),
        "shape": feats,
    }
    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-opus-4-8",
            max_tokens=1024,
            system=("You are an RF signal analyst supporting a TSCM (technical "
                    "surveillance counter-measures) sweep team. Given a numeric "
                    "spectrum-capture profile, write a tight 3-5 sentence technical "
                    "description of the signal: emission class, likely source "
                    "candidates (licensed service vs consumer device vs potential "
                    "surveillance device), what the duty cycle and shape imply, and "
                    "what a field tech should do next to confirm. Be concrete and "
                    "calibrated; say when something is ambiguous."),
            messages=[{"role": "user",
                       "content": json.dumps(profile, indent=1)}],
        )
        text = next((b.text for b in response.content if b.type == "text"), "").strip()
        return text, ""
    except Exception as e:  # noqa: BLE001 - offline in the field is normal
        return "", f"Claude API call failed: {e}"


# ---------------------------------------------------------------------------
# Matching across the library
# ---------------------------------------------------------------------------

def find_matches(db, capture: dict, exclude_session: int | None = None) -> list[dict]:
    rows = db.query(
        "SELECT c.*, s.address FROM captures c JOIN sessions s ON c.session_id=s.id "
        "WHERE ABS(c.center_mhz - ?) <= ? AND c.id != ?",
        (capture["center_mhz"], MATCH_TOL_MHZ, capture.get("id", -1)))
    out = []
    for r in rows:
        if exclude_session and r["session_id"] == exclude_session:
            continue
        bw_a, bw_b = capture.get("bw_khz") or 100, r["bw_khz"] or 100
        if max(bw_a, bw_b) / max(min(bw_a, bw_b), 1) > MATCH_BW_RATIO:
            continue
        out.append({"capture_id": r["id"], "session_id": r["session_id"],
                    "address": r["address"], "room": r["room"],
                    "center_mhz": r["center_mhz"], "label": r["label"],
                    "category": r["category"], "status": r["status"],
                    "captured_utc": r["captured_utc"]})
    return out
