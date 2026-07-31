# Signal Identification Guide — Shield TSCM / DCS

**Goal:** Turn a detected emission (spectrum fingerprint in the wiki) into a confident ID: "This is normal client phone traffic" vs "This is a hidden analog bug" vs "This is a WiFi camera" vs "unknown threat".

The app already does heavy lifting via baseline + correlation. The new demod/listen layer (RTL-SDR / HackRF + SDR# or in-app) + field tests close the loop.

## 1. Cell Phone Uplinks — Special Case

### What they are
Uplink = frequencies **phones transmit toward towers**. 
Downlink = towers transmitting toward phones.

Phones are **not** continuous transmitters. Uplink energy appears as **bursty / intermittent** activity:
- Idle/registration: occasional short bursts
- Active call/data: more frequent packets, power-controlled

### How the current system already classifies them
From `allocations.py` + OpenCellID/CellMapper:
- Matched to carrier-owned blocks (e.g. "AWS UPLINK (B4/B66)", "700 MHz UPLINK (B12/B17)", "850 MHz UPLINK (B5/B26)", "PCS UPLINK (B2/B25)", TDD bands).
- Marked `uplink: true` + `caution: true`.
- Classification usually **PLAUSIBLE** (or VERIFIED for downlinks with nearby towers).
- Label often "Cellular — unattributed" or carrier-specific.
- In UI/reports: shows ⚠ caution marker.
- Static + dynamic baseline (OpenCellID + your CellMapper import) tells you which carriers have towers nearby and what blocks they own.

See capture SVGs in the wiki for real examples with exact labels.

Uplink activity is **expected** when people have phones. It is **not automatically a threat**.

### Practical ways to confirm "normal phones" vs suspicious uplink device
1. **Activity correlation test (best and cheapest)**
   - Have everyone put phones on airplane mode (or into a Faraday bag/pouch) one person or group at a time.
   - Watch the live trace / directional meter / wiki updates.
   - Legit uplinks drop dramatically or disappear when phones are silenced.
   - A hidden device or exfil gadget in the uplink band will usually stay constant.

2. **Direction finding (DF) with directional antenna**
   - Use the app's Directional mode + your directional antenna.
   - Sweep bearings. Peak signal when pointed at:
     - People (pockets, purses) or specific rooms with phones → normal.
     - A fixed object, wall, outlet, lamp, or empty area with no phones → suspicious.
   - Phones move with their owners; a bug is usually fixed.

3. **Count vs inventory**
   - Run the app's wireless inventory (or manual WiFi/BT scan).
   - Ask client: "How many phones/tablets on site right now?"
   - Multiple simultaneous uplink bursts roughly matching the number of active devices = good sign.
   - One persistent strong uplink with no corresponding phone = investigate.

4. **Phone field-test mode (confirm active bands)**
   - Android: dial `*#*#4636#*#*` → Phone information → set network type or see serving cell + EARFCN.
   - Apps: Network Cell Info, CellMapper, LTE Discovery, G-NetTrack.
   - See exactly which bands the real phones are using. Compare to the energy you see on the spectrum.

5. **Spectral / demod signature (with new SDR layer)**
   - LTE uplink uses SC-FDMA (looks different from simple FM).
   - Bursty with specific resource block structure.
   - In SDR# / SDR++ (NFM or wide enough filter): sounds like digital noise / data packets. No clear analog voice.
   - Power varies (phones ramp power to talk to distant tower).
   - With HackRF + advanced tools (gr-lte, srsRAN, etc.): possible to see more, but overkill for most TSCM.
   - Record a short IQ or audio clip linked to the capture for later review.

6. **Other context**
   - Correlate with known downlink energy from the same tower (weaker usually).
   - Time patterns: spikes when people are actively using data/calls.
   - TDD bands (B41/n41, CBRS, C-band) share spectrum for UL/DL — harder to separate.

### Red flags for a "bad" uplink transmitter
- Persistent high-duty-cycle uplink with no phones present or all in airplane mode.
- Signal strength much higher than expected for a phone (or doesn't follow power control).
- Fixed location (DF always points to the same spot, not moving people).
- Wrong band for local towers (baseline will show mismatch).
- Continuous carrier instead of scheduled bursts.
- Combined with other anomalies (e.g., unknown device in WiFi inventory).

**Bottom line for uplinks:** The spectrum + baseline tells you "this belongs to cellular." The field tests + demod tell you "this is coming from actual client phones."

## 2. Identification Framework for Everything Else

Use this layered process on every anomaly (especially UNVERIFIED or caution-flagged PLAUSIBLE).

### Layer 1 — Automatic (app already does this)
- Baseline match (FCC, ULS, OpenCellID, CellMapper, static allocations).
- Classification: VERIFIED / PLAUSIBLE / UNVERIFIED.
- Caution flag (ISM bands, uplinks, DECT, GPS, 49 MHz, etc.).
- Spectral fingerprint (wiki capture: BW, duty cycle, persistence, peaks, SNR).
- From `sigcapture.py` rule-based description + optional AI.
- Wireless inventory (WiFi APs + BLE advertisers).

### Layer 2 — Spectral characteristics (wiki captures)
| Feature              | Common meaning                          | Example bands          |
|----------------------|-----------------------------------------|------------------------|
| Narrow (<25 kHz), continuous or high duty | Analog voice bug or beacon             | 433/915 MHz ISM       |
| Bursty, low duty     | Digital data, keyfob, packetized       | 315/433/868, BLE      |
| Wide (MHz-scale)     | WiFi, video, LTE carrier fragment      | 2.4/5 GHz, cellular   |
| Multiple sidebands   | FM with audio or digital modulation    | Many                  |
| Very low power       | Distant or low-power device            | —                     |

Use the high-iteration zoom capture in the app for best shape.

### Layer 3 — Demod / Listen (new capability we're adding)
This is the big missing piece for identification.

- **Analog bugs / wireless mics / some keyfobs** (common in 300-900 MHz ISM):
  - Mode: NFM (narrow) or AM.
  - Result: Clear room audio, voice, tones, or "dead air" carrier.
  - Record the clip and link it to the capture.

- **Digital / data devices**:
  - Sounds like noise, modem, or bursts.
  - Record IQ (complex samples) → open in **Universal Radio Hacker (URH)** for protocol reverse engineering.
  - Or use specific decoders (keyfob, weather, etc.).

- **Cellular uplink/downlink**:
  - Digital. Sounds like data. Full stack decode is advanced.
  - Useful for timing and BW confirmation, not "hearing the call" easily.

- **Broadcast**:
  - FM/AM → music/talk. Easy confirm.

- **WiFi / video**:
  - Wide. Sounds like noise. Better identified with protocol tools below.

Suggested modes (reuse existing BW/duty logic):
- < 25 kHz narrow → start NFM
- Wider continuous low-duty → AM or raw
- Bursty packet → listen raw or wide filter

### Layer 4 — Direction finding + behavioral correlation (field work)
- Directional antenna + live strength (app already supports).
- On/off tests:
  - Turn off WiFi router / cordless base / known devices.
  - Ask occupants to use specific devices while you watch.
  - Airplane mode test for phones.
- Move people around while watching peaks.
- Ambient vs interior delta (app computes this).

### Layer 5 — Protocol / device-specific tools (cheap and powerful)
- **WiFi**: Phone WiFi analyzer apps, Acrylic WiFi, Wireshark (with monitor-mode adapter). See channels, BSSIDs, data rates, hidden SSIDs.
- **Bluetooth / BLE / Zigbee**: nRF Connect (excellent free app), Bluefruit, Wireshark.
- **DECT cordless**: Pick up a handset and watch 1.9 GHz bursts. Some SDR decoders exist.
- **Keyfobs / remotes / sensors**: SDR# + specific plugins or URH. Many 433/315 MHz use simple OOK/ASK.
- **Cellular confirmation**: Phone engineering menus + apps listed above.
- **General scanner**: Cheap handheld scanner (Uniden etc.) for quick voice checks on LMR/public safety.

### Layer 6 — Physical + non-RF
- Walk the DF bearing with thermal camera, NLJD, borescope, visual search.
- Check for new "chargers", smoke detectors, clocks, lamps, USB hubs, etc. in the hot spot.
- Powerline or ultrasonic bugs (outside pure RF, but relevant).

## Quick Decision Table (most common in US residential/commercial)

**433 / 915 MHz strong or persistent**
- Listen (NFM): voice → bug
- Bursty no audio → remote/sensor or digital bug
- Match baseline ISM caution → investigate further

**2.4 / 5 GHz bursty**
- WiFi inventory or analyzer → client devices?
- Unknown high-power → possible camera or rogue AP
- Listen: usually just noise

**~1.9 GHz**
- DECT. Test cordless phones.

**Cellular uplink blocks (see list above)**
- Correlation tests + DF to people = normal
- Persistent with phones silenced = problem

**VHF/UHF narrow (150-470 MHz)**
- Listen: voice → two-way radio or bug
- Match LMR / public safety baseline

**Very low freq (< 1 MHz or 49 MHz)**
- Rare for modern bugs. Legacy baby monitors or weird carriers. Caution.

**GPS bands (1575 MHz etc.) detectable**
- Should be below noise. Energy here = repeater, jammer, or spoofer. High priority.

## Recommended Workflow with the App + New Listen Capability

1. Run normal sweep → review Results (ranked anomalies) and Wiki.
2. For any UNVERIFIED or caution item:
   - Click capture → note center freq + BW + suggested mode.
   - Move to directional if needed.
   - Launch listen helper or in-app demod: tune, choose mode, listen + record.
3. Run correlation tests while monitoring.
4. Use phone apps / sniffers in parallel.
5. DF walk.
6. Update wiki entry with what you heard ("clear voice — 'testing 1 2 3'", "digital bursts", "matches client's iPhone on B5 uplink", "no audio — likely normal cellular").
7. If still unknown after all layers → physical search + note as high-anomaly.

## Enhancements (for the DCS / ShieldTSCM platform)

- Add "Tune & Listen" button directly on Results and Wiki rows (prefills freq + auto-suggested mode from existing features).
- Store demod notes + audio clip path linked to `captures` table.
- Special uplink test workflow button ("Run phone correlation test") that guides the operator and logs results.
- Surface client wireless inventory more prominently alongside RF anomalies.
- Rule-based + AI description enrichment using demod outcome ("analog voice confirmed" boosts anomaly score).

See also:
- `DEMOD_LISTEN_DESIGN.md` for implementation plan.
- `allocations.py` for the full current band model and caution logic.
- `correlation.py` for classification rules.
- `sigcapture.py` for features and descriptions.

This combination (great baseline + fingerprints + listen + disciplined field tests) is how professional TSCM operators separate the thousands of normal emitters from the few that matter. The app already has the foundation; adding the listen layer completes it for identification. 

Update this guide as you develop new techniques or encounter novel signals.