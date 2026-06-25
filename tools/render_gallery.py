"""Render the strongest UNVERIFIED captures (sessions 30+31) to uniform PNGs for
Appendix A, and emit a manifest the docx build reads. Read-only on sweeps.db."""
import os, sqlite3, json
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                 # spectrum_survey/
PARENT = os.path.dirname(ROOT)               # Shield_TSCM_Business/
OUT = os.path.join(PARENT, "Cooke_Reese_Appendix_A_Captures", "gallery")
os.makedirs(OUT, exist_ok=True)
DB = os.path.join(ROOT, "data", "sweeps.db")

def render(data, meta, path, W=1100, H=470):
    fr, avg, mx = data["freqs"], data["avg"], data["maxpeak"]
    pl, pt, pw, ph = 80, 60, W - 120, H - 150
    amin, amax = -120.0, -10.0
    X = lambda i: pl + pw * i / (len(fr) - 1)
    def Y(v):
        v = max(amin, min(amax, v)); return pt + ph * (1 - (v - amin) / (amax - amin))
    img = Image.new("RGB", (W, H), (4, 7, 12)); dr = ImageDraw.Draw(img)
    try:
        f = ImageFont.truetype("consola.ttf", 13); fb = ImageFont.truetype("consolab.ttf", 16); fs = ImageFont.truetype("consola.ttf", 11)
    except Exception:
        f = fb = fs = ImageFont.load_default()
    dr.rectangle([pl, pt, pl + pw, pt + ph], fill=(0, 2, 8), outline=(38, 48, 61))
    for g in range(-110, -19, 10):
        y = Y(g); dr.line([pl, y, pl + pw, y], fill=(28, 36, 46)); dr.text((pl - 26, y - 7), str(g), font=fs, fill=(107, 123, 144))
    for k in range(11):
        x = pl + pw * k / 10; fm = fr[0] + (fr[-1] - fr[0]) * k / 10
        dr.line([x, pt, x, pt + ph], fill=(28, 36, 46)); dr.text((x - 18, pt + ph + 6), f"{fm:.3f}", font=fs, fill=(107, 123, 144))
    dr.line([(X(i), Y(mx[i])) for i in range(len(fr))], fill=(255, 208, 0), width=2)
    dr.line([(X(i), Y(avg[i])) for i in range(len(fr))], fill=(0, 200, 255), width=2)
    c = meta["center_mhz"]
    if fr[0] <= c <= fr[-1]:
        cx = pl + pw * (c - fr[0]) / (fr[-1] - fr[0]); dr.line([cx, pt, cx, pt + ph], fill=(231, 76, 60), width=1)
    dr.text((pl, 18), f"UNVERIFIED  {c:.4f} MHz", font=fb, fill=(232, 238, 245))
    dr.text((pl, 40), f"peak {meta['peak']:.1f} dBm  floor {meta['floor']:.1f} dBm  bw ~{meta['bw']:.0f} kHz  persistence {meta['persist']*100:.0f}%", font=f, fill=(143, 160, 184))
    dr.text((pl, H - 40), "MAX HOLD (gold) / AVERAGE (cyan)  -  red dashed = detection center  -  Shield Security Systems, session " + str(meta["sess"]), font=fs, fill=(107, 123, 144))
    img.save(path)

c = sqlite3.connect(DB); c.row_factory = sqlite3.Row; cur = c.cursor()
rows = cur.execute("""SELECT * FROM captures WHERE session_id IN (30,31) AND classification='UNVERIFIED'
                      AND data_json IS NOT NULL AND data_json!='' ORDER BY peak_dbm DESC""").fetchall()
# rank 1 = flagged 317 kHz (already embedded as Figure A.1); gallery = ranks 2..10
gallery = [r for r in rows if (r["center_mhz"] or 0) >= 1.0][:9]
manifest = []
for i, r in enumerate(gallery, start=2):
    data = json.loads(r["data_json"])
    meta = {"center_mhz": r["center_mhz"], "peak": r["peak_dbm"] or 0, "floor": r["floor_dbm"] or 0,
            "bw": r["bw_khz"] or 0, "persist": r["persistence"] or 0, "sess": r["session_id"]}
    fn = f"galA{i:02d}_{r['center_mhz']:.3f}MHz.png".replace(".", "p", 1)
    render(data, meta, os.path.join(OUT, fn))
    pct = (r["persistence"] or 0) * 100
    persist = "continuous" if pct >= 95 else ("<1%" if pct < 1 else f"{pct:.0f}%")
    manifest.append({"fig": f"A.{i}", "file": os.path.join("Cooke_Reese_Appendix_A_Captures", "gallery", fn),
                     "freq_mhz": round(r["center_mhz"], 4), "peak": round(r["peak_dbm"], 1),
                     "bw_khz": round(r["bw_khz"], 0), "persist": persist})

with open(os.path.join(PARENT, "appendix_a_gallery.json"), "w", encoding="utf-8") as fh:
    json.dump(manifest, fh, indent=2)
print(f"rendered {len(manifest)} gallery captures -> {OUT}")
for m in manifest:
    print(f"  {m['fig']}: {m['freq_mhz']} MHz  peak {m['peak']}  bw {m['bw_khz']}kHz  {m['persist']}")
