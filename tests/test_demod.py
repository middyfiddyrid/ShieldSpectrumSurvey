"""Unit tests for audio demodulation — synthetic FM/AM tone recovery + WAV."""

import unittest

import numpy as np

from survey.demod import demodulate, pick_mode, wav_bytes


def fm_iq(fs, n, audio_hz=1000.0, deviation=5000.0):
    t = np.arange(n) / fs
    msg = np.sin(2 * np.pi * audio_hz * t)
    phase = 2 * np.pi * np.cumsum(deviation * msg) / fs
    return np.exp(1j * phase).astype(np.complex64)


def am_iq(fs, n, audio_hz=1000.0):
    t = np.arange(n) / fs
    return ((1.0 + 0.5 * np.sin(2 * np.pi * audio_hz * t))).astype(np.complex64)


def ssb_iq(fs, n, audio_hz=1500.0, upper=True):
    """Single-sideband IQ of a pure audio tone, as complex baseband on the
    tuned carrier. USB puts the tone at +audio_hz, LSB at -audio_hz."""
    t = np.arange(n) / fs
    sign = 1.0 if upper else -1.0
    return np.exp(1j * 2 * np.pi * sign * audio_hz * t).astype(np.complex64)


def cw_iq(fs, n, offset_hz=120.0):
    """A pure carrier at a small offset from the tuned center (a steady
    unmodulated key-down CW signal)."""
    t = np.arange(n) / fs
    return np.exp(1j * 2 * np.pi * offset_hz * t).astype(np.complex64)


def peak_hz(audio, rate):
    spec = np.abs(np.fft.rfft(audio.astype(float) * np.hanning(len(audio))))
    freqs = np.fft.rfftfreq(len(audio), 1.0 / rate)
    return freqs[int(np.argmax(spec))]


def mag_at_hz(audio, rate, target_hz, halfwidth_hz=150.0):
    """Spectral magnitude in a narrow band around target_hz (Hann-windowed)."""
    spec = np.abs(np.fft.rfft(audio.astype(float) * np.hanning(len(audio))))
    freqs = np.fft.rfftfreq(len(audio), 1.0 / rate)
    band = np.abs(freqs - target_hz) <= halfwidth_hz
    return float(spec[band].max()) if band.any() else 0.0


class TestDemod(unittest.TestCase):
    def setUp(self):
        self.fs = 2_400_000.0
        self.n = 240_000

    def test_nfm_recovers_tone(self):
        pcm, rate = demodulate(fm_iq(self.fs, self.n), self.fs, "nfm")
        self.assertEqual(pcm.dtype, np.dtype("<i2"))
        self.assertGreater(rate, 8000)
        self.assertLess(abs(peak_hz(pcm, rate) - 1000.0), 200.0)

    def test_wfm_recovers_tone(self):
        pcm, rate = demodulate(fm_iq(self.fs, self.n, deviation=50000.0), self.fs, "wfm")
        self.assertLess(abs(peak_hz(pcm, rate) - 1000.0), 250.0)

    def test_am_recovers_tone(self):
        pcm, rate = demodulate(am_iq(self.fs, self.n), self.fs, "am")
        self.assertLess(abs(peak_hz(pcm, rate) - 1000.0), 200.0)

    def test_usb_recovers_tone(self):
        pcm, rate = demodulate(ssb_iq(self.fs, self.n, 1500.0, upper=True), self.fs, "usb")
        self.assertEqual(pcm.dtype, np.dtype("<i2"))
        self.assertGreater(rate, 8000)
        self.assertLess(abs(peak_hz(pcm, rate) - 1500.0), 200.0)

    def test_lsb_recovers_tone(self):
        pcm, rate = demodulate(ssb_iq(self.fs, self.n, 1500.0, upper=False), self.fs, "lsb")
        self.assertLess(abs(peak_hz(pcm, rate) - 1500.0), 200.0)

    def test_ssb_sideband_selectivity(self):
        # A USB-only signal demodulated as LSB shifts the tone OUT of the audio
        # passband, so its energy AT THE TONE FREQUENCY is far weaker than when
        # demodulated with the correct (USB) mode. (Compare in-band spectral
        # magnitude, not total power — the output is peak-normalized.)
        iq = ssb_iq(self.fs, self.n, 1500.0, upper=True)
        right, rr = demodulate(iq, self.fs, "usb")
        wrong, wr = demodulate(iq, self.fs, "lsb")
        right_mag = mag_at_hz(right, rr, 1500.0)
        wrong_mag = mag_at_hz(wrong, wr, 1500.0)
        # correct sideband recovers the tone; opposite sideband rejects it (>6x).
        self.assertGreater(right_mag, wrong_mag * 6.0)

    def test_cw_produces_beat_tone(self):
        from survey.demod import CW_BFO
        pcm, rate = demodulate(cw_iq(self.fs, self.n, offset_hz=120.0), self.fs, "cw")
        self.assertEqual(pcm.dtype, np.dtype("<i2"))
        # a steady carrier should come out as an audible tone near the BFO beat
        # frequency (CW_BFO + the small carrier offset).
        self.assertLess(abs(peak_hz(pcm, rate) - (CW_BFO + 120.0)), 150.0)

    def test_too_short_raises(self):
        with self.assertRaises(ValueError):
            demodulate(np.zeros(4, dtype=np.complex64), self.fs, "nfm")

    def test_wav_header(self):
        pcm, rate = demodulate(fm_iq(self.fs, self.n), self.fs, "nfm")
        wav = wav_bytes(pcm, rate)
        self.assertEqual(wav[:4], b"RIFF")
        self.assertEqual(wav[8:12], b"WAVE")
        self.assertEqual(len(wav), 44 + pcm.size * 2)

    def test_pick_mode(self):
        self.assertEqual(pick_mode(98.5, "FM 98.5"), "wfm")
        self.assertEqual(pick_mode(1.01, "AM 1010 kHz"), "am")
        self.assertEqual(pick_mode(462.5, "Industrial/Business"), "nfm")
        self.assertEqual(pick_mode(14.2, "USB voice net"), "usb")
        self.assertEqual(pick_mode(7.05, "LSB"), "lsb")
        self.assertEqual(pick_mode(7.03, "CW / Morse"), "cw")


if __name__ == "__main__":
    unittest.main()
