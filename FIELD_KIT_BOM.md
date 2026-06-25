# Field Demod + Portable TSCM Kit — Bill of Materials (starter)

## Core Add-on (Cheapest Path to Listen)
| Item | Approx. Cost (USD) | Notes / Links (search) |
|------|---------------------|------------------------|
| RTL-SDR Blog V4 (aluminum case, SMA, TCXO, Bias-T) | 35–45 | Primary cheap demod receiver. 500 kHz–1.766 GHz covers 90%+ of practical bug bands. |
| SMA pigtails / adapters (2–3x 6-12" male-male or male-female) | 8–15 | Reuse your directional + omni. Match RF Explorer SMA-F. |
| Short USB 3.0 cable (shielded, 1–2 ft) | 5–10 | Reduce noise. |
| Small powered USB hub (4+ port, PD pass-through preferred) | 15–25 | Powers SDR + Explorer stably from tablet bank. |
| Closed-back headphones or quality earbuds | 15–40 | (You may already own.) Critical: hear the demod, not room noise. |
| **Subtotal add-on** | **~$80-130** | Start here tomorrow. |

## Portable Compute (replace the tower)
| Option | Cost | Recommendation |
|--------|------|----------------|
| Used/refurb Windows 11 2-in-1 or detachable (Surface Pro/Go class, 8+ GB RAM, USB-C) | 350–650 | **Best first choice.** Full compatibility with app.py, SDR#, URH, everything. |
| Budget new Windows tablet (various Chuwi / Teclast / etc. with good reviews) | 250–450 | Test USB stability + power delivery. |
| Raspberry Pi 5 (8 GB) + 7–10" touch display kit + SD + basic case | 180–280 | + power bank separate. Lighter, silent. Use SDR++ or GQRX. |
| **Power bank** (20000 mAh+ PD 65W+ output, Anker or equivalent) | 40–70 | Runs full kit all day. |
| **Subtotal portable compute** | **400–750** | Dramatic improvement over tower. |

## Nice-to-Have / Next Level
| Item | Cost | Purpose |
|------|------|---------|
| HackRF One | ~330 | Full 1M–6 GHz to match Explorer; future-proof + GNU Radio power. |
| PortaPack (for HackRF) | 120–220 | Standalone handheld listen without tablet. |
| Manual SMA coax switch (2-port) | 20–40 | Fast flip between sweep and listen without unplugging. |
| SDRplay RSPdx or RSP1B | 150–300 | Upgrade receiver perf (dynamic range) if RTL overloads in urban. |
| Small Pelican / Apache / similar hard case + foam or velcro | 30–80 | Organized kit that survives travel. |
| LNA (bias-T powered, 0.1–2 GHz or wide) + attenuator set | 25–50 | Weak signals or very strong local broadcast. |
| Foldable / compact backup antenna (telescopic or small discone) | 20–40 | Redundancy. |

## Total Realistic Starter Field Kit (no tower)
- RTL path + good Windows tablet/laptop + power + case + cables: **~$550–850**
- Includes everything needed to do full sweeps + demod/listen on site with one carry bag.

## Antennas (you already have)
- Directional (use for bearing + max signal)
- Omni (ambient + quick checks)
Keep using them. Just move the SMA connector to the SDR when you want to listen.

## Software (all free or already in use)
- Existing: spectrum_survey (app.py) — now augmented conceptually by this design.
- SDR#: primary for RTL on Windows.
- SDRuno (if choosing SDRplay).
- SDR++ or GQRX (cross platform / Pi).
- Universal Radio Hacker (URH) — for recording a burst then reverse-engineering digital protocols.
- (Optional later) GNU Radio flowgraphs for custom.

## Suggested Purchase Order
1. RTL-SDR Blog V4 + 2 pigtails (order today).
2. Decent powered USB hub + short cables.
3. Test on current laptop: install Zadig + SDR# + tune a known signal from recent wiki.
4. Buy the tablet / 2-in-1 or Pi kit.
5. Case + organization.
6. Later: HackRF if you want one-box full-band coverage for both detection and demod.

Once RTL arrives we can add the first "copy frequency + suggested demod mode" helper directly into the wiki/results UI and a simple launch script.

This stack keeps the strengths of the RF Explorer (precise, dual-module, calibrated sweeps + beautiful fingerprints) while adding the missing "listen to confirm" capability that turns unknown shapes into identified threats or cleared emitters.