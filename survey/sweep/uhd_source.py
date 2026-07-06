"""SUOGOEST B220 Mini (USRP B210-compatible, AD9361 2R2T) spectrum source.

The B220 Mini enumerates as an Ettus USRP B2xx (VID 2500 PID 0020) and is
driven by the UHD host library — NOT libiio/pyadi-iio like the Pluto. There
are no official UHD Python wheels for Windows/Python 3.14, so this module
binds uhd.dll's stable C API directly with ctypes (`_UHDLib` + `UHDRadio`),
then plugs into the exact same stepped-sweep engine the Pluto uses:
`B220Source` subclasses `AD936xSource` (see sdr_source.py) and only provides
the radio-binding hooks. Planner/runner/detector/UI stay untouched, as
designed ("A future B220Source ... sets channels = 2").

What the second receive channel buys the app: the RF Explorer models a
LEFT/RIGHT dual-module device, and until now every SDR mapped its single RX
onto "right". The B220 reports BOTH modules — right = RX channel 0,
left = RX channel 1 — so the existing dual-phase interior sweep drives two
physically separate antenna ports with no UI changes.

CALIBRATION HONESTY: like the Pluto, this is not an absolute-calibrated
instrument. Samples arrive as fc32 normalised to ~[-1, 1] (so
ADC_FULL_SCALE = 1.0) and power is PSD-plus-offset (`b220.cal_offset_db`).
Lean on baseline-delta detection, not absolute dBm.

RECEIVE-ONLY: only RX is ever configured; no transmit path is touched.
"""

import ctypes
import ctypes.util
import logging
import os
import time
from typing import Optional

import numpy as np

from .device import DeviceError, DeviceInfo, ModuleInfo
from .sdr_source import AD936xSource

log = logging.getLogger("survey.uhd")

# --- UHD C API constants (uhd/types/*.h; ASCII-valued enums, stable ABI) ----
TUNE_POLICY_AUTO = 65          # 'A'
STREAM_MODE_START_CONTINUOUS = 97   # 'a'
STREAM_MODE_STOP_CONTINUOUS = 111   # 'o'
STREAM_MODE_NUM_SAMPS_AND_DONE = 100  # 'd'
RX_METADATA_ERROR_NONE = 0
RX_METADATA_ERROR_TIMEOUT = 1


class _TuneRequest(ctypes.Structure):
    _fields_ = [("target_freq", ctypes.c_double),
                ("rf_freq_policy", ctypes.c_int),
                ("rf_freq", ctypes.c_double),
                ("dsp_freq_policy", ctypes.c_int),
                ("dsp_freq", ctypes.c_double),
                ("args", ctypes.c_char_p)]


class _TuneResult(ctypes.Structure):
    _fields_ = [("clipped_rf_freq", ctypes.c_double),
                ("target_rf_freq", ctypes.c_double),
                ("actual_rf_freq", ctypes.c_double),
                ("target_dsp_freq", ctypes.c_double),
                ("actual_dsp_freq", ctypes.c_double)]


class _StreamArgs(ctypes.Structure):
    _fields_ = [("cpu_format", ctypes.c_char_p),
                ("otw_format", ctypes.c_char_p),
                ("args", ctypes.c_char_p),
                ("channel_list", ctypes.POINTER(ctypes.c_size_t)),
                ("n_channels", ctypes.c_int)]


class _StreamCmd(ctypes.Structure):
    _fields_ = [("stream_mode", ctypes.c_int),
                ("num_samps", ctypes.c_size_t),
                ("stream_now", ctypes.c_bool),
                ("time_spec_full_secs", ctypes.c_int64),
                ("time_spec_frac_secs", ctypes.c_double)]


def _sha256(path: str) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _default_uhd_dirs() -> list:
    dirs = []
    env = os.environ.get("UHD_DIR")
    if env:
        dirs.append(os.path.join(env, "bin"))
        dirs.append(env)
    for pf in (os.environ.get("ProgramFiles", r"C:\Program Files"),
               os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
        dirs.append(os.path.join(pf, "UHD", "bin"))
    dirs.append(r"C:\Program Files\PothosSDR\bin")
    return dirs


class _UHDLib:
    """Loads uhd.dll and declares the C-API signatures we use."""

    def __init__(self, uhd_dir: str = ""):
        self.dll = self._load(uhd_dir)
        d = self.dll
        H = ctypes.c_void_p          # opaque handles
        e = ctypes.c_int             # uhd_error
        # usrp lifecycle / config
        d.uhd_usrp_make.argtypes = [ctypes.POINTER(H), ctypes.c_char_p]
        d.uhd_usrp_make.restype = e
        d.uhd_usrp_free.argtypes = [ctypes.POINTER(H)]
        d.uhd_usrp_free.restype = e
        d.uhd_usrp_last_error.argtypes = [H, ctypes.c_char_p, ctypes.c_size_t]
        d.uhd_usrp_last_error.restype = e
        d.uhd_usrp_get_mboard_name.argtypes = [H, ctypes.c_size_t,
                                               ctypes.c_char_p, ctypes.c_size_t]
        d.uhd_usrp_get_mboard_name.restype = e
        d.uhd_usrp_get_rx_num_channels.argtypes = [H, ctypes.POINTER(ctypes.c_size_t)]
        d.uhd_usrp_get_rx_num_channels.restype = e
        d.uhd_usrp_set_rx_rate.argtypes = [H, ctypes.c_double, ctypes.c_size_t]
        d.uhd_usrp_set_rx_rate.restype = e
        d.uhd_usrp_set_rx_bandwidth.argtypes = [H, ctypes.c_double, ctypes.c_size_t]
        d.uhd_usrp_set_rx_bandwidth.restype = e
        d.uhd_usrp_set_rx_gain.argtypes = [H, ctypes.c_double, ctypes.c_size_t,
                                           ctypes.c_char_p]
        d.uhd_usrp_set_rx_gain.restype = e
        d.uhd_usrp_set_rx_agc.argtypes = [H, ctypes.c_bool, ctypes.c_size_t]
        d.uhd_usrp_set_rx_agc.restype = e
        d.uhd_usrp_set_rx_antenna.argtypes = [H, ctypes.c_char_p, ctypes.c_size_t]
        d.uhd_usrp_set_rx_antenna.restype = e
        d.uhd_usrp_set_rx_freq.argtypes = [H, ctypes.POINTER(_TuneRequest),
                                           ctypes.c_size_t, ctypes.POINTER(_TuneResult)]
        d.uhd_usrp_set_rx_freq.restype = e
        d.uhd_usrp_get_rx_stream.argtypes = [H, ctypes.POINTER(_StreamArgs), H]
        d.uhd_usrp_get_rx_stream.restype = e
        # rx streamer
        d.uhd_rx_streamer_make.argtypes = [ctypes.POINTER(H)]
        d.uhd_rx_streamer_make.restype = e
        d.uhd_rx_streamer_free.argtypes = [ctypes.POINTER(H)]
        d.uhd_rx_streamer_free.restype = e
        d.uhd_rx_streamer_issue_stream_cmd.argtypes = [H, ctypes.POINTER(_StreamCmd)]
        d.uhd_rx_streamer_issue_stream_cmd.restype = e
        d.uhd_rx_streamer_recv.argtypes = [H, ctypes.POINTER(ctypes.c_void_p),
                                           ctypes.c_size_t, ctypes.POINTER(H),
                                           ctypes.c_double, ctypes.c_bool,
                                           ctypes.POINTER(ctypes.c_size_t)]
        d.uhd_rx_streamer_recv.restype = e
        # rx metadata
        d.uhd_rx_metadata_make.argtypes = [ctypes.POINTER(H)]
        d.uhd_rx_metadata_make.restype = e
        d.uhd_rx_metadata_free.argtypes = [ctypes.POINTER(H)]
        d.uhd_rx_metadata_free.restype = e
        d.uhd_rx_metadata_error_code.argtypes = [H, ctypes.POINTER(ctypes.c_int)]
        d.uhd_rx_metadata_error_code.restype = e
        # --- TX path (added for active stimulation; RX-only radios never touch it) ---
        d.uhd_usrp_set_tx_rate.argtypes = [H, ctypes.c_double, ctypes.c_size_t]
        d.uhd_usrp_set_tx_rate.restype = e
        d.uhd_usrp_set_tx_bandwidth.argtypes = [H, ctypes.c_double, ctypes.c_size_t]
        d.uhd_usrp_set_tx_bandwidth.restype = e
        d.uhd_usrp_set_tx_gain.argtypes = [H, ctypes.c_double, ctypes.c_size_t,
                                           ctypes.c_char_p]
        d.uhd_usrp_set_tx_gain.restype = e
        d.uhd_usrp_set_tx_antenna.argtypes = [H, ctypes.c_char_p, ctypes.c_size_t]
        d.uhd_usrp_set_tx_antenna.restype = e
        d.uhd_usrp_set_tx_freq.argtypes = [H, ctypes.POINTER(_TuneRequest),
                                           ctypes.c_size_t, ctypes.POINTER(_TuneResult)]
        d.uhd_usrp_set_tx_freq.restype = e
        d.uhd_usrp_get_tx_stream.argtypes = [H, ctypes.POINTER(_StreamArgs), H]
        d.uhd_usrp_get_tx_stream.restype = e
        d.uhd_tx_streamer_make.argtypes = [ctypes.POINTER(H)]
        d.uhd_tx_streamer_make.restype = e
        d.uhd_tx_streamer_free.argtypes = [ctypes.POINTER(H)]
        d.uhd_tx_streamer_free.restype = e
        d.uhd_tx_streamer_send.argtypes = [H, ctypes.POINTER(ctypes.c_void_p),
                                           ctypes.c_size_t, ctypes.POINTER(H),
                                           ctypes.c_double, ctypes.POINTER(ctypes.c_size_t)]
        d.uhd_tx_streamer_send.restype = e
        # uhd_tx_metadata_make(handle*, has_time_spec, full_secs, frac_secs, sob, eob)
        d.uhd_tx_metadata_make.argtypes = [ctypes.POINTER(H), ctypes.c_bool,
                                           ctypes.c_int64, ctypes.c_double,
                                           ctypes.c_bool, ctypes.c_bool]
        d.uhd_tx_metadata_make.restype = e
        d.uhd_tx_metadata_free.argtypes = [ctypes.POINTER(H)]
        d.uhd_tx_metadata_free.restype = e

    @staticmethod
    def _load(uhd_dir: str):
        candidates = ([os.path.join(uhd_dir, "bin"), uhd_dir] if uhd_dir else []) \
            + _default_uhd_dirs()
        tried = []
        for d in candidates:
            dll = os.path.join(d, "uhd.dll")
            if not os.path.isfile(dll):
                tried.append(d)
                continue
            try:
                os.add_dll_directory(d)
            except (OSError, AttributeError):
                pass
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
            return ctypes.CDLL(dll)
        # last resort: whatever the loader can find on PATH (also non-Windows)
        name = ctypes.util.find_library("uhd")
        if name:
            return ctypes.CDLL(name)
        raise DeviceError(
            "uhd.dll not found — install UHD (files.ettus.com, Win64 installer) "
            f"or set b220.uhd_dir in config.yaml. Looked in: {tried}")


class UHDRadio:
    """Thin RX-only wrapper over one USRP session: tune / gain / one-shot
    capture / continuous stream, on a selectable RX channel."""

    def __init__(self, lib: _UHDLib, device_args: str = "type=b200"):
        self.lib = lib
        self.h = ctypes.c_void_p()
        err = lib.dll.uhd_usrp_make(ctypes.byref(self.h),
                                    device_args.encode("utf-8"))
        if err != 0:
            raise DeviceError(
                f"uhd_usrp_make({device_args!r}) failed (uhd_error {err}). Is the "
                f"radio plugged into USB 3.0, the Ettus WinUSB driver installed, "
                f"and no other process using it? (First open flashes FX3 firmware "
                f"+ FPGA over USB and can take ~20 s.)")
        self._md = ctypes.c_void_p()
        self._check(lib.dll.uhd_rx_metadata_make(ctypes.byref(self._md)),
                    "rx_metadata_make")
        self._streamer = None
        self._streamer_chan = None
        self._streaming = False
        self._tx_streamer = None
        self._tx_thread = None
        self._tx_stop = None

    # -- error plumbing --
    def _check(self, err: int, what: str):
        if err == 0:
            return
        buf = ctypes.create_string_buffer(512)
        try:
            self.lib.dll.uhd_usrp_last_error(self.h, buf, 512)
        except Exception:  # noqa: BLE001
            pass
        detail = buf.value.decode("utf-8", "replace").strip()
        raise DeviceError(f"UHD {what} failed (uhd_error {err}): {detail}")

    # -- info --
    def mboard_name(self) -> str:
        buf = ctypes.create_string_buffer(128)
        self._check(self.lib.dll.uhd_usrp_get_mboard_name(self.h, 0, buf, 128),
                    "get_mboard_name")
        return buf.value.decode("utf-8", "replace")

    def num_rx_channels(self) -> int:
        n = ctypes.c_size_t()
        self._check(self.lib.dll.uhd_usrp_get_rx_num_channels(
            self.h, ctypes.byref(n)), "get_rx_num_channels")
        return int(n.value)

    # -- config (RX only) --
    def set_rate(self, hz: float, chan: int):
        self._check(self.lib.dll.uhd_usrp_set_rx_rate(
            self.h, float(hz), chan), "set_rx_rate")

    def set_bandwidth(self, hz: float, chan: int):
        self._check(self.lib.dll.uhd_usrp_set_rx_bandwidth(
            self.h, float(hz), chan), "set_rx_bandwidth")

    def set_gain(self, db: float, chan: int):
        self._check(self.lib.dll.uhd_usrp_set_rx_gain(
            self.h, float(db), chan, b""), "set_rx_gain")

    def set_agc(self, enable: bool, chan: int):
        self._check(self.lib.dll.uhd_usrp_set_rx_agc(
            self.h, bool(enable), chan), "set_rx_agc")

    def set_antenna(self, name: str, chan: int):
        self._check(self.lib.dll.uhd_usrp_set_rx_antenna(
            self.h, name.encode("utf-8"), chan), "set_rx_antenna")

    def set_freq(self, hz: float, chan: int) -> float:
        req = _TuneRequest(target_freq=float(hz),
                           rf_freq_policy=TUNE_POLICY_AUTO, rf_freq=0.0,
                           dsp_freq_policy=TUNE_POLICY_AUTO, dsp_freq=0.0,
                           args=None)
        res = _TuneResult()
        self._check(self.lib.dll.uhd_usrp_set_rx_freq(
            self.h, ctypes.byref(req), chan, ctypes.byref(res)), "set_rx_freq")
        return float(res.actual_rf_freq + res.actual_dsp_freq)

    # -- streaming --
    def _ensure_streamer(self, chan: int):
        if self._streamer is not None and self._streamer_chan == chan:
            return
        self.destroy_streamer()
        st = ctypes.c_void_p()
        self._check(self.lib.dll.uhd_rx_streamer_make(ctypes.byref(st)),
                    "rx_streamer_make")
        chan_list = (ctypes.c_size_t * 1)(chan)
        args = _StreamArgs(cpu_format=b"fc32", otw_format=b"sc16", args=b"",
                           channel_list=chan_list, n_channels=1)
        err = self.lib.dll.uhd_usrp_get_rx_stream(self.h, ctypes.byref(args), st)
        if err != 0:
            self.lib.dll.uhd_rx_streamer_free(ctypes.byref(st))
            self._check(err, "get_rx_stream")
        self._streamer = st
        self._streamer_chan = chan

    def destroy_streamer(self):
        if self._streaming:
            try:
                self.stop_continuous()
            except Exception:  # noqa: BLE001
                pass
        if self._streamer is not None:
            self.lib.dll.uhd_rx_streamer_free(ctypes.byref(self._streamer))
            self._streamer = None
            self._streamer_chan = None

    def _recv_into(self, out: np.ndarray, timeout_s: float) -> int:
        """Fill `out` (complex64) from the streamer; returns samples received."""
        got = 0
        n = out.size
        while got < n:
            ptr = out[got:].ctypes.data_as(ctypes.c_void_p)
            buffs = (ctypes.c_void_p * 1)(ptr)
            rx = ctypes.c_size_t()
            self._check(self.lib.dll.uhd_rx_streamer_recv(
                self._streamer, buffs, n - got, ctypes.byref(self._md),
                ctypes.c_double(timeout_s), False, ctypes.byref(rx)),
                "rx_streamer_recv")
            code = ctypes.c_int()
            self.lib.dll.uhd_rx_metadata_error_code(self._md, ctypes.byref(code))
            if code.value == RX_METADATA_ERROR_TIMEOUT:
                break
            if code.value not in (RX_METADATA_ERROR_NONE,):
                # overflow etc. — tolerate, keep what we have (sweep averages it out)
                log.debug("UHD rx metadata error code %d", code.value)
            got += int(rx.value)
            if rx.value == 0:
                break
        return got

    def capture(self, nsamps: int, chan: int, timeout_s: float = 5.0) -> np.ndarray:
        """One-shot capture of `nsamps` complex64 samples (NUM_SAMPS_AND_DONE)."""
        self._ensure_streamer(chan)
        cmd = _StreamCmd(stream_mode=STREAM_MODE_NUM_SAMPS_AND_DONE,
                         num_samps=int(nsamps), stream_now=True,
                         time_spec_full_secs=0, time_spec_frac_secs=0.0)
        self._check(self.lib.dll.uhd_rx_streamer_issue_stream_cmd(
            self._streamer, ctypes.byref(cmd)), "issue_stream_cmd")
        out = np.empty(int(nsamps), dtype=np.complex64)
        got = self._recv_into(out, timeout_s)
        if got == 0:
            raise DeviceError("UHD capture returned no samples (timeout)")
        return out[:got]

    def start_continuous(self, chan: int):
        self._ensure_streamer(chan)
        cmd = _StreamCmd(stream_mode=STREAM_MODE_START_CONTINUOUS,
                         num_samps=0, stream_now=True,
                         time_spec_full_secs=0, time_spec_frac_secs=0.0)
        self._check(self.lib.dll.uhd_rx_streamer_issue_stream_cmd(
            self._streamer, ctypes.byref(cmd)), "issue_stream_cmd(start)")
        self._streaming = True

    def read_continuous(self, n_block: int, timeout_s: float = 2.0) -> np.ndarray:
        out = np.empty(int(n_block), dtype=np.complex64)
        got = self._recv_into(out, timeout_s)
        return out[:got]

    def stop_continuous(self):
        if self._streamer is None:
            return
        cmd = _StreamCmd(stream_mode=STREAM_MODE_STOP_CONTINUOUS,
                         num_samps=0, stream_now=True,
                         time_spec_full_secs=0, time_spec_frac_secs=0.0)
        self.lib.dll.uhd_rx_streamer_issue_stream_cmd(self._streamer,
                                                      ctypes.byref(cmd))
        self._streaming = False
        # drain whatever is in flight so the next one-shot starts clean
        junk = np.empty(8192, dtype=np.complex64)
        for _ in range(8):
            if self._recv_quiet(junk) == 0:
                break

    def _recv_quiet(self, out: np.ndarray) -> int:
        try:
            ptr = out.ctypes.data_as(ctypes.c_void_p)
            buffs = (ctypes.c_void_p * 1)(ptr)
            rx = ctypes.c_size_t()
            self.lib.dll.uhd_rx_streamer_recv(
                self._streamer, buffs, out.size, ctypes.byref(self._md),
                ctypes.c_double(0.1), False, ctypes.byref(rx))
            return int(rx.value)
        except Exception:  # noqa: BLE001
            return 0

    # -- transmit (active stimulation only) --
    #
    # HARDWARE-UNTESTED: written to the UHD C API but not yet bench-verified on a
    # real B220 (no radio available at build time), same status as the NLJD B210 TX
    # path. Streams a caller-supplied complex64 waveform on a background thread until
    # stop_tx(). TX-A (TX/RX port) and RX-A (RX2 port) are separate antennas, so this
    # can run WHILE capture()/read_continuous() runs on the RX side — the whole point
    # of a 2T2R radio for simultaneous stimulate-and-listen.

    def _tx_setup(self, center_hz: float, rate_hz: float, gain_db: float,
                  chan: int, antenna: str):
        self._check(self.lib.dll.uhd_usrp_set_tx_rate(self.h, float(rate_hz), chan),
                    "set_tx_rate")
        try:
            self._check(self.lib.dll.uhd_usrp_set_tx_bandwidth(
                self.h, float(rate_hz), chan), "set_tx_bandwidth")
        except DeviceError:
            pass                              # some frontends refuse an explicit BW
        try:
            self._check(self.lib.dll.uhd_usrp_set_tx_antenna(
                self.h, antenna.encode("utf-8"), chan), "set_tx_antenna")
        except DeviceError:
            pass
        req = _TuneRequest(target_freq=float(center_hz),
                           rf_freq_policy=TUNE_POLICY_AUTO, rf_freq=0.0,
                           dsp_freq_policy=TUNE_POLICY_AUTO, dsp_freq=0.0, args=None)
        res = _TuneResult()
        self._check(self.lib.dll.uhd_usrp_set_tx_freq(
            self.h, ctypes.byref(req), chan, ctypes.byref(res)), "set_tx_freq")
        self._check(self.lib.dll.uhd_usrp_set_tx_gain(
            self.h, float(gain_db), chan, b""), "set_tx_gain")

    def start_tx(self, waveform: np.ndarray, center_hz: float, rate_hz: float,
                 gain_db: float, chan: int = 0, antenna: str = "TX/RX"):
        """Loop `waveform` (complex64, |amp|<1) out of TX until stop_tx()."""
        import threading
        if self._tx_thread is not None:
            self.stop_tx()
        self._tx_setup(center_hz, rate_hz, gain_db, chan, antenna)
        st = ctypes.c_void_p()
        self._check(self.lib.dll.uhd_tx_streamer_make(ctypes.byref(st)),
                    "tx_streamer_make")
        chan_list = (ctypes.c_size_t * 1)(chan)
        args = _StreamArgs(cpu_format=b"fc32", otw_format=b"sc16", args=b"",
                           channel_list=chan_list, n_channels=1)
        err = self.lib.dll.uhd_usrp_get_tx_stream(self.h, ctypes.byref(args), st)
        if err != 0:
            self.lib.dll.uhd_tx_streamer_free(ctypes.byref(st))
            self._check(err, "get_tx_stream")
        self._tx_streamer = st
        buf = np.ascontiguousarray(waveform.astype(np.complex64))
        stop = threading.Event()
        self._tx_stop = stop

        def _pump():
            md = ctypes.c_void_p()
            self.lib.dll.uhd_tx_metadata_make(ctypes.byref(md), False, 0, 0.0,
                                              True, False)   # start_of_burst
            sent = ctypes.c_size_t()
            n = buf.size
            while not stop.is_set():
                ptr = buf.ctypes.data_as(ctypes.c_void_p)
                buffs = (ctypes.c_void_p * 1)(ptr)
                try:
                    self.lib.dll.uhd_tx_streamer_send(
                        st, buffs, n, ctypes.byref(md), ctypes.c_double(1.0),
                        ctypes.byref(sent))
                except Exception:  # noqa: BLE001
                    break
            # end-of-burst so the DAC stops cleanly
            eob = ctypes.c_void_p()
            self.lib.dll.uhd_tx_metadata_make(ctypes.byref(eob), False, 0, 0.0,
                                              False, True)
            z = np.zeros(1, dtype=np.complex64)
            zbuf = (ctypes.c_void_p * 1)(z.ctypes.data_as(ctypes.c_void_p))
            try:
                self.lib.dll.uhd_tx_streamer_send(st, zbuf, 1, ctypes.byref(eob),
                                                  ctypes.c_double(0.5), ctypes.byref(sent))
            except Exception:  # noqa: BLE001
                pass
            self.lib.dll.uhd_tx_metadata_free(ctypes.byref(md))
            self.lib.dll.uhd_tx_metadata_free(ctypes.byref(eob))

        self._tx_thread = threading.Thread(target=_pump, daemon=True)
        self._tx_thread.start()

    def stop_tx(self):
        if self._tx_stop is not None:
            self._tx_stop.set()
        if self._tx_thread is not None:
            self._tx_thread.join(timeout=1.5)
        if self._tx_streamer is not None:
            self.lib.dll.uhd_tx_streamer_free(ctypes.byref(self._tx_streamer))
            self._tx_streamer = None
        self._tx_thread = None
        self._tx_stop = None

    def close(self):
        try:
            self.stop_tx()
        except Exception:  # noqa: BLE001
            pass
        self.destroy_streamer()
        if self._md:
            self.lib.dll.uhd_rx_metadata_free(ctypes.byref(self._md))
            self._md = ctypes.c_void_p()
        if self.h:
            self.lib.dll.uhd_usrp_free(ctypes.byref(self.h))
            self.h = ctypes.c_void_p()


# ---------------------------------------------------------------------------
# The BaseDevice: same stepped-sweep engine as the Pluto, UHD-bound, 2 RX.
# ---------------------------------------------------------------------------

class B220Source(AD936xSource):
    """SUOGOEST B220 Mini (AD9361 2R2T) via UHD. channels=2: the app's
    right module = RX channel 0, left module = RX channel 1."""

    channels = 2
    CFG_SECTION = "b220"
    # UHD cpu_format fc32 is already normalised to ~[-1, 1]
    ADC_FULL_SCALE = 1.0
    SIDE_TO_CHAN = {"right": 0, "left": 1, "rx": 0}

    def __init__(self, cfg):
        super().__init__(cfg)
        self.info.driver = "b220"
        self._device_args = str(cfg.get("b220", "device_args",
                                        default="type=b200"))
        self._antenna = str(cfg.get("b220", "antenna", default="RX2"))
        self._uhd_dir = str(cfg.get("b220", "uhd_dir", default=""))
        self._chan = 0
        self._nsamp_active = self._nsamp

    # -- radio binding hooks (see AD936xSource) --

    def _open_radio(self):
        # UHD needs its FPGA/firmware images to (re)initialise the FX3; they were
        # downloaded to a user-writable dir (no admin) — point UHD there if the
        # process didn't inherit the env var (e.g. a GUI/shortcut launch).
        if os.name == "nt" and not os.environ.get("UHD_IMAGES_DIR"):
            cand = os.path.join(os.environ.get("LOCALAPPDATA", ""), "uhd", "images")
            if os.path.isdir(cand):
                os.environ["UHD_IMAGES_DIR"] = cand
        self._ensure_uhd_images()
        lib = _UHDLib(self._uhd_dir)
        try:
            self.sdr = UHDRadio(lib, self._device_args)
        except DeviceError:
            raise
        except Exception as e:  # noqa: BLE001
            raise DeviceError(f"could not open UHD radio: {e}") from e
        self.info.port = self._device_args
        try:
            self.info.model = f"{self.sdr.mboard_name()} (B220 Mini, 2RX)"
        except Exception:  # noqa: BLE001
            self.info.model = "USRP B2xx (B220 Mini)"
        self._apply_channel_count(self.sdr.num_rx_channels())
        for ch in range(self.channels):
            try:
                self.sdr.set_antenna(self._antenna, ch)
            except DeviceError as e:
                self.info.warnings.append(f"antenna {self._antenna} ch{ch}: {e}")

    def _apply_channel_count(self, n: int):
        """Downgrade gracefully (with a visible warning) if the radio reports
        fewer RX channels than the B220's expected 2."""
        if n < 2:
            self.info.warnings.append(
                f"radio reports {n} RX channel(s), expected 2 — left module "
                f"disabled")
            type(self).channels = n or 1

    def _ensure_uhd_images(self):
        """The B220 Mini is a LibreSDR clone on a Xilinx Artix-7, NOT the genuine
        B210's Spartan-6. UHD's stock `usrp_b210_fpga.bin` is a Spartan-6 bitstream
        that can never configure this board — it fails with `fx3 is in state 5`
        (FPGA DONE never asserts). We ship the correct Artix-7 bitstream in
        native/uhd/ and install it into UHD's images dir (backing up any Ettus
        original as *.ettus-orig, never deleting) so a bare `--args type=b200`
        loads it. Idempotent: only copies when the target differs by hash. If the
        operator has already pointed b220.fpga at a specific image, we do nothing.

        Config knobs (both optional):
          b220.fpga        — explicit FPGA .bin path; skips this auto-install
          b220.fpga_bundled— override the bundled image name in native/uhd/
        """
        if str(self.cfg.get("b220", "fpga", default="")).strip():
            return  # operator pinned an explicit image; respect it
        here = os.path.dirname(os.path.abspath(__file__))
        bundled_dir = os.path.normpath(os.path.join(here, "..", "..", "native", "uhd"))
        bundled = os.path.join(
            bundled_dir,
            str(self.cfg.get("b220", "fpga_bundled", default="usrp_b210_fpga.bin")))
        if not os.path.isfile(bundled):
            return  # nothing bundled — fall back to whatever UHD already has
        images_dir = (os.environ.get("UHD_IMAGES_DIR")
                      or self._uhd_dir
                      or os.path.join(os.environ.get("LOCALAPPDATA", ""), "uhd", "images"))
        if not images_dir or not os.path.isdir(images_dir):
            return
        target = os.path.join(images_dir, "usrp_b210_fpga.bin")
        try:
            if os.path.isfile(target) and _sha256(target) == _sha256(bundled):
                return  # already the right image
            if os.path.isfile(target):
                backup = target + ".ettus-orig"
                if not os.path.exists(backup):   # keep the FIRST original only
                    os.replace(target, backup)   # rename, never delete
            import shutil
            shutil.copyfile(bundled, target)
            log.info("installed LibreSDR FPGA image into %s", images_dir)
            self.info.warnings.append(
                "installed the LibreSDR (Artix-7) FPGA image — the stock Ettus "
                "B210 bitstream cannot configure this clone")
        except OSError as e:
            self.info.warnings.append(
                f"could not install the LibreSDR FPGA image ({e}); if connect "
                f"fails with 'fx3 is in state 5', copy native/uhd/usrp_b210_fpga.bin "
                f"into {images_dir} by hand")

    def connect(self) -> DeviceInfo:
        info = super().connect()
        # base class reported one "right" module; a 2RX radio has both sides
        if self.channels >= 2 and info.modules:
            m = info.modules[0]
            info.modules = [
                ModuleInfo("right", f"{self.info.model} RX-A", m.min_mhz, m.max_mhz),
                ModuleInfo("left", f"{self.info.model} RX-B", m.min_mhz, m.max_mhz),
            ]
        return info

    def select_module(self, side: str):
        """Map the app's dual-module model onto the two physical RX channels."""
        ch = self.SIDE_TO_CHAN.get((side or "").strip().lower(), 0)
        if ch >= self.channels:
            ch = 0
        if ch == self._chan:
            return
        self._chan = ch
        # re-assert per-channel config; the streamer is rebuilt lazily
        self.sdr.set_rate(self._fs, ch)
        self.sdr.set_bandwidth(self._fs, ch)
        self._set_gain(self._gain_mode, self._gain_db)

    def _set_lo(self, hz: float):
        self.sdr.set_freq(float(hz), self._chan)

    def _set_sample_rate(self, hz: float):
        self.sdr.set_rate(float(hz), self._chan)

    def _set_rf_bandwidth(self, hz: float):
        self.sdr.set_bandwidth(float(hz), self._chan)

    def _set_buffer_size(self, n: int):
        self._nsamp_active = int(n)

    def _set_gain(self, mode: str, db: float):
        """Manual fixed gain for repeatable baselines; AGC for first-look.
        A bad gain number must never take the radio down (same policy as the
        Pluto): back off, then fall to AGC."""
        if mode != "manual":
            try:
                self.sdr.set_agc(True, self._chan)
                return
            except DeviceError:
                pass  # fall through to manual
        for g in (float(db), 60.0, 50.0, 40.0):
            try:
                self.sdr.set_agc(False, self._chan)
                self.sdr.set_gain(g, self._chan)
                return
            except DeviceError:
                continue
        try:
            self.sdr.set_agc(True, self._chan)
        except DeviceError:
            pass

    def _flush_buffer(self):
        # one-shot NUM_SAMPS_AND_DONE captures start fresh after each command;
        # analog settling is covered by settle_seconds. Nothing to flush.
        return

    def _read_iq(self) -> np.ndarray:
        return self.sdr.capture(self._nsamp_active, self._chan)

    def stream_iq(self, center_hz: float, sample_rate: float, n_block: int, stop):
        """Continuous IQ for live audio (UHD continuous mode — the Pluto/iio
        kernel-buffer dance in the base class does not apply)."""
        self._set_sample_rate(sample_rate)
        self._set_rf_bandwidth(sample_rate)
        self._set_lo(center_hz)
        if self._settle_s > 0:
            time.sleep(self._settle_s)
        self.sdr.start_continuous(self._chan)
        try:
            while not stop.is_set():
                blk = self.sdr.read_continuous(int(n_block))
                if blk.size == 0:
                    continue
                yield blk
        finally:
            try:
                self.sdr.stop_continuous()
            except Exception:  # noqa: BLE001
                pass
            self._set_sample_rate(self._fs)
            self._set_rf_bandwidth(self._fs)
            self._set_buffer_size(self._nsamp)

    def capture_iq(self, center_hz: float, sample_rate: float,
                   n_samples: int) -> np.ndarray:
        iq = super().capture_iq(center_hz, sample_rate, n_samples)
        return iq  # fc32 already ±1 (ADC_FULL_SCALE = 1.0)

    # ================= active-stimulation protocol (survey.stimulate) ===========
    # Consumed by StimulationEngine. The B220 is the CONCURRENT radio: TX-A (TX/RX
    # port) illuminates while RX-A (RX2 port) listens, on independent chains.
    # HARDWARE-UNTESTED end-to-end — bench-validate per STIMULATE_HARDWARE_TODO.md
    # before exposing a transmit control.

    def stim_mode(self) -> str:
        return "concurrent"

    def stim_permitted_bands(self):
        from ..stimulate import DEFAULT_PERMITTED_BANDS_MHZ
        bands = self.cfg.get("stimulate", "permitted_bands_mhz",
                             default=list(DEFAULT_PERMITTED_BANDS_MHZ))
        return [tuple(b) for b in bands]

    def _stim_rx_channel(self) -> int:
        """Which RX channel listens during stimulation. Default 0 (RX2-A). Set
        stimulate.rx_channel = 1 to listen on RX2-B (the OTHER channel) for more
        isolation from TX/RX-A — the four ports are TX/RX-A, RX2-A, TX/RX-B, RX2-B."""
        ch = int(self.cfg.get("stimulate", "rx_channel", default=0))
        return ch if ch < self.channels else 0

    def stim_capture_quiet(self, center_hz: float, fs: float, n: int) -> np.ndarray:
        """RX capture with TX guaranteed OFF (the quiet reference)."""
        if self.sdr is not None:
            self.sdr.stop_tx()
        self._chan = self._stim_rx_channel()
        return self.capture_iq(center_hz, fs, n)

    def start_tx(self, waveform: np.ndarray, center_hz: float, rate_hz: float,
                 gain_db: float):
        """Generic transmitter entry point (so a B220 can be the TX side of a split
        Pluto/B220 rig). Emits on the configured TX channel/port."""
        tx_chan = int(self.cfg.get("stimulate", "tx_channel", default=0))
        self.sdr.start_tx(waveform, center_hz, rate_hz, gain_db, chan=tx_chan,
                          antenna=str(self.cfg.get("stimulate", "tx_antenna", default="TX/RX")))

    def stop_tx(self):
        if self.sdr is not None:
            self.sdr.stop_tx()

    def stim_tx_on(self, band_lo_hz: float, band_hi_hz: float):
        """Start the swept stimulus across [lo,hi] on the TX port for the whole burst."""
        from ..stimulate import make_chirp
        tx_rate = float(self.cfg.get("stimulate", "tx_sample_rate", default=self._fs))
        gain = float(self.cfg.get("stimulate", "tx_gain_db", default=50.0))
        span = float(band_hi_hz - band_lo_hz)
        center = (band_lo_hz + band_hi_hz) / 2.0
        n_wave = int(self.cfg.get("stimulate", "tx_wave_samples", default=1 << 16))
        wave = make_chirp(tx_rate, span, n_wave,
                          amp=float(self.cfg.get("stimulate", "tx_amp", default=0.5)))
        self.start_tx(wave, center, tx_rate, gain)

    def stim_tx_off(self):
        if self.sdr is not None:
            self.sdr.stop_tx()

    def stim_capture_stimulated(self, center_hz: float, band_lo_hz: float,
                                band_hi_hz: float, fs: float, n: int) -> np.ndarray:
        """RX capture while the stimulus is transmitting (stim_tx_on was called by
        the engine for the whole averaging burst). A short settle lets the TX chain
        come up before the first capture."""
        settle = float(self.cfg.get("stimulate", "tx_settle_s", default=0.05))
        if settle > 0:
            time.sleep(settle)
        self._chan = self._stim_rx_channel()
        return self.capture_iq(center_hz, fs, n)

    def is_alive(self) -> bool:
        return self.sdr is not None and bool(self.sdr.h)

    def close(self):
        if self.sdr is not None:
            log.info("B220Source.close()")
            try:
                self.sdr.close()
            except Exception:  # noqa: BLE001
                pass
            self.sdr = None
