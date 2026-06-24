"""Audio demodulation of Pluto IQ — the 'listen' layer (DEMOD_LISTEN_DESIGN.md).

Tune to a frequency, capture IQ, and turn it into something you can HEAR so an
operator can tell an analog voice bug from telemetry from digital hash. Modes:
WFM (broadcast), NFM (narrowband voice / land-mobile / classic audio bug), AM.

Receive-only, like the rest of the app. Pure numpy DSP (no scipy) — unit tested
in tests/test_demod.py. Audio quality is "field intelligible", not hi-fi: a
crude boxcar channel filter + FM/AM discriminator + decimation to ~24 kHz WAV.
Digital signals (cellular, Wi-Fi) correctly come out as hash — that's the tell.
"""

import struct

import numpy as np

AUDIO_RATE = 24000


def pick_mode(center_mhz: float, label: str = "") -> str:
    """Default demod mode by band (operator can override in the UI)."""
    lab = (label or "").lower()
    if "am " in lab or 0.5 <= center_mhz <= 1.71:
        return "am"
    if "fm " in lab and 87.5 <= center_mhz <= 108.0:
        return "wfm"
    if 87.5 <= center_mhz <= 108.0:
        return "wfm"
    return "nfm"   # narrowband default; digital signals will sound like hash


def _decimate(x: np.ndarray, factor: int) -> np.ndarray:
    """Boxcar (moving-average) anti-alias + downsample. Works for real/complex."""
    factor = max(1, int(factor))
    if factor == 1:
        return x
    n = (len(x) // factor) * factor
    if n == 0:
        return x
    return x[:n].reshape(-1, factor).mean(axis=1)


def demodulate(iq, fs: float, mode: str = "nfm") -> tuple[np.ndarray, int]:
    """IQ -> (int16 PCM mono, sample_rate). mode: 'wfm' | 'nfm' | 'am'."""
    iq = np.asarray(iq, dtype=np.complex64)
    if iq.size < 16:
        raise ValueError("capture too short to demodulate")
    mode = (mode or "nfm").lower()

    if mode == "am":
        audio = np.abs(iq).astype(np.float64)
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
        audio = np.angle(x[1:] * np.conj(x[:-1])).astype(np.float64)  # FM discriminator

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
