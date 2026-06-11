"""HTML/CSS report pipeline — renders the Jinja templates in
report_templates/ and prints them to PDF with headless Edge/Chrome.
The rendered .html is written next to the .pdf as a self-contained design
artifact (all images embedded)."""

import base64
import html
import json
import os
import re
import shutil
import subprocess
import time

from jinja2 import Template

from .report import rf_density, _dedup_nearest, _active_sources, _fmt_freq

TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "report_templates")

CATEGORY_COLORS = {
    "broadcast": "#00559d", "cellular": "#40a1f3", "ism": "#2f9e6e",
    "land_mobile": "#b07c2a", "aviation": "#7a5fb5", "ham": "#5c6670",
    "gps": "#c0535e", "other": "#9aa6b2",
}
CATEGORY_LABELS = {
    "broadcast": "Broadcast (TV/FM/AM)", "cellular": "Cellular",
    "ism": "ISM / Wi-Fi / consumer", "land_mobile": "Land-mobile / public safety",
    "aviation": "Aviation", "ham": "Amateur", "gps": "GPS", "other": "Other",
}


def _b64(path):
    try:
        with open(path, "rb") as f:
            return base64.b64encode(f.read()).decode()
    except OSError:
        return ""


def _client_initials(name):
    outs = []
    for part in re.split(r"\s*&\s*|\s+and\s+", name or ""):
        words = [w for w in part.split() if w[:1].isalpha()]
        if not words:
            continue
        if len(words) > 1:
            outs.append(f"{words[0][0]}.{words[-1][0]}.")
        else:
            outs.append(words[0][0] + ".")
    return " & ".join(outs) or "—"


def _strip_svg(baseline, width=980, height=72):
    """Allocated-bands map as inline SVG, colored by category."""
    hi = max((r["freq_high_mhz"] for r in baseline), default=6100.0)
    parts = [f'<svg width="100%" viewBox="0 0 {width} {height + 22}" '
             'xmlns="http://www.w3.org/2000/svg">',
             f'<rect x="0" y="0" width="{width}" height="{height}" fill="#0e1620"/>']
    for r in baseline:
        x0 = max(0.0, r["freq_low_mhz"]) / hi * width
        x1 = max(x0 + 0.8, r["freq_high_mhz"] / hi * width)
        color = CATEGORY_COLORS.get(r.get("category") or "other",
                                    CATEGORY_COLORS["other"])
        parts.append(f'<rect x="{x0:.1f}" y="4" width="{x1 - x0:.1f}" '
                     f'height="{height - 8}" fill="{color}" fill-opacity="0.9"/>')
    for ghz in range(0, int(hi / 1000) + 1):
        x = ghz * 1000 / hi * width
        parts.append(f'<line x1="{x:.0f}" y1="{height}" x2="{x:.0f}" '
                     f'y2="{height + 5}" stroke="#5c6670" stroke-width="1"/>')
        parts.append(f'<text x="{x + 3:.0f}" y="{height + 18}" '
                     'font-family="Open Sans,Arial" font-size="11" '
                     f'fill="#5c6670">{ghz} GHz</text>')
    parts.append("</svg>")
    return "".join(parts)


ENGAGEMENT_TERMS_HTML = {
    "onsite": ["On-site engagement: a technician performs the full sweep at the "
               "premises on the scheduled date."],
    "remote": ["Remote engagement: upon receipt of the agreed deposit, a "
               "pre-configured technical package (spectrum analyzer + antennas) "
               "ships with step-by-step setup instructions so you can run the "
               "guided sweep yourself.",
               "After your sweep is complete and you are satisfied with the "
               "results, return the device using the enclosed prepaid materials. "
               "The deposit is applied per the engagement agreement."],
    "monitoring": ["Monitoring engagement: equipment remains on site and reports "
                   "continuously; findings are reviewed on the agreed cadence."],
}


def _find_browser():
    for name in ("msedge", "chrome"):
        p = shutil.which(name)
        if p:
            return p
    for p in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Google\Chrome\Application\chrome.exe"):
        if os.path.exists(p):
            return p
    return None


def _print_pdf(html_path, pdf_path):
    browser = _find_browser()
    if not browser:
        raise RuntimeError("no Edge/Chrome found for PDF printing")
    subprocess.run(
        [browser, "--headless", "--disable-gpu", "--no-pdf-header-footer",
         f"--print-to-pdf={pdf_path}", html_path],
        check=True, capture_output=True, timeout=120)
    if not os.path.exists(pdf_path) or os.path.getsize(pdf_path) < 10_000:
        raise RuntimeError("PDF print produced no usable output")


def generate_presweep_html(db, cfg, session_id: int, reports_dir: str) -> dict:
    session = db.query_one("SELECT * FROM sessions WHERE id=?", (session_id,))
    if not session:
        raise ValueError("no such session")
    eng = db.query_one("SELECT * FROM engagement WHERE session_id=?", (session_id,)) or {}
    baseline = db.query("SELECT * FROM baseline WHERE session_id=? ORDER BY freq_low_mhz",
                        (session_id,))
    sources = db.query("SELECT * FROM source_status WHERE session_id=?", (session_id,))
    named = db.query("SELECT name FROM persons_of_interest WHERE session_id=? "
                     "AND named_by_client=1 ORDER BY name", (session_id,))
    opt_row = db.query_one("SELECT options_json FROM report_options WHERE session_id=?",
                           (session_id,))
    options = {}
    if opt_row and opt_row["options_json"]:
        try:
            options = json.loads(opt_row["options_json"])
        except (TypeError, ValueError):
            pass

    company = cfg.get("branding", "company", default="Shield Security Systems")

    def stations(svc):
        return _dedup_nearest([r for r in baseline
                               if (r.get("service") or "").upper() == svc
                               and r.get("confidence") == "record"])

    broadcast = [("Broadcast television", stations("TV")),
                 ("FM radio", stations("FM")),
                 ("AM radio", stations("AM"))]
    alloc_n = len([r for r in baseline if r.get("confidence") != "record"])

    groups = []
    for name, rows in broadcast:
        nearest = rows[0]["distance_mi"] if rows and rows[0].get("distance_mi") is not None \
            else None
        groups.append({
            "name": name,
            "expected": f"{len(rows)} licensed station{'s' if len(rows) != 1 else ''}",
            "nearest": f"{nearest:.1f} mi" if nearest is not None else "—"})
    groups.append({"name": "Cellular",
                   "expected": "all national carriers — detailed below", "nearest": ""})
    groups.append({"name": "Other allocations (aviation, amateur, ISM/Wi-Fi, "
                           "land-mobile, GPS…)",
                   "expected": f"{alloc_n} national frequency allocations", "nearest": ""})

    station_tables = []
    for name, rows in broadcast:
        if not rows:
            continue
        title = (f"{name} — {len(rows)} station" if len(rows) == 1
                 else f"{name} — closest of {len(rows)} stations")
        station_tables.append({
            "title": title,
            "rows": [{"callsign": html.escape(r.get("callsign") or r.get("label") or ""),
                      "freq": _fmt_freq(r["freq_low_mhz"]),
                      "dist": f"{r['distance_mi']:.1f}"
                              if r.get("distance_mi") is not None else "—"}
                     for r in rows[:5]]})

    observed, nearest_by_carrier = {}, {}
    for r in baseline:
        if (r.get("category") or "") != "cellular" or not r.get("licensee"):
            continue
        raw = r.get("raw_json")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (TypeError, ValueError):
                raw = {}
        tc = (raw or {}).get("tower_count")
        c = r["licensee"]
        if tc:
            observed[c] = max(observed.get(c, 0), int(tc))
        if r.get("distance_mi") is not None:
            nearest_by_carrier[c] = min(nearest_by_carrier.get(c, 1e9), r["distance_mi"])
    carrier_rows = [{"name": html.escape(c), "towers": n,
                     "nearest": f"{nearest_by_carrier[c]:.1f}"
                                if c in nearest_by_carrier else "—"}
                    for c, n in sorted(observed.items(), key=lambda kv: -kv[1])]

    shielding = None
    if options.get("rf_environment") and baseline:
        d = rf_density(baseline)
        paras = []
        nearest = (f" The nearest is about {d['nearest_mi']:.0f} mi away."
                   if d["nearest_mi"] is not None else "")
        paras.append(
            f"This location sits in a {d['tier']}-density broadcast RF environment: "
            f"roughly {d['count']} full-power broadcast transmitters operate within "
            f"~30 miles.{nearest} A dense ambient RF environment is normal for an urban "
            "or suburban area and is not itself a sign of surveillance — but it does "
            "affect both the sweep and any decision about shielding.")
        if d["cell_tier"] == "unknown":
            paras.append("This assessment is based on the broadcast environment; "
                         "tower-by-tower cellular mapping for this address is "
                         "incorporated as area survey data is loaded.")
        else:
            cell_near = (f" the nearest observed tower is about "
                         f"{d['cell_nearest_mi']:.1f} mi away"
                         if d["cell_nearest_mi"] is not None else
                         " tower distances were not reported by the data source")
            paras.append(
                f"Cellular infrastructure: approximately {d['cell_count']} cell towers "
                f"were observed near this location across all carriers;{cell_near}. "
                f"Proximity to cellular towers raises the ambient RF floor at the "
                f"premises ({d['cell_tier']} cellular proximity).")
        if d["recommend"]:
            paras.append(
                "Because the ambient environment here is dense, we recommend "
                "considering an RF shielding assessment. In a security context, "
                "shielding a sensitive room both reduces the chance that a device "
                "inside can reach a receiver outside, and lowers the ambient floor so "
                "a future sweep can resolve weaker anomalies. This is a security "
                "recommendation, not a health one.")
        else:
            paras.append(
                "Shielding is not indicated by the ambient environment alone at this "
                "location. If shielding is desired for a specific sensitive space, it "
                "can still be assessed on request.")
        shielding = {"paras": paras}

    aerial = ""
    for ext in (".png", ".jpg", ".jpeg"):
        p = cfg.abs_path(os.path.join("data", "property_photos",
                                      f"session{session_id}{ext}"))
        if os.path.exists(p):
            aerial = _b64(p)
            break

    etype = eng.get("engagement_type", "onsite")
    report_id = f"SHD-{time.strftime('%Y')}-TSCM-{session_id:03d}-PRELIM"
    ctx = {
        "company": company,
        "company_short": company.split()[0] if company else "Shield",
        "report_id": report_id,
        "client_initials": _client_initials(eng.get("client_name") or ""),
        "date_long": time.strftime("%B %d, %Y"),
        "address": html.escape(session["address"]),
        "logo_b64": _b64(cfg.get("branding", "logo_path", default="")),
        "aerial_b64": aerial,
        "terms": ENGAGEMENT_TERMS_HTML.get(etype, ENGAGEMENT_TERMS_HTML["onsite"]),
        "concerns": html.escape(eng.get("client_concerns") or "").replace("\n", "<br>"),
        "named_pois": [html.escape(p["name"]) for p in named],
        "sources": [html.escape(s["source"]) for s in _active_sources(sources)],
        "groups": groups,
        "station_tables": station_tables,
        "carrier_rows": carrier_rows,
        "baseline_total": len(baseline),
        "shielding": shielding,
        "strip_svg": _strip_svg(baseline) if baseline else "",
        "legend": [{"color": CATEGORY_COLORS[k], "label": CATEGORY_LABELS[k]}
                   for k in CATEGORY_COLORS],
    }

    with open(os.path.join(TEMPLATE_DIR, "presweep.html"), encoding="utf-8") as f:
        rendered = Template(f.read()).render(**ctx)

    ts = time.strftime("%Y%m%d_%H%M%S")
    os.makedirs(reports_dir, exist_ok=True)
    html_path = os.path.join(reports_dir, f"session{session_id}_presweep_{ts}.html")
    pdf_path = os.path.join(reports_dir, f"session{session_id}_presweep_{ts}.pdf")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(rendered)
    _print_pdf(html_path, pdf_path)
    db.execute("INSERT INTO reports (session_id,path,kind,generated_utc) VALUES (?,?,?,?)",
               (session_id, pdf_path, "PRESWEEP", time.time()))
    return {"path": pdf_path, "kind": "PRESWEEP", "html_path": html_path}
