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


def peak_hz(audio, rate):
    spec = np.abs(np.fft.rfft(audio.astype(float) * np.hanning(len(audio))))
    freqs = np.fft.rfftfreq(len(audio), 1.0 / rate)
    return freqs[int(np.argmax(spec))]


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


if __name__ == "__main__":
    unittest.main()
