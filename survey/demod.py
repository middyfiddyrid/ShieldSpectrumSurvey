"""Audio demodulation of Pluto IQ — the 'listen' layer (DEMOD_LISTEN_DESIGN.md).

Tune to a frequency, capture IQ, and turn it into something you can HEAR so an
operator can tell an analog voice bug from telemetry from digital hash. Modes:
WFM (broadcast), NFM (narrowband voice / land-mobile / classic audio bug), AM,
SSB (USB/LSB — single-sideband voice), CW (Morse / unmodulated carriers as a beat note).

Receive-only, like the rest of the app. Pure numpy DSP (no scipy) — unit tested
in tests/test_demod.py. Audio quality is "field intelligible", not hi-fi: a
crude boxcar channel filter + FM/AM discriminator + decimation to ~24 kHz WAV.
Digital signals (cellular, Wi-Fi) correctly come out as hash — that's the tell.
"""

import struct

import numpy as np

AUDIO_RATE = 24000
SSB_BW = 3000.0      # SSB audio passband (~3 kHz, communications voice)
CW_BFO = 700.0       # CW beat-frequency oscillator offset -> audible tone (Hz)
CW_BW = 1200.0       # narrow filter around the CW carrier before the beat


def pick_mode(center_mhz: float, label: str = "") -> str:
    """Default demod mode by band (operator can override in the UI)."""
    lab = (label or "").lower()
    if "usb" in lab:
        return "usb"
    if "lsb" in lab:
        return "lsb"
    if "cw" in lab or "morse" in lab:
        return "cw"
    if "am " in lab or 0.5 <= center_mhz <= 1.71:
        return "am"
    if "fm " in lab and 87.5 <= center_mhz <= 108.0:
        return "wfm"
    if 87.5 <= center_mhz <= 108.0:
        return "wfm"
    return "nfm"   # narrowband default; digital signals will sound like hash


def _fm_discriminator(x: np.ndarray) -> np.ndarray:
    """Instantaneous frequency of complex IQ via the phase of consecutive-sample
    products. Shared by demodulate()'s FM path and the POCSAG decoder."""
    return np.angle(x[1:] * np.conj(x[:-1])).astype(np.float64)


def _decimate(x: np.ndarray, factor: int) -> np.ndarray:
    """Boxcar (moving-average) anti-alias + downsample. Works for real/complex."""
    factor = max(1, int(factor))
    if factor == 1:
        return x
    n = (len(x) // factor) * factor
    if n == 0:
        return x
    return x[:n].reshape(-1, factor).mean(axis=1)


def _lowpass(x: np.ndarray, fs: float, cutoff: float) -> np.ndarray:
    """Boxcar (moving-average) low-pass that keeps the sample rate. The first
    null of an L-tap boxcar sits at fs/L, so size the window to ~fs/cutoff.
    Works for real or complex input (used for SSB/CW channel filtering)."""
    taps = max(1, int(round(fs / max(cutoff, 1.0))))
    if taps <= 1:
        return x
    kernel = np.ones(taps, dtype=np.float64) / taps
    return np.convolve(x, kernel, mode="same")


def demodulate(iq, fs: float, mode: str = "nfm", offset_hz: float = 0.0) -> tuple[np.ndarray, int]:
    """IQ -> (int16 PCM mono, sample_rate).
    mode: 'wfm' | 'nfm' | 'am' | 'usb' | 'lsb' | 'cw'.
    offset_hz: a movable demod tuner WITHIN the captured band — shifts the signal
    at (center + offset_hz) down to baseband so the per-mode channel filter below
    isolates THAT one signal, not the LO-spike-corrupted band center. 0 = center."""
    iq = np.asarray(iq, dtype=np.complex64)
    if iq.size < 16:
        raise ValueError("capture too short to demodulate")
    mode = (mode or "nfm").lower()
    if offset_hz:
        t = np.arange(iq.size, dtype=np.float64) / fs
        iq = (iq * np.exp(-1j * 2 * np.pi * offset_hz * t)).astype(np.complex64)

    if mode == "am":
        audio = np.abs(iq).astype(np.float64)
        arate = fs
    elif mode in ("usb", "lsb"):
        # IQ is complex baseband on the tuned carrier; one sideband sits above
        # (USB) and one below (LSB) it. Shift the wanted sideband down so its
        # low edge lands at DC, low-pass to the ~3 kHz voice passband, then take
        # the real part — the real signal contains both sidebands but the unwanted
        # mirror has been filtered away, leaving only the selected one as audio.
        t = np.arange(iq.size, dtype=np.float64) / fs
        shift = SSB_BW if mode == "usb" else -SSB_BW
        x = iq * np.exp(-1j * 2 * np.pi * shift * t).astype(np.complex64)
        x = _lowpass(x, fs, SSB_BW)
        audio = np.real(x).astype(np.float64)
        arate = fs
    elif mode == "cw":
        # Narrow-filter the carrier, then beat it against a BFO offset so a
        # steady (otherwise inaudible) carrier near DC becomes an audible tone
        # at CW_BFO Hz. Take the real part of the down-mixed, filtered signal.
        x = _lowpass(iq, fs, CW_BW)
        t = np.arange(x.size, dtype=np.float64) / fs
        x = x * np.exp(1j * 2 * np.pi * CW_BFO * t).astype(np.complex64)
        audio = np.real(x).astype(np.float64)
        arate = fs
    else:
        # complex channel-decimate toward the channel bandwidth (rejects
        # out-of-channel noise) before the FM discriminator.
        chan_bw = 16_000.0 if mode == "nfm" else 150_000.0
        dec = max(1, int(fs / (chan_bw * 2.0)))
        x = _decimate(iq, dec)
        arate = fs / dec
        if x.size < 4:
            raise ValueError("capture too short for this mode")
        audio = _fm_discriminator(x)                   # FM discriminator

    dec2 = max(1, int(round(arate / AUDIO_RATE)))
    audio = _decimate(audio, dec2)
    arate = arate / dec2

    audio = audio - np.mean(audio)                 # DC block
    peak = float(np.max(np.abs(audio))) or 1.0
    pcm = np.clip(audio / peak * 0.9, -1.0, 1.0)
    pcm16 = (pcm * 32767.0).astype("<i2")
    return pcm16, int(round(arate))


def wav_bytes(pcm16: np.ndarray, rate: int) -> bytes:
    """Minimal mono 16-bit PCM WAV container."""
    data = np.asarray(pcm16, dtype="<i2").tobytes()
    n = len(data)
    return (b"RIFF" + struct.pack("<I", 36 + n) + b"WAVE"
            + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
            + b"data" + struct.pack("<I", n) + data)


def wav_stream_header(rate: int) -> bytes:
    """WAV header for an OPEN-ENDED stream: declare a max-size data chunk so the
    browser keeps playing PCM as it arrives, until the connection is closed."""
    big = 0xFFFFFFFF
    return (b"RIFF" + struct.pack("<I", big) + b"WAVE"
            + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
            + b"data" + struct.pack("<I", big - 36))


# Magic preamble for the raw-PCM stream consumed by the Web Audio client. It is a
# single newline-terminated JSON line followed by an unbounded run of mono
# little-endian int16 PCM samples. Unlike the WAV stream this carries NO data-size
# lie, so the browser never treats the stream as a finite, seekable file — the
# client parses the header, then schedules every PCM frame it receives against the
# AudioContext clock (see app.js startListen). Keeping a magic + version lets the
# next agent evolve the framing without silently mis-decoding old clients.
PCM_STREAM_MAGIC = b"SHLDPCM1"


def pcm_stream_header(rate: int, mode: str = "") -> bytes:
    """One-line self-describing preamble for /api/listen_pcm.

    Format: PCM_STREAM_MAGIC + b"<json>\\n", where json declares the audio
    sample rate, sample format, channel count, and demod mode. Everything after
    the newline is raw mono '<i2' PCM."""
    import json as _json
    meta = {"rate": int(rate), "format": "s16le", "channels": 1, "mode": mode}
    return PCM_STREAM_MAGIC + (_json.dumps(meta, separators=(",", ":")) + "\n").encode()
