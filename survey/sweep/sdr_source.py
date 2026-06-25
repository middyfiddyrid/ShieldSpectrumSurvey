"""ADALM-Pluto / AD936x spectrum source.

This is a DROP-IN for the RF Explorer device layer. The rest of the app talks to
it through exactly the same `BaseDevice` contract (see device.py): connect /
select_module / set_range / set_calculator / read_sweep -> SweepFrame(freqs_mhz,
amps_dbm) / stream / collect / close. Nothing above this file (planner, runner,
detector, UI, reports) knows or cares that the samples came from an SDR.

The fundamental difference, hidden here:
  - The RF Explorer returns a FINISHED swept spectrum (amplitude vs frequency in
    dBm) over serial.
  - The Pluto returns raw complex IQ at ONE center frequency over a limited
    instantaneous bandwidth (= its sample rate).

So to answer a `set_range(lo, hi)` + `read_sweep()` the way the RF Explorer does,
this driver internally:
    tune -> capture IQ -> Welch PSD -> keep the usable central fraction of the
    segment -> retune with overlap -> stitch into one contiguous (freq, power)
    trace resampled onto a stable grid.
Each `read_sweep()` is one full re-sweep of the requested range, so the runner's
host-side averaging / max-peak accumulation works unchanged.

AD936x-common logic lives in `AD936xSource`; `PlutoSource` is the concrete
single-channel (1R1T) Pluto. A future `B220Source` (AD9361, 2R2T) subclasses
`AD936xSource` and sets `channels = 2` for true simultaneous dual-channel DF —
the contract does not change.

CALIBRATION HONESTY: the Pluto is not an absolute-calibrated instrument. Power is
reported as PSD in dBFS plus a single documented offset constant
(`pluto.cal_offset_db`) to approximate dBm. Lean on baseline-delta detection
(anomaly vs captured room baseline), NOT lab-grade absolute dBm.

This is RECEIVE-ONLY. Only RX is ever configured; no transmit path is touched.
"""

import logging
import os
import time
from typing import Optional

import numpy as np

from .device import BaseDevice, DeviceError, DeviceInfo, ModuleInfo, SweepFrame

log = logging.getLogger("survey.sdr")


def _ensure_libiio_on_path(libiio_dir: str = ""):
    """On Windows, pyadi-iio/pylibiio need the native libiio.dll discoverable. A
    process launched from a GUI/shortcut may not have inherited the user PATH
    edit, so explicitly register the bundled libiio dir (config pluto.libiio_dir,
    else %LOCALAPPDATA%\\libiio\\bin) before importing adi. No-op off Windows."""
    if os.name != "nt":
        return
    candidates = [libiio_dir] if libiio_dir else []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(os.path.join(local, "libiio", "bin"))
    for d in candidates:
        if d and os.path.isdir(d):
            try:
                os.add_dll_directory(d)
            except (OSError, AttributeError):
                pass
            if d.lower() not in os.environ.get("PATH", "").lower():
                os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
            return


# ---------------------------------------------------------------------------
# Pure DSP — no hardware, unit-tested in tests/test_sdr_source.py
# ---------------------------------------------------------------------------

def hann(n: int) -> np.ndarray:
    """Periodic Hann window (matches numpy.hanning's DFT-friendly sibling)."""
    if n <= 1:
        return np.ones(max(n, 1))
    return 0.5 - 0.5 * np.cos(2.0 * np.pi * np.arange(n) / n)


def welch_psd(iq: np.ndarray, fs: float, nperseg: int = 1024,
              overlap: float = 0.5) -> tuple[np.ndarray, np.ndarray]:
    """Welch PSD of a complex baseband capture, numpy-only (no scipy).

    Returns (baseband_freqs_hz, psd_db) with DC at the CENTER (fftshifted), so
    the frequency axis runs -fs/2 .. +fs/2. psd_db is 10*log10 of the averaged
    periodogram — relative dBFS-ish, NOT absolute dBm (caller adds a cal offset).
    """
    iq = np.asarray(iq, dtype=np.complex64)
    N = iq.size
    if N == 0:
        raise ValueError("empty IQ buffer")
    nperseg = int(min(nperseg, N))
    win = hann(nperseg)
    winpow = float(np.sum(win ** 2)) or 1.0
    step = max(1, int(nperseg * (1.0 - overlap)))

    acc = np.zeros(nperseg, dtype=np.float64)
    nseg = 0
    for start in range(0, N - nperseg + 1, step):
        seg = iq[start:start + nperseg] * win
        sp = np.fft.fftshift(np.fft.fft(seg))
        acc += np.abs(sp) ** 2
        nseg += 1
    if nseg == 0:  # buffer shorter than one segment — single windowed FFT
        seg = iq[:nperseg] * win[:N]
        sp = np.fft.fftshift(np.fft.fft(seg))
        acc = np.abs(sp) ** 2
        nseg = 1

    psd = acc / (nseg * fs * winpow)
    psd_db = 10.0 * np.log10(psd + 1e-20)
    freqs = np.fft.fftshift(np.fft.fftfreq(nperseg, d=1.0 / fs))
    return freqs, psd_db


def notch_dc(baseband_freqs: np.ndarray, psd_db: np.ndarray,
             half_bins: int = 2) -> np.ndarray:
    """Mitigate the DC / LO-leakage spike at the tune center.

    Design choice — NOTCH, not offset-tune. We keep ~80% of each segment to beat
    the analog filter edge rolloff; with only ~20% total edge budget there is no
    room to park the LO-leakage bin OUTSIDE the kept window via offset tuning
    (that would need keeping <=50% and ~doubling the step count). So instead we
    replace the few central bins with a linear interpolation across the notch and
    rely on inter-segment OVERLAP (see plan_substeps) so a real emitter sitting
    exactly on a former tune-center is still seen by a neighbouring segment.
    A TSCM sweep must never false-positive on its own LO; this guarantees it.
    """
    psd_db = np.array(psd_db, dtype=np.float64, copy=True)
    n = psd_db.size
    c = int(np.argmin(np.abs(baseband_freqs)))  # the 0 Hz bin
    a, b = max(0, c - half_bins), min(n - 1, c + half_bins)
    if a > 0 and b < n - 1:
        psd_db[a:b + 1] = np.linspace(psd_db[a - 1], psd_db[b + 1], b - a + 1)
    return psd_db


def plan_substeps(lo_hz: float, hi_hz: float, inst_bw_hz: float,
                  keep_fraction: float = 0.8,
                  overlap: float = 0.5) -> list[float]:
    """Tune-center plan covering [lo, hi] with overlapping usable windows.

    Each tune keeps a central `keep_fraction` of its instantaneous bandwidth;
    consecutive centers advance by keep_bw*(1-overlap) so kept windows overlap.
    The overlap both smooths segment seams and, crucially, fills the DC notch of
    each center: with overlap >= 0.5 the step is <= keep_bw/2, so every former
    tune-center falls inside a NEIGHBOUR's kept window and an emitter sitting
    exactly on a tune-center is still recovered (it would otherwise be notched).
    """
    keep_bw = inst_bw_hz * keep_fraction
    if keep_bw <= 0:
        raise ValueError("instantaneous bandwidth * keep_fraction must be > 0")
    step = max(keep_bw * (1.0 - overlap), keep_bw * 0.05)
    centers = []
    c = lo_hz + keep_bw / 2.0
    # guard against absurd plans (e.g. misconfigured tiny bandwidth)
    max_steps = 100000
    while (c - keep_bw / 2.0) < hi_hz and len(centers) < max_steps:
        centers.append(c)
        c += step
    if not centers:
        centers = [(lo_hz + hi_hz) / 2.0]
    return centers


def stitch_psd(grid_hz: np.ndarray, segments: list[tuple[np.ndarray, np.ndarray]]
               ) -> np.ndarray:
    """Stitch per-segment (kept_freqs_hz, kept_psd_db) onto a common grid.

    Overlapping contributions are averaged in the POWER domain (correct for noise
    floor) then converted back to dB. Grid points with no contributor are filled
    by interpolation across the gap so the returned trace is contiguous.
    """
    grid_hz = np.asarray(grid_hz, dtype=np.float64)
    power = np.zeros(grid_hz.size, dtype=np.float64)
    count = np.zeros(grid_hz.size, dtype=np.float64)
    for f_keep, p_keep in segments:
        if f_keep.size == 0:
            continue
        order = np.argsort(f_keep)
        f_keep, p_keep = f_keep[order], p_keep[order]
        mask = (grid_hz >= f_keep[0]) & (grid_hz <= f_keep[-1])
        if not np.any(mask):
            continue
        interp_db = np.interp(grid_hz[mask], f_keep, p_keep)
        power[mask] += 10.0 ** (interp_db / 10.0)
        count[mask] += 1.0
    out = np.full(grid_hz.size, np.nan, dtype=np.float64)
    hit = count > 0
    out[hit] = 10.0 * np.log10(power[hit] / count[hit] + 1e-20)
    if np.any(~hit) and np.any(hit):  # fill seams/gaps so the trace is contiguous
        idx = np.arange(grid_hz.size)
        out[~hit] = np.interp(idx[~hit], idx[hit], out[hit])
    elif np.all(~hit):
        out[:] = -150.0
    return out


# ---------------------------------------------------------------------------
# AD936x common source (stepped-sweep + PSD + stitch). Hardware-agnostic except
# for _open_radio / _tune / _grab_iq, which concrete subclasses provide.
# ---------------------------------------------------------------------------

class AD936xSource(BaseDevice):
    """Shared AD936x stepped-sweep engine. Subclasses bind the actual radio."""

    # single-RX by default; B220 (2R2T) overrides to 2
    channels = 1
    # each read_sweep() is a complete, internally-averaged stepped sweep — the
    # runner uses this to cap redundant host-side averaging passes (speed).
    is_swept_sdr = True

    def __init__(self, cfg):
        self.cfg = cfg
        self.sdr = None
        self.info = DeviceInfo(driver="ad936x")
        self._fs = float(cfg.get("pluto", "sample_rate", default=20_000_000))
        self._keep = float(cfg.get("pluto", "keep_fraction", default=0.8))
        self._overlap = float(cfg.get("pluto", "overlap", default=0.5))
        self._settle_s = float(cfg.get("pluto", "settle_seconds", default=0.05))
        self._nsamp = int(cfg.get("pluto", "capture_samples", default=1 << 16))
        self._nperseg = int(cfg.get("pluto", "fft_size", default=1024))
        self._dc_notch_bins = int(cfg.get("pluto", "dc_notch_bins", default=2))
        # central guard band (Hz) DISCARDED at every tune center to kill the DC /
        # LO-leakage spike; overlapping neighbours refill it (see read_sweep).
        self._dc_guard_hz = float(cfg.get("pluto", "dc_guard_hz", default=250_000))
        self._cal_offset = float(cfg.get("pluto", "cal_offset_db", default=-30.0))
        self._sweep_bins = int(cfg.get("pluto", "sweep_bins", default=2048))
        self._gain_mode = str(cfg.get("pluto", "gain_mode", default="manual"))
        self._gain_db = float(cfg.get("pluto", "gain_db", default=64.0))
        # per-set_range state (a stable grid so every read_sweep frame matches)
        self._range_hz = None
        self._grid_hz = None
        self._centers = None
        self._last_sweep_seconds = 0.0

    # -- subclass hooks --
    def _open_radio(self): raise NotImplementedError
    def _set_lo(self, hz: float): raise NotImplementedError
    def _set_sample_rate(self, hz: float): raise NotImplementedError
    def _set_rf_bandwidth(self, hz: float): raise NotImplementedError
    def _set_buffer_size(self, n: int): raise NotImplementedError
    def _set_gain(self, mode: str, db: float): raise NotImplementedError
    def _read_iq(self) -> np.ndarray: raise NotImplementedError
    def _flush_buffer(self): pass

    # -- lifecycle --

    def connect(self) -> DeviceInfo:
        self._open_radio()
        self._set_sample_rate(int(self._fs))
        self._set_rf_bandwidth(int(self._fs))
        self._set_buffer_size(self._nsamp)
        self._set_gain(self._gain_mode, self._gain_db)
        lo_min, lo_max = self._probe_tuning_range()
        # The Pluto has ONE wideband RX front end. The rest of the app models the
        # device as left/right SMA ports (RF Explorer dual-module); map our single
        # RX onto "right" (the RFE's wideband expansion port) so the existing
        # ambient / bug-scan / interior sweep flows drive it with no UI changes.
        # The future B220 (2R2T) will report two modules.
        self.info.modules = [ModuleInfo("right", self.info.model or "AD936x",
                                        lo_min / 1e6, lo_max / 1e6)]
        # NOTE: DeviceInfo.check_expected() is RF-Explorer-specific (6G Combo
        # PLUS / fw 03.45) and is intentionally NOT called here.
        self._validate_required_bands(lo_min, lo_max)
        return self.info

    def _probe_tuning_range(self) -> tuple[float, float]:
        """Find the real tunable range. Start from the configured/known limits
        and confirm the extremes actually tune; clamp + warn if the hardware
        rejects them (e.g. an un-extended AD9363A topping out at 3.8 GHz)."""
        want_min = float(self.cfg.get("pluto", "freq_min_hz", default=325_000_000))
        want_max = float(self.cfg.get("pluto", "freq_max_hz", default=3_800_000_000))
        real_min = self._confirm_tunable(want_min, ascending=True,
                                         ladder=[70e6, 200e6, 325e6, 400e6])
        real_max = self._confirm_tunable(want_max, ascending=False,
                                         ladder=[6000e6, 5800e6, 3800e6, 3000e6])
        if real_min > want_min + 1e6:
            self.info.warnings.append(
                f"radio floor is {real_min/1e6:.0f} MHz, not the requested "
                f"{want_min/1e6:.0f} MHz")
        if real_max < want_max - 1e6:
            self.info.warnings.append(
                f"radio ceiling is {real_max/1e6:.0f} MHz, not the requested "
                f"{want_max/1e6:.0f} MHz — frequency extension not applied")
        log.info("AD936x tunable range confirmed: %.1f-%.1f MHz",
                 real_min / 1e6, real_max / 1e6)
        return real_min, real_max

    def _confirm_tunable(self, target_hz: float, ascending: bool,
                         ladder: list[float]) -> float:
        """Try `target`, else walk a ladder toward the center until one tunes."""
        if self._try_tune(target_hz):
            return target_hz
        seq = sorted(ladder) if ascending else sorted(ladder, reverse=True)
        for f in seq:
            if self._try_tune(f):
                return f
        raise DeviceError("radio rejected every probed tune frequency")

    def _try_tune(self, hz: float) -> bool:
        try:
            self._set_lo(int(hz))
            return True
        except (OSError, ValueError, RuntimeError):
            return False

    def _validate_required_bands(self, lo_min: float, lo_max: float):
        """If config demands a band the radio can't reach, fail LOUDLY rather
        than silently returning garbage (per spec)."""
        if not self.cfg.get("pluto", "require_extended", default=False):
            return
        need_lo = float(self.cfg.get("pluto", "freq_min_hz", default=70e6))
        need_hi = float(self.cfg.get("pluto", "freq_max_hz", default=6000e6))
        if lo_min > need_lo + 1e6 or lo_max < need_hi - 1e6:
            raise DeviceError(
                f"pluto.require_extended is set but the radio only tunes "
                f"{lo_min/1e6:.0f}-{lo_max/1e6:.0f} MHz (need "
                f"{need_lo/1e6:.0f}-{need_hi/1e6:.0f} MHz). Apply the AD9364 "
                f"frequency extension or clear pluto.require_extended.")

    # -- contract methods (mirror the RF Explorer device) --

    def select_module(self, side: str):
        """The Pluto is single-RX: it has one front end. Accept whatever side the
        runner asks for (left/right/rx) — there is nothing to switch. The B220
        subclass overrides this to map a side to one of its two RX channels."""
        return

    def set_calculator(self, mode_name: str):
        """No on-device DSP (the RF Explorer has average/maxhold in firmware).
        The runner's host-side Accumulator already computes running average and
        max-peak from the realtime frame stream, so this is a deliberate no-op;
        transient capture comes from streaming more frames during the maxhold
        dwell, not a device mode."""
        return

    def set_range(self, start_mhz: float, end_mhz: float):
        lo_hz, hi_hz = start_mhz * 1e6, end_mhz * 1e6
        if hi_hz <= lo_hz:
            raise DeviceError(f"bad range {start_mhz}-{end_mhz} MHz")
        self._range_hz = (lo_hz, hi_hz)
        self._centers = plan_substeps(lo_hz, hi_hz, self._fs, self._keep,
                                      self._overlap)
        n = max(16, min(self._sweep_bins, 1 + int((hi_hz - lo_hz) / 1e3)))
        self._grid_hz = np.linspace(lo_hz, hi_hz, n)

    def read_sweep(self, timeout_s: float = 5.0) -> Optional[SweepFrame]:
        if self._range_hz is None:
            raise DeviceError("read_sweep before set_range")
        t0 = time.time()
        keep_half = self._keep * self._fs / 2.0
        guard = self._dc_guard_hz
        segments = []
        for center in self._centers:
            self._set_lo(int(center))
            if self._settle_s > 0:
                time.sleep(self._settle_s)
            self._flush_buffer()           # discard the first (stale) buffer
            iq = self._read_iq()
            f_bb, psd_db = welch_psd(iq, self._fs, self._nperseg)
            # Keep the central usable fraction, but DISCARD a guard band around DC
            # (the LO-leakage spike). With overlap >= 0.5 every discarded center is
            # refilled by a neighbour segment that sees it cleanly off its own DC,
            # so the sweep never false-positives on its own LO and loses no real
            # spectrum. (notch_dc remains available but a hard discard is more
            # robust than interpolation against the real radio's wide DC skirt.)
            keep = (np.abs(f_bb) <= keep_half) & (np.abs(f_bb) >= guard)
            segments.append((center + f_bb[keep], psd_db[keep]))
        stitched = stitch_psd(self._grid_hz, segments)
        amps_dbm = stitched + self._cal_offset
        self._last_sweep_seconds = time.time() - t0
        self.info.last_sweep_seconds = round(self._last_sweep_seconds, 2)
        return SweepFrame([f / 1e6 for f in self._grid_hz],
                          [float(a) for a in amps_dbm], time.time())

    # AD936x returns 12-bit samples scaled to roughly +/-2048 ADC counts.
    ADC_FULL_SCALE = 2048.0

    def capture_iq(self, center_hz: float, sample_rate: float,
                   n_samples: int) -> np.ndarray:
        """Complex IQ at one center — for the future demod / BLE listen layer
        (see DEMOD_LISTEN_DESIGN.md). The RF Explorer never could feed this; the
        Pluto can. Returns complex64 NORMALISED to ~[-1, 1] (raw ADC counts /
        full scale), so downstream demod sees float IQ regardless of bit depth."""
        self._set_sample_rate(int(sample_rate))
        self._set_rf_bandwidth(int(sample_rate))
        self._set_buffer_size(int(n_samples))
        self._set_lo(int(center_hz))
        if self._settle_s > 0:
            time.sleep(self._settle_s)
        self._flush_buffer()
        iq = np.asarray(self._read_iq(), dtype=np.complex64) / self.ADC_FULL_SCALE
        # restore sweep config so a subsequent read_sweep is unaffected
        self._set_sample_rate(int(self._fs))
        self._set_rf_bandwidth(int(self._fs))
        self._set_buffer_size(self._nsamp)
        return iq

    def stream_iq(self, center_hz: float, sample_rate: float, n_block: int, stop):
        """Continuous IQ generator for live audio: tune ONCE, then yield blocks
        until `stop` (a threading.Event) is set. Restores the sweep config on
        exit so a later sweep/read is unaffected. Caller must serialise device
        access (only one stream/capture/sweep at a time)."""
        self._set_sample_rate(int(sample_rate))
        self._set_rf_bandwidth(int(sample_rate))
        self._set_lo(int(center_hz))
        # Re-assert gapless DMA HERE: a prior capture/sweep destroys+recreates the
        # rx buffer, which resets the kernel-buffer count to 1 (single-buffer
        # refill gap => ~0.6x). Destroy, set count, THEN build the buffer and
        # never destroy it again inside the loop.
        try:
            self.sdr.rx_destroy_buffer()
            self.sdr._rxadc.set_kernel_buffers_count(4)
        except Exception:  # noqa: BLE001
            pass
        self._set_buffer_size(int(n_block))
        if self._settle_s > 0:
            time.sleep(self._settle_s)
        try:
            self.sdr.rx()      # prime (first buffer post-retune is stale)
        except Exception:  # noqa: BLE001
            pass
        try:
            while not stop.is_set():
                yield np.asarray(self._read_iq(), dtype=np.complex64) / self.ADC_FULL_SCALE
        finally:
            try:
                self.sdr.rx_destroy_buffer()
            except Exception:  # noqa: BLE001
                pass
            self._set_sample_rate(int(self._fs))
            self._set_rf_bandwidth(int(self._fs))
            self._set_buffer_size(self._nsamp)

    def is_alive(self) -> bool:
        return self.sdr is not None

    def close(self):
        if self.sdr is not None:
            log.info("%s.close()", type(self).__name__)
            try:
                del self.sdr
            except Exception:  # noqa: BLE001
                pass
            self.sdr = None


# ---------------------------------------------------------------------------
# Concrete ADALM-Pluto (single RX, 1R1T)
# ---------------------------------------------------------------------------

class PlutoSource(AD936xSource):
    channels = 1

    def __init__(self, cfg):
        super().__init__(cfg)
        self.info.driver = "pluto"
        self._uri = (os.environ.get("PLUTO_URI")
                     or cfg.get("pluto", "uri", default="ip:192.168.2.1"))

    def _open_radio(self):
        _ensure_libiio_on_path(self.cfg.get("pluto", "libiio_dir", default=""))
        try:
            import adi
        except ImportError as e:
            raise DeviceError(
                "pyadi-iio not installed (pip install pyadi-iio) — and on "
                "Windows the native libiio runtime must be on PATH") from e
        # A fast reconnect can race the previous libiio context's release (the
        # radio reports busy for a beat), so retry a few times before giving up.
        last = None
        for attempt in range(4):
            try:
                self.sdr = adi.Pluto(uri=self._uri)
                last = None
                break
            except Exception as e:  # noqa: BLE001 - libiio raises bare OSError/Exception
                last = e
                time.sleep(0.6)
        if last is not None:
            raise DeviceError(
                f"could not open Pluto at {self._uri}: {last}. Is it plugged in, "
                f"are the ADI USB drivers installed, and is another copy of the app "
                f"already using it?") from last
        self.info.port = self._uri
        # gapless DMA for real-time audio streaming — set ONCE, before any rx
        # buffer is created, so it persists for the whole session.
        try:
            self.sdr._rxadc.set_kernel_buffers_count(4)
        except Exception:  # noqa: BLE001
            pass
        ctx = getattr(self.sdr, "_ctx", None)
        self.info.model = "ADALM-Pluto (AD936x)"
        if ctx is not None:
            try:
                self.info.model = ctx.attrs.get("hw_model", self.info.model).value
            except Exception:  # noqa: BLE001
                pass

    def _set_lo(self, hz: float):
        self.sdr.rx_lo = int(hz)

    def _set_sample_rate(self, hz: float):
        self.sdr.sample_rate = int(hz)

    def _set_rf_bandwidth(self, hz: float):
        self.sdr.rx_rf_bandwidth = int(hz)

    def _set_buffer_size(self, n: int):
        self.sdr.rx_buffer_size = int(n)

    def _set_gain(self, mode: str, db: float):
        # Manual gain at a FIXED level for repeatable baseline comparison;
        # slow_attack AGC only for first-look survey (per spec).
        self.sdr.gain_control_mode_chan0 = mode
        if mode == "manual":
            self.sdr.rx_hardwaregain_chan0 = float(db)

    def _flush_buffer(self):
        try:
            self.sdr.rx_destroy_buffer()
        except Exception:  # noqa: BLE001
            pass
        self.sdr.rx()  # the first buffer after a retune holds stale samples

    def _read_iq(self) -> np.ndarray:
        return np.asarray(self.sdr.rx(), dtype=np.complex64)
