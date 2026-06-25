# Shield TSCM Spectrum Survey — Session Handoff

Continuation brief for a fresh Fable/Claude Code session. Read this first, then `git log --oneline -20` and skim `README.md`.

---

# ⚡ LATEST SESSION HANDOFF — 2026-06-25 — main merge + POCSAG decoder (READ THIS FIRST)

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
