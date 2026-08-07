# Session handoff — 2026-08-06 (branch `sweep-stability-window4`)

Pull this branch on the desktop:

```bash
git fetch origin && git checkout sweep-stability-window4
```

Backend changed, so **restart the app on 8765**, then **Ctrl+F5** the browser.

---

## 1. The "wouldn't let me close the sweep" incident — what actually happened

Not what it looked like, and **my first diagnosis was wrong**; the corrected version:

- 20:23:57 — the B220 worker died mid-segment-23. The runner auto-reconnected.
- ~20:24:00 — CONNECT was pressed ~2 s later. **Two workers raced for the USB radio.**
  The runner's own reconnect lost, with `uhd_error 11` — whose text ("no OTHER
  process using it?") points at a stranger when the competitor was this app's own
  sibling worker. That error killed sweep 209 (status=error, **data kept**).
- 20:24:44 — 1172 observations archived.
- 20:24:44 → **20:35:35** — the finalize thread sat in the novelty pass for
  **eleven minutes** with `sweep.running` still True. ABORT was a no-op and every
  new sweep 409'd for that whole window. It cleared itself at 20:35:35.

**No restart was ever needed** — there was just no way to know you had to wait.
(I initially reported the thread had died and only a restart would clear it. A
`py-spy dump` showed only MainThread while that thread was demonstrably alive and
working. A py-spy dump missing a thread is not proof the thread is gone.)

## 2. What was fixed

| Fix | Effect |
|---|---|
| **Novelty query** — `ABS(center-?)` → `BETWEEN` | non-sargable → `idx_captures_freq` now used. **183.9 s → 0.25 s (722x)**, measured on a copy of the real DB |
| **Novelty writes** — 1483 `execute()` → one `executemany` | `db.execute()` opens a connection and commits per call; that was the other half of the 11 minutes |
| **`running=False` moved into a `finally`** + BaseException guard | a genuinely dead finalize can no longer strand the flag; whatever killed it gets logged |
| **`finalizing` published in `/api/state`** | the UI can show SAVING instead of SWEEPING |
| **`abort()` returns what it did** and clears a stale flag | a phantom sweep is now **one click**, not a restart. It also refuses to promise an erase it cannot perform |
| **Single-flight connect lock per radio** | CONNECT joins an in-flight `_reconnect` instead of racing it → no more self-inflicted `uhd_error 11` |

**Still open and NOT a code problem:** the B220 has been dropping off USB several
times a day since 8/4 (`uhd_error 40` stream faults, silent worker deaths,
`uhd_error 11` open bursts). The Pluto has been solid throughout. Worth a replug,
a different USB3 port, and a different cable.

## 3. New: TV channel check

A **CHECK** action on every TV row in the baseline table. Measures whether the
licensed station is actually radiating: the **ATSC 8VSB pilot** at +309.440 kHz
above the lower channel edge, under a flat 5.381 MHz data plateau
(`survey/atsc.py`). A pilot with no plateau, or a plateau with no pilot, is
reported as exactly that.

Why it earns its place: a 6 MHz block you have already filed as "that's just
channel 34" is the most comfortable place in the spectrum for something else to
sit.

**It does not decode digital pictures** — 8VSB video needs an
equalizer/trellis/RS/MPEG chain that will not run in numpy, and the panel says so
rather than implying one is coming. `survey/video.py` still recovers a still from
**analog** rasters, and the TV panel shows it when the carrier turns out to be one.

Detector margins: **36 dB** on a real pilot vs **1–2 dB** on impostors.

## 4. New: WINDOW 4 — auto-demod

Button in the header. A **docked pane, not a popup** — it keeps working across
views. It walks **every detection in the session** on the monitor radio,
continuously, so each row arrives carrying a measured answer: modulation class,
occupied bandwidth, symbol rate, SNR, signature-ID candidates, the `interpret()`
verdict, and playable audio. Results persist onto the signal, so the ledger and
report show them without re-running anything.

It is a guest on the radio: monitor only (never the sweeper), registers so
`busy()` sees it, and stands down the moment you take the radio for a manual
LISTEN/ANALYZE/DF/record.

**Content gate.** Characterization is content-free and runs for everything. Audio
and payload decode are interception of CONTENT, so they run only for signals
attributed to **client space** — or after a written acknowledgment covering the
run, which writes a row to the existing `decode_overrides` audit table. The pane
shows exactly how many signals are being withheld. There is deliberately no
silent mode where this records everyone's conversations because it was left on.

**Honest limit:** one radio tunes one frequency at a time. It round-robins — a
300-signal ledger comes round roughly every ten minutes — so every row shows the
age of its own result.

## 5. New: layout presets

Next to STYLE in the footer: **STD / COMPACT / FIELD / WIDE**. Changes type size,
spacing, nav width, chart height and table viewport together. STD is byte-identical
to what shipped. Independent of the colour theme — any layout combines with any theme.

Layout lives on `<body>` with its own storage key, because the anti-FOUC restore
at the top of `index.html` assigns `documentElement.className` wholesale and would
wipe a layout class parked there.

## 6. Hidden cameras — what this app can and cannot recover

"If it can't decode a TV station, how does it decode a camera?" — they are not the
same problem, and the difficulty runs the opposite way to intuition.

| Camera type | Picture? | Why |
|---|---|---|
| **Analog video sender** (cheap covert cams, FPV, "wireless video sender") | **YES — recovered still** | FM composite video: discriminate, slice sync, read the raster. `survey/video.py`. No error correction, no equalizer, no transport stream |
| **ATSC broadcast TV** | no | trellis + Reed-Solomon + adaptive equalizer + MPEG transport demux — the hardest video chain in the band. Also not a bug: it is a licensed transmitter on a tower |
| **WiFi IP camera** (most common modern covert cam) | no, and never | the video is H.264 inside WPA2/WPA3. That is **cryptography, not DSP** — no radio recovers it. Found via NETWORK CHECK + RF correlation + LOCATE/DF. If it is on the client's own network you can often just watch it from the network side |
| **Digital FPV / proprietary** (OcuSync, HDZero, Walksnail) | no | proprietary, usually encrypted, no public spec. DF it |

**Two real bugs this question exposed, both now fixed:**

1. **Auto-demod captured at a fixed 5 MHz.** A wideband video sender overflows
   that, its sync tips alias, and a camera reads as ordinary wideband FM. Capture
   width is now chosen per signal from the width the sweep measured. Measured on a
   synthetic NTSC sender at 1 MHz deviation: **20 MHz capture recovers a picture,
   5 MHz capture does not.**

2. **`looks_like_video()` was rejecting real cameras — this was pre-existing, in
   your analyze path, not new code.** The gate requires `occupied_bw_khz >= 1000`,
   but `iq_features` badly under-reads an FM carrier: it is constant-envelope and
   instantaneously narrow, so over a short slice it has not yet swept its
   deviation. Measured on a signal the decoder **does** lock and return a picture
   from: **78 kHz** over 0.41 ms, **117 kHz** over 1.6 ms, **723 kHz** over 6.6 ms
   — all below the 1 MHz gate. Result: `route_decode` never tried video, and
   `/api/analyze/signal` reported that camera as an **"OOK/FSK-DEVICE"**.

   `looks_like_video` now takes an optional `bw_khz_hint` — pass the sweep's
   max-hold width, which does not have this problem. **Additive and non-breaking:
   callers that pass no hint behave exactly as before**, so your existing analyze
   path is unchanged until you decide to pass the hint there too. That is your
   call, not mine — flagging it rather than changing a shared gate you verify by
   hand.

WINDOW 4 now surfaces a confirmed raster as a red row with the recovered still
inline, sorted to the top of the ledger. The raster confirmation is content-free
and always reported (a camera must never go unreported because attribution has not
been set); only the **picture** is behind the content gate.

**The caveat that ships with every positive:** a sync lock is evidence of a video
raster, **not proof of a camera**. A monitor emanation, a video sender relaying a
TV, or a bench test pattern all produce the same signature. Find the device and
recover it physically before calling it.

## 7. What you still owe (real-hardware verify)

1. **Restart 8765 + Ctrl+F5.**
2. **TV check against a real station** — pick a strong local one in the baseline,
   hit CHECK, confirm it says "ATSC 8VSB confirmed on air". This is the first real
   test of the pilot detector against live RF; it has only ever seen synthetic input.
2b. **Camera recovery with your test transmitter** — put an analog video sender on
   the bench and confirm WINDOW 4 flags it as a VIDEO RASTER and pulls a still. The
   whole video path has only ever been proven against `encode_video`'s synthetic
   raster; the recovered frame in testing showed a ~0.12 % line-rate error (a
   visible diagonal skew), which is legible but wants a real-signal check.
3. **WINDOW 4 with the Pluto** — confirm it cycles, that audio is real, and that it
   yields the radio instantly when you hit LISTEN.
4. **Confirm the abort self-heal** next time a sweep gets stuck — one click should
   now clear it.

Tests: 1183 → 1208 (`python -m unittest discover -s tests`, ~63 min).
