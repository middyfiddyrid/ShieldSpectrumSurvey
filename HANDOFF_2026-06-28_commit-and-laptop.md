# HANDOFF — 2026-06-28 — Commit everything, then move the TSCM app to the laptop

Read `HANDOFF.md` too (architecture + non-negotiable guardrails). This file is the
session-specific plan. Goal of the next session, in order:

1. **Commit all the uncommitted work** in this repo.
2. **Get the app onto the laptop** so Matt can walk around the house during a sweep.
3. **Back up the Nations Media email bank** to the laptop (verified) — separate task, same destination.

---

## 1) Commit the work

- **Branch:** `pocsag-decoder`.  **No git remote is configured** (`git remote -v` is empty).
- Working tree: **17 modified files** (this session's work + Croc's prior uncommitted decode work) and **2 untracked**: `SETUP_NEW_LAPTOP.bat`, `SYNC_VERSION.txt`.
- **Before committing, run the tests:** `python -m unittest discover -s tests -p "test_*.py"` → expect **224 passing**.
- **DO NOT commit `config.yaml`** — it's gitignored and holds live secrets (OpenCellID key, Pluto cal/gain). **Back it up (copy aside) before any branch switch/merge** (machine-local, irreplaceable).
- Commit on `pocsag-decoder`, then merge to `main` if desired (Matt's default is PRs to main — but **no remote**, so it's a local merge unless you add a GitHub remote first; adding a remote is also step 2's cleanest path).
- **Review the two untracked files** — `SETUP_NEW_LAPTOP.bat` is almost certainly the laptop-migration helper (directly relevant to step 2); decide whether to commit it.
- LF→CRLF warnings on commit are cosmetic (autocrlf); ignore.

### What changed this session (for an accurate commit message)
- **Plain-language UI vocabulary:** internal codes mapped to friendly labels everywhere users see them — `PLAUSIBLE`→"Likely normal", `Scanning…`→"Detecting…", `VERIFIED`→"Identified", `UNVERIFIED`→"Anomaly". Internal enum values + DB unchanged. (app.js, index.html, report.py)
- **F1 — screen wake-lock:** keeps the laptop screen awake while the app is open / sweeping. (app.js, index.html footer)
- **F2 — per-segment expected/found side panel + anomaly list:** new `expected_for_range()` in expected.py; runner publishes `current_segment.expected` (looking→found) + `anomalies_found`; `renderSegPanel()` in app.js; panel markup in index.html.
- **F3 — "dampen / focus anomalies":** mute expected/known signals, rescale the live chart so weak anomalies fill the view, anomalies-only ledger. (app.js, index.html)
- **F4 — single "Main sweep" step** for single-RX devices (Pluto); RF Explorer keeps the LEFT/RIGHT port steps. Branches on module count, not a hardcoded "pluto". (index.html nav/view, app.js)
- **F5 — operator-controlled HOLD:** sweeps now run **straight through by default**; HOLD / NEXT SEGMENT / RESUME let the tech pause on demand. This REPLACED an earlier auto-pause-every-segment checkbox that surprised the operator (he was rightly angry — see memory `no-surprise-modes`). (runner.py, server.py, app.js, index.html)
- **Live-inspect latency fixes** (the "graph isn't instantaneous vs SDR Console" complaint): block size 0.5→0.15 s (config.py DEFAULTS already 0.15; `config.yaml` set on this machine; index.html Block default now 0.15), skip redundant stale-frame redraws (server `seq` de-dup), time-based trace smoothing (no multi-second lingering), instant panel scroll. **Bigger win still available if wanted:** replace HTTP polling with SSE/WebSocket push — see the trace findings.
- **Adversarial-review fixes:** baseline-strip focus/mute leak, label-overlay scale anchoring in focus mode, live-ledger empty-state, mergeSweepLabels identity preservation, HOLD overlay auto-reveal edge-trigger.

### ⚠ EVERYTHING THIS SESSION IS HARDWARE-UNTESTED
All of it was verified only in the **simulator** (`--simulate`, which needs the device picker set to **RF Explorer** — with source=pluto the sim is bypassed). The latency fixes and the HOLD flow especially need a real-Pluto pass.

---

## 2) Move the app to the laptop (main goal — walk the house during sweeps)

- **No git remote**, so to get the code onto the laptop, pick one: (a) create a GitHub remote, push, then `git clone` on the laptop; (b) copy the repo folder over; (c) `git bundle`.
- **Check `SETUP_NEW_LAPTOP.bat` first** — likely automates the laptop setup.
- Laptop prerequisites (per memory `spectrum-survey-app`): Python + `requirements.txt`; **pyadi-iio==0.0.21**; native **libiio v0.25** DLLs (the repo bundles them under `native/libiio`); ADI PlutoSDR-M2k USB drivers. Plug the Pluto into the laptop.
- **Copy `config.yaml` to the laptop separately** (gitignored secrets). Optionally copy `data/` (FCC ULS extract, large) or just rebuild the baseline on the laptop.
- Run `python app.py`, connect the Pluto, and **test the latency fixes on real hardware** — click an expected signal; the live graph should feel much snappier than before.

---

## 3) Email bank backup (CRITICAL — irreplaceable, do this carefully)

See memory `nmp-email-bank-backup`. Summary:

- The **entire Nations Media email history is a single un-backed-up copy** in `C:\Users\Union Terminal\Downloads`: the takeout zips (`takeout-20240729...001/002/003.zip`) + `MPR_DATA_2.pst` (19.4 GB) + `MPR-DATA (1).pst` (2.6 GB). **~50 GB. The source Gmail account is deleted — these files are irreplaceable.**
- The **Lexar thumb drive (D:) is flaky** — it produced a same-size-but-**corrupt** copy (different SHA-256) and had a mid-write failure. **Do not trust it for the sole copy.** Use the laptop.
- **ALWAYS `Get-FileHash` (SHA-256) a copy and confirm it matches before deleting any C: original. Never delete a source until a verified copy exists elsewhere.**
- Transfer to the laptop via **local LAN or sneakernet** — the data stays local, never passes through Claude or the internet (only the tower needs internet for Claude Code to run). Copy → verify → keep two copies → only then clear it off C: (reclaims ~50 GB).
- A stray partial `takeout-...003.zip` may be on D: from a failed copy — verify/clean it (the sandbox blocks Remove-Item on some D: paths; may need to delete in Explorer).

---

## Machine state at handoff
- Tower **C: ~6 GB free** (was 0 — 100% full earlier today; freed by moving non-email Downloads to D:). Functional but tight until the email bank moves off.
- Spectrum app: features + latency fixes applied; **224 unit tests green** in the simulator. Nothing committed yet.
