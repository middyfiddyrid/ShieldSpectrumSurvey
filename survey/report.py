"""Client-facing PDF report — on demand, mirroring the screen.

The report renders from the SAME database tables every UI endpoint reads, at
the moment the GENERATE REPORT button is clicked: same baseline table, same
per-module detected-signal tables, same room comparison grid, same anomaly
scores. No separately-composed narrative. If any sweep phase is still missing
or running, the report is watermarked INTERIM and covers completed phases only.
Every generation is timestamped and archived in the session.
"""

import json
import os
import time

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (Flowable, Image, PageBreak, Paragraph,
                                SimpleDocTemplate, Spacer, Table, TableStyle)

PAGE_W, PAGE_H = letter

C_BG = colors.HexColor("#11161c")
C_ACCENT = colors.HexColor("#27ae60")
C_WARN = colors.HexColor("#e67e22")
C_BAD = colors.HexColor("#c0392b")

CLASS_COLOR = {"VERIFIED": colors.HexColor("#1e8449"),
               "PLAUSIBLE": colors.HexColor("#b9770e"),
               "UNVERIFIED": colors.HexColor("#922b21")}

BAND_COLORS = {"broadcast": "#2e86c1", "land_mobile": "#7d3c98", "ism": "#e67e22",
               "cellular": "#16a085", "aviation": "#5d6d7e", "gps": "#c0392b",
               "ham": "#7f8c8d", "other": "#566573"}


def _styles():
    ss = getSampleStyleSheet()
    ss.add(ParagraphStyle("Cover", fontName="Helvetica-Bold", fontSize=22, leading=26))
    ss.add(ParagraphStyle("H1x", fontName="Helvetica-Bold", fontSize=14, leading=18,
                          spaceBefore=14, spaceAfter=6))
    ss.add(ParagraphStyle("Mono", fontName="Courier", fontSize=8, leading=10))
    ss.add(ParagraphStyle("Cell", fontName="Helvetica", fontSize=7.5, leading=9))
    ss.add(ParagraphStyle("Body", fontName="Helvetica", fontSize=9.5, leading=13,
                          spaceAfter=6))
    return ss


class SpectrumStrip(Flowable):
    """Frequency strip: baseline allocations as colored bands, detected peaks overlaid."""

    def __init__(self, baseline_rows, signals, lo_mhz=0.0, hi_mhz=6100.0,
                 width=7.0 * inch, height=0.9 * inch):
        super().__init__()
        self.baseline = baseline_rows
        self.signals = signals
        self.lo, self.hi = lo_mhz, max(hi_mhz, lo_mhz + 1)
        self.width, self.height = width, height

    def _x(self, f):
        return max(0.0, min(1.0, (f - self.lo) / (self.hi - self.lo))) * self.width

    def draw(self):
        c = self.canv
        band_h = self.height * 0.45
        c.setFillColor(colors.HexColor("#1b2129"))
        c.rect(0, 0, self.width, band_h, stroke=0, fill=1)
        for r in self.baseline:
            color = BAND_COLORS.get(r.get("category") or "other", "#566573")
            x0, x1 = self._x(r["freq_low_mhz"]), self._x(r["freq_high_mhz"])
            c.setFillColor(colors.HexColor(color))
            c.rect(x0, 0, max(x1 - x0, 0.6), band_h, stroke=0, fill=1)
        for s in self.signals:
            x = self._x(s["center_mhz"])
            color = CLASS_COLOR.get(s.get("classification") or "UNVERIFIED", C_BAD)
            c.setStrokeColor(color)
            c.setLineWidth(1.1)
            c.line(x, band_h + 2, x, self.height - 8)
            c.setFillColor(color)
            c.circle(x, self.height - 6, 1.6, stroke=0, fill=1)
        c.setFillColor(colors.HexColor("#444c56"))
        c.setFont("Helvetica", 6)
        for ghz in range(0, int(self.hi / 1000) + 1):
            x = self._x(ghz * 1000)
            c.drawCentredString(x, band_h + 1, f"{ghz}G")


def _watermark_and_footer(interim: bool, company: str):
    def fn(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#666666"))
        canvas.drawString(0.75 * inch, 0.5 * inch, company)
        canvas.drawRightString(PAGE_W - 0.75 * inch, 0.5 * inch, f"Page {doc.page}")
        if interim:
            canvas.setFont("Helvetica-Bold", 60)
            canvas.setFillColorRGB(0.85, 0.3, 0.2, alpha=0.13)
            canvas.translate(PAGE_W / 2, PAGE_H / 2)
            canvas.rotate(45)
            canvas.drawCentredString(0, 0, "INTERIM")
        canvas.restoreState()
    return fn


def _fmt_freq(mhz):
    if mhz is None:
        return ""
    if mhz < 1:
        return f"{mhz * 1000:.1f} kHz"
    if mhz >= 1000:
        return f"{mhz / 1000:.4f} GHz"
    return f"{mhz:.4f} MHz"


def _table(data, col_widths, header_bg=C_BG, font_size=7.5):
    t = Table(data, colWidths=col_widths, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), header_bg),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), font_size),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#aab2bd")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f2f4f7")]),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    return t


def generate_report(db, cfg, session_id: int, reports_dir: str) -> dict:
    ss = _styles()
    session = db.query_one("SELECT * FROM sessions WHERE id=?", (session_id,))
    if not session:
        raise ValueError("no such session")
    baseline = db.query("SELECT * FROM baseline WHERE session_id=? ORDER BY freq_low_mhz",
                        (session_id,))
    sources = db.query("SELECT * FROM source_status WHERE session_id=?", (session_id,))
    sweeps = db.query("SELECT * FROM sweeps WHERE session_id=? ORDER BY id", (session_id,))
    signals = db.query("SELECT * FROM signals WHERE session_id=? ORDER BY center_mhz",
                       (session_id,))
    directional = db.query("SELECT * FROM directional WHERE session_id=? ORDER BY ts_utc",
                           (session_id,))
    device = json.loads(session["device_info"]) if session["device_info"] else None

    running = any(s["status"] == "running" for s in sweeps)
    has_interior = any(s["phase"] == "interior" and s["status"] in ("done", "aborted")
                       for s in sweeps)
    interim = running or not has_interior
    simulated = any(s["simulated"] for s in sweeps)
    kind = "INTERIM" if interim else "FINAL"

    ts = time.strftime("%Y%m%d_%H%M%S")
    os.makedirs(reports_dir, exist_ok=True)
    path = os.path.join(reports_dir, f"session{session_id}_{kind.lower()}_{ts}.pdf")

    company = cfg.get("branding", "company", default="")
    doc = SimpleDocTemplate(path, pagesize=letter, topMargin=0.7 * inch,
                            bottomMargin=0.8 * inch, leftMargin=0.75 * inch,
                            rightMargin=0.75 * inch,
                            title=f"RF Spectrum Survey — {session['address']}")
    el = []

    # ---- Cover block ----
    logo = cfg.get("branding", "logo_path", default="")
    if logo and os.path.exists(logo):
        try:
            el.append(Image(logo, width=2.2 * inch, height=0.9 * inch, kind="proportional"))
            el.append(Spacer(1, 10))
        except Exception:  # noqa: BLE001 - bad logo file renders text-only
            pass
    el.append(Paragraph(company or "RF Spectrum Survey", ss["Cover"]))
    el.append(Paragraph("Technical Surveillance Counter-Measures — RF Spectrum Survey",
                        ss["Body"]))
    el.append(Spacer(1, 16))
    swept = swept_range_label(sweeps, device)
    cover_rows = [
        ["Location", session["address"]],
        ["Date / time", time.strftime("%B %d, %Y  %H:%M", time.localtime(session["created_utc"]))],
        ["Operator", session["operator"] or cfg.get("branding", "operator", default="") or "—"],
        ["Swept range (as reported by hardware)", swept],
        ["Analyzer", (device or {}).get("model", "—") + "  fw " +
         (device or {}).get("firmware", "—") if device else "—"],
        ["Report status", kind + ("  •  covers completed phases only" if interim else "")],
    ]
    if simulated:
        cover_rows.append(["DATA SOURCE", "SIMULATED DEVICE — NOT A REAL SWEEP. "
                                          "Demo/diagnostic output only."])
    t = Table(cover_rows, colWidths=[2.3 * inch, 4.7 * inch])
    t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, colors.HexColor("#cccccc")),
    ]))
    el.append(t)
    el.append(PageBreak())

    # ---- Methodology summary ----
    el.append(Paragraph("Methodology", ss["H1x"]))
    for para in (
        "A database-verified baseline of all RF emissions expected at this location was "
        "assembled before sweeping, from FCC broadcast and license records, observed cellular "
        "infrastructure, and the national frequency allocation table. The baseline was reviewed "
        "on-screen prior to the survey.",
        "The premises were swept with a calibrated spectrum analyzer across the full range "
        "reported by the hardware (see cover). Each frequency segment received a layered "
        "capture: an averaging pass to recover weak continuous carriers from the noise floor, "
        "and a max-hold dwell pass to capture burst and frequency-hopping transmitters. Dwell "
        "time was weighted toward bands in which surveillance devices commonly operate "
        "(433/915 MHz ISM, 2.4/5.8 GHz, DECT, cellular uplink blocks).",
        "An exterior ambient reference sweep allows interior signals to be compared against "
        "the outside RF environment: a signal meaningfully stronger inside the premises than "
        "outside is locally sourced. Where multiple rooms were swept, per-room amplitude "
        "comparison localizes a source to the room in which it is strongest.",
        "Every detected signal was correlated against the baseline and classified VERIFIED "
        "(matches a specific licensed record), PLAUSIBLE (falls within a known allocation; "
        "flagged with caution in bands where covert devices commonly hide), or UNVERIFIED "
        "(no database or allocation match — candidate anomalies for physical investigation). "
        "This survey is receive-only; no transmissions were made.",
    ):
        el.append(Paragraph(para, ss["Body"]))

    # ---- Baseline ----
    el.append(Paragraph("Expected-Spectrum Baseline", ss["H1x"]))
    src_data = [["Source", "Status", "Entries", "Notes"]]
    for s in sources:
        src_data.append([s["source"], s["status"].upper(), str(s["count"] or 0),
                         Paragraph(s["message"] or "", ss["Cell"])])
    el.append(_table(src_data, [1.5 * inch, 0.9 * inch, 0.7 * inch, 3.9 * inch]))
    el.append(Spacer(1, 8))

    base_data = [["Frequency", "Service", "Callsign / Label", "Licensee",
                  "Dist (mi)", "Brg", "Source", "Confidence"]]
    for r in baseline:
        if r["freq_low_mhz"] == r["freq_high_mhz"]:
            f = _fmt_freq(r["freq_low_mhz"])
        else:
            f = f"{_fmt_freq(r['freq_low_mhz'])} – {_fmt_freq(r['freq_high_mhz'])}"
        base_data.append([
            Paragraph(f, ss["Cell"]), r["service"] or "",
            Paragraph((r["callsign"] or r["label"] or "")[:60], ss["Cell"]),
            Paragraph((r["licensee"] or "")[:40], ss["Cell"]),
            f"{r['distance_mi']:.1f}" if r["distance_mi"] is not None else "",
            f"{r['bearing_deg']:.0f}°" if r["bearing_deg"] is not None else "",
            r["source"], r["confidence"]])
    el.append(_table(base_data, [1.25 * inch, 0.55 * inch, 1.55 * inch, 1.25 * inch,
                                 0.5 * inch, 0.4 * inch, 0.85 * inch, 0.65 * inch]))

    # ---- Spectrum strip ----
    el.append(Paragraph("Spectrum Overview", ss["H1x"]))
    hi = 6100.0
    if device and device.get("modules"):
        hi = max(m["max_mhz"] for m in device["modules"])
    el.append(SpectrumStrip(baseline, [s for s in signals if s["phase"] != "ambient"],
                            0.0, hi))
    el.append(Paragraph(
        "Colored bands: expected allocations by category. Vertical markers: detected "
        "signals (green = verified, amber = plausible, red = unverified).", ss["Cell"]))

    # ---- Detected signals per module ----
    for module in ("left", "right", "single"):
        mod_signals = [s for s in signals if s["module"] == module and s["phase"] != "ambient"]
        if not mod_signals:
            continue
        el.append(Paragraph(f"Detected Signals — {module.upper()} module/port", ss["H1x"]))
        rows = [["Frequency", "Peak dBm", "BW kHz", "Persist", "Class", "Match / reason",
                 "Room"]]
        for s in sorted(mod_signals, key=lambda x: x["center_mhz"]):
            rows.append([
                _fmt_freq(s["center_mhz"]), f"{s['peak_dbm']:.0f}",
                f"{s['bw_khz']:.0f}" if s["bw_khz"] else "",
                f"{(s['persistence'] or 0) * 100:.0f}%",
                s["classification"] or "",
                Paragraph(((s["matched_label"] or "") + " — " +
                           (s["match_reason"] or ""))[:220], ss["Cell"]),
                s["room"] or ""])
        el.append(_table(rows, [1.0 * inch, 0.6 * inch, 0.55 * inch, 0.5 * inch,
                                0.8 * inch, 2.6 * inch, 0.95 * inch]))

    # ---- Room comparison ----
    rooms = sorted({s["room"] for s in signals if s["phase"] == "interior" and s["room"]})
    if len(rooms) > 1:
        el.append(Paragraph("Room Comparison (peak dBm by room)", ss["H1x"]))
        anomalous = [s for s in signals if s["phase"] == "interior"
                     and (s["classification"] != "VERIFIED" or s["caution"])]
        freq_keys = sorted({round(s["center_mhz"], 3) for s in anomalous})
        rows = [["Frequency"] + rooms]
        for fk in freq_keys[:60]:
            row = [_fmt_freq(fk)]
            for room in rooms:
                vals = [s["peak_dbm"] for s in anomalous
                        if s["room"] == room and abs(s["center_mhz"] - fk) <= 0.2]
                row.append(f"{max(vals):.0f}" if vals else "—")
            rows.append(row)
        w = min(1.0, 5.5 / max(len(rooms), 1)) * inch
        el.append(_table(rows, [1.3 * inch] + [w] * len(rooms)))

    # ---- Expected vs found (receive-chain verification) ----
    from .expected import expected_vs_found
    exp = expected_vs_found(db, session_id)
    if exp["counts"]["total_records"]:
        c = exp["counts"]
        el.append(Paragraph("Expected-Signal Verification", ss["H1x"]))
        el.append(Paragraph(
            f"Of {c['total_records']} licensed transmitters predicted at this location by "
            f"the database baseline, {c['found']} were received during the survey. "
            "Signals expected but not received are listed below with the assessed reason — "
            "this validates the receive chain and documents why any predicted signal is "
            "absent, rather than leaving the gap unexplained.", ss["Body"]))
        missing = [r for r in exp["rows"] if not r["found"]]
        if missing:
            rows = [["Frequency", "Record", "Dist mi", "Expectation", "Assessed reason"]]
            for r in missing[:40]:
                f = _fmt_freq(r["freq_low_mhz"]) if r["freq_low_mhz"] == r["freq_high_mhz"] \
                    else f"{_fmt_freq(r['freq_low_mhz'])} – {_fmt_freq(r['freq_high_mhz'])}"
                rows.append([Paragraph(f, ss["Cell"]),
                             Paragraph((r["callsign"] or r["label"] or "")[:40], ss["Cell"]),
                             f"{r['distance_mi']:.0f}" if r["distance_mi"] is not None else "",
                             r["expectation"],
                             Paragraph(r["reason"][:220], ss["Cell"])])
            el.append(_table(rows, [1.1 * inch, 1.2 * inch, 0.55 * inch,
                                    0.8 * inch, 3.35 * inch]))
            if len(missing) > 40:
                el.append(Paragraph(f"…and {len(missing) - 40} more lower-expectation "
                                    "records (full list in the CSV export).", ss["Cell"]))
        else:
            el.append(Paragraph("Every predicted signal was received.", ss["Body"]))

    # ---- Anomaly list ----
    el.append(Paragraph("Anomalies — Unverified / Flagged Signals (ranked)", ss["H1x"]))
    anomalies = [s for s in signals if s["phase"] == "interior"
                 and (s["classification"] == "UNVERIFIED" or s["caution"])]
    anomalies.sort(key=lambda s: -(s["anomaly_score"] or 0))
    if anomalies:
        rows = [["Score", "Frequency", "Peak", "Persist", "In/Out Δ", "Strongest room",
                 "Class", "Why it didn't verify"]]
        for s in anomalies:
            rows.append([
                f"{s['anomaly_score']:.0f}" if s["anomaly_score"] is not None else "",
                _fmt_freq(s["center_mhz"]), f"{s['peak_dbm']:.0f}",
                f"{(s['persistence'] or 0) * 100:.0f}%",
                f"+{s['interior_delta_db']:.0f} dB" if s["interior_delta_db"] else "—",
                (s["strongest_room"] or "—") +
                (f" (+{s['localization_score']:.0f} dB)" if s["localization_score"] else ""),
                s["classification"],
                Paragraph((s["match_reason"] or "")[:240], ss["Cell"])])
        el.append(_table(rows, [0.45 * inch, 0.95 * inch, 0.45 * inch, 0.5 * inch,
                                0.6 * inch, 1.15 * inch, 0.75 * inch, 2.15 * inch]))
    else:
        el.append(Paragraph(
            "No unverified or caution-flagged signals were detected in the completed phases.",
            ss["Body"]))

    # ---- Directional findings ----
    if directional:
        el.append(Paragraph("Directional Follow-up Readings", ss["H1x"]))
        rows = [["Frequency", "Room", "Peak dBm", "Operator notes", "Time"]]
        for d in directional:
            rows.append([_fmt_freq(d["center_mhz"]), d["room"] or "",
                         f"{d['peak_dbm']:.0f}" if d["peak_dbm"] is not None else "",
                         Paragraph((d["notes"] or "")[:200], ss["Cell"]),
                         time.strftime("%H:%M", time.localtime(d["ts_utc"]))])
        el.append(_table(rows, [1.0 * inch, 1.1 * inch, 0.7 * inch, 3.2 * inch, 0.7 * inch]))

    # ---- Wireless device inventory ----
    inv = db.query("SELECT * FROM inventory WHERE session_id=? "
                   "ORDER BY owned ASC, rssi_dbm DESC", (session_id,))
    if inv:
        el.append(Paragraph("Wireless Device Inventory (WiFi / Bluetooth LE)", ss["H1x"]))
        el.append(Paragraph(
            "Devices observed by local adapters during the survey. Devices the client "
            "accounts for are marked CLIENT-OWNED; strong unaccounted transmitters "
            "warrant follow-up.", ss["Body"]))
        rows = [["Type", "Name", "Identifier", "RSSI dBm", "Ch/Freq", "Accounting"]]
        for d in inv:
            status = "CLIENT-OWNED" if d["owned"] else "UNACCOUNTED"
            if not d["owned"] and d["rssi_dbm"] is not None and d["rssi_dbm"] >= -55.0:
                status = "UNACCOUNTED — STRONG SIGNAL"
            rows.append([d["kind"].upper(),
                         Paragraph((d["name"] or "(no name)")[:40], ss["Cell"]),
                         d["identifier"],
                         f"{d['rssi_dbm']:.0f}" if d["rssi_dbm"] is not None else "—",
                         (d["channel"] or "") +
                         (f" / {_fmt_freq(d['freq_mhz'])}" if d["freq_mhz"] else ""),
                         status])
        el.append(_table(rows, [0.5 * inch, 1.4 * inch, 1.45 * inch, 0.65 * inch,
                                1.25 * inch, 1.45 * inch]))

    # ---- Recommendations ----
    el.append(Paragraph("Recommendations / Next Steps", ss["H1x"]))
    recs = []
    if anomalies:
        top = anomalies[0]
        recs.append(f"Physically investigate the ranked anomalies above, beginning with "
                    f"{_fmt_freq(top['center_mhz'])}"
                    + (f" in {top['strongest_room']}" if top["strongest_room"] else "") + ".")
        recs.append("Correlate each anomaly's location with accessible concealment points "
                    "(power sources, furniture, fixtures) and inspect physically (visual, "
                    "NLJD, thermal as applicable).")
    else:
        recs.append("No anomalies require physical follow-up from the completed sweep phases.")
    if interim:
        recs.append("This is an INTERIM report; complete the remaining sweep phases for "
                    "full coverage before drawing final conclusions.")
    recs.append("RF conditions change; periodic re-surveys are recommended after personnel "
                "changes, construction, or any suspected intrusion.")
    for r in recs:
        el.append(Paragraph("•  " + r, ss["Body"]))

    cb = _watermark_and_footer(interim, company)
    doc.build(el, onFirstPage=cb, onLaterPages=cb)

    db.execute("INSERT INTO reports (session_id,path,kind,generated_utc) VALUES (?,?,?,?)",
               (session_id, path, kind, time.time()))
    return {"path": path, "kind": kind, "interim": interim, "simulated": simulated}


def swept_range_label(sweeps: list[dict], device: dict | None) -> str:
    """The cover-block range claim reflects what was ACTUALLY swept this session,
    not the device's capability. Only when the completed sweeps cover every
    hardware-reported module end-to-end does the report make the continuous-
    coverage claim, using the ranges verbatim from hardware."""
    done = [s for s in sweeps if s["status"] == "done" and s["phase"] in ("ambient", "interior")
            and s["range_low_mhz"] is not None]
    if not done:
        return "(no completed sweeps yet)"
    # merge swept intervals
    spans = sorted((s["range_low_mhz"], s["range_high_mhz"]) for s in done)
    merged = [list(spans[0])]
    for lo, hi in spans[1:]:
        if lo <= merged[-1][1] + 0.001:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])

    def f(mhz):
        return f"{mhz * 1000:.0f} kHz" if mhz < 1 else \
            (f"{mhz / 1000:.4f} GHz" if mhz >= 1000 else f"{mhz:.3f} MHz")

    mods = (device or {}).get("modules") or []
    full = bool(mods) and all(
        any(lo <= m["min_mhz"] + 0.01 and hi >= m["max_mhz"] - 0.01 for lo, hi in merged)
        for m in mods)
    if full:
        return DeviceRange(device)
    label = "swept " + ", ".join(f"{f(lo)} – {f(hi)}" for lo, hi in merged)
    unswept = [m for m in mods if not any(
        lo <= m["min_mhz"] + 0.01 and hi >= m["max_mhz"] - 0.01 for lo, hi in merged)]
    if unswept:
        label += ("; NOT fully swept: "
                  + ", ".join(f"{m['side'].upper()} module ({f(m['min_mhz'])} – "
                              f"{f(m['max_mhz'])})" for m in unswept))
    return label


ENGAGEMENT_TERMS = {
    "onsite": [
        "On-site engagement: a Shield technician performs the full sweep at the premises on "
        "the scheduled date.",
    ],
    "remote": [
        "Remote engagement: upon receipt of the agreed deposit, Shield ships a pre-configured "
        "technical package (spectrum analyzer + antennas) with step-by-step setup instructions "
        "so you can run the guided sweep yourself.",
        "After your sweep is complete and you are satisfied with the results, return the device "
        "to Shield using the enclosed prepaid materials. The deposit is applied per the "
        "engagement agreement.",
    ],
    "monitoring": [
        "Continuous-monitoring engagement: upon receipt of the agreed deposit, Shield ships a "
        "pre-configured monitoring package with setup instructions.",
        "Keep the device in place for ongoing monitoring; Shield bills monthly for the duration. "
        "Return the device when you choose to end monitoring.",
    ],
}


def generate_presweep_report(db, cfg, session_id: int, reports_dir: str) -> dict:
    """Client-facing PRE-sweep report: expected RF environment + methodology +
    engagement terms + the client's own stated concerns. Deliberately excludes
    all internal assessment material (Persons of Interest, background, capability
    notes) — those never go to the client."""
    ss = _styles()
    session = db.query_one("SELECT * FROM sessions WHERE id=?", (session_id,))
    if not session:
        raise ValueError("no such session")
    eng = db.query_one("SELECT * FROM engagement WHERE session_id=?", (session_id,)) or {}
    baseline = db.query("SELECT * FROM baseline WHERE session_id=? ORDER BY freq_low_mhz",
                        (session_id,))
    sources = db.query("SELECT * FROM source_status WHERE session_id=?", (session_id,))
    etype = eng.get("engagement_type", "onsite")
    company = cfg.get("branding", "company", default="")

    ts = time.strftime("%Y%m%d_%H%M%S")
    os.makedirs(reports_dir, exist_ok=True)
    path = os.path.join(reports_dir, f"session{session_id}_presweep_{ts}.pdf")
    doc = SimpleDocTemplate(path, pagesize=letter, topMargin=0.7 * inch,
                            bottomMargin=0.8 * inch, leftMargin=0.75 * inch,
                            rightMargin=0.75 * inch,
                            title=f"Pre-Sweep Briefing — {session['address']}")
    el = []
    logo = cfg.get("branding", "logo_path", default="")
    if logo and os.path.exists(logo):
        try:
            el.append(Image(logo, width=2.2 * inch, height=0.9 * inch, kind="proportional"))
            el.append(Spacer(1, 10))
        except Exception:  # noqa: BLE001
            pass
    el.append(Paragraph(company or "Shield TSCM", ss["Cover"]))
    el.append(Paragraph("Pre-Sweep Briefing — Technical Surveillance Counter-Measures",
                        ss["Body"]))
    el.append(Spacer(1, 14))
    cover = [["Prepared for", eng.get("client_name") or "—"],
             ["Location", session["address"]],
             ["Date", time.strftime("%B %d, %Y")],
             ["Engagement", etype.capitalize()]]
    t = Table(cover, colWidths=[2.0 * inch, 5.0 * inch])
    t.setStyle(TableStyle([("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
                           ("FONTSIZE", (0, 0), (-1, -1), 10),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                           ("LINEBELOW", (0, 0), (-1, -1), 0.4, colors.HexColor("#cccccc"))]))
    el.append(t)
    el.append(Spacer(1, 14))

    el.append(Paragraph("What this briefing is", ss["H1x"]))
    el.append(Paragraph(
        "Before any sweep, Shield assembles a database-verified picture of every radio signal "
        "that is <i>expected</i> at your location — licensed broadcast stations, public-safety "
        "and land-mobile systems, cellular infrastructure, and the national frequency "
        "allocations. This briefing shows you that expected environment up front. During the "
        "sweep, anything we detect that cannot be matched to this known picture becomes a "
        "candidate for physical investigation. Knowing the baseline in advance is what lets us "
        "separate the ordinary radio noise of your neighborhood from a device that does not "
        "belong.", ss["Body"]))

    el.append(Paragraph("Engagement terms", ss["H1x"]))
    for line in ENGAGEMENT_TERMS.get(etype, ENGAGEMENT_TERMS["onsite"]):
        el.append(Paragraph("•  " + line, ss["Body"]))

    if eng.get("client_concerns"):
        el.append(Paragraph("Your stated concerns", ss["H1x"]))
        el.append(Paragraph(eng["client_concerns"], ss["Body"]))

    el.append(Paragraph("Expected RF Environment at Your Location", ss["H1x"]))
    if sources:
        src_data = [["Source", "Status", "Entries"]]
        for s in sources:
            src_data.append([s["source"], s["status"].upper(), str(s["count"] or 0)])
        el.append(_table(src_data, [2.2 * inch, 1.4 * inch, 1.0 * inch]))
        el.append(Spacer(1, 8))
    if baseline:
        hi = max((r["freq_high_mhz"] for r in baseline), default=6100.0)
        el.append(SpectrumStrip(baseline, [], 0.0, hi))
        el.append(Spacer(1, 6))
        rows = [["Frequency", "Service", "Callsign / Label", "Dist (mi)", "Source"]]
        for r in baseline[:120]:
            f = _fmt_freq(r["freq_low_mhz"]) if r["freq_low_mhz"] == r["freq_high_mhz"] \
                else f"{_fmt_freq(r['freq_low_mhz'])} – {_fmt_freq(r['freq_high_mhz'])}"
            rows.append([Paragraph(f, ss["Cell"]), r["service"] or "",
                         Paragraph((r["callsign"] or r["label"] or "")[:46], ss["Cell"]),
                         f"{r['distance_mi']:.1f}" if r["distance_mi"] is not None else "",
                         r["source"]])
        el.append(_table(rows, [1.35 * inch, 0.7 * inch, 2.3 * inch, 0.7 * inch, 1.0 * inch]))
        if len(baseline) > 120:
            el.append(Paragraph(f"…and {len(baseline) - 120} more expected allocations.",
                                ss["Cell"]))
    else:
        el.append(Paragraph("Baseline not yet built for this address — build it before "
                            "issuing the briefing.", ss["Body"]))

    el.append(Paragraph("Methodology (what the sweep will do)", ss["H1x"]))
    for para in (
        "The premises are swept across the full frequency range the analyzer supports, with "
        "extra dwell time on the bands where surveillance devices commonly operate. An exterior "
        "reference reading lets us tell a signal that originates inside your premises from the "
        "ordinary signals present everywhere in the area.",
        "Every detected signal is matched against the expected environment above and classified. "
        "Signals that cannot be accounted for are localized room-by-room and, where warranted, "
        "hunted with a directional antenna. You receive a final report mirroring exactly what is "
        "found on site. This survey is receive-only; nothing is transmitted.",
    ):
        el.append(Paragraph(para, ss["Body"]))

    cb = _watermark_and_footer(False, company)
    doc.build(el, onFirstPage=cb, onLaterPages=cb)
    db.execute("INSERT INTO reports (session_id,path,kind,generated_utc) VALUES (?,?,?,?)",
               (session_id, path, "PRESWEEP", time.time()))
    return {"path": path, "kind": "PRESWEEP"}


def DeviceRange(device: dict) -> str:
    mods = device.get("modules") or []
    if not mods:
        return "(device reported no modules)"
    spans = sorted([(m["min_mhz"], m["max_mhz"]) for m in mods])
    gaps = [(a[1], b[0]) for a, b in zip(spans, spans[1:]) if b[0] > a[1]]
    lo, hi = spans[0][0], max(s[1] for s in spans)
    lo_txt = f"{lo * 1000:.0f} kHz" if lo < 1 else f"{lo:.3f} MHz"
    hi_txt = f"{hi / 1000:.1f} GHz" if hi >= 1000 else f"{hi:.1f} MHz"
    if gaps:
        return (f"coverage {lo_txt} – {hi_txt} with gap(s): "
                + ", ".join(f"{a:.1f}–{b:.1f} MHz" for a, b in gaps))
    return f"continuous coverage {lo_txt} – {hi_txt}"


def export_csv(db, session_id: int, out_dir: str) -> str:
    import csv as _csv
    os.makedirs(out_dir, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    path = os.path.join(out_dir, f"session{session_id}_signals_{ts}.csv")
    signals = db.query("SELECT * FROM signals WHERE session_id=? ORDER BY center_mhz",
                       (session_id,))
    cols = ["center_mhz", "bw_khz", "peak_dbm", "avg_dbm", "floor_dbm", "persistence",
            "phase", "module", "room", "classification", "matched_label", "match_reason",
            "caution", "anomaly_score", "interior_delta_db", "localization_score",
            "strongest_room"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = _csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for s in signals:
            w.writerow(s)
    return path
