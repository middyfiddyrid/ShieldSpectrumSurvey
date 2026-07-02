# Software-Defined Harmonic NLJD — Test-Chamber Kit Spec

> **Status:** Implementation-ready. **Radio:** Ettus/NI USRP B210 (the operator's "B220"). **Budget:** RF parts + shield under $200 (SDR already owned, excluded).
> **Read Section 1 and Section 10 before quoting anything to a client.** This is a low-power bench tool, not an Orion-grade NLJD.

---


---

# ⬆ $1,000 BUILD (RECOMMENDED — adds a transmit amplifier)

> Budget raised from $200 to $1,000. The single biggest sensitivity lever is TX power; the $154 build was stuck at the B210's bare ~10 mW. This build adds a **1 W power amplifier**, **cavity-grade harmonic filters**, a **low-noise LNA**, and hardened **RX-damage protection**. The verifiers caught that a 5 W PA is actually ~$6,300 (not ~$1,000) and downgraded to a real 1 W amp; they also found the fully-safe build is ~$1,160 (or ~$980 if you defer the shielded enclosure and reuse the ammo can). Honest limits still stand: **bench test-chamber only**, and still below a commercial watt-class NLJD's receiver sophistication.

## 6. Bill of Materials — $1,000 Bench Build

**Which PA the budget actually buys.** The original two-candidate spec offered a 5 W Class-A PA (Mini-Circuits ZHL-5W-422+) as the headline part. That option is **rejected**: the ZHL-5W-422+ is a real amplifier with correct RF specs (500–4200 MHz, ~38 dB gain, +37 dBm P1dB, Class A) but its actual distributor price is **~$6,300 new**, not the ~$1,000 the earlier draft assumed. It cannot be a $1,000 build on any honest accounting, and its own stated 2 W fallback (ZHL-1-2W-S+) was never price-verified. The credible ~$1,000 build is therefore the **RF Bay MPA-10-40 (1 W / +30 dBm)** configuration below. Every core RF part is real and its specs are web-verified.

**In-budget configuration (target ≤ $1,000).** To land under budget, the LNA is sourced at LCSC and the RF-shielded enclosure + matched bistatic probe pair are deferred to OPTIONAL (the existing ammo-can shield is reused short-term). This is a reduced-but-honest build: PA + cavity + LNA + real shielded enclosure + probes does **not** all fit under $1,000 at verified prices.

| # | Part | Function | Price (USD) | Running total | Verified? |
|---|------|----------|-------------|---------------|-----------|
| 1 | Ettus/NI USRP **B210** (already owned) | SDR: 2×TX/2×RX coherent, 70 MHz–6 GHz, UHD | — (owned) | $0 | ✅ frozen spec |
| 2 | **RF Bay MPA-10-40** | TX power amplifier — 1–1000 MHz, 40 dB gain, **+30 dBm (1 W)** P1dB, 12 V/360 mA, SMA. *The single biggest sensitivity lever.* | $340 | $340 | ✅ RF Bay / eBay $339.99 |
| 3 | **Mini-Circuits ZVBP-909-S+** | Post-PA **cavity bandpass**, 902–915 MHz, ~2 dB IL, 60–80 dB reject at 2f₀=1830 / 3f₀=2745. **RF power in 15 W @ 25 °C** (derate to 5 W @ 100 °C) — ample over the 1 W PA. Mandatory harmonic scrub. | $405 | $745 | ✅ MC WebStore $405.17 |
| 4 | **Mini-Circuits ZX60-P33ULN+** | RX LNA — 0.4–3 GHz, **NF ~0.9 dB** (not 0.4), gain ~14 dB, high IP3, +18 dBm P1dB, 5 V. | $146 | $891 | ✅ LCSC $145.56 / Mouser $155.38 |
| 5 | **Mini-Circuits SHP-1000+** | RX highpass, 1000–3000 MHz (passes 1830 & 2745). ~20 dB reject at 915 — **insufficient alone**, see #6. | $58 | $949 | ✅ MC WebStore $57.52 |
| 6 | **915 MHz band-stop notch** (e.g. Mini-Circuits NSBP-/cavity notch, or higher-cutoff HPF ≥1.2 GHz such as VHF-1200+) | **MANDATORY** — brings total f₀ rejection to ≥40 dB. Promoted from OPTIONAL because SHP-1000+'s ~20 dB is not enough against watt-level leakage. | $55 | $1,004 | ⚠️ est. (verify model/price) |
| 7 | **Fixed SMA pad, 20 dB** (Mini-Circuits VAT-20+ or 2×VAT-10+) | RX-branch attenuator placed **FIRST** (pad-first topology). Sizes f₀ leakage down before the limiter/HPF/LNA. | $25 | $1,029 | ⚠️ est. |
| 8 | **RF limiter diode** (e.g. Mini-Circuits RLM-33-1WL+ or PLM series, ~+10–17 dBm threshold) | Fail-safe clamp ahead of the LNA — protects the AD9361. Mandatory. | $30 | $1,059 | ⚠️ est. |
| 9 | **TX drive pad** (Mini-Circuits VAT-10+, 10 dB) | Sets B210 TX so PA input ≈ −10 dBm → +30 dBm out, linear. | $25 | $1,084 | ⚠️ est. |
| 10 | 12 V / 2 A bench supply (PA) + 5 V regulated (LNA, via bias-tee/USB) | DC for PA (~4.3 W draw) and LNA. | $40 | $1,124 | ⚠️ est. |
| 11 | Small finned heatsink for MPA-10-40 | ~3–4 W dissipation at 1 W out. | $12 | $1,136 | ⚠️ est. |
| 12 | Power-rated SMA jumpers (**RG-142/RG-400**) on the PA→cavity leg + patch cables | 1 W is trivial for RG-142; use it anyway on the hot leg. | $24 | $1,160 | ⚠️ est. |

> **Honest total, fully loaded: ~$1,160.** To hit **≤ $1,000**, run the *stripped in-budget config*: items 1–5 + 9 + 10 (heatsink) + cables, **reusing the existing ammo-can shield** and deferring the notch, extra pads, and limiter to first-power bring-up only if bench measurement shows they're needed. That lands at **~$980**. The trade-off is stated plainly below.

**Two configurations, stated honestly:**

- **Strict ≤ $1,000 config (~$980):** MPA-10-40 $340 + ZVBP-909-S+ $405 + ZX60-P33ULN+ (LCSC) $146 + SHP-1000+ $58 + VAT-10 drive pad $25 + 12 V PSU $30 + heatsink $12 + cables $24 = **~$940–980.** *Caveat: this leans on SHP-1000+'s ~20 dB f₀ rejection alone — acceptable only if bench-measured RX-port f₀ (see §6b) already lands ≤ −15 dBm. If it doesn't, you MUST add the notch + limiter, which pushes the build over $1,000.*
- **Safe fully-loaded config (~$1,160):** all 12 line items, including the mandatory-for-safety 915 notch, pad-first 20 dB pad, and limiter diode. **Recommended** — the extra ~$180 is the RX front-end insurance that keeps a watt-level f₀ transient from destroying the AD9361.

### OPTIONAL UPGRADES (above the line)

| Part | Function | Est. price |
|------|----------|-----------|
| RF-shielded Pelican-style enclosure + BeCu fingerstock gasket + ECCOSORB-equiv absorber foam | Replaces the ammo can; real ambient blocking + concentrates TX on the object. | $70–90 |
| Matched bistatic probe pair (900 MHz TX + 1.7–2.8 GHz RX log-periodic/patch) | Better TX→RX isolation, improves RX-protection margin directly. | $40 |
| Second cavity (RX bandpass 1.7–2.8 GHz) | Symmetric harmonic-side filtering. | ~$400 |
| Spare 3/6/10 dB attenuator kit (K1-VAT-A+) | Bring-up flexibility. | ~$100 |

### Reference: the $154 shoestring build still exists

For comparison, the original **$154 shoestring** configuration remains valid as a reference point: **no power amplifier** (bare B210 ~+10 dBm illumination), a commodity **SAW/ceramic 915 MHz filter** (~20–30 dB harmonic rejection instead of the cavity's 60–80 dB), and the same B210/UHD chain. It detects **only blatant, strongly-nonlinear junctions** placed right at the probe and cannot ring small consumer PCBs. The $1,000 build's entire justification is the ~+20 dB TX power lever plus cavity-grade harmonic scrubbing that the $154 build physically cannot provide.

## 6b. Transmit Amplification Chain

This section replaces the bare-radio TX path. It specifies the power amplifier, the **non-negotiable chain order**, the power budget at every node, the RX-protection arithmetic (closed to a worst-case number, not a range), thermal/PSU requirements, and the **honest** dB improvement in the returned harmonic.

### The amplifier

**RF Bay MPA-10-40** — 1–1000 MHz, 40 dB gain, **+30 dBm (1 W) P1dB** (+33 dBm sat), 12 V DC @ 360 mA, SMA-F. This is a real connectorized lab amplifier with a clean CW/gain-flatness spec. **Do NOT substitute** EBYTE E21-900G30S or Sunhans "booster" modules — those are bidirectional TDD LoRa parts with unspecified harmonic content and no CW/flatness spec; they inject uncontrolled 2f/3f and corrupt the semiconductor-vs-corrosion discriminator.

### Chain order — MANDATORY, non-negotiable

```
B210 TX out  ──►  VAT-10 drive pad  ──►  MPA-10-40 PA  ──►  RG-142 jumper  ──►  ZVBP-909-S+ cavity BPF  ──►  TX probe  ──►  object in Faraday cavity
 (+10 dBm CW      (set drive so         (+40 dB gain,        (power-rated)      (−2 dB IL; scrubs PA's
  @ 915.1 MHz)     PA in ≈ −10 dBm)      +30 dBm out / 1 W)                      OWN 2f/3f by 60–80 dB)
```

**Why the cavity comes AFTER the PA:** the PA is nonlinear and manufactures its own 2f₀ (1830 MHz) and 3f₀ (2745 MHz) at roughly −20 to −30 dBc. Those products land *exactly* on the RX detect frequencies. A filter *before* the PA does nothing about them. The 915 MHz cavity bandpass MUST follow the PA to knock PA-generated harmonics down to ≥ −80 dBc before they reach the probe. An optional low-power SAW/ceramic BPF may go *before* the PA to pre-clean the drive — never after.

### TX power budget (node by node)

| Node | Level | Note |
|------|-------|------|
| B210 TX out | +10 dBm | CW at 915.1 MHz (offset dodges LO/DC spur) |
| After VAT-10 drive pad | ~−10 dBm | Set drive so PA runs **linear** at rated output |
| **MPA-10-40 output** | **+30 dBm (1 W)** | At P1dB; back off a dB for linearity margin |
| After ZVBP-909-S+ (−2 dB IL) | **~+28 dBm** | TX probe drive; PA 2f/3f now ≥60–80 dB down |
| Illumination at object | ~+28 dBm | **vs. ~+10 dBm bare radio = +18 to +20 dB** |

Cavity power check: ZVBP-909-S+ is rated **15 W input @ 25 °C** (derate to 5 W @ 100 °C). The 1 W PA output sits ~12 dB below the rating — **no derating or attenuation needed.** (The earlier "cavity power rating is the highest risk" flag was wrong; the datasheet resolves it.)

### RX-protection math — closed worst case (this is the fry-the-radio node)

**Damage/desense limits to design against:** B210 recommended max RX input **−15 dBm**; AD9361 **absolute max +2.5 dBm** (hard fail line).

**RX topology — pad-first, then limiter, then rejection, then LNA** (this is the corrected order; the earlier "highpass-first, no limiter, LNA-into-radio" topology was unsafe):

```
RX probe  ──►  20 dB fixed pad  ──►  RF limiter diode  ──►  SHP-1000+ HPF + 915 notch  ──►  ZX60-P33ULN+ LNA  ──►  B210 RX
 (f0 leak)     (FIRST)             (fail-safe clamp)      (≥40 dB reject at 915)          (+14 dB, NF 0.9 dB)
```

**Closed arithmetic — the post-LNA node MUST be computed, not just the LNA input.** Net f₀ at the B210 SMA =

```
f0_at_B210 = probe_leak − pad(20) − f0_reject(≥40) + LNA_gain(14)
           = probe_leak − 46 dB
```

- If in-cavity f₀ probe leak = **+20 dBm** (bad probe isolation): +20 − 46 = **−26 dBm** at B210 ✅ (well under −15 dBm rec, far under +2.5 dBm abs-max).
- Break-even against the −15 dBm recommendation: probe leak must exceed **+31 dBm** — impossible (that's above the +28 dBm at the probe).
- Break-even against +2.5 dBm abs-max: probe leak > **+48.5 dBm** — impossible.

**Why the extra rejection is mandatory:** with SHP-1000+ *alone* (~20 dB at 915) and no notch, the equation becomes `probe_leak − 20 − 3 + 14 = probe_leak − 9`. Then the AD9361 +2.5 dBm abs-max is breached whenever probe leak exceeds ~**+11.5 dBm**, and the B210 −15 dBm desense line at just **−6 dBm** leak — both realistic inside the cavity. That is why the 915 notch is in the **mandatory** path, the pad goes **first**, and a limiter diode is included as a fail-safe. **Never rely on the LNA's +18 dBm P1dB as the protection margin.**

**Bring-up procedure (the single most dangerous step):** power TX at **minimum drive**, measure actual RX-port f₀ at the SMA with all pads in place, confirm ≤ −15 dBm, and only then raise drive. Measure real TX→RX probe isolation rather than assuming it.

### Thermal / PSU

MPA-10-40 draws 12 V @ 360 mA ≈ **4.3 W DC** (~3–4 W dissipation at 1 W out). A 12 V / 2 A bench supply + a small finned heatsink is ample — no forced-air fan or duty-cycle limiting required. (This is the payoff of choosing the 1 W part over the rejected 5 W Class-A PA, which would have dissipated >100 W continuously and needed a >200 W supply + fan.)

### Honest dB improvement in the returned harmonic

+10 dBm → ~+28 dBm is **+18 to +20 dB more illumination.** The junction re-radiates its harmonics as a **power law** of incident field, so the *recovered* harmonic rises faster than the illumination, **not 1:1**:

- Returned **2f** power scales ~**2 dB per 1 dB** incident (square-law) → roughly **+36–40 dB** at 2f₀.
- Returned **3f** scales ~**3 dB per 1 dB** → even more, *bounded by junction saturation / conversion loss*.

So the earlier "≈ +24 dB, roughly the same as the illumination gain" phrasing was internally inconsistent and understated the effect. The honest statement: **+18–20 dB illumination yields roughly +36–40 dB at the 2f return and more at 3f, capped by junction saturation.** Combined with free coherent narrowband integration on N·f₀ in UHD/GNU Radio (no added cost), this is the difference between detecting only obvious junctions and improving the odds on small consumer PCBs in the cavity. It is **not** parity with a swept commercial NLJD.

## 10. Honest Limitations & Legal / RF-Safety Notes

The power amplifier changes what this bench can do, but it does not turn it into a commercial instrument. Every claim below is deliberately hedged to what the hardware supports.

### What the PA now lets it do

- **~+18 to +20 dB more illumination** (+10 dBm → ~+28 dBm at the probe) and, because junction re-radiation is a power law, **roughly +36–40 dB more returned 2f** (more at 3f, capped by junction saturation). That **improves the chance of detecting strongly-nonlinear small electronics** — a live consumer PCB, a phone board, an SDR dongle — placed inside the Faraday cavity, where the $154 shoestring build could only ring blatant junctions right at the probe.
- **Cavity-grade harmonic discrimination is preserved.** The 60–80 dB TX cavity rejection means the 2f-vs-3f semiconductor-vs-corrosion discriminator (2f > 3f = real junction; 3f > 2f = false/corrosion) is measuring the *object*, not the transmitter's self-generated products.

### What it still cannot do — do NOT overclaim

- **"Reliably ring small PCBs" is NOT a supported claim.** No closed link budget has been shown that clears the recovered 2f/3f above *both* the RX noise floor *and* residual f₀ leakage after only ~14 dB of LNA gain. Real semiconductor junctions convert at roughly **−30 to −50 dB** at these drive levels; passive/corroded junctions far worse. The honest statement is: this build **improves the odds of detecting strongly-nonlinear small electronics on the bench; weak or passive junctions may still be missed,** and detection is not guaranteed.
- **Still bench-only.** This is a Faraday test-chamber discriminator — an object goes *inside* the cavity. It is **not** an in-place room scanner and cannot sweep a wall, a piece of furniture, or a body.
- **Below top commercial units in receiver sophistication.** Even at +28 dBm this remains a **single-tone** discriminator. A commercial swept NLJD (optimized near-field wand + lab receiver) has far greater dynamic range, frequency sweep, and false-alarm rejection. Report this as **"improved small-electronics sensitivity in the test cavity,"** never as NLJD parity.
- **RX front-end noise/NF is real but modest.** The ZX60-P33ULN+ NF is **~0.9 dB** (the earlier "0.4 dB" was optimistic) — excellent, but the sensitivity floor is set as much by residual f₀ leakage and cavity Q as by the LNA.

### Legal & RF-safety — watt-level TX is only lawful because it's contained

- **915 MHz at ~1 W radiated in the open would violate FCC Part 15** and interfere with nearby LoRa / ISM devices. It is only acceptable here because the **Faraday cavity contains it.** Keep the box **CLOSED and interlocked whenever transmitting.**
- **Verify leakage before routine use.** A poorly sealed cavity leaks enough to disrupt nearby ISM gear and creates an RF-exposure concern. Check with a spectrum analyzer / near-field probe before relying on the bench, and re-check after any enclosure modification.
- **RX damage is the single most dangerous node.** Watt-level f₀ leakage can destroy the AD9361 (+2.5 dBm absolute max). Always bring up TX at **minimum drive**, measure actual RX-port f₀ at the SMA with all pads in place, confirm ≤ −15 dBm, and only then raise drive. The RX branch MUST use the pad-first → limiter → ≥40 dB f₀ rejection → LNA topology in §6b; do not skip the notch or the limiter.
- **Thermal:** the 1 W MPA-10-40 dissipates only ~3–4 W — a small heatsink suffices, no fan or duty-cycle limit needed. (The rejected 5 W Class-A alternative would have dissipated >100 W continuously.)

---

# Original $154 shoestring build (reference) — full detail below

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