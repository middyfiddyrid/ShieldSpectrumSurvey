"""Passive emanation detector — find a hidden device by the RF it LEAKS, without
transmitting anything and without it ever intending to be heard.

THE GAP THIS FILLS
==================
The interior sweep finds signals a device deliberately transmits. The active-ping
(survey.stimulate) finds devices that REACT to a stimulus. Neither reliably catches
the worst case: a small camera or voice recorder that only writes to an SD card and
never transmits, sitting silent and un-pingable. Those devices still LEAK — their
clocks, sensors, and switching supplies radiate faint, unintentional narrowband
tones. This module hunts those leaks.

METHOD (after RFScan, Sun/Givehchian/Bharadia, PoPETs 2025 — the paper the operator
cited; this is a pragmatic re-implementation of its passive core, NOT the DNN):
  1. NON-COHERENT AVERAGING. Emanations are weak. Average many PSD captures of the
     same window (power domain) so the noise averages down and a stable faint tone
     climbs out. (The paper averages ~500 sweeps; we make it configurable.)
  2. SMOOTHED NOISE FLOOR. A running-median floor per band, so a spike is judged
     against its LOCAL floor (which varies across the spectrum), not a global one.
  3. BASELINE SUBTRACTION (optional). If a reference capture of the same band exists
     (a prior clean sweep of this address, or the room with the suspected device's
     area powered down), subtract it so ambient comms/known emitters drop out and
     only what's NEW remains.
  4. HARMONIC-COMB TEST — the crux. A single device's emanation is not one tone but
     a COMB of tones evenly spaced by its clock frequency (e.g. a camera sensor at
     24/48 MHz shows spikes every 24 MHz). Ambient Wi-Fi/LTE/broadcast are wide or
     irregular, NOT evenly-spaced narrowband combs. So we group detected spikes into
     harmonic combs; a comb of >=`min_harmonics` tones is the signature of ONE
     concealed emitter, and the spacing estimates its clock.

HONESTY (goes in front of clients; do not oversell):
  * Emanations are FAINT. This wants a close antenna and a relatively quiet RF spot;
    thick walls, distance, and a noisy environment can bury a real device. A null
    result is not proof of a clean room — it is one more independent look.
  * We require a real COMB (>=3-4 harmonics), never a lone spike, so we do not cry
    wolf on every stray tone.
  * The clock estimate hints at device type; it is a lead to localize, not an ID.

Pure numpy DSP — unit tested in tests/test_emanations.py. The scan engine drives any
SDR that exposes capture_iq (Pluto / B220), so no new radio protocol is needed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, asdict, field
from typing import Optional

import numpy as np

from .sweep.sdr_source import welch_psd


# ---------------------------------------------------------------------------
# Result records
# ---------------------------------------------------------------------------

@dataclass
class Spike:
    freq_mhz: float
    prominence_db: float

    def to_dict(self):
        return asdict(self)


@dataclass
class EmanationComb:
    """A set of evenly-spaced tones = one suspected concealed emitter."""
    clock_mhz: float                 # estimated clock/spacing
    harmonics_mhz: list              # the tone frequencies in the comb
    n_harmonics: int
    strength_db: float               # mean prominence of the comb's tones
    device_hint: str                 # plain-language guess from the clock
    plain: str

    def to_dict(self):
        return asdict(self)


@dataclass
class EmanationResult:
    verdict: str                     # SUSPECTED_EMITTER | NONE | INCONCLUSIVE
    plain: str
    combs: list                      # [EmanationComb, ...]
    spikes: list                     # [Spike, ...] all detected (for display)
    band_mhz: tuple
    averaged_sweeps: int
    had_baseline: bool
    ts: float = field(default_factory=time.time)
    warnings: list = field(default_factory=list)

    def to_dict(self):
        d = asdict(self)
        d["combs"] = [c if isinstance(c, dict) else c.to_dict() for c in self.combs]
        d["spikes"] = [s if isinstance(s, dict) else s.to_dict() for s in self.spikes]
        return d


# ---------------------------------------------------------------------------
# Pure DSP
# ---------------------------------------------------------------------------

def average_psd_db(psds_db: list) -> np.ndarray:
    """Non-coherent average of N PSDs (average in the LINEAR power domain, return
    dB). This is what pulls a faint, stable emanation tone up out of the noise: the
    tone adds coherently sweep-to-sweep while the noise averages down."""
    if not len(psds_db):
        raise ValueError("no PSDs to average")
    lin = np.mean([10.0 ** (np.asarray(p) / 10.0) for p in psds_db], axis=0)
    return 10.0 * np.log10(lin + 1e-20)


def smooth_floor_db(psd_db: np.ndarray, win: int = 51) -> np.ndarray:
    """Running-median noise floor. A median (not mean) window ignores the very
    spikes we want to detect, giving a floor that tracks the band without being
    pulled up by emanation lines."""
    n = psd_db.size
    win = max(3, min(win, n if n % 2 else n - 1))
    if win % 2 == 0:
        win += 1
    half = win // 2
    padded = np.pad(psd_db, half, mode="edge")
    out = np.empty(n)
    for i in range(n):
        out[i] = np.median(padded[i:i + win])
    return out


def detect_spikes(freqs_hz: np.ndarray, psd_db: np.ndarray, floor_db: np.ndarray,
                  prominence_db: float = 8.0, min_sep_bins: int = 2) -> list[Spike]:
    """Narrowband local maxima standing `prominence_db` above the smoothed floor."""
    n = psd_db.size
    out: list[Spike] = []
    last = -min_sep_bins - 1
    for i in range(1, n - 1):
        prom = float(psd_db[i] - floor_db[i])
        if (prom >= prominence_db and psd_db[i] >= psd_db[i - 1]
                and psd_db[i] >= psd_db[i + 1] and i - last > min_sep_bins):
            out.append(Spike(freq_mhz=float(freqs_hz[i] / 1e6), prominence_db=round(prom, 1)))
            last = i
    return out


def subtract_baseline_db(test_db: np.ndarray, base_db: np.ndarray) -> np.ndarray:
    """test minus baseline in the linear domain, floored at ~0, returned as dB. What
    remains is the energy the test capture ADDED over the reference — ambient
    comms/known emitters present in both cancel out."""
    t = 10.0 ** (np.asarray(test_db) / 10.0)
    b = 10.0 ** (np.asarray(base_db) / 10.0)
    # floor the residual at a small GLOBAL level (fraction of the median power), so a
    # fully-cancelled tone drops to the noise floor regardless of how loud it was —
    # a per-bin floor would leave loud cancelled tones as spurious residual spikes.
    floor_lin = float(np.median(t)) * 1e-3 + 1e-20
    resid = np.maximum(t - b, floor_lin)
    return 10.0 * np.log10(resid)


CLOCK_MIN_HZ = 1_000_000
CLOCK_MAX_HZ = 200_000_000


def find_harmonic_combs(spikes: list[Spike], min_harmonics: int = 3,
                        tol_frac: float = 0.03, min_tol_hz: float = 40_000.0,
                        min_density: float = 0.6) -> list[dict]:
    """Group spikes into evenly-spaced combs (a device's clock harmonics).

    For each candidate clock `d` (every pairwise spike spacing in the plausible clock
    range), snap each spike to its nearest integer multiple of d and keep the ones
    within tolerance. A genuine comb is DENSE — its matched harmonic orders form a
    near-consecutive run (order 1,2,3,4…), so we require `min_density` of the order
    span to be filled. That density test is what defeats the GCD trap: two unrelated
    devices at 24 and 27 MHz share a spacing-3 "grid", but only sparsely (orders
    8,9,16,18,…), so it is rejected while the real 24 and 27 combs each pass.

    Larger clocks are tried first so a comb is reported at its true fundamental, not
    a /2 or /3 subharmonic. Overlapping combs are de-duplicated, richest first.
    """
    pts = sorted(((s.freq_mhz * 1e6, s.prominence_db) for s in spikes),
                 key=lambda x: x[0])
    freqs = [p[0] for p in pts]
    prom = {round(p[0]): p[1] for p in pts}
    if len(freqs) < min_harmonics:
        return []

    cands = set()
    for i in range(len(freqs)):
        for j in range(i + 1, len(freqs)):
            d = freqs[j] - freqs[i]
            if CLOCK_MIN_HZ <= d <= CLOCK_MAX_HZ:
                cands.add(round(d))

    combs = []
    for d in sorted(cands, reverse=True):          # prefer the true (larger) fundamental
        tol = max(tol_frac * d, min_tol_hz)
        matched = []
        for f in freqs:
            m = round(f / d)
            if m >= 1 and abs(f - m * d) <= tol:
                matched.append((m, f))
        orders = sorted({m for m, _ in matched})
        if len(orders) < min_harmonics:
            continue
        span = orders[-1] - orders[0] + 1
        if len(orders) / span < min_density:       # sparse grid (GCD trap) -> reject
            continue
        hs = [f for _, f in matched]
        clock = float(np.median(np.diff(sorted(hs)))) if len(hs) > 1 else float(d)
        strength = float(np.mean([prom.get(round(f), 0.0) for f in hs]))
        combs.append({"clock_hz": clock, "harmonics_hz": sorted(hs),
                      "n": len(orders), "strength_db": round(strength, 1)})

    combs.sort(key=lambda c: (-c["n"], -c["strength_db"], -c["clock_hz"]))
    chosen, covered = [], set()
    for c in combs:
        hs = {round(f) for f in c["harmonics_hz"]}
        if len(hs - covered) >= min_harmonics:
            chosen.append(c)
            covered |= hs
    return chosen


def _device_hint(clock_mhz: float) -> str:
    """A cautious lead from the clock spacing — NOT an identification."""
    if 23.0 <= clock_mhz <= 25.0 or 47.0 <= clock_mhz <= 49.0:
        return "24/48 MHz is a common image-sensor clock — possible hidden camera"
    if 26.5 <= clock_mhz <= 27.5:
        return "27 MHz is a common camera/MCU crystal — possible camera or wireless module"
    if 11.5 <= clock_mhz <= 12.5:
        return "12 MHz is a common USB/MCU clock — possible USB device or microcontroller"
    if 15.5 <= clock_mhz <= 16.5:
        return "16 MHz is a common microcontroller crystal — possible recorder/MCU"
    if 24.9 <= clock_mhz <= 25.1:
        return "25 MHz is a common Ethernet/MCU clock"
    return "clock does not match a common camera/MCU crystal — identify by localizing"


def _comb_to_result(c: dict) -> EmanationComb:
    clock_mhz = round(c["clock_hz"] / 1e6, 3)
    harmonics = [round(f / 1e6, 3) for f in c["harmonics_hz"]]
    hint = _device_hint(clock_mhz)
    plain = (f"{c['n']} evenly-spaced tones ~{clock_mhz:.2f} MHz apart "
             f"({harmonics[0]:.1f}–{harmonics[-1]:.1f} MHz) — the harmonic comb of a "
             f"single device's {clock_mhz:.2f} MHz clock. {hint}. Localize it.")
    return EmanationComb(clock_mhz=clock_mhz, harmonics_mhz=harmonics,
                         n_harmonics=c["n"], strength_db=c["strength_db"],
                         device_hint=hint, plain=plain)


# ---------------------------------------------------------------------------
# Scan engine — drives any SDR with capture_iq (no new radio protocol)
# ---------------------------------------------------------------------------

class EmanationScanner:
    """Sweeps a band in hops, averages each hop's PSD, detects spikes, optionally
    subtracts a baseline, and reports harmonic combs. `radio` need only implement
    capture_iq(center_hz, fs, n) — the Pluto and B220 both do."""

    def __init__(self, cfg, radio):
        self.cfg = cfg
        self.radio = radio
        self.fs = float(cfg.get("emanations", "sample_rate", default=40_000_000))
        self.nperseg = int(cfg.get("emanations", "fft_size", default=4096))
        self.dwell = int(cfg.get("emanations", "dwell_samples", default=1 << 18))
        self.averages = int(cfg.get("emanations", "averages", default=50))
        self.prominence_gate = float(cfg.get("emanations", "prominence_gate_db", default=8.0))
        self.min_harmonics = int(cfg.get("emanations", "min_harmonics", default=3))
        self.floor_win = int(cfg.get("emanations", "floor_window_bins", default=51))

    def _hop_psd(self, center_hz: float):
        psds = []
        for _ in range(self.averages):
            iq = self.radio.capture_iq(center_hz, self.fs, self.dwell)
            fr, pdb = welch_psd(np.asarray(iq, dtype=np.complex64), self.fs,
                                nperseg=min(self.nperseg, max(256, len(iq) // 4)))
            psds.append(pdb)
        return fr, average_psd_db(psds)

    def scan(self, band_lo_hz: float, band_hi_hz: float,
             baseline: Optional[dict] = None) -> EmanationResult:
        """Scan [lo,hi] for emanation combs. `baseline` (optional) is a prior result
        of scan_raw() over the same hops to subtract. Returns the ranked combs."""
        band_mhz = (band_lo_hz / 1e6, band_hi_hz / 1e6)
        centers = []
        c = band_lo_hz + self.fs / 2.0
        while c < band_hi_hz + self.fs / 2.0:
            centers.append(c)
            c += self.fs * 0.9                          # 10% overlap between hops
        all_spikes: list[Spike] = []
        warnings: list[str] = []
        for center in centers:
            try:
                fr, avg_db = self._hop_psd(center)
            except Exception as e:  # noqa: BLE001
                warnings.append(f"hop @ {center/1e6:.0f} MHz failed: {e}")
                continue
            abs_freqs = fr + center
            base_db = (baseline or {}).get(round(center))
            work_db = subtract_baseline_db(avg_db, base_db) if base_db is not None else avg_db
            floor = smooth_floor_db(work_db, self.floor_win)
            all_spikes.extend(detect_spikes(abs_freqs, work_db, floor,
                                            self.prominence_gate))
        # de-dupe spikes from overlapping hops (same freq within a bin)
        all_spikes.sort(key=lambda s: s.freq_mhz)
        deduped: list[Spike] = []
        for s in all_spikes:
            if deduped and abs(s.freq_mhz - deduped[-1].freq_mhz) < (self.fs / self.nperseg) / 1e6:
                if s.prominence_db > deduped[-1].prominence_db:
                    deduped[-1] = s
                continue
            deduped.append(s)

        combs_raw = find_harmonic_combs(deduped, self.min_harmonics)
        combs = [_comb_to_result(c) for c in combs_raw]
        if combs:
            verdict = "SUSPECTED_EMITTER"
            plain = (f"{len(combs)} harmonic comb(s) found — the signature of "
                     f"concealed device(s) leaking clock harmonics. "
                     + "; ".join(c.device_hint for c in combs) +
                     ". These do not transmit on their own — LOCALIZE with a directional "
                     "antenna. (Faint leaks: confirm by moving the antenna closer.)")
        elif deduped:
            verdict = "NONE"
            plain = (f"{len(deduped)} narrowband tone(s) seen but none form an evenly-spaced "
                     "harmonic comb — consistent with ordinary ambient signals, not a hidden "
                     "device's clock. NOTE: emanations are faint; a null result is not proof "
                     "of a clean room — re-scan closer / quieter, and use the sweep + active probe too.")
        else:
            verdict = "NONE"
            plain = ("No narrowband emanations above the noise floor. Emanations are faint, so "
                     "this is one independent look, not proof of a clean room — move the antenna "
                     "closer and re-scan, and lean on the sweep + active probe too.")
        return EmanationResult(verdict=verdict, plain=plain, combs=combs, spikes=deduped,
                               band_mhz=band_mhz, averaged_sweeps=self.averages,
                               had_baseline=baseline is not None, warnings=warnings)

    def scan_raw(self, band_lo_hz: float, band_hi_hz: float) -> dict:
        """Capture a baseline: {round(center_hz): averaged_psd_db} for later
        subtraction. Run this on a reference (clean/quiet) capture of the same band."""
        out = {}
        c = band_lo_hz + self.fs / 2.0
        while c < band_hi_hz + self.fs / 2.0:
            try:
                _fr, avg_db = self._hop_psd(c)
                out[round(c)] = avg_db
            except Exception:  # noqa: BLE001
                pass
            c += self.fs * 0.9
        return out
