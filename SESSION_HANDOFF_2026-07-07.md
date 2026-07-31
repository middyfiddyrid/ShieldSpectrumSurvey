# Shield Spectrum Survey — session handoff (2026-07-07)

Complete summary of the working session: what was built, decisions, and the exact
commands to run it on the laptop. All code is on GitHub, branch **`b220-mini-source`**.

## 1. Pull it on the laptop
Everything is committed and pushed to `b220-mini-source` on
`github.com/middyfiddyrid/ShieldSpectrumSurvey` (clean tree). The B220 FPGA image is
in the repo (`native/uhd/usrp_b210_fpga.bin`), so the radio self-installs it at connect.

```bash
cd <laptop spectrum_survey folder>
git fetch origin
git checkout b220-mini-source
git pull origin b220-mini-source
```

## 2. Two things that DON'T travel over git
**a) `config.yaml` is machine-local (gitignored).** To run the split active-ping test
on the laptop, paste this top-level block into the laptop's `config.yaml`:

```yaml
stimulate:
  setup: split
  tx_source: pluto
  rx_source: b220
  rx_sample_rate: 4000000
  averages: 6
  prominence_gate_db: 8.0
  delta_gate_db: 6.0
  tx_amp: 0.4
  pluto_tx_hardwaregain_db: -30.0
  tx_settle_s: 0.1
```

**b) Host-side radio deps** (OS-wide, not in the repo): UHD runtime (`uhd.dll`) + Ettus
WinUSB drivers for the B220; pyadi-iio + libiio for the Pluto (libiio DLLs bundled in
`native/libiio`, UHD is separate); `pip install bleak` for the Bluetooth scan. If the
laptop has never had the B220 on it, the UHD host install is the thing to double-check.

## 3. What was built this session (covert-device detection)
Three device classes, three detection modes, plus supporting fixes. Test suite: 325 green.

- **Scoring explanations** — every anomaly score carries a plain-language breakdown (what
  raised it, which dB threshold it cleared). Shows in Results + Manual Triage tables, the
  client PDF, and the CSV.
- **Bluetooth** (`survey/ble.py`) — identifies every advertiser: MAC address type (public
  vs the 3 random kinds incl. rotating "resolvable-private"), vendor/OUI, decoded
  manufacturer + service data, and flags location **TRACKERS** (AirTag / Find My / Tile /
  SmartTag / Chipolo). New "Bluetooth Detail Sweep" card (trackers first). MAC always ID'd.
- **Active ping (stimulation)** — transmit a chirp, flag anything that REACTS. Three rigs,
  config-selectable: single B220 (TX/RX-A out, RX2-A in), single B220 cross-channel (listen
  on RX2-B for isolation), and **SPLIT** (Pluto transmits, B220 listens — best isolation).
  Honest gates: only finds responsive devices (a null is **not** a clean bill); refuses
  out-of-ISM-band TX without a logged override. ACTIVE PROBE card on the Results tab.
- **Passive emanation detector** (`survey/emanations.py`) — the piece the sweep and the ping
  can't cover: a silent SD-card recorder that never transmits still leaks its clock
  harmonics. Non-coherent averaging + median floor + baseline subtraction + a harmonic-comb
  test that groups evenly-spaced tones into one device and estimates its clock (24/48 MHz =>
  likely camera). PASSIVE EMANATION SCAN card. This is the passive core of the RFScan paper.

## 4. The B220 has FOUR SMA ports (not two)
TX/RX-A, RX2-A, TX/RX-B, RX2-B. Two transmit-capable, two receive-only. The dual sweep uses
the two RX2 ports; active stimulation transmits on a TX/RX port.

## 5. The FM "swept for but missing" issue — explained + fixed
The radio is fine: the B220 detected 15 FM stations across the whole dial (89.3–106.5 MHz,
some at −37 dBm). The alarming "22 strong stations MISSING" was the SOFTWARE grading
expectation by distance only and ignoring transmitter power — so 37–250 W translators got
flagged "strong — investigate."

**Fix committed:** expectation now parses the FCC ERP (power). On the live Switzer session
the 22 "missing" reclassify from *22 strong* to **3 strong / 2 medium / 17 weak** — 17 were
never real misses. The 3 genuine full-power misses now say "wide ambient sweep threshold —
re-run a NARROW band sweep to confirm."

**Why an FM miss ≠ can't catch a bug:** those FM stations are 8–14 MILES away; a planted bug
is feet away, and signal falls off with the square of distance — a covert transmitter is
usually STRONGER than a distant broadcast, not fainter. BLE detection uses the laptop's
Bluetooth radio to decode packets/MAC directly — it doesn't use the sweep threshold at all.
The tool's value is a systematic, documented survey + a baseline + four independent ways to
run down anomalies — not a magic catch-all (and it won't claim to be one).

## 6. Git / prod state (prod is UNTOUCHED)
- Desktop branch `b220-mini-source` = 36 commits ahead of prod (`main`), fully pushed.
- Nothing pushed to `main`/prod — pending the call on how to reconcile desktop vs laptop
  first (risk: a laptop-first prod push could revert the desktop's newer work).

## 7. Hardware / bench notes
- B220 receive side is LIVE-VERIFIED (2 modules, real IQ at 49.8 dB SNR). The old
  "fx3 state 5" was a LibreSDR-clone FPGA-image issue — solved, image bundled in the repo.
- The active-ping TX path is **HARDWARE-UNTESTED** end-to-end. Bench before field use:
  antennas on, start TX gain LOW, confirm it radiates with a 2nd SDR, and confirm a non-ISM
  band never keys the transmitter. See `STIMULATE_HARDWARE_TODO.md`.
- Plug the B220 and Pluto into separate direct USB 3.0 ports (not a shared hub). After a
  replug the B220's first connect takes ~20 s (firmware + FPGA reload).
- Split-rig test: connect B220 first, then Pluto (keep-other), confirm both connected, then
  Results → ACTIVE PROBE → watch 915, band 902–928 → PROBE.

## 8. Open next steps
- Bench the active-ping TX (esp. the split Pluto-TX / B220-RX rig).
- Optional: a one-click "FM band check" (targeted narrow sweep) to prove sensitivity on the
  3 strong stations the wide sweep missed.
- The call on getting this branch to prod/`main` and the laptop-vs-desktop reconciliation.
