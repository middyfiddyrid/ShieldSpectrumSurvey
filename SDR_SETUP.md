# SDR Source Setup — ADALM-Pluto (and the July B220)

The sweep engine can run from the **RF Explorer** (default, unchanged) or an
**ADALM-Pluto** SDR. The rest of the app is identical either way — the Pluto is
wrapped as a `BaseDevice` (`survey/sweep/sdr_source.py`) that synthesises the same
`SweepFrame(freqs_mhz, amps_dbm)` the RF Explorer produces, by stepping the LO,
capturing IQ, computing a Welch PSD, and stitching the segments together.

## Selecting the source

```yaml
# config.yaml
device:
  source: pluto        # "rfexplorer" (default) | "pluto"
pluto:
  uri: "ip:192.168.2.1"   # USB-Ethernet gadget; or "usb:" for the USB IIO backend
```

Or per-run, without editing config:

```
set SWEEP_SOURCE=pluto
set PLUTO_URI=ip:192.168.2.1
python app.py
```

`SWEEP_SOURCE` overrides `device.source`; `PLUTO_URI` overrides `pluto.uri`.

## Windows install (one time)

The Python wrapper is on PyPI; the **native libiio runtime** and the **Pluto USB
drivers** are separate and are what actually talk to the radio.

1. **Python wrapper** (in `requirements.txt`):
   `python -m pip install pyadi-iio` (pulls `pylibiio` + `numpy`).
2. **Native libiio runtime** — `import iio` fails with
   `LoadLibrary() argument 1 must be str, not None` until `libiio.dll` is on PATH.
   Download libiio **v0.25** (must match `pylibiio`) Windows zip from
   <https://github.com/analogdevicesinc/libiio/releases>, and put `libiio.dll`
   plus its deps (`libusb-1.0.dll`, `libxml2.dll`, `libserialport-0.dll`,
   `msvcp140.dll`, `vcruntime140.dll`) and `iio_info.exe` on PATH. On this machine
   they live in `%LOCALAPPDATA%\libiio\bin` (added to the user PATH).
3. **Pluto USB drivers** — install `PlutoSDR-M2k-USB-Drivers.exe` from
   <https://github.com/analogdevicesinc/plutosdr-m2k-drivers-win/releases>
   (signed by Analog Devices). Without it, Device Manager shows the **RNDIS** and
   **IIO** functions in an *Error* state and nothing can reach the radio. After
   installing, unplug/replug the Pluto.

Verify:

```
iio_info -u ip:192.168.2.1        # or: iio_info -S   to scan
python -c "import adi; print(adi.Pluto('ip:192.168.2.1'))"
```

## Frequency coverage and the AD9364 extension

The Pluto Rev.C here is an **AD9363A**. Stock, it tunes only **~325 MHz – 3.8 GHz**
— our probe confirmed 70 MHz, 5.0/5.8/6.0 GHz all return *Invalid argument*. TSCM
wants ~70 MHz – 6 GHz (FM, 2.4 GHz Wi-Fi/BLE, **5.8 GHz Wi-Fi**, cellular).

The standard unlock makes the chip report as an **AD9364** (70 MHz – 6 GHz):

```
ssh root@192.168.2.1            # default password: analog
fw_printenv attr_name attr_val  # check current
fw_setenv attr_name compatible
fw_setenv attr_val ad9364
reboot
```

Reversible — set `attr_val` back to `ad9363`. Above 3.8 GHz / below 325 MHz the
silicon runs **out of its calibrated spec** (reduced sensitivity, uncalibrated
gain) — acceptable for relative/baseline-delta TSCM work, not for absolute power.

After extending, set the new limits so the app uses them:

```yaml
pluto:
  freq_min_hz: 70000000
  freq_max_hz: 6000000000
  require_extended: true   # HARD-FAIL at connect if the radio can't actually reach them
```

### 5.8 GHz validation at startup

`PlutoSource.connect()` probes the real tunable range and records it as the
module range. If `pluto.require_extended: true` and the radio cannot reach
`freq_min_hz`/`freq_max_hz`, connect raises a clear `DeviceError` (e.g. *"radio
only tunes 325–3800 MHz, need 70–6000 MHz; apply the AD9364 frequency
extension"*) instead of silently returning garbage for the 5.8 GHz band.

## Calibration — read this before trusting any dBm number

The Pluto is **not an absolute-calibrated instrument.** The PSD is computed in
relative dBFS and shifted by a single constant, `pluto.cal_offset_db` (default
`-30`), to *approximate* dBm. This offset is a rough per-unit constant, not a
per-frequency calibration, and accuracy degrades toward the band edges and
(if extended) outside 325 MHz–3.8 GHz.

**Lean on baseline-delta detection** (anomalous emitter vs. a captured room
baseline), exactly as the correlation engine already does — not on absolute dBm.
Tune `cal_offset_db` per unit against a known reference if you need the displayed
numbers to be in the right ballpark.

## How the wideband sweep is built (and its knobs)

Per `set_range(lo, hi)` the driver plans overlapping tune steps; each
`read_sweep()` does, for every step: tune → settle → discard the stale buffer →
capture IQ → Welch PSD → keep the central usable fraction → **discard a guard
band around DC** → stitch onto a stable frequency grid.

| `pluto.` key | default | meaning |
|---|---|---|
| `sample_rate` | 20 MHz | instantaneous bandwidth per tune. **Keep ≲ 20 MSPS** — USB 2.0 won't sustain more and oversized buffers underflow. |
| `keep_fraction` | 0.8 | central fraction of each capture kept (drops analog-filter edge rolloff). |
| `overlap` | 0.5 | inter-segment overlap. **Must be ≥ 0.5** so every tune-center is refilled by a neighbour (else an emitter on a center is lost to the DC guard). Higher overlap = cleaner centers, slower sweep. |
| `dc_guard_hz` | 250 kHz | central guard **discarded** at each tune center to remove the LO-leakage spike. This is the DC mitigation (chosen over offset-tuning, which can't fit with 80% keep). A TSCM sweep must never flag its own LO. |
| `fft_size` | 1024 | Welch segment length. |
| `capture_samples` | 65536 | IQ samples per tune. |
| `sweep_bins` | 2048 | resampled grid points per frame (caps the host-side detector cost). |
| `gain_mode` / `gain_db` | manual / 64 | **manual at a fixed level for repeatable baselines**; use `slow_attack` AGC only for first-look survey. |
| `settle_seconds` | 0.05 | post-retune settle before discarding the stale buffer. |

Retune latency (tens of ms/step) dominates wide sweeps; the per-frame stepped-
sweep duration is logged and exposed as `DeviceInfo.last_sweep_seconds`.

## Dual-antenna note (and the B220)

The Pluto is **single-RX (1R1T)**. The directional-antenna follow-up and any
dual-antenna workflow are a **sequential pass or a physical SMA swap, not
simultaneous.** The interface already exposes `channels` (1 for Pluto) so the
**B220 Mini (AD9361, 2R2T)** can subclass `AD936xSource`, set `channels = 2`, and
enable true simultaneous dual-channel DF with no contract change.

## Validating the hardware path

`python tests/pluto_live_check.py` (needs a real Pluto) runs a live stitched
sweep over the 2.4 GHz ISM band and checks: contiguous trace, no center-bin
spikes (worst-center excess ≤ 6 dB over local floor), seam continuity, and a
`capture_iq()` round-trip. The pure DSP (Welch / notch / plan / stitch / stepped
sweep) is covered hardware-free by `tests/test_sdr_source.py`.
