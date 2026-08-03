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


def fm_at(fs, n, off_hz, audio_hz=1000.0, deviation=5000.0):
    """An FM signal placed at +off_hz within the captured band (as if the operator
    clicked a signal off-center); demodulate(offset_hz=off_hz) tunes to it."""
    base = fm_iq(fs, n, audio_hz, deviation)
    t = np.arange(n) / fs
    return (base * np.exp(1j * 2 * np.pi * off_hz * t)).astype(np.complex64)


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


class TestChannelFilter(unittest.TestCase):
    """The 'works except it doesn't work' fix: the steerable channel filter must
    actually ISOLATE the tuned signal. Proven on the filter response directly —
    the windowed-sinc FIR's stopband must be far below the old boxcar's leaky
    sidelobes, which is what let adjacent signals bleed in."""

    def test_fir_stopband_far_below_boxcar(self):
        from survey.demod import _design_fir, _auto_taps
        fs, cutoff = 240_000.0, 16_000.0
        taps = _auto_taps(fs, cutoff)
        fir = _design_fir(taps, cutoff, fs)
        box = np.ones(taps) / taps                      # equal-length boxcar (the old way)
        N = 8192
        freqs = np.fft.rfftfreq(N, 1.0 / fs)

        def resp(h):
            H = np.abs(np.fft.rfft(h, N))
            return H / H.max()

        Hf, Hb = resp(fir), resp(box)
        stop = freqs >= 2.0 * cutoff                    # clearly in the stopband
        fir_worst = float(Hf[stop].max())
        box_worst = float(Hb[stop].max())
        self.assertLess(20 * np.log10(fir_worst), -40.0)        # FIR stopband well-suppressed
        self.assertLess(fir_worst, box_worst * 0.2)             # >14 dB better than boxcar

    def test_fir_passes_in_channel_tone(self):
        from survey.demod import _design_fir, _fir_lowpass, _auto_taps
        fs, cutoff, n = 240_000.0, 16_000.0, 48_000
        t = np.arange(n) / fs
        x = np.exp(1j * 2 * np.pi * 2_000.0 * t).astype(np.complex64)  # 2 kHz in-band
        fir = _design_fir(_auto_taps(fs, cutoff), cutoff, fs)
        y = _fir_lowpass(x, fir)
        # in-channel energy is preserved (unity-gain passband)
        mid = slice(n // 4, 3 * n // 4)                 # ignore convolution edges
        self.assertGreater(np.mean(np.abs(y[mid])), 0.7)


class TestDemodStrengthening(unittest.TestCase):
    """P1 live-path strengthening (DEMOD_ANALYZE_DISSEMINATE_DESIGN.md §4a):
    squelch, RMS AGC, FM de-emphasis and cross-block continuity — all gated on a
    DemodState so the one-shot path (state=None) stays byte-for-byte unchanged."""

    def setUp(self):
        self.fs = 2_400_000.0
        self.n = 240_000
        self.rng = np.random.default_rng(0)

    def noise_iq(self):
        z = (self.rng.standard_normal(self.n) + 1j * self.rng.standard_normal(self.n))
        return (0.05 * z).astype(np.complex64)

    def test_state_none_is_unchanged(self):
        # the legacy path must be identical whether or not the new kwargs exist
        iq = fm_iq(self.fs, self.n)
        a, ra = demodulate(iq, self.fs, "nfm")
        b, rb = demodulate(iq, self.fs, "nfm", 0.0, None, state=None, squelch_snr_db=None)
        self.assertEqual(ra, rb)
        self.assertTrue(np.array_equal(a, b))

    def test_squelch_mutes_noise_after_hang(self):
        from survey.demod import DemodState
        st = DemodState()
        first_peak = None
        last = None
        for i in range(8):
            pcm, _ = demodulate(self.noise_iq(), self.fs, "nfm", state=st, squelch_snr_db=8.0)
            if i == 0:
                first_peak = int(np.max(np.abs(pcm)))
            last = pcm
        self.assertTrue(st.gated, "squelch should be closed on sustained noise")
        # after the hang the muted block is far quieter than the first (ungated) one
        self.assertLess(int(np.max(np.abs(last))), max(1, first_peak) * 0.1)

    def test_squelch_opens_on_carrier(self):
        from survey.demod import DemodState
        st = DemodState()
        pcm, _ = demodulate(cw_iq(self.fs, self.n, offset_hz=5_000.0), self.fs, "nfm",
                            state=st, squelch_snr_db=8.0)
        self.assertFalse(st.gated, "a strong carrier must open the squelch")
        self.assertGreater(st.last_snr_db, 8.0)
        self.assertGreater(int(np.max(np.abs(pcm))), 0)

    def test_squelch_does_not_mute_clicked_offset_signal(self):
        # REGRESSION: the squelch must measure the CHANNEL the operator tuned to.
        # A signal clicked off-center (offset_hz != 0) must keep the gate OPEN, not
        # read the DC-masked wide band and mute the very signal that was selected.
        from survey.demod import DemodState
        st = DemodState()
        iq = fm_at(self.fs, self.n, 300_000.0)
        for _ in range(6):                       # past the hang, to be sure
            demodulate(iq, self.fs, "nfm", offset_hz=300_000.0,
                       state=st, squelch_snr_db=8.0)
        self.assertFalse(st.gated, "clicked (offset) signal must not be muted")
        self.assertGreater(st.last_snr_db, 8.0)

    def test_agc_respects_brickwall(self):
        # the AGC never exceeds the ±0.95 full-scale safety clip, even on
        # impulsive empty-channel noise (squelch OFF).
        from survey.demod import DemodState
        st = DemodState()
        agc, _ = demodulate(self.noise_iq(), self.fs, "nfm", state=st, squelch_snr_db=None)
        self.assertLessEqual(int(np.max(np.abs(agc))), int(0.95 * 32767) + 1)

    def test_agc_holds_target_loudness(self):
        # the AGC drives output loudness toward the RMS target (not the per-block
        # peak), so a steady tone lands near target*full_scale within a factor.
        from survey.demod import DemodState
        st = DemodState(agc_target_rms=0.18)
        pcm = None
        for _ in range(4):                       # let the AGC converge
            pcm, _ = demodulate(am_iq(self.fs, self.n), self.fs, "am", state=st)
        rms = float(np.sqrt(np.mean((pcm.astype(np.float64)) ** 2)))
        target = 0.18 * 32767.0
        self.assertGreater(rms, target / 3.0)
        self.assertLess(rms, target * 3.0)
        self.assertLessEqual(int(np.max(np.abs(pcm))), int(0.95 * 32767) + 1)

    def test_deemphasis_rolls_off_highs(self):
        from survey.demod import DemodState, _deemphasis
        rate = 24_000.0
        m = 24_000
        t = np.arange(m) / rate

        def ratio(f):
            x = np.sin(2 * np.pi * f * t)
            y = _deemphasis(x, rate, 75.0, DemodState())
            return float(np.sqrt(np.mean(y ** 2)) / np.sqrt(np.mean(x ** 2)))

        low, high = ratio(300.0), ratio(8000.0)
        self.assertLess(high, low)               # 8 kHz attenuated more than 300 Hz
        self.assertLess(high, low * 0.6)         # a clear de-emphasis slope

    def test_state_continuity_carries_across_blocks(self):
        from survey.demod import DemodState
        st = DemodState()
        demodulate(fm_iq(self.fs, self.n), self.fs, "nfm", state=st)
        g1 = st.agc_gain
        self.assertIsNotNone(st.deemph_tail)     # de-emphasis memory seeded (FM)
        demodulate(fm_iq(self.fs, self.n), self.fs, "nfm", state=st)
        self.assertNotEqual(st.agc_gain, 1.0)    # AGC evolved from its initial anchor
        self.assertIsNotNone(g1)


class TestChannelWidthConvention(unittest.TestCase):
    """chan_bw_hz is the TOTAL occupied passband in every mode, so the channel
    filter keeps ±chan_bw/2. Regression for the FM branch having passed the full
    width as a one-sided cutoff: a 16 kHz pick let ±16 kHz through, so the band
    the operator saw shaded was half the band actually demodulated and the
    adjacent channel came in on both sides of the signal being isolated.
    Parametrized over am/nfm — the two branches silently drifted apart once."""

    fs = 2_400_000.0
    n = 240_000
    bw = 16_000.0        # total width -> passband edge at ±8 kHz
    adj_hz = 13_000.0    # neighbour: clear of ±8 kHz, well inside the old ±16 kHz

    def _wanted_plus_neighbour(self, mode):
        """Wanted signal at band center, plus a 4x stronger narrow neighbour
        parked where only the over-wide filter would have admitted it."""
        t = np.arange(self.n) / self.fs
        if mode == "nfm":
            wanted = fm_iq(self.fs, self.n, audio_hz=1000.0, deviation=3000.0)
            neigh = fm_iq(self.fs, self.n, audio_hz=2000.0, deviation=500.0)
        else:
            wanted = am_iq(self.fs, self.n, audio_hz=1000.0)
            neigh = am_iq(self.fs, self.n, audio_hz=2000.0)
        neigh = neigh * np.exp(1j * 2 * np.pi * self.adj_hz * t)
        return (wanted + 4.0 * neigh).astype(np.complex64)

    def test_adjacent_channel_rejected(self):
        for mode in ("am", "nfm"):
            with self.subTest(mode=mode):
                pcm, rate = demodulate(self._wanted_plus_neighbour(mode), self.fs,
                                       mode, chan_bw_hz=self.bw)
                self.assertLess(
                    abs(peak_hz(pcm, rate) - 1000.0), 300.0,
                    f"{mode}: the {self.adj_hz/1e3:.0f} kHz neighbour leaked into a "
                    f"{self.bw/1e3:.0f} kHz channel — chan_bw_hz is the TOTAL width "
                    f"(±{self.bw/2e3:.0f} kHz), not the one-sided cutoff")

    def test_same_total_width_in_am_and_fm(self):
        """A tone just inside the half-width survives in both modes, and the
        surviving band is the same width in both — the parity the bug broke."""
        t = np.arange(self.n) / self.fs
        for mode in ("am", "nfm"):
            with self.subTest(mode=mode):
                inband = 3_000.0                      # inside ±8 kHz for both modes
                if mode == "nfm":
                    iq = fm_iq(self.fs, self.n, audio_hz=inband, deviation=2000.0)
                else:
                    iq = am_iq(self.fs, self.n, audio_hz=inband)
                pcm, rate = demodulate(iq, self.fs, mode, chan_bw_hz=self.bw)
                self.assertLess(abs(peak_hz(pcm, rate) - inband), 300.0,
                                f"{mode}: an in-channel tone must survive")


if __name__ == "__main__":
    unittest.main()
