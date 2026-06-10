"""RF Explorer device layer.

Target hardware (ground truth, per build spec): RF Explorer 6G Combo PLUS
(Slim), firmware v3.45, dual internal modules on LEFT and RIGHT SMA ports,
optional RFELIC05 wideband license to 7.5 GHz. Nothing here ASSUMES those
ranges though — on connect the device is queried for model, firmware, and each
module's exact frequency limits, and all segment planning is driven from what
the hardware reports. A mismatch with the expected model/firmware is logged as
a warning, not an error.

Driver strategy:
  1. Official RFExplorer python library if importable AND it connects cleanly.
  2. Raw documented UART protocol over pyserial (handles PLUS models if the
     library lags them).
  3. SimulatedDevice (separate module) for demo/dev — never silently: the UI
     and DB mark everything SIMULATED.

This is a RECEIVE-ONLY application: the only commands ever sent are
configuration/query commands (config request, frequency range, calculator
mode, module select). No transmit capability exists in this protocol use.
"""

import logging
import threading
import time
from dataclasses import dataclass, field, asdict
from typing import Callable, Optional

import serial
from serial.tools import list_ports

log = logging.getLogger("survey.device")

EXPECTED_MODEL_SUBSTRINGS = ("6G COMBO", "PLUS")
EXPECTED_FIRMWARE = "03.45"

# RF Explorer UART API calculator modes (C+<mode>)
CALC_NORMAL, CALC_MAX, CALC_AVG, CALC_OVERWRITE, CALC_MAXHOLD = 0, 1, 2, 3, 4
CALC_BY_NAME = {"normal": CALC_NORMAL, "max": CALC_MAX, "average": CALC_AVG,
                "maxhold": CALC_MAXHOLD}

# Model codes from the UART API #C2-M response
MODEL_NAMES = {
    0: "433M", 1: "868M", 2: "915M", 3: "WSUB1G", 4: "2.4G", 5: "WSUB3G",
    6: "6G", 10: "WSUB1G_PLUS", 11: "ProAudio", 12: "2.4G_PLUS",
    13: "4G_PLUS", 14: "6G_PLUS", 16: "W5G3G", 17: "W5G4G5", 18: "W5G5G",
    19: "MW5G_3G", 20: "MW5G_4G5", 21: "MW5G_5G", 60: "RFEGen", 255: "NONE",
}


class DeviceError(Exception):
    pass


@dataclass
class ModuleInfo:
    side: str                  # 'right' (mainboard) | 'left' (expansion)
    model: str
    min_mhz: float
    max_mhz: float

    def to_dict(self):
        return asdict(self)


@dataclass
class DeviceInfo:
    model: str = "unknown"
    firmware: str = "unknown"
    port: str = ""
    modules: list = field(default_factory=list)     # [ModuleInfo]
    wideband_license: bool = False
    simulated: bool = False
    driver: str = ""
    warnings: list = field(default_factory=list)

    def to_dict(self):
        d = asdict(self)
        return d

    def swept_range_label(self) -> str:
        if not self.modules:
            return "no modules reported"
        lo = min(m.min_mhz for m in self.modules)
        hi = max(m.max_mhz for m in self.modules)
        gaps = self._gaps()
        if gaps:
            gap_txt = "; coverage gap(s): " + ", ".join(
                f"{a:.3f}-{b:.3f} MHz" for a, b in gaps)
        else:
            gap_txt = ""
        lo_txt = f"{lo * 1000:.0f} kHz" if lo < 1 else f"{lo:.3f} MHz"
        hi_txt = f"{hi / 1000:.3f} GHz".rstrip("0").rstrip(".") if hi >= 1000 else f"{hi:.1f} MHz"
        cont = "continuous coverage " if not gaps else "coverage "
        return f"{cont}{lo_txt} – {hi_txt}{gap_txt}"

    def _gaps(self):
        spans = sorted([(m.min_mhz, m.max_mhz) for m in self.modules])
        gaps = []
        for (a_lo, a_hi), (b_lo, b_hi) in zip(spans, spans[1:]):
            if b_lo > a_hi:
                gaps.append((a_hi, b_lo))
        return gaps

    def check_expected(self):
        """Programmatic hardware verification — warnings only, never fatal."""
        mu = self.model.upper()
        if not any(s in mu for s in EXPECTED_MODEL_SUBSTRINGS):
            self.warnings.append(
                f"Model '{self.model}' does not look like the expected 6G Combo PLUS")
        if EXPECTED_FIRMWARE not in self.firmware:
            self.warnings.append(
                f"Firmware '{self.firmware}' differs from expected v{EXPECTED_FIRMWARE}")
        for w in self.warnings:
            log.warning("Hardware verification: %s", w)


@dataclass
class SweepFrame:
    freqs_mhz: list
    amps_dbm: list
    ts: float


class BaseDevice:
    """Interface shared by the official-lib driver, raw driver, and simulator."""

    info: DeviceInfo

    def connect(self) -> DeviceInfo: ...
    def select_module(self, side: str): ...
    def set_range(self, start_mhz: float, end_mhz: float): ...
    def set_calculator(self, mode_name: str): ...
    def read_sweep(self, timeout_s: float = 5.0) -> Optional[SweepFrame]: ...
    def close(self): ...

    def stream(self, seconds: float, on_sweep: Callable[[SweepFrame], None],
               abort: Optional[threading.Event] = None) -> int:
        """Collect sweeps for `seconds`; returns count delivered."""
        n, t_end = 0, time.time() + seconds
        while time.time() < t_end:
            if abort is not None and abort.is_set():
                break
            frame = self.read_sweep(timeout_s=min(5.0, max(0.5, t_end - time.time())))
            if frame is not None:
                on_sweep(frame)
                n += 1
        return n

    def collect(self, count: int, on_sweep: Callable[[SweepFrame], None],
                abort: Optional[threading.Event] = None,
                timeout_s: float = 60.0) -> int:
        n, t_end = 0, time.time() + timeout_s
        while n < count and time.time() < t_end:
            if abort is not None and abort.is_set():
                break
            frame = self.read_sweep()
            if frame is not None:
                on_sweep(frame)
                n += 1
        return n


def autodetect_port() -> Optional[str]:
    """The 6G Combo PLUS presents as a standard CP210x USB-UART bridge."""
    candidates = []
    for p in list_ports.comports():
        desc = (p.description or "").lower()
        if (p.vid == 0x10C4) or "cp210" in desc or "silabs" in desc or "silicon labs" in desc:
            candidates.append(p.device)
    if candidates:
        return candidates[0]
    ports = [p.device for p in list_ports.comports()]
    return ports[0] if ports else None


# ---------------------------------------------------------------------------
# Raw UART driver (documented RF Explorer serial protocol)
# ---------------------------------------------------------------------------

class RawRFEDevice(BaseDevice):
    BAUD = 500000

    def __init__(self, port: str = "", baud: int = BAUD, sweep_points: int = 112):
        self.port_name = port
        self.baud = baud
        self.sweep_points = sweep_points
        self.ser: Optional[serial.Serial] = None
        self.info = DeviceInfo(driver="raw_uart")
        self._buf = b""
        self._lock = threading.Lock()
        self._current_config = {}     # parsed last #C2-F line
        self._active_side = "right"

    # -- protocol helpers --

    def _send(self, command: str):
        """RF Explorer command framing: '#' + <total length byte> + command."""
        payload = b"#" + bytes([len(command) + 2]) + command.encode("ascii")
        with self._lock:
            self.ser.write(payload)

    def _read_until_newline(self, timeout_s: float) -> Optional[bytes]:
        t_end = time.time() + timeout_s
        while time.time() < t_end:
            idx = self._buf.find(b"\r\n")
            if idx >= 0:
                line, self._buf = self._buf[:idx], self._buf[idx + 2:]
                return line
            chunk = self.ser.read(4096)
            if chunk:
                self._buf += chunk
        return None

    # -- lifecycle --

    def connect(self) -> DeviceInfo:
        self.port_name = self.port_name or autodetect_port()
        if not self.port_name:
            raise DeviceError("No serial ports found — is the RF Explorer plugged in? "
                              "(and the vendor RF Explorer software closed?)")
        try:
            self.ser = serial.Serial(self.port_name, self.baud, timeout=0.5)
        except serial.SerialException as e:
            raise DeviceError(f"Could not open {self.port_name}: {e}") from e
        self.info.port = self.port_name
        time.sleep(0.5)
        self.ser.reset_input_buffer()
        self._buf = b""
        self._send("C0")                       # request config + model
        self._await_identity(timeout_s=8.0)
        if not self.info.modules:
            raise DeviceError("Device did not report model/config (#C2-M / #C2-F) — "
                              "wrong port or unsupported protocol")
        self.info.check_expected()
        return self.info

    def _await_identity(self, timeout_s: float):
        """Parse #C2-M (models) and #C2-F (current config incl. module min/max)."""
        t_end = time.time() + timeout_s
        got_m = got_f = False
        while time.time() < t_end and not (got_m and got_f):
            line = self._read_until_newline(0.5)
            if line is None:
                continue
            if line.startswith(b"#C2-M:"):
                got_m = self._parse_models(line)
            elif line.startswith(b"#C2-F:"):
                got_f = self._parse_config(line)
        # read the inactive module's range by switching to it and back
        if got_m and got_f and len(self.info.modules) == 2:
            try:
                other = "left" if self._active_side == "right" else "right"
                first = self._active_side
                self.select_module(other)
                self.select_module(first)
            except DeviceError as e:
                self.info.warnings.append(f"Could not read second module range: {e}")

    def _parse_models(self, line: bytes) -> bool:
        try:
            parts = line.decode(errors="replace").split(":", 1)[1].split(",")
            main_code = int(parts[0])
            exp_code = int(parts[1])
            firmware = parts[2].strip() if len(parts) > 2 else "unknown"
        except (ValueError, IndexError):
            return False
        main = MODEL_NAMES.get(main_code, f"code{main_code}")
        exp = MODEL_NAMES.get(exp_code, f"code{exp_code}")
        self.info.firmware = firmware
        self.info.model = f"{main}+{exp}" if exp_code != 255 else main
        # mainboard = RIGHT SMA, expansion = LEFT SMA on dual-port units
        self.info.modules = []
        self.info.modules.append(ModuleInfo("right", main, 0.0, 0.0))
        if exp_code != 255:
            self.info.modules.append(ModuleInfo("left", exp, 0.0, 0.0))
        return True

    def _parse_config(self, line: bytes) -> bool:
        """#C2-F:Start_kHz,FreqStep_Hz,AmpTop,AmpBottom,SweepPoints,ExpActive,
        CurrentMode,Min_kHz,Max_kHz,MaxSpan_kHz[,RBW_kHz,AmpOffset,CalcMode]"""
        try:
            parts = line.decode(errors="replace").split(":", 1)[1].split(",")
            cfg = {
                "start_khz": float(parts[0]), "step_hz": float(parts[1]),
                "sweep_points": int(parts[4]), "expansion_active": parts[5].strip() == "1",
                "min_khz": float(parts[7]), "max_khz": float(parts[8]),
                "max_span_khz": float(parts[9]),
            }
        except (ValueError, IndexError):
            return False
        self._current_config = cfg
        self._active_side = "left" if cfg["expansion_active"] else "right"
        for m in self.info.modules:
            if m.side == self._active_side:
                m.min_mhz = cfg["min_khz"] / 1000.0
                m.max_mhz = cfg["max_khz"] / 1000.0
        # wideband license detection: any module reporting beyond 6.1 GHz
        if cfg["max_khz"] > 6_150_000:
            self.info.wideband_license = True
        return True

    def select_module(self, side: str):
        """CM<0> = mainboard (RIGHT SMA), CM<1> = expansion (LEFT SMA)."""
        want_expansion = (side == "left")
        self._send("CM" + ("\x01" if want_expansion else "\x00"))
        t_end = time.time() + 8.0
        while time.time() < t_end:
            line = self._read_until_newline(0.5)
            if line and line.startswith(b"#C2-F:") and self._parse_config(line):
                if self._active_side == side:
                    return
        raise DeviceError(f"Module switch to {side} not confirmed by device")

    def set_range(self, start_mhz: float, end_mhz: float):
        start_khz, end_khz = int(start_mhz * 1000), int(end_mhz * 1000)
        self._send(f"C2-F:{start_khz:07d},{end_khz:07d}")
        t_end = time.time() + 6.0
        while time.time() < t_end:
            line = self._read_until_newline(0.5)
            if line and line.startswith(b"#C2-F:") and self._parse_config(line):
                return
        raise DeviceError(f"Range {start_mhz}-{end_mhz} MHz not confirmed by device")

    def set_calculator(self, mode_name: str):
        mode = CALC_BY_NAME.get(mode_name, CALC_NORMAL)
        self._send("C+" + chr(mode))
        time.sleep(0.15)

    def read_sweep(self, timeout_s: float = 5.0) -> Optional[SweepFrame]:
        """Sweep lines: '$S' + count byte + <count> amplitude bytes (each -dBm*2),
        or '$s' + count byte where points = (count+1)*16 for extended sweeps."""
        t_end = time.time() + timeout_s
        while time.time() < t_end:
            idx_s = self._buf.find(b"$S")
            idx_x = self._buf.find(b"$s")
            idx = min(i for i in (idx_s, idx_x) if i >= 0) if (idx_s >= 0 or idx_x >= 0) else -1
            if idx < 0:
                chunk = self.ser.read(4096)
                if chunk:
                    self._buf += chunk
                continue
            if len(self._buf) < idx + 3:
                chunk = self.ser.read(4096)
                if chunk:
                    self._buf += chunk
                continue
            marker = self._buf[idx:idx + 2]
            count_byte = self._buf[idx + 2]
            n_points = (count_byte + 1) * 16 if marker == b"$s" else count_byte
            need = idx + 3 + n_points + 2
            if len(self._buf) < need:
                chunk = self.ser.read(4096)
                if chunk:
                    self._buf += chunk
                    continue
                continue
            raw = self._buf[idx + 3: idx + 3 + n_points]
            self._buf = self._buf[need:]
            amps = [-(b / 2.0) for b in raw]
            cfg = self._current_config
            if not cfg:
                return None
            start_mhz = cfg["start_khz"] / 1000.0
            step_mhz = cfg["step_hz"] / 1e6
            freqs = [start_mhz + i * step_mhz for i in range(len(amps))]
            return SweepFrame(freqs, amps, time.time())
        return None

    def close(self):
        if self.ser:
            try:
                self.ser.close()
            except Exception:  # noqa: BLE001
                pass
            self.ser = None


# ---------------------------------------------------------------------------
# Official-library driver (preferred when the lib supports the connected unit)
# ---------------------------------------------------------------------------

class OfficialRFEDevice(BaseDevice):
    def __init__(self, port: str = "", baud: int = 500000, sweep_points: int = 112):
        from RFExplorer import RFECommunicator  # raises ImportError if absent
        self._rfe_cls = RFECommunicator
        self.port_name = port
        self.baud = baud
        self.rfe = None
        self.info = DeviceInfo(driver="official_lib")

    def connect(self) -> DeviceInfo:
        self.rfe = self._rfe_cls()
        self.rfe.AutoConfigure = False
        port = self.port_name or autodetect_port()
        if not port:
            raise DeviceError("No serial ports found")
        if not self.rfe.ConnectPort(port, self.baud):
            raise DeviceError(f"Official library could not connect on {port}")
        self.info.port = port
        time.sleep(2.0)
        self.rfe.SendCommand_RequestConfigData()
        t_end = time.time() + 8.0
        while time.time() < t_end and not self.rfe.ActiveModel:
            self.rfe.ProcessReceivedString(True)
            time.sleep(0.1)
        if not self.rfe.ActiveModel:
            raise DeviceError("Official library got no model response")
        self.info.model = str(self.rfe.ActiveModel)
        self.info.firmware = str(getattr(self.rfe, "RFExplorerFirmwareDetected", "unknown"))
        main_min = getattr(self.rfe, "MinFreqMHZ", 0.0)
        main_max = getattr(self.rfe, "MaxFreqMHZ", 0.0)
        self.info.modules = [ModuleInfo("right", self.info.model, main_min, main_max)]
        if getattr(self.rfe, "ExpansionBoardActive", False) or \
                getattr(self.rfe, "ExpansionBoardModel", None):
            self.info.modules.append(ModuleInfo(
                "left", str(getattr(self.rfe, "ExpansionBoardModel", "expansion")), 0.0, 0.0))
        self.info.check_expected()
        return self.info

    def select_module(self, side: str):
        if side == "left":
            self.rfe.SendCommand_EnableExpansion() if hasattr(self.rfe, "SendCommand_EnableExpansion") \
                else self.rfe.SendCommand("CM\x01")
        else:
            self.rfe.SendCommand_EnableMainboard() if hasattr(self.rfe, "SendCommand_EnableMainboard") \
                else self.rfe.SendCommand("CM\x00")
        time.sleep(3.0)
        self.rfe.SendCommand_RequestConfigData()
        t_end = time.time() + 6.0
        while time.time() < t_end:
            self.rfe.ProcessReceivedString(True)
            time.sleep(0.1)
        for m in self.info.modules:
            if m.side == side:
                m.min_mhz = getattr(self.rfe, "MinFreqMHZ", m.min_mhz)
                m.max_mhz = getattr(self.rfe, "MaxFreqMHZ", m.max_mhz)

    def set_range(self, start_mhz: float, end_mhz: float):
        self.rfe.UpdateDeviceConfig(start_mhz, end_mhz)
        time.sleep(1.0)
        self.rfe.SweepData.CleanAll()

    def set_calculator(self, mode_name: str):
        self.rfe.SendCommand("C+" + chr(CALC_BY_NAME.get(mode_name, CALC_NORMAL)))
        time.sleep(0.15)

    def read_sweep(self, timeout_s: float = 5.0) -> Optional[SweepFrame]:
        t_end = time.time() + timeout_s
        start_count = self.rfe.SweepData.Count
        while time.time() < t_end:
            self.rfe.ProcessReceivedString(True)
            if self.rfe.SweepData.Count > start_count:
                sweep = self.rfe.SweepData.GetData(self.rfe.SweepData.Count - 1)
                freqs = [sweep.GetFrequencyMHZ(i) for i in range(sweep.TotalSteps)]
                amps = [sweep.GetAmplitudeDBM(i, None, False) for i in range(sweep.TotalSteps)]
                return SweepFrame(freqs, amps, time.time())
            time.sleep(0.02)
        return None

    def close(self):
        if self.rfe:
            try:
                self.rfe.Close()
            except Exception:  # noqa: BLE001
                pass
            self.rfe = None


def open_device(cfg) -> BaseDevice:
    """Driver selection: simulator (explicit config only) -> official lib -> raw UART."""
    if cfg.get("device", "simulate", default=False):
        from .simulator import SimulatedDevice
        dev = SimulatedDevice()
        dev.connect()
        return dev
    port = cfg.get("device", "port", default="") or ""
    baud = cfg.get("device", "baud", default=500000)
    points = cfg.get("sweep", "sweep_points", default=112)
    errors = []
    try:
        dev = OfficialRFEDevice(port, baud, points)
        dev.connect()
        return dev
    except ImportError:
        errors.append("RFExplorer library not installed")
    except Exception as e:  # noqa: BLE001 - fall through to raw driver
        errors.append(f"official lib: {e}")
    try:
        dev = RawRFEDevice(port, baud, points)
        dev.connect()
        dev.info.warnings.extend(errors)
        return dev
    except Exception as e:  # noqa: BLE001
        errors.append(f"raw driver: {e}")
        raise DeviceError("; ".join(errors)) from e
