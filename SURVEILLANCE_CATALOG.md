# Surveillance-Device Identification Catalog — Operator Guide

Internal aid for the Shield TSCM spectrum survey. **Additive** to `sigidwiki_ref.json` — it adds per-device-class intelligence (real products, identifying features, confirm steps) on top of the existing signal matcher. Every entry is confirmed by inventory / demod / localization, **not RF class alone**.

**Hardware baseline:** a **stock ADALM-Pluto tunes 325–3800 MHz only**. It **cannot** see 5 GHz Wi-Fi or 5.8 GHz FPV video unless the AD9363→AD9364 firmware mod is applied (raises the ceiling to ~6 GHz, 56 MHz instantaneous BW). Rows above 3.8 GHz are flagged `[REQUIRES MODDED PLUTO or Wi-Fi NIC]`.

**Counts:** 10 device classes, 23 signatures.

---

## 1. wifi_ip_camera
**What it is:** Hidden Wi-Fi/IP cameras streaming video over 2.4 GHz (and, dual-band, 5 GHz) WLAN. The tell is bursty wideband OFDM plus a camera-class device that shouldn't be on the network.

| Signature | Band (MHz) | BW (kHz) | Class | Duty |
|---|---|---|---|---|
| 2.4 GHz WLAN uplink (spy cam / baby monitor) | 2400–2483.5 | 20000 | ofdm_wideband | bursty |
| 5 GHz U-NII dual-band *(modded Pluto / Wi-Fi NIC)* | 5170–5825 | 20000 | ofdm_wideband | bursty |

**Real products:** generic Amazon/AliExpress spy mini-cams (clock/charger/smoke-detector form factors, device class only — model numbers churn); Wyze Cam v3 (real 2.4 GHz cam); Ring/Blink/Nest indoor (real dual-band, shown as benign reference).
**Confirm:** sweep 2400–2483.5, wave a hand / toggle a light and watch the uplink burst rate climb; **do not sweep 5 GHz on a stock Pluto** (ceiling 3.8 GHz) — mod it or use the Wi-Fi NIC/router client list; enumerate DHCP clients for an unexplained camera-class MAC; lens/pinhole search at the strongest burst.
**Could legitimately be:** any 2.4 GHz Wi-Fi device (phone, laptop, printer, hub, other cam); baby monitors / video doorbells match exactly; 5 GHz hits are usually benign routers/laptops/TVs.

---

## 2. analog_fpv_micro_camera
**What it is:** Analog FPV / covert video transmitters — wide **continuous** FM video links (drone cams, pinhole board cams).

| Signature | Band (MHz) | BW (kHz) | Class | Duty |
|---|---|---|---|---|
| 1.2/1.3 GHz FM video | 1080–1360 | 12000 | analog_video | continuous |
| 2.4 GHz FM video | 2400–2483.5 | 18000 | analog_video | continuous |
| 5.8 GHz Raceband/A/B/E/F *(modded Pluto / FPV Rx)* | 5645–5945 | 18000 | analog_video | continuous |

**Real products:** 5.8 GHz FPV VTx (Raceband 40-ch modules); 1.2/1.3 GHz analog VTx modules; 2.4 GHz board/pinhole "nanny-cam" transmitters (no Wi-Fi).
**Confirm:** a **continuous** wide carrier (vs bursty Wi-Fi) that demods to a live picture is definitive; 1.2/1.3 GHz — note 1256/1280 MHz are the commonly cited US ham-legal channels (sources vary; legality ≠ benign); 5.8 GHz needs a modded Pluto or FPV goggles/receiver.
**Could legitimately be:** hobbyist 1.2 GHz amateur TV (ATV); Wi-Fi/BT/Zigbee share 2.4 GHz but are bursty/hopping, not continuous; 5.8 GHz Wi-Fi/cordless share the band.

---

## 3. ble_tracker
**What it is:** Bluetooth-LE item trackers repurposed for covert location tracking. Periodic GFSK advertising bursts on 3 fixed channels; a phone/network relays position.

| Signature | Band (MHz) | BW (kHz) | Class | Duty |
|---|---|---|---|---|
| BLE tracker advertising bursts | 2400–2483.5 | 2000 | fsk | intermittent |

**Real products:** Apple AirTag (BLE+UWB); Samsung Galaxy SmartTag/SmartTag2; Tile (Mate/Pro/Slim/Sticker); Chipolo ONE/CARD, Pebblebee Clip/Card, Motorola Moto Tag (Find My / Find Hub compatible).
**Confirm:** watch 2402/2426/2480 MHz for periodic bursts; use phone "unknown tracker" alerts / Apple Tracker Detect / a BLE scanner, filter for Find My (Apple **0x004C**) and Google FMDN (service **0xFEAA**, **frame 0x41**); force the anti-stalking beep — randomized **~8–24 h** on later firmware (older units up to ~3 days) — and localize; UWB tags support precision-find.
**Could legitimately be:** any BLE device (phone, earbud, watch, hub) — a raw advertising burst isn't a tracker; **0x004C alone** = all Apple Continuity/Handoff, not just Find My; **0xFEAA** is also legacy **Eddystone/physical-web beacons** — disambiguate via frame 0x41; retail beacons/BLE sensors are benign.

---

## 4. bluetooth_audio_bug
**What it is:** Covert audio bugs exfiltrating room audio over Bluetooth — Classic (A2DP/HFP streaming, high-duty) or BLE.

| Signature | Band (MHz) | BW (kHz)* | Class | Duty |
|---|---|---|---|---|
| BT Classic A2DP/HFP streaming | 2400–2483.5 | 1000 | fhss | continuous |
| BLE audio (connected/advertising) | 2400–2483.5 | 2000 | fsk | intermittent |

*BW is **per-hop channel width**, not aggregate stream occupancy.
**Real products:** spy-shop BT "listening" modules / disguised BT speakers & earpieces (device class, model numbers churn); repurposed BT audio SoCs in fake chargers/power banks; consumer BT speakers/headsets (the legit hardware a bug impersonates).
**Confirm:** hopping GFSK that stays **near-continuous** = active stream (vs low-duty beacon); BT/BLE discovery scan for an unexplained A2DP/HFP device; introduce an audio cue and watch the stream respond; proximity-search the strongest hopping source. *Note: the app has no fhss runtime path — a hopping BT stream typically classifies as **fsk**.* AFH minimum is **15 channels (US)** / 20 (EU/spec floor).
**Could legitimately be:** any BT speaker/headset/car-kit/earbud streaming A2DP/HFP; BT mice/keyboards; an idle BT device advertising as a beacon.

---

## 5. gsm_cellular_audio_bug
**What it is:** Room-audio bugs that call out over cellular (2G GSM legacy; LTE-M/NB-IoT modern). Often voice-activated (VOX).

| Signature | Band (MHz) | BW (kHz) | Class | Duty |
|---|---|---|---|---|
| 2G GSM uplink (GMSK) | 380–1990 | 200 | continuous_digital | bursty |
| LTE-M (Cat-M1) uplink | 600–2200 | 1400 | continuous_digital | bursty |
| NB-IoT uplink (thin CW-ish line) | 600–2200 | 180 | continuous_digital | intermittent |

**Real products:** GSM "SIM-card" / N9-class mini listening bugs (EU spy-shops — StealthTronic, DPL, Efeel, spyequipmentuk; class-level, VOX callback); LTE-M/NB-IoT modem bugs (modern, where 2G is dead); disguised GSM bugs in power strips/chargers/clocks.
**Confirm:** watch cellular uplink bands for an unexplained room-origin burst that keys up when you speak (VOX ~**45 dB** at a few metres, vendor-variable, **not** a fixed 60 dB); a **3.75 kHz NB-IoT single-tone** looks like a thin CW line — flag any narrow single-tone room uplink for localization regardless of assigned class; a bug inches from the antenna is easy to receive on any band.
**Could legitimately be:** any phone/tablet/hotspot/smartwatch/cellular-IoT (alarm panels, meters, trackers); a benign NB-IoT meter/sensor can alias to analog_voice. *Full-rate LTE is ofdm_wideband in the app; LTE-M/NB-IoT is deliberately continuous_digital here (BW only 1.4 MHz / ~180 kHz) — not a wrong-class flag.*

---

## 6. analog_rf_audio_bug
**What it is:** Classic analog RF bugs / body wires / wireless mics — a **continuous narrowband FM** (or SCA sub-carrier) voice carrier you can demod and listen to. Most directly confirmable class.

| Signature | Band (MHz) | BW (kHz) | Class | Duty |
|---|---|---|---|---|
| Free-running FM bug (88–108 kit) | 65–130 | 180 | analog_voice | continuous |
| Wireless mic / body wire — VHF | 169–216 | 200 | analog_voice | continuous |
| Wireless mic / body wire — UHF TV-band | 470–608 | 200 | analog_voice | continuous |
| SCA sub-carrier (67/92 kHz on FM) | 87.5–108 | 220 | analog_voice | continuous |

**Real products:** single-transistor Colpitts 88–108 MHz FM bug kits (real, widely sold); Shure BLX UHF mics (verified band edges H8 518–542, H9 512–542, H10 542–572, J10 584–608); VHF mics 169–172 / 174–216 MHz; SCA sub-carrier audio technique (F908-type internals and LEA body-wire model numbers are **unverified marketing/device-class only** — never cite as SKUs).
**Confirm:** sweep VHF/UHF for a continuous narrow carrier and **demod FM — recovered room audio confirms**; free-running bugs cluster 88–108 (108–137 MHz is the aeronautical band, so a bug there is atypical and stands out — not an empty gap); UHF mic band 470–608 with the 608–614 MHz astronomy carve-out (47 CFR 15.236); demod the 67/92 kHz SCA sub-carrier for hidden speech.
**Could legitimately be:** licensed wireless mics / IEMs / assistive-listening in active use; FM broadcast + legit SCA (reading services, background music); aeronautical VOR/ILS/ATC voice. *A wideband compandered mic measured >300 kHz reclassifies out of analog_voice into continuous_digital in this app; the figures here stay under the cap.*

---

## 7. dect_cordless_bug
**What it is:** DECT-band bugs / cordless-phone / baby-monitor relays. ~1.728 MHz TDMA channels.

| Signature | Band (MHz) | BW (kHz) | Class | Duty |
|---|---|---|---|---|
| DECT cordless / covert relay | 1880–1930 | 1728 | continuous_digital | bursty |

**Real products:** DECT cordless phones / headset bases (e.g. Poly CA22CD-SC — **discontinued 2025**, cited only to illustrate the office DECT form factor); DECT baby monitors; covert DECT audio relays.
**Confirm:** sweep 1880–1930 (US: focus 1920–1930 UPCS) for ~1.728 MHz TDMA bursts; **inventory every DECT emitter** and localize the unexplained one. UPCS is power-limited to **100 mW + listen-before-talk**, so a covert relay obeys the same cap — signal strength is **not** a discriminator; timing + inventory are.
**Could legitimately be:** legit DECT phones / office headset bases / DECT baby monitors — the band is theirs.

---

## 8. subghz_ism_tracker
**What it is:** Sub-GHz ISM location/telemetry — LoRa/CSS, FSK trackers, 433 MHz recovery beacons. Very low power, low duty; easy to miss.

| Signature | Band (MHz) | BW (kHz) | Class | Duty |
|---|---|---|---|---|
| LoRa / CSS tracker | 433–928 | 125 | fhss* | intermittent |
| 433 MHz last-metre recovery beacon | 433–434 | 40 | ook | intermittent |
| Sub-GHz FSK telemetry tracker | 300–928 | 50 | fsk | bursty |

*`fhss` is a **stand-in** — the app has no chirp/CSS class; watch the waterfall for the **chirp (frequency ramp)**, not the label.
**Real products (verified):** Digital Matter Yabby Edge LoRaWAN (US915/IP68/AAA); Digital Matter Barra Recovery (433 MHz last-metre beacon, 2×AA); HopeRF RFM95W / Semtech SX1276 modules. Consumer GPS trackers (LandAirSea 54/Overdrive, Spytec GL300, Trackimo) are **form-factor examples only — they exfil over cellular, not confirmed sub-GHz emitters.**
**Confirm:** sweep 433/868/915 for periodic low-power bursts; the CSS **chirp** on the waterfall is the load-bearing tell; SX1276 sensitivity ~**−123 dBm (SF7/125 kHz)** to ~**−137 dBm (SF12/125 kHz)**, −148 dBm only the datasheet floor at max spreading — low power = short range, so **dwell and get close**.
**Could legitimately be:** crowded ISM — OOK remotes, keyfobs, garage openers, TPMS, weather sensors, smart meters, LoRaWAN IoT; legit LoRaWAN networks use the same chirps.

---

## 9. cellular_gps_tracker
**What it is:** Covert vehicle/asset GPS trackers reporting over LTE-M/NB-IoT (2G on legacy). GPS is **receive-only** — the tell is the **cellular uplink callback**.

| Signature | Band (MHz) | BW (kHz) | Class | Duty |
|---|---|---|---|---|
| LTE-M (Cat-M1) uplink callback | 600–2200 | 1400 | continuous_digital | bursty |
| NB-IoT uplink callback | 600–2200 | 180 | continuous_digital | intermittent |
| Legacy 2G GSM uplink callback | 380–1990 | 200 | continuous_digital | bursty |

**Real products (verified):** LandAirSea 54 & Overdrive (magnetic covert); Spytec GL300/GL300MA; Trackimo (Amazon ASIN B07R6ZD8JS); Digital Matter Yabby Edge / Oyster.
**Confirm:** watch LTE-M/NB-IoT uplink for a **repeating check-in cadence** from the vehicle/asset; **don't expect emissions at GPS L1 (1575.42 MHz)** — energy there instead suggests a jammer/repeater; a tracker inches from the antenna is easy to receive on any band (higher-band tower path loss doesn't weaken the local emission you receive); localize + physical/magnetic search (wheel wells, bumpers, OBD-II, under seats).
**Could legitimately be:** any phone/hotspot/smartwatch/connected-car TCU/cellular-IoT uplink; factory telematics (eCall/OnStar-style). *duty is bursty even though class = continuous_digital — key off duty, not the class name.*

---

## 10. wifi_gadget_spy
**What it is:** Disguised everyday gadgets (pen, USB drive, charger, power bank, clock) recording/streaming audio/video over **2.4 GHz Wi-Fi — no SIM**.

| Signature | Band (MHz) | BW (kHz) | Class | Duty |
|---|---|---|---|---|
| Wi-Fi spy gadget (pen/charger/USB) | 2400–2483.5 | 20000 | ofdm_wideband | bursty |

**Real products (verified):** SpyEquipmentUK "WiFi Listen and Record Pen" (2.4 GHz Wi-Fi, no SIM, app live-listen + VOX push, 1–30 min or continuous, ~8 m mic, 5–9 day battery); "Mriya" **Wi-Fi variant** (WAV 128 kbps, VOX, 32 GB microSD — **note the same name also tags a 4G/GSM variant**, so the name alone doesn't confirm RF class); Wi-Fi hidden-cam gadgets (chargers/clocks/smoke detectors — overlaps `wifi_ip_camera`).
**Confirm:** sweep 2400–2483.5 for bursty WLAN OFDM tied to a small gadget appearing as an **unexplained Wi-Fi client**; enumerate stations/DHCP for an OUI/MAC on a no-name module in a pen/charger form factor; trigger VOX (speak / change the scene) and watch the uplink rise; inspect the object for a lens/pinhole/mic port at the strongest burst.
**Could legitimately be:** any legit 2.4 GHz client (phone, laptop, IoT, USB Wi-Fi adapter, smart pen); legit voice recorders/webcams in pen/charger form factors — **the disguise, not the RF, is the giveaway.**

---

### Client-report guardrails
- Product names/specs are **internal identification aids** only — do not paste spy-shop marketing or vendor waterfall images into client deliverables (sigidwiki content is CC BY-NC-SA / non-commercial).
- Phrase every catalog hit as "a signal **consistent with** X, pending confirmation," never "X detected." Show the **"could legitimately be"** overlap next to any flagged device.
- Do not present legality conclusions (FPV 1.2 GHz channels, mic-band rules) to a client without counsel review.

---

# Wiring into identify.py (integration note)

# Integrating surveillance_catalog.json into survey/identify.py

## Where the file lives
Ship it alongside the existing reference: `survey/surveillance_catalog.json` (same directory as `sigidwiki_ref.json`, which is `os.path.dirname(__file__)`). Define a sibling to `DEFAULT_REF_PATH`:

```python
DEFAULT_CATALOG_PATH = os.path.join(os.path.dirname(__file__), "surveillance_catalog.json")
```

## Loader — flatten device_classes into the SAME candidate list
The catalog is **additive**. Load it, flatten every `device_class.signatures[]` entry into the same flat list `load_reference()` returns, and stamp each flattened signature with a back-pointer to its parent so the matcher can surface device-class intelligence on a hit.

```python
def load_catalog(path: str | None = None) -> list[dict]:
    """Flatten surveillance_catalog.json signatures into sigidwiki-ref-shaped
    rows, each carrying its parent device_class context. ADDITIVE — never
    replaces sigidwiki_ref.json."""
    p = path or DEFAULT_CATALOG_PATH
    if not os.path.exists(p):
        return []                      # optional file; app runs without it
    doc = json.load(open(p, "r", encoding="utf-8")) or {}
    rows = []
    for dc in doc.get("device_classes", []):
        ctx = {
            "device_class": dc["device_class"],
            "device_summary": dc.get("summary", ""),
            "confirm_steps": dc.get("confirm_steps", []),
            "false_positive_overlap": dc.get("false_positive_overlap", []),
            "device_tscm_notes": dc.get("tscm_notes", ""),
            "example_products": dc.get("example_products", []),
        }
        for sig in dc.get("signatures", []):
            row = dict(sig)            # name/freq_min_mhz/.../class/duty/tscm/url
            row["_catalog"] = ctx      # parent context rides along
            rows.append(row)
    return rows
```

## Merge into the existing ranking — no change to identify() scoring
`identify(center_hz, feats, reference, top_n)` already reads exactly the fields the flattened rows provide (`freq_min_mhz`, `freq_max_mhz`, `bandwidth_khz`, `class`, `duty`, `name`, `url`, `mode`, `tscm`). So the ONLY change is to concatenate the two lists before ranking:

```python
reference = load_reference()          # sigidwiki_ref "signals"
reference += load_catalog()           # surveillance catalog signatures (additive)
matches = identify(center_hz, feats, reference, top_n=5)
```

Do **not** modify `_bw_score`, band scoring, class scoring, or duty scoring — catalog rows are scored identically to sigidwiki rows, so existing matching is untouched and existing tests still pass.

## Surface the parent device-class on a match
`identify()` builds each output dict from a fixed set of ref keys and drops unknown keys. Add a pass-through so `_catalog` reaches the UI. Minimal change inside the `identify()` output builder:

```python
out.append({
    "name": ref["name"], "url": ref["url"], "mode": ref["mode"],
    "class": ref["class"], "bandwidth_khz": ref["bandwidth_khz"],
    "tscm": ref.get("tscm", ""), "in_band": in_band,
    "score": round(score, 1), "reasons": reasons,
    "catalog": ref.get("_catalog"),      # None for plain sigidwiki rows
})
```

In the SIGNAL ID / match view, when a top candidate has a non-null `catalog`, render the device-class panel: `device_class`, `device_summary`, then **Confirm steps**, **Could legitimately be** (`false_positive_overlap`), and **Real product examples** (`example_products`). For plain sigidwiki hits `catalog` is `None` and the view is unchanged.

## Class-scoring caveat the matcher must respect (already handled by design)
`classify_modulation()` only ever emits `analog_voice, analog_video, fsk, ook, ofdm_wideband, continuous_digital, unknown, noise_floor`. It **never returns `fhss`, `psk`, or `noise_like`**. Catalog rows tagged `fhss` (Bluetooth-Classic bug, LoRa/CSS tracker) will therefore never win the `+20` same-class bonus — they still rank on band + bandwidth + duty, which is intended. Each such signature's `tscm`/`mode` text explains the true modulation and what the app will actually classify it as (e.g. "app likely classes this as fsk"). Do not "fix" this by remapping catalog classes to what the classifier emits — the human-accurate label plus the operator note is the design; the ranking already works without the class bonus.

## Duty note
`identify()` compares `feats["continuity"]` (`continuous`/`bursty`/`intermittent`) to `ref["duty"]`. All catalog `duty` values are in that set. Note `cellular_gps_tracker` and `gsm_cellular_audio_bug` carry `class: continuous_digital` with `duty: bursty` — the matcher keys duty off the `duty` field, not the class name, so this is consistent; don't assume `duty=="continuous"` just because the class string contains "continuous".

## Hardware gating (Pluto reach)
Two signatures have `freq_min_mhz > 3800` (`wifi_ip_camera` 5 GHz row at 5170; `analog_fpv_micro_camera` 5.8 GHz row at 5645). A **stock** ADALM-Pluto tops out at 3.8 GHz and cannot tune there. If the app knows whether the connected Pluto is AD9364-modded, it should either (a) skip/grey these rows and show the `[REQUIRES MODDED PLUTO or Wi-Fi NIC]` note, or (b) route 5 GHz camera detection to the Wi-Fi-NIC/network-scan path. Their `name`/`tscm`/`mode` already carry the caveat inline so nothing breaks if the app can't detect the mod — the operator is warned in the match text.

## Client-doc guardrails (carry through to any report generator)
- Product names/specs are for **internal** identification only. Do NOT paste spy-shop marketing copy or vendor waterfall images into client deliverables without review; sigidwiki content is CC BY-NC-SA (non-commercial).
- Every class is confirmed by **inventory / demod / localization**, not RF class alone. When emitting a client-facing line, phrase catalog hits as "a signal *consistent with* X, pending confirmation," never "X detected." The `false_positive_overlap` list is what a client reads as "could legitimately be…" and must appear next to any flagged device.
- Do not present US-legal-channel or legality conclusions (FPV 1.2 GHz, mic bands) to a client without counsel review.
