"""Record a narrated demo video of the SHIELD TSCM spectrum-survey app.

Drives the REAL app UI (http://127.0.0.1:8791 by default) over a REAL prior
session — actual sweep data, baseline rows, and archived signal fingerprints
recorded from this terminal. No synthetic spectrographs are shown; the only
injected elements are the caption bar and title cards.

Street addresses are blurred via CSS so the video is safe to publish.

Usage:  python tools/record_demo_video.py [--base http://127.0.0.1:8791]
        [--session 58] [--out marketing/dcs_app_demo.webm]
Requires: pip install playwright requests && python -m playwright install chromium
"""

import argparse
import os
import shutil
import sys
import time

import requests
from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# CSS injected for the recording only: caption bar, title cards, PII blur.
OVERLAY_CSS = """
#hdr-addr, #tbl-sessions td:nth-child(2) { filter: blur(7px); }
#demo-cap { position: fixed; left: 0; right: 0; bottom: 34px; z-index: 400;
  display: flex; justify-content: center; pointer-events: none; }
#demo-cap span { max-width: 900px; background: rgba(5,8,12,.88); color: #e6edf5;
  border: 1px solid #2c3a49; border-radius: 8px; padding: 10px 18px;
  font: 15px 'Consolas','Cascadia Mono',monospace; line-height: 1.45;
  box-shadow: 0 12px 40px -18px #000; opacity: 0; transition: opacity .35s; }
#demo-cap span.show { opacity: 1; }
#demo-card { position: fixed; inset: 0; z-index: 500; background: #0b0f14;
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: 18px; opacity: 0; pointer-events: none; transition: opacity .5s; }
#demo-card.show { opacity: 1; }
#demo-card .t1 { font: bold 34px 'Consolas',monospace; color: #fff; letter-spacing: 3px; }
#demo-card .t1 b { color: #1abc9c; }
#demo-card .t2 { font: 16px 'Consolas',monospace; color: #9fb0c0; max-width: 760px;
  text-align: center; line-height: 1.6; }
#demo-card .t3 { font: 12px 'Consolas',monospace; color: #7b8794; letter-spacing: .18em; }
"""

OVERLAY_JS = """
() => {
  const cap = document.createElement('div');
  cap.id = 'demo-cap'; cap.innerHTML = '<span></span>';
  document.body.appendChild(cap);
  const card = document.createElement('div');
  card.id = 'demo-card';
  card.innerHTML = '<div class="t1"></div><div class="t2"></div><div class="t3"></div>';
  document.body.appendChild(card);
  window.__cap = (txt) => {
    const s = document.querySelector('#demo-cap span');
    if (!txt) { s.classList.remove('show'); return; }
    s.classList.remove('show');
    setTimeout(() => { s.textContent = txt; s.classList.add('show'); }, 240);
  };
  window.__card = (t1, t2, t3) => {
    const c = document.getElementById('demo-card');
    if (t1 === null) { c.classList.remove('show'); return; }
    c.querySelector('.t1').innerHTML = t1;
    c.querySelector('.t2').innerHTML = t2 || '';
    c.querySelector('.t3').innerHTML = t3 || '';
    c.classList.add('show');
  };
}
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8791")
    ap.add_argument("--session", type=int, default=58)
    ap.add_argument("--out", default=os.path.join(HERE, "marketing", "dcs_app_demo.webm"))
    args = ap.parse_args()

    # open the real session server-side so the UI boots straight into it
    r = requests.post(args.base + "/api/session/open", json={"id": args.session}, timeout=30)
    r.raise_for_status()
    print(f"opened session #{args.session}")

    vid_dir = os.path.join(os.path.dirname(os.path.abspath(args.out)), "_video_tmp")
    os.makedirs(vid_dir, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(
            viewport={"width": 1600, "height": 900},
            record_video_dir=vid_dir,
            record_video_size={"width": 1600, "height": 900})
        page = ctx.new_page()
        page.goto(args.base, wait_until="networkidle")
        page.add_style_tag(content=OVERLAY_CSS)
        page.evaluate(OVERLAY_JS)

        def cap(txt):
            page.evaluate("t => window.__cap(t)", txt)

        def card(t1, t2="", t3=""):
            page.evaluate("a => window.__card(a[0], a[1], a[2])", [t1, t2, t3])

        def view(name):
            page.click(f'nav .step[data-view="{name}"]')

        def hold(s):
            time.sleep(s)

        # ---- 1 · title card ----
        card("SHIELD<b>TSCM</b> // SPECTRUM SURVEY",
             "A professional bug sweep, on screen — every emitter judged against "
             "what is actually licensed at the address.",
             "REAL SWEEP DATA · RECORDED FROM THE OPERATOR TERMINAL")
        hold(5)
        card(None)
        hold(0.6)

        # ---- 2 · session ----
        view("session")
        cap("Every engagement starts with an address. The app pulls FCC broadcast, "
            "ULS licenses, and cell-tower records to build the expected RF picture "
            "before anyone walks in.")
        hold(8)

        # ---- 3 · baseline ----
        view("baseline")
        cap("The database-verified baseline: what SHOULD be on the air at this "
            "address — station by station, band by band.")
        hold(9)

        # ---- 4 · results ----
        view("results")
        cap("After the sweep, every detected emitter is classified against that "
            "baseline: identified, known-band, or anomaly.")
        hold(7)
        page.evaluate("document.querySelector('#tbl-anomalies') && "
                      "document.querySelector('#tbl-anomalies').scrollIntoView({block:'center'})")
        cap("Whatever the baseline cannot explain is ranked by anomaly score and "
            "queued for directional follow-up — this is a real result set from "
            "this terminal.")
        hold(9)

        # ---- 5 · bug-frequency scan (catalog bands) ----
        view("bugscan")
        cap("The bug-frequency scan concentrates on the bands covert devices "
            "actually use — 315/433/915 MHz ISM, DECT, 2.4/5.8 GHz, GPS, and the "
            "cellular uplinks a GSM or LTE audio bug calls out on.")
        hold(9)
        cap("Those bands come straight from our surveillance-device catalog: "
            "Wi-Fi cameras, BLE trackers, Bluetooth and cellular audio bugs, "
            "wireless microphones — 10 device classes, 23 signatures.")
        hold(8)

        # ---- 6 · signal wiki (real fingerprints) ----
        view("wiki")
        hold(2.5)
        try:
            page.click("#tbl-wiki tr", timeout=4000)
            hold(1.5)
        except Exception:
            pass
        cap("Every observation is archived to the signal wiki — these are real "
            "captured fingerprints from past sweeps. Six months from now, the app "
            "flags anything at this address that changed.")
        hold(10)

        # ---- 7 · manual triage ----
        view("triage")
        cap("Nothing ships until every unexplained signal is dispositioned — "
            "cleared, suspect, or flagged. The review is gated: it is not done "
            "until the queue is empty.")
        hold(8)

        # ---- 8 · switchable styles ----
        cap("Operators pick the interface style — the live instruments stay "
            "readable in all of them.")
        for t in ("t-day", "t-neon", ""):
            page.click(f'.themebar button[data-t="{t}"]')
            hold(2.2)
        hold(1)

        # ---- 9 · report ----
        view("results")
        cap("The client report writes itself from the sweep record — band-by-band "
            "clearance, anomalies, recommendations.")
        try:
            if page.is_enabled("#btn-report"):
                page.click("#btn-report")
                page.wait_for_selector("a.report-ready", timeout=45000)
                hold(3)
            else:
                hold(4)
        except Exception:
            hold(3)
        hold(3)

        # ---- 10 · end card ----
        cap(None)
        hold(0.5)
        card("DCS <b>/</b> DeConfliction Systems",
             "Bug sweeps you can watch happen.<br>Powered by Shield Security — "
             "exclusive launch partner.",
             "SHIELD TSCM · SPECTRUM SURVEY")
        hold(5)

        page.close()
        video_path = page.video.path()
        ctx.close()
        browser.close()

    out = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    shutil.move(video_path, out)
    shutil.rmtree(vid_dir, ignore_errors=True)
    print("video saved:", out, f"({os.path.getsize(out)/1e6:.1f} MB)")


if __name__ == "__main__":
    sys.exit(main())
