# Session handoff — 2026-07-19 (Shield Sweep)

Read `Dog Pics'\CLAUDE.md` first. This file = what THIS session built + what Matt
still has to do. Previous handoff (B220 uhd_error 44 guidance, lightbox/netscan
features): `SESSION_HANDOFF_2026-07-15.md`.

## Built this session: DEVICE LAB guided capture v2 (Matt's spec)
Matt's ask: camera first → guide the tech until the DUT is identified BY VISUAL
and BY RF BEHAVIOR (near-field, then omni, then directional), with the camera
recording while the device moves closer/further from the NF probe, then the app
TELLS the tech each move (back to 10 ft, next room, and so on) and how to
exercise the device (put a camera in record mode etc.).

The old flow (Grok's) took ONE 6.5 ms IQ grab per button press — bursty DUTs
(fobs, Wi-Fi) were usually missed and the tech had to walk back to the laptop.

### New flow (all live in the source; 499 tests pass)
photo → identify → antenna → **near-field LIVE** (freq scan + WALK TEST) →
10 ft → next room → custom stops → **exercise states** → stim → demod → commit.

- **`survey/lab_live.py` (NEW)** — LiveSession thread: continuous short IQ grabs
  (~65k samples each), rolling power trace, PEAK-HOLD spectrum (bursts kept),
  best-SNR feature set, frequency SCAN over catalog guesses until energy locks,
  stability detector for auto-capture. One session at a time; owns the monitor
  lock; `harvest()` records a stop/state from the accumulators and RESETS them
  WITHOUT stopping — the tech walks the whole ladder hands-free.
  GOTCHA: the stop event is `self._halt` — naming it `_stop` clobbers
  `threading.Thread._stop` and breaks `join()`.
- **WALK TEST** (attribution proof): app speaks a move-in/move-out cadence
  (8 s laps × 3), webcam records via MediaRecorder synced to the RF clock
  (saved `data/device_lab/videos/`, served at `/api/device_lab/media/…`), and
  `device_lab.walk_score()` Pearson-correlates received power against the
  commanded cadence. r ≥ 0.6 + swing ≥ 6 dB → `tracks_motion` = "the emission
  IS the device in your hand". Verdict + r + swing stored in pkg["walk_test"].
- **Identity fusion** `device_lab.fuse_ident()`: visual/text catalog match +
  RF signature candidates across all captures/states + walk attribution →
  status ladder unknown → visual_only → rf_confirmed → identified. Shown in
  the triage IDENT panel and stored in pkg["ident"].
- **Range ladder**: near-field FIRST (find + freq-lock + prove), then 10 ft,
  next room, plus ADD STOP ("down the hall", "outside" — `add_stop`, step
  `capture_stop:<id>`). Auto-capture fires when the live reading holds steady
  3 s; chime + spoken next instruction (speechSynthesis, `.dl-voice` toggles,
  localStorage `dl_voice`). Manual ⦿ CAPTURE NOW is the fallback.
- **Exercise states** `EXERCISE_STATES` / `exercise_states_for()`: per-class
  scripts (Wi-Fi cam: standby/record/stream/off; fob: idle/press/hold; cell:
  idle/active/airplane; default idle/active/off). Each state harvested into
  pkg["states"] — powered-off proves silence. A later sweep can match "camera
  in record mode", not just "2.4 GHz wideband".
- **Server** (`survey/server.py`): new endpoints under `/api/device_lab/<id>/`:
  `live/start|state|stop|retune|walk`, `stops`, `state_capture`, `video`,
  `media/<kind>/<name>`. `/capture` now harvests the live loop when running
  (else one-shot fallback), accepts custom stop ids, and gates on
  `capture_iq` capability (NotImplementedError → 409) instead of is_swept_sdr.
  stim/demod/commit call `_lab_stop_live()` first (loop holds the monitor lock).
- **Simulator** now implements `capture_iq` (synthetic carriers, power
  "breathing" on an 8 s period) so the WHOLE lab is drivable with --simulate.
  CAVEAT: in sim the walk verdict phase-aligns by luck — tracks_motion one run,
  no_track the next. That is the sim, not a bug; walk_score is unit-tested.
- **UI** (`index.html` + `app.js`): triage step (live trace + peak-hold canvas,
  camera PiP, walk pill, IDENT panel), ladder step (progress pills, GO LIVE
  hands-free, auto-capture, add stop), states step (state cards). Old
  freq_confirm step kept for legacy packages.

### Verified (isolated sim instance, port 8899, scratchpad copy)
Full end-to-end in the Browser pane: scan→lock 433.92 plant (SNR 59 dB), walk
test r=0.96 tracks_motion (and a no_track run), near-field harvest (77 grabs
peak-hold), auto-capture 10 ft/next room, custom stop, 3-state matrix, ident
"IDENTIFIED — … motion-attributed at near field", commit + signature library
entry. Zero JS console errors. Tests: `python -m unittest discover -s tests`
→ 499 pass (new: test_lab_live.py; test_device_lab.py extended).

## Matt must do (to see it on his rig)
1. **Restart the app on 8765** (backend changes — new endpoints/modules).
2. **Ctrl+F5** the browser tab (new app.js/index.html).
3. Allow **camera** permission for the walk-test video; voice prompts are
   browser speech (no install). HTTPS not needed on localhost.
4. Verify with the REAL test transmitter: walk test should read tracks_motion
   when carrying it, no_track when the energy is something else.
5. Auto-capture thresholds may need field tuning: `lab_live.py`
   SNR_STABLE_DB=6, STABLE_TOL_DB=3, 3 s hold.

## Also built this session (Matt's attenuator/frequency questions)
**Overload protection (attenuator/LNA discussion):** the near-field probe can
drive the AD936x past linear — compression fabricates harmonics/IMD (scan locks
FALSE peaks) and a clipped power reading saturates (walk test reads garbage).
Now: every grab is clip-checked (|I|/|Q| ≥ 0.9 FS on >0.2 % of samples), the
loop auto-steps RX gain down 8 dB at a time (`set_gain` moved to the SDR BASE
class so the B220 has it too; worker proxy already forwarded it), gain is
FROZEN mid-walk (a step would fake motion — walk verdict becomes `clipped`
instead), still-hot-at-0-dB → red "fit a 10–30 dB SMA pad" banner + voice, pad
logged via `/pad` into every capture record (`pad_db`), and the radio is handed
back at the configured gain on session exit. Simulator got the same gain model
(scales + hard-clips) so the path is drivable in demo.

**WIDE FIND (how to find ANY device's frequency — no guess list):**
`lab_live.WideFindSession` — two-pass stepped max-hold sweep over a span preset
(sub-GHz / 2.4 / 5 GHz / full 70–6000): pass 1 with the DUT POWERED OFF
(baseline), operator confirms power-on, pass 2, per-bin diff. New energy ≥8 dB
= the DUT, wherever it transmits; ambient cancels. `group_harmonics()` tags
integer families — near-field couples harmonics hard, so the FUNDAMENTAL is
the lowest of a family, not the loudest peak. Endpoints
`/wide_find/start|state|continue|stop|adopt`; triage-step UI card with
progress, power-on gate, results list, USE → adopts freq + goes live.
Sim caveat: the sim can't power its plant off, so both passes see it and the
diff is correctly empty — the positive path is unit-tested (FakeOnOffDevice →
finds 433.92 fundamental).

**Perf fix:** `iq_features` costs ~1 s per call — it was running per-grab and
capping the loop at <1 grab/s. Now throttled (only when the grab beats best
SNR, ≥2 s apart) → dense trace rate restored. Suite: **504 tests pass**.

## NOT done / open
- Client USB build does NOT include any of this yet (Matt verifies on real
  hardware first — see [[client-deploy-native-libs]] before any deploy).
- Directional (RXB) bearing at the next-room stop is a note field, not a
  guided sub-step — candidate next feature.
- Walk-test video is saved with the package but not yet shown in the report
  HTML export.
