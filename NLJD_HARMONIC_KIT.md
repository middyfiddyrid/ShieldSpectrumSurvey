# Software-Defined Harmonic NLJD — Test-Chamber Kit Spec

> **Status:** Implementation-ready. **Radio:** Ettus/NI USRP B210 (the operator's "B220"). **Budget:** RF parts + shield under $200 (SDR already owned, excluded).
> **Read Section 1 and Section 10 before quoting anything to a client.** This is a low-power bench tool, not an Orion-grade NLJD.

---

## 1. What this is / what it is NOT

**What it is.** A **bench test-chamber** that turns an already-owned USRP B210 SDR into a rudimentary **software-defined harmonic Non-Linear Junction Detector (NLJD)**. You place an object **inside a small shielded (Faraday) cavity**, the SDR illuminates it with a clean ~10 mW CW tone at **915 MHz**, and a coherent-timebase receiver listens for the weak **2nd harmonic (1830 MHz)** and **3rd harmonic (2745 MHz)** that semiconductor junctions re-radiate — even when the electronics are powered off. The **2f/3f amplitude ratio** is used as a *continuous* discriminator between a real electronic junction and a harmless corroded-metal junction.

**What it is NOT — state these plainly to every client:**

- **NOT a commercial-grade NLJD.** A real NLJD (REI Orion, etc.) transmits **watts**; this SDR transmits **~10 mW** — roughly **20–30 dB (hundreds to thousands of times) less illumination power**. This chamber reliably flags only **BLATANT electronics** sitting in the box. It will **miss** small, RF-shielded, or deeply-buried junctions. **A null result is NOT proof the object is clean.**
- **NOT an in-place room scanner.** Objects go **inside** the box. You cannot walk this around a wall or a chair. The shield's job is to (a) block outside RF so ambient occupancy of 1830/2745 MHz is irrelevant and (b) concentrate the tiny 10 mW on the object.
- **NOT a calibrated instrument.** The B210 is **gain-only / uncalibrated** — you cannot command an absolute dBm and no vendor NF/phase-noise figures exist for it. Every number this tool reports is a **relative delta over an empty-box baseline**, never an absolute power. Any sensitivity claim must be labeled *measured-on-this-unit*, never cited from a datasheet.
- **NOT able to fix its own physics in software.** Two hard limits below are physical and require **external hardware filters**; no amount of numpy DSP removes them.

**The two hard physical limits (non-negotiable, must appear in client-facing copy):**

1. **Low TX power (~10 mW).** Orders of magnitude below a watt-class commercial NLJD. Detects blatant electronics only.
2. **SDR self-generated harmonics.** The B210's own broadband direct-conversion TX emits its **own** 2f/3f energy that lands at **exactly** 1830/2745 MHz — the same frequencies we detect. This **must** be suppressed at the source by a **physical 915 MHz TX bandpass** and kept off the receiver by a **physical RX highpass + LNA**. **A filter cannot separate the SDR's residual harmonic from the target's harmonic — they are the identical frequency.** The only things that separate them are (a) TX-side suppression at the source and (b) **empty-box baseline subtraction** in the DSP. The reported quantity is always an **object-induced delta over the empty-box self-leak floor**, never absolute harmonic power, and a bare "line present" is never a detection.

---

## 2. How harmonic NLJD works + the 2f/3f discriminator

**The mechanism.** Illuminate an object with a spectrally clean tone at `f0`. Anything with a **non-linear current–voltage (I-V) characteristic** re-radiates energy at harmonics `2·f0`, `3·f0`, … The *shape* of that non-linearity determines *which* harmonic dominates, and that is the entire diagnostic value of an NLJD over a plain metal detector: a metal detector fires on any conductor; the harmonic ratio tells you **whether that conductor contains an actual semiconductor**.

**Semiconductor junction (real electronics — a diode, transistor, or IC inside a bug, powered or not).** A p-n junction follows the Shockley diode equation, `i ≈ e^(v/nVt)` — a smooth, strongly **asymmetric** exponential curve. An asymmetric non-linearity is dominated by its **even-order (quadratic)** term, so it re-radiates a **strong 2nd harmonic** and a weaker 3rd. Signature: **2f > 3f**.

**False junction (corrosion / rust / loose oxide-coated metal-to-metal (MOM) contact / dissimilar metals touching).** This behaves like a **back-to-back diode pair** — a roughly **symmetric / antisymmetric** I-V curve. A symmetric non-linearity **suppresses even harmonics** and is dominated by its **odd-order (cubic)** term. Signature: **3f > 2f**.

**The discriminator — a CONTINUOUS dB value, not a clean binary.** The DSP computes:

```
ratio_dB = delta_2f_dB − delta_3f_dB        (both on baseline-subtracted deltas)
```

- `ratio_dB > +hysteresis`  → leans **SEMICONDUCTOR** (real electronics)
- `ratio_dB < −hysteresis`  → leans **FALSE_JUNCTION** (corrosion / MOM)
- `|ratio_dB| ≤ hysteresis` → **INCONCLUSIVE** — reported as such, never silently classified

**Honesty note on the discriminator (verifier correction folded in).** The 2f>3f / 3f>2f rule is directionally correct and matches accepted NLJD practice, **but it is not a decisive binary.** A real diode driven hard *also* produces substantial 3rd harmonic, and the ratio is **bias- and drive-level dependent**, so `|ratio_dB|` near 0 dB is common and genuinely inconclusive. Report the ratio as a **continuous discriminator with an explicit inconclusive band around 0 dB** so borderline cases — a real device sitting next to a corroded bracket, or a bias-dependent junction — stay **visible to the operator** rather than being auto-classified. At ~10 mW this decision is only trustworthy for **blatant electronics inside the sealed cavity**.

---

## 3. Frozen frequency plan

The tone is emitted with a **+100 kHz baseband offset** so the transmitted line sits at **915.1 MHz**, dodging the AD9361 direct-conversion **LO-leakage / DC spur** that sits exactly at the LO center. **All detection bins are computed relative to the *actual* emitted 915.1 MHz, not the nominal 915.**

| Role | Nominal | **Emitted / detected line** | Notes |
|---|---|---|---|
| **f0** — TX CW illumination | 915 MHz | **915.100 MHz** | LO 915 MHz + 100 kHz baseband offset; stays off DC/LO spur |
| **2·f0** — primary RX detect (2nd harmonic) | 1830 MHz | **1830.200 MHz** | Semiconductor-dominant harmonic |
| **3·f0** — RX discriminator (3rd harmonic) | 2745 MHz | **2745.300 MHz** | Corrosion/MOM-dominant harmonic |

**Why 915 MHz is the frozen primary (not 1.5 GHz, not 2.4 GHz):**

- **Both harmonics fit in-band with margin.** 1830 and 2745 MHz are both well under the B210's 6 GHz ceiling, so the **full 2f/3f ratio discriminator is available.** (A future 2.4 GHz high-band mode would push 3f to 7.2 GHz — out of band — leaving only the 2nd harmonic and killing the discriminator. That is the decisive reason the primary stays at 915 MHz.)
- **Return strength is the binding constraint at 10 mW.** Junction harmonic-conversion efficiency falls as illumination frequency rises, so the **lowest sensible f0 gives the strongest return.** 915 MHz is the industry-canonical NLJD low-band frequency (REI Orion / NJE and virtually all classic units use 888/915 MHz) for exactly this reason.
- **BOM cost.** 902–928 MHz is the most saturated ISM filter/SAW/antenna market in existence, so the required physical parts are cheap commodity items. A 1.5 GHz plan would need pricier GPS/aero-band parts.

---

## 4. The radio — USRP B210

**Confirmed identity.** The operator's "B220" is the **Ettus Research / NI USRP B210**. No Ettus product is literally named "B220"; every stated spec matches the B210 exactly. The 2 TX / 2 RX description is unique to the B210 (the 1×1 B200 uses the pin-limited AD9364).

| Attribute | Value |
|---|---|
| RFIC | Analog Devices **AD9361** (2×2 direct-conversion transceiver) |
| Channels | 2 TX / 2 RX, full-duplex MIMO |
| Frequency | **70 MHz – 6 GHz** continuous; each LO independently tunable 50 MHz – 6 GHz |
| Instantaneous BW | up to **56 MHz** (61.44 MS/s) |
| FPGA / host link | Xilinx Spartan-6 XC6SLX150 / USB 3.0 SuperSpeed |
| TX output | Absolute-max ceiling +20 dBm/port; realistic max-gain **+6 to +16 dBm**, frequency-dependent. **Uncalibrated — gain-only, no absolute-dBm command.** This build runs ~+10 dBm (~10 mW), well within the linear region. |
| RX max input | ~**−15 dBm** (damage/compression ref) — bounds how much 915 MHz leakage the RX tolerates before it manufactures its own harmonics |
| Driver | **UHD** (`import uhd`), Python bindings |

**LO architecture — the correct mechanism (verifier correction folded in).** The AD9361 has **ONE TX LO shared by both TX channels** and **ONE RX LO shared by both RX channels**; the **TX LO and RX LO are mutually independent**. That **TX-LO/RX-LO independence** — **not** an "A-vs-B synthesizer" split — is what lets the box transmit CW at 915 MHz while the receiver sits at 1830/2745 MHz (standard FDD full-duplex). The A-vs-B channel choice does **not** give independent frequencies.

**Consequence for RX-B (correction folded in).** Because **both** RX channels share the **single** RX LO, RX-B **cannot** sit at 915 MHz to sample the fundamental while RX-A sits at a harmonic. RX-B can only serve as a **same-harmonic common-mode reference tap** — observed at whichever harmonic RX-A is currently dwelling on — for leakage cancellation and phase reference. The "reference tap that watches the fundamental" idea is **physically impossible on this radio** and has been removed.

**Coherence.** All TX/RX chains derive from the B210's common master clock, so the TX and RX are phase-coherent **within a dwell**. **However, phase is NOT preserved across an RX retune** (1830 ↔ 2745): each retune injects a fresh random PLL/VCO-divider phase offset. This is acceptable **because the discriminator compares per-dwell single-bin POWER** (magnitude-squared, phase-independent), so cross-hop phase is never used. See the wording correction in Section 8.

**Full-duplex self-interference is real and documented (honesty item).** On the B210 there is **no isolating RF switch** between the separate TX and RX SMA ports (unlike the N210's ~30 dB switch). Users report that "the moment the TX stream is set up, the RX stream becomes unusable," and nearby TX/RX frequencies cause LO-leakage cross-coupling. **This is the single strongest justification for the mandatory external TX bandpass + RX highpass/LNA.** Software cannot fix on-board TX→RX leakage.

**UHD Python API notes (for TX-CW-while-RX-capturing):**
- One process, **multiple threads** (a USRP cannot be shared across processes). Use a single `uhd.usrp.MultiUSRP()`.
- `set_tx_freq(uhd.types.TuneRequest(915e6), 0)`, `set_rx_freq(uhd.types.TuneRequest(1830e6), 0)`, `set_tx_gain(g,0)`, `set_rx_gain(g,0)`.
- Streamers: `get_tx_stream(StreamArgs('fc32','sc16'))`, `get_rx_stream(...)`. **Use `uhd.usrp.StreamArgs` (with a `try/except` fallback to `uhd.types.StreamArgs` for older UHD)** — the location is version-dependent.
- Run the TX `send()` loop (continuously sending the 100 kHz-offset complex sinusoid = CW at 915.1 MHz) in one thread and the RX `recv()` loop in another; both release the GIL so they genuinely run concurrently.
- **RX `recv()` requires a 2-D buffer** shaped `(num_channels, num_samps)` even for one channel — see the DSP bug fix in Section 8.
- Use `set_command_time` on the shared timebase to align TX start / RX capture.

**Provenance caveat.** Third-party "AD9361 B210-compatible" clones exist (HamGeek, generic Amazon boards). If the unit is a clone, TX flatness, LO leakage, and filtering may be worse than the genuine Ettus/NI part; all numbers above are for the genuine B210. Confirm provenance.

---

## 5. RF signal-chain block diagram

```
                              ┌─────────────────────────────────────────────┐
                              │            SHIELDED FARADAY CAVITY            │
                              │        (ammo can, seam-treated, absorber-     │
                              │              lined; object on pedestal)       │
                              │                                               │
  USRP B210                   │   TX probe                        RX probe    │
 ┌──────────┐   915 MHz       │  (¼λ @915,                       (¼λ @1830/   │
 │  TX-A    │───► BANDPASS ───►│───vertical) ~~~►  [OBJECT]  ◄~~~ 2745, horiz.,│
 │ (915.1)  │    915 MHz SAW   │       ▲   cross-polarized, opposite walls    │
 │          │   (strip SDR's   │       │                          │          │
 │          │    own 2f/3f)    └───────┼──────────────────────────┼──────────┘
 │          │                          │                          │
 │          │              ┌───────────┘  optional RX-B ref tap   │
 │          │              │  (coupler OUTSIDE box, same harmonic  │
 │          │              │   as RX-A — common-mode leakage ref)  │
 │          │              ▼                                       ▼
 │  RX-B ◄──┼───── [DC block] ◄── coupler                 HIGHPASS  (pass 1.8–2.8 GHz,
 │ (ref)    │                                             ~1.8 GHz  reject 915 leak)
 │          │                                                 │
 │  RX-A ◄──┼─────────────────────────────────────────────── LNA ◄┘  (SPF5189Z,
 │(1830/    │                                            (+ atten pad to protect     ~18 dB,
 │  2745)   │                                             AD9361 RX from 915 leak)   0.6 dB NF)
 └──────────┘
```

**TX line:**  `SDR TX-A → 915 MHz bandpass (SAW) → TX probe`
**RX line:**  `RX probe → RX highpass (~1.8 GHz corner) → attenuator pad → LNA → SDR RX-A`
**Optional RX-B:**  `TX-line coupler (outside box) → DC block → SDR RX-B` (same-harmonic common-mode reference only)

The TX bandpass strips the SDR's self-generated 2f/3f **before** the antenna. The RX highpass rejects the strong 915 MHz fundamental leak **before** it desenses/intermodulates the AD9361. **Neither filter can separate the SDR's residual harmonic from the target's harmonic** — that is done by TX-side suppression + baseline subtraction (Section 8).

---

## 6. Bill of Materials

**This is the ONE canonical BOM.** The three earlier draft BOMs in the working notes named different RX-highpass parts (HFCN-1810+ / VHF-1500+ / VHF-1200+) and, in one place, the **$405 ZVBP-909-S+ cavity bandpass** — that part is **removed** here: it costs ~2× the entire budget, was out of stock, and its 902–915 MHz passband puts the 915.1 MHz line at its upper roll-off edge. All parts below are real; prices are current street prices with verification status flagged.

| # | Part | Function | Price (USD) | Running total | Verified? |
|---|---|---|---|---|---|
| 1 | **902–928 MHz SAW bandpass module**, SMA, center 915, IL ~2 dB (e.g. Amazon B0BK2K74V3 / "FBP-915s" class) | TX harmonic scrub (strip SDR's own 2f/3f) | $12.00 | $12.00 | Est. (Amazon blocked auto-fetch) |
| 2 | **Mini-Circuits HFCN-1810+** ceramic LTCC highpass (pass ~1.9 GHz+, stop DC–1.6 GHz) | RX highpass — reject 915 leak, pass both harmonics | $12.23 | $24.23 | **VERIFIED (DigiKey)** |
| 3 | **Qorvo SPF5189Z LNA module**, 50–4000 MHz, NF ~0.6 dB, gain ~18 dB, SMA | RX LNA — recover weak harmonic return (covers BOTH 1830 & 2745) | $10.00 | $34.23 | Est. |
| 4 | **Nooelec SMA attenuator kit**, 6 pc 2 W, DC–6 GHz (1/2/3/6/10/20 dB) | Protect AD9361 RX from 915 leak / TX turn-on | $34.95 | $69.18 | Est. (street ~$33–37) |
| 5 | **RG316 SMA M-M jumpers**, ~15 cm, multipack | Interconnect | $12.00 | $81.18 | Est. |
| 6 | **SMA adapter/coupler kit** incl. tee (for RX-B tap) | Adapters + RX-B reference tap | $10.00 | $91.18 | Est. |
| 7 | **SMA DC block**, 2 W, DC–6 GHz | Protect RX-B from TX/bias DC on the tap line | $13.00 | $104.18 | Est. |
| 8 | **2nd SPF5189Z module** (RX-B reference channel) | **REQUIRED at this budget** (see note) common-mode leakage subtraction | $10.00 | $114.18 | Est. |
| 9 | **DIY shield/cavity + antennas + absorber** (galvanized ammo can + copper-foil/fingerstock seam + lossy foam + 2× DIY monopoles) | Faraday cavity + bistatic probes | $40.00 | **$154.18** | Est. |

### **VERIFIED BOM TOTAL: $154.18 — $45.82 under the $200 ceiling.**

**Why the 2nd SPF5189Z (item 8) is REQUIRED, not optional.** A commodity 902–928 SAW gives only **~20–30 dB** rejection at the octave-up 2nd harmonic (SAW filters have spurious/re-entrant passbands and rarely spec stopband depth an octave out), **not** the 30–45 dB earlier drafts assumed and **far** below the ~60–80 dB a $405 cavity gives. So the SDR's residual self-harmonics are stronger at this price point, which makes the **RX-B common-mode reference subtraction channel effectively mandatory** to reach a usable measurement.

**Honesty flag for the client-facing doc.** This is a **SAW-grade** TX harmonic scrub, **not cavity-grade**. Do not let any spec imply cavity performance at SAW cost. The whole build detects **blatant electronics only**.

**Price-verification gap.** Only item 2 (HFCN-1810+, $12.23 DigiKey) is hard-verified; Amazon returned HTTP 500 to the automated fetcher, so items 1, 3, 4, 5, 6, 7, 8 are typical current street prices. Amazon RF-module prices swing 20–40%. **Confirm the seven estimated line items in-cart before relying on $154.18.** The $45.82 headroom is contingency for those swings and for a shield upgrade.

### Optional upgrades (separate column — NOT in the $154.18 total)

| Part | Why | ~Price |
|---|---|---|
| Mini-Circuits **VHF-1200+** connectorized highpass (1.18 GHz cutoff, solder-free SMA) | Replaces item 2 if you don't want to edge-mount an SMA on the ceramic HFCN | ~$40 |
| Mini-Circuits **VHF-1500+** highpass (passband starts **~1600 MHz**, not the "1700 MHz" in an earlier draft) | Alternate solder-free RX highpass | ~$40 |
| Mini-Circuits **ZX60-272LN-S+** (2.3–2.7 GHz, 0.8 dB NF) | Better dedicated 3f-band LNA | ~$135 |
| Mini-Circuits **ZX60-P33ULN+** (0.4–3 GHz, 0.9 dB NF) | One premium LNA covers both harmonics | ~$145 |
| Mini-Circuits **ZVBP-909-S+** cavity bandpass (902–915 MHz) | Cavity-grade TX scrub — **listed only to document why it was rejected: $405, ~2× budget, out of stock, 915.1 MHz at its band edge** | ~$405 |
| **TitanRF fabric / RadioScreen copper mesh** box | Lab-grade (>60 dB) shield vs. the ~20–40 dB ammo can | ~$30–50/yd |

---

## 7. Shield / cavity + antenna build

**Cavity (recommended): a galvanized/steel .50-cal ammo can (~$20–25).** Bare steel body gives ~20–40 dB at 1–3 GHz; **the lid seam and rubber gasket — not the steel — set the real isolation.** A 1 mm gap wrecks GHz shielding.

**Seam treatment IS the shield design (build-blocking step):**
- Cheapest: **copper-foil tape with conductive adhesive** burnished over the lid-to-body mating edge (+10–20 dB, consumable).
- Permanent upgrade: **beryllium-copper fingerstock or copper braid** compressed 20–30 % on close — this buys the jump from ~30 dB to **50–60 dB**. Continuous metal-to-metal contact all the way around the lid is make-or-break.

**RF absorber foam is functionally required (not decoration).** A bare metal box is a resonant cavity: near its resonances the empty-box RX baseline swings wildly with tiny object movements, destroying the ability to measure a clean 2f/3f return. Line the interior — especially the wall behind the RX probe and the end caps — with **carbon-loaded lossy reticulated foam** (Eccosorb-equivalent from dB Absorber or 3PB Solutions US-series, peaks 0.5–3.0 GHz, which covers our 1.8–2.75 GHz band). Use **flat lossy sheet, not pyramidal anechoic foam** (overkill/expensive for a tiny cavity). Budget ~$25–40.

**Antennas — bistatic pair, no circulator needed.** Two separate DIY probes from SMA panel-mount jacks + solid copper wire (semi-rigid center conductor or 16 AWG), each mounted through a drilled hole so the **can wall is the monopole ground plane**:
- **TX probe:** ¼λ @ 915 MHz ≈ **82 mm** (trim from slightly long to resonance), fed from TX-A, mounted **vertical**.
- **RX probe:** ¼λ in the harmonic band (1830 MHz ≈ 41 mm, 2745 MHz ≈ 27 mm); cut **~34 mm** as a both-harmonics compromise (exact match matters less — the LNA sets sensitivity), mounted **horizontal**.
- **Cross-polarized + opposite walls** gives **15–30 dB of free TX→RX isolation**, critical because the 915 MHz leak is far stronger than the harmonic return. Object sits on a low-loss-foam pedestal in the **center**; probe tips offset so direct TX→RX line-of-sight coupling is minimized while both still couple to the object. Keep tips >¼λ off the absorber and the object.

**RX-B reference tap** is an **RF-chain** element, not a probe: it taps the **TX line via a coupler OUTSIDE the box** (through a DC block) to sample the SDR's own residual leakage at the **same harmonic RX-A is dwelling on**.

**Safety (state plainly).** RF exposure at ~10 mW is negligible — orders of magnitude below any occupational limit even with the box open, and the closed steel cavity contains essentially all of it. **No meaningful RF hazard.** Two operational rules still apply and go in the SOP: (1) the box must stay **CLOSED while transmitting** — not for RF safety but because an open box loses the shield, readmits ambient GHz signals, and invalidates the measurement; software should gate TX behind a **"lid-closed" confirmation**. (2) the object under test must be **de-energized / no live mains** inside a sealed metal box (fire/thermal, not RF).

---

## 8. DSP module design

**Delivered, tested files (in the real app):**
- `C:\Users\Union Terminal\Desktop\Shield_TSCM_Business\spectrum_survey\survey\nljd.py`
- `C:\Users\Union Terminal\Desktop\Shield_TSCM_Business\spectrum_survey\tests\test_nljd.py` (9/9 passing; regression `test_sdr_source.py` still 9/9; `import survey.nljd` works with no UHD installed)

**Structure.** `B210Source(AD936xSource)` inherits the **entire** existing stepped-sweep RECEIVE engine verbatim (so the B210 also works as a normal spectrum source), and **adds**: `nljd_tx_cw()` / `nljd_tx_off()` (background-thread CW streamer on TX-A), `nljd_capture_at()`, and `nljd_hw_filters_attested()`. A `_UHDRxAdapter` shim maps the 6 RX hooks onto `uhd.usrp.MultiUSRP` so the AD936x engine stays driver-neutral. **UHD is imported lazily** (like pyadi-iio in `PlutoSource`). The DSP engine `NLJDEngine` is hardware-agnostic and unit-tested against a synthetic harmonic radio.

**Measurement routine.** Transmit CW at 915.1 MHz → for `n in (2,3)` tune the RX LO so the harmonic lands bin-aligned at a small positive offset (~10 % up from DC, off the RX DC/LO spur) → single large-N FFT (default 65536) → `extract_line_power` returns `(line_dB, noise_floor_dB, snr_dB)` → average `n_avg` dwells in the **power** domain.

**Baseline + delta is the crux that makes 10 mW usable.** `measure_baseline()` runs with the box **CLOSED and EMPTY** to capture the box's own harmonic leakage floor (residual SDR self-harmonics + cavity resonances). `measure()` subtracts it, so the reported `delta_2f` / `delta_3f` is **only what the object added**. We never claim absolute harmonic power (hopeless at this budget), only the object-induced delta — which is why the B210's lack of absolute-dBm calibration is irrelevant **by construction**.

### Verifier corrections folded into the design

1. **RX `recv()` 2-D buffer bug (most likely hard failure at bring-up) — FIXED.** UHD Python `recv()` requires a 2-D `(num_channels, num_samps)` array even for one channel. The adapter now allocates `buff = np.zeros((1, self._nsamps), dtype=np.complex64)`, calls `self._rx.recv(buff, md, timeout)`, and returns `buff[0, :got]`. A 1-D buffer would raise or silently under-fill. Re-verify `recv()` arg count/timeout against the installed UHD version at bring-up.

2. **"Coherent" wording corrected to be technically defensible.** The DSP is **incoherent power detection with FFT bin concentration**, not phase-locked matched filtering. It uses **per-dwell single-bin POWER** (magnitude-squared) and does **not** phase-reference RX to TX. The ~`10·log10(N)` processing-gain **number is correct** (a CW line concentrates in one bin while noise spreads across N), but the docstrings now say **"narrowband FFT processing gain (incoherent power detection)"** — not "coherent processing gain." This is turned into a stated **strength**: because it never uses cross-frequency phase, it is **immune to the PLL phase reset on every 1830↔2745 retune.**

3. **RX-overload guard added (the most important physics gap).** Because the filters correctly suppress the SDR's own 2f/3f, the empty-box baseline line is small — so the delta method attributes any rise to the object. **But if the RX front end is compressing/intermodulating on residual 915 MHz leakage (RX max input ~−15 dBm), the RX manufactures its own 2f/3f that scales with total received power** — which rises when *any* reflective/resonant object (even a plain conductor) is inserted, and would falsely read as SEMICONDUCTOR. Neither the SNR gate nor the delta gate caught this. **Now:** baseline records total in-window RX power; at measure time, if inserting the object raises **total** received power past a threshold **OR** 2f and 3f rise **together by similar amounts** (broadband RX self-intermod signature, not a junction), the verdict downgrades to **INCONCLUSIVE** with plain text *"possible RX overload — reduce RX gain / add attenuation, re-baseline."* A rising-total-power case can no longer silently classify as SEMICONDUCTOR. (Bring-up step 3 remains the manual electrical confirmation.)

4. **RX-B comment corrected.** RX-B shares the single RX LO with RX-A, so it can only be a **same-harmonic common-mode reference** — it **cannot** monitor the 915 fundamental. The "fundamental leakage tap" framing is removed from code comments and spec.

5. **`StreamArgs` location made version-robust** — `uhd.usrp.StreamArgs` with a `try/except` fallback to `uhd.types.StreamArgs`.

6. **NLJD holds its own sample rate.** The inherited `capture_iq` restores the 20 MHz sweep `fs` after every call, thrashing 8 rate-flips per measurement against the 2 MHz NLJD rate. The NLJD path now holds `self._fs` at `nljd.rx_sample_rate` for the whole measurement and restores once at the end.

7. **Negative-delta + overload regimes now tested.** Tests added for the object *dropping* the leak line (deltas can go negative — `classify()` must behave) and for the both-harmonics-rise/overload case.

**Decision rule (`classify()`):** `ratio_db = delta_2f_db − delta_3f_db`; `> +hysteresis` → SEMICONDUCTOR, `< −hysteresis` → FALSE_JUNCTION, `|ratio| ≤ hysteresis` → INCONCLUSIVE. Ratio surfaced as a **continuous dB discriminator**, never silently binarized.

**Honesty gate (three explicit refusal paths, all tested):**
- `require_hw_filters=True` and the operator has **not** attested the physical filters are installed → verdict **BLOCKED** (plain text: software cannot substitute for the filters). This is an **operator attestation**, not an electrical verification — software genuinely cannot confirm the filters exist; bring-up step 3 is the human proof.
- Neither harmonic clears `snr_gate_db` (default 6 dB) above its noise floor → **NO_JUNCTION** (never a fabricated hit).
- `confident` additionally requires the object return to rise ≥ `delta_gate_db` (default 3 dB) **above** the empty-box baseline. Every NO_JUNCTION string states a null is **not proof the object is clean at this TX power.**

**Endpoints to add in `survey/server.py`** (mirror the existing `/api/inspect` + `/api/device/connect` pattern; acquire the controller device lock, guard that `dev is B210Source` and not busy, degrade gracefully when the B210 isn't connected):
- `POST /api/nljd/baseline {}` → runs `measure_baseline()`, stores on the controller/session, returns `baseline.to_dict()`.
- `POST /api/nljd/measure {}` → requires a prior baseline; returns `NLJDVerdict.to_dict()` `{verdict, plain, ratio_db, delta_2f_db, delta_3f_db, harmonics[], confident, hw_filters_attested, warnings}`.
- `POST /api/nljd/hw_filters {installed:bool}` → `dev.set_hw_filters_attested(v)` (persists to `nljd.hw_filters_installed`).
- Also wire source alias `'b210'` into `survey/sweep/device.py` `open_device()` alongside the pluto/sdr/ad936x branch (`from .nljd import B210Source`).

**UI card ("Bench NLJD — junction test chamber"; plain-language, no surprise modes — every state change is an explicit operator click):**
1. Checkbox **"TX bandpass + RX highpass filters installed"** (bound to `/api/nljd/hw_filters`, **unchecked by default**), gates the Measure button and shows the physics reason when unchecked.
2. **Step 1 — Baseline empty box** button (`/api/nljd/baseline`) with status line *"empty-box reference captured HH:MM"* and a **staleness indicator** (baseline invalidates if TX/RX gain, cabling, or chamber changes between baseline and measure).
3. **Step 2 — Measure object** button (disabled until baseline exists) → renders the verdict as a colored plain-language sentence plus a readout of 2f/3f ratio (dB), delta-2f, delta-3f, and per-harmonic SNR.

**Config keys** (add to `survey/config.py` DEFAULTS): `nljd.{f0_hz=915e6, tx_bb_offset_hz=100000, rx_sample_rate=2e6, tx_sample_rate=2e6, fft_size=65536, dwell_samples=262144, snr_gate_db=6.0, delta_gate_db=3.0, ratio_hysteresis_db=1.0, averages=4, require_hw_filters=true, hw_filters_installed=false}`; `b210.{device_args='type=b200', tx_gain_db=70.0}`.

**Test plan** (`tests/test_nljd.py`, unittest, no hardware): `FakeNLJDRadio` fabricates harmonic IQ (empty-box self-leak line + operator-inserted object profile). Covers line extraction vs. noise; the three decision branches; end-to-end SEMICONDUCTOR / FALSE_JUNCTION / NO_JUNCTION / BLOCKED; and (added) the negative-delta and RX-overload regimes. The test models the real workflow: baseline taken with the empty-box radio, object profile inserted before `measure()`, so delta subtraction is genuinely exercised.

**Untested-against-hardware caveat.** The UHD TX/RX paths (`nljd_tx_cw` pump thread, `_UHDRxAdapter.recv`, TX burst metadata) follow standard UHD idioms but have **not** been run against real hardware or a live UHD install. Only the numpy DSP + decision logic are hardware-independently tested. Treat the exact streamer/metadata calls as needing a hardware smoke test — API details vary by UHD version.

---

## 9. Bring-up & calibration procedure (empty-cavity baseline FIRST)

1. **Install UHD** + Python API (`pip install uhd`), run `uhd_images_downloader` for FX3/FPGA images, confirm `uhd_usrp_probe` sees the B210 on USB 3.0.
2. **Loopback sanity, no chamber:** attenuated cable TX-A → RX-A; confirm `nljd_tx_cw` produces a clean line at **915.1 MHz** and RX sees it.
3. **Install the physical filters and PROVE they work (this is what justifies checking the "filters installed" box):** with TX on and RX at 1830/2745, confirm the self-harmonic line **DROPS substantially** versus no-filter. **Then confirm the RX is not compressing on 915 leakage** — this is the manual defense against RX self-intermod faking a detection (Section 8, correction 3). Adjust the RX attenuator pad until the RX is clearly linear on the 915 leak.
4. **Empty closed chamber → run BASELINE.** Re-measure the empty box → expect **NO_JUNCTION** (and confirm total-power baseline is recorded).
5. **Drop in a known powered-off electronic** (any board with an IC) → expect **SEMICONDUCTOR, 2f > 3f.**
6. **Drop in a rusty bolt / corroded bracket** → expect **FALSE_JUNCTION, 3f > 2f.**
7. **Tune** `tx_gain_db` and `averages` for the strongest stable delta without DAC clipping (tone amplitude held at 0.5 full-scale in code for filter/coupler headroom).
8. **Re-baseline whenever anything changes** — TX/RX gain, cabling, or the chamber. The B210 is uncalibrated and frequency-dependent; a stale baseline makes the delta meaningless. If live bring-up shows LO/thermal drift moving the line off its FFT bin, widen the line search to a small peak-find window around the expected bin.

---

## 10. Honest limitations & when to use a real NLJD instead

**Limitations (repeat to every client):**
- **~10 mW illumination is 20–30 dB below a commercial NLJD.** Detects **blatant electronics inside the sealed cavity only.** Misses small, RF-shielded, or deeply-buried junctions. **NO_JUNCTION is not proof the object is clean.**
- **SAW-grade (not cavity-grade) TX harmonic scrub** at this budget (~20–30 dB, unguaranteed an octave out). The RX-B common-mode reference channel is **required**, not optional, to compensate.
- **No filter separates the SDR's residual harmonic from the target's harmonic** — same frequency. Separation is done only by TX-side suppression + empty-box baseline subtraction. The output is always an **object-induced delta**, never absolute power.
- **The discriminator is continuous and often inconclusive** near 0 dB (bias/drive dependent). Borderline ratios are *inconclusive*, not *clear*.
- **RX self-intermod on 915 leakage can fake a semiconductor hit** if the RX is overloaded. The software overload guard downgrades suspicious cases to INCONCLUSIVE, but the electrical fix (attenuation, re-baseline) is the operator's.
- **Uncalibrated radio; no vendor NF/phase-noise.** Any sensitivity number is *measured-on-this-unit*, never a datasheet claim. **Verify unit provenance** (clone vs. genuine B210).
- **Bench-only.** Not an in-place room sweep.

**When to reach for a real NLJD instead:**
- **Any live counter-surveillance sweep of a room, wall, vehicle, furniture, or body** — this chamber cannot scan in place; use an REI Orion-class unit.
- **When a *negative* result must be trustworthy** (clearing a space before a sensitive meeting). This tool's null is weak evidence; a watt-class NLJD's null is meaningful.
- **Small, low-junction-count, or RF-shielded targets** (a single SIM-sized implant, a shielded module) — below this tool's blatant-only sensitivity.
- **When you need a defensible chain-of-custody / court-grade finding** — use certified commercial equipment with published specs.

**Where this tool earns its keep:** a **cheap, honest bench triage** for screening seized/suspect objects for **obvious** electronics before committing a real NLJD or lab, and as a hands-on teaching rig for the 2f/3f physics — as long as nobody oversells its null result.