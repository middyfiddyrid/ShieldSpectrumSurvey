# DEMOD → ANALYZE → DISSEMINATE — Sign-Gated Per-Signal Classification & Action Layer

**Companion to `DEMOD_LISTEN_DESIGN.md`.** That document added the *listen* path (tune → demod → hear it). This one adds the next link: after the operator **signs an on-screen attribution/authorization**, clicking **Analyze** over the live analyzer window (1) strengthens the demod front-end so weak signals are actually usable, (2) auto-classifies every signal in the current capture window and auto-sets each demod width to its occupied bandwidth, (3) offers a per-class **disseminate** action (video → confirm/locate, BLE/Wi-Fi → identify at broadcast-metadata level, pager/OOK → decode text, encrypted/wideband → identify+locate+evidence), and (4) records everything as hashed, authorization-linked, chain-of-custody evidence surfaced in the report.

Everything here is **RECEIVE-ONLY**. No transmit path is added or implied anywhere. The **content-interception boundary** that `decode_router.interpret()` already draws is preserved and made load-bearing throughout: we classify, identify, locate, decode *broadcast/one-way* protocols, and capture evidence — we never decode the content of a third party's two-way communication.

> **Honesty note carried from the hardware reality:** the ADALM-Pluto is a single-RX (1R1T) radio tuned **once** per listen/analyze session over its instantaneous bandwidth. It is **NOT absolute-calibrated** (`cal_offset_db = -30` is a baseline-delta display constant, not dBm). Several claims in earlier drafts were corrected during review and are flagged inline as **[corrected]**.

---

## 1. Overview & the operator story

```
        ┌──────────────────────────────────────────────────────────────────┐
        │  1. SIGN        Operator affirms 4 attestations + signs.          │
        │                 Immutable session_authorizations row written.     │
        │                 Passive spectrum/waterfall view works pre-sign;   │
        │                 receive / analyze / decode / record are GATED.    │
        └───────────────┬──────────────────────────────────────────────────┘
                        │  (server returns 403 on analyze/listen/record until signed)
                        ▼
        ┌──────────────────────────────────────────────────────────────────┐
        │  2. TUNE + LISTEN   inspectByFreq → live IQ stream (one LO).       │
        │                     Strengthened demod: RX gain staging, SNR       │
        │                     squelch, RMS-target AGC, FM de-emphasis,       │
        │                     anti-alias decimation, "why is it weak?"       │
        │                     front-end diagnostic on the S-meter.          │
        └───────────────┬──────────────────────────────────────────────────┘
                        │  operator clicks ▦ CLASSIFY WINDOW
                        ▼
        ┌──────────────────────────────────────────────────────────────────┐
        │  3. ANALYZE WINDOW  Detect every peak in the CURRENT capture       │
        │                     window (NOT a swept scan). Sequentially        │
        │                     capture_iq per peak → iq_features →            │
        │                     classify_modulation → identify → route_decode  │
        │                     → interpret. Stream per-signal cards (NDJSON).  │
        │                     Each card carries occupied_bw → auto-width.     │
        └───────────────┬──────────────────────────────────────────────────┘
                        │  per-card primary action from interpret().readable
                        ▼
        ┌──────────────────────────────────────────────────────────────────┐
        │  4. DISSEMINATE   analog_video → confirm signature + locate        │
        │                   analog_voice → listen + record audio evidence    │
        │                   ook/fsk pager → decode text (ECPA-gated)         │
        │                   ble/wifi (band_hint) → identify at broadcast-    │
        │                       metadata level + sniffer hand-off            │
        │                   encrypted/ofdm/unknown → identify + locate +     │
        │                       capture IQ evidence (NEVER content)          │
        │                                                                    │
        │   Evidence: sha256 + "captured under client authorization" tag +   │
        │             FK to the signing authorization (+ any ECPA override). │
        └──────────────────────────────────────────────────────────────────┘
```

The four steps map to four subsystem areas below (§4a–§4d). Each is independently shippable; the phased plan in §8 sequences them.

---

## 2. Goals / Non-goals

### Goals
- **Make "listen" actually strong.** Replace per-block peak-normalize (which amplifies pure-noise blocks to full scale) with SNR-keyed squelch + RMS-target AGC; add FM de-emphasis; remove residual boxcar aliasing; add live RX gain staging and a plain-English "is this a front-end/antenna problem or a DSP problem" diagnostic.
- **One-click classify the whole window.** Detect every peak inside the current capture window and run the existing single-signal fusion (`iq_features → classify_modulation → identify → route_decode → interpret`) per peak, streamed as progressive cards.
- **Auto-width.** Wire each classified signal's measured `occupied_bw_khz` into the already-existing `setDemodBw → /api/listen/bandwidth → controller.listen_chan_bw_hz → demodulate(chan_bw_hz)` path.
- **Per-class dissemination.** Turn `interpret().readable` into a concrete, honesty-gated action per class.
- **Sign gate + evidence chain.** A net-new per-session authorization signature that gates receive/analyze/decode/record; every recording hashed and linked to that signature; a court-/client-facing Evidence & Chain-of-Custody report appendix.

### Non-goals
- **No transmit. Ever.** (Receive-only is an architectural invariant, not a config.)
- **No third-party two-way content decode.** Encrypted/OFDM/unknown yields identify+locate+evidence, never content.
- **No device/person following or deanonymization.** "Locate" is RSSI-to-room on the client's premises, not tracking a person's device across space/time.
- **No BLE/BT hop-following or payload decode on the Pluto.** Physically infeasible on one RX; the design *recommends* an Ubertooth One / nRF52840-Sniffle add-on, never pretends to do it.
- **No absolute-dBm claims.** All power/RSSI is baseline-delta; evidence artifacts must say so.
- **No broadcast-quality video.** Analog-video handling is signature-confirm + locate + audio subcarrier, **not** a watchable frame feed **[corrected — see §4c and §5]**.

---

## 3. Current state (what exists) & why the demod feels weak

### What exists and works
- **Live analyzer + listen** (`survey/web/app.js`, `survey/server.py`): `inspectByFreq()` tunes once, streams IQ, and drives a single-capture Welch PSD analyzer trace, an STFT waterfall, an S-meter, click-to-tune (digital offset, no LO re-tune), and continuous Web-Audio playback (`/api/listen_pcm`). The trace is a **single-tune** capture over the instantaneous bandwidth — **not** a stitched swept span.
- **Per-signal fusion** (`POST /api/analyze/signal`, `server.py:714`): one `capture_iq` → `iq_features` → `identify` → `welch_psd` analyzer → `spectrogram` → `route_decode`/`interpret` → `novelty`, returned as one JSON. This is the single existing seam where classification + decode already compose.
- **Feature/ID stack** (`survey/identify.py`): `iq_features()` (occupied_bw, SNR, duty/continuity, FSK states/shift, symbol rate, `modulation_class`), `classify_modulation()`, `identify()` scoring against `sigidwiki_ref.json` + `surveillance_catalog.json` (with `device_class`/`confirm_steps`/`false_positive_overlap`).
- **Multi-signal enumerator** (`survey/sweep/detector.py` — **[corrected path]**, not `survey/detector.py`): `detect_signals()` enumerates peaks over **accumulated sweep** bins. Its merge step is an **inline loop** inside `detect_signals` (lines 52–61); there is **no** standalone `_merge_runs` helper today.
- **Decode router** (`survey/decode_router.py`): `route_decode()` (POCSAG/FLEX/OOK/DTMF/CW, each self-gated for honesty) + `interpret()` → verdict with `readable ∈ {decoded, listen, maybe_decode, encrypted, identify_only, none, unknown}`.
- **Attribution + ECPA override** (`survey/db.py`, `server.py`): per-signal `signals.attribution_status` enum; immutable `decode_overrides` log (`ecpa_ack` + `reason`, else 400). Regression harness in `tests/test_attribution.py`.
- **Recording** (`survey/sweep/runner.py`): `start_recording()` writes WAV (audio) or raw complex64 `.cf32` (IQ evidence); never-delete/orphan-recovery filenames; `recordings` DB rows.
- **Reports** (`survey/report.py`): `generate_report()` and siblings, `DEFAULT_REPORT_OPTIONS` toggle registry.

### Why the demod feels weak (all grounded in `survey/demod.py`)
1. **Per-block peak-normalize** (`demod.py:166–169`) rescales *every* 0.15 s block independently, so an empty channel is amplified to full-scale hash. There is **no squelch**.
2. **No output AGC** — level jumps block-to-block; there is no target-loudness control.
3. **No FM de-emphasis** — WFM/NFM sound bright/hissy.
4. **Residual boxcar `_decimate`** (`demod.py:51–59`, ~−13 dB sidelobes) is the last aliasing source on the audio path; the good windowed-sinc `_fir_decimate` already exists in the same file but is not used at that call site.
5. **RX gain set once at `connect()`** — no live control, no operator-facing "is this a front-end problem" readout.
6. **`demodulate()` is stateless per call** — any IIR (de-emphasis, AGC release) or hysteresis (squelch) must carry per-stream state or it clicks at every 0.15 s block seam. `_audio_blocks` already carries `psd_ema`/`nf_ema`, so the seam for new state exists.

### The two structural gaps for the goal
- **No window-wide classifier.** `detect_signals` (multi, amplitude-only, no modulation) and `iq_features`/`identify` (single, modulation) are **never composed over a span**. `/api/analyze/signal` is strictly single-emitter.
- **Auto-width is unwired.** `occupied_bw_khz` is computed and *displayed* but never fed to `setDemodBw`/`listen_chan_bw_hz`.
- **No sign gate.** The goal's "operator SIGNS" step has **no table, endpoint, or gate today** — it is entirely net-new. (Per-signal `attribution_status` and the per-decode ECPA override exist, but neither is a session-level pre-analyze gate.)

---

## 4. Design

### 4a. Demod front-end strengthening

All edits are receive-only DSP/RX-front-end. They touch the **live stream path only**; the one-shot `/api/inspect` and `/api/analyze/signal` audio keep today's behavior byte-for-byte (via `state=None`).

#### The per-stream state object (the seam that fixes statelessness)
`demodulate()` gains an optional `state` argument. `state=None` preserves the old stateless per-block peak-normalize (one-shot callers unchanged). `state != None` enables continuity for the live stream.

```python
# survey/demod.py — new dataclass after _fir_decimate (~line 95)
class DemodState:
    def __init__(self):
        self.agc_gain   = 1.0     # carried across blocks (no level jump)
        self.deemph_z   = 0.0     # de-emphasis IIR memory (no click at seam)
        self.sq_open    = False
        self.sq_hang    = 0
        self.last_snr_db = None
        self.last_out_rms = None
        self.gated      = False
```

Extended signature:
```python
def demodulate(iq, fs, mode='nfm', offset_hz=0.0, chan_bw_hz=None,
               state=None, squelch_snr_db=None, deemphasis_us=None) -> (int16 PCM, rate)
```

#### The five changes
1. **Live RX gain staging.** New public `set_gain(mode, db)` **on `PlutoSource`** (next to the crash-safe `_set_gain` body at `sdr_source.py:539`), plus re-assert gain at `stream_iq()` start. **[corrected]** — the safe back-off/fallback body lives on `PlutoSource`, not a fictional `BaseDevice`; the base `AD936xSource._set_gain` (line 255) only raises `NotImplementedError`. `set_gain` inherits crash-safety only on `PlutoSource` (the only class instantiated for live listen).
2. **SNR-keyed squelch/noise-gate.** Below threshold, fade to silence over the block (cosine ramp), freeze AGC, set `gated=True`; hysteresis via `squelch_hang_blocks`. **Default OFF** (never risk muting a weak bug). Audio-only — **never** gates the IQ/evidence recording path.
3. **RMS-target AGC.** Replaces per-block peak-normalize on the live path: separate fast attack (~8 ms) / slow release (~300 ms), `agc_max_gain` ceiling, brickwall headroom safety.
4. **FM de-emphasis (50/75 µs).** One-pole IIR carrying `deemph_z` across blocks. **[corrected placement]** — apply **AFTER** the `dec2` decimation, at the final ~24 kHz audio rate (3.6 k samples/block), **not** before it at the 300 kHz discriminator rate (45 k samples/block for WFM). Vectorized single-pole recurrence (`lfilter`-equivalent), never a per-sample Python loop.
5. **Anti-alias decimation.** Swap the final boxcar `_decimate` for the existing windowed-sinc `_fir_decimate` — **but only when `dec2 > 1`** **[corrected]** (for NFM at 2.4 MHz fs, `dec2 == 1`, so the boxcar is already a pass-through; skip the FIR to avoid a wasted 401-tap convolve). Real improvement applies to WFM and other high-arate paths.

#### The "why is it weak?" diagnostic
`_audio_blocks` publishes a `demod` object into the existing `listen_spectrum` frame:
```json
"demod": { "snr_db": 4.2, "gated": false, "agc_gain_db": 31.0,
           "rx_gain_mode": "manual", "rx_gain_db": 64.0, "squelch_db": null }
```
The client renders a one-line verdict: *"Weak — raise RX gain or use AGC (likely front-end/antenna)"* vs *"Weak at max RX gain — antenna/coupling/range (front-end limit, not DSP)"* vs *"DSP boosting +NN dB — signal genuinely weak"* vs *"OK — SNR NN dB · RX manual"*.

#### The two SNR-source honesty tradeoffs **[corrected — pick one, don't claim both]**
- The analyzer Welch is a 2048-pt FFT over the full 2.4 MHz span → ~1172 Hz/bin, so an NFM 16 kHz passband is only ~14 bins and a 3 kHz SSB passband ~2–3 bins. Peak-minus-median over so few bins is a **high-variance** SNR estimate exactly in the low-SNR regime the squelch cares about.
- **Recommendation:** compute a **cheap dedicated in-band SNR** (short FFT or time-domain power ratio *after the channel filter*) for the gate, on the pre-yield path; keep the full analyzer Welch/STFT **after** the `yield` (off the audio critical path, as today at `server.py:939–973`). Do **not** move the heavy analyzer FFTs ahead of the yield just to reuse them — that adds audio-path latency/underrun on USB2. If the welch is moved before the demod call to feed SNR, wrap it so a welch exception falls back to **squelch-open** (`last_snr_db=None`) rather than dropping the audio block.

#### Mid-stream retune hygiene **[corrected]**
`listen_offset_hz` and `listen_chan_bw_hz` are read fresh every block; when the operator clicks a new signal or changes width, the passband shifts discontinuously. On detecting a change in `off`/`bw` between blocks, **reset/fast-converge `DemodState`** (zero `deemph_z`, snap `agc_gain` toward the new block's target) so a mid-stream signal change doesn't click.

---

### 4b. Auto-classify + auto-width pipeline

One new streaming endpoint composes the two halves that never met: the peak enumerator and the single-signal fusion.

#### New peak detector — in `survey/sweep/detector.py` **[corrected path]**
```python
# survey/sweep/detector.py — new fn after detect_signals (~line 81)
def detect_peaks_in_psd(freqs_mhz, power_dbm, threshold_db=12.0,
                        merge_gap_bins=2, min_run_bins=1, floor_dbm=None):
    # floor = median(power_dbm) unless provided; hot = bins >= floor+threshold;
    # merge adjacent hot runs within merge_gap_bins; power-weighted center per run.
    # returns (floor, [ {center_mhz, occ_bw_khz_est, peak_dbm, floor_dbm, bin_start, bin_end} ])
```
- **[corrected]** There is no `_merge_runs` helper today — the merge loop is inlined in `detect_signals` (lines 52–61). Either **factor it out** into `_merge_runs(hot, merge_gap_bins)` and have both callers use it, or inline the identical loop here. Do not reference a helper that doesn't exist.
- **[corrected — DC/LO-leakage spike, BLOCKER fix]** The live analyzer frame is produced in `_audio_blocks` by calling `welch_psd` **directly with no DC notch** (`notch_dc`/`dc_guard_hz` are applied only in `read_sweep`). The Pluto has a large DC/LO-leakage spike at band center that would form a hot run on **every** run. Before detection, **mask the central ±`dc_guard_hz` (default 250 kHz) bins** and **drop any detected run whose center falls in the guard band**. The tune-center bin is never a valid signal.

#### New endpoint — `POST /api/analyze/window` (NDJSON stream)
```python
@app.post('/api/analyze/window')
def analyze_window():
    if not st.authorization_signed():            # §4d sign gate — HARD prerequisite
        return err('sign the on-screen authorization before classifying', 403)
    frame = st.controller.listen_spectrum
    if not frame or not frame.get('analyzer'):
        return err('no live analyzer frame — open a signal and let the stream settle', 409)
    a = frame['analyzer']
    _mask_dc_guard(a['power_dbm'], a['freqs_mhz'], dc_guard_hz)   # [corrected]
    _, peaks = detect_peaks_in_psd(a['freqs_mhz'], a['power_dbm'], threshold_db=thr)
    peaks = sorted(peaks, key=lambda p:-p['peak_dbm'])[:max_signals]
    dev, mlock, mstop, msrc = st.controller.monitor_target()
    is_sdr = dev is not None and hasattr(dev,'capture_iq') and getattr(dev,'is_swept_sdr',False)  # [corrected: must assign]
    ref = load_reference()
    def gen():
        yield ndjson({'type':'plan','total':len(peaks),
                      'window_fs_hz': fs,                         # [corrected: label ACTUAL fs]
                      'peaks':[{k:p[k] for k in ('center_mhz','occ_bw_khz_est','peak_dbm')} for p in peaks]})
        for i,p in enumerate(peaks):
            c = p['center_mhz']
            if not is_sdr or st.controller.busy(msrc):
                yield ndjson({'type':'signal','index':i,'center_mhz':c,'in_range':False,
                              'candidates':identify(c*1e6,None,ref)[:3],'features':None}); continue
            try:
                mstop.set()
                # [corrected] data-mode captures need the longer window the single-signal path uses
                secs = pocsag_seconds if _looks_data_mode(p) else window_analyze_seconds
                with mlock: iq = dev.capture_iq(c*1e6, fs, int(fs*secs))
                feats = iq_features(iq, fs); routed = route_decode(iq, fs, feats)
                interp = interpret(feats, routed.get('protocol'))
                dec = routed if routed.get('messages') else None
                if st.demo: dec = None; interp = interpret(feats, None)
                wide = feats['occupied_bw_khz']*1000 >= 0.9*fs
                bw = None if (not feats.get('signal_present') or wide) else feats['occupied_bw_khz']*1000
                yield ndjson({'type':'signal','index':i,'center_mhz':c,'features':feats,
                              'interpretation':interp,'candidates':identify(c*1e6,feats,ref)[:3],
                              'decode':dec,'chan_bw_hz':bw,'wider_than_capture':wide,
                              'signal_present':feats.get('signal_present')})
            except Exception as e:
                yield ndjson({'type':'signal','index':i,'center_mhz':c,'error':str(e)})
        yield ndjson({'type':'done'})
    return Response(stream_with_context(gen()), mimetype='application/x-ndjson')
```

#### Auto-width wiring
Each `signal` line carries `chan_bw_hz = occupied_bw_khz*1000` (or `null` when weak/wider-than-capture). The client feeds it into the **existing** `setDemodBw(sig.chan_bw_hz)` → `POST /api/listen/bandwidth`. **This is the missing wire; the plumbing already exists.**

#### Client rendering
`analyzeWindow()` reads the NDJSON `fetch` `ReadableStream`, accumulating a text buffer and splitting on `\n`. **[corrected]** — this buffer-split loop is **new** client code; `pumpPcm` scans for exactly one header newline then switches to raw binary PCM, so only the *reader acquisition* is shared, not line-splitting. On `plan`: render N placeholder cards + a `k/N` progress pill. On each `signal`: fill card k (reuse `READABLE_COLOR`, `decodeMsgLine`, the features/candidate formatters from `renderAnalyze`). Weak peaks render muted ("no clear signal (SNR x dB)") with no class and no auto-width. `wider_than_capture` shows "wider than the capture window — widen Instant BW or run a full sweep" instead of a truncated BW.

#### Reach corrections **[all corrected during review]**
- **Window size is the live `fs`, default 2.4 MHz — not 20 MHz.** The analyzer frame is fed by `stream_iq` at `stream_sample_rate = 2_400_000` unless the operator opened LISTEN with `ibw_mhz=20`. Label cards with the **actual** live fs; to classify a full 20 MHz the operator must first re-open listen at a wider IBW.
- **"Tune + set width" must restart the stream.** The batch preempts the live stream (`mstop.set()`), so `_audio_blocks` exits and nothing reads `listen_offset_hz`/`listen_chan_bw_hz` afterward. The per-card action must **restart `/api/listen_pcm` at the chosen center**, *then* apply `setDemodBw` — not just write attributes into a dead stream.
- **Offset can't reach far peaks.** `/api/listen/offset` clamps to `fs*0.48` (≈ ±1.15 MHz at 2.4 MHz fs) and is a baseband shift, not an LO re-tune. For any card whose center is outside ±(fs·0.48) of the live center, do a **real re-tune** (new stream at that center), not an offset.
- **Data-mode capture length.** POCSAG/FLEX decoders need the same `pocsag_seconds ≈ 3.0` the single-signal path uses; a 1.5 s batch capture will systematically under-decode pager text. Extend the capture for data-mode peaks; keep 1.5 s for analog/identify-only peaks.
- **Auto-width is quantized.** At 2.4 MHz fs and `nperseg ≤ 1024`, bin spacing ≈ 2.3 kHz, so a 12.5 kHz NFM channel spans a handful of bins. Raise `nperseg` on the per-signal capture for narrow signals, or state honestly that width is quantized to ~2 kHz and floor `chan_bw` to a sane minimum.

### 4b.1 Artemis reference source + formulaic (ACF) identification

**Purpose.** §4b classifies every peak in the live window (`iq_features → classify_modulation → identify → route_decode → interpret`). This subsection adds the operator's ask — *"also use the Artemis signal wiki; it also contains formulaic strategies to identify signals"* — in two composable pieces that plug into the **existing** feature/ID stack in `survey/identify.py`:

1. **Artemis as a third reference SOURCE** alongside `sigidwiki_ref.json` + `surveillance_catalog.json`. A new read-only loader `survey/artemis.py` opens the operator's **own** Artemis 4 SQLite DB and folds its signals into the exact reference-row shape `load_reference()` already yields — plus one new optional field, `acf_ms: list[float]` (Artemis stores the Auto-Correlation Function as a time in **milliseconds**, and a single signal may carry several: e.g. a symbol period *and* a frame period).
2. **A formulaic (ACF-driven) identification layer.** ACF is promoted to a first-class measured feature (`_symbol_rate_hz` → `_acf_analysis`), `identify()` gains an ACF match score with a **formula-reason string** (`"measured ACF 6.6 ms ≈ Artemis ACF 6.67 ms (±1.0%)"`), and a `build_id_report()` assembles the systematic measured params + per-candidate formulas that ride out on `/api/analyze/signal` and the §4b window cards.

Everything stays receive-only, relative-RSSI, honest-confidence. ACF is **time-domain**, so it is trustworthy on the uncalibrated Pluto (unlike power) — subject to the honesty box below. The two halves are independent: the loader carries Artemis's *published* `acf_ms` values as comparison targets; the ACF layer *measures* ACF from IQ. Neither is required for the other to ship.

---

#### A. Artemis SQLite loader — `survey/artemis.py` (NEW, stdlib-only, fail-soft)

Reference-source paths are **hard-coded today** (`DEFAULT_REF_PATH`/`CATALOG_PATH`, `identify.py:228-229`) with no config key. Artemis must **not** copy that pattern — it takes a **new config seam** `pluto.artemis_db_path` (default `''` = disabled) and a loader that emits the same row shape `load_reference()` returns:

```
{name, freq_min_mhz, freq_max_mhz, bandwidth_khz, mode, class, duty, tscm, url}
   + acf_ms: list[float]      # NEW optional — consumed by the formulaic layer
   + source: 'artemis'        # provenance tag (card badge + license labeling)
   + license_note: <string>   # mirrored CC BY-NC-SA note on every Artemis row
```

`identify()` reads `ref['freq_min_mhz']`/`ref['freq_max_mhz']` **mandatorily** (no `.get`, lines 279/286-287) and `ref['bandwidth_khz']/['class']/['mode']/['name']/['url']` positionally, so each must be present and unit-correct or scoring silently breaks. Artemis stores a **single** center frequency, so `freq_min/max` are **synthesized** from center ± half-occupied-bandwidth (or ±2% when BW is unknown), floored so the band is never zero-width. `sqlite3` is already a dependency (`survey/db.py:8,290`); open read-only so we never mutate the operator's DB.

```python
# survey/artemis.py — NEW. Read-only Artemis 4 SQLite loader. stdlib only
# (sqlite3/re/os/logging/pathlib), numpy-free, fail-soft (returns [] on any
# error, never raises to identify.py). Reads ONLY relational fields — never the
# waterfall-image / audio-sample blobs Artemis bundles (licensing, see box).
import logging, os, pathlib, re, sqlite3
log = logging.getLogger("survey.artemis")

_ARTEMIS_LICENSE_NOTE = (
    "Artemis DB content is sigidwiki-derived (CC BY-NC-SA). Internal "
    "identification only; do NOT embed Artemis/sigidwiki waterfall images or "
    "verbatim descriptions in client deliverables without attribution / "
    "non-commercial review.")

# schema flexibility: A4 (AresValley rewrite) is relational; column NAMES drift
# across app versions and custom user DBs. DISCOVER the table + columns.
_TABLE_CANDIDATES = ("signals", "signal", "Signals", "SIGNALS")
_COL_ALIASES = {
    "name":       ["name", "signal_name", "title"],
    "freq":       ["frequency", "freq", "center_frequency", "centre_frequency"],
    "freq_min":   ["frequency_min", "freq_min", "min_frequency", "inferior_frequency", "lower_frequency"],
    "freq_max":   ["frequency_max", "freq_max", "max_frequency", "superior_frequency", "upper_frequency"],
    "bandwidth":  ["bandwidth", "band_width", "bw"],
    "modulation": ["modulation", "mod"],
    "mode":       ["mode"],
    "category":   ["category_label", "category", "cat"],
    "description":["description", "notes", "info"],
    "acronym":    ["acronym", "abbr"],
    "urls":       ["urls", "url", "wiki_url", "link", "links"],
    "acf":        ["acf", "acf_ms", "autocorrelation", "auto_correlation"],
}

# closed target == classify_modulation() outputs (identify.py:190-220). Anything
# else silently never earns the +20 class credit. psk/qam -> continuous_digital
# (the nearest tag classify_modulation ACTUALLY emits — it never returns 'psk').
_CLASS_MAP = {
    "fsk":"fsk","gfsk":"fsk","2-fsk":"fsk","2fsk":"fsk","4-fsk":"fsk","4fsk":"fsk","mfsk":"fsk","afsk":"fsk","msk":"fsk","gmsk":"fsk",
    "ook":"ook","ask":"ook","cw":"ook","morse":"ook",
    "ofdm":"ofdm_wideband","cofdm":"ofdm_wideband","dmt":"ofdm_wideband","dsss":"ofdm_wideband","wifi":"ofdm_wideband","lte":"ofdm_wideband",
    "fm":"analog_voice","nfm":"analog_voice","wfm":"analog_voice","am":"analog_voice","ssb":"analog_voice","usb":"analog_voice","lsb":"analog_voice","voice":"analog_voice",
    "atv":"analog_video","video":"analog_video","pal":"analog_video","ntsc":"analog_video",
    "psk":"continuous_digital","bpsk":"continuous_digital","qpsk":"continuous_digital","qam":"continuous_digital","apsk":"continuous_digital","psk31":"continuous_digital",
}

def _open_ro(db_path):
    # PROPER file URI (Windows-safe): pathlib emits file:///C:/Users/...  with
    # forward slashes + percent-encoding — a naive 'file:'+abspath leaves
    # backslashes/spaces that SQLite's URI parser rejects, which on this Windows
    # box would make EVERY operator DB fail to open.
    uri = pathlib.Path(os.path.abspath(db_path)).as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)   # never creates; ro
    conn.row_factory = sqlite3.Row
    return conn

def _find_table(conn):
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    for cand in _TABLE_CANDIDATES:
        if cand in names:
            return cand
    for t in names:                                   # heuristic fallback
        cols = {c[1].lower() for c in conn.execute(f'PRAGMA table_info("{t}")')}
        if (cols & set(_COL_ALIASES["name"])) and \
           (cols & set(_COL_ALIASES["freq"] + _COL_ALIASES["freq_min"])):
            return t
    return None

def _resolve_columns(conn, table):
    have = {c[1].lower(): c[1] for c in conn.execute(f'PRAGMA table_info("{table}")')}
    out = {}
    for field, aliases in _COL_ALIASES.items():
        for a in aliases:
            if a in have:
                out[field] = have[a]; break
    return out

def _get(row, cols, field):
    col = cols.get(field)
    return row[col] if col is not None else None

def _to_mhz(v):
    try: x = float(v)
    except (TypeError, ValueError): return None
    if x <= 0: return None
    if x >= 1e6: return x / 1e6          # Hz  (A4 stores Hz)
    if x >= 1e3: return x / 1e3          # kHz (rare)
    return x                             # already MHz

def _bw_to_khz(v):
    try: x = float(v)
    except (TypeError, ValueError): return None
    if x <= 0: return None
    if x >= 1e6: return x / 1e3          # Hz -> kHz
    return x                             # kHz (BW unit is genuinely ambiguous —
                                         # see risk/open-decision + artemis_bw_unit)

def _synthesize_band(freq_mhz, bw_khz):
    if bw_khz and bw_khz > 0:
        half = max((bw_khz / 1e3) / 2.0, freq_mhz * 0.001)   # >=0.1% of center
    else:
        half = freq_mhz * 0.02
    return round(freq_mhz - half, 6), round(freq_mhz + half, 6)

def _map_class(modulation, category):
    for raw in (modulation, category):
        if not raw: continue
        key = re.sub(r"[^a-z0-9 -]", "", str(raw).strip().lower())
        if key in _CLASS_MAP: return _CLASS_MAP[key]
        for tok, cls in _CLASS_MAP.items():            # substring ('gaussian fsk')
            if tok in key: return cls
    return "unknown"                                   # safe: no +20, never crashes

def _parse_acf_ms(v):
    # Artemis ACF is a TIME in ms; may be multi-valued ('6.67; 40.0' / list).
    if v is None: return []
    if isinstance(v, (int, float)): return [round(float(v), 4)] if v > 0 else []
    out = []
    for tok in re.split(r"[;,/\s]+", str(v).strip()):
        m = re.search(r"[-+]?\d*\.?\d+", tok)
        if m:
            try:
                x = float(m.group())
                if x > 0: out.append(round(x, 4))
            except ValueError: pass
    return out

def _map_row(row, cols):
    name = _get(row, cols, "name")
    if not name: return None
    freq_mhz = _to_mhz(_get(row, cols, "freq"))
    fmin = _to_mhz(_get(row, cols, "freq_min"))
    fmax = _to_mhz(_get(row, cols, "freq_max"))
    bw_khz = _bw_to_khz(_get(row, cols, "bandwidth"))
    if fmin is None or fmax is None:
        if freq_mhz is None: return None               # unusable — no band
        fmin, fmax = _synthesize_band(freq_mhz, bw_khz)
    if fmin > fmax: fmin, fmax = fmax, fmin
    modulation = _get(row, cols, "modulation")
    mode = _get(row, cols, "mode")
    mode_str = " / ".join(str(x) for x in (modulation, mode) if x) or "unknown"
    acro = _get(row, cols, "acronym") or ""
    desc = _get(row, cols, "description") or ""
    tscm = ((f"[{acro}] " if acro else "") + str(desc)).strip()
    tscm = (tscm + " (Artemis reference — verify against sigidwiki entry.)").strip()
    urls = _get(row, cols, "urls") or ""
    return {
        "name": str(name),
        "freq_min_mhz": fmin, "freq_max_mhz": fmax,
        "bandwidth_khz": round(bw_khz, 1) if bw_khz is not None else 0.0,
        "mode": mode_str,
        "class": _map_class(modulation, _get(row, cols, "category")),
        "duty": "",                                    # Artemis has no duty -> no +8
        "tscm": tscm,
        "url": str(urls).split()[0] if urls else "",   # first URL only
        "acf_ms": _parse_acf_ms(_get(row, cols, "acf")),
        "source": "artemis",
        "license_note": _ARTEMIS_LICENSE_NOTE,
    }

def load_artemis_reference(db_path):
    if not db_path:
        return []
    try:
        conn = _open_ro(db_path)
    except sqlite3.Error as e:
        log.warning("Artemis DB not opened (%s) — skipping Artemis reference", e)
        return []                                      # WARNING, not silent info
    rows = []
    try:
        table = _find_table(conn)
        if not table:
            log.warning("Artemis DB %s: no recognizable signal table — skipping", db_path)
            return []
        cols = _resolve_columns(conn, table)
        if "name" not in cols or not (cols.get("freq") or cols.get("freq_min")):
            log.warning("Artemis table %s missing name/frequency columns — skipping", table)
            return []
        sel = ", ".join(f'"{c}"' for c in set(cols.values()))
        for r in conn.execute(f'SELECT {sel} FROM "{table}"'):
            mapped = _map_row(r, cols)
            if mapped is not None:
                rows.append(mapped)
    except sqlite3.Error as e:
        log.warning("Artemis read error (%s) — returning %d rows parsed so far", e, len(rows))
    finally:
        conn.close()
    log.info("Artemis reference: %d signals from %s", len(rows), db_path)
    return rows
```

**Fold into `load_reference()` — `identify.py:232-257`, config-driven, guarded, isolation preserved.** No path is hard-coded; `load_reference()` gains an optional `artemis_db_path` parameter, and folds **only when `path is None`** (the same `if path is None:` guard the catalog uses, so `load_reference(path=explicit)` still loads ONLY that file — the documented test-isolation contract holds):

```python
def load_reference(path=None, artemis_db_path=None):
    ...  # existing sigidwiki load + (path is None) catalog fold, unchanged
    if path is None and artemis_db_path:
        try:
            from .artemis import load_artemis_reference
            refs.extend(load_artemis_reference(artemis_db_path))   # already fail-soft
        except Exception:                                          # belt-and-suspenders
            log.warning("Artemis reference load skipped", exc_info=True)
    return refs
```

`identify()` **ignores unknown keys today**, so carrying `acf_ms`/`source`/`license_note` requires **zero** `identify()` change for safety — they simply ride along until the formulaic layer consumes them.

**Absent-DB guard.** `artemis_db_path=''` (default) → no read, no import, behavior byte-identical to today. A missing / locked / corrupt / odd-schema DB degrades exactly like the optional `surveillance_catalog.json` does now: `load_artemis_reference` returns `[]`, sigidwiki + catalog matching are untouched. `mode=ro` + `timeout=5` bound any write-lock wait (Artemis open in another process) and guarantee we never create or mutate a file.

**Wire the config key into the module at startup** (`identify` has no `Config` handle; per-request `load_reference()` callers pass the resolved path). Inside `create_app()` (`server.py`), immediately after `st = AppState(db, cfg)` (**server.py:79**), compute the path once and hand it to every `load_reference()` call — the real callers are **`/api/identify` (`server.py:600` load, `633` identify)** and **`/api/analyze/signal` (`server.py:747` load, `820` identify)**. (`/api/inspect` never calls `load_reference`/`identify`, so it needs no change.)

```python
# server.py, inside create_app(), just after line 79 (st = AppState(db, cfg))
_art = cfg.get("pluto", "artemis_db_path", default="")
st.artemis_db_path = cfg.abs_path(_art) if _art else ""   # abs_path -> relative to config.yaml
# each endpoint: ref = load_reference(artemis_db_path=st.artemis_db_path)
```

---

#### B. ACF promotion in the DSP layer — `_symbol_rate_hz` → `_acf_analysis`

> **Correction from review — `_symbol_rate_hz` is NOT a working symbol-rate extractor today.** At `fs = 2.4 MHz`, `lo = max(2, int(fs/2e6)) = 2`, and the envelope ACF decays monotonically from lag 0, so `argmax(seg)` pins to lag 2 (the DC-adjacent shoulder, `ac ≈ 0.99`) for essentially any input — it returns `≈ fs/2` regardless of the signal. The test suite passes only because nothing asserts `symbol_rate_kbd`. So this is a **fix, not a byte-compatible refactor**, and the earlier "symbol scale resolves fine / 6.67 ms ⇒ 150 baud symbol-scale example" framing is **withdrawn**: today's code cannot resolve any period longer than the `4000`-lag cap (1.667 ms), and its argmax never leaves the DC shoulder anyway.

`_acf_analysis` measures the envelope autocorrelation honestly and returns **multiple** peaks in **milliseconds**, split into a symbol-scale primary and an optional frame-scale peak, each with a quality:

```python
def _acf_analysis(iq, fs, *, capture_seconds, snr_db, signal_present,
                  max_frame_ms=60.0, min_repeats=10):
    NONE = {"acf_ms": None, "acf_peaks_ms": [], "acf_rate_hz": None,
            "frame_period_ms": None, "acf_quality": 0.0,
            "acf_note": "no repetitive envelope structure"}
    if not signal_present or snr_db < 6.0:           # inherit the SNR/present gate
        return {**NONE, "acf_note": "below SNR gate — ACF not meaningful"}
    mag = np.abs(iq).astype(np.float64); mag -= mag.mean()
    if np.allclose(mag, 0): return NONE
    # DC/LO high-pass: the live listen frame has NO DC notch (notch only in
    # read_sweep); a wandering LO injects a slow AM term that competes with the
    # true lag. One-pole HP on the mean-subtracted envelope kills it.
    mag = mag - _one_pole_lowpass(mag, alpha=0.001)

    # analysis window: up to the full capture (bounded) so FRAME lags are
    # reachable — NOT the old fixed 16384-sample (6.83 ms) window.
    n = int(min(mag.size, max(1 << 14, capture_seconds * fs)))

    # FFT autocorrelation, O(n log n). np.correlate(mode='full') is O(n^2) and
    # DID NOT COMPLETE on a 1.2M / 7.2M-sample capture in review — it must NOT
    # appear on the analyze path. Unbiased normalization by overlap count (n-k)
    # removes np.correlate's triangular bias that otherwise SUPPRESSES long lags.
    m = 1 << int(np.ceil(np.log2(2 * n)))
    F = np.fft.rfft(mag[:n], m)
    ac = np.fft.irfft(F * np.conj(F), m)[:n]
    if ac[0] <= 0: return NONE
    overlap = np.arange(n, 0, -1, dtype=np.float64)  # (n-k) terms per lag
    ac = (ac / ac[0]) * (n / overlap)                # unbiased, normalized

    lo = max(2, int(fs / 2e6))                       # drop DC-adjacent; ~1.2 MBaud ceiling
    hi = min(ac.size, lo + int(fs * (max_frame_ms / 1000.0)))
    seg = ac[lo:hi]
    if seg.size == 0: return NONE
    # first TRUE off-shoulder local maxima with prominence over the local floor —
    # NOT argmax (argmax on a monotone decay pins to lo). A real period repeats:
    # verify a comb at 2k,3k,... The 0.2 height gate is kept as the primary knob.
    peaks = _acf_peaks(seg, lo, min_height=0.2, min_prominence=0.08)
    if not peaks:
        return {**NONE, "acf_note": "no ACF peak >=0.2 — noise / one-shot / OFDM / random-payload"}
    ranked = sorted(peaks, key=lambda p: p["height"], reverse=True)
    def ms(lag): return 1000.0 * lag / fs
    acf_peaks = [{"acf_ms": round(ms(p["lag"]), 4), "strength": round(p["height"], 3)}
                 for p in ranked[:4]]
    prim = ranked[0]; acf_ms = ms(prim["lag"])
    # FRAME peak: strongest peak that is (a) >= ~5x the symbol ms AND (b) NOT within
    # tol of an integer multiple of the primary lag (harmonic-comb rejection — a
    # periodic envelope produces a full comb at 2k,3k,... that would otherwise be
    # mislabeled a 'frame'), AND (c) yields >= min_repeats repeats in the capture.
    frame_ms = None
    for p in ranked:
        fms = ms(p["lag"])
        if fms < 5.0 * acf_ms: continue
        ratio = fms / acf_ms
        if abs(ratio - round(ratio)) < 0.06: continue          # symbol harmonic — skip
        if (capture_seconds * 1000.0) / fms < min_repeats: continue  # LOAD-BEARING gate
        frame_ms = fms; break
    q = _acf_quality(prim["height"], snr_db, capture_seconds, frame_ms or acf_ms)
    return {"acf_ms": round(acf_ms, 4),
            "acf_peaks_ms": acf_peaks,
            "acf_rate_hz": round(1000.0 / acf_ms, 1),   # envelope-PATTERN repetition rate (see box)
            "frame_period_ms": round(frame_ms, 3) if frame_ms else None,
            "acf_quality": round(q, 3),
            "acf_note": "ACF ok" if q >= 0.4 else "low-confidence ACF (weak/short)"}

def _acf_quality(h, snr_db, cap_s, reported_ms):
    hq = min(1.0, max(0.0, (h - 0.2) / 0.6))            # 0.2->0, 0.8->1 (dominant)
    sq = min(1.0, max(0.0, (snr_db - 6.0) / 14.0))      # 6->0, 20->1
    reps = cap_s * 1000.0 / reported_ms if reported_ms > 0 else 0.0
    rq = min(1.0, reps / 50.0)                          # repeats of the REPORTED period
    return 0.6 * hq + 0.25 * sq + 0.15 * rq

def _symbol_rate_hz(iq, fs):    # thin back-compat shim (returns Hz, as before)
    a = _acf_analysis(iq, fs, capture_seconds=iq.size / fs,
                      snr_db=99.0, signal_present=True)
    return 1000.0 / a["acf_ms"] if a.get("acf_ms") else 0.0   # 1000/ms == fs/k
```

`iq_features()` (`identify.py:138-187`) calls `_acf_analysis` at the existing `_symbol_rate_hz` call site (**~line 170**; `snr_db` and `signal_present` are already local at lines 164-165) and merges the new fields next to `symbol_rate_kbd`, which is **preserved** (now ACF-sourced) so `tests/test_identify.py` and `classify_modulation` are untouched:

```python
acf = _acf_analysis(iq, fs, capture_seconds=iq.size / fs,
                    snr_db=snr_db, signal_present=signal_present)
feats.update({
    "acf_ms": acf["acf_ms"], "acf_peaks_ms": acf["acf_peaks_ms"],
    "acf_rate_hz": acf["acf_rate_hz"], "frame_period_ms": acf["frame_period_ms"],
    "acf_quality": acf["acf_quality"], "acf_note": acf["acf_note"],
    "symbol_rate_kbd": round((acf["acf_rate_hz"] or 0.0) / 1e3, 2)})   # key unchanged
```

> **Honesty box — when ACF is / isn't meaningful (all reviewer-confirmed):**
> - **"Repetition period", not "baud".** The envelope ACF peaks at the *repetition period of the on/off pattern*, which equals the symbol period **only** when the envelope changes state every symbol. A random NRZ/OOK/PSK payload has **no** periodic re-correlation — its envelope ACF is a single triangle with no comb, so `_acf_peaks` correctly returns **nothing** and `acf_ms = None`. **ACF fires only for PERIODIC-ENVELOPE emitters** (burst/frame cadence, sync preambles, on-off patterns, deterministic beacons — pagers, telemetry beacons, keyers) — which is exactly the repetitive structure a bug sweep cares about — and stays silent for random-payload continuous modulation. That silence is correct, not a bug. We therefore report `acf_rate_hz` (the pattern-repetition rate) and match Artemis `acf_ms` **period-vs-period**; any "baud" phrasing is an estimate labeled as such.
> - **Resolution.** Lag quantum = `1/fs = 0.417 µs`; finest resolvable period ≈ `0.83 µs` (lag 2) ⇒ a ~1.2 MBaud ceiling. Sub-µs structure is unresolved and reported as such.
> - **Frame scale is code-limited, not span-limited — and the repeats gate is load-bearing.** The live analyze capture spans 2000–3000 ms (`analyze_seconds=2.0` / `pocsag_seconds=3.0`), so a tens-of-ms frame fits comfortably in *span*; the old limiters were the 16384-sample window and 1.667 ms lag cap, now raised. A frame period is emitted **only** when the capture holds `≥ min_repeats` (10) repeats of it — a 40 ms frame at 1.5 s gives ~37 repeats (reliable); at 3.0 s ~75. The `> 1000*capture_seconds` check is a backstop; the `≥10-repeats` gate is what actually prevents a fabricated period. `capture_seconds` must be the **actual** captured span (`iq.size/fs`), which it is.
> - **No period on noise/one-shot/OFDM.** Below the SNR gate, or with a near-white envelope (OFDM/noise), or a single transient burst (no repeat), `acf_ms = None`, `acf_rate_hz = None`, `acf_quality = 0`, and no ACF formula is scored. Unbiased long-lag normalization is paired with a prominence + comb gate precisely so raised long-lag noise variance cannot masquerade as a period.
> - **Cost.** FFT autocorrelation only — `np.correlate(mode='full')` at full capture is O(n²) and did not complete in review; it must not ship on the analyze path.
> - **Clock.** Timing accuracy is bounded by the Pluto reference-clock ppm (nominal `fs` assumed) — tens of ppm, far below the ~10% ID tolerance, so it does not affect matching but ACF is not clock-exact.

---

#### C. `_acf_score` in `identify()` — formula-reason strings

`identify()` (`identify.py:268-318`) gains an ACF term inside the `if feats:` branch, after the class/duty scoring. It fires **only** when both the measurement (`feats['acf_ms']`) and the ref (`ref.get('acf_ms')`, Artemis rows or hand-curated) carry ACF — sigidwiki starter rows without ACF are unaffected. Weight is a deliberately modest **+15 max** (below band 50 / bw 30 / class 20, above duty 8) so a strong ACF match **reinforces** but never overrides band+class:

```python
def _acf_score(meas_acf_ms, ref_acf_list, fs, tol_pct=8.0):
    if not meas_acf_ms or not ref_acf_list:
        return (0.0, None, "")
    lag_quantum_ms = 1000.0 / fs                       # absolute floor for sub-ms
    abs_floor_ms = 2.0 * lag_quantum_ms                # ~2 lag quanta
    best = None
    for r in ref_acf_list:
        if r <= 0: continue
        err_pct = abs(meas_acf_ms - r) / r * 100.0
        within = err_pct <= tol_pct or abs(meas_acf_ms - r) <= abs_floor_ms
        if within and (best is None or err_pct < best[0]):
            best = (err_pct, r)
    if best is None:
        return (0.0, None, "")
    err_pct, r = best
    s = max(0.0, 1.0 - err_pct / tol_pct)
    baud = 1000.0 / meas_acf_ms                         # ESTIMATE (see honesty box)
    baud_ref = 1000.0 / r
    reason = (f"measured ACF {meas_acf_ms:.2f} ms (~{baud:.0f} sym/s) ≈ "
              f"Artemis ACF {r:.2f} ms (~{baud_ref:.0f} sym/s), ±{err_pct:.1f}%")
    return (s, r, reason)
```

Inside `identify()`'s `if feats:` block, collect the concrete **formula chips** (distinct from the prose `reasons`) and fold in the ACF score:

```python
formula = []
if feats.get("occupied_bw_khz"):
    formula.append(f"BW ~{feats['occupied_bw_khz']:.0f} kHz")
if feats.get("fsk_shift_khz"):
    formula.append(f"FSK shift {feats['fsk_shift_khz']:.1f} kHz")
if feats.get("acf_ms"):
    formula.append(f"ACF {feats['acf_ms']:.2f} ms => ~{feats['acf_rate_hz']:.0f} /s")
acf_s, acf_matched, acf_reason = _acf_score(
    feats.get("acf_ms"), ref.get("acf_ms") or [], fs=capture_fs)
if acf_s > 0:
    score += 15.0 * acf_s
    reasons.append(acf_reason); formula.append(acf_reason)
# ... each candidate dict gains:  'formula_reasons': formula,
#                                 'acf_matched_ms': acf_matched,
#                                 'source': ref.get('source'),          # 'artemis' badge
#                                 'license_note': ref.get('license_note')
```

*(`_acf_score` needs `fs`; `identify()` gains an optional `capture_fs` param — defaulted to the live `2.4e6` — since the lag-quantum floor is fs-dependent. Tolerance is a fractional window OR a ~2-lag-quantum absolute floor so sub-ms symbol periods aren't held to an unreachably tight `±8%` window; it is lag-dependent because integer-lag quantization coarsens at short lags.)*

---

#### D. The formulaic ID report — `build_id_report()`, wired into cards + `/api/analyze/signal`

A pure-assembly (no DSP) object bundling the systematic measured params + derived formulas + the ranked candidates (already carrying `formula_reasons`). Degrades to an explicit honest note for noise/one-shot:

```python
def build_id_report(center_hz, feats, candidates):
    if not feats or not feats.get("signal_present"):
        return {"measured": None, "formulas": [], "candidates": candidates,
                "note": "no clear signal — ACF/formulaic ID needs a repetitive "
                        "emitter above the noise floor"}
    m = {"center_mhz": round(center_hz / 1e6, 4),
         "occupied_bw_khz": feats["occupied_bw_khz"],
         "acf_ms": feats.get("acf_ms"), "acf_peaks_ms": feats.get("acf_peaks_ms"),
         "acf_rate_hz": feats.get("acf_rate_hz"),
         "frame_period_ms": feats.get("frame_period_ms"),
         "fsk_shift_khz": feats.get("fsk_shift_khz"),
         "distinct_tones": feats.get("distinct_tones"),
         "modulation_class": feats.get("modulation_class"),
         "acf_quality": feats.get("acf_quality"), "acf_note": feats.get("acf_note")}
    formulas = []
    if m["acf_rate_hz"]:
        formulas.append(f"pattern rate ≈ 1000/{m['acf_ms']:.2f} ms = {m['acf_rate_hz']:.0f} /s (estimate)")
    if m["frame_period_ms"]:
        formulas.append(f"frame rate ≈ 1000/{m['frame_period_ms']:.1f} ms = {1000.0/m['frame_period_ms']:.1f} Hz")
    if m["fsk_shift_khz"]:
        formulas.append(f"FSK shift = {m['fsk_shift_khz']:.1f} kHz")
    return {"measured": m, "formulas": formulas, "candidates": candidates,
            "note": m["acf_note"]}
```

**Wiring.** On `/api/analyze/signal` (`server.py`), pass Artemis into the load and attach the report **after** candidates are built and **after** the demo strip (so the report reflects nulled content, consistent with the §4b demo-honesty invariant):

```python
ref = load_reference(artemis_db_path=st.artemis_db_path)   # server.py:747
# ... existing capture / feats / candidates (identify at 820) ...
id_report = build_id_report(center_mhz * 1e6, feats, candidates)
# response dict gains:  'id_report': id_report
```

On the §4b `/api/analyze/window` NDJSON `gen()`, each `signal` line gains the same shape so window cards render identical formula chips:

```python
cands = identify(c * 1e6, feats, ref)[:3]                  # already carry formula_reasons
yield ndjson({"type": "signal", "index": i, "center_mhz": c, "features": feats,
              "candidates": cands, "id_report": build_id_report(c * 1e6, feats, cands),
              "interpretation": interp, ...})
```

Frame-scale ACF claims ride on the capture length these endpoints already use: symbol/pattern-scale ACF is available even on the 1.5 s window; frame-scale appears only when the span holds `≥10` repeats (§4b already routes data-mode peaks to the longer `pocsag_seconds` capture).

> **Licensing box (CC BY-NC-SA — governs by construction).** Artemis 4 is open-source software, but its **bundled DB is sigidwiki-derived**, so it inherits the same CC BY-NC-SA constraint already recorded in `sigidwiki_ref.json _meta.license_note`: *"sigidwiki content is CC BY-NC-SA. Use for internal identification; do NOT embed their waterfall images in client deliverables without attribution / non-commercial review."* The design is licensing-clean by construction:
> 1. **Default OFF** — `pluto.artemis_db_path` defaults to `''`; nothing Artemis-derived is read unless the operator opts in by pointing at **their own** Artemis SQLite DB. Shield ships **no** scraped copy.
> 2. **Read-only** (`mode=ro`) — we never modify, copy, or re-export the operator's DB; no derivative database is persisted.
> 3. **Images/audio structurally excluded** — the loader reads **only** relational/text fields (`name/frequency/bandwidth/modulation/mode/location/category/category_label/description/acronym/urls/acf`); it never touches the waterfall-image or audio-sample blobs, so those cannot leak into a client deliverable.
> 4. **Provenance + mirrored note** — every Artemis row carries `source:'artemis'` + `license_note` (verbatim mirror of the sigidwiki note); `report.py` should suppress/attribute any `source=='artemis'` descriptive text in client-facing output the same way it already handles sigidwiki and the surveillance catalog.
> 5. **Non-commercial** — internal identification during a paid TSCM engagement is the same internal-use posture `sigidwiki_ref` already operates under; any doubt on a specific deliverable is an attribution/NC **review** item, not a code decision. Test fixtures build their **own** tiny SQLite DBs — no real Artemis content enters the repo or CI.

---

### Consolidated deltas (paste into §7)

**New files** (add to §7 *New files* list):
- `survey/artemis.py` — read-only Artemis 4 SQLite reference loader (stdlib-only, fail-soft).
- `tests/test_artemis.py` — synthetic in-memory/temp SQLite DBs: canonical-schema mapping (Hz→MHz, BW→kHz, class normalized, ACF parsed to list); single-freq band synthesis; multi-ACF `'6.67; 40.0'`→`[6.67,40.0]`; unknown modulation→`'unknown'` (no crash); custom table name + aliased columns discovered; **missing DB → `[]` and creates NO file** (proves `mode=ro`); no name/freq columns → `[]` with warning; **file URI opens under a path containing a space** (Windows). Plus extend `tests/test_identify.py`: `load_reference(artemis_db_path=tmp)` folds rows and `identify()` ranks an in-band Artemis row without `KeyError`; `load_reference(path=explicit)` excludes Artemis (isolation).
- `tests/test_acf.py` — `_acf_analysis`: periodic-envelope pattern recovers `acf_ms`/`acf_rate_hz`; `acf_peaks_ms` has ≥2 descending-strength entries; 40 ms frame at 3.0 s reports `frame_period_ms`, at 0.3 s (<10 repeats) reports `None`; **random-payload carrier → `acf_ms=None`** (scope boundary asserted); noise/one-shot/OFDM → `None`; below-SNR → `None`; `frame_period_ms ≤ 1000*capture_seconds` always; >1.2 MBaud structure unresolved.
- `tests/test_formulaic_id.py` — `_acf_score` matches within tol with a formula string, scores 0 outside tol, sub-ms match uses the lag-quantum floor, no ACF score when `feats['acf_ms']` is None; matched candidate carries `formula_reasons`; `build_id_report` returns `measured=None`+honest note for noise, bundles candidates otherwise.
- *(Regression:* existing `tests/test_identify.py` + `tests/test_analyze_fuse.py` still pass — `symbol_rate_kbd` key preserved, `classify_modulation` untouched, `/api/analyze/signal` keeps its keys plus `id_report`.)*

**Config keys** (add to §7 *Config* `pluto:` block — repo-root `spectrum_survey/config.yaml` **pluto block lines 75-110**, and mirror in `config.py` `DEFAULTS['pluto']` **lines 50-112**; all additive, safe defaults, honored by `_merge`):
```yaml
pluto:
  # --- Artemis reference source (§4b.1) ---
  artemis_db_path: ''        # path (abs or relative to config.yaml) to the OPERATOR'S
                             # OWN Artemis 4 SQLite DB. '' = OFF. Read-only extra
                             # reference source. Keep sigidwiki/Artemis waterfall
                             # IMAGES out of client deliverables (CC BY-NC-SA).
  artemis_bw_unit: auto      # auto | hz | khz | mhz — override BW unit for custom DBs
  # --- formulaic ACF layer (§4b.1) ---
  pocsag_seconds: 3.0        # PROMOTE from inline default (server.py:776,778) to a
                             # declared key — frame-ACF confidence depends on it.
  acf_tol_pct: 8.0           # _acf_score fractional match tolerance
  acf_max_frame_ms: 60.0     # ACF lag-search budget (replaces the hard 4000-lag cap)
  acf_min_repeats: 10        # frame peak needs >= this many repeats in the capture
  acf_min_quality: 0.4       # below this, surface the lag but flag low confidence
```

**Endpoint field additions** (annotate §7 *Endpoints* table — no NEW endpoints):
- `POST /api/analyze/signal` (**already EXTENDED**) → response gains `id_report {measured{center_mhz, occupied_bw_khz, acf_ms, acf_peaks_ms:[{acf_ms,strength}], acf_rate_hz, frame_period_ms, fsk_shift_khz, distinct_tones, modulation_class, acf_quality, acf_note}, formulas:[str], candidates, note}`; each candidate gains `formula_reasons:[str]`, `acf_matched_ms`, `source`, `license_note`.
- `POST /api/analyze/window` (§4b, **already EXTENDED**) → each NDJSON `signal` line gains the same `id_report` + per-candidate `formula_reasons`/`source`.
- `POST /api/identify` (**EXTENDED**) → `load_reference(artemis_db_path=...)` folds Artemis candidates into its existing `candidates` (no shape change; Artemis rows just appear when the DB is set).

**Phase placement:** extends **P3** (Artemis fold into `load_reference()` + `_acf_analysis`/`iq_features` + `_acf_score`/`build_id_report` on the window path — same phase as `detect_peaks_in_psd`) and **P4** (the `source='artemis'` badge + `report.py` CC BY-NC-SA attribution handling land with the dispatcher's card/report work). `pluto.artemis_db_path` defaults OFF, so P3/P4 ship with byte-identical default behavior; enabling Artemis is an operator opt-in.

---

### 4c. Per-class dissemination dispatcher

A single pure-Python (numpy-free, mirrors `decode_router.py`) descriptor factory maps each classified signal to an ordered list of honesty-gated actions.

```python
# survey/dispatch.py — NEW
def disseminate_actions(feats, interpretation, candidates=None, decode=None,
                        *, in_range=True, demo=False, attribution_status='unassessed') -> list[dict]:
    # descriptor: {action, label, verb, endpoint, method, params, class,
    #              primary, enabled, disabled_reason, note}
```

**Primary switch = `interpret().readable`; secondary = `feats['modulation_class']` and `feats['band_hint']`.** **[corrected]** — the description and pseudocode must agree on switch order; and BLE/Wi-Fi branch on **`feats['band_hint']`** (`'ble_adv_channel'`/`'wifi_2g4'`), **not** on `modulation_class` — `classify_modulation()` never returns `'ble'`/`'wifi'` (only `fsk/ook/ofdm_wideband/analog_video/analog_voice/continuous_digital/unknown/noise_floor`), so a `cls in ('ble','wifi')` branch is dead code.

| Class (readable / modulation / band_hint) | Primary action | Also offered | Content? |
|---|---|---|---|
| `listen` + `analog_video` | **Confirm camera signature + audio subcarrier + locate** **[corrected: NOT watch/frame-grab]** | record IQ, locate | No |
| `listen` + `analog_voice` | **Listen + record audio evidence** | locate | No (analog listen) |
| `decoded`/`maybe_decode` + `ook`/`fsk` | **Decode text** (enabled only if `decode.messages` **and** `attribution=='client_space'`) | locate, record IQ | Yes — ECPA-gated |
| `band_hint == ble_adv_channel` | **Identify device** (presence + RSSI + sniffer hand-off) | locate, record IQ | No (broadcast metadata) |
| `band_hint == wifi_2g4` | **Identify AP** (beacon/probe presence) | locate, record IQ | No (management header) |
| `encrypted`/`identify_only`, or `ofdm_wideband`/`continuous_digital`/`unknown` | **Identify + locate + capture IQ evidence** | — | **NEVER** a content action |
| `none`/`unknown`/`feats is None` | (empty, or single identify) | — | No |

#### Honesty gates baked in
- `decode_text` is enabled **only** when `decode.get('messages')` is truthy **and** `attribution_status == 'client_space'` **and** not demo. **[corrected]** — do **not** claim `/api/analyze/signal` already gates content by attribution: today it decodes for any in-range signal and only demo mode suppresses it. The dispatcher must additionally gate on attribution and set `disabled_reason='attribute to client space first / log ECPA override'` otherwise.
- Encrypted/OFDM/unknown **never** produce a `decode_text` action. `test_dispatch.py` asserts this to catch drift if someone edits the dispatcher and `interpret()` out of sync.
- **Generic FSK caveat** **[corrected]** — `route_decode` only has POCSAG/FLEX and narrowband-2-FSK OOK; an arbitrary FSK telemetry/bug carrier returns `messages=[]`, so `decode_text` renders permanently disabled ("no protocol matched its gate"). The headline "pager/OOK/FSK → decode text" is honest only for those specific protocols; generic FSK yields identify+locate+IQ-evidence. Set the `disabled_reason` accordingly.

#### Supporting parsers (broadcast-metadata level only)
- **`survey/ble.py`** — `parse_adv_pdu(bytes)` is a pure, unit-tested byte-parser (AdvA, address type, Flags, 16-bit company ID → manufacturer, service UUIDs, local name, tx power, known-tracker table: Apple `0x004C`/Find My, Tile `0xFEED`/`0xFEEC`, Samsung `0xFD5A`, Google FMDN `0xFEAA`). `parse_advertising(iq, fs, center_hz, sniffer_pdu=None)` returns **presence + RSSI-relative + explicit sniffer hand-off** on the Pluto alone (`decoded=False`), and decoded fields **only** when an external sniffer feed supplies a raw PDU. **[corrected — BLE presence is one adv channel at a time]**: the three adv channels (2402/2426/2480 MHz) span 78 MHz; a single ≤20 MHz capture cannot contain all three. Either do **three sequential short captures** and report per-channel presence (document it as a 3-tune scan), or scope the endpoint to "presence at the tuned adv channel only" and drop any implication of cross-channel discovery.
- **`survey/wifi.py`** — `parse_mgmt_header(frame_bytes)` extracts **only** SSID (tag 0) + BSSID (addr3) from **management** frames (rejects `ftype != 0`), populated only from an external monitor-NIC/sniffer feed. Pluto path is presence + RSSI-relative only.
- **`band_hint(center_hz, feats)`** in `identify.py` is **advisory** — it never overrides `modulation_class` (2.4 GHz is a false-positive minefield: BLE/Wi-Fi/Zigbee/RC all look alike by shape). A wrong hint surfaces "presence, no PDU decoded — hand off to sniffer", never a fabricated device ID.

#### Analog video **[corrected — the biggest scope reduction]**
There is **no video demodulator** in `demod.py` and the capture rate cannot carry a video channel: analog FPV video occupies ~17–18 MHz and NTSC composite baseband is ~6 MHz, while the analyze/listen path runs at **2.4 MHz**. A 2.4 MHz window captures ~13% of the channel and cannot reconstruct a frame; even the 20 MHz sweep rate barely spans one channel and there is still no NTSC/PAL line-sync demod in the codebase. **The honest deliverable is:**
1. Confirm the **video signature** (continuous wide FM hump + line-rate comb from `spectrogram()`), which `iq_features`/`classify_modulation` already give as `modulation_class == 'analog_video'`.
2. Demod the **FM audio subcarrier** to "hear the room."
3. **RSSI-locate** the camera.

A true frame-grab (composite-video FM demod + line-sync recovery at ≥6 MHz capture) is **net-new DSP, out of scope for this design**, and would be gated behind `dispatch.enable_video` (**default false**) until validated on a real 900 MHz/1.2 GHz analog camera. If any partial raster path is later added, it must return `confidence='no_lock'`/`raster=None` (never a garbage canvas that reads as "no camera") when sync autocorrelation fails.

#### New endpoints (both reuse `monitor_target()`/`mlock`/`mstop` **with `mstop.set()` and range/busy guards** **[corrected]**)
- `POST /api/rf/metadata {center_mhz|signal_id, kind:'ble'|'wifi', sniffer_pdu?}` → presence + RSSI-relative + hand-off (Pluto) or decoded broadcast metadata (sniffer-fed).
- `POST /api/video/frame` — **[corrected scope]** returns the video-signature confirmation + audio-subcarrier availability + locate hint (not a frame), behind `dispatch.enable_video`.

> **RSSI field naming** **[corrected]** — rename all `rssi_dbm` fields to `rssi_rel_db` (or `level_delta_db`) end-to-end. The box is not absolute-calibrated; a field named `_dbm` misrepresents it. `correlation.compute_room_localization`/`compute_interior_delta` already work on relative deltas.

---

### 4d. Flow + attribution gate + evidence / chain-of-custody

#### The sign gate (net-new — this must exist before §4b/§4c can ship)
New **immutable** table mirroring `decode_overrides`:
```sql
CREATE TABLE IF NOT EXISTS session_authorizations (
  id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL,
  operator TEXT DEFAULT '', client_name TEXT DEFAULT '',
  scope_text TEXT DEFAULT '', authorized_area TEXT DEFAULT '',
  attestations_json TEXT DEFAULT '{}',
  signature_name TEXT NOT NULL, signature_kind TEXT DEFAULT 'typed',
  signature_blob TEXT DEFAULT '', signature_utc REAL NOT NULL,
  revoked_utc REAL, created_utc REAL NOT NULL);
```
Never `UPDATE` except to set `revoked_utc` (soft revoke). Helpers: `add_session_authorization(...)`, `active_authorization(session_id)`, `revoke_authorization(auth_id, session_id)`, `list_session_authorizations(session_id)`.

**Server gate** — one helper called at the three receive/evidence seams:
```python
def _require_authorization():
    s = st.session()
    if not s: return err('no session'), 400
    if not db.active_authorization(s['id']):
        return err('Sign the attribution/authorization to receive, analyze, decode or record — '
                   'passive spectrum view is available without signing.', 403)
    return None
```
Called at the **top of** `analyze_signal` (`server.py:714`), `_validate_listen_request` (`server.py:848`, guards both stream endpoints), `record_start` (`server.py:1076`), and `analyze_window` (§4b). **Passive `/api/listen_spectrum` + waterfall polling are NOT gated** so the operator can eyeball the band before committing.

`POST /api/authorization` requires **all** attestations true (`receive_only`, `authorized_area`, `client_consent`, `no_third_party_content`) **and** a non-empty `signature_name`, else 400 — the same shape as the ECPA override's `ecpa_ack`+`reason` gate.

> **`analyze_window` must fail closed** **[corrected]** — until the concrete gate predicate (`st.authorization_signed()` reading `active_authorization`) exists, the endpoint returns 403 unconditionally. It must never fall through to running captures ungated; it is the highest-fan-out decode surface.

#### Per-class disseminate suggestion (server-derived)
`analyze_signal` attaches a `disseminate` object (from `dispatch.disseminate_actions`) to its JSON, computed **after** the demo strip so demo actions reflect nulled content. The window endpoint attaches the same per-signal shape, so the card renderer is reused unchanged.

#### Evidence & chain-of-custody
Additive columns on `recordings` (via `_migrate`): `sha256 TEXT`, `custody_tag TEXT DEFAULT ''` (holds the literal `"captured under client authorization"`), `authorization_id INTEGER` (FK to `session_authorizations`), `decode_override_id INTEGER` (FK, set only when the clip backed a content decode).

`start_recording()` gains `authorization_id` + `decode_override_id`; at **finalize** (after the file is safely on disk, preserving the orphan-recovery guarantee) it computes the hash **streaming in chunks** **[corrected]** and passes all four into `add_recording`:
```python
import hashlib
h = hashlib.sha256()
with open(abs_path, 'rb') as fh:
    for chunk in iter(lambda: fh.read(1 << 20), b''):
        h.update(chunk)
digest = h.hexdigest()
```
(A max-length 120 s IQ clip at 2.4 MSPS complex64 is ~2.3 GB — a single `open().read()` would spike RAM, worse if the rate is later raised. Chunked hashing is constant-memory.)

**Report appendix.** New `DEFAULT_REPORT_OPTIONS['evidence_appendix'] = False`. `build_evidence_appendix(db, cfg, session_id, el, ss)` renders: (1) the signed authorization block; (2) chain-of-custody recordings table (freq, kind, duration, sample_rate, created_utc, `sha256[:12]`, custody_tag, linked authorization id, linked override id); (3) ECPA decode-override log; (4) per-signal attribution counts; (5) a fixed footnote that power/RSSI is **relative baseline-delta (`cal_offset_db`), not absolute-calibrated dBm**. **[corrected]** — use the **existing** stylesheet keys `ss['H1x']` (heading) and `ss['Cell']`/`ss['Body']` (footnote); there is no `ss['H2']` or `ss['Note']` (only `Cover/H1x/Mono/Cell/Body` exist), so those would `KeyError`.

> **Integrity honesty** — label the hash exactly: *"SHA-256 file integrity hash (self-attested capture, not third-party notarized)."* It proves file-vs-DB consistency, not operator identity or DB-tamper resistance.

---

## 5. Hardware feasibility matrix

Pluto: 1R1T, tuned once, ~20 MHz max IBW (**analyze/listen path configured at 2.4 MHz**), 325–3800 MHz stock / 70 MHz–6 GHz on AD9364 build, manual gain ~64 dB, **not absolute-calibrated**.

| Class | Pluto today | + Ubertooth/nRF52 sniffer | + 6 GHz-extended Pluto |
|---|---|---|---|
| **Analog voice bug** | ✅ Detect + classify + **listen** + locate + audio evidence | (n/a) | Same, plus >3.8 GHz carriers |
| **Analog video (camera)** | ⚠️ **Confirm signature + audio subcarrier + locate only** — NO frame-grab (carrier 15–24 MHz ≫ 2.4 MHz path; no video demod exists) **[corrected]** | (n/a) | Adds 5.8 GHz FPV band; still needs net-new video DSP + ≥6 MHz capture for any frame |
| **Pager POCSAG/FLEX** | ✅ Decode text (needs `pocsag_seconds≈3.0` capture; ECPA-gated on client-space) | (n/a) | Same |
| **OOK / narrowband 2-FSK** | ✅ Decode text when gate met | (n/a) | Same |
| **Generic FSK telemetry** | ⚠️ Identify + locate + IQ-evidence only (no matching decoder) **[corrected]** | Protocol sniffer may decode | Same |
| **BLE (adv channels)** | ⚠️ **Presence + RSSI-rel + duty at one adv channel per tune** (3-tune scan for all three); **NO device/tracker/company-ID** | ✅ AdvA + company ID + service UUID + tracker via sniffer PDU into `parse_adv_pdu` | Same as Pluto today |
| **BLE data-channel / classic BT FHSS** | ❌ Infeasible (37-ch @ ~1600 hops/s; 79-ch FHSS) — **recommend, never do** | ✅ Correct tool for hop-follow | ❌ Still infeasible |
| **Wi-Fi 2.4 GHz** | ⚠️ Presence + RSSI-rel only | ✅ SSID/BSSID via monitor-NIC/sniffer header parse | Adds 5 GHz presence |
| **OFDM / encrypted wideband** | ✅ Identify + locate + IQ evidence — **NEVER content** | (n/a) | Wider capture improves BW estimate |
| **<70 MHz / >6 GHz / optical / acoustic** | ❌ Unreachable by any SDR | ❌ | ❌ (>6 GHz still out) |

**Auto-width trust bound** **[corrected]**: occupied-BW and auto-width are trustworthy only for signals **narrower than the ~2.4 MHz capture rate**. Wider signals (Wi-Fi, OFDM, video) are reported as "exceeds capture window (>~2.4 MHz)", not a bogus finite width. Raising `listen/stream/analyze_sample_rate` toward ~20 MHz for a wideband analyze pass is the mitigation (memory/USB2 permitting).

---

## 6. Legal / scope boundary (how the code enforces it)

The content-interception line sits **exactly** where `decode_router.interpret()` draws it, enforced in code (not just docs):

**IN SCOPE — the code routes:**
- Demod/decode of client-authorized devices: POCSAG/FLEX/OOK/DTMF/CW text, each behind its own honesty gate, only when `readable ∈ {decoded, maybe_decode}`, **and** (dispatcher) `attribution_status == 'client_space'`, **and** demo strips it, **and** third-party decodes still require a logged ECPA override.
- BLE **broadcast advertising** metadata (AdvA, address type, Flags, company ID, service UUIDs, local name, tx power, known-tracker detection) — openly broadcast, one-way, metadata not content.
- Wi-Fi beacon/probe **management-frame** SSID/BSSID presence — header fields only.
- RSSI-to-locate (relative, on the client's premises) via `compute_room_localization`/`compute_interior_delta`.
- Analog-video **signature confirm + locate + audio subcarrier**.
- IQ/audio captured as timestamped evidence, hashed, tagged "captured under client authorization", linked to the signing authorization (+ any override).

**OUT OF SCOPE — structurally unreachable:**
- Decoding a third party's two-way communication content → `encrypted`/`identify_only` yields identify+locate+evidence, **never** a content action; the dispatcher has no code path to produce one.
- Following/deanonymizing a specific person's device → BLE parser reads only broadcast adv (never the 37-channel data hops, infeasible anyway); Wi-Fi never touches data frames; "locate" is room-level RSSI, not device tracking.

**Enforcement points:**
- **Sign gate** (`_require_authorization`, 403) blocks receive/analyze/decode/record until signed.
- **ECPA override** (`/api/decode/override`, `ecpa_ack`+`reason` else 400) remains the chokepoint for any non-client-space content decode; the disseminate `decode_text` action routes through it client-side (`runDisseminate → ecpaOverride`).
- **Demo strip** nulls decode and re-derives `interpret()` without a decoded claim.
- **`DEMO_BLOCKED`** — **[corrected]** it is a static `request.path.startswith(...)` tuple. `/api/authorization` does **not** prefix-match the existing `/api/attribution` entry, so **add the literal `"/api/authorization"`** (covers `GET`/`POST` and `/api/authorization/revoke` since all share the prefix). For `/api/rf/metadata` when `sniffer_ingest` is on, make blocking **conditional inside `_demo_guard`** (or null third-party `local_name`/`SSID`/`BSSID` in the endpoint when `st.demo`), since a static tuple entry would also kill the safe presence-only path.
- **Calibration honesty** — every evidence artifact labels power/RSSI relative baseline-delta, never absolute dBm.

---

## 7. Data model, endpoints & config (consolidated)

### DB touch-points (`survey/db.py`)
- **NEW** `session_authorizations` (immutable, soft-revoke). Helpers: `add_session_authorization`, `active_authorization`, `revoke_authorization`, `list_session_authorizations`.
- **ALTER** `recordings` (additive `_migrate`): `sha256`, `custody_tag`, `authorization_id` (FK), `decode_override_id` (FK).
- `add_recording(...)` gains keyword params `sha256=None, custody_tag='', authorization_id=None, decode_override_id=None` (all defaulted — backward-compatible).
- **Unchanged:** `signals.attribution_status`, `decode_overrides` (immutable), `reports`.

### Endpoints

| Method | Path | Status | Purpose |
|---|---|---|---|
| POST | `/api/authorization` | **NEW** | Sign per-session attestation (all 4 affirmations + signature else 400). Returns immutable row. |
| GET | `/api/authorization` | **NEW** | Current active (non-revoked) authorization or `{}`. |
| POST | `/api/authorization/revoke` | **NEW** | Soft-revoke (`revoked_utc`); never deletes. |
| POST | `/api/analyze/window` | **NEW** | Sign-gated NDJSON stream: detect peaks → per-peak fusion → per-signal cards + `chan_bw_hz` + `disseminate`. |
| POST | `/api/rf/metadata` | **NEW** | BLE/Wi-Fi broadcast-metadata parse; presence+hand-off (Pluto) or decoded (sniffer-fed). |
| POST | `/api/video/frame` | **NEW** | Video-signature confirm + audio-subcarrier + locate (**not** a frame); behind `dispatch.enable_video`. |
| POST | `/api/listen/gain` | **NEW** | Live RX gain staging `{mode, gain_db?}`; warns RSSI no longer matches baseline. `DEMO_BLOCKED`. |
| POST | `/api/listen/squelch` | **NEW** | Live SNR squelch `{snr_db\|null}`, read per-block like offset. `DEMO_BLOCKED`. |
| POST | `/api/analyze/signal` | **EXTENDED** | + `_require_authorization()` gate; + `disseminate` object; + `actions` list. |
| GET | `/api/listen_pcm`, `/api/listen` | **EXTENDED** | `_require_authorization()` via `_validate_listen_request`. |
| POST | `/api/record/start` | **EXTENDED** | Gate + thread `authorization_id`/`decode_override_id`/custody flag into `start_recording`. |
| GET | `/api/listen_spectrum` | **EXTENDED** | + `demod` object (SNR/gated/agc/rx-gain) for the front-end diagnostic. Data source for peak detection. |
| POST | `/api/listen/bandwidth`, `/api/listen/offset` | **REUSED** | Auto-width sink + tune sink (with the far-peak re-tune correction). |
| POST | `/api/decode/override` | **UNCHANGED** | ECPA chokepoint; override id linked onto the backing recording. |

### Config (`config.yaml`)

```yaml
pluto:
  # front-end (§4a)
  listen_squelch_snr_db: null          # OFF; set ~8 to mute empty channels
  listen_gain_mode_default: manual
  # window classify (§4b)
  window_max_signals: 12
  window_threshold_db: 12.0            # baseline-delta over live floor
  window_min_run_bins: 1
  window_analyze_seconds: 1.5          # analog/identify; data modes use pocsag_seconds ~3.0
  # dissemination (§4c)
  video_ibw_mhz: 18.0
  video_seconds: 0.2                   # [corrected] bounded — was 1.0 (~144 MB single buffer)
  ble_seconds: 1.0
demod:
  deemphasis_us: 75.0                  # 50 in ITU-R region 1
  agc_target_rms: 0.18
  agc_attack_ms: 8.0
  agc_release_ms: 300.0
  agc_max_gain: 40.0
  squelch_hang_blocks: 3
dispatch:
  enable_video: false                  # net-new DSP; off until hardware-validated
  sniffer_ingest: false                # accept external BLE/Wi-Fi PDU feed
authorization:
  required_attestations: [receive_only, authorized_area, client_consent, no_third_party_content]
report_options:
  evidence_appendix: false
```
(Existing keys reused: `pluto.cal_offset_db`, `recordings.max_seconds`, `pluto.dc_guard_hz`, `pluto.listen_sample_rate`, `pluto.stream_sample_rate`, `pluto.analyze_seconds`, `branding.company/operator`.)

### New files
`survey/dispatch.py`, `survey/ble.py`, `survey/wifi.py`, `tests/test_dispatch.py`. (Extend `tests/test_demod.py`, `tests/test_attribution.py`.)

---

## 8. Phased implementation plan (each phase independently shippable)

**P1 — Demod front-end strengthening (§4a).** Highest felt-quality win, no legal surface, no new endpoints beyond gain/squelch. Add `DemodState`, squelch (default OFF), RMS AGC, de-emphasis (post-decimation, vectorized), FIR-decimate guarded on `dec2>1`, `set_gain` on `PlutoSource`, the `demod` telemetry object + client diagnostic. Tests: squelch mutes noise, AGC converges, de-emphasis rolloff, FIR beats boxcar. *Ships alone; nothing below depends on it.*

**P2 — Sign gate + evidence chain (§4d).** The hard prerequisite for P3/P4. Add `session_authorizations` + gate helper + 3 endpoints; add `DEMO_BLOCKED` entry; add recordings hash/tag/FK columns + chunked hashing; add the evidence appendix (`ss['H1x']`/`ss['Cell']`). Extend `test_attribution.py` (gate blocks-then-allows, custody stamp, soft revoke, demo 403). *Ships alone; passive view still works.*

**P3 — Auto-classify + auto-width (§4b).** Requires P2's gate. Add `detect_peaks_in_psd` (DC-guard masking) in `survey/sweep/detector.py`; factor out `_merge_runs`; add `/api/analyze/window` NDJSON; client `analyzeWindow` + card renderer + auto-width wire + far-peak re-tune. *Ships once P2 lands.*

**P4 — Per-class dissemination (§4c).** Requires P2 (gate) and benefits from P3 (cards). Add `dispatch.py` + `ble.py` + `wifi.py` + `band_hint`; `/api/rf/metadata`; `disseminate` on `analyze_signal`; client action row. Ship BLE/Wi-Fi as **presence + hand-off** and analog-video as **signature-confirm** with `enable_video=false`. Tests: no content action for encrypted/unknown; BLE never fabricates identity; demo strips content actions; `parse_adv_pdu` tracker fixtures; `parse_mgmt_header` rejects data frames.

**P5 — Advanced / add-on (post-validation).** Sniffer ingest path (`sniffer_ingest=true` + conditional demo-block/nulling); 3-tune BLE adv scan; optional wideband (20 MHz) analyze pass for accurate wide-signal BW; net-new analog-video frame-grab DSP behind `enable_video` after validation on a known FM-video transmitter.

---

## 9. Risks & open decisions (with recommended defaults)

### Risks
| Risk | Mitigation |
|---|---|
| Squelch mutes a real weak bug | Default **OFF**; conservative default + hang when on; diagnostic shows *why* it went silent; **never** gates the IQ/evidence path (assert in a test). |
| Live gain change breaks the `cal_offset_db` baseline | Endpoint + diagnostic explicitly warn "RSSI no longer matches baseline until re-swept"; don't feed post-change RSSI into locate; manual fixed gain stays default. |
| DC/LO spike → phantom peak every run | Mask ±`dc_guard_hz` and drop center-guard runs before `detect_peaks_in_psd` **[corrected — was unaddressed]**. |
| "Tune + set width" no-ops after batch preempts the stream | Per-card action restarts `/api/listen_pcm` at the center, then applies width **[corrected]**. |
| Far peaks unreachable by offset | Re-tune (new stream) for peaks outside ±`fs·0.48` **[corrected]**. |
| Sequential batch pauses audio (12 × ~2 s) | Cap `window_max_signals`; strongest-first; stream cards progressively; show `k/N` pill; document the pause. |
| 2.4 GHz false positives | `band_hint` advisory only; BLE/Wi-Fi actions return presence+hand-off, never a fabricated ID; tests assert. |
| Analog-video over-promise | Scoped to signature-confirm+locate+audio; `enable_video=false`; bounded `video_seconds=0.2` **[corrected]**. |
| Dispatcher/`interpret()` drift re-adds a content action | Dispatcher consumes `interpret()` output; `test_dispatch.py` asserts no content action for encrypted/identify_only/ofdm/continuous/unknown. |
| Evidence over-claims integrity | Label "self-attested SHA-256, not notarized"; chunked hashing; never-delete/orphan policy intact. |

### Open decisions (recommended defaults — operator answers pending)
1. **Squelch default ON or OFF?** → **OFF** in config (missing a weak signal is the worst outcome for a sweep tool); UI may pre-select a mild value on first manual entry.
2. **De-emphasis NFM + WFM, 50 vs 75 µs?** → **Both**, default **75 µs** (`demod.deemphasis_us`), configurable.
3. **Peak source: live frame vs fresh wideband capture?** → **Reuse the live frame** (zero extra latency, matches what the operator sees); rely on per-signal fresh `capture_iq` for precise BW; add a fresh-wideband path only if operators report merged peaks.
4. **Progress transport?** → **NDJSON over `fetch` ReadableStream** (no job registry; werkzeug dev server handles a bounded ≤12-item stream).
5. **Auto-apply width to strongest signal, or per-card click?** → **Per-card "Tune + set width" click**, but auto-set width for the signal **already under the demod marker** if it matches a classified peak (satisfies the goal for the active signal without hijacking tuning).
6. **Session-level vs per-action authorization?** → **Session-level umbrella** signature (gates all receive/analyze/record) **while preserving the per-decode ECPA override** for any non-client-space content. Matches how `attribution_status` already layers under a broader lawful basis; avoids prompt-fatigue.
7. **Typed vs drawn signature?** → **Typed required, drawn optional** (`signature_kind` + optional `signature_blob`); keeps the DB lean.
8. **Revoke quarantines prior evidence?** → **No.** Soft revoke re-engages the gate for **new** captures only; evidence captured under a then-valid signature stays linked to its (never-deleted) authorization row. Purge remains the separate MOVE-not-delete action.
9. **Evidence appendix on by default?** → **Off** (opt-in like other report options); surface a UI nudge when recordings or overrides exist.
10. **Ship analog-video enabled?** → **No** (`dispatch.enable_video=false`); descriptor renders disabled ("video demod pending hardware validation") until a real analog-camera capture confirms the path.

---

*Receive-only. Client-authorized. Sign-gated. Evidence-grade, hashed, and honestly bounded to what a single Pluto can actually do.*
