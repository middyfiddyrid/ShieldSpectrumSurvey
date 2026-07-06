# Active stimulation ("ping") — hardware binding TODO

**STATUS 2026-07-06:** the TX path is **WIRED end-to-end** but **HARDWARE-UNTESTED** — do
not trust it in the field until the bench checklist below passes. The B220 receive side is
now live-bench verified (the `fx3 state 5` blocker was a LibreSDR-clone FPGA-image issue,
solved — see the main HANDOFF), so the TX bench can proceed.

### The B220 has FOUR SMA ports (not two)
Two are transmit-capable, two are receive-only:

| Port | Channel | Role |
|------|---------|------|
| **TX/RX A** | A | transmit **or** receive |
| **RX2 A**   | A | receive only |
| **TX/RX B** | B | transmit **or** receive |
| **RX2 B**   | B | receive only |

The receive-only dual sweep uses the two **RX2** ports (RX2-A = "right" module, RX2-B =
"left"). Active stimulation uses a **TX/RX** port to transmit.

### Two rigs (config `stimulate.setup`)
1. **`single`** (default) — one B220: transmit on **TX/RX-A**, listen on **RX2-A**
   (AD9361 full-duplex, same board). Simplest, but the transmitter leaks into its own
   receiver. Set `stimulate.rx_channel = 1` to listen on **RX2-B** (the other channel)
   instead — more isolation for the cost of using the second frontend.
2. **`split`** — transmit on ONE radio, listen on ANOTHER (`stimulate.tx_source` /
   `stimulate.rx_source`, e.g. **Pluto TX + B220 RX**). Two physically separate boxes =
   the best TX/RX isolation. Both radios must be connected (DEVICE card, keep-other).

### What exists
- `survey/stimulate.py` — engine + `make_chirp()` + `SplitStimRadio`, unit-tested against
  synthetic radios (`tests/test_stimulate.py`: concurrent lifecycle + split rig).
- `survey/sweep/uhd_source.py` — `UHDRadio.start_tx/stop_tx` (ctypes UHD TX streamer) +
  `B220Source.start_tx/stop_tx` + `stim_*` (configurable TX/RX channel).
- `survey/sweep/sdr_source.py` — `PlutoSource.start_tx/stop_tx` (pyadi cyclic buffer) so the
  Pluto can be the split transmitter.
- `POST /api/stimulate/probe` (demo-blocked, single + split) + the **ACTIVE PROBE** UI card.

The Pluto **interleaved** single-radio variant (burst-then-listen) is still unbuilt.

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
Rig: `stimulate.setup` ("single" | "split"), `.tx_source` ("pluto"), `.rx_source` ("b220"),
`.rx_channel` (0 = RX2-A; 1 = RX2-B), `.tx_channel` (0), `.tx_antenna` ("TX/RX").
RX: `stimulate.rx_sample_rate` (4e6), `.fft_size` (4096), `.dwell_samples` (262144),
`.averages` (6), `.prominence_gate_db` (8), `.delta_gate_db` (6),
`.permitted_bands_mhz` (the three ISM bands).
TX: `stimulate.tx_sample_rate` (radio `_fs`), `.tx_gain_db` (50, B220 UHD scale),
`.pluto_tx_hardwaregain_db` (-20, Pluto 0..-89 dB), `.tx_amp` (0.5),
`.tx_wave_samples` (65536), `.tx_settle_s` (0.05).

## Still unbuilt
- **Pluto interleaved variant** (burst-then-listen on the single-radio Pluto) — only the
  B220 concurrent path exists. Add `stim_mode()=="interleaved"` methods on `PlutoSource`.
