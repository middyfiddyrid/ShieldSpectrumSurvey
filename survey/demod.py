"""Audio demodulation of Pluto IQ — the 'listen' layer (DEMOD_LISTEN_DESIGN.md).

Tune to a frequency, capture IQ, and turn it into something you can HEAR so an
operator can tell an analog voice bug from telemetry from digital hash. Modes:
WFM (broadcast), NFM (narrowband voice / land-mobile / classic audio bug), AM,
SSB (USB/LSB — single-sideband voice), CW (Morse / unmodulated carriers as a beat note).

Receive-only, like the rest of the app. Pure numpy DSP (no scipy) — unit tested
in tests/test_demod.py. Audio quality is "field intelligible", not hi-fi: a
windowed-sinc channel filter + FM/AM discriminator + decimation to ~24 kHz WAV.
Digital signals (cellular, Wi-Fi) correctly come out as hash — that's the tell.

Two output paths (DEMOD_ANALYZE_DISSEMINATE_DESIGN.md §4a):
  - state=None  : one-shot callers (inspect / analyze / record) — the legacy
                  per-block peak-normalize, unchanged byte-for-byte.
  - state=Demod : the LIVE stream — SNR-keyed squelch, RMS-target AGC, FM
    State        de-emphasis and FIR anti-alias, all carrying per-stream state so
                  nothing clicks at the 0.15 s block seam. This is the fix for
                  "the demod feels weak": an empty channel is muted (or held at a
                  bounded level) instead of a per-block peak-normalize amplifying
                  pure noise to full scale.
"""

import struct

import numpy as np

AUDIO_RATE = 24000
SSB_BW = 3000.0      # SSB audio passband (~3 kHz, communications voice)
CW_BFO = 700.0       # CW beat-frequency oscillator offset -> audible tone (Hz)
CW_BW = 1200.0       # narrow filter around the CW carrier before the beat
NFM_CHAN_BW = 16_000.0    # narrowband-FM channel filter cutoff (voice / bug / LMR)
WFM_CHAN_BW = 150_000.0   # broadcast-FM channel filter cutoff
FIR_MAX_TAPS = 401        # cap on the windowed-sinc channel filter length (compute bound)


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


def _auto_taps(fs: float, cutoff: float, beta: float = 4.0,
               max_taps: int = FIR_MAX_TAPS) -> int:
    """Pick an odd FIR length for a `cutoff` filter at `fs`. More taps = sharper
    transition; capped so a narrow channel at a high capture rate stays cheap."""
    taps = int(round(beta * fs / max(cutoff, 1.0)))
    taps = max(31, min(int(max_taps), taps))
    return taps | 1   # odd -> symmetric, integer group delay


def _design_fir(numtaps: int, cutoff: float, fs: float) -> np.ndarray:
    """Windowed-sinc (Hamming) low-pass FIR, unity DC gain. ~-53 dB stopband —
    vastly better out-of-channel rejection than a boxcar's ~-13 dB sidelobes, so
    a clicked-on signal is actually ISOLATED instead of swamped by its neighbors."""
    numtaps = max(3, int(numtaps)) | 1
    fc = min(max(cutoff / fs, 1e-4), 0.49)          # normalized cutoff (cycles/sample)
    n = np.arange(numtaps) - (numtaps - 1) / 2.0
    h = 2.0 * fc * np.sinc(2.0 * fc * n) * np.hamming(numtaps)
    s = float(np.sum(h))
    if s:
        h = h / s
    return h.astype(np.float64)


def _fir_lowpass(x: np.ndarray, fir: np.ndarray) -> np.ndarray:
    """Linear-phase FIR low-pass (real or complex input)."""
    return np.convolve(x, fir, mode="same")


def _fir_decimate(x: np.ndarray, fir: np.ndarray, factor: int) -> np.ndarray:
    """FIR anti-alias low-pass then downsample by `factor` (real or complex)."""
    factor = max(1, int(factor))
    y = np.convolve(x, fir, mode="same")
    return y if factor == 1 else y[::factor]


def _lowpass(x: np.ndarray, fs: float, cutoff: float) -> np.ndarray:
    """Windowed-sinc FIR low-pass that keeps the sample rate (used for SSB/CW
    channel filtering). Replaces the old boxcar: same role, far cleaner stopband
    so adjacent signals don't bleed into the passband."""
    fir = _design_fir(_auto_taps(fs, cutoff), cutoff, fs)
    return _fir_lowpass(x, fir)


# ---------------------------------------------------------------------------
# Live-stream state + strengthening DSP (only used when demodulate(state=...))
# ---------------------------------------------------------------------------

class DemodState:
    """Per-stream demod state so squelch hysteresis, the AGC gain, and the FM
    de-emphasis IIR carry across the 0.15 s audio blocks instead of resetting
    (which would click at every block seam). Construct ONE per /api/listen_pcm
    stream; the AGC/de-emphasis tunables are seeded from config once here.

    Also the telemetry surface: last_snr_db / gated / agc_gain feed the
    "why is it weak?" front-end diagnostic (server publishes them into
    listen_spectrum.demod)."""

    def __init__(self, *, agc_target_rms: float = 0.18, agc_attack_ms: float = 8.0,
                 agc_release_ms: float = 300.0, agc_max_gain: float = 40.0,
                 squelch_hang_blocks: int = 3, deemphasis_us: float = 75.0):
        # tunables (seeded from config; constant for the stream's life)
        self.agc_target_rms = float(agc_target_rms)
        self.agc_attack_ms = float(agc_attack_ms)
        self.agc_release_ms = float(agc_release_ms)
        self.agc_max_gain = float(agc_max_gain)
        self.squelch_hang_blocks = int(squelch_hang_blocks)
        self.deemphasis_us = float(deemphasis_us)
        # evolving state (carried block to block)
        self.agc_gain = 1.0          # last block's output gain (ramp anchor)
        self.deemph_tail = None      # last K-1 input samples for the de-emphasis FIR
        self.sq_open = True          # squelch starts OPEN (never hide a signal on tune-in)
        self.sq_hang = 0             # consecutive below-threshold blocks
        # telemetry (read by the server for the diagnostic line)
        self.last_snr_db = None
        self.last_out_gain = 1.0
        self.gated = False

    def reset_continuity(self):
        """On a mid-stream retune / width change the passband shifts, so old
        de-emphasis memory and AGC anchor no longer apply — snap them so the
        change doesn't click. Keeps tunables + squelch decision."""
        self.deemph_tail = None
        self.agc_gain = min(self.agc_gain, 1.0)


def _periodogram_snr_db(iq: np.ndarray, nperseg: int = 1024,
                        max_segs: int = 64, mask_dc: bool = True) -> float:
    """Cheap in-band SNR for the squelch gate + S-meter: peak-over-median of an
    AVERAGED (Welch) periodogram. Averaging is essential — a SINGLE periodogram of
    white noise is chi-square-spiky (peak/median ~10 dB), which would read as
    'signal present' on an empty channel and defeat the squelch; averaging
    flattens noise to ~1-2 dB while a real carrier still spikes.

    mask_dc=True masks ±guard bins around DC (the band-center Pluto LO spike) — use
    on a RAW wide capture. mask_dc=False keeps DC — use on a CHANNEL-ISOLATED
    baseband where the tuned signal legitimately sits at DC (masking it there would
    hide the very signal we are gating on)."""
    iq = np.asarray(iq)
    npg = int(min(nperseg, iq.size))
    if npg < 32:
        return 0.0
    segs = int(min(max(1, iq.size // npg), max_segs))
    w = np.hanning(npg)
    frames = iq[:segs * npg].reshape(segs, npg) * w
    spec = np.fft.fftshift(np.fft.fft(frames, axis=1), axes=1)
    p = np.mean(np.abs(spec) ** 2, axis=0)       # Welch average over segments
    if mask_dc:
        c = p.size // 2
        guard = max(1, p.size // 64)             # mask ± a small band around DC/LO
        lo, hi = max(0, c - guard), min(p.size, c + guard + 1)
        p = np.delete(p, np.s_[lo:hi])
    if p.size == 0:
        return 0.0
    med = float(np.median(p))
    peak = float(np.max(p))
    if med <= 0 or peak <= 0:
        return 0.0
    return float(10.0 * np.log10(peak / med))


def _deemphasis(audio: np.ndarray, rate: float, tau_us: float,
                state: "DemodState") -> np.ndarray:
    """FM de-emphasis: a 50/75 µs one-pole low-pass that undoes the transmitter's
    pre-emphasis so FM audio isn't bright/hissy. Implemented as a SHORT truncated-
    exponential FIR (unity DC gain) — vectorized and numerically stable (the
    cumulative-division vectorization of a one-pole underflows here), with
    overlap-carry via state.deemph_tail so it doesn't click at the block seam."""
    tau = max(1e-9, float(tau_us) * 1e-6)
    dt = 1.0 / float(rate)
    alpha = dt / (tau + dt)
    a = 1.0 - alpha
    if a <= 0.0:
        return audio
    k = int(np.clip(int(np.ceil(np.log(1e-4) / np.log(a))), 4, 512))
    kernel = alpha * (a ** np.arange(k))
    tail = state.deemph_tail
    if tail is None or tail.size != (k - 1):
        tail = np.zeros(k - 1, dtype=np.float64)
    xin = np.concatenate([tail, audio.astype(np.float64)])
    y = np.convolve(xin, kernel)[k - 1: k - 1 + audio.size]
    state.deemph_tail = xin[-(k - 1):].copy() if (k - 1) > 0 else None
    return y


def _agc_block(audio: np.ndarray, state: "DemodState", arate: float,
               gated: bool) -> tuple[np.ndarray, float]:
    """RMS-target AGC on the live path (replaces per-block peak-normalize). Holds
    a target loudness with a fast attack / slow release, capped by agc_max_gain,
    and ramps the scalar gain from the previous block's value across THIS block so
    the level never steps at the seam. When `gated` (squelch closed) it drives the
    gain toward zero — silence — instead of amplifying noise. Brickwall-clips the
    output for safety. Returns (int16 PCM, gain_applied)."""
    audio = np.asarray(audio, dtype=np.float64)
    if audio.size == 0:
        return np.zeros(0, dtype="<i2"), state.agc_gain
    if gated:
        target = 0.0
    else:
        rms = float(np.sqrt(np.mean(audio * audio))) + 1e-9
        target = float(np.clip(state.agc_target_rms / rms, 0.0, state.agc_max_gain))
    block_s = max(1e-4, audio.size / float(arate))
    tc_ms = state.agc_attack_ms if target < state.agc_gain else state.agc_release_ms
    coef = 1.0 - np.exp(-block_s / max(1e-4, tc_ms / 1000.0))
    new_gain = state.agc_gain + coef * (target - state.agc_gain)
    ramp = np.linspace(state.agc_gain, new_gain, audio.size)
    out = np.clip(audio * ramp, -0.95, 0.95)
    state.agc_gain = new_gain
    pcm16 = (out * 32767.0).astype("<i2")
    return pcm16, new_gain


def demodulate(iq, fs: float, mode: str = "nfm", offset_hz: float = 0.0,
               chan_bw_hz: float = None, state: "DemodState" = None,
               squelch_snr_db: float = None) -> tuple[np.ndarray, int]:
    """IQ -> (int16 PCM mono, sample_rate).
    mode: 'wfm' | 'nfm' | 'am' | 'usb' | 'lsb' | 'cw'.
    offset_hz: a movable demod tuner WITHIN the captured band — shifts the signal
    at (center + offset_hz) down to baseband so the per-mode channel filter below
    isolates THAT one signal, not the LO-spike-corrupted band center. 0 = center.
    state: None -> the legacy one-shot per-block peak-normalize (callers unchanged).
    A DemodState -> the strengthened LIVE path (squelch/AGC/de-emphasis, carried).
    squelch_snr_db: if not None (and state given), mute the block when the measured
    in-band SNR falls below it (with hysteresis/hang). None -> squelch OFF."""
    iq = np.asarray(iq, dtype=np.complex64)
    if iq.size < 16:
        raise ValueError("capture too short to demodulate")
    mode = (mode or "nfm").lower()
    if offset_hz:
        t = np.arange(iq.size, dtype=np.float64) / fs
        iq = (iq * np.exp(-1j * 2 * np.pi * offset_hz * t)).astype(np.complex64)

    if mode == "am":
        if chan_bw_hz:                                 # adjustable channel width (AM audio ~bw/2)
            iq = _lowpass(iq, fs, max(2000.0, min(float(chan_bw_hz) / 2.0, fs * 0.45)))
        audio = np.abs(iq).astype(np.float64)
        chan_sig = iq                                  # channel-isolated baseband for the squelch SNR
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
        chan_sig = x
        arate = fs
    elif mode == "cw":
        # Narrow-filter the carrier, then beat it against a BFO offset so a
        # steady (otherwise inaudible) carrier near DC becomes an audible tone
        # at CW_BFO Hz. Take the real part of the down-mixed, filtered signal.
        x = _lowpass(iq, fs, CW_BW)
        t = np.arange(x.size, dtype=np.float64) / fs
        x = x * np.exp(1j * 2 * np.pi * CW_BFO * t).astype(np.complex64)
        audio = np.real(x).astype(np.float64)
        chan_sig = x
        arate = fs
    else:
        # FIR channel-filter + decimate toward the channel bandwidth before the
        # FM discriminator. The sharp stopband rejects out-of-channel signals
        # (the steerable-tuner "isolate THIS signal" requirement); the boxcar it
        # replaces leaked neighbours straight through its -13 dB sidelobes.
        default_bw = NFM_CHAN_BW if mode == "nfm" else WFM_CHAN_BW
        chan_bw = float(chan_bw_hz) if chan_bw_hz else default_bw
        chan_bw = max(2000.0, min(chan_bw, fs * 0.45))   # adjustable channel width (the on-screen band)
        dec = max(1, int(fs / (chan_bw * 2.0)))
        fir = _design_fir(_auto_taps(fs, chan_bw), chan_bw, fs)
        x = _fir_decimate(iq, fir, dec)
        arate = fs / dec
        if x.size < 4:
            raise ValueError("capture too short for this mode")
        audio = _fm_discriminator(x)                   # FM discriminator (unchanged: POCSAG shares it)
        chan_sig = x

    # in-band SNR (live path only) measured on the CHANNEL-ISOLATED baseband, NOT
    # the raw wide capture: the offset shift moved the tuned signal to DC and the
    # per-mode filter rejected out-of-channel energy, so the squelch/S-meter track
    # THIS signal — they don't mute the clicked one or open on a neighbour.
    snr_db = (_periodogram_snr_db(chan_sig, nperseg=256, mask_dc=False)
              if state is not None else None)
    dec2 = max(1, int(round(arate / AUDIO_RATE)))

    if state is None:
        # -------- legacy one-shot path: unchanged behavior (callers depend on it)
        audio = _decimate(audio, dec2)
        arate = arate / dec2
        audio = audio - np.mean(audio)                 # DC block
        peak = float(np.max(np.abs(audio))) or 1.0
        pcm = np.clip(audio / peak * 0.9, -1.0, 1.0)
        pcm16 = (pcm * 32767.0).astype("<i2")
        return pcm16, int(round(arate))

    # -------- live stream path: anti-alias -> de-emphasis -> squelch -> AGC ------
    if dec2 > 1:                                        # FIR anti-alias only when we actually decimate
        cutoff = min(AUDIO_RATE * 0.5, arate * 0.45)
        fir2 = _design_fir(_auto_taps(arate, cutoff), cutoff, arate)
        audio = _fir_decimate(audio, fir2, dec2)
    arate = arate / dec2

    audio = audio - np.mean(audio)                     # DC block
    if state.deemphasis_us and mode in ("nfm", "wfm"):
        audio = _deemphasis(audio, arate, state.deemphasis_us, state)

    gated = False
    if squelch_snr_db is not None and snr_db is not None:
        if snr_db < float(squelch_snr_db):
            state.sq_hang += 1
        else:
            state.sq_hang = 0
            state.sq_open = True
        if state.sq_hang > state.squelch_hang_blocks:
            state.sq_open = False
        gated = not state.sq_open
    state.last_snr_db = snr_db
    state.gated = gated

    pcm16, out_gain = _agc_block(audio, state, arate, gated)
    state.last_out_gain = out_gain
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
