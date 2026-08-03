"""Unit tests for the DMR frame layer.

Three layers of assertion, deliberately:

1. SPEC ANCHORS that are not self-consistency. Every FEC code is checked against
   its PUBLISHED minimum distance — Golay(20,8,8), the EMB's (16,7,6),
   Hamming(15,11,3), Hamming(13,9,3) — so a wrong generator polynomial or parity
   mask fails loudly here instead of silently mis-decoding a real signal. The ten
   sync words are checked against ETSI TS 102 361-1 Table 9.2, including the
   polarity-pair property that makes receive polarity unresolvable from the sync.
2. ROUND TRIPS through the real chain: build bursts, encode to C4FM IQ with
   dmr.encode_dmr(), decode, and assert the exact talkgroup, source radio ID,
   colour code, slot and ENCRYPTION STATE come back.
3. NEGATIVES: noise, analog FM, a POCSAG page, an unmodulated carrier and a
   random C4FM stream must produce NO messages. A DMR decoder that invents a
   talkgroup is worse than one that finds nothing.

Encrypted traffic is tested as a RESULT, not as a failure: each of the three
independent indicators (PI header, link-control privacy bit, EMB privacy
indicator) gets its own test asserting the call is reported as encrypted.

All headless — no hardware, and nothing here has seen a real off-air signal.
"""

import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from survey import c4fm, dmr                                       # noqa: E402
from survey.dmr import (SYNC_PATTERNS, DT_PI_HEADER, DT_VOICE_LC_HEADER,
                        DT_TERMINATOR_LC, DT_CSBK, FLCO_GROUP_VOICE,
                        FLCO_UNIT_VOICE, SVC_PRIVACY_BIT, SVC_EMERGENCY_BIT,
                        BURST_BITS, decode_dmr, encode_dmr, build_data_burst,
                        build_voice_burst, pack_full_lc, parse_full_lc,
                        golay_20_8_encode, golay_20_8_decode, emb_encode,
                        emb_decode, pack_emb, bptc_encode, bptc_decode,
                        hamming_15_11_encode, hamming_15_11_decode,
                        hamming_13_9_encode, hamming_13_9_decode)   # noqa: E402
from survey.pocsag import encode_pocsag                             # noqa: E402

FS = 48000.0
TG, SRC, CC = 4021, 2621102, 1


def _lc(service_options=0x00, flco=FLCO_GROUP_VOICE, dst=TG, src=SRC):
    """A 96-bit BPTC payload: the 72-bit Full LC plus 24 zeroed RS(12,9) bits
    (this build does not verify the RS — see the dmr.py docstring)."""
    return pack_full_lc(flco, 0, service_options, dst, src) + [0] * 24


def _mindist(codewords):
    return min(bin(c).count("1") for c in codewords if c)


def _decode(bursts, fs=FS, **kw):
    return decode_dmr(encode_dmr(bursts, fs, **kw), fs)


def _only_voice_call(result):
    calls = [m for m in result["messages"] if m["type"] == "voice_call"]
    assert len(calls) == 1, "expected 1 voice call, got %r" % (result["messages"],)
    return calls[0]


class TestSyncPatterns(unittest.TestCase):
    """ETSI TS 102 361-1 Table 9.2 — external anchors, not encoder output."""

    def test_the_ten_sync_words(self):
        self.assertEqual(len(SYNC_PATTERNS), 10)
        self.assertEqual(SYNC_PATTERNS["bs_voice"], 0x755FD7DF75F7)
        self.assertEqual(SYNC_PATTERNS["bs_data"], 0xDFF57D75DF5D)
        self.assertEqual(SYNC_PATTERNS["ms_voice"], 0x7F7D5DD57DFD)
        self.assertEqual(SYNC_PATTERNS["ms_data"], 0xD5D7F77FD757)
        for v in SYNC_PATTERNS.values():
            self.assertEqual(v >> 48, 0)                 # exactly 48 bits

    def test_sync_words_are_polarity_pairs(self):
        # Inverting the C4FM rails XORs a 48-bit window with 0xAAAAAAAAAAAA, and
        # each voice sync maps onto its own data sync. This is WHY decode_dmr
        # resolves polarity from FEC rather than from the sync word; if this ever
        # stops holding, that whole design decision is worth revisiting.
        mask = c4fm.dibit_invert_mask(24)
        self.assertEqual(mask, 0xAAAAAAAAAAAA)
        for voice, data in (("bs_voice", "bs_data"), ("ms_voice", "ms_data"),
                            ("direct_ts1_voice", "direct_ts1_data"),
                            ("direct_ts2_voice", "direct_ts2_data"),
                            ("ms_rc", "reserved")):
            self.assertEqual(SYNC_PATTERNS[voice] ^ mask, SYNC_PATTERNS[data],
                             "%s/%s are not a polarity pair" % (voice, data))

    def test_sync_words_are_far_apart_within_a_polarity(self):
        # They must not be confusable at SYNC_MAX_ERRS, or a data burst could be
        # read as a voice burst at the SAME polarity.
        names = list(SYNC_PATTERNS)
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                d = bin(SYNC_PATTERNS[a] ^ SYNC_PATTERNS[b]).count("1")
                self.assertGreater(d, 2 * dmr.SYNC_MAX_ERRS, "%s vs %s" % (a, b))


class TestBlockCodes(unittest.TestCase):
    def test_golay_minimum_distance_is_8(self):
        # published (20,8,8). A wrong generator polynomial changes this.
        self.assertEqual(_mindist([golay_20_8_encode(d) for d in range(256)]), 8)
        self.assertEqual(len({golay_20_8_encode(d) for d in range(256)}), 256)

    def test_golay_is_systematic_and_roundtrips(self):
        for d in (0x00, 0x01, 0xA5, 0xFF, 0x5C):
            cw = golay_20_8_encode(d)
            self.assertEqual((cw >> 12) & 0xFF, d)       # data in bits 19..12
            self.assertEqual(golay_20_8_decode(cw), (d, 0))

    def test_golay_corrects_three_errors(self):
        rng = np.random.default_rng(0)
        for _ in range(40):
            d = int(rng.integers(0, 256))
            cw = golay_20_8_encode(d)
            bits = rng.choice(20, 3, replace=False)
            bad = cw
            for b in bits:
                bad ^= 1 << int(b)
            got, ne = golay_20_8_decode(bad)
            self.assertEqual(got, d)
            self.assertEqual(ne, 3)

    def test_emb_code_minimum_distance_is_6(self):
        self.assertEqual(_mindist([emb_encode(d) for d in range(128)]), 6)

    def test_emb_roundtrip_and_two_error_correction(self):
        for cc in range(16):
            for pi in (0, 1):
                word = emb_encode(pack_emb(cc, pi, 2))
                got, ne = emb_decode(word ^ 0b101)       # 2 bit errors
                self.assertEqual(dmr.unpack_emb(got),
                                 {"colour_code": cc, "pi": pi, "lcss": 2})
                self.assertEqual(ne, 2)

    def test_emb_rejects_beyond_correction(self):
        # honesty: past its distance the code must return nothing, not snap to
        # whatever codeword happened to be nearest
        word = emb_encode(pack_emb(1, 0, 0))
        self.assertEqual(emb_decode(word ^ 0b1111111, max_err=2), (None, -1))

    def test_hamming_minimum_distances_are_3(self):
        def words(enc, k):
            out = []
            for d in range(1 << k):
                cw = enc([(d >> j) & 1 for j in range(k - 1, -1, -1)])
                v = 0
                for b in cw:
                    v = (v << 1) | b
                out.append(v)
            return out
        self.assertEqual(_mindist(words(hamming_15_11_encode, 11)), 3)
        self.assertEqual(_mindist(words(hamming_13_9_encode, 9)), 3)

    def test_hamming_corrects_one_error_at_every_position(self):
        rng = np.random.default_rng(1)
        for enc, dec, k, n in ((hamming_15_11_encode, hamming_15_11_decode, 11, 15),
                               (hamming_13_9_encode, hamming_13_9_decode, 9, 13)):
            data = rng.integers(0, 2, k).tolist()
            cw = enc(data)
            self.assertEqual(dec(cw), (cw, 0))
            for pos in range(n):
                bad = list(cw)
                bad[pos] ^= 1
                fixed, ne = dec(bad)
                self.assertEqual(ne, 1, "position %d" % pos)
                self.assertEqual(fixed, cw, "position %d" % pos)


class TestBPTC(unittest.TestCase):
    def test_interleave_is_a_permutation_and_invertible(self):
        bits = list(np.random.default_rng(2).integers(0, 2, 196))
        # the x181 index map must hit every position exactly once
        self.assertEqual(sorted((i * dmr.BPTC_INTERLEAVE_STEP) % 196
                                for i in range(196)), list(range(196)))
        self.assertEqual(dmr.bptc_deinterleave(dmr.bptc_interleave(bits)), bits)

    def test_roundtrip_is_exact(self):
        p = list(np.random.default_rng(3).integers(0, 2, 96))
        got, ok, corrected = bptc_decode(bptc_encode(p))
        self.assertTrue(ok)
        self.assertEqual(corrected, 0)
        self.assertEqual(got, p)

    def test_product_code_corrects_scattered_errors(self):
        # the point of a product code: an error a row cannot place is trivial for
        # its column, so several scattered bit errors still recover exactly
        rng = np.random.default_rng(4)
        p = list(rng.integers(0, 2, 96))
        on_air = bptc_encode(p)
        for n_err in (1, 2, 3, 5, 8):
            bad = list(on_air)
            for i in rng.choice(196, n_err, replace=False):
                bad[int(i)] ^= 1
            got, ok, _c = bptc_decode(bad)
            self.assertTrue(ok, "%d errors" % n_err)
            self.assertEqual(got, p, "%d errors" % n_err)

    def test_random_bits_do_not_validate(self):
        # The BPTC 'ok' flag is the integrity gate the burst parser trusts, so
        # noise must fail it rather than yield a plausible-looking payload.
        # Without BPTC_MAX_CORRECTIONS the iterative decoder converges onto a
        # valid codeword for ~9 % of random inputs — this is the regression test
        # for that, so do not weaken it to a "mostly" assertion.
        rng = np.random.default_rng(5)
        for _ in range(300):
            _p, ok, _c = bptc_decode(list(rng.integers(0, 2, 196)))
            self.assertFalse(ok)

    def test_the_correction_budget_is_what_rejects_noise(self):
        # pin the mechanism, not just the outcome: random vectors DO reach a
        # clean syndrome, they just need far more bit flips than a real fade
        rng = np.random.default_rng(6)
        over_budget = 0
        for _ in range(300):
            _p, ok, corrected = bptc_decode(list(rng.integers(0, 2, 196)))
            self.assertFalse(ok)
            if corrected > dmr.BPTC_MAX_CORRECTIONS:
                over_budget += 1
        self.assertGreater(over_budget, 0)


class TestFullLC(unittest.TestCase):
    def test_field_layout(self):
        lc = parse_full_lc(_lc(0x00))
        self.assertEqual(lc["flco"], FLCO_GROUP_VOICE)
        self.assertEqual(lc["talkgroup"], TG)
        self.assertEqual(lc["src"], SRC)
        self.assertIsNone(lc["target_radio"])
        self.assertFalse(lc["privacy"])
        self.assertFalse(lc["emergency"])
        self.assertFalse(lc["rs_checked"])       # honest about the missing RS(12,9)

    def test_privacy_and_emergency_bits(self):
        self.assertTrue(parse_full_lc(_lc(SVC_PRIVACY_BIT))["privacy"])
        self.assertTrue(parse_full_lc(_lc(SVC_EMERGENCY_BIT))["emergency"])
        self.assertFalse(parse_full_lc(_lc(SVC_EMERGENCY_BIT))["privacy"])

    def test_unit_to_unit_dst_is_a_radio_not_a_talkgroup(self):
        lc = parse_full_lc(_lc(0x00, flco=FLCO_UNIT_VOICE, dst=999999))
        self.assertEqual(lc["target_radio"], 999999)
        self.assertIsNone(lc["talkgroup"])

    def test_full_24_bit_address_range(self):
        lc = parse_full_lc(_lc(0x00, dst=0xFFFFFF, src=0xABCDEF))
        self.assertEqual(lc["talkgroup"], 0xFFFFFF)
        self.assertEqual(lc["src"], 0xABCDEF)


class TestBurstStructure(unittest.TestCase):
    def test_bursts_are_264_bits(self):
        self.assertEqual(len(build_data_burst("bs_data", CC, DT_CSBK, _lc())), BURST_BITS)
        self.assertEqual(len(build_voice_burst("bs_voice")), BURST_BITS)
        self.assertEqual(len(build_voice_burst(emb=(CC, 1, 0))), BURST_BITS)

    def test_slot_type_survives_the_burst_layout(self):
        # the 20-bit Golay word is split either side of the 48-bit sync
        for cc in (0, 1, 9, 15):
            for dt in (DT_PI_HEADER, DT_VOICE_LC_HEADER, DT_CSBK, 0xB):
                bits = build_data_burst("bs_data", cc, dt, _lc())
                self.assertEqual(dmr.parse_slot_type(bits), (cc, dt, 0))

    def test_emb_survives_the_burst_layout(self):
        # 8 bits before and 8 bits after the 32-bit embedded signalling
        bits = build_voice_burst(emb=(7, 1, 3))
        emb = dmr.parse_emb(bits)
        self.assertEqual((emb["colour_code"], emb["pi"], emb["lcss"]), (7, 1, 3))


class TestFullChain(unittest.TestCase):
    """Bursts -> C4FM IQ -> symbols -> bursts -> call records."""

    def _call(self, service_options=0x00, **kw):
        pi = 1 if service_options & SVC_PRIVACY_BIT else 0
        lc = _lc(service_options)
        bursts = [build_data_burst("bs_data", CC, DT_VOICE_LC_HEADER, lc),
                  build_voice_burst("bs_voice", voice_seed=1),
                  build_voice_burst(emb=(CC, pi, 0), voice_seed=2),
                  build_data_burst("bs_data", CC, DT_TERMINATOR_LC, lc)]
        return _decode(bursts, **kw)

    def test_clear_call_talkgroup_source_and_colour_code(self):
        m = _only_voice_call(self._call())
        self.assertEqual(m["talkgroup"], TG)
        self.assertEqual(m["source"], SRC)
        self.assertEqual(m["colour_code"], CC)
        self.assertIs(m["encrypted"], False)
        self.assertIn("clear", m["text"])

    def test_recovers_through_decimation_from_a_real_capture_rate(self):
        m = _only_voice_call(self._call(fs=2_400_000.0))
        self.assertEqual((m["talkgroup"], m["source"], m["colour_code"]), (TG, SRC, CC))

    def test_recovers_with_inverted_polarity(self):
        # the sync word cannot resolve polarity (see TestSyncPatterns), so this
        # exercises the FEC-driven polarity decision
        r = self._call(invert=True)
        self.assertTrue(r["stats"]["inverted"])
        self.assertTrue(r["stats"]["polarity_resolved"])
        m = _only_voice_call(r)
        self.assertEqual((m["talkgroup"], m["source"]), (TG, SRC))

    def test_recovers_under_noise(self):
        m = _only_voice_call(self._call(noise=0.10, seed=7))
        self.assertEqual((m["talkgroup"], m["source"], m["colour_code"]), (TG, SRC, CC))

    def test_every_colour_code_roundtrips(self):
        for cc in (0, 1, 5, 10, 15):
            bursts = [build_data_burst("bs_data", cc, DT_VOICE_LC_HEADER, _lc()),
                      build_data_burst("bs_data", cc, DT_TERMINATOR_LC, _lc())]
            m = _only_voice_call(_decode(bursts))
            self.assertEqual(m["colour_code"], cc)

    def test_unit_to_unit_call_and_emergency_flag(self):
        bursts = [build_data_burst("ms_data", 7, DT_VOICE_LC_HEADER,
                                   _lc(SVC_EMERGENCY_BIT, flco=FLCO_UNIT_VOICE,
                                       dst=999999))]
        m = _only_voice_call(_decode(bursts))
        self.assertIsNone(m["talkgroup"])
        self.assertEqual(m["target_radio"], 999999)
        self.assertTrue(m["emergency"])
        self.assertIn("EMERGENCY", m["text"])

    def test_direct_mode_sync_names_the_slot(self):
        bursts = [build_data_burst("direct_ts2_data", 5, DT_VOICE_LC_HEADER, _lc()),
                  build_voice_burst("direct_ts2_voice", voice_seed=3)]
        r = _decode(bursts)
        first = r["bursts"][0]
        self.assertEqual(first["slot"], 2)
        self.assertEqual(first["slot_source"], "sync")

    def test_bs_slot_is_reported_as_relative_not_absolute(self):
        # without the CACH the absolute slot is unknowable; it must be labelled
        r = self._call()
        for b in r["bursts"]:
            self.assertEqual(b["slot_source"], "alternation")
        self.assertIn("relative", _only_voice_call(r)["text"])

    def test_csbk_is_reported_as_signalling_not_voice(self):
        payload = list(np.random.default_rng(8).integers(0, 2, 96))
        r = _decode([build_data_burst("bs_data", 2, DT_CSBK, payload)])
        csbks = [m for m in r["messages"] if m["type"] == "csbk"]
        self.assertEqual(len(csbks), 1)
        self.assertEqual(csbks[0]["colour_code"], 2)
        self.assertIn("not voice", csbks[0]["text"])

    def test_sync_less_voice_bursts_are_found_on_the_tdma_grid(self):
        # bursts B-F carry an EMB where the sync would be, so they can only be
        # found by stepping the 30 ms grid from a confirmed burst
        bursts = [build_data_burst("bs_data", 4, DT_VOICE_LC_HEADER, _lc()),
                  build_voice_burst("bs_voice", voice_seed=4),
                  build_voice_burst(emb=(4, 0, 1), voice_seed=5),
                  build_voice_burst(emb=(4, 0, 2), voice_seed=6),
                  build_voice_burst(emb=(4, 0, 3), voice_seed=7)]
        r = _decode(bursts)
        self.assertGreaterEqual(r["stats"]["emb_ok"], 3)
        self.assertGreaterEqual(r["stats"]["bursts"], 5)


class TestEncryptionIsAFirstClassResult(unittest.TestCase):
    """For a sweep, 'DMR, colour code 1, talkgroup 4021, ENCRYPTED' is a finding.
    Each of the three independent indicators must produce it."""

    def test_link_control_privacy_bit(self):
        bursts = [build_data_burst("bs_data", CC, DT_VOICE_LC_HEADER,
                                   _lc(SVC_PRIVACY_BIT))]
        m = _only_voice_call(_decode(bursts))
        self.assertIs(m["encrypted"], True)
        self.assertEqual(m["talkgroup"], TG)         # still fully identified
        self.assertEqual(m["source"], SRC)
        self.assertIn("ENCRYPTED", m["text"])
        self.assertIn("privacy bit", m["encryption_evidence"])

    def test_pi_header(self):
        payload = list(np.random.default_rng(9).integers(0, 2, 96))
        r = _decode([build_data_burst("bs_data", 3, DT_PI_HEADER, payload),
                     build_voice_burst("bs_voice", voice_seed=9)])
        pi = [m for m in r["messages"] if m["type"] == "pi_header"]
        self.assertEqual(len(pi), 1)
        self.assertIs(pi[0]["encrypted"], True)
        self.assertIn("ENCRYPTED", pi[0]["text"])
        self.assertTrue(pi[0]["payload_hex"])        # raw header handed up
        self.assertIn("ENCRYPTED", r["note"])

    def test_emb_privacy_indicator_without_any_header(self):
        # the hard case: the call was already up when the sweep started, so
        # there is no LC and no PI header — only the EMB says it is encrypted
        bursts = [build_data_burst("bs_data", 6, DT_CSBK,
                                   list(np.random.default_rng(10).integers(0, 2, 96))),
                  build_voice_burst("bs_voice", voice_seed=10),
                  build_voice_burst(emb=(6, 1, 0), voice_seed=11),
                  build_voice_burst(emb=(6, 1, 0), voice_seed=12)]
        r = _decode(bursts)
        act = [m for m in r["messages"] if m["type"] == "voice_activity"]
        self.assertEqual(len(act), 1)
        self.assertIs(act[0]["encrypted"], True)
        self.assertIn("EMB", act[0]["encryption_evidence"])
        self.assertIn("ENCRYPTED", act[0]["text"])
        self.assertIsNone(act[0]["talkgroup"])       # honest about what is missing
        self.assertIn("unknown", act[0]["text"])

    def test_encryption_is_never_reported_as_a_failed_decode(self):
        bursts = [build_data_burst("bs_data", CC, DT_VOICE_LC_HEADER,
                                   _lc(SVC_PRIVACY_BIT)),
                  build_voice_burst("bs_voice", voice_seed=13),
                  build_voice_burst(emb=(CC, 1, 0), voice_seed=14)]
        r = _decode(bursts)
        self.assertTrue(r["messages"])               # not silence
        self.assertIn("ENCRYPTED", r["note"])

    def test_clear_traffic_is_not_called_encrypted(self):
        bursts = [build_data_burst("bs_data", CC, DT_VOICE_LC_HEADER, _lc(0x00)),
                  build_voice_burst(emb=(CC, 0, 0), voice_seed=15)]
        r = _decode(bursts)
        self.assertIs(_only_voice_call(r)["encrypted"], False)
        self.assertNotIn("ENCRYPTED", r["note"])


class TestNegatives(unittest.TestCase):
    """Nothing here may produce a message. A fabricated talkgroup in a sweep
    report is worse than an empty result."""

    def _assert_nothing(self, iq, fs, label):
        r = decode_dmr(iq, fs)
        self.assertEqual(r["messages"], [], "%s produced messages" % label)
        self.assertTrue(r["note"])

    def test_noise(self):
        rng = np.random.default_rng(11)
        n = 400_000
        self._assert_nothing((rng.standard_normal(n)
                              + 1j * rng.standard_normal(n)).astype(np.complex64),
                             2_400_000.0, "noise")

    def test_analog_fm_voice(self):
        t = np.arange(240_000) / 2_400_000.0
        dev = 3000.0 * np.sin(2 * np.pi * 1000.0 * t)
        iq = np.exp(1j * 2 * np.pi * np.cumsum(dev) / 2_400_000.0).astype(np.complex64)
        self._assert_nothing(iq, 2_400_000.0, "analog FM")

    def test_pocsag_page(self):
        iq = encode_pocsag([{"ric": 123456, "mode": "alpha", "text": "HELLO WORLD"}],
                           1200, FS, preamble_bits=576)
        self._assert_nothing(iq, FS, "POCSAG")

    def test_unmodulated_carrier(self):
        t = np.arange(96_000) / FS
        self._assert_nothing(np.exp(1j * 2 * np.pi * 1200.0 * t).astype(np.complex64),
                             FS, "carrier")

    def test_random_c4fm_symbols_are_not_dmr(self):
        # correct modulation, correct symbol rate, no DMR framing — the sync +
        # FEC gate is the only thing standing between this and a fake decode
        d = np.random.default_rng(12).integers(0, 4, 40_000).astype(np.uint8)
        self._assert_nothing(c4fm.encode_c4fm(d, FS), FS, "random C4FM")

    def test_a_lone_sync_with_no_valid_fec_is_not_reported(self):
        # splice one real sync word into random C4FM: the sync matches, nothing
        # else validates, so the honesty gate must reject it
        rng = np.random.default_rng(13)
        d = rng.integers(0, 4, 4000).astype(np.uint8)
        sync = np.array([(SYNC_PATTERNS["bs_voice"] >> (2 * k)) & 3
                         for k in range(23, -1, -1)], dtype=np.uint8)
        d[1000:1024] = sync
        r = decode_dmr(c4fm.encode_c4fm(d, FS), FS)
        self.assertEqual(r["messages"], [])
        self.assertIn("not reported", r["note"])

    def test_p25_frame_sync_does_not_trip_the_dmr_sync_search(self):
        # DMR and P25 Phase 1 are the SAME modulation at the SAME symbol rate and
        # share c4fm.py, so the sync words are the only thing separating them.
        # P25's 0x5575F5FF77FF must be nowhere near a DMR sync word.
        p25_sync = 0x5575F5FF77FF
        for name, pattern in SYNC_PATTERNS.items():
            d = bin(p25_sync ^ pattern).count("1")
            di = bin(p25_sync ^ pattern ^ c4fm.dibit_invert_mask(24)).count("1")
            self.assertGreater(min(d, di), 2 * dmr.SYNC_MAX_ERRS, name)
        rng = np.random.default_rng(15)
        d = rng.integers(0, 4, 4000).astype(np.uint8)
        sync = np.array([(p25_sync >> (2 * k)) & 3 for k in range(23, -1, -1)],
                        dtype=np.uint8)
        for at in (500, 1500, 2500):
            d[at:at + 24] = sync
        self._assert_nothing(c4fm.encode_c4fm(d, FS), FS, "P25 sync words")

    def test_a_real_p25_call_is_not_decoded_as_dmr(self):
        # Guarded: survey/p25.py is another module's territory, so a change to
        # its encoder API skips this rather than failing the DMR suite.
        try:
            from survey import p25
            dibits = p25.encode_call(0x293, 4021, 2621102, ldu_pairs=2)
        except Exception as exc:                                    # noqa: BLE001
            raise unittest.SkipTest("p25 encoder unavailable: %s" % exc)
        self._assert_nothing(c4fm.encode_c4fm(dibits, FS), FS, "P25 call")

    def test_too_short_raises(self):
        with self.assertRaises(ValueError):
            decode_dmr(np.zeros(8, dtype=np.complex64), FS)


class TestGate(unittest.TestCase):
    def test_looks_like_dmr(self):
        self.assertTrue(dmr.looks_like_dmr(
            {"modulation_class": "fsk", "fsk_states": 4, "symbol_rate_kbd": 4.8,
             "occupied_bw_khz": 12.5}))
        self.assertFalse(dmr.looks_like_dmr(
            {"modulation_class": "fsk", "fsk_states": 2, "symbol_rate_kbd": 1.2,
             "occupied_bw_khz": 12.0}))
        self.assertFalse(dmr.looks_like_dmr({}))


class TestRouterRegistration(unittest.TestCase):
    def test_dmr_is_routable_and_counted_as_data(self):
        from survey import decode_router
        self.assertIn("dmr", decode_router.DATA_MODES)
        self.assertIn("dmr", decode_router.DECODE_MODES)
        self.assertIn("dmr", decode_router._ORDER)
        self.assertIn("dmr", decode_router._decoders())

    def test_router_decodes_a_preferred_dmr_capture(self):
        from survey.decode_router import route_decode
        iq = encode_dmr([build_data_burst("bs_data", CC, DT_VOICE_LC_HEADER,
                                          _lc(SVC_PRIVACY_BIT))], FS)
        r = route_decode(iq, FS, None, prefer="dmr")
        self.assertTrue(r["messages"])
        self.assertEqual((r["protocol"] or "").upper(), "DMR")
        self.assertIn("ENCRYPTED", r["messages"][0]["text"])

    def test_router_honesty_gate_holds_on_noise(self):
        from survey.decode_router import route_decode
        rng = np.random.default_rng(14)
        n = 200_000
        iq = (rng.standard_normal(n) + 1j * rng.standard_normal(n)).astype(np.complex64)
        r = route_decode(iq, 1_000_000.0, None, prefer="dmr")
        self.assertEqual(r["messages"], [])
        self.assertIsNone(r["protocol"])


if __name__ == "__main__":
    unittest.main()
