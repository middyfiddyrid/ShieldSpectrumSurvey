# Shield Sweep — Signal accuracy enhancements

**Purpose:** Roadmap for making “what is this emission?” more accurate without
lying to the operator. Spectrum almost never carries a product name; accuracy
comes from **fusing** shape, protocol IDs, inventory, and controlled field tests.

**Related:** `SIGNAL_IDENTIFICATION_GUIDE.md`, `survey/behavior.py`,
`survey/rf_match.py`, `survey/identify.py`, `survey/ble.py`, `survey/inventory.py`.

**Honesty rules (do not break these):**
- Phrase findings as **“consistent with …”**, never **“Samsung TV detected”** or
  **“hidden camera confirmed”** from RF alone.
- Protocol IDs (BSSID, BLE company id) may name a *vendor class*; shape-only may
  not.
- Content listen stays ECPA-gated / audited (`app.admin`, decode override).
- Receive-only: no jamming, deauth, or TX to knock devices off air.

---

## 0. Already shipped (this line of work)

| Item | Where | What it does |
|------|--------|----------------|
| Behavioral class | `survey/behavior.py` | video_like, keyfob_like, hid_keepalive, wifi_data, ble_class, analog_audio_bug, cellular_uplink, … |
| RF ↔ inventory | `survey/rf_match.py` | Wi‑Fi channel-center match; BLE co-band soft match (trackers first) |
| Enrichment | `nextstep.enrich_signal_rows` | Attaches `behavior`, `inventory_matches`, smarter next-step chips |
| API | `/api/signals`, `/api/identify`, `/api/analyze/signal` | Returns behavior + inventory summary |
| UI | live ledger + Results signals + lightbox | Behavior phrase, inventory line, keep ledger after sweep ends |

**Still weak in shipped code:** inventory only helps if the operator ran a Wi‑Fi/BLE
scan; OUI/vendor naming for Wi‑Fi is thin; no guided field tests; no personal
capture library; burst-period / multi-capture duty is coarse (uses persistence).

---

## 1. Remaining enhancements (priority order)

### P1 — Richer protocol IDs (biggest accuracy bang)

**Goal:** When the device uses an open wireless stack, surface the *real*
identifier the operator can hand to the client.

#### 1A. Wi‑Fi OUI / vendor class on every AP and (where possible) client

| | |
|--|--|
| **Why** | SSID “LivingRoom” is weak; OUI → “Samsung / Amazon / Espressif / Ubiquiti” turns an RF hit into a shoppable class. |
| **How** | Ship a curated OUI table (TSCM-relevant vendors: camera makers, TV, mesh, IoT SoCs) similar to `ble.OUI_VENDORS`. Map BSSID first 3 octets → vendor. |
| **Wire** | `inventory.scan_wifi` → set `extra` / new `vendor` field; `rf_match` phrases include vendor; lightbox “Inventory match”. |
| **Done when** | A channel-matched AP shows e.g. `Nest cam class (Google) · BSSID …` not only SSID. |
| **Out of scope** | Full IEEE OUI dump (huge); active association / deauth. |

#### 1B. Expand BLE company IDs + service UUIDs (mice, keyboards, TVs, trackers)

| | |
|--|--|
| **Why** | Random MACs hide OUI; manufacturer data still says Apple / Logitech / Tile. |
| **How** | Grow `ble.py` tables: HID dongles, smart TV remotes, more trackers, hearing aids, smartwatches. Flag `is_hid`, `is_av_remote` like `is_tracker`. |
| **Wire** | BLE inventory + soft RF match boost HID/tracker scores. |
| **Done when** | Unowned Tile/AirTag and “Logitech-like HID” are obvious in BLE detail without IQ. |

#### 1C. Time/RSSI correlation (RF peak ↔ inventory)

| | |
|--|--|
| **Why** | Channel match is static; a burst that appears when a phone unlocks is stronger evidence. |
| **How** | While sweeping 2.4/5 GHz, optionally re-scan Wi‑Fi/BLE on a slow cadence; store `{t, kind, id, rssi}` samples; after segment, score inventory rows whose RSSI rose with RF occupancy. |
| **Done when** | Match object can say `co-timed with BLE “Phone” (+12 dB when RF peaked)`. |
| **Caution** | Best-effort; Windows scan is slow — never block the sweep. |

#### 1D. One-click “scan inventory for this hit”

| | |
|--|--|
| **Why** | Operators forget to scan before looking at anomalies. |
| **How** | Next-step chip / lightbox button: `POST /api/inventory/scan` then re-enrich this signal. |
| **Done when** | From a 2.4 GHz video_like hit, one click refreshes Wi‑Fi/BLE and repaints matches. |

---

### P2 — Stronger behavioral signatures (still not brand)

**Goal:** Better *class* scores from multi-segment / multi-capture shape, not
single-pass persistence.

#### 2A. Burst period & duty from IQ (not only sweep persistence)

| | |
|--|--|
| **Why** | Key fobs ≈ rare single burst; mice ≈ ~8–20 ms keepalives; Wi‑Fi ≈ irregular packet trains; analog video ≈ near-continuous. |
| **How** | In `iq_features` / new `behavior_from_iq`: envelope autocorrelation → `burst_period_ms`, `on_time_ms`, `duty_cycle` (already partial). Feed `classify_behavior`. |
| **Done when** | Continuous 5.8 GHz wide → video_like ≥0.75; synthetic OOK keyfob → keyfob_like with period evidence in the phrase. |

#### 2B. Multi-pass occupancy histogram per detection

| | |
|--|--|
| **Why** | One sweep pass mis-labels hoppers and intermittent bugs. |
| **How** | Persist per-signal hit mask or pass-count series (segment already has `hits[]`). Derive `duty_stable`, `first_seen_pass`, `last_seen_pass`. |
| **Done when** | Behavior evidence lists e.g. `present on 3/40 passes (7.5% duty)`. |

#### 2C. Explicit “false friend” pairs in the UI

| | |
|--|--|
| **Why** | nRF24 HID and some bugs look the same; Wi‑Fi cam and busy AP look the same. |
| **How** | For each `class_id`, show `could_also_be` from `surveillance_catalog.json` `false_positive_overlap` + fixed strings in `behavior.py`. |
| **Done when** | Lightbox always shows “Could legitimately be: …” under behavior phrase. |

#### 2D. Cam-class confidence ladder

| Score drivers | |
|---------------|--|
| Continuous + wide + 2.4/5.8 | base |
| IQ analog_video / ofdm_wideband | + |
| No matching owned AP on channel | + |
| Fixed DF room across two bearings | + (needs DF log link) |
| Client confirms no camera | + |

Never auto-promote to “camera found.” Cap language at “high consistency with continuous video-class link.”

---

### P3 — Guided field experiments (operator gold standard)

**Goal:** Turn the playbook into clickable procedures with pass/fail recorded on
the signal.

#### 3A. Airplane-mode / phone correlation test

| | |
|--|--|
| **Steps UI** | 1) Note uplink count 2) All phones airplane 3) Re-sweep uplink bands or 60s monitor 4) Record drop %. |
| **API** | `POST /api/experiments/phone_test` `{ before_signal_ids[], after_note }` or attach notes to signals. |
| **Done when** | Uplink row next-step can mark `phones silent → energy gone (likely phones)` vs `still present (investigate)`. |

#### 3B. Inventory subtract (power-off known gear)

| | |
|--|--|
| **Steps** | Mark owned APs/TVs/cameras → prompt power-off → re-scan RF + inventory → residue list. |
| **Done when** | Residue-only anomalies sorted to top of triage. |

#### 3C. DECT base unplug test

| | |
|--|--|
| **Already** | Next-step text exists. |
| **Add** | Checkbox workflow + “energy remaining after unplug” flag on DECT-band hits. |

#### 3D. Key-fob press correlation

| | |
|--|--|
| **Steps** | Operator presses each known remote; app records time of 315/433/915 bursts; links burst to “Front door fob” note. |
| **Done when** | Uncorrelated 433 bursts stay flagged; correlated ones clear with note. |

---

### P4 — Personal / site capture library (better than stock sigidwiki alone)

**Goal:** Match against **your** confirmed field captures, not only public
reference shapes.

#### 4A. “Save as reference” from a confirmed signal

| | |
|--|--|
| **How** | After operator clears/flags with notes, optional **Save signature** → store center band, bw, duty, IQ feats, label, site type (office/home). SQLite table `signal_refs` or JSON under `data/refs/`. |
| **Match** | Extend `identify.identify` to score local refs with higher weight than generic wiki. |
| **Done when** | Second visit to same site ranks “prior: lobby badge reader” above generic ISM. |

#### 4B. Export/import ref packs

| | |
|--|--|
| **Why** | Train junior techs; share de-identified packs between kits. |
| **How** | Zip of JSON + optional IQ snippets; no client PII (strip address/names). |

---

### P5 — Decode / demod depth (protocol where legal and useful)

**Goal:** Open the stack only where it improves ID, not content surveillance.

| Protocol | Use | Note |
|----------|-----|------|
| OOK/FSK keyfob libraries | Pulse length fingerprint → vendor family | Prefer URH-assisted offline; in-app later |
| POCSAG/FLEX | Already partly present | Keep honesty gates |
| Analog NFM | Audio class only under ECPA rules | Already gated |
| Wi‑Fi management frames (passive) | Beacon IEs: vendor, model hints | Monitor mode hard on Windows laptop NIC |
| Cellular | Do **not** decode content | Band/burst only |

**Windows constraint:** full passive Wi‑Fi monitor mode is often unavailable on
stock laptop NICs — don’t promise beacon IE parsing without a supported adapter.

---

### P6 — UX so accuracy is actually used

| Item | Why |
|------|-----|
| Ledger never empty after sweep | **Fixed** — keep last sweep signals |
| Results table: Behavior + Inventory columns | **Fixed** |
| Lightbox always shows behavior phrase + “could also be” | Partial — add false-friend line |
| Auto-prompt inventory scan when first 2.4/5 GHz anomaly appears | Not done |
| Sort/filter ledger by behavior class | Not done (`?behavior=video_like`) |
| Report PDF: behavior + inventory line per anomaly | Not done — `survey/report.py` |
| Client report language review | Never upgrade “consistent with” to “is” in PDF |

---

### P7 — Hardware / RF quality (accuracy of *detection*, not just labels)

| Item | Why |
|------|-----|
| B220 LibreSDR FPGA pin + replug guidance | Already improved; still field-fragile |
| Antenna notes per band on cam-class / DECT hits | Wrong antenna → miss |
| Dual-radio: B220 sweep + Pluto identify IQ | Better features while sweeping |
| Calibration honesty | Keep cal_offset visible; don’t imply absolute dBm |

---

## 2. Suggested implementation sequence (PR-sized)

1. **PR-A — Wi‑Fi OUI + “Scan inventory for this signal”**  
   Curated OUI table, vendor on inventory rows, lightbox button, tests.

2. **PR-B — BLE HID/AV expansion + false-friend lines**  
   Tables + behavior `could_also_be` in API/UI.

3. **PR-C — IQ burst period → behavior**  
   Features + unit tests with synthetic OOK/continuous IQ.

4. **PR-D — Phone-test + inventory-subtract experiment UI**  
   Minimal state machine + notes on signals; no fancy workflow engine.

5. **PR-E — Local reference library**  
   Save/match/export; gitignore runtime data.

6. **PR-F — Report + filter polish**  
   PDF anomaly lines; ledger filter by class_id.

---

## 3. Acceptance criteria (product)

An operator on a real site should be able to:

1. Finish a sweep and **still click** hits (ledger retained).  
2. See **behavior class** on every non-broadcast hit without running IDENTIFY.  
3. Run Wi‑Fi/BLE scan once and see **named inventory** on matching RF rows.  
4. For uplinks: complete a **phone test** and mark residue.  
5. For 2.4/5 continuous wide: get **cam-class language + inventory + DF next step**, never a false “camera serial.”  
6. For 433 bursts: get **keyfob class + press-to-correlate** guidance.  
7. Export a report that uses the same cautious wording as the UI.

---

## 4. Explicit non-goals

- Mass auto-demod of content / bulk “listen to everything.”  
- Claiming unique device identity from spectrum shape alone.  
- Jamming, deauth, or TX attacks on client networks.  
- Full LTE/5G stack decode.  
- Replacing physical search / DF with software confidence scores.

---

## 5. Testing expectations

| Layer | Tests |
|-------|--------|
| Pure classifiers | Synthetic rows/IQ (already `tests/test_behavior.py`; extend) |
| OUI / BLE tables | Unit map known prefixes → vendor |
| API enrich | Flask client + fake inventory (pattern in `test_ledger_detail.py`) |
| UI | Hard-refresh; no sim-as-proof for RF claims — operator verifies on hardware |
| Regression | `python -m unittest discover -s tests` stays green |

---

## 6. One-page operator cheat sheet (ship in UI later)

| Look | Likely class | Where the real ID is | Next move |
|------|--------------|----------------------|-----------|
| Continuous wide 2.4/5.8 | Video / always-on data | Wi‑Fi BSSID/OUI; DF fixture | Inventory + power-off known cams/APs |
| Bursty wide 2.4/5 | Wi‑Fi data | Same | Inventory |
| Bursty 2.4 mid-width | BLE / HID | BLE company id / services | BLE scan; trackers first |
| Rare short 315/433/915 | Key fob / sensor | Pulse library; button press | IDENTIFY + press known remotes |
| Narrow continuous sub‑GHz | Analog audio class | Usually none | LISTEN (policy) + DF |
| Cellular uplink blocks | Phones | Not IMEI from sweep | Airplane-mode test |
| GPS band energy | Spoof/repeat/jam class | N/A | DF now |

---

## 7. Status snapshot

| Theme | Status |
|-------|--------|
| Behavior classes | **Shipped** |
| RF ↔ Wi‑Fi/BLE match | **Shipped** (static; no co-timing yet) |
| Ledger stays clickable | **Shipped** |
| Wi‑Fi OUI / vendor class | **Shipped** — `survey/wifi_oui.py`, inventory + match phrases |
| One-click inventory for hit | **Shipped** — `POST /api/inventory/scan_for_signal` + lightbox **📡 SCAN INV** |
| BLE HID/AV flags + company ids | **Shipped** (expanded tables; `is_hid` / `is_av_remote`) |
| Could-also-be false friends | **Shipped** — `behavior.could_also_be` in API + lightbox |
| IQ burst period | **Shipped** — `iq_features.burst_period_ms` feeds HID/keyfob |
| Cam-class confidence ladder | **Shipped** (duty/SNR/IQ boosts) |
| Guided experiments | **Shipped** — `POST /api/experiments/record` + lightbox **🧪 FIELD TEST** |
| Local capture library | **Shipped** — `data/sig_library/`, save/match APIs + **💾 SAVE SIG** |
| Report anomaly wording | **Shipped** — behavior + inventory lines in PDF why-cell |
| Co-timed RSSI correlation | **Not shipped** — §1C (slow Windows scans; later) |
| Ledger filter by behavior class | **Not shipped** — optional UX |
| P7 radio quality | Ongoing (FPGA pin, replug, USB) |

**Explicitly out of scope (do not implement):** bulk content auto-demod, facial
recognition on intercepts, full LTE/5G content stack, jamming/deauth.
