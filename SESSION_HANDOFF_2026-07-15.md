# Session handoff — 2026-07-15 (Shield Sweep)

Read `Dog Pics'\CLAUDE.md` first (auto-loads: what the app is, paths, hardware,
receive-only rule, how Matt verifies). This file = what THIS session did + the
open issue Matt is hitting right now.

## ⚠️ THE LIVE ISSUE RIGHT NOW ("you broke it" / "96.5 didn't play")
It is NOT a code break. Verified this session:
- App on 8765 responds **HTTP 200**, all source **compiles**, **no tracebacks**,
  **no JS console errors** on the live page.
- Root cause = **the B220's UHD driver is crashing**. Logs show repeatedly:
  `UHD set_rx_rate failed (uhd_error 44)`, `b220 radio worker died mid-call`,
  `b220 radio worker is not running — reconnect to respawn it`. A dead radio =
  nothing can tune/monitor/DF, so clicking 96.5 MHz had no working radio.
- SECOND factor: Matt's browser tab loaded at 07:21, **before** the click-handoff
  feature existed. Logs show his click sent `/api/listen/bandwidth` (old "just
  monitor" path) and there is **zero** `/api/directional/start` — so the new
  code never ran in his tab. He was on stale JS.

**Fix Matt needs to do (tell him plainly, don't re-diagnose):**
1. DEVICE card → **CONNECT** the B220 (respawns the worker). If it won't come
   back, **replug the B220 USB** (rear/powered port, data cable) — the chip is
   USB-sensitive; `uhd_error 44` recurring = physical cable/port/power, not code.
2. **Hard-refresh the browser (Ctrl+F5)** to load the current app.js.

Matt is frustrated and on-site with a client. Be calm, look at his rig
(read-only curls to 8765 + `logs/app.log`) before claiming anything.

## What THIS session built (all in the live source; 447 tests pass)
All frontend/JS + a few backend fields. Frontend changes need only Ctrl+F5;
backend/DB/config changes need an app restart.

**Signal lightbox (click a signal's thumbnail):**
- Verification **rundown** — ordered cheapest-first checklist (band → client
  relevance → baseline → shape → anomaly → novelty → signature ID → demod), each
  with result or a one-click run button; refines live after IDENTIFY/ANALYZE.
- Signature-ID **scored candidates** ("20% Analog FM audio…"); click one → a
  **measured-vs-expected side-by-side**.
- **◂ ▸ prev/next arrows** (+ ← → keys) step signals in ledger order.
- **📡 DIRECTIONAL (RX-A)** button → `/api/directional/start` + directional view.
- More info: interior-vs-ambient delta, localization/room, attribution.

**Blinded cue (content-BLIND, ECPA-safe) — `survey/nextstep.py blinded_cue_for`:**
- "Client relevance" line: *"Appears to originate inside the client's space;
  carries audio — room-bug signature"* vs *"third party — not client"*. Judged
  from SHAPE (narrow+persistent+analog band) + SPATIAL (interior delta,
  localization, novelty, operator attribution) only. NEVER from content.

**Admin verify-listen (`app.admin: true` in config.yaml; surfaced as
`state.admin`):** lightbox gets **▶ LISTEN TO VERIFY (admin)** — logs an audited
`/api/decode/override` then plays audio so the owner can confirm the blinded call
against a KNOWN test signal. Field techs (admin off) see only the blinded cue.
DO NOT build a role that bypasses the audit or mass-auto-demods content (ECPA).

**Device-type tags on the sweep chart** (`device_hint_for`): labels say "Cell
phone (uplink) / WiFi / Cordless (DECT) / ISM / Broadcast / GPS" from the band;
a real signature ID overrides the guess.

**Sweep bug FIXED (B220 double-sweep):** the B220's two RX ports are the SAME
70–6000 MHz range (shared LO → can't split-tune → they're a DF pair), so the old
RF-Explorer "sweep left, move antenna to right, sweep again" dance re-swept the
identical range. Now `isSingleRangeSweep()` detects same-range ports → **one**
sweep on RX-A. RF Explorer (distinct bands) keeps its real 2-step dance.

**Sweep-complete NEXT banner:** ambient→interior, interior→triage,
directional→report. No more "sits there when done."

**Auto-handoff on signal click (Matt: "never show me 'not plugged in'"):**
`listenAt()` — if a sweep is running and there's NO second (Pluto) monitor radio
(`hasConcurrentMonitor()` false), it **aborts the sweep and direction-finds the
clicked signal on the one radio**; if the radio's worker is dead it
**reconnects + retries** (hardened for the B220 crash). Two independent-LO radios
(B220 + Pluto) still monitor concurrently.

**HARDWARE FACT to reuse:** B210/B220 = one AD9361, ONE shared RX LO → the two RX
CANNOT tune two different frequencies at once (that's why they DF together, and
why concurrent monitor-at-another-freq needs the separate Pluto). BladeRF 2.0 =
same AD9361 limit. True single-box concurrency = multi-daughterboard USRP
(X310/N-series). Don't claim the B220 can split-tune.

## Earlier this session (also live): NETWORK CHECK (LAN) feature
`survey/netscan.py` — read-only device discovery (ARP+ping+DNS, local /24 only,
no creds), owned-allowlist flagging, **router-based** block guidance (detects
gateway + brand, gives MAC-filter steps — NEVER transmits/jams: that's illegal,
see memory [[receive-only-no-jammers]]), device→RF correlation, LOCATE (DF the
correlated emission). In-app **INSTALL RADIO DRIVER** button
(`/api/device/install_driver` — WARNING: on Windows this launches the real
installer .exe; NEVER POST it as a test on Matt's machine, it pops UAC).

## Client thumb drive — DONE and correct at `D:\ShieldSweep_Installer`
Shipped as `app.zip` (so native/libiio DLLs survive the copy — the earlier
"client radio dead" bug was those DLLs missing). Config = Pluto,
`uri: ip:192.168.2.1`, keys blank, no client data. Installer extracts + makes a
Desktop **START SWEEP** button + runs the Pluto driver. See memory
[[client-deploy-native-libs]]. The network-check + lightbox features are NOT yet
folded into the client build — Matt wants to verify on real hardware first.

## Open / pending (Matt's running wishlist)
- Verify all the above on REAL hardware once the B220 stops crashing (his job).
- He wanted "auto-demodulate every signal" — built the COMPLIANT version
  (auto content-blind classification + blinded cue). Do NOT mass-auto-demod
  content or add a role that bypasses the ECPA gate.
- Isolated-test pattern: copy to
  `scratchpad\shieldtest`, run `python app.py --simulate --port 8899
  --no-browser`, drive via the Browser pane. NEVER run a 2nd process against
  8765's `data/sweeps.db`. Kill the 8899 python when done.
- `jscheck.py` in scratchpad false-positives at line ~63 (the esc() regex) —
  ignore; verify JS by loading in the Browser pane and reading console errors.
