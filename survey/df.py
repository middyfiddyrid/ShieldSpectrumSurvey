"""Phase-interferometry direction finding on a coherent dual-RX SDR (B220).

Both AD9361 RX chains share one LO, so two simultaneous captures are
phase-coherent within a tune. With two antennas spaced `d` metres apart the
arriving wavefront hits them with a path difference d·sin(θ), which shows up
as a cross-spectrum phase Δφ = 2π·d·sin(θ)/λ — invert that and you have a
bearing relative to the antenna bar.

HONESTY (what this can and cannot do):
- A two-element array has a FRONT/BACK MIRROR: θ and 180°−θ are
  indistinguishable. Both bearings are always reported; the operator breaks
  the tie by rotating the bar or walking (the true bearing stays put, the
  mirror flips).
- The inter-channel phase OFFSET (cables, analog chains, LO divider phase) is
  unknown and re-randomized on every retune. Bearings are meaningless until
  the operator calibrates: hold the bar BROADSIDE to the emitter (both
  antennas equidistant) and zero the phase. Retuning invalidates calibration.
- Spacing vs wavelength: unambiguous bearing needs d ≤ λ/2 (0.35 m is good to
  ~428 MHz; 2.4 GHz needs ≤ 6 cm). Wider spacing still gives a usable
  rotate-for-null indicator but the reported angle aliases — flagged.
- Indoors, multipath scrambles phase. The coherence metric (0..1: "is this
  the same wavefront on both antennas?") gates the output: low coherence
  means NO bearing, not a confidently wrong needle.

Pure numpy — the caller supplies a (2, n) coherent IQ pair. Unit tested with
synthetic wavefronts in tests/test_df.py; no hardware here.
"""

import time
from dataclasses import dataclass, field

import numpy as np

C_LIGHT = 299_792_458.0

# verdicts, from best to worst
BEARING_OK = "BEARING_OK"
AMBIGUOUS_SPACING = "AMBIGUOUS_SPACING"   # phase fine, but d > λ/2 aliases the angle
LOW_COHERENCE = "LOW_COHERENCE"           # multipath / two different emitters
NO_SIGNAL = "NO_SIGNAL"                   # nothing above the floor on this tune


@dataclass
class DFMeasurement:
    verdict: str
    plain: str                       # client/operator-facing honesty sentence
    phase_deg: float = 0.0           # raw cross-spectrum phase A→B
    phase_cal_deg: float = 0.0       # phase after calibration offset
    bearing_deg: float | None = None  # relative to broadside of the bar, -90..90
    mirror_deg: float | None = None   # the front/back twin
    coherence: float = 0.0
    snr_a_db: float = 0.0
    snr_b_db: float = 0.0
    amp_ratio_db: float = 0.0        # A stronger than B → positive
    freq_hz: float = 0.0
    spacing_m: float = 0.0
    max_unambiguous_hz: float = 0.0  # highest freq this spacing resolves uniquely
    calibrated: bool = False
    ts: float = field(default_factory=time.time)
    warnings: list = field(default_factory=list)

    def to_dict(self):
        return {
            "verdict": self.verdict, "plain": self.plain,
            "phase_deg": round(self.phase_deg, 1),
            "phase_cal_deg": round(self.phase_cal_deg, 1),
            "bearing_deg": None if self.bearing_deg is None else round(self.bearing_deg, 1),
            "mirror_deg": None if self.mirror_deg is None else round(self.mirror_deg, 1),
            "coherence": round(self.coherence, 3),
            "snr_a_db": round(self.snr_a_db, 1), "snr_b_db": round(self.snr_b_db, 1),
            "amp_ratio_db": round(self.amp_ratio_db, 1),
            "freq_hz": self.freq_hz, "spacing_m": self.spacing_m,
            "max_unambiguous_mhz": round(self.max_unambiguous_hz / 1e6, 1),
            "calibrated": self.calibrated, "ts": self.ts,
            "warnings": list(self.warnings),
        }


def _wrap_deg(x: float) -> float:
    return float((x + 180.0) % 360.0 - 180.0)


def cross_spectrum(iq_a: np.ndarray, iq_b: np.ndarray, fs: float,
                   nperseg: int = 4096) -> dict:
    """Welch-averaged auto/cross spectra of a coherent pair.
    Returns freqs (baseband, Hz), sxx, syy (real PSDs), sxy (complex)."""
    n = min(len(iq_a), len(iq_b))
    nperseg = int(min(nperseg, n))
    if nperseg < 256:
        raise ValueError("need at least 256 samples per channel")
    segs = n // nperseg
    win = np.hanning(nperseg).astype(np.float32)
    sxx = np.zeros(nperseg)
    syy = np.zeros(nperseg)
    sxy = np.zeros(nperseg, dtype=np.complex128)
    for i in range(segs):
        a = np.fft.fftshift(np.fft.fft(iq_a[i * nperseg:(i + 1) * nperseg] * win))
        b = np.fft.fftshift(np.fft.fft(iq_b[i * nperseg:(i + 1) * nperseg] * win))
        sxx += (a * np.conj(a)).real
        syy += (b * np.conj(b)).real
        sxy += a * np.conj(b)
    sxx /= segs
    syy /= segs
    sxy /= segs
    freqs = np.fft.fftshift(np.fft.fftfreq(nperseg, d=1.0 / fs))
    return {"freqs": freqs, "sxx": sxx, "syy": syy, "sxy": sxy}


def measure(iq_a: np.ndarray, iq_b: np.ndarray, fs: float, rf_freq_hz: float,
            spacing_m: float, cal_offset_deg: float | None = None,
            nperseg: int = 4096, signal_down_db: float = 6.0,
            snr_gate_db: float = 6.0, coherence_gate: float = 0.5) -> DFMeasurement:
    """One DF measurement from a coherent capture pair.

    cal_offset_deg None => uncalibrated (bearing still computed from raw phase
    but flagged; the needle is only trustworthy after calibration)."""
    cs = cross_spectrum(iq_a, iq_b, fs, nperseg)
    sxx, syy, sxy = cs["sxx"], cs["syy"], cs["sxy"]
    total = sxx + syy
    floor = float(np.median(total))
    peak_i = int(np.argmax(total))
    peak = float(total[peak_i])

    snr_a = 10.0 * np.log10(max(float(sxx[peak_i]), 1e-30) /
                            max(float(np.median(sxx)), 1e-30))
    snr_b = 10.0 * np.log10(max(float(syy[peak_i]), 1e-30) /
                            max(float(np.median(syy)), 1e-30))

    lam = C_LIGHT / max(rf_freq_hz, 1.0)
    max_unamb_hz = C_LIGHT / (2.0 * max(spacing_m, 1e-3))

    if peak <= 0 or 10.0 * np.log10(peak / max(floor, 1e-30)) < snr_gate_db \
            or min(snr_a, snr_b) < snr_gate_db:
        return DFMeasurement(
            verdict=NO_SIGNAL,
            plain="No clear signal above the noise floor on this tune — "
                  "no bearing. Move closer or check the frequency.",
            snr_a_db=snr_a, snr_b_db=snr_b, freq_hz=rf_freq_hz,
            spacing_m=spacing_m, max_unambiguous_hz=max_unamb_hz,
            calibrated=cal_offset_deg is not None)

    # signal bins: within signal_down_db of the peak, contiguous around it
    thr = peak * (10.0 ** (-signal_down_db / 10.0))
    sig = total >= max(thr, floor * 2.0)
    lo = peak_i
    while lo > 0 and sig[lo - 1]:
        lo -= 1
    hi = peak_i
    while hi < len(sig) - 1 and sig[hi + 1]:
        hi += 1
    idx = np.arange(lo, hi + 1)

    # power-weighted magnitude-squared coherence + phase over the signal bins
    w = total[idx]
    coh_bins = (np.abs(sxy[idx]) ** 2) / np.maximum(sxx[idx] * syy[idx], 1e-30)
    coherence = float(np.sum(coh_bins * w) / np.sum(w))
    phase_rad = float(np.angle(np.sum(sxy[idx] * w)))
    phase_deg = float(np.degrees(phase_rad))
    amp_ratio_db = 10.0 * np.log10(max(float(np.sum(sxx[idx])), 1e-30) /
                                   max(float(np.sum(syy[idx])), 1e-30))

    calibrated = cal_offset_deg is not None
    phase_cal_deg = _wrap_deg(phase_deg - (cal_offset_deg or 0.0))

    common = dict(phase_deg=phase_deg, phase_cal_deg=phase_cal_deg,
                  coherence=coherence, snr_a_db=snr_a, snr_b_db=snr_b,
                  amp_ratio_db=amp_ratio_db, freq_hz=rf_freq_hz,
                  spacing_m=spacing_m, max_unambiguous_hz=max_unamb_hz,
                  calibrated=calibrated)

    if coherence < coherence_gate:
        return DFMeasurement(
            verdict=LOW_COHERENCE,
            plain="The two antennas disagree about this wavefront "
                  f"(coherence {coherence:.2f}) — multipath or overlapping "
                  "emitters. No bearing; move a few metres and retry.",
            **common)

    # phase → bearing.  sin(θ) = Δφ·λ / (2π·d)
    sin_theta = np.radians(phase_cal_deg) * lam / (2.0 * np.pi * spacing_m)
    warnings = []
    if not calibrated:
        warnings.append("UNCALIBRATED — hold the bar broadside to the emitter "
                        "and tap CALIBRATE; until then the needle has an "
                        "unknown constant offset.")
    if rf_freq_hz > max_unamb_hz:
        warnings.append(
            f"antenna spacing {spacing_m:.2f} m is wider than λ/2 at "
            f"{rf_freq_hz / 1e6:.1f} MHz — the angle aliases; use the needle "
            "only as a rotate-for-zero indicator, or shorten the spacing.")
        return DFMeasurement(
            verdict=AMBIGUOUS_SPACING,
            plain="Phase lock is good but the spacing is too wide for a "
                  "unique angle at this frequency — rotate the bar until the "
                  "phase reads 0° and the emitter is broadside (or behind).",
            warnings=warnings, **common)

    sin_theta = float(np.clip(sin_theta, -1.0, 1.0))
    bearing = float(np.degrees(np.arcsin(sin_theta)))
    mirror = _wrap_deg(180.0 - bearing)
    return DFMeasurement(
        verdict=BEARING_OK,
        plain=(f"Bearing {bearing:+.0f}° from broadside (or its mirror "
               f"{mirror:+.0f}° behind you) — rotate the bar to confirm: the "
               "true bearing stays put, the mirror flips."),
        bearing_deg=bearing, mirror_deg=mirror, warnings=warnings, **common)
