"""Export Appendix A — render the UNVERIFIED capture traces for the Cooke-Reese
on-site sweep (sessions 30 + 31) into a separate, self-contained HTML file plus
a folder of individual SVGs. Read-only against sweeps.db. Outputs to the
Shield_TSCM_Business folder (parent of spectrum_survey)."""
import os, sys, json, sqlite3, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                 # spectrum_survey/
PARENT = os.path.dirname(ROOT)               # Shield_TSCM_Business/
sys.path.insert(0, ROOT)
from survey.sigcapture import render_capture_svg

DB = os.path.join(ROOT, "data", "sweeps.db")
SESSIONS = (30, 31)
OUT_DIR = os.path.join(PARENT, "Cooke_Reese_Appendix_A_Captures")
OUT_HTML = os.path.join(PARENT, "Cooke_Reese_TSCM_Appendix_A_Signal_Captures.html")
os.makedirs(OUT_DIR, exist_ok=True)

c = sqlite3.connect(DB); c.row_factory = sqlite3.Row; cur = c.cursor()
addr = cur.execute("SELECT address FROM sessions WHERE id=?", (SESSIONS[0],)).fetchone()["address"]

q = """SELECT * FROM captures
       WHERE session_id IN (%s) AND classification='UNVERIFIED'
         AND data_json IS NOT NULL AND data_json!=''
       ORDER BY peak_dbm DESC""" % ",".join("?" * len(SESSIONS))
rows = cur.execute(q, SESSIONS).fetchall()

cards = []
for r in rows:
    try:
        data = json.loads(r["data_json"])
    except Exception:
        continue
    if not data.get("freqs"):
        continue
    cap = r["captured_utc"]
    captured = datetime.datetime.utcfromtimestamp(cap).strftime("%Y-%m-%d %H:%M UTC") if cap else ""
    center = r["center_mhz"] or 0.0
    flagged = center < 1.0  # the ~317 kHz transient
    meta = {
        "label": ("FLAGGED — DIRECTIONAL FOLLOW-UP" if flagged else "UNVERIFIED")
                 + f" · {center:.4f} MHz",
        "center_mhz": center, "peak_dbm": r["peak_dbm"] or 0.0,
        "floor_dbm": r["floor_dbm"] or 0.0, "bw_khz": r["bw_khz"] or 0.0,
        "persistence": r["persistence"] or 0.0,
        "captured": captured, "session_id": r["session_id"],
        "address": addr, "room": r["room"] or "", "module": r["module"] or "",
    }
    svg = render_capture_svg(data, meta, deidentified=False)
    tag = f"{center:.4f}".replace(".", "p")
    fn = f"cap_{'FLAG_' if flagged else ''}{tag}MHz_s{r['session_id']}.svg"
    with open(os.path.join(OUT_DIR, fn), "w", encoding="utf-8") as fh:
        fh.write(svg)
    note = ("<b>317 kHz transient.</b> Strong one-time peak; localized with the "
            "directional antenna and did not reproduce at that amplitude. Recorded "
            "as an isolated transient — <b>no surveillance device confirmed.</b>"
            if flagged else
            f"Unmatched detection — peak {meta['peak_dbm']:.1f} dBm, "
            f"persistence {meta['persistence']*100:.0f}%. No allocation match.")
    cards.append((flagged, center, f"""
  <div class="card{' flag' if flagged else ''}">
    <div class="hd">{'⚑ ' if flagged else ''}{center:.4f} MHz
      <span class="sub">session {r['session_id']} · {captured}</span></div>
    {svg}
    <div class="note">{note}</div>
  </div>""", fn))

# flagged first, then by descending peak (already peak-sorted from SQL)
cards.sort(key=lambda x: (not x[0],))
body = "".join(x[2] for x in cards)

html = f"""<!doctype html><html><head><meta charset="utf-8">
<title>Cooke &amp; Reese TSCM — Appendix A — Unverified Signal Captures</title>
<style>
 body{{background:#0b0f14;color:#e8eef5;font-family:Segoe UI,Arial,sans-serif;margin:0;padding:32px}}
 h1{{font-size:20px;margin:0 0 4px}} .meta{{color:#8fa0b8;font-size:13px;margin-bottom:20px}}
 .intro{{color:#c3ccd8;font-size:13px;max-width:1100px;line-height:1.5;margin-bottom:24px}}
 .card{{background:#11161d;border:1px solid #26303d;border-radius:8px;padding:14px;margin:0 0 22px}}
 .card.flag{{border-color:#e74c3c;box-shadow:0 0 0 1px #e74c3c33}}
 .hd{{font-size:15px;font-weight:600;margin-bottom:8px}} .sub{{color:#8fa0b8;font-weight:400;font-size:12px;margin-left:8px}}
 .note{{color:#aebaccb;font-size:12.5px;margin-top:8px;line-height:1.45}}
 svg{{width:100%;height:auto;border-radius:4px}}
 @media print{{body{{background:#fff;color:#000}}.card{{break-inside:avoid;border-color:#888}}.note{{color:#222}}.meta,.intro{{color:#333}}}}
</style></head><body>
<h1>Appendix A — Unverified Signal Captures</h1>
<div class="meta">Shield Security Systems · Report SHD-2026-TSCM-020 · {addr} · on-site survey June 17, 2026 · sessions 30–31</div>
<div class="intro">A spectrum capture was retained for every detection in the survey. This appendix renders the
<b>{len(cards)} UNVERIFIED (no allocation match)</b> emissions — the candidate-anomaly set — ordered with the one
flagged transient first, then by descending peak amplitude. <b>"Unverified" means not database-identified, not
"suspicious" by default;</b> most are weak and/or intermittent, consistent with ambient or transient RF.
Yellow trace = max-hold, blue = average; red dashed line marks the detection center. No surveillance device was
confirmed (see report §5.3).</div>
{body}
</body></html>"""

with open(OUT_HTML, "w", encoding="utf-8") as fh:
    fh.write(html)

print(f"rendered {len(cards)} captures")
print("SVG folder:", OUT_DIR)
print("HTML file :", OUT_HTML)
