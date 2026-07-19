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

def render_capture_svg(data: dict, meta: dict, width=1200, height=560,
                       deidentified: bool = False) -> str:
    """data: {freqs, avg, maxpeak, hits, passes}; meta: annotation fields.

    deidentified=True renders the capture for the public signal library / demo:
    the provenance footer (session #, address, room, module, capture time) is
    dropped entirely and replaced with a neutral de-identified credit line, so
    no source client's location can leak through a published spectrograph.
    """
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
    if deidentified:
        prov = ("Shield Security Systems signal library — de-identified reference "
                "capture")
        credit = ("De-identified reference spectrograph — numeric capture only; "
                  "no client, location, or session data.")
    else:
        prov = (f"{meta.get('captured', '')} · session #{meta.get('session_id', '?')} "
                f"· {meta.get('address', '')} · room: {meta.get('room', '?')} "
                f"· module: {meta.get('module', '?')}")
        credit = ("Shield TSCM internal signal library — high-iteration zoom "
                  "capture (vector; zoom freely)")

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
  <text x='{plot_x}' y='{height - 26}' fill='#6b7b90' font-size='11'>{credit}</text>
</svg>"""


def render_thumb_svg(data: dict, meta: dict, width=240, height=80) -> str:
    """Tiny chrome-free fingerprint for table thumbnails: max-hold filled +
    average line + center marker only. The full render_capture_svg text/grid
    would be illegible at 70 px wide; this stays readable at any size."""
    freqs, avg, maxp = data["freqs"], data["avg"], data["maxpeak"]
    n = len(freqs)
    if n < 2:
        return "<svg xmlns='http://www.w3.org/2000/svg'/>"
    lo = min(min(avg), min(maxp), meta.get("floor_dbm") or 0.0) - 3
    hi = max(maxp) + 3
    if hi - lo < 10:
        hi = lo + 10

    def X(i):
        return width * i / (n - 1)

    def Y(v):
        v = max(lo, min(hi, v))
        return height - 2 - (height - 6) * (v - lo) / (hi - lo)

    def path(arr):
        return "M " + " L ".join(f"{X(i):.1f} {Y(arr[i]):.1f}" for i in range(n))

    fill = path(maxp) + f" L {width} {height} L 0 {height} Z"
    center = meta.get("center_mhz")
    cx = None
    if center is not None and freqs[0] < freqs[-1] and freqs[0] <= center <= freqs[-1]:
        cx = width * (center - freqs[0]) / (freqs[-1] - freqs[0])
    marker = (f"<line x1='{cx:.1f}' y1='0' x2='{cx:.1f}' y2='{height}' "
              f"stroke='#e74c3c' stroke-width='1' stroke-dasharray='3,3' opacity='.8'/>"
              if cx is not None else "")
    return (f"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 {width} {height}' "
            f"preserveAspectRatio='none'>"
            f"<rect width='{width}' height='{height}' fill='#04070c'/>"
            f"<path d='{fill}' fill='rgba(255,208,0,0.14)'/>"
            f"<path d='{path(maxp)}' fill='none' stroke='#ffd000' stroke-width='1.2'/>"
            f"<path d='{path(avg)}' fill='none' stroke='#00c8ff' stroke-width='1'/>"
            f"{marker}</svg>")


# ---------------------------------------------------------------------------
# Auto-archive: every detected signal becomes a wiki observation per sweep
# ---------------------------------------------------------------------------

def _crop_segment(seg_data: dict, center: float, half_span: float):
    """Crop a stored segment trace to a window around a signal — gives a
    per-observation picture rendered from data already collected (no device
    time)."""
    freqs = seg_data.get("freqs") or []
    if not freqs:
        return None
    lo, hi = center - half_span, center + half_span
    idx = [i for i, f in enumerate(freqs) if lo <= f <= hi]
    if len(idx) < 4:  # window too narrow in this segment — use whole segment
        idx = list(range(len(freqs)))
    a, b = idx[0], idx[-1] + 1
    return {"freqs": freqs[a:b], "avg": seg_data["avg"][a:b],
            "maxpeak": seg_data["maxpeak"][a:b], "hits": seg_data["hits"][a:b],
            "passes": seg_data.get("passes", 1)}


def archive_observations(db, cfg, session_id: int, images_dir: str, address: str):
    """Called after a sweep completes. Archives EVERY detected signal this
    session as a lightweight wiki observation (cropped trace image from the
    sweep data + characteristics), so the library grows by hundreds per sweep
    and the same emitter accrues a time series for variance comparison. The
    high-iteration manual zoom capture is separate (kind='zoom')."""
    import os as _os
    _os.makedirs(images_dir, exist_ok=True)
    # only archive signals from sweeps not already observation-archived
    done = {r["signal_id"] for r in db.query(
        "SELECT signal_id FROM captures WHERE session_id=? AND kind='observation' "
        "AND signal_id IS NOT NULL", (session_id,))}
    signals = db.query(
        "SELECT s.*, seg.data_json AS seg_data, seg.low_mhz AS seg_lo, "
        "seg.high_mhz AS seg_hi FROM signals s LEFT JOIN segments seg "
        "ON s.segment_id=seg.id WHERE s.session_id=?", (session_id,))
    n = 0
    for s in signals:
        if s["id"] in done or not s["seg_data"]:
            continue
        try:
            seg = json.loads(s["seg_data"])
        except (TypeError, ValueError):
            continue
        half = max((s["bw_khz"] or 100) / 1000.0 * 5, 0.25)
        crop = _crop_segment(seg, s["center_mhz"], half)
        if crop is None:
            continue
        feats = signal_features(crop, s["floor_dbm"] or -110)
        category = auto_category(s["classification"] or "", s["matched_label"] or "",
                                 s["center_mhz"], bool(s["caution"]))
        label = (s["matched_label"] or f"{s['center_mhz']:.4f} MHz "
                 + ("unverified" if s["classification"] == "UNVERIFIED" else "signal"))
        captured = time.strftime("%Y-%m-%d %H:%M")
        meta = {"center_mhz": s["center_mhz"], "peak_dbm": s["peak_dbm"],
                "floor_dbm": s["floor_dbm"], "bw_khz": s["bw_khz"],
                "persistence": s["persistence"], "label": label,
                "session_id": session_id, "address": address,
                "room": s["room"] or "", "module": s["module"] or "",
                "captured": captured}
        svg = render_capture_svg(crop, meta)
        fname = f"obs_s{session_id}_{int(s['center_mhz'] * 10000)}_{s['id']}.svg"
        with open(_os.path.join(images_dir, fname), "w", encoding="utf-8") as f:
            f.write(svg)
        status = "identified" if s["classification"] == "VERIFIED" else "unconfirmed"
        db.execute(
            "INSERT INTO captures (session_id,signal_id,kind,center_mhz,span_mhz,bw_khz,"
            "peak_dbm,floor_dbm,persistence,room,module,captured_utc,image_path,"
            "data_json,features_json,classification,matched_label,category,label,status,"
            "description) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (session_id, s["id"], "observation", round(s["center_mhz"], 5),
             round(2 * half, 4), s["bw_khz"], s["peak_dbm"], s["floor_dbm"],
             s["persistence"], s["room"] or "", s["module"] or "", time.time(),
             fname, json.dumps(crop), json.dumps(feats),
             s["classification"] or "", s["matched_label"] or "",
             category, label, status, rule_based_description(
                 {"bw_khz": s["bw_khz"], "center_mhz": s["center_mhz"]}, feats)))
        n += 1
    return n


def signal_timeline(db, center_mhz: float, tol_mhz: float = MATCH_TOL_MHZ) -> dict:
    """All observations + zooms of one emitter across every session, oldest
    first, with variance stats — the 'is someone hiding in this band?' view."""
    rows = db.query(
        "SELECT c.id, c.kind, c.center_mhz, c.bw_khz, c.peak_dbm, c.floor_dbm, "
        "c.persistence, c.room, c.module, c.captured_utc, c.image_path, c.label, "
        "c.session_id, s.address FROM captures c JOIN sessions s ON c.session_id=s.id "
        "WHERE ABS(c.center_mhz - ?) <= ? ORDER BY c.captured_utc ASC",
        (center_mhz, tol_mhz))
    peaks = [r["peak_dbm"] for r in rows if r["peak_dbm"] is not None]
    bws = [r["bw_khz"] for r in rows if r["bw_khz"]]
    sessions = {r["session_id"] for r in rows}
    variance = {}
    if peaks:
        variance = {
            "observations": len(rows), "sessions": len(sessions),
            "peak_min_dbm": round(min(peaks), 1), "peak_max_dbm": round(max(peaks), 1),
            "peak_range_db": round(max(peaks) - min(peaks), 1),
            "bw_min_khz": round(min(bws), 1) if bws else None,
            "bw_max_khz": round(max(bws), 1) if bws else None,
            "first_seen": rows[0]["captured_utc"], "last_seen": rows[-1]["captured_utc"],
        }
        # variance flag: a "stable" allocation whose amplitude or bandwidth swings
        # widely over time can be a transmitter masquerading in a known band
        if variance["peak_range_db"] >= 20:
            variance["flag"] = ("amplitude varies >20 dB across sightings — inconsistent "
                                "with a fixed licensed source; possible intermittent or "
                                "mobile transmitter in this band")
        elif bws and max(bws) / max(min(bws), 1) >= 3:
            variance["flag"] = ("bandwidth varies >3x across sightings — possible second "
                                "emitter sharing this frequency neighborhood")
    return {"rows": rows, "variance": variance}


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
