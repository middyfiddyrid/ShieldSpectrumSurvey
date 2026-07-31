"""Software-defined harmonic Non-Linear Junction Detector (NLJD) — BENCH mode.

WHAT THIS IS (and is NOT)
=========================
This is a *bench test-chamber* NLJD, not an in-place room scanner. The operator
places an object INSIDE a small Faraday cavity, we transmit a clean CW tone at
f0 = 915 MHz on the B210's TX-A, and we listen on RX-A for the weak harmonics a
semiconductor junction re-radiates at 2f0 = 1830 MHz and 3f0 = 2745 MHz. The
2f/3f amplitude ratio separates a REAL semiconductor (any diode/transistor/IC,
powered or not) from a FALSE junction (corroded metal, rust, loose oxide MOM
contact).

TWO HARD PHYSICAL LIMITS — STATED PLAINLY, NEVER OVERSOLD (these go in front of
paying clients, per Shield honesty policy):

  1. TX POWER. The B210 puts out only ~+6..+16 dBm (a few mW to ~40 mW, and it is
     an UNCALIBRATED, gain-only radio — you cannot command an absolute dBm). A
     commercial NLJD (REI Orion/NJE) transmits WATTS. We are orders of magnitude
     down. => This tool detects BLATANT electronics sitting in the shielded box.
     It is NOT an Orion-grade find and can miss a small, shielded, or deeply
     buried junction. The empty-cavity baseline + delta trick (below) is what lets
     ~10 mW do useful work at all; it does not manufacture sensitivity we lack.

  2. SELF-GENERATED HARMONICS. The AD9361 TX chain emits its OWN 2f0/3f0 harmonics
     that land at EXACTLY our detection frequencies. Software cannot remove energy
     that is already colliding with the signal on the RX ADC. This MUST be scrubbed
     in HARDWARE: a physical bandpass on the TX line (pass 915, kill 1830/2745) and
     a physical highpass + LNA on the RX line (pass 1.8-2.8 GHz, reject the 915
     leakage). If the required external filters are absent, this module REFUSES to
     assert a detection — see require_hw_filters gate.

FREQUENCY PLAN (frozen; see NLJD_HARDWARE_BOM.md):
  f0  = 915  MHz  TX CW illumination (emitted at 915.1 MHz: +100 kHz baseband
                  offset so the line sits off the direct-conversion LO/DC spur)
  2f0 = 1830 MHz  primary RX detect  (even harmonic -> semiconductor signature)
  3f0 = 2745 MHz  RX discriminator   (odd harmonic  -> corrosion/MOM signature)

DECISION RULE (the thing that makes this more than a metal detector):
  measure coherently-integrated power in the 2f0 bin and the 3f0 bin.
    2f0 > 3f0  => SEMICONDUCTOR  (smooth asymmetric diode I-V, even-order dominant)
    3f0 > 2f0  => FALSE junction (symmetric back-to-back MOM I-V, odd-order dominant)
  Report the ratio in dB (ratio_db = P2f_dB - P3f_dB) as a CONTINUOUS discriminator,
  not just a binary, so borderline cases (real device next to a corroded bracket,
  bias-dependent junctions) stay visible to the operator.

HONESTY / CONFIDENCE GATE:
  If the harmonic return at BOTH 2f0 and 3f0 sits below the measured noise floor
  by less than `snr_gate_db`, the verdict is NO_JUNCTION — we never hallucinate a
  hit. A verdict of SEMICONDUCTOR/FALSE is only emitted when a harmonic clears the
  noise floor AND (in armed mode) the hardware filters are attested present.

INTEGRATION
===========
This mirrors the Pluto's BaseDevice pattern in survey/sweep/sdr_source.py. The
B210 differs in ONE way that matters here: it can TRANSMIT while it RECEIVES on an
independent RF chain. So B210Source subclasses AD936xSource (inheriting the whole
stepped-sweep receive engine unchanged, so it still works as a normal spectrum
source for the rest of the app) and ADDS a transmit path. The NLJD measurement
engine (NLJDEngine) is pure-numpy DSP that takes any object exposing
`nljd_capture_at(rx_hz)` + `nljd_tx_cw(f0_hz)` / `nljd_tx_off()`, so it is unit-
testable with a synthetic radio and never imports UHD.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, asdict, field
from typing import Optional

import numpy as np

from .sweep.sdr_source import AD936xSource, welch_psd
from .sweep.device import DeviceError, DeviceInfo, ModuleInfo

log = logging.getLogger("survey.nljd")

# ---------------------------------------------------------------------------
# Frozen plan constants (single source of truth; config may override at runtime)
# ---------------------------------------------------------------------------
F0_HZ = 915_000_000          # fundamental CW illumination
TX_BB_OFFSET_HZ = 100_000    # baseband offset so emitted line dodges the LO/DC spur
HARMONICS = (2, 3)           # detect 2nd and 3rd
# RX capture spans a narrow window centered on each harmonic; the harmonic line
# is recovered in a sub-kHz FFT bin at exactly N*(f0 + bb_offset).


# ---------------------------------------------------------------------------
# Result records (JSON-friendly, mirror SweepFrame/DeviceInfo dataclass style)
# ---------------------------------------------------------------------------

@dataclass
class HarmonicReading:
    """One harmonic dwell: absolute frequency and the recovered line power."""
    n: int                       # 2 or 3
    freq_mhz: float
    line_power_db: float         # coherently-integrated power at the exact bin (rel dBFS)
    noise_floor_db: float        # median PSD in the dwell window, excluding the line
    snr_db: float                # line_power_db - noise_floor_db
    present: bool                # snr_db >= snr_gate_db

    def to_dict(self):
        return asdict(self)


@dataclass
class NLJDBaseline:
    """Empty-cavity reference. Subtracted from a measurement so the residual is
    ONLY what the object added — this is what makes ~10 mW usable: we are not
    measuring absolute harmonic power (hopeless at this budget) but the DELTA an
    object introduces above the box's own leakage floor."""
    f0_mhz: float
    readings: dict                 # {"2": line_power_db, "3": line_power_db}
    noise_floor_db: dict           # {"2": .., "3": ..}
    ts: float
    n_avg: int
    note: str = ""

    def to_dict(self):
        return asdict(self)


@dataclass
class NLJDVerdict:
    verdict: str                   # SEMICONDUCTOR | FALSE_JUNCTION | NO_JUNCTION | BLOCKED
    plain: str                     # client-facing plain-language sentence
    ratio_db: float                # P2f - P3f AFTER baseline subtraction (the discriminator)
    delta_2f_db: float             # object-minus-baseline at 2f0 (how much the object added)
    delta_3f_db: float
    harmonics: list                # [HarmonicReading, ...] raw (object present)
    confident: bool                # cleared the SNR gate on at least one harmonic
    hw_filters_attested: bool
    ts: float = field(default_factory=time.time)
    warnings: list = field(default_factory=list)

    def to_dict(self):
        d = asdict(self)
        d["harmonics"] = [h if isinstance(h, dict) else h.to_dict() for h in self.harmonics]
        return d


# ---------------------------------------------------------------------------
# Pure-numpy DSP — no hardware, unit-tested with a synthetic radio
# ---------------------------------------------------------------------------

def extract_line_power(iq: np.ndarray, fs: float, want_offset_hz: float,
                       nperseg: int = 32768) -> tuple[float, float, float]:
    """Recover the CW harmonic line sitting at baseband `want_offset_hz` inside a
    capture whose LO was tuned to N*(f0+bb_offset) - want_offset_hz... in practice
    we tune the RX LO a known small amount OFF the exact harmonic so the harmonic
    lands at +want_offset_hz baseband, cleanly away from the RX chain's own DC/LO
    spur (same trick the TX uses). We then read the single FFT bin at that offset.

    Returns (line_power_db, noise_floor_db, snr_db). All relative dBFS-ish; the
    NLJD only ever uses DIFFERENCES (object-minus-baseline) so absolute cal is
    irrelevant by construction — the binding honesty win of the baseline method.

    A single large-N FFT (not Welch-averaged) gives the tightest bin and thus the
    most coherent processing gain against the 10 mW budget: SNR improves ~10log10(N)
    as the CW line concentrates into one bin while noise spreads across all N.
    """
    iq = np.asarray(iq, dtype=np.complex64)
    N = iq.size
    if N == 0:
        raise ValueError("empty IQ buffer")
    nperseg = int(min(nperseg, N))
    # Hann to control scalloping; the line is parked ON a bin center by choosing
    # want_offset_hz as an integer multiple of fs/nperseg upstream (see engine).
    win = 0.5 - 0.5 * np.cos(2.0 * np.pi * np.arange(nperseg) / nperseg)
    seg = iq[:nperseg] * win
    sp = np.fft.fftshift(np.fft.fft(seg))
    freqs = np.fft.fftshift(np.fft.fftfreq(nperseg, d=1.0 / fs))
    psd = np.abs(sp) ** 2
    psd_db = 10.0 * np.log10(psd + 1e-20)

    line_bin = int(np.argmin(np.abs(freqs - want_offset_hz)))
    # line power = the target bin (plus its immediate neighbours to catch a little
    # spectral leakage), noise floor = ROBUST median of the window EXCLUDING a guard
    # around the line and around DC (RX LO leakage).
    lo, hi = max(0, line_bin - 1), min(psd.size, line_bin + 2)
    line_power = float(psd[lo:hi].sum())
    line_power_db = 10.0 * np.log10(line_power + 1e-20)

    guard = max(3, nperseg // 512)
    dc_bin = int(np.argmin(np.abs(freqs)))
    mask = np.ones(psd.size, dtype=bool)
    mask[max(0, line_bin - guard):line_bin + guard + 1] = False
    mask[max(0, dc_bin - guard):dc_bin + guard + 1] = False
    noise_floor_db = float(np.median(psd_db[mask])) if mask.any() else -200.0
    snr_db = line_power_db - noise_floor_db
    return line_power_db, noise_floor_db, snr_db


def classify(delta_2f_db: float, delta_3f_db: float, confident: bool,
             hysteresis_db: float = 1.0) -> tuple[str, float]:
    """Apply the 2f/3f decision rule on BASELINE-SUBTRACTED deltas.

    Returns (verdict, ratio_db). ratio_db = delta_2f_db - delta_3f_db.
      ratio_db > +hyst  -> SEMICONDUCTOR (even-order dominant)
      ratio_db < -hyst  -> FALSE_JUNCTION (odd-order dominant, corrosion/MOM)
      |ratio_db| <= hyst -> ambiguous; report the stronger but flag borderline.
    `confident` False forces NO_JUNCTION (nothing cleared the noise gate).
    """
    ratio_db = delta_2f_db - delta_3f_db
    if not confident:
        return "NO_JUNCTION", ratio_db
    if ratio_db > hysteresis_db:
        return "SEMICONDUCTOR", ratio_db
    if ratio_db < -hysteresis_db:
        return "FALSE_JUNCTION", ratio_db
    # within the dead-band: lean on which harmonic is actually present/stronger
    return ("SEMICONDUCTOR" if delta_2f_db >= delta_3f_db else "FALSE_JUNCTION"), ratio_db


def _plain_language(verdict: str, ratio_db: float, confident: bool,
                    borderline: bool) -> str:
    """Client-facing sentence (per Matt's plain-language-UI + honesty rules)."""
    if verdict == "BLOCKED":
        return ("Cannot measure: the required TX bandpass and RX highpass filters "
                "are not confirmed installed. Software cannot substitute for them.")
    if verdict == "NO_JUNCTION" or not confident:
        return ("No electronic junction detected in the box. The harmonic return "
                "stayed at the noise floor. NOTE: at this transmitter power this "
                "test only reliably finds blatant electronics inside the shield — "
                "a null result is not proof the object is clean.")
    strength = "clearly" if abs(ratio_db) >= 6 else "marginally"
    if verdict == "SEMICONDUCTOR":
        s = (f"Likely REAL electronics: the 2nd harmonic is {strength} stronger than "
             f"the 3rd (2f/3f = {ratio_db:+.1f} dB), the signature of a semiconductor "
             f"junction.")
    else:
        s = (f"Likely a FALSE junction (corrosion / loose metal contact): the 3rd "
             f"harmonic is {strength} stronger than the 2nd (2f/3f = {ratio_db:+.1f} "
             f"dB), the signature of a metal-oxide contact, not a semiconductor.")
    if borderline:
        s += (" This is a BORDERLINE reading — the two harmonics are close; treat as "
              "inconclusive and re-run, or inspect the object directly.")
    return s


# ---------------------------------------------------------------------------
# Measurement engine — hardware-agnostic (takes a radio-like object)
# ---------------------------------------------------------------------------

class NLJDEngine:
    """Drives baseline + measure against any object implementing the small NLJD
    radio protocol (nljd_tx_cw / nljd_tx_off / nljd_capture_at). B210Source below
    implements it against real UHD; tests implement it with synthetic IQ.
    """

    def __init__(self, cfg, radio):
        self.cfg = cfg
        self.radio = radio
        self.f0_hz = float(cfg.get("nljd", "f0_hz", default=F0_HZ))
        self.tx_bb_offset = float(cfg.get("nljd", "tx_bb_offset_hz", default=TX_BB_OFFSET_HZ))
        self.fs = float(cfg.get("nljd", "rx_sample_rate", default=2_000_000))
        self.nfft = int(cfg.get("nljd", "fft_size", default=1 << 16))
        self.dwell_samples = int(cfg.get("nljd", "dwell_samples", default=1 << 18))
        self.snr_gate_db = float(cfg.get("nljd", "snr_gate_db", default=6.0))
        self.hysteresis_db = float(cfg.get("nljd", "ratio_hysteresis_db", default=1.0))
        self.n_avg = int(cfg.get("nljd", "averages", default=4))
        self.require_hw_filters = bool(cfg.get("nljd", "require_hw_filters", default=True))
        self._baseline: Optional[NLJDBaseline] = None

    # -- geometry: where each harmonic line lands --

    def _harmonic_line_offset(self) -> float:
        """The emitted CW is at f0 + tx_bb_offset. Its Nth harmonic is at
        N*(f0 + tx_bb_offset). We tune the RX LO to N*(f0+tx_bb_offset) - want,
        so the harmonic lands at +`want` baseband. Choose `want` as an integer
        number of FFT bins so the line sits on a bin center (no scalloping)."""
        bin_hz = self.fs / self.nfft
        want = round((self.fs * 0.10) / bin_hz) * bin_hz   # ~10% up from DC, bin-aligned
        return want

    def _dwell(self, n: int) -> HarmonicReading:
        """Transmit CW at f0, capture at the Nth harmonic, average line power."""
        want = self._harmonic_line_offset()
        emit_hz = self.f0_hz + self.tx_bb_offset
        harm_hz = n * emit_hz
        rx_lo = harm_hz - want
        lines, floors = [], []
        for _ in range(self.n_avg):
            iq = self.radio.nljd_capture_at(rx_lo, self.fs, self.dwell_samples)
            lp, nf, _ = extract_line_power(iq, self.fs, want, self.nfft)
            lines.append(lp)
            floors.append(nf)
        # average in the power domain for the line, median for the floor
        line_db = 10.0 * np.log10(np.mean(10.0 ** (np.array(lines) / 10.0)) + 1e-20)
        floor_db = float(np.median(floors))
        snr = line_db - floor_db
        return HarmonicReading(n=n, freq_mhz=harm_hz / 1e6, line_power_db=line_db,
                               noise_floor_db=floor_db, snr_db=snr,
                               present=snr >= self.snr_gate_db)

    # -- public API --

    def measure_baseline(self, note: str = "") -> NLJDBaseline:
        """EMPTY-CAVITY reference. Operator runs this with the box closed and
        EMPTY. Captures the box's own harmonic leakage floor (SDR self-harmonics
        that survived the TX filter + cavity resonances) so a later measurement
        can subtract it and report only the object's contribution."""
        self.radio.nljd_tx_cw(self.f0_hz, self.tx_bb_offset)
        try:
            readings, floors = {}, {}
            for n in HARMONICS:
                r = self._dwell(n)
                readings[str(n)] = r.line_power_db
                floors[str(n)] = r.noise_floor_db
        finally:
            self.radio.nljd_tx_off()
        self._baseline = NLJDBaseline(
            f0_mhz=self.f0_hz / 1e6, readings=readings, noise_floor_db=floors,
            ts=time.time(), n_avg=self.n_avg, note=note)
        return self._baseline

    def measure(self, baseline: Optional[NLJDBaseline] = None) -> NLJDVerdict:
        """OBJECT-PRESENT measurement. Requires a baseline (arg or last captured).
        Subtracts the empty-box reference and classifies the residual."""
        base = baseline or self._baseline
        warnings: list[str] = []
        hw_ok = bool(self.radio.nljd_hw_filters_attested())
        if self.require_hw_filters and not hw_ok:
            v = NLJDVerdict(verdict="BLOCKED",
                            plain=_plain_language("BLOCKED", 0.0, False, False),
                            ratio_db=0.0, delta_2f_db=0.0, delta_3f_db=0.0,
                            harmonics=[], confident=False, hw_filters_attested=False,
                            warnings=["required TX bandpass / RX highpass not attested"])
            return v
        if base is None:
            raise DeviceError("run measure_baseline (empty box) before measure()")

        self.radio.nljd_tx_cw(self.f0_hz, self.tx_bb_offset)
        try:
            harmonics = [self._dwell(n) for n in HARMONICS]
        finally:
            self.radio.nljd_tx_off()

        r2 = next(h for h in harmonics if h.n == 2)
        r3 = next(h for h in harmonics if h.n == 3)
        delta_2f = r2.line_power_db - base.readings.get("2", r2.line_power_db)
        delta_3f = r3.line_power_db - base.readings.get("3", r3.line_power_db)
        # "confident" = at least one harmonic BOTH clears the absolute noise gate
        # (a real line, not fabricated from noise) AND rises by at least
        # `delta_gate_db` above the empty-box baseline. The delta requirement is the
        # honesty crux: the empty box ALWAYS shows a self-leakage line, so a bare
        # "line present" is not a detection — only a line the OBJECT pushed up is.
        delta_gate = float(self.cfg.get("nljd", "delta_gate_db", default=3.0))
        confident = ((r2.present and delta_2f >= delta_gate)
                     or (r3.present and delta_3f >= delta_gate))
        verdict, ratio_db = classify(delta_2f, delta_3f, confident, self.hysteresis_db)
        borderline = confident and abs(ratio_db) <= self.hysteresis_db
        if not hw_ok:
            warnings.append("hardware TX/RX filters NOT attested — result advisory only")
        plain = _plain_language(verdict, ratio_db, confident, borderline)
        return NLJDVerdict(verdict=verdict, plain=plain, ratio_db=round(ratio_db, 2),
                           delta_2f_db=round(delta_2f, 2), delta_3f_db=round(delta_3f, 2),
                           harmonics=harmonics, confident=confident,
                           hw_filters_attested=hw_ok, warnings=warnings)


# ---------------------------------------------------------------------------
# B210 device: RX inherits AD936xSource unchanged; TX path added here.
# ---------------------------------------------------------------------------

class B210Source(AD936xSource):
    """Ettus/NI USRP B210 (AD9361, 2 TX / 2 RX, 70 MHz-6 GHz, up to 56 MHz BW,
    UHD driver). As a spectrum SOURCE it behaves exactly like the Pluto (stepped
    sweep + Welch PSD + stitch — all inherited). It ADDS a coherent CW transmit
    path so it can drive the NLJD. TX-A and RX-A share the FPGA timebase, so the
    harmonic dwell is coherent with the illumination.

    UHD is imported lazily (like pyadi-iio in PlutoSource) so importing this module
    never requires the driver; the synthetic test radio needs no UHD at all.
    """

    channels = 2
    is_swept_sdr = True

    def __init__(self, cfg):
        super().__init__(cfg)
        self.info = DeviceInfo(driver="uhd_b210")
        self._args = str(cfg.get("b210", "device_args", default="type=b200"))
        self._tx_gain = float(cfg.get("b210", "tx_gain_db", default=70.0))
        self._usrp = None
        self._tx_streamer = None
        self._tx_stop = None
        self._tx_thread = None
        # operator attestation that the physical filters are installed (persisted
        # in config so the UI checkbox survives restarts). Software CANNOT verify
        # this electrically — it is an honest operator gate, not a claim.
        self._hw_filters = bool(cfg.get("nljd", "hw_filters_installed", default=False))

    # ---- UHD open (mirrors PlutoSource._open_radio structure) ----
    def _open_radio(self):
        try:
            import uhd  # noqa: F401
        except ImportError as e:
            raise DeviceError(
                "UHD Python API not installed (pip install uhd, plus the UHD "
                "runtime / FX3 firmware for the B210). Required for the B210.") from e
        import uhd
        try:
            self._usrp = uhd.usrp.MultiUSRP(self._args)
        except Exception as e:  # noqa: BLE001
            raise DeviceError(f"could not open B210 ({self._args}): {e}. Plugged into "
                              f"USB 3.0? UHD images downloaded (uhd_images_downloader)?") from e
        self.info.port = self._args
        self.info.model = "USRP B210 (AD9361)"
        # tell AD936xSource's RX engine how to reach the radio
        self.sdr = _UHDRxAdapter(self._usrp)   # provides the rx()/attr shims RX uses

    # NOTE: _set_lo/_set_sample_rate/_set_rf_bandwidth/_set_buffer_size/_set_gain/
    # _read_iq for the RECEIVE path are implemented on _UHDRxAdapter so the whole
    # inherited stepped-sweep read_sweep() works verbatim. See that class.

    def _set_lo(self, hz):            self.sdr.set_rx_lo(hz)
    def _set_sample_rate(self, hz):   self.sdr.set_rx_rate(hz)
    def _set_rf_bandwidth(self, hz):  self.sdr.set_rx_bw(hz)
    def _set_buffer_size(self, n):    self.sdr.set_rx_nsamps(n)
    def _set_gain(self, mode, db):    self.sdr.set_rx_gain(mode, db)
    def _read_iq(self):               return self.sdr.recv()
    def _flush_buffer(self):          self.sdr.flush()

    def connect(self) -> DeviceInfo:
        info = super().connect()
        # B210 reports TWO RX chains -> expose as left/right so DF workflows map on
        info.modules = [ModuleInfo("right", "B210 RX-A", 70.0, 6000.0),
                        ModuleInfo("left", "B210 RX-B", 70.0, 6000.0)]
        return info

    # ================= NLJD radio protocol (consumed by NLJDEngine) =============

    def nljd_hw_filters_attested(self) -> bool:
        return self._hw_filters

    def set_hw_filters_attested(self, val: bool):
        self._hw_filters = bool(val)
        try:
            self.cfg.set_and_save(("nljd", "hw_filters_installed"), self._hw_filters)
        except Exception:  # noqa: BLE001
            log.exception("could not persist nljd.hw_filters_installed")

    def nljd_tx_cw(self, f0_hz: float, bb_offset_hz: float):
        """Start a continuous CW tone on TX-A at f0_hz + bb_offset_hz.
        Implementation: tune TX LO to f0_hz, then stream a complex exponential at
        +bb_offset_hz baseband so the EMITTED line is at f0_hz+bb_offset_hz, off
        the TX LO/DC spur. Runs on a background thread until nljd_tx_off()."""
        import threading
        import uhd
        st = uhd.types.StreamArgs("fc32", "sc16")
        st.channels = [0]                      # TX-A
        self._usrp.set_tx_rate(self.fs_tx)
        self._usrp.set_tx_freq(uhd.types.TuneRequest(f0_hz), 0)
        self._usrp.set_tx_gain(self._tx_gain, 0)
        self._tx_streamer = self._usrp.get_tx_stream(st)
        block = 4096
        n = np.arange(block)
        phase_step = 2.0 * np.pi * bb_offset_hz / self.fs_tx
        # 0.5 amplitude so a bench-tuned tone doesn't clip the DAC (headroom for the
        # TX bandpass insertion + coupler). NEVER command full-scale into the filter.
        tone = (0.5 * np.exp(1j * phase_step * n)).astype(np.complex64)
        self._tx_stop = threading.Event()

        def _pump():
            md = uhd.types.TXMetadata()
            md.start_of_burst = True
            md.has_time_spec = False
            phase = 0.0
            while not self._tx_stop.is_set():
                blk = (0.5 * np.exp(1j * (phase_step * n + phase))).astype(np.complex64)
                phase = (phase + phase_step * block) % (2.0 * np.pi)
                try:
                    self._tx_streamer.send(blk, md)
                except Exception:  # noqa: BLE001
                    break
                md.start_of_burst = False
            # graceful end-of-burst
            try:
                end = uhd.types.TXMetadata()
                end.end_of_burst = True
                self._tx_streamer.send(np.zeros(1, dtype=np.complex64), end)
            except Exception:  # noqa: BLE001
                pass

        self._tx_thread = threading.Thread(target=_pump, daemon=True)
        self._tx_thread.start()

    def nljd_tx_off(self):
        if self._tx_stop is not None:
            self._tx_stop.set()
        if self._tx_thread is not None:
            self._tx_thread.join(timeout=1.0)
        self._tx_streamer = None
        self._tx_thread = None
        self._tx_stop = None

    @property
    def fs_tx(self) -> float:
        return float(self.cfg.get("nljd", "tx_sample_rate", default=2_000_000))

    def nljd_capture_at(self, rx_lo_hz: float, fs: float, n_samples: int) -> np.ndarray:
        """One IQ capture at rx_lo_hz on RX-A while TX-A is transmitting. Uses the
        inherited capture_iq path (retune -> flush stale -> grab), so it shares the
        exact same settle/flush semantics the rest of the app trusts."""
        return self.capture_iq(rx_lo_hz, fs, n_samples)


class _UHDRxAdapter:
    """Thin shim presenting the handful of RX operations AD936xSource calls
    (set_rx_lo/rate/bw/gain, recv, flush) on top of uhd.usrp.MultiUSRP. Keeps the
    UHD-specific streamer bookkeeping out of the AD936x sweep engine so that engine
    stays driver-neutral. Only RX-A (channel 0) is wired for the receive path;
    RX-B is reserved for the optional TX-leakage reference tap."""

    def __init__(self, usrp):
        import uhd
        self._uhd = uhd
        self.usrp = usrp
        self._rate = 2_000_000.0
        self._nsamps = 1 << 16
        self._rx = None

    def _ensure_streamer(self):
        if self._rx is None:
            st = self._uhd.types.StreamArgs("fc32", "sc16")
            st.channels = [0]
            self._rx = self.usrp.get_rx_stream(st)

    def set_rx_lo(self, hz):   self.usrp.set_rx_freq(self._uhd.types.TuneRequest(float(hz)), 0)
    def set_rx_rate(self, hz): self._rate = float(hz); self.usrp.set_rx_rate(float(hz), 0)
    def set_rx_bw(self, hz):   self.usrp.set_rx_bandwidth(float(hz), 0)
    def set_rx_nsamps(self, n): self._nsamps = int(n)

    def set_rx_gain(self, mode, db):
        if mode == "manual":
            self.usrp.set_rx_gain(float(db), 0)
        else:
            try:
                self.usrp.set_rx_agc(True, 0)
            except Exception:  # noqa: BLE001
                self.usrp.set_rx_gain(40.0, 0)

    def recv(self) -> np.ndarray:
        self._ensure_streamer()
        buff = np.zeros(self._nsamps, dtype=np.complex64)
        md = self._uhd.types.RXMetadata()
        cmd = self._uhd.types.StreamCMD(self._uhd.types.StreamMode.num_done)
        cmd.num_samps = self._nsamps
        cmd.stream_now = True
        self._rx.issue_stream_cmd(cmd)
        got, view = 0, buff
        while got < self._nsamps:
            n = self._rx.recv(view[got:], md, 3.0)
            if n == 0:
                break
            got += n
        return buff[:got] if got else buff

    def flush(self):
        # a retune leaves stale samples queued; one throwaway recv clears them
        try:
            self.recv()
        except Exception:  # noqa: BLE001
            pass
