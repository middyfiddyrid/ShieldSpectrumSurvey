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

## OPEN WORK — P1–P5 ALL COMPLETED 2026-06-11 (commits 27514bf…ae70350)

**P1 ✅ Reports render as visible links.** All three generate buttons (post-sweep header, pre-sweep on Session tab, internal memo on POI tab) now show a persistent `OPEN PDF / DOWNLOAD` link next to the button — `window.open` removed everywhere. Bonus root-cause fix: on a plain boot (no `#hash`) `show()` never ran, so the engagement form stayed blank and the pre-sweep button would have saved blanks over the real engagement; boot now always routes through `show()`.

**P2 ✅ Per-POI documents.** Creating a POI auto-adds a technician task to gather that person's client-authorized background; POI panel has an ATTACHED DOCUMENTS block (case_files gained `poi_id`); session Case Files table shows the linked POI.

**P3 ✅ Tower-proximity shielding in pre-sweep.** `rf_density()` folds in cellular (per-carrier tower counts deduped across OpenCellID/CellMapper, nearest-tower distance; recommendation triggers on broadcast density OR tower proximity). Section renders in the pre-sweep behind the same `rf_environment` option, now also toggleable from the Session tab. **Note: until the OpenCellID key or a CellMapper import exists, the section honestly reports tower data unavailable.** 6 unit tests cover the logic.

**P4 ✅ POI opportunity vectors.** New internal-only field (shared workplace, custody exchanges, common associates…), rendered in the internal memo as hypothesis-framed "where to look" guidance. Absent from all client documents.

**P5 ✅ Case-study / device library.** CASE STUDIES tab; entries render in client reports ONLY when the operator attests they are de-identified composites. Both operator composites seeded (household-member consumer device; vehicle GPS/audio kit + phone mirroring). Optional `case_studies` report section in pre- and post-sweep; left OFF for session 16 — per-engagement choice.

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

`app.py` · `survey/server.py` (routes) · `survey/report.py` (all PDFs) · `survey/sweep/{device,runner,planner,detector,calibration,simulator}.py` · `survey/sigcapture.py` (wiki) · `survey/expected.py` · `survey/correlation.py` · `survey/baseline/*` · `survey/web/{index.html,app.js}` · `config.yaml` · `tests/*`

Standing notes: never delete files (move/rename). All client/business language in the PDFs (engagement terms, exposure wording, confidentiality block) is draft and needs the operator's/counsel's review before regular use.
