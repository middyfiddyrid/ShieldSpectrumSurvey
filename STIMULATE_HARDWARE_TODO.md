# Active stimulation ("ping") — hardware binding TODO

**STATUS 2026-07-06:** the **simultaneous (B220) path is now WIRED end-to-end** but is
**HARDWARE-UNTESTED** — do not trust it in the field until the bench checklist below
passes. What exists:
- `survey/stimulate.py` — engine + decision logic + `make_chirp()` stimulus, unit-tested
  against a synthetic radio (`tests/test_stimulate.py`, incl. the concurrent TX-lifecycle).
- `survey/sweep/uhd_source.py` — `UHDRadio.start_tx/stop_tx` (ctypes UHD TX streamer on a
  background thread) + `B220Source.stim_*` (concurrent: TX-A on TX/RX port, RX-A on RX2).
- `POST /api/stimulate/probe` (demo-blocked) + the **ACTIVE PROBE** UI card (Results tab)
  with the band-authorization + honesty caveats up front.

**BLOCKER:** the physical B220 is currently stuck at FPGA load (`fx3 is in state 5`) — it
needs a physical USB unplug/replug before ANY B220 work (RX or TX) can be bench-verified
(see the main HANDOFF). Until the radio comes up, the TX path cannot be validated.

Remaining before field use = the bench checklist. The Pluto interleaved variant
(burst-then-listen on a single radio) is still unbuilt — only the B220 concurrent path is wired.

## Why it's gated
- **Physics:** the ADALM-Pluto is single TX/RX and cannot cleanly transmit and
  receive in the same band simultaneously (self-interference swamps RX). On the Pluto
  this must run **time-interleaved**: stimulate in a burst → stop TX → capture the
  response / ring-down. Genuinely concurrent stimulate-while-listen needs the
  **B210** (independent TX/RX chains — the same radio `nljd.py` already drives).
- **Legality:** transmitting is radiating. Stay inside operator-authorized ISM bands
  (902–928, 2400–2483.5, 5725–5875 MHz, low power); never cellular / public-safety /
  aviation. The engine already refuses out-of-band TX unless the operator overrides
  with a logged acknowledgment.
- **Honesty:** this only finds the *responsive* subset (receivers / oscillators / AGC
  / nonlinear junctions). A passive, shielded recorder can sit silent — a "no
  response" is **not** a clean bill. The engine's plain-language says so; keep it.

## The small radio protocol to implement (on `PlutoSource` and/or `B210Source`)
```
stim_mode() -> "concurrent" (B210) | "interleaved" (Pluto)
stim_permitted_bands() -> [(lo_mhz, hi_mhz), ...]   # from config, operator-declared
stim_capture_quiet(center_hz, fs, n) -> iq          # TX guaranteed OFF
stim_capture_stimulated(center_hz, lo_hz, hi_hz, fs, n) -> iq
    # B210: TX stimulus on chain B while RX captures on chain A
    # Pluto: TX a burst sweeping lo->hi, stop, then capture (ring-down / changed emission)
```
Reuse the existing `capture_iq()` for the RX side and the `nljd.py` TX-thread pattern
(`nljd_tx_cw`) for the transmit side. The stimulus can be a swept CW or band-limited
noise across `[lo,hi]`.

## Bench checklist (the B220 concurrent path is WIRED — validate it here)
Clear the FX3 blocker first (physical USB replug), then:
0. Antenna on the **TX/RX** port (stimulus out) AND on **RX2** (listen). Never TX into a
   port with no antenna/load. Keep `stimulate.tx_gain_db` LOW to start (default 50 is a
   guess — dial up carefully while watching a second SDR).
1. Confirm TX actually radiates: a spectrum-analyzer / second SDR sees the chirp sweeping
   `[lo,hi]` when `UHDRadio.start_tx` runs.
2. Put a known responsive device in the room (superregenerative RX / cheap RF remote
   receiver) → **ACTIVE PROBE** in the UI → expect `RESPONSE_DETECTED` with a responder
   at a sensible frequency.
3. Empty room → expect `NO_RESPONSE` (no false trigger from the stimulus floor rise; the
   synthetic tests cover the DSP, verify on real captures).
4. Band gate: probe a non-authorized band with override OFF → `BLOCKED_BAND`, and confirm
   with the second SDR that the transmitter **never keyed**.
5. Confirm `stim_tx_off()` always stops the DAC (no stuck carrier) after every probe,
   including on error paths.

## Config keys (survey/config.py defaults; override in config.yaml)
RX: `stimulate.rx_sample_rate` (4e6), `.fft_size` (4096), `.dwell_samples` (262144),
`.averages` (6), `.prominence_gate_db` (8), `.delta_gate_db` (6),
`.permitted_bands_mhz` (the three ISM bands).
TX: `stimulate.tx_sample_rate` (radio `_fs`), `.tx_gain_db` (50), `.tx_amp` (0.5),
`.tx_wave_samples` (65536), `.tx_antenna` ("TX/RX"), `.tx_settle_s` (0.05).

## Still unbuilt
- **Pluto interleaved variant** (burst-then-listen on the single-radio Pluto) — only the
  B220 concurrent path exists. Add `stim_mode()=="interleaved"` methods on `PlutoSource`.
