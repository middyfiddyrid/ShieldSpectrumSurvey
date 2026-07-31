# DEMOD + LISTEN — Signal Identification Layer for Shield TSCM / DCS

**Goal:** After spectrum detection and wiki fingerprinting, be able to quickly tune to a suspect frequency, demodulate, and *listen* (or decode) so we can positively identify emitters as:
- Analog audio bug (voice, room audio)
- Digital data / telemetry / command
- Video subcarrier or other
- Legit service (confirm by ear or decode)
- Noise / spurious

Current state (spectrum_survey + RF Explorer 6G Combo PLUS):
- Excellent calibrated power + shape fingerprinting (AVG + MaxHold narrow-span SVGs in the Signal Wiki).
- 136+ reference captures in demo library across Unknown, Broadcast, Cellular, ISM/WiFi/BT, DECT, etc.
- **No audio / demod path.** RF Explorer device layer + sigcapture is amplitude-only. We flag "unverified" and "narrowband NFM class" in descriptions but cannot confirm by ear.

User reality:
- RF Explorer + directional + omni antennas.
- Last job: lugged entire tower PC. Awful.
- Wants tablet-in-field eventually.

## Recommended Stack (Cheapest → Best)

### Cheapest Immediate (add-on, <$60, works today)
- **RTL-SDR Blog V4** (~$35-45): SMA, TCXO, bias-T, 500 kHz – 1.766 GHz (covers vast majority of classic analog/digital bugs: 300-900 MHz, VHF/UHF, many ISM).
- Adapters: SMA male pigtails / barrel to reuse existing directional + omni (RF Explorer is SMA female).
- **Software (free):** SDR# (SDRSharp) — best Windows UI, AM/NFM/WFM/SSB/CW, recording, frequency manager, plugins. Tune from wiki center freq, switch modes by ear.
- Optional cheap: powered USB hub (~$15), closed-back headphones or earbuds.
- **Run on:** existing Windows laptop or buy any cheap used Windows 2-in-1 / tablet.
- Limitation: coverage drops above ~1.7 GHz (2.4G video bugs etc. may need aliasing or separate tools). Very low kHz-MHz signals (some wiki examples ~150-300 kHz) need note or HF upconverter (~$40-60 extra) or soundcard VLF.

**Why cheapest/best starter:** Lowest cost, huge community, works with existing antennas + PC. You can start identifying signals on the next sweep.

### Balanced / Recommended Value (~$250-380)
- **Primary receiver add:** SDRplay RSP1B or RSPdx (~$150-300). Better dynamic range, filtering, and sensitivity than RTL; less overload from strong broadcast. Excellent SDRuno software (Windows native).
- Or **HackRF One** (~$330): full 1 MHz – 6 GHz match to the RF Explorer range. IQ full-rate, GNU Radio native, can do wide captures. Add-on PortaPack (~$120-200) turns it into a battery-powered handheld with screen + audio jack — no computer required for basic listen/analyze on site.
- Keep RF Explorer as the *primary calibrated spectrum tool* (accurate dBm, dual-module planning, proven in the app). Use SDR/HackRF as the "tune-and-listen probe" for anomalies.

### Premium / Pro Field
- Dedicated TSCM receivers (REI etc.) cost thousands and are overkill for most work. Stick with calibrated analyzer + flexible SDR.

### Tablet / True Field-Portable Compute
**Goal:** no tower, minimal bag.

**Option A — Best compatibility (recommended first buy):**
- Windows 11 2-in-1 or detachable tablet (Surface Pro / Go used/refurb, or budget brands ~$300-700 with 8+ GB RAM, USB-C with video/power delivery).
- Runs *everything* natively: the Flask web UI (point Edge/Chrome to http://127.0.0.1:8765), SDR#, URH (Universal Radio Hacker for digital protocol reverse), Python tools, full reports.
- USB hub for RF Explorer (COM) + SDR dongle.
- Battery life: 6-10 hrs typical with power management.

**Option B — Lightweight / cheaper custom (~$250-400 all-in):**
- Raspberry Pi 5 (8 GB) + official 7" or third-party 8-10" touch DSI/HDMI display.
- SDR++ (modern cross-platform SDR app — excellent waterfalls, demod, recording, supports RTL/HackRF/Airspy).
- Or GQRX.
- Run the TSCM app locally (Flask) + browser on the Pi desktop/touch UI. Or make the UI more PWA-like.
- Power: PD power bank + Pi power management. 3D-print or buy small case.
- Pros: silent, low power, hackable GPIO for future relay antenna switch.
- Cons: ARM quirks for some Windows-only SDR tools (workarounds exist).

**Android fallback (quick & dirty):** High-end tablet + USB-C OTG + RTL dongle + "SDR Touch" app. Good for fast listen. Weaker for recording/IQ export/advanced analysis. Use as secondary probe.

**Key portability win:** The existing spectrum_survey UI is already a local web app. Tablet browser + localhost is 90% of the experience. Add a "Demod" panel and it becomes one device.

## System Design — Block Schematic

```
ANTENNAS (user's existing)
├── Omni (wide coverage)
└── Directional (Yagi / log periodic / parabolic — for bearing)

          SMA cables / quick disconnects
                   │
          ┌────────┴────────┐
          │  Manual SMA     │   (cheap: 2-port coax switch ~$20-40 or just swap pigtails)
          │  Switch or T    │
          └────────┬────────┘
                   │
        ┌──────────┴──────────┐
        ▼                     ▼
[RF Explorer 6G Combo PLUS]   [SDR Probe: RTL-SDR V4 or HackRF One]
   (LEFT 50k-960M + RIGHT     (full IQ stream → demod)
    240M-6.1G verified)       
        │ USB-UART (CP210x)    │ USB (high speed recommended)
        │                      │
        ▼                      ▼
   [Powered USB Hub or direct]◄──────────────────────┐
        │                                            │
        └────────────────── USB ─────────────────────┘
                           │
                           ▼
              [FIELD TABLET / LAPTOP]
              - Runs spectrum_survey (app.py -- local web @8765)
              - Browser UI for sweep + wiki + new Demod tab
              - SDR# / SDRuno / SDR++ side-by-side or integrated
              - Audio output → closed headphones / earbuds
              - Optional: URH for post-capture digital analysis
              - Power: built-in battery + external PD bank

AUDIO PATH (critical for ID)
  SDR demod (AM/NFM/SSB/WFM/raw) → system sound → headphones
  Record button → WAV (timestamped + freq + session link) saved next to capture

POWER (field must-have)
  Tablet/PD bank ──USB-PD──> Hub ──> SDR + (Explorer if needed)
  Separate small power for long days.
```

**Wiring notes:**
- Prefer shortest high-quality SMA cables (loss matters at GHz).
- Use a cheap manual coax switch or labeled pigtails ("Sweep" vs "Listen").
- For simultaneous: resistive splitter (3 dB loss each) or active multicoupler later.
- Bias-T on RTL V4 can power an optional remote LNA/preamp on long cable runs (rarely needed inside building).
- Always use ferrite chokes on USB cables near computer to reduce computer noise.

**Alternative simple layout (no switch, minimal new parts):**
Two short pigtails. When you lock an anomaly in the UI:
1. Note freq + suggested mode from BW/duty.
2. Physically move the directional (or omni) SMA to the SDR dongle.
3. Tune in SDR#.
4. Walk/rotate for max, listen, record clip.
5. Move SMA back for next spectrum check.

## Software Architecture & Integration

### Phase 1 — Zero-code immediate (use today)
- In wiki / results list: each unverified signal shows center freq + "Copy freq" + "Suggested mode" (derived from existing bw_khz / rule_based_description).
- Operator opens SDR#, pastes freq, chooses mode by ear, records.
- Manually save audio clip filename with session/freq and attach to report later.

### Phase 2 — In-app "Listen" (recommended)
Extend `spectrum_survey/`:

New files / modules:
- `survey/demod.py` — thin wrapper.
  - Detect available SDRs (pyrtlsdr for RTL, or subprocess hackrf / Soapy).
  - `tune_and_stream(freq_mhz, mode, gain=..., squelch=...)` → generator of PCM frames or WAV writer.
  - Support modes: AM, NFM, WFM, LSB, USB, CW (narrow filters for voice bugs).
- Web:
  - New /demod or tab in UI.
  - From capture row: "Tune & Listen" autofills freq + recommended mode.
  - Controls in browser: freq fine-tune, mode buttons, gain, squelch, volume.
  - Waterfall mini (canvas + WS or use existing live sweep tech).
  - Big RECORD AUDIO → saves `audio_<session>_<freq>_<ts>.wav` + DB row linked to capture (new `audio_clips` table or extend `captures`).
  - "Export for URH" button → complex IQ float32 or signed16 WAV.
- Backend: WebSocket for live audio chunks (browser plays via Web Audio) or simple server-side file recording only (lower latency risk).
- For simplicity first: server records the file; browser just shows controls + "play last recording". Or pipe to local sound and let tablet play.

Auto-suggest mode (reuse existing logic):
- bw < 25 kHz → NFM (classic voice bug) or SSB if very narrow.
- Wider continuous → AM or WFM.
- Bursty → listen raw or look for subcarrier.

Link everything: clicking a wiki entry in the app can offer "Recapture + Listen" workflow (zoom spectrum capture + simultaneous demod).

### Phase 3 — Advanced ID
- Integrate lightweight DSP or call out to URH/inspectrum for burst analysis.
- Optional: on recorded audio, local Whisper transcription (if voice) — mark as "room audio captured" with caution (state wiretap laws).
- Category promotion in wiki: "Unknown" → "Analog audio bug (voice confirmed)" with clip attached.

### External tool bridge (easy win)
Add in UI: "Open in SDR#" button that writes a temp .xml or uses COM/CLI to preset freq/mode in SDR# (SDR# supports some command-line / frequency manager import).

## Field Tablet Kit — Physical Layout

**Target form factor:** Fits in a medium messenger bag or small Pelican-style case. One person carry. Setup < 5 min.

**Core kit (minimal):**
- Tablet / 2-in-1 or Pi+touch
- RF Explorer 6G (in padded sleeve or original box)
- RTL-SDR V4 or HackRF (small static bag)
- 2x SMA pigtails (6-12")
- Coax switch or Y (optional)
- Existing directional + omni (breakdown or rolled)
- Short USB cable + small powered hub (Anker or similar, 4+ ports, PD pass-through)
- Closed headphones or quality earbuds + extension
- 20000 mAh+ PD power bank (runs everything for a full day)
- USB-C multiport if tablet
- Small notepad + Sharpie + tape (for temp labels)
- Optional: small LNA, attenuator kit, spare batteries, flashlight

**Case organization sketch (top view inside case):**
```
[ TABLET / SCREEN ]          [ RF EXPLORER + cables coiled ]
          |                            |
[ SDR dongle + pigtail pouch ]   [ Antenna 1 folded | Antenna 2 ]
          |
[ Hub + power bank velcroed to lid ]
[ Headphones in net pocket ]
[ Notepad + pens slot ]
```

Label ports: "SWEEP", "LISTEN". Color-code cables.

**Power discipline:** One main PD bank → hub → devices. Tablet can charge from same or separate. Monitor RF Explorer battery separately.

**Tablet UI tweaks for field (future):**
- Larger touch targets.
- Dark theme already good.
- "Field mode" toggle: larger fonts, simplified left rail, one-tap "Next room + listen candidate".
- Install as PWA or use Edge kiosk mode.
- Offline first (already mostly is, with cached baseline).

## Legal / Operational Notes (preserve existing guardrails)
- Receive-only. Never transmit.
- Audio recording of client spaces: follow local one-party / all-party consent. Flag in reports ("audio captured under client authorization").
- Evidence handling: timestamped files, hash if needed for chain, store with sweeps.db / reports/.
- Do not advertise "guaranteed bug detection" — this augments spectrum + physical + NLJD.

## Phased Implementation Roadmap

1. **Day 0 (today):** Buy RTL-SDR V4. Install SDR#. On next gig use alongside RF Explorer. Manually tune from wiki freqs. Bring a small Windows laptop or test a tablet.
2. **Week 1:** Document common modes per band in a simple table (add to memory/ or glossary). Add "copy freq + suggested mode" buttons/links in the existing wiki UI and results (small code change in report_html or web/app.js + server).
3. **Month 1:** Implement basic demod.py + web panel for RTL (start with file record only). Test on known sources (keyfob, walkie, your own test bug).
4. **Portable purchase:** Acquire dedicated Windows tablet or Pi kit. Test full sweep + demod on a real site with only the portable rig.
5. **Polish:** Audio clips in DB + report appendix. Integration with "directional follow-up" workflow. Optional HackRF support.
6. **Advanced:** Pi standalone mode, PortaPack for ultra-minimal listen-only probe, digital decode helpers.

## BOM (approximate 2026 prices — shop Amazon / RTL-SDR site / Great Scott Gadgets / Nooelec)
- RTL-SDR Blog V4: $40
- SMA adapters / 2-3 pigtails: $10-15
- Powered USB-C hub (good one): $15-25
- Headphones (decent closed): $20-50 (you may already have)
- Windows tablet / 2-in-1 (used/refurb capable): $350-650
- (Alt) Raspberry Pi 5 8GB + 8" touch kit + case/power: ~$250-350
- HackRF One (later upgrade): ~$330
- Coax switch (optional nice-to-have): $25
- Pelican / equivalent case + foam or velcro organization: $30-80

**Total starter portable add:** ~$100-150 (RTL + cables + hub + power) on top of existing antennas + a $400 tablet/laptop = dramatic improvement over tower.

## Next Actions for Matt
- Order RTL-SDR Blog V4 + a couple SMA pigtails today.
- Install SDR# + Zadig driver (follow rtl-sdr.com quickstart).
- Pick a test signal from recent wiki (e.g. one of the ~300 kHz or a 400-900 MHz unverified) and practice on a known transmitter (garage door, wireless mic, etc.).
- Decide tablet vs laptop vs Pi path (I recommend starting with a solid used Windows 2-in-1 so everything "just works" including the current Python app).
- Tell me when ready and we'll wire the first "Tune from wiki" button + basic audio record into the app.

This turns the current "we see a signal shape" capability into "we see it, we listen, we confirm what it is carrying." That is the missing identification link.

Receive-only. Client-authorized. Evidence-grade clips.

Let's build the listen layer.