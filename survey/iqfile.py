"""SigMF raw-IQ record / replay — capture off-air ONCE, iterate the decoder offline.

WHY THIS MODULE EXISTS
survey/ambe.py states the problem plainly: no frame this repo decodes has ever
come off a real radio. Two assumptions are unvalidated and INVISIBLE to synthetic
tests — the physical bit ORDER inside the 88-bit IMBE information vector (our own
convention; the widths and the quantizer arithmetic are sourced, the bit
positions are not) and transport de-interleaving (absent). Settling either one
needs a real signal and then MANY decode attempts against that same signal.

Re-receiving per attempt would be both slow and non-repeatable: the air changes
between tries, so a decode that improves might be a better assumption or might
just be a stronger burst — you could not tell which. Writing the samples to disk
once and replaying them makes every subsequent change a controlled experiment.

WHY SigMF AND NOT A PRIVATE FORMAT
https://sigmf.org — a `.sigmf-data` file of interleaved samples plus a
`.sigmf-meta` JSON sidecar. GNU Radio, inspectrum, SDRangel, sigmf-python and
Universal Radio Hacker all read it. A capture that stumps this decoder can be
opened in a tool that is already known to work and compared side by side; that
cross-check is most of the value of the bench exercise, and a private container
would throw it away for nothing.

WHAT IS AND IS NOT GUARANTEED
  * cf32_le (numpy complex64) round-trips EXACTLY, bit for bit. complex64 is what
    capture_iq/stream_iq return, so a replay hands the decoder the same array it
    would have seen live. tests/test_iqfile.py pins this.
  * ci16_le is supported on read and on write. It is LOSSY relative to complex64:
    each float is scaled by 32767 and rounded, and anything outside +/-1.0 CLIPS.
    It halves the file, which for a long capture is real, but a bench recording
    you intend to bisect a bit-order assumption against should be cf32_le.
  * A short or byte-misaligned data file is REPORTED, never zero-padded. A
    truncated capture read as zeros looks exactly like a dead band, and the whole
    point of a field trip is that you cannot go back and check.

MEMORY. A 60 s capture at 2.4 MS/s is ~1.1 GB as complex64 — larger than the
comfortable working set on the field laptop. Nothing here loads a whole file
unconditionally: SigMFReader.blocks() streams, .read(count) is bounded, and
.read_all() is the one call that does load everything and is named so you cannot
do it by accident.

numpy + stdlib only, in keeping with the rest of survey/.
"""

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np

log = logging.getLogger("survey.iqfile")

SIGMF_VERSION = "1.0.0"          # the widely-implemented spec level; GNU Radio,
                                 # inspectrum and sigmf-python all read it
DATA_EXT = ".sigmf-data"
META_EXT = ".sigmf-meta"

# Our non-core metadata lives under a declared namespace, which is what the spec
# asks for. Anything a foreign tool does not understand it is required to ignore,
# so these fields cost nothing in interoperability.
SHIELD_NS = "shield"
SHIELD_EXT_VERSION = "1.0.0"

# SigMF datatype string -> (on-disk numpy dtype, elements per complex sample,
# full-scale divisor). The divisor exists because both SDR sources in this repo
# normalise to ~+/-1 before handing IQ up (sdr_source.ADC_FULL_SCALE, and UHD's
# fc32 which is already +/-1), so an int16 file holds that normalised value times
# full scale — NOT raw ADC counts. Anyone reading these files elsewhere gets the
# same convention every other SigMF ci16 recording uses.
_DTYPES = {
    "cf32_le": (np.dtype("<c8"), 1, 1.0),
    "ci16_le": (np.dtype("<i2"), 2, 32767.0),
}
DEFAULT_DATATYPE = "cf32_le"

# |I| or |Q| at or above this (post-normalisation) means the front end was
# pinned. Not exactly 1.0: a hard-limited converter and a rounding artefact land
# in the same place, and being one LSB pedantic here would hide real clipping.
CLIP_LEVEL = 0.999
# Below this peak the capture is buried in the noise floor — technically valid,
# practically useless for symbol recovery. Worth saying out loud at record time,
# while the operator is still standing next to the antenna.
WEAK_PEAK = 0.02
DEFAULT_BLOCK = 1 << 18          # 262144 samples = 2 MB as complex64


class IQFileError(Exception):
    """A recording could not be written, or could not be trusted on read."""


def utc_now_iso() -> str:
    """SigMF's `core:datetime` format: ISO-8601, UTC, explicit Z, ms precision."""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def paths_for(path: str):
    """(data_path, meta_path) for a base name, a .sigmf-data or a .sigmf-meta.

    Accepting all three means the operator can tab-complete either half of the
    pair and get the same recording, which is what he will actually do at 2 a.m.
    """
    p = str(path)
    for ext in (DATA_EXT, META_EXT):
        if p.lower().endswith(ext):
            p = p[:-len(ext)]
            break
    else:
        if p.lower().endswith(".sigmf"):
            p = p[:-len(".sigmf")]
    return p + DATA_EXT, p + META_EXT


def _sample_bytes(datatype: str, num_channels: int = 1) -> int:
    dt, elems, _ = _DTYPES[datatype]
    return dt.itemsize * elems * int(num_channels)


def _check_datatype(datatype: str) -> str:
    if datatype not in _DTYPES:
        raise IQFileError(
            f"unsupported SigMF datatype {datatype!r} — this module handles "
            f"{', '.join(sorted(_DTYPES))} (complex, little-endian). A file in "
            "another datatype is not corrupt, it is just not ours to read.")
    return datatype


def to_complex64(raw: np.ndarray, datatype: str) -> np.ndarray:
    """On-disk elements -> normalised complex64. Inverse of from_complex64()."""
    _, elems, scale = _DTYPES[_check_datatype(datatype)]
    if elems == 1:
        return np.asarray(raw, dtype=np.complex64)
    f = np.asarray(raw, dtype=np.float32) / np.float32(scale)
    return (f[0::2] + 1j * f[1::2]).astype(np.complex64)


def from_complex64(iq, datatype: str) -> np.ndarray:
    """Normalised complex64 -> on-disk elements. Rounds and clips for ci16_le;
    cf32_le is exact (the array is only re-viewed as little-endian)."""
    dt, elems, scale = _DTYPES[_check_datatype(datatype)]
    a = np.asarray(iq, dtype=np.complex64)
    if elems == 1:
        return np.ascontiguousarray(a, dtype=dt)
    inter = np.empty(a.size * 2, dtype=np.float64)
    inter[0::2] = a.real
    inter[1::2] = a.imag
    inter *= scale
    np.clip(inter, -scale, scale, out=inter)
    return np.rint(inter).astype(dt)


@dataclass
class IQMeta:
    """What makes a recording reproducible, and nothing else.

    These are exactly the questions you cannot answer later from the samples
    alone: where was the radio tuned, how fast was it sampling, when, on what
    hardware, at what gain, and what did the operator think he was pointing at.
    Get one of them wrong and the file is scientifically worthless even though it
    replays perfectly.
    """
    center_hz: float
    sample_rate: float
    datatype: str = DEFAULT_DATATYPE
    utc_start: str = ""              # ISO-8601 Z; filled at write time if blank
    source: str = ""                 # "b220" / "pluto" / whatever opened the radio
    hardware: str = ""               # free-form device string (model, serial, driver)
    gain_db: float = None
    gain_mode: str = ""
    note: str = ""                   # operator's free text — what he aimed at, and why
    num_channels: int = 1
    recorder: str = "Shield TSCM spectrum_survey (survey/iqfile.py)"
    sample_count: int = None         # filled at close; None until then
    sha512: str = ""                 # of the data file, filled at close
    extra: dict = field(default_factory=dict)   # extra shield: fields (clipping etc.)

    def to_sigmf(self) -> dict:
        """The SigMF metadata object. `core:datatype`, `core:version` and one
        capture segment with `core:sample_start` are the spec's hard
        requirements; everything else here is either core-recommended or lives
        under our declared namespace."""
        _check_datatype(self.datatype)
        g = {
            "core:datatype": self.datatype,
            "core:version": SIGMF_VERSION,
            "core:sample_rate": float(self.sample_rate),
            "core:num_channels": int(self.num_channels),
            "core:recorder": self.recorder,
            "core:extensions": [
                {"name": SHIELD_NS, "version": SHIELD_EXT_VERSION, "optional": True},
            ],
        }
        if self.note:
            g["core:description"] = self.note
        if self.hardware:
            g["core:hw"] = self.hardware
        if self.sha512:
            g["core:sha512"] = self.sha512
        shield = {
            f"{SHIELD_NS}:source": self.source,
            f"{SHIELD_NS}:gain_db": (None if self.gain_db is None
                                     else float(self.gain_db)),
            f"{SHIELD_NS}:gain_mode": self.gain_mode,
            f"{SHIELD_NS}:note": self.note,
        }
        # sample_count is OURS, not core: SigMF derives the count from the file
        # size. We record it anyway so a truncated file can be DETECTED rather
        # than merely read short — see SigMFReader.
        if self.sample_count is not None:
            shield[f"{SHIELD_NS}:sample_count"] = int(self.sample_count)
        for k, v in (self.extra or {}).items():
            shield[k if ":" in k else f"{SHIELD_NS}:{k}"] = v
        g.update(shield)
        return {
            "global": g,
            "captures": [{
                "core:sample_start": 0,
                "core:frequency": float(self.center_hz),
                "core:datetime": self.utc_start or utc_now_iso(),
            }],
            "annotations": [],
        }

    @classmethod
    def from_sigmf(cls, obj: dict) -> "IQMeta":
        g = (obj or {}).get("global") or {}
        caps = (obj or {}).get("captures") or []
        cap0 = caps[0] if caps else {}
        # Required by the spec. Missing means this is not a SigMF recording we
        # can honour, and guessing a datatype would silently misread every sample.
        for req in ("core:datatype", "core:version"):
            if req not in g:
                raise IQFileError(f"SigMF metadata is missing the required "
                                  f"field {req!r}")
        fs = g.get("core:sample_rate")
        if not fs:
            # Core-recommended rather than required by the spec, but a replay
            # without it cannot compute a symbol rate — refuse instead of
            # inventing one.
            raise IQFileError("SigMF metadata has no core:sample_rate — a replay "
                              "cannot recover symbol timing without it")
        extra = {k: v for k, v in g.items()
                 if k.startswith(f"{SHIELD_NS}:") and k not in (
                     f"{SHIELD_NS}:source", f"{SHIELD_NS}:gain_db",
                     f"{SHIELD_NS}:gain_mode", f"{SHIELD_NS}:note",
                     f"{SHIELD_NS}:sample_count")}
        return cls(
            center_hz=float(cap0.get("core:frequency") or 0.0),
            sample_rate=float(fs),
            datatype=_check_datatype(g["core:datatype"]),
            utc_start=str(cap0.get("core:datetime") or ""),
            source=str(g.get(f"{SHIELD_NS}:source") or ""),
            hardware=str(g.get("core:hw") or ""),
            gain_db=g.get(f"{SHIELD_NS}:gain_db"),
            gain_mode=str(g.get(f"{SHIELD_NS}:gain_mode") or ""),
            note=str(g.get(f"{SHIELD_NS}:note") or g.get("core:description") or ""),
            num_channels=int(g.get("core:num_channels") or 1),
            recorder=str(g.get("core:recorder") or ""),
            sample_count=g.get(f"{SHIELD_NS}:sample_count"),
            sha512=str(g.get("core:sha512") or ""),
            extra=extra,
        )


class SigMFWriter:
    """Streaming writer: append blocks as they arrive off the radio.

    The sidecar is written TWICE — once at open with everything already known,
    and again at close with the final sample count, the SHA-512 and the clipping
    telemetry. That ordering is deliberate: if the radio faults or the operator
    yanks the USB mid-capture, the partial data file still has a sidecar naming
    its frequency and sample rate, so it is a salvageable recording instead of an
    anonymous pile of bytes. The reader will then correctly report it as
    truncated, because the close-time sample count never landed.
    """

    def __init__(self, path: str, meta: IQMeta):
        self.data_path, self.meta_path = paths_for(path)
        self.meta = meta
        _check_datatype(meta.datatype)
        if int(meta.num_channels) != 1:
            raise IQFileError("only single-channel recordings are supported here "
                              "— a 2-channel SigMF file interleaves channels and "
                              "the decode chain expects one stream")
        if not meta.utc_start:
            self.meta.utc_start = utc_now_iso()
        d = os.path.dirname(os.path.abspath(self.data_path))
        if d:
            os.makedirs(d, exist_ok=True)
        self.samples = 0
        self.clipped = 0
        self.peak = 0.0
        self._sumsq = 0.0
        self._hash = hashlib.sha512()
        self._f = open(self.data_path, "wb")
        self._closed = False
        self._write_meta()               # crash-survivable sidecar, see docstring

    # -- context manager so a failed capture still closes the file handle
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    def _write_meta(self):
        tmp = self.meta_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.meta.to_sigmf(), f, indent=2)
            f.write("\n")
        os.replace(tmp, self.meta_path)

    def append(self, iq) -> int:
        """Write one block. Returns the number of complex samples written.

        Clipping is measured on the NORMALISED float, before any int16
        quantisation, so the number means "the front end was pinned" regardless
        of the on-disk datatype. That is the number that tells the operator to
        turn the gain down, and it is the one thing about a capture that cannot
        be fixed afterwards.
        """
        if self._closed:
            raise IQFileError("writer is closed")
        a = np.asarray(iq, dtype=np.complex64).ravel()
        if a.size == 0:
            return 0
        mag = np.maximum(np.abs(a.real), np.abs(a.imag))
        self.clipped += int(np.count_nonzero(mag >= CLIP_LEVEL))
        self.peak = max(self.peak, float(mag.max()))
        self._sumsq += float(np.sum((a.real.astype(np.float64) ** 2
                                     + a.imag.astype(np.float64) ** 2)))
        raw = from_complex64(a, self.meta.datatype)
        buf = raw.tobytes()
        self._f.write(buf)
        self._hash.update(buf)
        self.samples += int(a.size)
        return int(a.size)

    @property
    def seconds(self) -> float:
        return self.samples / float(self.meta.sample_rate or 1.0)

    def warnings(self) -> list:
        """Things that would silently ruin this capture, in plain language.

        Said at record time, not at replay time — a clipped or buried capture is
        a wasted trip only if nobody notices until the operator is home.
        """
        out = []
        if self.samples == 0:
            out.append("NO SAMPLES were written — the radio produced nothing. "
                       "This file is empty; do not walk away from the site.")
            return out
        if self.clipped:
            frac = self.clipped / float(self.samples)
            out.append(
                f"CLIPPING: {self.clipped} of {self.samples} samples "
                f"({frac * 100:.2f}%) hit the converter rails. A clipped capture "
                "spreads the signal across the band and wrecks the C4FM eye — "
                "drop RX gain ~10 dB and record again.")
        if self.peak < WEAK_PEAK:
            out.append(
                f"WEAK: peak |I|/|Q| is {self.peak:.4f} of full scale. The signal "
                "is down in the quantiser noise; raise gain or get closer before "
                "trusting a decode against this file.")
        return out

    def summary(self) -> dict:
        rms = (self._sumsq / self.samples) ** 0.5 if self.samples else 0.0
        return {
            "data_path": self.data_path, "meta_path": self.meta_path,
            "datatype": self.meta.datatype, "samples": self.samples,
            "bytes": self.samples * _sample_bytes(self.meta.datatype,
                                                  self.meta.num_channels),
            "seconds": round(self.seconds, 4),
            "sample_rate": float(self.meta.sample_rate),
            "center_hz": float(self.meta.center_hz),
            "peak_amplitude": round(self.peak, 6),
            "rms_amplitude": round(rms, 6),
            "clipped_samples": self.clipped,
            "clipped_fraction": (self.clipped / self.samples) if self.samples else 0.0,
            "sha512": self.meta.sha512,
            "warnings": self.warnings(),
        }

    def close(self) -> dict:
        """Flush, finalise the sidecar, return the summary. Idempotent."""
        if self._closed:
            return summary_from_meta_path(self.meta_path)
        try:
            self._f.close()
        finally:
            self._closed = True
        self.meta.sample_count = self.samples
        self.meta.sha512 = self._hash.hexdigest()
        self.meta.extra = dict(self.meta.extra or {})
        self.meta.extra.update({
            "clipped_samples": self.clipped,
            "clipped_fraction": round(
                (self.clipped / self.samples) if self.samples else 0.0, 9),
            "peak_amplitude": round(self.peak, 6),
            "clip_level": CLIP_LEVEL,
        })
        self._write_meta()
        return self.summary()


def summary_from_meta_path(meta_path: str) -> dict:
    with open(meta_path, "r", encoding="utf-8") as f:
        obj = json.load(f)
    m = IQMeta.from_sigmf(obj)
    return {"data_path": paths_for(meta_path)[0], "meta_path": meta_path,
            "datatype": m.datatype, "samples": m.sample_count,
            "sample_rate": m.sample_rate, "center_hz": m.center_hz,
            "sha512": m.sha512, "warnings": []}


def write_sigmf(path: str, iq, meta: IQMeta) -> dict:
    """One-shot write of an in-memory array. Convenience over SigMFWriter for
    tests and short captures; a real recording should stream through the writer
    rather than build a 1 GB array first."""
    with SigMFWriter(path, meta) as w:
        w.append(iq)
        return w.close()


class SigMFReader:
    """Streaming reader. Open, ask what it holds, then take it in blocks.

    Honesty contract (the reason `strict` defaults to True): the sample count on
    disk is compared against the count the writer recorded at close. Fewer
    samples than declared means the capture was interrupted — a crashed driver, a
    full disk, a pulled cable. Reading that as a short-but-fine file, or padding
    it to length, would present an accident as data.
    """

    def __init__(self, path: str, strict: bool = True):
        self.data_path, self.meta_path = paths_for(path)
        if not os.path.exists(self.meta_path):
            raise IQFileError(f"no SigMF sidecar at {self.meta_path} — without it "
                              "the samples have no frequency, rate or datatype "
                              "and cannot be replayed")
        if not os.path.exists(self.data_path):
            raise IQFileError(f"no SigMF data file at {self.data_path}")
        with open(self.meta_path, "r", encoding="utf-8") as f:
            try:
                self.sigmf = json.load(f)
            except json.JSONDecodeError as e:
                raise IQFileError(f"SigMF sidecar is not valid JSON: {e}") from e
        self.meta = IQMeta.from_sigmf(self.sigmf)
        if int(self.meta.num_channels) != 1:
            raise IQFileError(f"{self.meta.num_channels}-channel recording — this "
                              "reader handles single-channel files only")
        self._sbytes = _sample_bytes(self.meta.datatype, self.meta.num_channels)
        self._dt, self._elems, _scale = _DTYPES[self.meta.datatype]
        size = os.path.getsize(self.data_path)
        self.file_bytes = size
        self.trailing_bytes = size % self._sbytes
        self.sample_count = size // self._sbytes
        self.declared_sample_count = self.meta.sample_count
        self.truncated = bool(self.declared_sample_count is not None
                              and self.sample_count < int(self.declared_sample_count))
        self.problems = []
        if self.trailing_bytes:
            self.problems.append(
                f"data file is {size} bytes, not a whole number of "
                f"{self._sbytes}-byte samples ({self.trailing_bytes} bytes left "
                "over) — the file was cut mid-sample or is not this datatype")
        if self.truncated:
            missing = int(self.declared_sample_count) - self.sample_count
            self.problems.append(
                f"TRUNCATED: sidecar declares {self.declared_sample_count} samples, "
                f"the file holds {self.sample_count} ({missing} missing, "
                f"{missing / float(self.meta.sample_rate or 1.0):.3f} s) — the "
                "capture was interrupted")
        if self.declared_sample_count is None:
            self.problems.append(
                "sidecar has no shield:sample_count — the recording was never "
                "closed cleanly, so completeness cannot be checked (the samples "
                "present are still real)")
        if self.problems and strict:
            raise IQFileError("; ".join(self.problems) + ". Pass strict=False to "
                              "read what is there anyway.")
        self._f = open(self.data_path, "rb")
        self._closed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    # -- what it holds -------------------------------------------------------
    @property
    def sample_rate(self) -> float:
        return float(self.meta.sample_rate)

    @property
    def center_hz(self) -> float:
        return float(self.meta.center_hz)

    @property
    def datatype(self) -> str:
        return self.meta.datatype

    @property
    def seconds(self) -> float:
        return self.sample_count / float(self.meta.sample_rate or 1.0)

    def describe(self) -> dict:
        return {
            "data_path": self.data_path, "meta_path": self.meta_path,
            "datatype": self.datatype, "samples": self.sample_count,
            "declared_samples": self.declared_sample_count,
            "bytes": self.file_bytes, "seconds": round(self.seconds, 4),
            "sample_rate": self.sample_rate, "center_hz": self.center_hz,
            "utc_start": self.meta.utc_start, "source": self.meta.source,
            "hardware": self.meta.hardware, "gain_db": self.meta.gain_db,
            "gain_mode": self.meta.gain_mode, "note": self.meta.note,
            "sha512": self.meta.sha512, "truncated": self.truncated,
            "problems": list(self.problems), "extra": dict(self.meta.extra or {}),
        }

    # -- reading -------------------------------------------------------------
    def _clamp(self, start: int, count):
        start = max(0, int(start))
        avail = max(0, self.sample_count - start)
        if count is None:
            return start, avail
        return start, max(0, min(int(count), avail))

    def read(self, count: int, start: int = 0) -> np.ndarray:
        """Bounded read: at most `count` samples from `start`, as complex64.

        Short only at end of file. The count is mandatory — an unbounded read of
        a multi-gigabyte capture is a decision, not a default (see read_all)."""
        if self._closed:
            raise IQFileError("reader is closed")
        start, n = self._clamp(start, count)
        if n == 0:
            return np.zeros(0, dtype=np.complex64)
        self._f.seek(start * self._sbytes)
        raw = np.frombuffer(self._f.read(n * self._sbytes), dtype=self._dt)
        return to_complex64(raw, self.meta.datatype)

    def read_all(self) -> np.ndarray:
        """The whole file in memory. ~1.1 GB for 60 s at 2.4 MS/s as complex64 —
        deliberately named so nobody does this to a field capture by accident."""
        return self.read(self.sample_count, 0)

    def blocks(self, block: int = DEFAULT_BLOCK, start: int = 0, count=None):
        """Iterate the recording in complex64 chunks of at most `block` samples.

        This is the path a replay should use: constant memory regardless of file
        size, and the last block is short rather than padded."""
        if self._closed:
            raise IQFileError("reader is closed")
        start, remaining = self._clamp(start, count)
        block = max(1, int(block))
        pos = start
        while remaining > 0:
            n = min(block, remaining)
            chunk = self.read(n, pos)
            if chunk.size == 0:
                break
            yield chunk
            pos += chunk.size
            remaining -= chunk.size

    def verify_sha512(self):
        """(ok, digest) against the sidecar's `core:sha512`, streamed.

        Not automatic: hashing a 1 GB file every time you replay it would tax
        every iteration of the loop this module exists to make fast. Run it once
        after the capture, and again if a file has travelled."""
        h = hashlib.sha512()
        with open(self.data_path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        digest = h.hexdigest()
        if not self.meta.sha512:
            return None, digest          # nothing recorded to compare against
        return (digest == self.meta.sha512), digest

    def close(self):
        if not self._closed:
            try:
                self._f.close()
            finally:
                self._closed = True


def read_sigmf(path: str, count=None, start: int = 0, strict: bool = True):
    """Convenience: open, read, close. Returns (iq, IQMeta).

    `count=None` reads to the end — fine for a test vector or a short capture,
    reckless for a field recording. Prefer SigMFReader.blocks() there."""
    with SigMFReader(path, strict=strict) as r:
        iq = r.read_all() if count is None else r.read(count, start)
        return iq, r.meta
