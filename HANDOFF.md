# Shield TSCM Spectrum Survey — Session Handoff

Continuation brief for a fresh Fable/Claude Code session. Read this first, then `git log --oneline -20` and skim `README.md`.

---

# ⚡⚡ 2026-07-11 (later) — manual sweep keeps sweeping + operator RBW + waterfall alignment + SIMULATE-B220 guard

Second operator voice dump today: (1) manual mode should KEEP SWEEPING until NEXT is
hit (it idled the radio); (2) technician-adjustable RBW; (3) "the waterfall appears
longer in width than the graph."

## SHIPPED (on `main`, tests 370 green)
1. **Manual gate re-sweeps live** — `_sustain_segment` gained `keep`/`set_sustaining`
   params; the F5 between-segment gate now runs that live-watch loop with
   keep="NEXT not pressed" (awaiting_advance still drives the button; `sustaining`
   stays false so the F6 UI is untouched). A device hiccup during the gate falls
   back to the old idle wait — it never auto-advances past the operator. New
   tests/test_manual_gate.py (radio keeps reading + live_trace keeps moving while
   gated; abort during gate stops promptly).
2. **Operator RBW** — "RBW kHz" input on all five scan cards' custom-tech panels
   (ambient-rbw/bug-rbw/rbw-L/rbw-R/rbw-main) → /api/plan `rbw_khz` (preview) +
   /api/sweep/start → `start_sweep(rbw_khz=)` → `dev.set_rbw_khz()` per sweep
   (ALWAYS called; None restores config so overrides never leak). SDR implementation
   (AD936xSource, covers Pluto+B220): nperseg = pow2(fs/rbw) clamped
   [256, capture_samples//4] (>=4 Welch averages), output grid 1 pt/RBW capped 8192
   (legacy: 1 pt/kHz capped sweep_bins). `predicted_rbw_khz(span, rbw?)` is a PURE
   preview (never mutates — a live sweep re-reads _nperseg every frame). RFE:
   BaseDevice no-op + UI note "RBW fixed at span/112 — use Seg MHz". merge_gap_bins
   is auto-scaled when the grid is finer than default so one signal doesn't split
   into many detections. The plan ETA line now shows the REAL device RBW (the old
   span/112 display was RFE-only and wrong for SDRs). tests/test_rbw.py.
   ⚠ Baseline-comparability caveat: sweeps at a non-default RBW shift narrowband
   SNR (density-normalised PSD) — compare baseline-deltas at matching RBW.
3. **Waterfall/chart alignment (the reported mismatch)** — ROOT CAUSE: `#waterfall`
   was a SIBLING of `.sweep-stage` at width:70% while the chart sat inside
   `.chart-col` (70% minus the 250px seg-panel + 12px gap) → waterfall exactly 262px
   wider, columns drifting right of chart peaks. FIX: the canvas moved INSIDE
   `.chart-col` directly under the chart at width:100% (identical flex width).
   Also pushWaterfall now samples bin CENTERS (round(x/(W-1)*(n-1))) matching
   drawTrace's i/(n-1) mapping (the old floor(x/W*n) skewed a half-bin). The
   monitor-panel pair (insp-analyzer/insp-waterfall) was measured aligned to
   <0.5 px — untouched.
4. **SIMULATE-B220 guard (found during verify)** — `open_device` checked
   `device.simulate` AFTER the pluto/b220 branches, so a `--simulate` run with
   `device.source: b220` opened AND TRIED TO FLASH THE REAL B220. The simulate
   check now comes first (a demo run can never touch hardware). Also fixed:
   the SDR avg-passes cap read `pluto.sweep_avg_passes` even for the B220
   (now dev.CFG_SECTION).

## ADVERSARIAL REVIEW (7-agent, 4 confirmed) — both root causes FIXED + regression-tested
- **CRITICAL — coarse grids point-sampled the PSD and could HIDE a narrowband
  emitter** (np.interp in stitch_psd; a carrier 79 dB over the floor vanished under
  a 500 kHz operator RBW; a milder PRE-EXISTING version affected wide default
  segments: 2048-pt grid vs 19.5 kHz analysis bins). FIX: `stitch_psd` now does
  PEAK-PRESERVING reduction (`_peak_reduce_db`, per-grid-cell MAX — real-SA peak-
  detector semantics) whenever the grid is coarser than the analysis bins; interp
  stays for fine grids (honest noise floor). Verified: the review's repro carrier
  now reads ~78 dB above floor at EVERY rbw (was 0.7 dB = invisible).
- **MAJOR — the per-sweep RBW override LEAKED into wiki fingerprint captures,
  directional passes, calibration, and /api/plan previews** (a leaked 500 kHz
  override collapsed a 0.8 MHz capture zoom to 16 grid points = junk evidence).
  FIX: work() clears it in a finally; start_captures/start_directional/
  run_calibration also reset to config RBW on entry.

## VERIFIED ON REAL HARDWARE (B220 Mini, session 67, sweep erased after)
- Ran the operator's own scenario on the REAL radio: bug-scan, custom, manual
  advance, RBW 10 → gate opened with "live on 300.000-348.000 MHz", live_trace
  advanced every ~2 s DURING the gate (radio kept sweeping), segment banner showed
  RBW 10 kHz, live frame carried 4801 grid points (48 MHz / 10 kHz — vs the old
  2048 cap), chart & waterfall measured pixel-identical (365 px @ left 134),
  NEXT advanced to the next segment, abort{erase} left no rows.
- The app process was restarted to load this code (old PID predated the changes;
  the one-segment demo sweep Matt had stuck at the old idle gate was erase-aborted
  first). NOTE: a stale `python app.py` predating 2026-07-11 lacks ALL of this.

## Bench notes for Matt
- Manual sweep: start a custom sweep with "Manual advance" → after each segment the
  chart+waterfall KEEP MOVING ("▶ LIVE — still sweeping this segment") until NEXT.
- RBW: set e.g. 10 in the RBW kHz box (custom profile) on the B220 → the ETA line
  and the live segment banner show the achieved RBW; finer RBW = more grid points,
  same sweep time (dwell governs time, not RBW). Coarse RBW can no longer hide a
  narrow signal (peak-detector stitch).
- Waterfall: signal columns should now sit exactly under their chart peaks.

---

# ⚡⚡ NEXT-SESSION HANDOFF — 2026-07-11 — Pluto auto-monitor during B220 sweeps + demod fuzz honesty + DISSEMINATE row (READ FIRST)

Operator voice dump (2026-07-11): (1) during a B220 sweep, a "cursory glance at a signal"
should hand off to the Pluto automatically for listen/demod; (2) demod after the
attribution step plays only FUZZ — "I should be hearing something"; (3) "there's no
dissemination."

## SHIPPED (on `main`, tests 361 green)
1. **Pluto auto-handoff** — `runner.ensure_monitor()`: monitor ACTIONS (inspect /
   analyze / listen / record) arriving while the sweeper is busy auto-OPEN the Pluto
   as the monitor (60 s retry cooldown on failure, non-fatal fallback). Sweeper-role
   promotion generalized: any non-Pluto radio (B220 **or** RFE) becomes the sweeper
   when both are open, regardless of connect order. `config.yaml` now has
   `pluto.concurrent_monitor: true` (code default stays false).
2. **Fuzz cause #1 — live squelch was OFF by default.** `listen_squelch_snr_db: 5.0`
   now defaults on (config.py + config.yaml): an empty/mistuned channel MUTES instead
   of AGC-amplifying noise to full scale; the sm-demod readout shows SNR + why.
   Set `null` to hear raw noise again.
3. **Fuzz cause #2 — one-shot ANALYZE audio.** `/api/analyze/signal` no longer returns
   an audio clip when `signal_present` is false (the legacy peak-normalize path turns
   noise into full-scale fuzz); the note says "audio muted — check antenna/gain".
4. **Fuzz cause #3 — wrong config section.** All listen/analyze knobs in server.py now
   resolve per MONITOR radio via `_mcfg(msrc, key, default)`: the monitor source's own
   config section first, then `pluto:`. Critically `cal_offset_db` (B220 +36 vs Pluto
   −30 — a 66 dB analyzer display error when the B220 was the single radio).
   `runner.start_recording` resolves `stream_sample_rate`/`listen_block_seconds` the same way.
5. **DISSEMINATE row (minimal §4c of DEMOD_ANALYZE_DISSEMINATE_DESIGN.md)** — after
   ◎ ANALYZE the card now ends with a per-class next action: analog → "▶ LISTEN ·
   NFM · 12 kHz" (auto mode from modulation_class, auto channel width from
   occupied_bw → `setDemodBw`) + record audio evidence; decodable → "⟲ DECODE ·
   <proto>" + IQ evidence; encrypted/unknown → capture IQ evidence + locate note;
   empty channel → honest "nothing here". Plus an attribution reminder when
   unassessed. New app.js: `dissemRow/dissemListen/dissemDecode/suggestedModeFor/
   nearestBwStep`, `App.lastAnalyze`.

## VERIFIED vs NOT
- **Verified headless:** 361 unit tests green (7 new: ensure_monitor auto-connect /
  off-or-idle / failure-cooldown / connect-order promotion; analyze audio-mute gate;
  b220 per-source knob resolution). Live UI in --simulate preview: no JS errors, all
  five dissemRow variants render, `/api/device/roles` reports `concurrent_enabled:true`.
- **NEEDS THE BENCH (Matt, both radios plugged in):**
  1. Start a B220 sweep → click a signal → audio should come FROM THE PLUTO without a
     "radio busy" 409 (DEVICE card roles: b220=sweeper, pluto=monitor).
  2. Tune a known-good analog signal (FM broadcast, wfm) → real audio, squelch OPEN.
     Empty channel → SILENCE + "SQ muted" in the DEMOD readout (not fuzz).
  3. If a REAL signal squelches: the sm-demod line shows measured SNR — lower
     `pluto.listen_squelch_snr_db` (or null) and tell the next session; that points at
     RX gain staging / antenna, not the DSP.
  4. ◎ ANALYZE on a strong signal → DISSEMINATE row appears → "▶ LISTEN" retunes with
     the measured width and you HEAR it.
- config.yaml got re-dumped by a Config.set during the preview boot (comments
  stripped — known behavior); both new keys survived, values intact.

## Still open (carried forward)
- Continuous gapless listen-while-sweeping on ONE radio (B220 2RX could in principle
  sweep on RX-A and monitor on RX-B; the stream path still binds one channel).
- FLEX real-world constants; the FULL §4c dispatcher + session-level SIGN gate
  (P2–P4 of DEMOD_ANALYZE_DISSEMINATE_DESIGN.md) — `session_authorizations` is still
  NOT built; what exists is per-signal attribution + the per-decode ECPA override.

---

# ⚡⚡ 2026-07-06 (covert-device-detection stream) — active-ping TX + PASSIVE EMANATION detector

On `b220-mini-source` (shared tree w/ the B220 + BeenVerified sessions — commit tight).
Full suite **309 green**. Three detection modes now cover the three device classes:
- **Transmits deliberately** → the interior sweep (existing).
- **Reacts to a ping** → `survey/stimulate.py` + `POST /api/stimulate/probe` + ACTIVE PROBE
  card. Concurrent B220 TX path WIRED (`uhd_source` `start_tx/stop_tx`, `B220Source.stim_*`),
  HARDWARE-UNTESTED — bench per `STIMULATE_HARDWARE_TODO.md` (B220 blocked at FX3, replug).
- **Silent (SD-card recorder, never transmits)** → **`survey/emanations.py`** (NEW, committed
  `5c03ae9`/`d9d90d3`): non-coherent averaging + median floor + baseline subtraction +
  harmonic-comb detection (order-density test defeats the GCD trap; estimates the device
  clock → camera-sensor hint). `POST /api/emanations/scan` (receive-only, any SDR) + PASSIVE
  EMANATION SCAN card. 12 tests. This is the passive core of the cited RFScan paper.
- Also this stream earlier: scoring explanations (`b71038d`) + BLE enrichment (`6eb3f4b`).
STILL NEXT: bench the B220 TX; Pluto interleaved stimulate variant; emanation baseline-capture UI.

---

# ⚡⚡ NEXT-SESSION HANDOFF — 2026-07-06 — B220 Mini arrived + integrated (READ FIRST)

The **SUOGOEST B220 Mini** (USRP B210-compatible, AD9361, 70 MHz–6 GHz, **2R2T**,
USB 3.0) arrived and is integrated as a third device source. Branch
**`b220-mini-source`** (off `active-stim-ble-scoring`). Full suite green (291
tests incl. 18 new). The parallel active-stimulation session's TX hooks
(`UHDRadio.start_tx/stop_tx`, `B220Source.stim_*`) are interleaved in
`uhd_source.py`/`stimulate.py` and were committed together — the two streams
interlock.

## What was built
- **`survey/sweep/uhd_source.py`** — `_UHDLib` (ctypes over `uhd.dll`'s C API;
  no UHD Python wheels exist for Win/Py3.14; struct layouts pinned by tests) +
  `UHDRadio` + **`B220Source(AD936xSource)`** with `channels = 2`:
  right module = RX ch0, left = RX ch1, so dual-phase interior sweeps drive two
  physical antenna ports. `capture_iq`/`stream_iq` (identify/analyze/listen/
  record) all wired. `UHD_IMAGES_DIR` auto-set to `%LOCALAPPDATA%\uhd\images`.
- `AD936xSource` gained **`CFG_SECTION`** ("pluto" default; "b220" override) —
  each radio reads its own config block. New `b220:` section in config defaults
  (`cal_offset_db: 36` — fc32 ±1 scale ≈ +66 dB vs the Pluto's raw counts).
- `open_device` routes `b220|usrp|uhd`; DEVICE dropdown gained "B220 Mini";
  app.js: `SDR_SOURCES` set + `isSdrDevice()` so tune/listen/analyze gates
  accept both SDRs; role names show "B220 Mini".
- Tests: `tests/test_uhd_source.py` (18: struct ABI sizes, cfg-section split,
  side→channel mapping, synthetic dual-channel sweep, stream lifecycle, gain
  backoff, factory routing).

## Windows bring-up (DONE on the tower — see SDR_SETUP.md B220 section)
Ettus WinUSB drivers installed (pnputil, all infs) · UHD 4.10 host at
`C:\Program Files\UHD` (SHA-256 verified) · b2xx images at
`%LOCALAPPDATA%\uhd\images` · probe DETECTED **B210**.

## ✅ FX3-state-5 SOLVED — it's a LibreSDR clone, needed the RIGHT FPGA image
The `fx3 is in state 5` failure was NOT a USB/cable/power problem (that theory
was wrong; it persisted identically on USB2 and after power cycles). The B220
Mini is a **LibreSDR clone on a Xilinx Artix-7 XC7A100T**, not the genuine
B210's Spartan-6 — so UHD's stock `usrp_b210_fpga.bin` could never configure it
(FPGA DONE never asserts → state 5). Fix: the correct **Artix-7 bitstream**
(2.9 MB, sha256 `0ab54295…`, from github.com/lmesserStep/LibreSDRB210). Both the
200T image (4.34 MB) AND the stock Ettus image state-5; only the 100T image
configured this unit.
- Bundled in **`native/uhd/usrp_b210_fpga.bin`** (+ Ettus `usrp_b200_fw.hex` +
  a provenance README). `B220Source._ensure_uhd_images()` installs it into UHD's
  images dir at connect, backing up any Ettus original as `*.ettus-orig` (never
  deletes). Idempotent (hash-gated). Overridable via `b220.fpga` /
  `b220.fpga_bundled`. If a future unit is the 200T variant, drop the 200T bin
  (sha `7322a2a9…`) into native/uhd/.
- Images RAM-loaded per session — nothing flashed, power cycle reverts to blank.

## ✅ LIVE-BENCH VERIFIED on the real B220 (2026-07-06)
Through the app's own code path (`POST /api/device/connect {source:b220}` on the
live server): connected as **"B210 (B220 Mini, 2RX)"**, reported **TWO modules**
(right=RX-A, left=RX-B, both 70–6000 MHz), `simulated:false`, zero warnings. UI
shows the dual-port workflow (nav "4 · SWEEP — LEFT PORT" / "5 · SWEEP — RIGHT
PORT"; footer RANGE right+left). Live IQ via `/api/identify @ 2437 MHz` →
**SNR 49.8 dB**, occupied BW ~1 MHz, identify pipeline classified it (nRF24 GFSK
candidates — a real 2.4 GHz dongle in the room). No console errors. **315 tests
green** (22 in test_uhd_source incl. the image-install tests).

## ⚑ B220 — remaining operator bench (physical antennas)
1. Antennas on **both RX2 SMA ports**. Ambient sweep → interior sweep (both LEFT
   and RIGHT phases now drive the two physical RX channels).
2. Click a baseline freq → tune/listen; ◎ ANALYZE on a known signal; ● REC.
3. Tune `b220.cal_offset_db` against a known reference (start 36).
4. Active-stim session's TX path: verify separately per its own checklist
   (TX-A port, chirp keying) — receive-only sweeps must stay receive-only.
5. Throughput on Windows caps ~20–32 MS/s (driver-stack limit for these clones);
   fine for the stepped sweep.

---

# ⚡⚡ PRIOR HANDOFF — 2026-07-04 — scoring-explain + BLE-enrich + active-ping engine

Branch **`active-stim-ble-scoring`** (off `pocsag-decoder`). NOT merged/pushed. **273 tests green.**
Three operator asks from the 2026-07-04 voice dump (+ a linked PoPETs paper). `config.yaml` backed
up to `config.yaml.bak-20260704-032113` before branching.

## THE PAPER (important correction)
The linked paper (popets-2025-0011, UCSD "RFScan") is a **PASSIVE** unintentional-EM-emanation
detector (500× non-coherent averaging + empty-room baseline subtraction + tone-clustering + DNN
fingerprint + AoA). It explicitly argues **against** the operator's "ping it and watch it react"
idea (active probing is evadable + stimulates legit devices). Both are legit; treated as separate
detectors. **RFScan-passive is NOT built yet** and is the most paper-faithful next build — it fits
the existing baseline/novelty/sigcapture architecture well (see paper text in scratchpad if needed).

## SHIPPED (each committed, tests green)
1. **Scoring explanation (DONE, `b71038d`).** `correlation.explain_anomaly_score()` →
   `ScoreExplanation(score, factors, summary)`; `anomaly_score()` delegates (arithmetic byte-identical,
   old tests pass). New `signals.score_reason` (idempotent migration); runner stores it; surfaced in
   Results + Manual-Triage tables (visible note + score tooltip), client PDF anomaly table, CSV. 6 tests.
2. **BLE enrichment (DONE, `6eb3f4b`).** New `survey/ble.py` (pure logic, 17 tests): `classify_mac()`
   pins address type (public OUI vs the 3 random kinds incl. rotating resolvable-private),
   decode manufacturer/service/OUI from curated tables (extensible via `survey/ble_extra.json`),
   flags trackers (AirTag/Find My/Tile/SmartTag/Chipolo). `inventory.scan_ble` enriches rows;
   `scan_ble_details()` + `POST /api/ble/scan` (demo-blocked) back the new **BLUETOOTH DETAIL SWEEP**
   card. UI verified in sim (no console errors; tracker card renders).
3. **Active-ping detector ENGINE (DONE, `5d5b8b7`) — TX NOT wired.** `survey/stimulate.py`: quiet-vs-
   stimulated differential PSD; `find_responders()` triple-gate (prominence + delta + not-already-
   present) so a flat stimulus floor-rise and always-on lines don't false-trigger. 9 tests vs a
   synthetic radio. Honest gates: responsive-subset-only (null≠clean), refuses out-of-ISM-band TX
   without a logged override. **Real TX path deliberately unbuilt — bench first: see
   `STIMULATE_HARDWARE_TODO.md`** (implement the small radio protocol on Pluto[interleaved]/B210[concurrent]).

## REMAINING / NEXT
- **Stimulate:** implement + BENCH the TX radio protocol (Pluto burst-then-listen / B210 concurrent),
  then wire `POST /api/stimulate/probe` (demo-block) + a UI with band-authorization + caveats up front.
- **BLE:** optionally cross-reference the standalone BLE MACs against 2.4 GHz sweep detections
  ("this 2.44 GHz energy = these advertisers"); add config.yaml `pluto.address_type` passthrough if WinRT exposes it.
- **RFScan-passive** emanation detector (the actual paper) as a distinct high-value build.
- A `--simulate` preview config was added to `.claude/launch.json` (machine-local, gitignored).

---

# ⚡⚡ NEXT-SESSION HANDOFF — 2026-06-26 — triage / monitor / novelty / dual-device BUILT (READ FIRST)

The full 2026-06-25 vision below is now **built, reviewed, and verified headless + in the live UI.** This block supersedes it.

## Branch / state
- All on branch **`pocsag-decoder`** (off main). NOT merged, NOT pushed. Bench-test on the Pluto, then merge the whole branch to main when satisfied.
- **140 unit tests green** (`python -m unittest discover -s tests`).
- `data/sweeps.db` was backed up to **`data/sweeps.db.bak-pretriage`** before the (additive, idempotent) migration ran. `config.yaml` was NOT touched.

## SHIPPED THIS SESSION (built in 9 steps, tests green at each)
1. **DB migration** (`survey/db.py`) — `signals` gained `triage_status` / `triage_notes` / `triage_updated_utc` / `is_novel_to_address` / `novelty_reason`; new **`recordings`** table; helpers `set_signal_triage` / `add_recording` / `list_recordings`; `TRIAGE_STATUSES`/`TRIAGE_DECISIONS` consts. Migrations are PRAGMA-guarded + idempotent.
2. **Novelty** (`survey/novelty.py`, new) — `compute_novelty()` (captures⋈sessions by address, `captured_utc < session.created_utc` excludes the current visit) + `mark_session_novelty()` called after each sweep's postprocess in `runner.work()`.
3. **Gated MANUAL triage backend** — `GET /api/triage/last` (last interior sweep's unexplained detections + counts + `remaining_unexplained`), `POST /api/signals/<id>/triage` (session-scoped, enum-validated). Post-hoc, no radio — works with only the RFE.
4. **Steerable channel-filter fix** (`survey/demod.py`) — boxcar `_lowpass`/`_decimate` → windowed-sinc Hamming FIR (`_design_fir`/`_fir_lowpass`/`_fir_decimate`/`_auto_taps`); ~>14 dB better stopband so click-to-isolate actually isolates. `_fm_discriminator` LEFT UNCHANGED (POCSAG shares it). `(pcm16, arate)` contract unchanged.
5. **Fused ANALYZE** — `POST /api/analyze/signal`: ONE IQ capture → `iq_features` + `identify` (sigidwiki candidates + TSCM note) + analyzer + waterfall + (POCSAG decode | demod audio) + novelty. The "real demod" that says WHAT it is. RF-only → demo-available.
6. **RECORD** — `POST /api/record/start|stop`, `GET /api/recordings`, `GET /api/recordings/<id>/audio` (session-scoped + path-traversal guard), `POST /api/recordings/purge` (MOVES to `_archive/`, never deletes). WAV under `recordings/<session>/` (gitignored).
7. **Dual-device split** (`survey/sweep/runner.py`) — `devices`/`device_locks`/`listen_stops` dicts, `connect_device(source, keep_other=)`, `busy(source=)`, `monitor_target()`, `device_roles()`, separate `_monitor_thread`. `GET /api/device/roles`, `/api/state.roles`, `/api/device/connect {keep_other}`. **Behind `pluto.concurrent_monitor` (config default FALSE) — flag OFF = byte-identical legacy single-device behavior.**
8. **UI** (`survey/web/{index.html,app.js}`) — MANUAL TRIAGE nav tab + gated review list with decision buttons/notes/completion-gate; the inspect panel is now the MONITOR with **◎ ANALYZE** + **● REC** + features/candidates/novelty/recordings; ▲ NEW novelty badges. **BUGFIX:** the inspect/monitor panel was authored inside the hidden `view-baseline` section, so every non-baseline list (hunt/results/wiki/triage) called it but it never showed — lifted to a global element at boot (`liftInspectPanel`).
9. **(NOT DONE — intentional)** flip `pluto.concurrent_monitor` to true. See the bench checklist.

## SIGNAL DECODERS — "interpret what it's transmitting, pager or not" (2026-06-26 cont.)
New modules, each with a self-test encoder + round-trip tests + a noise→empty honesty gate (no hallucinated decodes — the POCSAG-review bug class):
- **`survey/ook.py`** — generic OOK/ASK + 2-FSK device decoder (rtl_433-style): pulse/gap timing → PWM/PPM/Manchester/NRZ → bits + device guess. The long tail of cheap bugs/trackers/cameras/remotes. Gate = ≥2 byte-identical repeats. Real-protocol-faithful.
- **`survey/tones.py`** — DTMF (Goertzel) + CW/Morse→text. Real-faithful.
- **`survey/flex.py`** — FLEX paging. ⚠ **Correct framework but uses INTERNAL placeholder sync/A-word constants — round-trips its own encoder, NOT bit-compatible with off-air commercial FLEX yet.** Needs the real FLEX constants swapped + a live-capture validation pass before it reads a real pager. Until then its honesty gate keeps it silent (won't false-decode).
- **`survey/decode_router.py`** — `route_decode()` auto-tries the decoder matching `iq_features` (POCSAG/FLEX/OOK/DTMF/CW; each gate prevents false positives), `interpret()` gives the plain-English field verdict ("analog voice — LISTEN, you'll hear the room" / "FSK telemetry — decode below" / "wideband digital — encrypted, capture+locate").
- Wired into **`/api/analyze/signal`**: ONE capture → identify + auto-decode + interpretation + audio. The monitor's ◎ ANALYZE button shows it; mode picker gained FLEX/OOK/DTMF.
- **Encrypted intruders: detect/identify/locate + capture, NOT decrypt** (no key = no plaintext; attempting interception is an ECPA problem). RECORD gained **kind='iq'** (⦿ IQ button) → raw complex64 `.cf32` evidence with chain-of-custody metadata in the recordings row, to hand to authorities. Minimize: keep only what's germane.
- Decode reality on hardware: POCSAG/OOK/DTMF/CW trustworthy; FLEX pending calibration; analog = listen.

## ADVERSARIAL REVIEW (two 8-agent passes) — 4 confirmed total, all fixed + regression-tested
Decoder pass (2 confirmed): decoded content was reachable in the public demo via `/api/analyze/signal` → now stripped in demo mode (RF identification stays, decoded payload dropped); orphaned-IQ-file-on-finalize-failure → logged per never-delete policy.
Triage/monitor pass (2 confirmed):
- `/api/triage`, `/api/novelty`, `/api/record*` were not demo-gated → added to `DEMO_BLOCKED` (server.py). 2 new `test_demo_guard` cases.
- `/api/inspect` wasn't ported to the dual-device split → ported to `monitor_target()`/`busy(msrc)` (OFF-path identical).
- 3 findings refuted by the verify pass (no action).

## VERIFIED vs NOT
- **Verified:** 140 tests; live UI (no JS errors, triage end-to-end on real session #49, all new endpoints 200, panel-lift confirmed via a11y snapshot).
- **STILL NEEDS THE PLUTO (Matt):** audio quality of the new FIR demod, POCSAG on a real pager, the fused ANALYZE on live IQ, RECORD on a live signal, and — most importantly — the dual-device concurrency.

## ⚑ STEP-9 BENCH CHECKLIST (before flipping `pluto.concurrent_monitor` true)
1. Connect BOTH radios: RFE on COM3 (sweeper) + Pluto on USB (monitor).
2. Set `pluto.concurrent_monitor: true` in `config.yaml`; restart `app.py`.
3. In the DEVICE card connect the RFE, then connect the Pluto with **keep_other** (don't close the RFE). Confirm `/api/device/roles` shows `rfexplorer=sweeper`, `pluto=monitor`, `concurrent_enabled=true`.
4. Start an interior sweep on the RFE; while it runs, open a signal → **◎ ANALYZE** and **● REC** on the Pluto. They must succeed WITHOUT a "radio busy" 409 (that proves the per-source locks work).
5. The MANUAL TRIAGE pill should read "LIVE" (not post-hoc) when both radios are up.
6. NOTE: continuous live-listen-while-sweeping (the open-ended `/api/listen` stream) was deferred — `analyze` + `record` are wired for concurrency, the streaming-listen path still uses the legacy single-radio lock. Wire `_audio_blocks` to per-source state if you want gapless listen during a sweep. Full simultaneous sweep+monitor on ONE radio still waits for the July 2RX/2TX B220.

## STANDING GOTCHAS (unchanged)
- `config.yaml` is gitignored + holds live secrets — back it up before any branch switch/merge.
- A test "Flag" triage decision was left on **825.8318 MHz (sid 3282), session #49 / sweep #89** during verification — re-triage it.
- Keep the audio path + `device_lock` + POCSAG intact in any listen/sweep change.

---

# ⚡⚡ NEXT-SESSION HANDOFF — 2026-06-25 (late) — signal-hunt shipped; BIG triage/monitor vision to build (now BUILT, see above)

Context ran out mid-build. This is the continuation prompt. The core idea: **demod/triage
exists to work the ILLEGITIMATE / unknown signals, NOT the legit baseline ones.**

## Branch / state
- ALL of today's work is on branch **`pocsag-decoder`** (off main). NOT merged, NOT pushed.
  Bench-test, then merge the whole branch to main when Matt's happy.
- **101 unit tests green** (`python -m unittest discover -s tests`). Run from the repo root.
- Matt launches from the desktop; the app reads the working tree + `config.yaml` directly.

## SHIPPED & COMMITTED today (newest first)
- `ffa1275` **steerable demod** — `demodulate(offset_hz)` digital down-conversion; POST
  `/api/listen/offset` (live, no re-tune); click the live analyzer/waterfall → demod THAT signal;
  yellow cursor marks it. [Matt: "works except it doesn't work" — see OPEN Q1.]
- `60856d7` **adjustable segment size** — `segment_width_mhz` on `/api/plan` + `/api/sweep/start`
  via a `_WidthCfg` shim; "Seg MHz" input on the scan card.
- `eea245c` **manual scan → first non-baseline signal + ◂/▸/AUTO + LIVE** — GET
  `/api/scan/unknowns` (UNVERIFIED/caution interior signals, de-duped); `/api/state.live`; SIGNAL
  HUNT card in Results; `huntStep()`/AUTO timer; `#insp-live-badge`.
- earlier today: listen **waterfall rework** (DPR-sharp canvas + offscreen scroll buffer + stable
  EMA color), **POCSAG** decoder, **SSB/CW**, **Web Audio** listen, **listen-on-all-signals**,
  **libiio bundled** in `native/libiio/`. (0.15 s latency cut exists in code but config.yaml is
  back to 0.5 s — Matt's choice, leave it.)

## VERIFIED vs NOT
- Verified headless: every endpoint/DSP shape + 101 tests.
- **NEEDS BENCH (Matt, real Pluto + browser):** waterfall smoothness/sharpness, audio holding,
  JUMP-TO-FIRST-UNKNOWN + ◂/▸/AUTO, the steerable click-demod, POCSAG on a real pager.

## THE BIG BUILD — operator vision (2026-06-25 voice dumps)

### A. Two bug-sweep modes
- **AUTOPILOT** — current behavior: auto-log every detection + prioritize by anomaly score, hands-off.
- **MANUAL (gated triage)** — segment by segment: as the sweep enters a segment, mark every signal
  in it; the operator must DECIDE on each (cleared / suspect / flag / ask-client + notes, sometimes
  a client conversation), and the sweep does **NOT advance to the next segment until every signal
  is triaged**. A forced, complete per-signal review of the not-already-explained signals.

### B. Click a signal (from the live incoming list) → opens a LIVE MONITOR panel
- Monitor options: **RECORD, FLAG-for-later, NOTES, decision.**
- **"Real demod" = a per-signal ANALYZE that says WHAT IT IS**, not just audio: FUSE
  `identify.py` (modulation class FSK/PSK/AM/FM + occupied BW + FSK shift + symbol rate + SNR, AND
  the sigidwiki match w/ TSCM note) + protocol decode (POCSAG; add more) + live demod audio + the
  steerable cursor. "A monitor is different than a spectrum analyzer" → lock ONE signal and
  receive/decode/ID it live, distinct from the power-vs-freq analyzer.
- **KEY: the ID capability ALREADY EXISTS** (`identify.py` + `/api/identify` SIGNAL ID card +
  POCSAG) — it just isn't fused into the demod button (which today only plays audio = the
  "bullshit demod"). The build is to FUSE + surface it in the monitor/triage panel.

### C. NEW-TO-ENVIRONMENT (novelty) detection
- A recurring address has many prior sweeps. If a signal is **NEW** (absent from this address's
  sweep history) → flag it and show **what's DIFFERENT** (new freq / new since date / changed
  power·BW vs prior sightings). Build on the **Signal Wiki** (`survey/sigcapture.py` — already
  cross-session: per-emitter sighting counts, timeline, "hiding in a known band" variance flag).

## BUILD APPROACH (next session)
1. **Design pass (Workflow)** mapping: `survey/sweep/runner.py` (the segment loop — how a sweep
   walks segments + whether it can PAUSE/RELEASE the radio per segment), `correlation.classify_signal`,
   `sigcapture.py` (wiki cross-session history = novelty source), `identify.py` + `/api/identify`,
   the POCSAG `/api/inspect` path, and the listen/monitor stack (`inspectByFreq` + steerable demod +
   `/api/listen_pcm` + `/api/scan/unknowns`).
2. **Gated MANUAL loop** needs: a per-signal decision state (new `signals` column e.g.
   `triage_status` pending/cleared/suspect/flagged/ask-client + `triage_notes`), a segment gate, and
   AUTOPILOT left unchanged. **RESOLVE FIRST — device contention:** the sweep runs on a worker
   thread holding `device_lock`; you can't monitor (needs the radio) while a live sweep holds it.
   Likely answer: triage the **last sweep's stored detections post-hoc** (radio free), OR have the
   sweep **pause + release** the radio between segments. Confirm with Matt (OPEN Q2).
3. **Per-signal ANALYZE / monitor panel:** one action = identify + decode + tune the steerable demod
   + show novelty + decision/notes/record/flag UI. Reuse **RECORD (Phase 4, still UNBUILT — design
   in the workflow output `w7715t2vn`: `runner.start_recording` worker mirroring `start_directional`,
   a `recordings` DB table, `/api/record/start` + `/api/recordings` + `/recordings/<f>`, gitignored
   `recordings/`).
4. **Novelty:** query the wiki/signals history for this session's address; tag hunt/triage rows
   "NEW since <date>" / "seen N prior sweeps" + the delta.

## OPEN QUESTIONS for Matt
1. Steerable demod "works except it doesn't work" — real BUG (clicking doesn't isolate, audio still
   muddy → sharpen the channel filter; today it's a crude boxcar that aliases far signals), or just
   "pointless until fused with ANALYZE/monitor"? Clarify.
2. Gated manual: pause-the-live-sweep-per-segment (radio contention) vs triage-the-last-sweep
   post-hoc? (Recommend post-hoc, or pause+release.)
3. RECORD defaults: audio vs IQ, retention (`recordings/` is client-sensitive PII — must purge per
   engagement).

## STANDING GOTCHAS
- `config.yaml` is gitignored + holds live secrets (OpenCellID key, ULS path, `pluto:` section) and
  gets **clobbered by branch switch/merge** — back it up first. (memory: spectrum-survey-config-hazard)
- Audio works at 0.5 s blocks. libiio is bundled in `native/libiio/`. AD9364 extension is OFF
  (radio reports 325–3800 MHz — no FM/5.8 GHz until re-flashed).
- Keep the audio path + `device_lock` + POCSAG intact in any listen/sweep change.

---

# SESSION HANDOFF — 2026-06-25 — main merge + POCSAG decoder (earlier today)

Two things this session: (1) the whole `pluto-sdr-source` line was merged into **`main`**
(`--no-ff`, commit `eb1a176`); (2) **demod #3 — a POCSAG paging decoder — was built** on a new
**`pocsag-decoder`** branch (off main). **101 tests green** (`python -m unittest discover -s tests`).

### ⚠️ config.yaml got clobbered by the merge — ACTION NEEDED
`config.yaml` was tracked on main but un-tracked+gitignored on the branch (commit `00e8625`).
`git checkout main` + the merge silently overwrote then deleted the live working copy; the live values
were never in git (correct, for secrets) so they're **unrecoverable**. I restored the last *committed*
version (loads fine, gitignored, not re-tracked) but it predates the Pluto work. **Re-enter in
config.yaml:** `api_keys.opencellid` (your key), `uls.extract_dir` (your FCC ULS extract path), and a
`pluto:` section (your tuning / extended-range choice — app runs on `survey/config.py` defaults
meanwhile). LESSON: that file is gitignored-but-holds-live-state — copy it aside before any branch
switch/merge.

### POCSAG decoder (branch `pocsag-decoder`, commits 6c61258 + d67c6fe — NOT merged to main)
- **`survey/pocsag.py`** — numpy DSP front end (shared `demod._fm_discriminator`, fractional-timing
  bit slice) + pure-Python BCH(31,21)+parity check/2-bit-correct, batch walk, numeric/alpha decode.
  Tries all bauds (512/1200/2400); polarity auto-resolved by the FSC/FSC_INV lock. `encode_pocsag()`
  lives alongside for self-testing. Honesty: every page carries `bits_corrected`/`uncorrectable`;
  no-signal / garbage-behind-a-false-lock returns EMPTY (a confidence gate on clean codewords), never
  hallucinated pages.
- Integration: `/api/inspect` decodes when `mode=='pocsag'` (~3 s one-shot capture -> `decode` key);
  inspect-panel **POCSAG (decode)** dropdown mode lists pages in `#insp-decode` (textContent, third-party
  text stays internal); `/api/identify` + SIGNAL ID card show a "looks like a pager" hint.
- **Adversarially verified** (3-lens workflow + adjudicator): FSC/IDLE/BCH-0x769/(32,22)dmin=6/RIC/
  numeric/alpha all independently confirmed spec-correct; the review caught the locks[0] + garbage-batch
  false-page bugs, now fixed. External golden-vector tests added (not encoder-generated).
- **STILL HARDWARE-UNTESTED:** no Pluto + no real pager signal here. Bench-verify: tune a known pager
  freq, pick POCSAG mode, confirm real pages decode. `pocsag-decoder` is NOT merged to main yet
  (awaiting that check) — merge when satisfied.

---

# SESSION HANDOFF — 2026-06-24 (later) — demod backlog #1+#2 + Web Audio fix

Four tasks off the prior handoff's PENDING list landed on `pluto-sdr-source` (now **18 commits
ahead of main, still NOT merged**). **75 unit tests green** (`python -m unittest discover -s tests`).
Each task was built by a separate agent; integrated, tested, and committed by the orchestrator.

DONE this session:
1. **Loose Jun 19–20 working-tree files committed** (`76a31d2`) — DEMOD_LISTEN_DESIGN.md,
   FIELD_KIT_BOM.md, SIGNAL_IDENTIFICATION_GUIDE.md, tools/{export_appendix_a,listen_helper,
   render_gallery}.py, marketing/{dcs_explainer.html, partnership DRAFT}. Secret-scanned before
   commit; `marketing/netlify_build/` gitignored as a deploy duplicate; README +14 lines.
2. **SSB (USB/LSB) + CW demod** (`34e5052`) — added to `survey/demod.py` demodulate()/pick_mode()
   and the `#insp-mode` <select>. New mode keys: `usb`, `lsb`, `cw`. 4 new tone/selectivity tests.
   They flow straight through /api/listen[_pcm] (no server-side allowlist).
3. **Web Audio chunked playback** (`36e29a2`) — fixes the real-browser audio stutter. Server:
   `_validate_listen_request()` + `_audio_blocks()` shared generator (owns the
   preempt/device_lock/teardown contract); NEW **`/api/listen_pcm`** (raw int16 + one-line JSON
   header) alongside `/api/listen` (WAV, now the fallback). Client: `Listen` module fetch-streams
   PCM and schedules AudioBuffers against the AudioContext clock (0.25 s lead, re-anchors on
   underrun). `openInspect` prefers it, falls back to the `<audio>` element.
4. **Listen on ALL signals** (`0faa788`) — `inspectByFreq(center,label,mode?)` core entry point;
   listen affordances added to Results (`#tbl-anomalies`), detected Signals (`#tbl-signals`), and
   the Signal Wiki (a ▶ in `#tbl-wiki`). The shared delegate runs in the **capture phase** so the
   wiki ▶ doesn't also open the row's detail editor. **POI got NONE** — `persons_of_interest` has
   no frequency field and inventing one would breach the POI-internal-only guardrail. Also fixed a
   long-standing double-"MHz" in the inspect title + out-of-range message (`fmtF` already carries
   the unit and emits GHz/kHz too).

⚠️ STILL NEEDS THE OPERATOR / HARDWARE (could not be verified headless — no Pluto, preview can't
play audio):
- **Smooth audio in a REAL browser** is the one unverified thing. Routes + engine are proven (75
  tests; both stream routes return clean 409/400 via the Flask test client, no 500s), but audio
  can't sound in the headless preview. VERIFY: `python app.py` → connect the Pluto (**antenna on
  the RX SMA**) → click a listen frequency on a Results/Wiki/baseline row → press ▶ if autoplay is
  blocked → expect continuous audio + a moving analyzer/waterfall with no stutter. If it stutters,
  raise `PCM_PLAY_LEAD_S` in `survey/web/app.js`.
- Confirm the new Results/Wiki listen affordances actually tune + play on a connected unit.

▶ STILL PENDING from the demod backlog (NOT started):
- **Digital decoders (POCSAG first)** — the hard one; scope per the older handoff block below.
- Secondary audio items still open: werkzeug ~0.8× delivery / true gapless buffer
  (`set_kernel_buffers_count` reset between snapshot and stream); calibration is approximate.

---

# SESSION HANDOFF — 2026-06-24 (earlier) — ADALM-Pluto SDR repoint

The whole session repointed the app from RF Explorer to the **ADALM-Pluto SDR** and built a
tune/view/listen/identify stack on it. All work is on branch **`pluto-sdr-source`** (13 commits,
**NOT merged to main**). 71 unit tests green (`python -m unittest discover -s tests`).

## Environment (already set up on THIS machine — a fresh clone elsewhere must redo these)
- `pip install pyadi-iio` (0.0.21) + `pylibiio` installed (user site). numpy present.
- **Native libiio v0.25 DLLs** copied to `%LOCALAPPDATA%\libiio\bin` and added to the **user PATH**
  (libiio.dll, libusb-1.0, libxml2, libserialport-0, msvcp140, vcruntime140, iio_info.exe).
  Without this, `import adi` fails with `LoadLibrary() argument 1 must be str, not None`.
- **ADI PlutoSDR-M2k USB drivers** installed (RNDIS + WinUSB), else Device Manager shows the RNDIS
  and IIO functions in *Error* and nothing reaches the radio.
- **AD9364 frequency extension APPLIED to the physical Pluto** (it's an AD9363A): serial console
  COM5, login root/analog, `fw_setenv attr_name compatible` + `fw_setenv attr_val ad9364` + reboot.
  Now tunes **70 MHz–6 GHz** + up to **56 MHz BW**. REVERSIBLE: set `attr_val ad9363`. The
  pluto serial-console / probe scripts are in `%TEMP%\pluto_*.py` (transient).
- **`config.yaml` is now UNTRACKED + gitignored** (it holds the OpenCellID key, ULS path, the
  extended freq range, and pluto tuning). The app runs on `survey/config.py` DEFAULTS if absent.
- Run live: `python app.py` then pick **ADALM-Pluto** in the DEVICE dropdown (or `SWEEP_SOURCE=pluto`,
  or `device.source: pluto`). Default source is still `rfexplorer` (unchanged behaviour).
- **Drive/verify in the browser via the preview tool** (`preview_start` config `spectrum-survey-live`,
  port 8765). NOTE the preview is a **headless/sandboxed browser → media (audio) does NOT play there**;
  spectrum/data verify fine, but audio playback must be checked in a REAL browser.

## What was built (all on the branch; commit ids in `git log main..HEAD`)
1. **`survey/sweep/sdr_source.py`** — `AD936xSource` + `PlutoSource` as a `BaseDevice` (so planner/
   runner/detector/UI are untouched). Synthesises a wide `SweepFrame(MHz,dBm)` via tune→IQ→Welch
   PSD→**DC-guard discard**→stitch. `capture_iq()`, `stream_iq()` (gapless via
   `set_kernel_buffers_count(4)` at connect), `channels` (1; B220→2). numpy-only DSP.
2. **Runtime device picker** (DEVICE card): RF Explorer ⇆ Pluto, persists to `device.source`.
   `/api/device/connect` takes `{source}`; `/api/state` reports active `source`.
3. **sigidwiki identification** — `survey/identify.py` (IQ modulation features: occupied BW, FSK
   shift/states, duty, spectral flatness, SNR; SNR-gated so empty channels read `noise_floor`) +
   `survey/sigidwiki_ref.json` (curated ref; **lives in survey/ because data/ is gitignored**) +
   `/api/identify` (best-of-N IQ to catch bursts) + the "SIGNAL ID" card.
4. **Baseline sources configured**: **FCC ULS** (downloaded Land Mobile extracts → gitignored
   `data/uls_extract/`, ~1.3 GB; `uls.extract_dir` set; ~23s first scan then cached) and
   **OpenCellID** (key in config; **fixed its bbox bug** — getInArea caps at 4 km², now tiled).
   FCC FM/AM/TV work but need broadcast radius ~25 mi (transmitters are 10–40 mi out — UI note added).
   Still NOT configured (need operator creds): RadioReference, CellMapper import.
5. **Click-to-inspect / tune-view-listen**: click any frequency in the EXPECTED-SPECTRUM BASELINE →
   tune the Pluto → **live analyzer + scrolling waterfall + demodulated audio**, with Next/Prev.
   Backend: `/api/listen` (open-ended WAV stream; tune once, demod live IQ blocks; WFM/NFM/AM via
   `survey/demod.py`) + `/api/listen_spectrum` (latest analyzer+waterfall, polled every 400 ms by
   the UI). Device access serialised by `controller.device_lock` + `listen_stop` (sweep/inspect
   preempt the stream). `.freqlink` styled theme-cyan.

## ⚠️ OPEN ISSUES (verify/fix these first)
- **Audio playback in a REAL browser is UNVERIFIED.** The continuous stream works server-side
  (proven: bytes keep flowing; spectrum frames advance). But the headless preview can't play audio
  (`currentTime=0`), and the **werkzeug dev server throttles the stream to ~0.8× real-time → likely
  rebuffers/stutters.** Probable proper fix: **Web Audio API chunked playback** (fetch + decode +
  schedule) instead of `<audio src=/api/listen>`, and/or a real streaming server. **Confirm in the
  operator's browser first** (refresh, click an in-range freq, press ▶ if autoplay blocked).
- **Antenna**: a live test saw FM 98.5 at ~−124 dBm (noise floor) → make sure an antenna is on the
  Pluto **RX SMA**, else everything looks flat/sounds like hiss.
- **Gapless/real-time**: `set_kernel_buffers_count(4)` is set at connect, but an inspect/`capture_iq`
  before a stream recreates the buffer and resets it → stream ~0.6–0.8×. True gapless needs avoiding
  buffer destroy/recreate between snapshot and stream (or feeding the snapshot from the stream).
- **Calibration**: `pluto.cal_offset_db = -30` is approximate, NOT lab-grade. Use baseline-delta.

## ▶ PENDING — operator-requested demod work (selected 2026-06-24, NOT STARTED). Build all three:
1. **Listen on ALL signals, not just baseline rows.** Wire the existing tune→demod→audio+waterfall
   (the `openInspect`/inspect-panel flow in `survey/web/app.js`) onto the **sweep-detected signals
   (Results tab), the Signal Wiki, and POI**. Mostly UI wiring: make an `inspectByFreq(center,label,
   mode?)` entry point not tied to `App.blView`, and add clickable "listen" affordances on those
   lists. Backend (`/api/listen`, `/api/listen_spectrum`) already supports any freq.
2. **More analog modes: SSB (USB/LSB) + CW.** Add to `survey/demod.py` `demodulate(iq,fs,mode)`:
   USB/LSB (complex frequency-shift to put the sideband at baseband, low-pass, take real) and CW
   (beat against a BFO offset → audible tone). Add to the mode `<select>` and `pick_mode()`. Unit-test
   in `tests/test_demod.py` like the existing FM/AM tone tests.
3. **Digital decoders (POCSAG / DMR / etc.).** Substantial new DSP per protocol — scope carefully.
   **Start with POCSAG** (2-FSK paging → readable text: discriminate FM, slice bits at 512/1200/2400
   bps, sync on the preamble/frame-sync word, decode BCH/text) as the most tractable, highest-TSCM-value
   one (paging is plaintext). DMR/P25/etc. are much harder — consider an external decoder or defer.
   Surface decoded output in the inspect panel / wiki.

## Key new/changed files this session
New: `survey/sweep/sdr_source.py` · `survey/identify.py` · `survey/sigidwiki_ref.json` ·
`survey/demod.py` · `SDR_SETUP.md` · `tests/{test_sdr_source,test_identify,test_demod,pluto_live_check}.py`.
Changed: `survey/sweep/{device,runner}.py` · `survey/server.py` · `survey/config.py` ·
`survey/web/{index.html,app.js}` · `.gitignore` · `requirements.txt`.
Read **`SDR_SETUP.md`** for the SDR config knobs, the extension procedure, and the calibration note.

---

## What this is / how to run

- TSCM field spectrum survey app. Python/Flask local web UI at `localhost:8765`.
- Location: `C:\Users\Union Terminal\Desktop\Shield_TSCM_Business\spectrum_survey\` (its own git repo).
- Run live (real RF Explorer): `python app.py`  → open http://127.0.0.1:8765
- Run without hardware: `python app.py --simulate` (loudly marked SIMULATED everywhere).
- Tests: `python -m unittest discover -s tests` (38 passing).
- Pipeline: address → database baseline (FCC FM/AM/TV, ULS, OpenCellID, CellMapper import, static allocations) → dual-module RF Explorer sweep → detect/classify (VERIFIED/PLAUSIBLE/UNVERIFIED) → anomalies, expected-vs-found, room localization, directional → client PDF + internal memo.

## Hardware ground truth (verified on the unit)

- RF Explorer 6G Combo PLUS, fw 03.45, COM3 (CP210x). Raw-UART driver is primary (official lib falls back).
- **LEFT SMA = mainboard = 50 kHz–960 MHz. RIGHT SMA = expansion = 240 MHz–6.1 GHz.** (Confirmed by operator; do not flip.)
- C2-F needs the full 4-field form; module switch needs settle+drain; `$S` frames validated by `\r\n`. All in `survey/sweep/device.py`.

## ⚑ GUARDRAILS — these were explicit, repeated decisions. PRESERVE THEM.

1. **No demographic IQ/g prediction.** The operator asked for predicted-IQ-from-address/assets/job on named people; this was declined as invalid (SES proxy) and a liability. Capability is assessed from **evidence about the specific person** (profession, credentials, demonstrated skill) — and the API **requires a factual basis** when a capability tier is set (`/api/poi` rejects a tier with no basis). Do not add demographic cognitive scoring even if asked again; offer the evidence-based version.
2. **Persons of Interest / background = INTERNAL ONLY.** Client-facing docs (pre-sweep briefing, post-sweep findings report) contain **only** public RF baseline + methodology + engagement terms + the client's own stated concerns. Verified that "Persons of Interest", "capability", "IQ" are absent from client PDFs. The POI/assessment material lives only in the **Confidential Internal Memo** (`generate_internal_memo`), which carries confidentiality + "not a consumer report / not for adverse action / investigative hypothesis not fact" language.
3. **Background data (BeenVerified etc.):** client-authorized, permissible-purpose only, no dragnet on uninvolved third parties. Stored in `case_files/` (local, gitignored). The app never auto-ingests/parses it into profiles.
4. **RF exposure report section:** balanced (both sides), IARC 2B stated accurately, conflicts of interest disclosed **including Shield's own** (Shield sells shielding), framed on **security not health**, no medical claims, defers to a physician. Do not turn it into fear-selling.

## What is already built (do not rebuild)

- Baseline sources + caching + degraded-mode; geocoding (Census/Nominatim).
- Correlation engine, detector, planner (unit-tested). Sweep runner: dual-module, AVG+MaxHold, abort/resume, directional, calibration mode, low-frequency artifact filter.
- Multi-trace live chart (realtime/avg/maxhold) + waterfall + live known/unknown ledger.
- WiFi/BLE inventory (owned/unaccounted). CellMapper import (`tools/cellmapper_capture.js`).
- **Signal wiki**: every sweep auto-archives every detected signal (rendered from sweep data) as an observation; grouped by emitter with sighting counts; cross-session timeline + variance flag ("hiding in a known band"); manual high-iteration zoom captures; AI descriptions (Claude API, numeric profile only) via `api_keys.anthropic`.
- **Expected-vs-found**: predicted licensed stations not received, with reason (not swept / below threshold / intermittent).
- **Persons of Interest / deconfliction** panel (internal): access/means/motive, evidence-based capability+basis, CLEARED/OPEN/CONCERN.
- **Pre-sweep briefing PDF** (`generate_presweep_report`): baseline + terms (onsite/remote-deposit-ship-return/monitoring) + client concerns. **Confidential internal memo PDF** (`generate_internal_memo`): POI assessment.
- **Engagement workflow** + **technician task checklist** (seeded on engagement save). **Case-file upload**. **Report options** (RF environment & shielding, RF exposure context, glossary) toggled on Results tab.
- Discord "ask community" (sanitized). CSV export. Session-independent, deep-linkable views (`#wiki`, `#poi`, etc.).

## OPEN WORK — P1–P5 ALL COMPLETED 2026-06-11 (commits 27514bf…ae70350)

**P1 ✅ Reports render as visible links.** All three generate buttons (post-sweep header, pre-sweep on Session tab, internal memo on POI tab) now show a persistent `OPEN PDF / DOWNLOAD` link next to the button — `window.open` removed everywhere. Bonus root-cause fix: on a plain boot (no `#hash`) `show()` never ran, so the engagement form stayed blank and the pre-sweep button would have saved blanks over the real engagement; boot now always routes through `show()`.

**P2 ✅ Per-POI documents.** Creating a POI auto-adds a technician task to gather that person's client-authorized background; POI panel has an ATTACHED DOCUMENTS block (case_files gained `poi_id`); session Case Files table shows the linked POI.

**P3 ✅ Tower-proximity shielding in pre-sweep.** `rf_density()` folds in cellular (per-carrier tower counts deduped across OpenCellID/CellMapper, nearest-tower distance; recommendation triggers on broadcast density OR tower proximity). Section renders in the pre-sweep behind the same `rf_environment` option, now also toggleable from the Session tab. **Note: until the OpenCellID key or a CellMapper import exists, the section honestly reports tower data unavailable.** 6 unit tests cover the logic.

**P4 ✅ POI opportunity vectors.** New internal-only field (shared workplace, custody exchanges, common associates…), rendered in the internal memo as hypothesis-framed "where to look" guidance. Absent from all client documents.

**P5 ✅ Case-study / device library.** CASE STUDIES tab; entries render in client reports ONLY when the operator attests they are de-identified composites. Both operator composites seeded (household-member consumer device; vehicle GPS/audio kit + phone mirroring). Optional `case_studies` report section in pre- and post-sweep; left OFF for session 16 — per-engagement choice.

## OPERATOR ASKS — 2026-06-11 voice review of the first briefing

Done same day (commit f0b0062): grouped baseline w/ top-5 per category; cellular-carriers
subsection; **standing rule: internal/config state (NOT_CONFIGURED, "data not available") never
appears in a client document**; neutral allusion to client-named persons (no names/assessments —
guardrail intact).

**P6 — Behrens-style report template.** Use
`_source/Shield Security/Past Clients/Behrens/SSS-2025-iuR-021-FINAL_Behrens_Estate-Security-Review.pdf`
(37 pp) as the structural baseline for client reports: cover with property photo · executive
summary · table of contents · scope/confidentiality/methodological limitations · band-by-band
clearance table (CLEAR/expected per band) · carrier tower density line (per-carrier counts,
dominant flagged) · risk matrix · countermeasure status table · appendices (camera inventory,
RF charts, tower detail). Needs: property-photo upload per engagement (report asset), exec-summary
text from findings, TOC machinery in reportlab.

**P7 — Cell-tower map with sector azimuths over property imagery.** Behrens did this with
CellMapper screenshots (sector fans + per-cell azimuth like SW 221°). App version: render a map
figure with tower positions + azimuth wedges aimed relative to the client address. Imagery
reality check: satellite tops out ~30 cm/px commercial (NAIP free tier ~60 cm) — centimeter-class
means drone/ground photos, like the Behrens tower photo. If Matt's Google Earth Engine
subscription is to be used, need its service-account credentials in config; otherwise use the
property photo + a schematic map. Azimuth data comes with the CellMapper export
(`tools/cellmapper_capture.js`), not OpenCellID.

**P8 — Configure cellular tower sources (operator action).** OpenCellID needs Matt's free API
key (opencellid.org → account → API key → paste into `config.yaml` under `api_keys.opencellid`).
Claude cannot create the account/credentials. Alternative he already uses: export towers from his
CellMapper session via `tools/cellmapper_capture.js` into `data/cellmapper_towers.json`. Until
one exists, the carriers section uses the all-national-carriers wording (no counts).

## OPERATOR ASKS — 2026-06-20 voice dump (explainer demo + positioning + Shield partnership)

Core complaint: **the demos currently on the website (DCS demo + NMP Labs post) do NOT explain
what the product is — especially the TSCM side.** A prospect can't tell what it does or why it
matters. Need a demo that *explains* and *sells*.

- **D1 — Explainer demo for the website.** A demo that can be shown to people and actually
  conveys what DCS/TSCM is and its value (not just a bare tool a prospect has to decode).
- **D2 — Before/after sweep narrative.** Core story = the difference the software makes:
  the OLD way (technician writing readings down by hand on paper/clipboard, nothing visible
  to the client) vs. the NEW way (signals appearing on screen in real time; the customer can
  *watch what comes back* live). Make the contrast the centerpiece.
- **D3 — Video of someone conducting a sweep.** Wants actual footage of a sweep, before vs.
  after. (Claude can't shoot live video — deliverable is a shot-by-shot storyboard + voiceover
  script for Shield to film, and/or an auto-playing animated/web "replay" that looks like a
  live sweep and can be screen-recorded with narration.)
- **D4 — Competitive market research / positioning.** Go research the TSCM market and the tools
  competitors use; determine whether — and specifically *why* — DCS is better. Claims must be
  substantiated (see flag below).
- **D5 — Professional-use positioning.** Any professional engaged should be using Shield/DCS
  software (i.e. the software is the standard the operator works to).
- **D6 — First partnership announcement: Shield Security.** Announce DCS's first partnership
  with Shield Security; for now Shield Security has **EXCLUSIVE access** to the software.
  Counterparty/public-facing → draft carefully, confirm exact entity name + exclusivity terms
  before publishing.

### 2026-06-20 — explainer page DEPLOYED to the public demo (replaces bare tool on NMP)

- Built `marketing/dcs_explainer.html` (self-contained explainer + auto-playing before/after sweep
  animation). Matt approved ("that's beautiful") and asked to put it live "instead of the one we've
  got currently on NMP."
- **Deployed via the Render demo, NOT Wix** (the page is live JS/canvas — can't be a Wix blog post).
  The NMP Labs post already links DCS → `dcs-demo.onrender.com`, so making the explainer the front
  door of that URL auto-replaces what NMP shows. No Wix nav surgery needed.
- Change made in the **PrivateSweepDemo** repo (github.com/middyfiddyrid/PrivateSweepDemo, Render
  source; cloned fresh to verify — git creds are cached, push works): added `survey/web/explainer.html`
  + remapped routes in `survey/server.py` — **`/` now serves the explainer; the interactive tool moved
  to `/app`** (assets are absolute `/static/..` + `/api/..`, so the move is safe). Commit e00609c,
  pushed to `main` → Render auto-deploy.
- **DIVERGENCE (intentional):** only the PUBLIC DEMO repo has `/`=explainer, tool=`/app`. The
  **operational app in THIS repo is unchanged — `/` is still the tool** (the on-site operator wants
  the tool at root). If the demo build is ever regenerated from this repo, re-apply the route swap.
- **OPEN — partnership wording is now PUBLIC:** the page states "Shield Security — exclusive launch
  partner." Matt's dump said "Shield Security System(s) … has exclusive access." Confirm exact legal
  entity name + whether to read "exclusive access" vs "exclusive launch partner" → one-word edit +
  redeploy. Formal announcement still drafted (unpublished) at
  `marketing/shield_partnership_announcement_DRAFT.md`.

## 2026-06-23 — ADALM-Pluto SDR source added (RF Explorer repoint)

The sweep engine can now run from an **ADALM-Pluto** SDR as well as the RF
Explorer. Selected by `device.source: pluto` in config or `SWEEP_SOURCE=pluto`
(default stays `rfexplorer` — existing behaviour byte-for-byte unchanged).

- **New file `survey/sweep/sdr_source.py`**: `AD936xSource` (stepped-sweep + Welch
  PSD + DC-guard + stitch; numpy-only) and concrete `PlutoSource`. Implemented as
  a `BaseDevice`, so planner/runner/detector/UI/reports are **untouched** — it
  synthesises the same `SweepFrame(freqs_mhz, amps_dbm)` the RF Explorer produces.
- `BaseDevice` gained a `channels` property (1 for RFE/Pluto, 2 for the July
  **B220** which will subclass `AD936xSource` for simultaneous dual-channel DF)
  and a `capture_iq()` hook (for the future demod/BLE listen layer — RFE can't).
- **Hardware verified live** on a real Pluto (Rev.C **AD9363A**): contiguous
  2.4 GHz sweep, DC/LO-leakage spike removed (median center artifact ≈0.7 dB),
  `capture_iq()` round-trips. See `tests/pluto_live_check.py` (live) and
  `tests/test_sdr_source.py` (hardware-free DSP, in the 55-test suite).
- **Stock range only**: this AD9363A tunes **325 MHz–3.8 GHz** (probe-confirmed;
  no 5.8 GHz / FM). Operator chose NOT to apply the AD9364 frequency extension for
  now — `SDR_SETUP.md` documents the unlock + the `require_extended` startup guard.
- **Windows deps installed**: `pyadi-iio`==0.0.21 (+ pinned in requirements),
  native **libiio v0.25** DLLs in `%LOCALAPPDATA%\libiio\bin` (on user PATH), and
  the ADI **PlutoSDR-M2k USB drivers**. Full setup in `SDR_SETUP.md`.
- **Calibration is approximate** (`pluto.cal_offset_db`, default -30): relative
  dBFS+offset, NOT lab-grade dBm — lean on baseline-delta detection.
- Demod/BLE (`demod.py`/`ble.py` in the brief) do NOT exist yet; only the
  `capture_iq()` hook was added, per `DEMOD_LISTEN_DESIGN.md` (a future build).

### Remaining nice-to-haves (not blocking)

- Operator still needs to be walked through generating his first report (everything verified working in the live UI on 2026-06-11).
- OpenCellID API key / CellMapper import still unconfigured → cellular proximity reads "unknown" in the shielding section.
- Test PDFs in `reports/` from verification runs (session16 interim/presweep/memo dated 06-10 and 06-11) — operator should clear these out so they aren't mistaken for the real Grannemann briefing. Test upload artifact archived at `case_files/_test_artifacts/`.
- Session 19 (502 Winchester Dr, Rolla) exists alongside session 16 (8214 Kenwood, Grannemann). The server auto-opens the NEWEST session on restart — confirm with the operator which matter is active.

## How to verify your work

- `python -m unittest discover -s tests` (keep 38+ green).
- Use the preview tooling (`preview_start` with launch config `spectrum-survey-live` for hardware, or add a `--simulate` config) to drive the UI; `preview_screenshot` to confirm.
- For PDF content checks, generate via the API and read with `pypdf` (see `tests/expected_check.py` pattern).
- Real hardware is on COM3; `python app.py` then connect. For UI-only work use `--simulate`.
- The operator's real sessions are in `data/sweeps.db` (gitignored). Session 16 (Kenwood) has baseline + ambient/aborted data — no completed interior sweep, so its reports are INTERIM. A real FINAL report needs the LEFT+RIGHT interior phases actually run.

## Key files

`app.py` · `survey/server.py` (routes) · `survey/report.py` (all PDFs) · `survey/sweep/{device,runner,planner,detector,calibration,simulator,sdr_source}.py` · `survey/sigcapture.py` (wiki) · `survey/expected.py` · `survey/correlation.py` · `survey/baseline/*` · `survey/web/{index.html,app.js}` · `config.yaml` · `SDR_SETUP.md` · `tests/*`

Standing notes: never delete files (move/rename). All client/business language in the PDFs (engagement terms, exposure wording, confidentiality block) is draft and needs the operator's/counsel's review before regular use.
