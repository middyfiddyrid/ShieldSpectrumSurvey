"""Subprocess isolation for the native SDR drivers (the 2026-07-12 "STILL THE
REAL FIX").

The UHD (ctypes over uhd.dll) and libiio (pyadi-iio) drivers run native C++ in
the calling process: a USB fault can fastfail (0xc0000409) the whole Flask app
— nothing Python can catch. This module moves the driver into a supervised
WORKER SUBPROCESS behind `SubprocessDevice`, a BaseDevice-shaped proxy:

    runner / server / stimulate / emanations   (untouched — they see BaseDevice)
            │  method calls
    SubprocessDevice        (this file, parent side)
            │  control: multiprocessing.Pipe (pickled request/reply)
            │  bulk IQ: multiprocessing.shared_memory (complex64 slots)
            ▼
    worker_main             (this file, child side; spawned per device handle)
            └── real PlutoSource / B220Source — if IT dies, only the worker dies

Design doctrine (from the 2026-07-12 boundary survey — do not "improve" these
without re-reading the callers):

* A dead/crashed worker surfaces as DeviceError and is_alive() -> False.
  ONLY (DeviceError, OSError) ride the runner's segment-retry + _reconnect
  rails (runner.py MAX_SEGMENT_RETRIES loop); anything else hard-aborts a
  sweep. The proxy NEVER transparently respawns its worker: the runner's
  existing reconnect path replays module/RBW and the retried segment replays
  range/calculator — an in-proxy respawn would resume with default tuning and
  silently corrupt segment data.
* hasattr() is capability ROUTING here, not just guarding: capture_iq_dual
  presence picks the DF radio, stim_mode/start_tx gate transmit paths,
  stream_iq gates listen/record, `environment` (simulator-only) is written
  when present. The proxy therefore mirrors the CONCRETE worker class's
  surface exactly — conditional names live in an instance registry served by
  __getattr__, never on the class.
* dev.info is a live object mutated device-side (select_module updates module
  ranges; read_sweep updates last_sweep_seconds; warnings grow). Every RPC
  reply piggybacks the worker's current DeviceInfo; `.info` is served from
  that parent-side copy so ~1 Hz /api/state polls never touch the pipe.
* predicted_rbw_khz/grid_points are called from Flask request threads with NO
  lock while the sweep worker owns the pipe — they are answered LOCALLY from
  an unconnected "mirror" driver instance (pure math, no hardware), kept in
  sync by forwarding set_rbw_khz to both.
* read_sweep legitimately runs tens of seconds (wide spans tune hundreds of
  centers) and a first B220 open can take ~45 s (FX3 + FPGA load) — so there
  are no tight per-call deadlines; the generous device.worker_call_timeout_s
  (default 300 s) exists to convert a WEDGED native driver (Pluto rx() has no
  timeout) into kill-worker + DeviceError instead of today's forever-hang.
* Worker death must never leave RF transmitting: the worker's pipe-EOF path
  (parent gone) closes the device, which stops host-pumped TX. (A Pluto
  cyclic-buffer TX survives ANY host death — in-process or worker — that
  exposure is unchanged and documented in HANDOFF.md.)
"""

from __future__ import annotations

import faulthandler
import logging
import multiprocessing
import multiprocessing.connection as mp_conn
import os
import re
import threading
import time
from functools import partial
from multiprocessing import shared_memory

import numpy as np

from .device import BaseDevice, DeviceError, DeviceInfo

log = logging.getLogger("survey.worker_proxy")

# ring slots for stream_iq; small — the producer is real-time paced by the
# radio, the consumer (demod) is ms-fast; 4 absorbs GC/scheduling burps.
STREAM_SLOTS = 4

# names that may exist on a concrete driver but NOT on BaseDevice — forwarded
# generically over RPC when (and only when) the worker device really has them.
# capture_iq_dual / stream_iq get dedicated shared-memory paths below.
_GENERIC_FORWARD = (
    "set_gain", "start_tx", "stop_tx",
    "stim_mode", "stim_permitted_bands", "stim_capture_quiet",
    "stim_capture_stimulated", "stim_tx_on", "stim_tx_off",
)
# attribute VALUES mirrored from the worker at open time, present on the proxy
# only when present on the concrete device (hasattr fidelity).
_MIRROR_ATTRS = ("is_swept_sdr", "CFG_SECTION", "ADC_FULL_SCALE", "_fs")


def _safe_name(source: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", source)[:60]


# ---------------------------------------------------------------------------
# worker (child process) side
# ---------------------------------------------------------------------------

_CRASH_FH = None    # keeps the faulthandler target open for the process life


def _rebuild_config(path: str, data: dict, overrides: list):
    """Reconstruct the parent's Config in the child WITHOUT reading disk: the
    snapshot is authoritative for this handle's lifetime (a reconnect makes a
    fresh proxy and a fresh snapshot). Runtime overrides (--simulate/--demo)
    are session-only and never on disk, so they must travel explicitly."""
    from ..config import Config
    cfg = Config.__new__(Config)            # skip __init__: no disk read
    cfg.path = path or os.path.abspath("config.yaml")
    cfg.data = data
    cfg._runtime_overrides = {tuple(k): v for k, v in (overrides or [])}
    return cfg


def _open_real(cfg, source: str):
    """Construct + connect the REAL driver inside the worker. Mirrors
    open_device's SDR branches; RFE (pyserial — python-catchable faults) and
    the default simulator path are never routed here."""
    src = (source or "").strip().lower()
    if src in ("pluto", "sdr", "ad936x"):
        from .sdr_source import PlutoSource
        dev = PlutoSource(cfg)
    elif src in ("b220", "usrp", "uhd"):
        from .uhd_source import B220Source
        dev = B220Source(cfg)
    elif src == "simulate":
        # preview/e2e: run the simulator behind the worker so the whole IPC +
        # crash-recovery path can be exercised without hardware.
        from .simulator import SimulatedDevice
        dev = SimulatedDevice()
    elif src.startswith("test:") and os.environ.get("SWEEP_ALLOW_TEST_DEVICE") == "1":
        # "test:<abs path to .py>:<ClassName>" — unit-test fakes run in-worker
        # (crash injection etc). Gated by env so a stray source string can
        # never load arbitrary code in production.
        import importlib.util
        _, file_path, cls_name = src_split3(source)
        spec = importlib.util.spec_from_file_location("survey_workerfakes", file_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        # case-insensitive class lookup: connect_device lowercases source
        # strings before they reach open_device
        cls = getattr(mod, cls_name, None)
        if cls is None:
            cls = next((v for k, v in vars(mod).items()
                        if k.lower() == cls_name.lower()), None)
        if cls is None:
            raise DeviceError(f"test device class {cls_name!r} not found")
        dev = cls(cfg)
    else:
        raise DeviceError(f"worker cannot open source {source!r}")
    dev.connect()
    return dev


def src_split3(source: str):
    """'test:C:\\path\\fakes.py:Cls' -> ('test', 'C:\\path\\fakes.py', 'Cls')
    (rsplit: the path itself contains a drive colon on Windows)."""
    head, _, cls_name = source.rpartition(":")
    tag, _, file_path = head.partition(":")
    return tag, file_path, cls_name


def _caps_for(dev) -> dict:
    """The concrete device's capability surface, mirrored onto the proxy so
    hasattr-routing (DF radio pick, stim gating, listen gating, simulator
    environment writes) behaves exactly as in-process."""
    methods = [m for m in _GENERIC_FORWARD if callable(getattr(dev, m, None))]
    has = {}
    for a in _MIRROR_ATTRS:
        if hasattr(dev, a):
            has[a] = getattr(dev, a)
    caps = {
        "methods": methods,
        "attrs": has,
        "channels": int(getattr(dev, "channels", 1)),   # post-connect effective
        # base-defined names: report whether the CONCRETE class overrides them
        "real_capture_iq": type(dev).capture_iq is not BaseDevice.capture_iq,
        "real_predicted_rbw": (type(dev).predicted_rbw_khz
                               is not BaseDevice.predicted_rbw_khz),
        "has_stream_iq": callable(getattr(dev, "stream_iq", None)),
        "has_capture_iq_dual": callable(getattr(dev, "capture_iq_dual", None)),
        "has_environment": hasattr(dev, "environment"),
        "environment": getattr(dev, "environment", None),
        "cls_name": type(dev).__name__,
    }
    return caps


def _err_reply(exc: BaseException) -> dict:
    return {"err": f"{exc}", "cls": type(exc).__name__}


def _do_stream(conn, dev, msg):
    """Stream mode: run the device's own stream_iq generator, publish blocks
    into the parent-created shared-memory ring, flow-controlled by parent
    acks. The parent's stop is a message; the local Event we hand the driver
    preserves its tune-once/restore-in-finally semantics."""
    center_hz, fs, n_block = msg["args"]
    slots = int(msg["slots"])
    slot_bytes = int(n_block) * 8                       # complex64
    stop_local = threading.Event()
    seq, acked, err = 0, -1, None
    shm = shared_memory.SharedMemory(name=msg["shm"])
    conn.send({"stream": "started"})

    def _handle(m):
        nonlocal acked
        if not isinstance(m, dict):
            return
        if m.get("op") == "ack":
            acked = max(acked, int(m.get("seq", -1)))
        elif m.get("op") == "stream_stop":
            stop_local.set()

    try:
        for iq in dev.stream_iq(center_hz, fs, n_block, stop_local):
            # credit gate: never overwrite a slot the parent hasn't drained
            while (seq - acked) > (slots - 1) and not stop_local.is_set():
                _handle(conn.recv())
            if stop_local.is_set():
                break
            n = min(len(iq), int(n_block))
            dst = np.ndarray((n,), dtype=np.complex64, buffer=shm.buf,
                             offset=(seq % slots) * slot_bytes)
            dst[:] = iq[:n]
            del dst                 # export must not outlive shm.close()
            conn.send({"blk": seq, "n": n})
            seq += 1
            while conn.poll(0):                        # opportunistic drain
                _handle(conn.recv())
    except Exception as e:  # noqa: BLE001 — driver fault ends the stream
        err = f"{type(e).__name__}: {e}"
    finally:
        try:
            shm.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            conn.send({"stream": "end", "err": err, "info": dev.info})
        except Exception:  # noqa: BLE001
            pass


def worker_main(conn, cfg_path: str, cfg_data: dict, cfg_overrides: list,
                source: str, logs_dir: str):
    """Child entry point (multiprocessing spawn target). One device per
    worker; parent death (pipe EOF) closes the device so no RF path outlives
    supervision."""
    global _CRASH_FH
    try:
        os.makedirs(logs_dir, exist_ok=True)
        _CRASH_FH = open(os.path.join(
            logs_dir, f"crash-worker-{_safe_name(source)}.log"),
            "a", buffering=1, encoding="utf-8", errors="replace")
        _CRASH_FH.write(f"--- worker start pid={os.getpid()} source={source} "
                        f"{time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
        faulthandler.enable(file=_CRASH_FH, all_threads=True)
    except Exception:  # noqa: BLE001 — crash logging must never block startup
        pass
    try:
        h = logging.FileHandler(os.path.join(
            logs_dir, f"worker-{_safe_name(source)}.log"), encoding="utf-8")
        h.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logging.getLogger().addHandler(h)
        logging.getLogger().setLevel(logging.INFO)
    except Exception:  # noqa: BLE001
        pass

    dev = None
    try:
        while True:
            try:
                msg = conn.recv()
            except (EOFError, OSError):
                log.warning("parent pipe closed — worker shutting down")
                break
            op = msg.get("op")
            try:
                if op == "open":
                    cfg = _rebuild_config(cfg_path, cfg_data, cfg_overrides)
                    dev = _open_real(cfg, source)
                    conn.send({"ok": True, "info": dev.info,
                               "caps": _caps_for(dev)})
                elif op == "call":
                    fn = getattr(dev, msg["name"])
                    result = fn(*msg.get("args", ()), **msg.get("kwargs", {}))
                    conn.send({"ok": result, "info": dev.info})
                elif op == "setattr":
                    setattr(dev, msg["name"], msg["value"])
                    conn.send({"ok": True, "info": dev.info})
                elif op == "capture":
                    fn = getattr(dev, msg["name"])
                    arr = fn(*msg.get("args", ()))
                    arr = np.ascontiguousarray(arr, dtype=np.complex64)
                    shm = shared_memory.SharedMemory(name=msg["shm"])
                    try:
                        flat = arr.reshape(-1)
                        dst = np.ndarray(flat.shape, dtype=np.complex64,
                                         buffer=shm.buf)
                        dst[:] = flat
                        del dst         # buffer export must not outlive close
                    finally:
                        try:
                            shm.close()
                        except Exception:  # noqa: BLE001
                            pass
                    conn.send({"ok": {"shape": list(arr.shape)},
                               "info": dev.info})
                elif op == "stream_start":
                    _do_stream(conn, dev, msg)
                elif op in ("ack", "stream_stop"):
                    # straggler from a stream that just ended (the parent's
                    # last ack / stop can race the worker's "end") — ignore,
                    # or it would poison the next request with an error reply
                    pass
                elif op == "close":
                    if dev is not None:
                        try:
                            dev.close()
                        except Exception:  # noqa: BLE001
                            pass
                        dev = None
                    conn.send({"ok": True})
                    break
                else:
                    conn.send({"err": f"unknown op {op!r}", "cls": "DeviceError"})
            except Exception as e:  # noqa: BLE001 — python-level driver errors
                # survive in the worker; only NATIVE faults kill the process
                log.warning("worker op %s failed: %s: %s", op,
                            type(e).__name__, e)
                try:
                    conn.send(_err_reply(e))
                except Exception:  # noqa: BLE001
                    break
    finally:
        if dev is not None:
            try:
                dev.close()      # parent died / loop ended: stop RX and TX
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# parent (proxy) side
# ---------------------------------------------------------------------------

class SubprocessDevice(BaseDevice):
    """BaseDevice-shaped proxy whose real driver lives in a worker subprocess.

    One stable instance per connected radio (the runner's devices dict holds
    it for the handle's lifetime). A native crash in the driver kills only
    the worker: the pending call raises DeviceError, is_alive() flips False,
    and the app's existing reconnect paths (_ensure_device / segment retry /
    monitor eviction) spawn a fresh proxy+worker via open_device."""

    def __init__(self, cfg, source: str):
        self.cfg = cfg
        self._source = source
        self._dead = False
        self._lock = threading.RLock()          # serialises the pipe
        self._info = DeviceInfo(driver="worker")
        self._remote_attrs = {}
        self._forward_methods = ()
        self._caps = {}
        self._mirror = self._make_mirror(cfg, source)
        t = cfg.get("device", "worker_call_timeout_s", default=300.0)
        self._call_timeout = float(t) if t else None
        ct = cfg.get("device", "worker_connect_timeout_s", default=300.0)
        connect_timeout = float(ct) if ct else None

        snapshot = _snapshot_cfg(cfg)
        logs_dir = os.path.join(getattr(cfg, "base_dir", "") or os.getcwd(),
                                "logs")
        ctx = multiprocessing.get_context("spawn")
        self._conn, child_conn = ctx.Pipe(duplex=True)
        self._proc = ctx.Process(
            target=worker_main,
            args=(child_conn, snapshot["path"], snapshot["data"],
                  snapshot["overrides"], source, logs_dir),
            daemon=True,
            name=f"radio-worker-{_safe_name(source)}")
        self._proc.start()
        child_conn.close()          # EOF semantics: only the child holds it
        log.info("radio worker for %s spawned (pid %s)", source,
                 self._proc.pid)
        try:
            reply = self._request({"op": "open"}, timeout=connect_timeout)
        except DeviceError:
            self._kill_quietly()
            raise
        self._caps = reply.get("caps", {})
        for name, value in self._caps.get("attrs", {}).items():
            object.__setattr__(self, name, value)
        object.__setattr__(self, "channels", self._caps.get("channels", 1))
        if self._caps.get("has_environment"):
            self._remote_attrs["environment"] = self._caps.get("environment")
        fwd = list(self._caps.get("methods", ()))
        self._forward_methods = tuple(fwd)

    # -- conditional capability surface (hasattr fidelity) --

    def __getattr__(self, name):
        # only called for names not found normally; must never RPC (hasattr
        # probes from status paths land here)
        d = object.__getattribute__(self, "__dict__")
        ra = d.get("_remote_attrs") or {}
        if name in ra:
            return ra[name]
        caps = d.get("_caps") or {}
        if name == "stream_iq" and caps.get("has_stream_iq"):
            return self._stream_iq_impl
        if name == "capture_iq_dual" and caps.get("has_capture_iq_dual"):
            return self._capture_iq_dual_impl
        if name == "grid_points" and d.get("_mirror") is not None:
            return d["_mirror"].grid_points
        if name in (d.get("_forward_methods") or ()):
            return partial(self._forward, name)
        raise AttributeError(
            f"{type(self).__name__}({d.get('_source')}) has no attribute {name!r}")

    def __setattr__(self, name, value):
        if (name == "environment"
                and "environment" in getattr(self, "_remote_attrs", {})):
            self._request({"op": "setattr", "name": "environment",
                           "value": value}, timeout=self._call_timeout)
            self._remote_attrs["environment"] = value
            return
        object.__setattr__(self, name, value)

    # -- BaseDevice contract --

    @property
    def info(self) -> DeviceInfo:
        return self._info

    def is_alive(self) -> bool:
        try:
            return (not self._dead) and self._proc.is_alive()
        except Exception:  # noqa: BLE001
            return False

    def connect(self) -> DeviceInfo:
        # construction already spawned + connected; keep the call idempotent
        # for open_device symmetry
        return self._info

    def select_module(self, side: str):
        return self._forward("select_module", side)

    def set_range(self, start_mhz: float, end_mhz: float):
        return self._forward("set_range", start_mhz, end_mhz)

    def set_calculator(self, mode_name: str):
        return self._forward("set_calculator", mode_name)

    def read_sweep(self, timeout_s: float = 5.0):
        # None-on-timeout is a soft signal some callers 'continue' on — it
        # crosses the pipe as a plain None result, never an exception.
        return self._forward("read_sweep", timeout_s)

    def set_rbw_khz(self, rbw_khz):
        r = self._forward("set_rbw_khz", rbw_khz)
        if self._mirror is not None:
            try:
                self._mirror.set_rbw_khz(rbw_khz)   # pure math — keeps local
            except Exception:  # noqa: BLE001      # predicted/grid in sync
                pass
        return r

    def predicted_rbw_khz(self, span_mhz: float, rbw_khz=None):
        # LOCAL: called from request threads with no lock while a sweep owns
        # the pipe (set_live_rbw, /api/plan) — must not queue behind a
        # multi-second read_sweep.
        if self._mirror is not None:
            return self._mirror.predicted_rbw_khz(span_mhz, rbw_khz)
        if self._caps.get("real_predicted_rbw"):
            return self._forward("predicted_rbw_khz", span_mhz, rbw_khz)
        return None

    def capture_iq(self, center_hz: float, sample_rate: float,
                   n_samples: int) -> np.ndarray:
        if not self._caps.get("real_capture_iq"):
            return super().capture_iq(center_hz, sample_rate, n_samples)
        return self._capture("capture_iq",
                             (center_hz, sample_rate, int(n_samples)),
                             int(n_samples))

    def close(self):
        """Idempotent, never raises; escalates send→join→terminate→kill so a
        wedged worker can't stall connect_device's handle swap."""
        with_lock = self._lock.acquire(timeout=2.0)
        try:
            if not self._dead and self._proc.is_alive():
                try:
                    if with_lock:                   # pipe free: ask nicely
                        self._conn.send({"op": "close"})
                        self._proc.join(timeout=5.0)
                except Exception:  # noqa: BLE001
                    pass
        finally:
            if with_lock:
                self._lock.release()
        self._kill_quietly()

    # -- shared-memory bulk paths --

    def _capture_iq_dual_impl(self, center_hz: float, sample_rate: float,
                              n_samples: int) -> np.ndarray:
        return self._capture("capture_iq_dual",
                             (center_hz, sample_rate, int(n_samples)),
                             2 * int(n_samples))

    def _capture(self, name: str, args: tuple, max_samples: int) -> np.ndarray:
        with self._lock:
            self._check_live()
            shm = shared_memory.SharedMemory(
                create=True, size=max(8, max_samples * 8))
            try:
                reply = self._request({"op": "capture", "name": name,
                                       "args": args, "shm": shm.name},
                                      timeout=self._call_timeout,
                                      already_locked=True)
                shape = tuple(reply["ok"]["shape"])
                n = int(np.prod(shape)) if shape else 0
                flat = np.ndarray((n,), dtype=np.complex64, buffer=shm.buf)
                out = flat.copy().reshape(shape)
                del flat            # release the buffer export before close()
                return out
            finally:
                try:
                    shm.close()
                except Exception:  # noqa: BLE001 — a live numpy view can hold
                    pass           # the export; GC finishes the release then
                try:
                    shm.unlink()        # no-op on Windows, tidy on POSIX
                except Exception:  # noqa: BLE001
                    pass

    def _stream_iq_impl(self, center_hz: float, sample_rate: float,
                        n_block: int, stop):
        """Generator with the exact stream_iq contract: tune once, yield
        complex64 blocks until the caller's threading.Event is set. The stop
        event is translated to an out-of-band message; blocks are COPIED out
        of the ring before yielding (the recorder keeps references)."""
        n_block = int(n_block)
        slot_bytes = n_block * 8
        block_timeout = max(30.0, 10.0 * (n_block / float(sample_rate)))
        with self._lock:                # device is single-user by contract
            self._check_live()
            shm = shared_memory.SharedMemory(
                create=True, size=STREAM_SLOTS * slot_bytes)
            ended = False
            try:
                self._send({"op": "stream_start",
                            "args": (center_hz, sample_rate, n_block),
                            "shm": shm.name, "slots": STREAM_SLOTS})
                first = self._wait_msg(self._call_timeout)
                if isinstance(first, dict) and first.get("err"):
                    ended = True
                    raise DeviceError(
                        f"{self._source} stream failed to start: {first['err']}")
                while True:
                    if stop.is_set():
                        break
                    msg = self._wait_msg(block_timeout, stop=stop)
                    if msg is None:                # stop fired while waiting
                        break
                    if "blk" in msg:
                        seq, n = int(msg["blk"]), int(msg["n"])
                        view = np.ndarray((n,), dtype=np.complex64,
                                          buffer=shm.buf,
                                          offset=(seq % STREAM_SLOTS) * slot_bytes)
                        block = view.copy()   # ring slot gets reused; callers
                        del view              # keep refs (recorder) — and the
                                              # export must not outlive close()
                        self._send({"op": "ack", "seq": seq})
                        yield block
                    elif msg.get("stream") == "end":
                        ended = True
                        if msg.get("info") is not None:
                            self._info = msg["info"]
                        if msg.get("err"):
                            raise DeviceError(
                                f"{self._source} stream died: {msg['err']}")
                        break
            finally:
                if not ended and not self._dead:
                    try:
                        self._send({"op": "stream_stop"})
                        deadline = time.time() + 10.0
                        while time.time() < deadline:
                            m = self._wait_msg(max(0.1, deadline - time.time()))
                            if isinstance(m, dict) and m.get("stream") == "end":
                                if m.get("info") is not None:
                                    self._info = m["info"]
                                break
                    except Exception:  # noqa: BLE001 — teardown best-effort
                        pass
                try:
                    shm.close()
                except Exception:  # noqa: BLE001 — see _capture note
                    pass
                try:
                    shm.unlink()
                except Exception:  # noqa: BLE001
                    pass

    # -- RPC plumbing --

    def _forward(self, name, *args, **kwargs):
        return self._request({"op": "call", "name": name, "args": args,
                              "kwargs": kwargs},
                             timeout=self._call_timeout)["ok"]

    def _request(self, msg: dict, timeout, already_locked: bool = False) -> dict:
        if already_locked:
            return self._request_locked(msg, timeout)
        with self._lock:
            return self._request_locked(msg, timeout)

    def _request_locked(self, msg: dict, timeout) -> dict:
        self._check_live()
        self._send(msg)
        reply = self._wait_msg(timeout)
        if not isinstance(reply, dict):
            self._kill_quietly()
            raise DeviceError(f"{self._source} radio worker sent garbage — killed")
        if reply.get("err") is not None and "ok" not in reply:
            # python-level driver error: worker is still healthy; re-raise as
            # the retryable type the callers are built around
            raise DeviceError(reply["err"] if reply.get("cls") == "DeviceError"
                              else f"{reply.get('cls')}: {reply['err']}")
        if reply.get("info") is not None:
            self._info = reply["info"]
        return reply

    def _send(self, msg: dict):
        try:
            self._conn.send(msg)
        except (OSError, ValueError, BrokenPipeError) as e:
            self._mark_dead()
            raise DeviceError(
                f"{self._source} radio worker is gone ({type(e).__name__}) — "
                f"reconnect to respawn it") from e

    def _wait_msg(self, timeout, stop=None):
        """Receive one message, unblocking on worker death (sentinel) and —
        when `stop` is given — on the caller's stop event (returns None).
        Timeout (None = wait forever) kills a wedged worker: a hung native
        call can't be cancelled, only the process can."""
        end = None if timeout is None else (time.time() + timeout)
        while True:
            slice_s = 0.25
            if end is not None:
                remaining = end - time.time()
                if remaining <= 0:
                    self._kill_quietly()
                    raise DeviceError(
                        f"{self._source} radio worker unresponsive for "
                        f"{timeout:.0f}s — killed; reconnect to respawn it")
                slice_s = min(slice_s, remaining) if stop is not None else remaining
            elif stop is None:
                slice_s = None      # plain call, no deadline: block fully
            ready = mp_conn.wait([self._conn, self._proc.sentinel],
                                 timeout=slice_s)
            if self._conn in ready:
                try:
                    return self._conn.recv()
                except (EOFError, OSError) as e:
                    self._mark_dead()
                    raise DeviceError(
                        f"{self._source} radio worker died mid-call "
                        f"(native driver crash?) — reconnect to respawn it"
                    ) from e
            if self._proc.sentinel in ready:
                if self._conn.poll(0.2):        # reply may have raced death
                    try:
                        return self._conn.recv()
                    except (EOFError, OSError):
                        pass
                self._mark_dead()
                raise DeviceError(
                    f"{self._source} radio worker crashed (exit code "
                    f"{self._proc.exitcode}) — reconnect to respawn it")
            if stop is not None and stop.is_set():
                return None

    def _check_live(self):
        if self._dead or not self._proc.is_alive():
            self._mark_dead()
            raise DeviceError(
                f"{self._source} radio worker is not running — "
                f"reconnect to respawn it")

    def _mark_dead(self):
        if not self._dead:
            self._dead = True
            code = self._proc.exitcode if self._proc is not None else None
            log.warning("radio worker for %s is dead (exit code %s)",
                        self._source, code)

    def _kill_quietly(self):
        self._dead = True
        try:
            if self._proc.is_alive():
                self._proc.terminate()
                self._proc.join(timeout=3.0)
                if self._proc.is_alive():
                    self._proc.kill()
        except Exception:  # noqa: BLE001
            pass
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001
            pass

    # -- helpers --

    @staticmethod
    def _make_mirror(cfg, source: str):
        """Unconnected driver instance for LOCAL pure math (predicted RBW /
        grid points). Constructors touch no hardware; anything going wrong
        just disables the fast path."""
        try:
            src = (source or "").strip().lower()
            if src in ("pluto", "sdr", "ad936x"):
                from .sdr_source import PlutoSource
                return PlutoSource(cfg)
            if src in ("b220", "usrp", "uhd"):
                from .uhd_source import B220Source
                return B220Source(cfg)
        except Exception:  # noqa: BLE001
            log.debug("no pure-math mirror for %s", source, exc_info=True)
        return None


def _snapshot_cfg(cfg) -> dict:
    """Plain-data snapshot of a Config for the spawn child. Callers gate on
    isolation_available() so .data is known to be a dict here."""
    import copy
    overrides = [(list(k), v) for k, v in
                 (getattr(cfg, "_runtime_overrides", None) or {}).items()]
    return {"path": getattr(cfg, "path", "") or "",
            "data": copy.deepcopy(cfg.data),
            "overrides": overrides}


def isolation_available(cfg) -> bool:
    """Isolation needs a serializable config snapshot for the spawn child.
    Duck-typed test fakes (get()-only, no .data) can't provide one — they
    keep the legacy in-process path, which is exactly what the test suite
    expects of them."""
    return isinstance(getattr(cfg, "data", None), dict)
