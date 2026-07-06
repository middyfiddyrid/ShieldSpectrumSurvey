# Active stimulation ("ping") — hardware binding TODO

`survey/stimulate.py` ships the **measurement engine + decision logic only**, unit-
tested against a synthetic radio (`tests/test_stimulate.py`). The real transmit path
is intentionally **not wired** — we do not expose a "transmit" control until the TX
path and the legal band-authorization UX are built and **bench-validated**. This
file is the checklist to get there.

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

## Bench validation before exposing any UI
1. Confirm TX actually radiates (spectrum-analyzer or a second SDR sees the stimulus).
2. Put a known responsive device in the room (e.g. a superregenerative RX / a cheap
   RF remote receiver) and confirm `probe()` returns `RESPONSE_DETECTED` with a
   responder at a sensible frequency.
3. Confirm an empty room returns `NO_RESPONSE` (no false triggers from the stimulus
   floor rise — the synthetic tests cover the DSP, but verify on real captures).
4. Verify the band gate: probing a non-authorized band returns `BLOCKED_BAND` and
   does **not** key the transmitter.
5. Only then wire `POST /api/stimulate/probe` (demo-blocked — it transmits) + a UI
   control that shows the band-authorization + honesty caveats up front.

## Config keys (survey/config.py defaults; override in config.yaml)
`stimulate.rx_sample_rate` (4e6), `.fft_size` (4096), `.dwell_samples` (262144),
`.averages` (6), `.prominence_gate_db` (8), `.delta_gate_db` (6),
`.permitted_bands_mhz` (defaults to the three ISM bands).
