# Shield TSCM — Field Spectrum Survey

Local Windows app for professional TSCM sweeps with an RF Explorer **6G Combo PLUS (Slim)**.
Enter the sweep address → the app builds a **database-verified baseline** of every RF emission
*expected* at that location → commands the analyzer over USB serial through a dual-module,
multi-room sweep → outputs the signals that **could NOT be verified** against any licensed or
known source, ranked by anomaly score, with an on-demand client-facing PDF report.

**Receive-only. No transmit functionality of any kind.**

## Quick start

```
pip install -r requirements.txt
python app.py            # opens http://127.0.0.1:8765 in the browser
```

Demo/dev without hardware: `python app.py --simulate` (everything is loudly marked
SIMULATED in the UI, database, and any report).

## Workflow (left rail, top to bottom)

1. **Session / Baseline** — enter address (geocoded via US Census, Nominatim fallback),
   connect the RF Explorer (auto-detects the CP210x COM port; close the vendor RF Explorer
   software first), build the baseline.
2. **Baseline table** — full expected-spectrum table with per-source counts and status;
   show this to the client before sweeping.
3. **Ambient reference** — one full sweep OUTSIDE the premises (TSCM standard practice);
   powers the interior-vs-exterior delta.
4/5. **Sweep LEFT / RIGHT** — Phase A/B with on-screen antenna prompts; the device is
   queried for each module's actual range and all planning derives from what the hardware
   reports.
6. **Rooms** — "Next Room" repeats Phases A+B per room; the room × frequency heat table
   localizes sources.
7. **Directional** — lock each anomaly, live signal-strength meter, log bearings/notes.
8. **Results / Report** — ranked anomaly list; **GENERATE REPORT** (always in the header)
   renders a PDF mirroring the current screen state — INTERIM watermark until interior
   phases complete. All reports + CSV exports archive under `reports/`; sessions live in
   `data/sweeps.db`.

**Calibration mode** (tools, run at the shop): tests Normal/Average/Max/MaxHold strategies
and span widths against a known source (433 MHz keyfob etc.), then writes the winning
config and the measured sweep rate back to `config.yaml`. The test source must be actively
transmitting during the run — a 0% detection run saves nothing and says so.

**Wireless inventory** (tools): scans WiFi APs (netsh) and Bluetooth LE advertisers
(laptop adapter via `bleak`) so the client can account for their own devices; loud
(≥ −55 dBm) unaccounted devices are highlighted and included in the report.

**Live sweep view**: realtime, running-average, and max-hold traces drawn together
(RF Explorer for Windows style) with a waterfall, and a live known/unknown signal ledger
under the chart that classifies each detection against the baseline as it lands.

**Signal wiki** (tools): the internal cross-session signal library. CAPTURE buttons on
the Results tab zoom the analyzer onto each signal (narrow span, high-iteration average
+ max-hold dwell) and save a vector fingerprint with provenance, characteristics, and a
description (rule-based; AI-enriched via the Claude API when `api_keys.anthropic` is set —
numeric profile only, no client data leaves the machine otherwise). Entries are
categorized (per-carrier cellular, broadcast, ISM/WiFi/BT, DECT, unknown…), editable,
recapturable on site, and matched across sessions — a new capture near a previously
documented signal raises a "seen before at…" alert. `capture_images/` holds the SVGs
(git-ignored, sensitive).

## Baseline sources

| Source | Needs | Notes |
|---|---|---|
| Static national allocation table | nothing | always available; ISM/DECT/cellular-uplink/GPS bands carry caution flags |
| FCC FM / AM / TV queries | internet | legacy pipe-delimited endpoints, parsed defensively, cached to disk |
| FCC ULS (land mobile, paging, microwave) | weekly extract | download `l_LMpriv.zip` (+ others as wanted) from fcc.gov/uls/transactions/daily-weekly, unzip, set `uls.extract_dir` in config.yaml; flagged STALE after `uls.stale_days` |
| OpenCellID → carrier band table | free API key | set `api_keys.opencellid`; towers map to carrier-owned blocks (uplink blocks stay caution-flagged) |
| RadioReference | paid premium acct (optional) | set `api_keys.radioreference_user/pass`; shows "not configured" otherwise |
| CellMapper (import) | manual capture | run `tools/cellmapper_capture.js` in the DevTools console of your own logged-in cellmapper.net tab, pan the map, download to `data/cellmapper_towers.json`. No scraping — captures only what the site already sends your browser. |
| Airports (gates aviation bands) | internet once | OurAirports dataset, cached |

Every external query caches to `cache/` — a repeat visit to the same address works with
degraded internet. With zero internet the baseline degrades to the static table only and
the UI/report say so.

## Configuration

Everything lives in `config.yaml`: API keys, detection threshold (+12 dB default), sweep
profiles, per-band MaxHold dwell weights, segment widths, branding/logo for the report,
simulator switch. Calibration mode writes `sweep.seconds_per_sweep` and `calibration:` back
into the file.

## Tests

```
python -m unittest discover -s tests
```

Covers the correlation engine (classification buckets, tolerances, interior/exterior delta,
room localization, anomaly scoring), the signal detector (noise floor, bin merging,
transient persistence), and the sweep planner (coverage, risk-weighted dwell, time budgets)
— all with synthetic data, no hardware needed.

## Hardware notes

- Target: RF Explorer 6G Combo PLUS (Slim), fw 3.45, dual SMA (LEFT = expansion module,
  RIGHT = mainboard). The app verifies model/firmware programmatically at connect and logs
  mismatches as warnings; module ranges (and the RFELIC05 wideband extension, if licensed)
  are read from the device, never assumed.
- Driver order: official `RFExplorer` python library → raw documented UART protocol
  fallback (handles PLUS units if the library lags) → simulator (explicit only).
- If only one module responds, the sweep runs single-module and the report states the
  coverage gap verbatim.
- Serial disconnects retry and resume at the failed segment; aborting a phase keeps all
  data collected so far.

## Data sensitivity

Sweep findings are sensitive. `data/sweeps.db`, `cache/`, and `reports/` stay on this
machine; the PDF in `reports/` is the only artifact intended for the client.

## Demod / Listen Layer (in design)

Current hardware (RF Explorer) + app is spectrum + fingerprint only. To identify
what a signal *carries* (voice bug vs data vs broadcast), we need a demod path.

See:
- DEMOD_LISTEN_DESIGN.md — full stack recs, block schematic, software plan, tablet field layout.
- FIELD_KIT_BOM.md — exact buy list (cheapest RTL-SDR V4 path + portable compute).

**Immediate (no code change):** Buy RTL-SDR Blog V4 (~$40). Use SDR# side-by-side with the app.
Tune directly from wiki/result frequencies (copy-paste). Record audio clips manually for now.

Integration (planned): "Tune & Listen" buttons on Results and Wiki views, in-app audio recording linked to captures, auto mode suggestion from existing BW/duty features.
