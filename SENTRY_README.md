# Sentry RF Intelligence

This is a separate, live-hardware B220 field application. It does not share its
database, logs, recordings, reports, case files, port, or Git history with the
existing ShieldTSCM installation.

## Start

1. Close any other application using the B220.
2. Connect the B220 by USB 3.
3. Double-click `START Sentry RF Intelligence.bat`.
4. The console opens `http://127.0.0.1:8876`.

The first B220 connection may take 20–30 seconds while UHD prepares the radio.

## Direct spectrum probe

- Move across the live spectrum or waterfall to inspect the cursor frequency,
  estimated signal center, occupied width, peak power, and SNR.
- Click to pin a frequency. The passive watch accumulates frame count, duration,
  average/max power, and center drift while local identification runs.
- Click elsewhere on the plot or press Release to return to hover mode.
- Open Receiver / Analyze is a separate deliberate operator action.

## Catalog intelligence

Local Signal Identification Wiki reference data and the surveillance-device
catalog always run first. Optional read-only online corroboration is configured
under `catalog_enrichment` in `config.yaml`; failures fall back to local results.
Signal Identification Wiki is enabled by default and cached for seven days.
Wikipedia is present as an optional source but disabled by default.

## Isolation and safety

- Runtime state begins empty under this app's own `data/` directory.
- Existing B220 and calibration settings were copied; API credentials were not.
- The app binds only to `127.0.0.1` on port `8876`.
- Active RF stimulation is disabled in this B220 receive-only build.
- Passive pinning does not run payload decoders or demodulated audio.
