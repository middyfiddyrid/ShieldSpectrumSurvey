# Shield TSCM Spectrum Survey — Session Handoff

Continuation brief for a fresh Fable/Claude Code session. Read this first, then `git log --oneline -20` and skim `README.md`.

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

## OPEN WORK (priority order) — this is where to start

**P1 — No report has ever rendered for the operator (pre-sweep OR post-sweep). THE BLOCKER.**
The endpoints work server-side (verified: `/api/report/generate`, `/api/report/presweep`, `/api/report/internal_memo` all return 200 and write PDFs to `reports/`). But the operator never sees one. Root cause is client-side: `window.open("/reports/<file>")` is almost certainly being blocked by the popup blocker, and/or stale JS after server restarts. **Fix: stop relying on `window.open`.** After any report generates, render a visible, persistent clickable link (and/or auto-navigate a download) in the page, e.g. in the Reports list and a success banner. Make this the first thing you do. Then walk the operator through generating one. (Test scripts that prove the backend works: `tests/expected_check.py`, ad-hoc in `reports/`.)

**P2 — Per-POI document prompt missing.** When the operator uploads a person's background or adds a POI, there is no nudge to gather/upload additional docs for that POI. Add: when a POI is created, auto-add a technician task ("gather client-authorized background for <name>") and surface an "attach documents" affordance per POI (link case_files to a poi_id). Keep the client-authorized / no-dragnet framing.

**P3 — Pre-sweep shielding rec from tower proximity.** The RF environment & shielding section currently (a) keys off broadcast transmitters only and (b) is only in the post-sweep report. Operator wants it in the **pre-sweep** briefing and based on **proximity to cell towers** (OpenCellID / CellMapper import give tower counts + nearest distance) as well as broadcasters. Extend `rf_density()` to fold in cellular tower proximity, and add the optional RF-environment/shielding section to the pre-sweep report (as a report option there too).

**P4 — POI opportunity vectors.** Add a field/section for **social or institutional opportunity vectors** — areas/relationships that warrant observation/de-confliction (e.g. shared workplace, shared custody, common associates). Internal-only; surface in the internal memo. Keep evidence-based, hypothesis-framed.

**P5 — Anonymized case-study / device library (referenceable in reports).** Operator wants a library of example unauthorized-device finds — device photo + its signal capture + an **anonymized** case history — that can be cited in reports (educational/credibility). Build a `case_studies` table + UI + optional report section. **Must be genuinely de-identified composites**, never traceable to a real client/subject. Seed with the two operator-provided composites:
  - A minor attempting to monitor a parent after being disciplined (consumer audio/location device).
  - A partner of a household dependent who deployed multiple surreptitious devices in the dependent's vehicle — GPS tracker, audio transmitter — plus phone mirroring. (Describe device classes + RF signatures generically; no names, no identifying detail.)

## How to verify your work

- `python -m unittest discover -s tests` (keep 38+ green).
- Use the preview tooling (`preview_start` with launch config `spectrum-survey-live` for hardware, or add a `--simulate` config) to drive the UI; `preview_screenshot` to confirm.
- For PDF content checks, generate via the API and read with `pypdf` (see `tests/expected_check.py` pattern).
- Real hardware is on COM3; `python app.py` then connect. For UI-only work use `--simulate`.
- The operator's real sessions are in `data/sweeps.db` (gitignored). Session 16 (Kenwood) has baseline + ambient/aborted data — no completed interior sweep, so its reports are INTERIM. A real FINAL report needs the LEFT+RIGHT interior phases actually run.

## Key files

`app.py` · `survey/server.py` (routes) · `survey/report.py` (all PDFs) · `survey/sweep/{device,runner,planner,detector,calibration,simulator}.py` · `survey/sigcapture.py` (wiki) · `survey/expected.py` · `survey/correlation.py` · `survey/baseline/*` · `survey/web/{index.html,app.js}` · `config.yaml` · `tests/*`

Standing notes: never delete files (move/rename). All client/business language in the PDFs (engagement terms, exposure wording, confidentiality block) is draft and needs the operator's/counsel's review before regular use.
