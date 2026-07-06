"""Active RF stimulation ("ping") detector — stimulate a band, look for a device
that RESPONDS to it.

THE IDEA (operator's ask)
=========================
Transmit a stimulus into the sweep area. A covert device with an active front end
— a bug that can be remotely keyed, a superregenerative receiver, anything with an
AGC or a nonlinear junction — can react: its emission changes, a new mixing line
appears, or it re-radiates. We capture the band with the stimulus OFF (quiet
reference) and again with it ON, and flag any NARROWBAND feature that appears or
jumps ONLY under stimulus. That correlated change is the tell.

WHAT THIS HONESTLY IS — AND IS NOT (this goes in front of paying clients)
========================================================================
1. IT ONLY FINDS THE *RESPONSIVE* SUBSET. Devices with a receiver/oscillator/AGC or
   a semiconductor junction can respond. A PASSIVE, well-shielded SD-card audio
   recorder with no RF receiver may not react at all. **A "no response" result is
   NOT proof the room is clean.** (The passive-emanation and sweep detectors cover
   the silent recorders; this is one more independent look, not a magic wand.) The
   paper the operator cited (RFScan, PoPETs 2025) is in fact a *passive* emanation
   detector and argues that active probing is evadable and can stimulate legitimate
   devices too — so we treat this as a COMPLEMENT to passive detection, never a
   replacement, and we never oversell it.
2. RADIO REALITY. The ADALM-Pluto is a single TX/RX device: it cannot cleanly
   transmit and receive in the same band at the same instant (self-interference
   swamps the RX). So on the Pluto this must run TIME-INTERLEAVED — stimulate in a
   burst, stop, then listen for the response / ring-down. Genuinely simultaneous
   "stimulate while listening" needs the B210's independent TX/RX chains (the same
   radio the NLJD uses). The engine is mode-agnostic: the radio object reports its
   own mode and owns the timing.
3. LEGAL / INTERFERENCE. Transmitting a stimulus is RADIATING. It must stay inside
   bands the operator is authorized to transmit in (ISM: 902-928, 2400-2483.5,
   5725-5875 MHz, low power) and NEVER on cellular / public-safety / aviation. The
   engine REFUSES to stimulate outside the operator-declared permitted bands unless
   the operator explicitly overrides (a logged, on-screen acknowledgment, mirroring
   the NLJD hardware attest and the ECPA decode override). Software cannot make an
   out-of-band transmission legal — only the operator's authority/coordination can.

HARDWARE BINDING IS DELIBERATELY NOT WIRED YET. This module ships the measurement
ENGINE and its decision logic, unit-tested against a synthetic radio. The real TX
path (Pluto burst timing / B210 concurrent TX) and the band-authorization UX must
be built and BENCH-VALIDATED before any "transmit" control is exposed — we do not
ship an untested transmitter. See STIMULATE_HARDWARE_TODO in the repo docs.

Pure numpy DSP — unit tested in tests/test_stimulate.py with a synthetic radio.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, asdict, field
from typing import Optional

import numpy as np

from .sweep.sdr_source import welch_psd

# ISM bands (MHz) an operator can typically transmit low power in the US (Part 15).
# This is the DEFAULT permitted set; the real authority is the operator's, and the
# radio object supplies the actual permitted list at runtime.
DEFAULT_PERMITTED_BANDS_MHZ = ((902.0, 928.0), (2400.0, 2483.5), (5725.0, 5875.0))


@dataclass
class Responder:
    """One frequency that reacted to the stimulus."""
    freq_mhz: float
    delta_db: float          # stimulated minus quiet at this line
    stim_snr_db: float       # prominence above the local floor in the STIM capture
    quiet_snr_db: float      # prominence in the QUIET capture (should be low if new)

    def to_dict(self):
        return asdict(self)


@dataclass
class StimResponse:
    verdict: str             # RESPONSE_DETECTED | NO_RESPONSE | BLOCKED_BAND | INCONCLUSIVE
    plain: str               # client-facing plain-language sentence
    responders: list         # [Responder, ...]
    band_mhz: tuple          # (lo, hi) probed
    mode: str                # concurrent | interleaved
    confident: bool
    band_permitted: bool
    ts: float = field(default_factory=time.time)
    warnings: list = field(default_factory=list)

    def to_dict(self):
        d = asdict(self)
        d["responders"] = [r if isinstance(r, dict) else r.to_dict() for r in self.responders]
        return d


# ---------------------------------------------------------------------------
# Stimulus waveform — pure numpy, unit tested (the radio streams this on TX)
# ---------------------------------------------------------------------------

def make_chirp(fs: float, span_hz: float, n: int, amp: float = 0.5,
               sweeps: float = 8.0) -> np.ndarray:
    """A repeating linear frequency sweep (chirp) across ±span/2 at baseband, to be
    up-converted by the TX LO so it illuminates the whole [center-span/2, center+span/2]
    band. A swept tone (vs. a static CW) pokes a device's receiver/AGC across its
    entire passband, not just one spot. `amp` stays <1 (DAC headroom, never full-scale
    into the front end — same discipline as the NLJD TX). `span_hz` is clamped to fs
    (can't synthesise beyond the Nyquist window; wider bands need multiple TX windows)."""
    span = float(min(span_hz, fs))
    n = int(n)
    if n <= 0:
        return np.zeros(0, dtype=np.complex64)
    period = max(1, int(n / max(sweeps, 1.0)))
    idx = np.arange(n)
    frac = (idx % period) / period                 # 0..1 sawtooth
    f_inst = -span / 2.0 + span * frac             # instantaneous freq, Hz
    phase = 2.0 * np.pi * np.cumsum(f_inst) / fs
    return (amp * np.exp(1j * phase)).astype(np.complex64)


# ---------------------------------------------------------------------------
# Pure DSP — no hardware
# ---------------------------------------------------------------------------

def _local_prominence_db(psd_db: np.ndarray, idx: int, half_win: int) -> float:
    """How far a bin stands above the median of its neighbourhood (a robust local
    floor). This is what separates a genuine narrowband LINE from a flat, band-wide
    rise: turning the stimulus on lifts the WHOLE band's floor, but that uniform lift
    has zero local prominence — only a real spike stands proud of its own neighbours."""
    lo = max(0, idx - half_win)
    hi = min(psd_db.size, idx + half_win + 1)
    neigh = np.concatenate([psd_db[lo:idx], psd_db[idx + 1:hi]])
    if neigh.size == 0:
        return 0.0
    return float(psd_db[idx] - np.median(neigh))


def find_responders(freqs_hz: np.ndarray, quiet_db: np.ndarray, stim_db: np.ndarray,
                    prominence_gate_db: float = 8.0, delta_gate_db: float = 6.0,
                    guard_bins: int = 2) -> list[Responder]:
    """Bins that are PROMINENT narrowband peaks under stimulus, jumped by at least
    ``delta_gate_db`` vs quiet, and were NOT already a comparably-prominent peak when
    quiet. That triple test is the honesty crux:
      * prominence  -> it is a real line, not the stimulus raising the whole floor;
      * delta       -> the stimulus is what brought it up (not always-on ambient);
      * quiet-absent-> it is a RESPONSE, not a signal that was there all along.
    """
    n = freqs_hz.size
    half = max(3, n // 64)
    out: list[Responder] = []
    last = -10
    for i in range(1, n - 1):
        if not (stim_db[i] >= stim_db[i - 1] and stim_db[i] >= stim_db[i + 1]):
            continue                                   # local max only
        sp = _local_prominence_db(stim_db, i, half)
        if sp < prominence_gate_db:
            continue
        delta = float(stim_db[i] - quiet_db[i])
        if delta < delta_gate_db:
            continue
        qp = _local_prominence_db(quiet_db, i, half)
        if qp >= prominence_gate_db - 1.0:
            continue                                   # already a peak when quiet -> not a response
        if i - last <= guard_bins:
            continue
        last = i
        out.append(Responder(freq_mhz=float(freqs_hz[i] / 1e6), delta_db=round(delta, 1),
                             stim_snr_db=round(sp, 1), quiet_snr_db=round(qp, 1)))
    out.sort(key=lambda r: r.delta_db, reverse=True)
    return out


def _plain(verdict: str, responders: list, band_mhz: tuple) -> str:
    lo, hi = band_mhz
    if verdict == "BLOCKED_BAND":
        return (f"Not transmitted: {lo:.0f}-{hi:.0f} MHz is outside the bands you have "
                "authorized to transmit in. Stimulating here could be unlawful "
                "interference — authorize the band (or coordinate) before probing it.")
    if verdict == "NO_RESPONSE":
        return (f"No device responded to the stimulus in {lo:.0f}-{hi:.0f} MHz. "
                "IMPORTANT: this only rules out devices that REACT to a ping "
                "(receivers, oscillators, nonlinear junctions). A passive, shielded "
                "recorder can sit silent through this — a null result is not a clean bill. "
                "Use it alongside the passive sweep, not instead of it.")
    if verdict == "INCONCLUSIVE":
        return ("Inconclusive — the captures were too noisy or too short to separate a "
                "response from the stimulus. Re-run with more averaging.")
    freqs = ", ".join(f"{r.freq_mhz:.3f} MHz (+{r.delta_db:.0f} dB)" for r in responders[:4])
    more = "" if len(responders) <= 4 else f" (+{len(responders) - 4} more)"
    return (f"{len(responders)} frequency(ies) REACTED to the stimulus: {freqs}{more}. "
            "Something in the area changed its emission when pinged — isolate and locate "
            "each one (a legitimate device with a receiver can also react, so confirm).")


# ---------------------------------------------------------------------------
# Measurement engine — hardware-agnostic (takes a radio-like object)
# ---------------------------------------------------------------------------

class StimulationEngine:
    """Drives quiet-vs-stimulated differential probing against any radio implementing
    the small stimulation protocol:

      stim_mode() -> "concurrent" | "interleaved"
      stim_permitted_bands() -> [(lo_mhz, hi_mhz), ...]   operator-authorized TX bands
      stim_capture_quiet(center_hz, fs, n) -> iq          TX guaranteed OFF
      stim_capture_stimulated(center_hz, lo_hz, hi_hz, fs, n) -> iq
                                                          TX stimulus applied per mode

    B210/Pluto adapters implement it against real hardware (NOT wired yet — bench
    first); tests implement it with synthetic IQ.
    """

    def __init__(self, cfg, radio):
        self.cfg = cfg
        self.radio = radio
        self.fs = float(cfg.get("stimulate", "rx_sample_rate", default=4_000_000))
        self.nfft = int(cfg.get("stimulate", "fft_size", default=4096))
        self.dwell_samples = int(cfg.get("stimulate", "dwell_samples", default=1 << 18))
        self.n_avg = int(cfg.get("stimulate", "averages", default=6))
        self.prominence_gate = float(cfg.get("stimulate", "prominence_gate_db", default=8.0))
        self.delta_gate = float(cfg.get("stimulate", "delta_gate_db", default=6.0))

    def _avg_psd(self, capture_fn) -> tuple[np.ndarray, np.ndarray]:
        freqs = None
        acc = None
        for _ in range(self.n_avg):
            iq = capture_fn()
            fr, psd_db = welch_psd(np.asarray(iq, dtype=np.complex64), self.fs,
                                   nperseg=min(self.nfft, max(256, len(iq) // 4)))
            lin = 10.0 ** (psd_db / 10.0)
            acc = lin if acc is None else acc + lin
            freqs = fr
        avg_db = 10.0 * np.log10(acc / self.n_avg + 1e-20)
        return freqs, avg_db

    def probe(self, center_hz: float, band_lo_hz: float, band_hi_hz: float,
              override_band: bool = False) -> StimResponse:
        band_mhz = (band_lo_hz / 1e6, band_hi_hz / 1e6)
        mode = self.radio.stim_mode()
        permitted = any(lo <= center_hz / 1e6 <= hi
                        for lo, hi in self.radio.stim_permitted_bands())
        warnings: list[str] = []

        if not permitted and not override_band:
            return StimResponse(verdict="BLOCKED_BAND",
                                plain=_plain("BLOCKED_BAND", [], band_mhz),
                                responders=[], band_mhz=band_mhz, mode=mode,
                                confident=False, band_permitted=False,
                                warnings=["transmit band not authorized; not stimulated"])
        if not permitted and override_band:
            warnings.append("BAND OVERRIDE: transmitting outside the authorized ISM "
                            "bands on operator authority — ensure this is lawful/coordinated")

        freqs_q, quiet_db = self._avg_psd(
            lambda: self.radio.stim_capture_quiet(center_hz, self.fs, self.dwell_samples))

        # Concurrent radios (B220: independent TX/RX chains) key the stimulus ONCE
        # for the whole averaging burst via stim_tx_on/off; interleaved/synthetic
        # radios that lack those hooks manage TX inside stim_capture_stimulated.
        tx_on = getattr(self.radio, "stim_tx_on", None)
        tx_off = getattr(self.radio, "stim_tx_off", None)
        if tx_on:
            tx_on(band_lo_hz, band_hi_hz)
        try:
            freqs_s, stim_db = self._avg_psd(
                lambda: self.radio.stim_capture_stimulated(center_hz, band_lo_hz, band_hi_hz,
                                                           self.fs, self.dwell_samples))
        finally:
            if tx_off:
                tx_off()
        if freqs_q is None or freqs_s is None or quiet_db.shape != stim_db.shape:
            return StimResponse(verdict="INCONCLUSIVE",
                                plain=_plain("INCONCLUSIVE", [], band_mhz),
                                responders=[], band_mhz=band_mhz, mode=mode,
                                confident=False, band_permitted=permitted, warnings=warnings)

        # shift the analyzer freqs to absolute RF for reporting
        abs_freqs = freqs_s + center_hz
        responders = find_responders(abs_freqs, quiet_db, stim_db,
                                     self.prominence_gate, self.delta_gate)
        confident = len(responders) > 0
        verdict = "RESPONSE_DETECTED" if confident else "NO_RESPONSE"
        return StimResponse(verdict=verdict, plain=_plain(verdict, responders, band_mhz),
                            responders=responders, band_mhz=band_mhz, mode=mode,
                            confident=confident, band_permitted=permitted, warnings=warnings)
