"""Unit tests for the DMR frame layer.

Four layers of assertion, deliberately:

1. SPEC ANCHORS that are not self-consistency. Every FEC code is enumerated in
   full and asserted EQUAL to its published ETSI TS 102 361-1 generator matrix —
   Golay(20,8) vs table B.11, the EMB quadratic-residue (16,7,6) vs B.12,
   Hamming(15,11,3) vs B.15, Hamming(13,9,3) vs B.14, RS(12,9) vs B.18 — and the
   BPTC bit ordering is recomputed against four worked entries of table B.3.
   Minimum distances are asserted too, but distance ALONE is not enough and this
   suite has the scar to prove it: the EMB generator polynomial was wrong for the
   whole life of the module, produced a perfectly valid (16,7,6) code, passed the
   distance test, and round-tripped this file's encoder against its own decoder
   every time. It was simply not DMR's (16,7,6). See
   TestCodesMatchTheEtsiGeneratorMatrices, which keeps that specific trap pinned.
   The ten sync words are checked against table 9.2, including the polarity-pair
   property that makes receive polarity unresolvable from the sync.
2. ROUND TRIPS through the real chain: build bursts, encode to C4FM IQ with
   dmr.encode_dmr(), decode, and assert the exact talkgroup, source radio ID,
   colour code, slot and ENCRYPTION STATE come back. Every link control built
   here carries REAL RS(12,9) parity under the correct per-data-type mask, so the
   outer checksum is exercised end to end rather than shipped as 24 zero bits.
3. NEGATIVES: noise, analog FM, a POCSAG page, an unmodulated carrier and a
   random C4FM stream must produce NO messages. A DMR decoder that invents a
   talkgroup is worse than one that finds nothing.
4. FALSE-ACCEPT REGRESSIONS, which are the point of the whole exercise. A random
   symbol stream carrying one planted sync word used to be accepted 3.0 % of the
   time and is now 0/6000; TestFalseAccepts re-runs those exact constructions and
   also pins the MECHANISM of the fix, so a future change that reopens the
   circular corroboration fails here by name rather than by drifting statistics.
   TestDetectionIsNotTradedAway is the other half: none of that tightening may
   cost a real call.

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
                        build_lc_payload, golay_20_8_encode, golay_20_8_decode,
                        emb_encode, emb_decode, pack_emb, bptc_encode,
                        bptc_decode, rs_12_9_parity, rs_12_9_check, gf_mul,
                        hamming_15_11_encode, hamming_15_11_decode,
                        hamming_13_9_encode, hamming_13_9_decode)   # noqa: E402
from survey.pocsag import encode_pocsag                             # noqa: E402

FS = 48000.0
TG, SRC, CC = 4021, 2621102, 1


def _lc(service_options=0x00, flco=FLCO_GROUP_VOICE, dst=TG, src=SRC,
        data_type=DT_VOICE_LC_HEADER):
    """A 96-bit BPTC payload: the 72-bit Full LC plus REAL RS(12,9) parity,
    masked for `data_type` per ETSI table B.21.

    The whole positive suite runs through this, so every round trip below
    exercises the outer checksum end to end rather than shipping 24 zero bits."""
    return build_lc_payload(flco, 0, service_options, dst, src, data_type)


def _lc_zero_rs(service_options=0x00, flco=FLCO_GROUP_VOICE, dst=TG, src=SRC):
    """Same link control with the RS parity zeroed — i.e. a word whose outer
    checksum is WRONG. Used to prove the RS is corroboration and not a gate."""
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
        # Without a data type there is no mask to unpick, so the RS is not
        # checked — and "not checked" is reported as exactly that, never as a
        # pass and never as a failure.
        self.assertFalse(lc["rs_checked"])
        self.assertFalse(lc["rs_ok"])
        # With the data type it verifies.
        checked = parse_full_lc(_lc(0x00), DT_VOICE_LC_HEADER)
        self.assertTrue(checked["rs_checked"])
        self.assertTrue(checked["rs_ok"])

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
        hdr = _lc(service_options, data_type=DT_VOICE_LC_HEADER)
        trm = _lc(service_options, data_type=DT_TERMINATOR_LC)
        bursts = [build_data_burst("bs_data", CC, DT_VOICE_LC_HEADER, hdr),
                  build_voice_burst("bs_voice", voice_seed=1),
                  build_voice_burst(emb=(CC, pi, 0), voice_seed=2),
                  build_data_burst("bs_data", CC, DT_TERMINATOR_LC, trm)]
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
                      build_data_burst("bs_data", cc, DT_TERMINATOR_LC,
                                       _lc(data_type=DT_TERMINATOR_LC))]
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


# ============================================================================
# ETSI generator matrices, transcribed from TS 102 361-1 V2.7.1 annex B.
# These are EXTERNAL DATA. Every code below is checked by enumerating its whole
# codebook and asserting equality with the matrix — not by round-tripping the
# encoder against its own decoder, which a matched pair of bugs passes happily.
# ============================================================================
G_GOLAY_20_8 = (                                     # table B.11
    "10000000001111011010", "01000000110110011001",
    "00100000011011001101", "00010000001101100111",
    "00001000110111000110", "00000100101010010111",
    "00000010100100111110", "00000001100011101011",
)
G_QR_16_7 = (                                        # table B.12
    "1000000001001111", "0100000100011110", "0010000110110111",
    "0001000111100010", "0000100111001001", "0000010011100101",
    "0000001001110011",
)
G_HAMMING_13_9 = (                                   # table B.14
    "1000000001111", "0100000001110", "0010000000111",
    "0001000001010", "0000100000101", "0000010001011",
    "0000001001100", "0000000100110", "0000000010011",
)
G_HAMMING_15_11 = (                                  # table B.15
    "100000000001001", "010000000001101", "001000000001111",
    "000100000001110", "000010000000111", "000001000001010",
    "000000100000101", "000000010001011", "000000001001100",
    "000000000100110", "000000000010011",
)
G_RS_12_9_PARITY = (                                 # table B.18, parity columns
    (0x1C, 0xBC, 0xFD), (0x89, 0x31, 0x08), (0xAD, 0x41, 0x36),
    (0x7D, 0x71, 0x16), (0xF3, 0xA6, 0x3A), (0x08, 0x83, 0x7B),
    (0x3F, 0x6F, 0x02), (0x6C, 0x0D, 0xA7), (0x0E, 0x38, 0x40),
)


def _codebook(matrix_rows, k):
    """Every codeword of the linear code with this generator matrix, keyed by the
    integer data word with d0 in the HIGH bit."""
    rows = [int(r, 2) for r in matrix_rows]
    out = {}
    for d in range(1 << k):
        cw = 0
        for i in range(k):
            if (d >> (k - 1 - i)) & 1:
                cw ^= rows[i]
        out[d] = cw
    return out


def _int_of(bits):
    v = 0
    for b in bits:
        v = (v << 1) | (int(b) & 1)
    return v


class TestCodesMatchTheEtsiGeneratorMatrices(unittest.TestCase):
    """The strongest anchors in this file: codebook EQUALITY against the
    published matrices.

    A minimum-distance test is necessary but not sufficient, and this suite
    learned that the hard way. The EMB generator polynomial was 0x1D1 for the
    whole life of the module. It produced a perfectly good (16,7,6) code, so
    test_emb_code_minimum_distance_is_6 passed; the encoder and decoder in
    dmr.py agreed with each other, so every round trip passed. It was simply not
    DMR's (16,7,6) — a different code with a disjoint codeword set — and every
    EMB in real off-air traffic would have failed to decode. Only equality
    against table B.12 catches that class of fault."""

    def test_golay_matches_etsi_table_b11(self):
        spec = _codebook(G_GOLAY_20_8, 8)
        self.assertEqual({d: golay_20_8_encode(d) for d in range(256)}, spec)

    def test_emb_matches_etsi_table_b12(self):
        # THE regression test for the 0x1D1 -> 0x139 fix.
        spec = _codebook(G_QR_16_7, 7)
        self.assertEqual({d: emb_encode(d) for d in range(128)}, spec)

    def test_emb_generator_polynomial_is_the_published_one(self):
        # x^8 + x^5 + x^4 + x^3 + 1, written 0471 octal in eq. B.5.
        self.assertEqual(dmr.EMB_QR_POLY, 0o471)
        self.assertEqual(dmr.EMB_QR_POLY, 0b100111001)

    def test_hamming_13_9_matches_etsi_table_b14(self):
        spec = _codebook(G_HAMMING_13_9, 9)
        got = {d: _int_of(hamming_13_9_encode([(d >> j) & 1 for j in range(8, -1, -1)]))
               for d in range(512)}
        self.assertEqual(got, spec)

    def test_hamming_15_11_matches_etsi_table_b15(self):
        spec = _codebook(G_HAMMING_15_11, 11)
        got = {d: _int_of(hamming_15_11_encode([(d >> j) & 1 for j in range(10, -1, -1)]))
               for d in range(2048)}
        self.assertEqual(got, spec)

    def test_a_wrong_polynomial_would_still_pass_the_distance_test(self):
        # Pin the REASON the tests above exist, so nobody deletes them as
        # duplicates of the minimum-distance tests. The old constant yields a
        # (16,7,6) code too — same distance, disjoint codewords.
        def old_encode(d7):
            reg = (d7 & 0x7F) << 8
            cw15 = reg | dmr._gf2_remainder(reg, 0x1D1, 8)
            return ((cw15 << 1) | (bin(cw15).count("1") & 1)) & 0xFFFF
        old = {old_encode(d) for d in range(128)}
        self.assertEqual(_mindist(old), 6)                 # distance test passes
        new = {emb_encode(d) for d in range(128)}
        self.assertEqual(old & new, {0})                   # yet only 0 is shared


class TestReedSolomon12_9(unittest.TestCase):
    """TS 102 361-1 B.3.6. The convention the previous build could not source is
    the FIRST CONSECUTIVE ROOT, and eq. B.9 states it plainly: the generator is
    (x + a)(x + a^2)(x + a^3), so FCR = 1."""

    def test_generator_polynomial_from_its_roots(self):
        # Rebuild g(x) from the roots and check it against eq. B.10's 01 0E 38 40.
        def expand(fcr):
            g = [1]
            for i in range(3):
                root = dmr._GF_EXP[fcr + i]
                ng = [0] * (len(g) + 1)
                for j, c in enumerate(g):
                    ng[j] ^= gf_mul(c, root)
                    ng[j + 1] ^= c
                g = ng
            return tuple(reversed(g))              # -> highest power first
        self.assertEqual(dmr.RS_FIRST_CONSECUTIVE_ROOT, 1)
        self.assertEqual(expand(1), (0x01, 0x0E, 0x38, 0x40))
        self.assertEqual(expand(1), dmr.RS_GENERATOR)
        # and the test DISTINGUISHES the conventions rather than merely being
        # consistent with one of them — the neighbours are different polynomials
        self.assertNotEqual(expand(0), dmr.RS_GENERATOR)
        self.assertNotEqual(expand(2), dmr.RS_GENERATOR)

    def test_field_is_the_published_one(self):
        # a^8 + a^4 + a^3 + a^2 + 1 (eq. B.14). Table B.19 row 8 reads 1D.
        self.assertEqual(dmr.RS_PRIMITIVE_POLY, 0x11D)
        self.assertEqual(dmr._GF_EXP[8], 0x1D)
        self.assertEqual(len({dmr._GF_EXP[i] for i in range(255)}), 255)

    def test_reproduces_the_etsi_generator_matrix(self):
        # Table B.18: row i is the parity contributed by message octet i alone.
        for i, expected in enumerate(G_RS_12_9_PARITY):
            msg = [0] * 9
            msg[i] = 1
            self.assertEqual(tuple(rs_12_9_parity(msg)), expected, "row %d" % i)

    def test_minimum_distance_is_4_over_single_symbol_messages(self):
        # RS(12,9,4) is MDS, so a message of symbol weight 1 must produce a
        # codeword of symbol weight >= 4 — i.e. all THREE parity octets non-zero.
        # Enumerated exhaustively over all 9 x 255 such messages.
        for pos in range(9):
            for val in range(1, 256):
                msg = [0] * 9
                msg[pos] = val
                par = rs_12_9_parity(msg)
                self.assertEqual(sum(1 for p in par if p), 3,
                                 "weight-1 message at %d value %02X gave %s"
                                 % (pos, val, par))

    def test_minimum_distance_holds_on_multi_symbol_differences(self):
        # Linear code, so distance = minimum weight of a non-zero codeword.
        # Sample weight-2 and weight-3 messages and assert total symbol weight >= 4.
        rng = np.random.default_rng(101)
        for _ in range(4000):
            msg = [0] * 9
            for pos in rng.choice(9, int(rng.integers(2, 4)), replace=False):
                msg[int(pos)] = int(rng.integers(1, 256))
            w = sum(1 for m in msg if m) + sum(1 for p in rs_12_9_parity(msg) if p)
            self.assertGreaterEqual(w, 4)

    def test_the_data_type_mask_is_applied_and_discriminates(self):
        # A Voice LC Header and a Terminator with LC carry IDENTICAL link
        # control and differ only by their CRC mask (table B.21). So checking a
        # header's payload as a terminator must FAIL — that is what makes an RS
        # pass also confirm the Golay slot type's data-type field.
        header = _lc(0x00, data_type=DT_VOICE_LC_HEADER)
        self.assertEqual(rs_12_9_check(header, DT_VOICE_LC_HEADER), (True, True))
        self.assertEqual(rs_12_9_check(header, DT_TERMINATOR_LC), (True, False))
        term = _lc(0x00, data_type=DT_TERMINATOR_LC)
        self.assertEqual(rs_12_9_check(term, DT_TERMINATOR_LC), (True, True))
        self.assertEqual(rs_12_9_check(term, DT_VOICE_LC_HEADER), (True, False))
        self.assertEqual(dmr.FULL_LC_RS_MASK[DT_VOICE_LC_HEADER], 0x969696)
        self.assertEqual(dmr.FULL_LC_RS_MASK[DT_TERMINATOR_LC], 0x999999)

    def test_a_data_type_with_no_24_bit_mask_reports_not_checked(self):
        # CSBK's mask is 16-bit and belongs to a CRC-CCITT this module does not
        # implement. The honest answer is "not checked", not "failed".
        self.assertEqual(rs_12_9_check(_lc(), DT_CSBK), (False, False))
        self.assertEqual(rs_12_9_check(_lc(), None), (False, False))

    def test_rs_detects_a_corrupted_link_control(self):
        for bit in (3, 20, 40, 60, 71):
            bad = list(_lc())
            bad[bit] ^= 1
            self.assertEqual(rs_12_9_check(bad, DT_VOICE_LC_HEADER), (True, False),
                             "bit %d" % bit)


class TestRsIsCorroborationNotAGate(unittest.TestCase):
    """The operator's explicit requirement, and the reason the previous build
    left the RS unimplemented rather than guess at it: if any part of the
    convention were still wrong, the failure must degrade to 'unverified', never
    to 'real calls silently discarded'."""

    def test_a_verified_call_says_so(self):
        r = _decode([build_data_burst("bs_data", CC, DT_VOICE_LC_HEADER, _lc())])
        m = _only_voice_call(r)
        self.assertTrue(m["rs_checked"])
        self.assertTrue(m["rs_ok"])
        self.assertIn("RS(12,9)", m["evidence"])
        self.assertIn("RS(12,9)-verified", r["note"])

    def test_a_call_with_WRONG_rs_parity_is_still_reported(self):
        # THE gate-vs-corroboration test. The payload carries a link control
        # whose outer checksum does not verify; the talkgroup and radio ID must
        # still come back, flagged as unverified.
        r = _decode([build_data_burst("bs_data", CC, DT_VOICE_LC_HEADER,
                                      _lc_zero_rs())])
        m = _only_voice_call(r)
        self.assertEqual(m["talkgroup"], TG)
        self.assertEqual(m["source"], SRC)
        self.assertTrue(m["rs_checked"])
        self.assertFalse(m["rs_ok"])
        self.assertIn("did NOT verify", m["evidence"])
        self.assertIn("did NOT verify", r["note"])

    def test_the_whole_positive_suite_verifies(self):
        # Every burst the encoder builds through _lc() must satisfy the RS, so a
        # future change to the mask table or the root convention fails loudly
        # across the suite rather than in one place.
        for dt in (DT_VOICE_LC_HEADER, DT_TERMINATOR_LC):
            for svc in (0x00, SVC_PRIVACY_BIT, SVC_EMERGENCY_BIT):
                lc = parse_full_lc(_lc(svc, data_type=dt), dt)
                self.assertTrue(lc["rs_ok"], "data type %d svc %02X" % (dt, svc))


class TestBptcLayoutAgainstTheSpec(unittest.TestCase):
    """TS 102 361-1 B.1.1 with its worked tables B.2 and B.3. The module docstring
    used to flag the x181 interleave, the 13x15 layout and the 3 reserved info
    bits as the most likely one-constant fix once real traffic was seen. They are
    now CONFIRMED CORRECT against the published tables — this is the check."""

    def test_interleave_step_and_reserved_bit_are_the_published_ones(self):
        self.assertEqual(dmr.BPTC_INTERLEAVE_STEP, 181)      # eq. B.1
        self.assertEqual(dmr.BPTC_RESERVED_INFO_BITS, 3)     # R(0)-R(2)
        self.assertEqual((dmr.BPTC_ROWS, dmr.BPTC_COLS), (13, 15))
        self.assertEqual((dmr.BPTC_DATA_ROWS, dmr.BPTC_DATA_COLS), (9, 11))

    def test_bptc_layout_matches_etsi_table_b3(self):
        """Recompute four table B.3 entries from the module's own constants.

        Table B.3 lists, for each INTERLEAVE index, which named bit lands there.
        Index 0 is R(3) — the pad bit that takes 195 to 196 — and the entries
        below pin the rest of the permutation, including the row-parity ORDER
        (H_R(3) leftmost), which is what the Hamming masks depend on."""
        inv = {}                                   # interleave index -> plain index
        for i in range(dmr.BPTC_BITS):
            inv[(i * dmr.BPTC_INTERLEAVE_STEP) % dmr.BPTC_BITS] = i
        # index 0 is R(3), outside the 13x15 matrix
        self.assertEqual(inv[0], 0)
        for interleave_index, row, col in dmr.BPTC_TABLE_B3_ANCHORS:
            plain = inv[interleave_index]
            # plain index 0 is R(3); 1.. are the matrix, row-major
            self.assertGreater(plain, 0)
            got_row, got_col = divmod(plain - 1, dmr.BPTC_COLS)
            self.assertEqual((got_row, got_col), (row, col),
                             "table B.3 interleave index %d" % interleave_index)

    def test_the_three_reserved_bits_lead_the_info_field(self):
        # R(0)-R(2) then I(95) down to I(0), row-major into the 9x11 corner.
        payload = [1] * 96
        deint = dmr.bptc_deinterleave(bptc_encode(payload))
        matrix = dmr._matrix_from_deint(deint)
        self.assertEqual(matrix[0][:dmr.BPTC_RESERVED_INFO_BITS], [0, 0, 0])
        self.assertEqual(matrix[0][dmr.BPTC_RESERVED_INFO_BITS:dmr.BPTC_DATA_COLS],
                         [1] * (dmr.BPTC_DATA_COLS - dmr.BPTC_RESERVED_INFO_BITS))
        self.assertEqual(deint[0], 0)                        # R(3)


# ============================================================================
# FALSE ACCEPTS. The measured priority: a fabricated "DMR call detected" in a
# client report is a serious failure, so these are the regressions that matter
# most in this file.
# ============================================================================
def _sync_dibits(name):
    p = SYNC_PATTERNS[name]
    return np.array([(p >> (2 * k)) & 3 for k in range(23, -1, -1)], dtype=np.uint8)


def _decode_dibits(d):
    return decode_dmr(None, FS, symbols={"dibits": np.asarray(d, dtype=np.uint8),
                                         "quality": 1.0})


def _planted(rng, k, n_dibits=4000):
    """Random symbols with k real sync words spliced in on the TDMA grid — the
    construction that measured 1.5-3.5 % acceptance before this work."""
    names = list(SYNC_PATTERNS)
    d = rng.integers(0, 4, n_dibits).astype(np.uint8)
    for i in range(k):
        at = 1000 + i * dmr.TDMA_FRAME_DIBITS
        d[at:at + dmr.SYNC_DIBITS] = _sync_dibits(names[int(rng.integers(0, 10))])
    return d


def _fabricates(result):
    """Did any message assert a colour code, talkgroup, radio ID or encryption
    state? That is the claim that ends up in front of a client."""
    for m in result["messages"]:
        if (m.get("colour_code") is not None or m.get("talkgroup") is not None
                or m.get("source") is not None or m.get("encrypted") is not None):
            return True
    return False


class TestFalseAccepts(unittest.TestCase):
    """MEASURED, not asserted by hope. Numbers in these docstrings come from
    scripted runs over the sample sizes stated; the tests below re-run smaller
    slices of exactly those constructions so a regression fails here."""

    def test_random_symbols_never_produce_a_message(self):
        for seed in range(300):
            r = _decode_dibits(np.random.default_rng(seed).integers(0, 4, 4000))
            self.assertEqual(r["messages"], [], "seed %d" % seed)

    def test_a_planted_sync_in_noise_is_not_a_call(self):
        """THE headline regression. One real sync word in an otherwise random
        symbol stream measured 3.0 % acceptance (6/200) before this work and
        0/6000 after.

        Every one of those false accepts had the same signature: a Golay(20,8)
        slot type decoded out of noise (33 % of random 20-bit words do), its
        colour code was accepted as an anchor even though the SAME burst's
        BPTC had failed, and one 2-error EMB out of ~12 probe positions then
        'agreed' with that anchor. The agreement was counted as the independent
        FEC evidence that opened the honesty gate."""
        for seed in range(300):
            r = _decode_dibits(_planted(np.random.default_rng(seed), 1))
            self.assertEqual(r["messages"], [], "seed %d produced %r"
                             % (seed, [m["text"] for m in r["messages"]]))

    def test_two_planted_syncs_in_noise_are_not_a_call(self):
        # measured 10.0 % before, 0/6000 after
        for seed in range(200):
            r = _decode_dibits(_planted(np.random.default_rng(seed), 2))
            self.assertEqual(r["messages"], [], "seed %d" % seed)

    def test_an_unverified_colour_code_cannot_corroborate_an_emb(self):
        """Pin the MECHANISM of the fix, not just its outcome.

        A burst whose Golay slot type decoded but whose BPTC product code failed
        must not put its colour code into the anchor set. Without this, noise
        supplies its own corroboration and the honesty gate is circular."""
        rng = np.random.default_rng(7)
        # a burst with a valid sync and slot type but a random (invalid) BPTC
        bits = build_data_burst("bs_data", 9, DT_VOICE_LC_HEADER,
                                list(rng.integers(0, 2, 96)))
        for i in range(dmr.INFO1[0], dmr.INFO1[0] + 20):      # wreck the BPTC
            bits[i] ^= 1
        parsed = dmr._parse_burst(bits, "bs_data", 0, 0, False)
        self.assertEqual(parsed["colour_code"], 9)            # Golay still decodes
        self.assertFalse(parsed["bptc_ok"])                   # but nothing verified
        # so it must contribute NO anchor to the EMB probe
        seen = set()
        probed = dmr._probe_embedded_voice(
            np.zeros(dmr.BURST_DIBITS * 30, dtype=np.uint8), [parsed], seen, False)
        for rec in probed:
            self.assertNotEqual(rec["colour_code"], 9)

    def test_what_an_emb_must_corroborate_scales_with_its_anchor(self):
        """26.8 % of random 16-bit words sit within the (16,7,6) correction
        radius, so an EMB on its own is a coincidence, not a detection. What it
        has to agree WITH depends on how strong that anchor is."""
        self.assertGreaterEqual(dmr.EMB_MIN_AGREEING_UNANCHORED, 2)

        # anchored on a BPTC-verified colour code: one EMB is enough
        r = _decode([build_data_burst("bs_data", 4, DT_VOICE_LC_HEADER, _lc()),
                     build_voice_burst(emb=(4, 1, 1), voice_seed=3)])
        self.assertEqual(r["stats"]["proven_colour_codes"], [4])
        self.assertGreaterEqual(r["stats"]["emb_ok"], 1)

        # anchored, but the EMB disagrees with the proven colour code: rejected
        r = _decode([build_data_burst("bs_data", 4, DT_VOICE_LC_HEADER, _lc()),
                     build_voice_burst(emb=(11, 1, 1), voice_seed=3)])
        self.assertEqual(r["stats"]["proven_colour_codes"], [4])
        self.assertNotIn(11, r["stats"]["colour_codes"])

    def test_unanchored_embs_must_corroborate_each_other(self):
        # No BPTC block anywhere, so the EMBs are all the evidence there is and
        # a single one cannot stand alone.
        one = _decode([build_voice_burst("bs_voice", voice_seed=1),
                       build_voice_burst(emb=(6, 0, 1), voice_seed=2)])
        self.assertEqual(one["stats"]["emb_ok"], 0)
        two = _decode([build_voice_burst("bs_voice", voice_seed=1),
                       build_voice_burst(emb=(6, 0, 1), voice_seed=2),
                       build_voice_burst(emb=(6, 0, 2), voice_seed=3)])
        self.assertGreaterEqual(two["stats"]["emb_ok"], 2)
        self.assertEqual(two["stats"]["colour_codes"], [6])

    def test_three_incoherent_syncs_are_not_a_detection(self):
        """Three unrelated 48-bit matches are three coincidences. Only syncs that
        agree on the sync WORD and on the TDMA grid phase can be one transmitter,
        and the no-FEC escape hatch counts those."""
        rng = np.random.default_rng(21)
        d = rng.integers(0, 4, 6000).astype(np.uint8)
        # different sync flavours, deliberately off the shared grid phase
        for i, name in enumerate(("bs_voice", "ms_voice", "direct_ts1_voice")):
            at = 1000 + i * dmr.TDMA_FRAME_DIBITS + i * 7
            d[at:at + dmr.SYNC_DIBITS] = _sync_dibits(name)
        r = _decode_dibits(d)
        self.assertLess(r["stats"]["coherent_syncs"], dmr.MIN_SYNCS_WITHOUT_FEC)
        self.assertEqual(r["messages"], [])
        self.assertIn("coherent", r["note"])

    def test_coherent_syncs_counts_one_transmitter_not_the_total(self):
        found = [(1000, "bs_voice", 0),
                 (1000 + dmr.TDMA_FRAME_DIBITS, "bs_voice", 0),
                 (1000 + 2 * dmr.TDMA_FRAME_DIBITS, "bs_voice", 0),
                 (1731, "ms_data", 1)]                # wrong word AND wrong phase
        self.assertEqual(dmr._coherent_syncs(found), 3)
        self.assertEqual(dmr._coherent_syncs([]), 0)
        off_grid = [(1000, "bs_voice", 0), (1003, "bs_voice", 0),
                    (1009, "bs_voice", 0)]
        self.assertEqual(dmr._coherent_syncs(off_grid), 1)

    def test_noise_never_fabricates_a_field(self):
        """The claim that actually reaches a client report. Across every noise
        construction, no message may assert a colour code, talkgroup, radio ID or
        encryption state. Measured 0.000 % over 6000 frames of each."""
        rng_makers = (
            lambda s: np.random.default_rng(s).integers(0, 4, 4000),
            lambda s: _planted(np.random.default_rng(s), 1),
            lambda s: _planted(np.random.default_rng(s), 2),
            lambda s: _planted(np.random.default_rng(s), 3),
        )
        for i, make in enumerate(rng_makers):
            for seed in range(120):
                with self.subTest(construction=i, seed=seed):
                    self.assertFalse(_fabricates(_decode_dibits(make(seed))))

    def test_link_control_with_zero_addresses_is_suppressed(self):
        # src 0 is not an assignable DMR radio ID and dst 0 is not a talkgroup;
        # all-zero is what a flattened BPTC block looks like.
        r = _decode([build_data_burst("bs_data", CC, DT_VOICE_LC_HEADER,
                                      _lc_zero_rs(dst=0, src=0))])
        self.assertEqual([m for m in r["messages"] if m["type"] == "voice_call"], [])

    def test_zero_addresses_survive_if_the_rs_verifies(self):
        # ...but plausibility is a weak heuristic, so real arithmetic overrides
        # it. We do not second-guess a system whose checksum verified.
        r = _decode([build_data_burst("bs_data", CC, DT_VOICE_LC_HEADER,
                                      _lc(dst=0, src=0))])
        self.assertEqual(len([m for m in r["messages"] if m["type"] == "voice_call"]), 1)


class TestEvidenceIsAlwaysStated(unittest.TestCase):
    """Every message says what stands behind it. The grades are not
    interchangeable and a sweep report needs to be able to tell them apart."""

    def test_every_message_carries_an_evidence_string(self):
        bursts = [build_data_burst("bs_data", CC, DT_VOICE_LC_HEADER, _lc()),
                  build_data_burst("bs_data", CC, DT_CSBK,
                                   list(np.random.default_rng(2).integers(0, 2, 96))),
                  build_voice_burst("bs_voice", voice_seed=1),
                  build_voice_burst(emb=(CC, 0, 1), voice_seed=2),
                  build_voice_burst(emb=(CC, 0, 2), voice_seed=3)]
        r = _decode(bursts)
        self.assertTrue(r["messages"])
        for m in r["messages"]:
            self.assertTrue(m.get("evidence"), "%s has no evidence" % m["type"])

    def test_a_sync_only_detection_admits_it_is_sync_only(self):
        """Three coherent syncs with unusable payload IS a real finding — DMR
        framing is present — but it is the weakest thing this module emits and it
        must not read like a decode. No colour code, no talkgroup, no encryption
        claim, and the text says so."""
        rng = np.random.default_rng(4)
        sy = _sync_dibits("bs_voice")
        out = [rng.integers(0, 4, 800).astype(np.uint8)]
        for _ in range(3):
            b = rng.integers(0, 4, dmr.BURST_DIBITS).astype(np.uint8)
            b[dmr.SYNC_OFFSET_DIBITS:dmr.SYNC_OFFSET_DIBITS + dmr.SYNC_DIBITS] = sy
            out.append(b)
            out.append(rng.integers(0, 4, dmr.TDMA_FRAME_DIBITS - dmr.BURST_DIBITS)
                       .astype(np.uint8))
        out.append(rng.integers(0, 4, 800).astype(np.uint8))
        r = _decode_dibits(np.concatenate(out))
        self.assertEqual(r["stats"]["coherent_syncs"], 3)
        self.assertFalse(_fabricates(r))
        act = [m for m in r["messages"] if m["type"] == "voice_activity"]
        self.assertEqual(len(act), 1)
        self.assertIsNone(act[0]["colour_code"])
        self.assertIsNone(act[0]["encrypted"])
        self.assertIn("SYNC PATTERN ALONE", act[0]["text"])
        self.assertIn("no FEC block validated", act[0]["evidence"])

    def test_a_verified_call_outranks_a_sync_only_finding(self):
        m = _only_voice_call(
            _decode([build_data_burst("bs_data", CC, DT_VOICE_LC_HEADER, _lc())]))
        self.assertIn("BPTC(196,96)", m["evidence"])
        self.assertNotIn("sync pattern only", m["evidence"])


class TestDetectionIsNotTradedAway(unittest.TestCase):
    """The other half of the false-accept work: none of the tightening above may
    cost a real call. Measured 60/60 full recoveries (talkgroup AND radio ID AND
    RS verified) at every noise level from 0.00 to 0.40; the front end, not the
    gating, sets the cliff between 0.48 and 0.56."""

    def _superframe(self, cc=1, privacy=False):
        svc = SVC_PRIVACY_BIT if privacy else 0
        bursts = [build_data_burst("bs_data", cc, DT_VOICE_LC_HEADER,
                                   _lc(svc, data_type=DT_VOICE_LC_HEADER)),
                  build_voice_burst("bs_voice", voice_seed=1)]
        for lcss in range(5):
            bursts.append(build_voice_burst(emb=(cc, 1 if privacy else 0, lcss % 4),
                                            voice_seed=2 + lcss))
        bursts.append(build_data_burst("bs_data", cc, DT_TERMINATOR_LC,
                                       _lc(svc, data_type=DT_TERMINATOR_LC)))
        return bursts

    def test_a_full_superframe_recovers_at_every_workable_noise_level(self):
        for noise in (0.0, 0.10, 0.20, 0.30, 0.40):
            for seed in (0, 1, 2):
                with self.subTest(noise=noise, seed=seed):
                    r = decode_dmr(encode_dmr(self._superframe(), FS,
                                              noise=noise, seed=seed), FS)
                    m = _only_voice_call(r)
                    self.assertEqual(m["talkgroup"], TG)
                    self.assertEqual(m["source"], SRC)
                    self.assertEqual(m["colour_code"], 1)
                    self.assertTrue(m["rs_ok"])

    def test_embedded_signalling_still_corroborates_a_real_superframe(self):
        r = _decode(self._superframe())
        self.assertGreaterEqual(r["stats"]["emb_ok"], 2)
        self.assertEqual(r["stats"]["proven_colour_codes"], [1])
        self.assertTrue(r["stats"]["colour_code_consistent"])

    def test_encryption_is_still_a_first_class_result_after_tightening(self):
        r = _decode(self._superframe(privacy=True))
        m = _only_voice_call(r)
        self.assertIs(m["encrypted"], True)
        self.assertEqual(m["talkgroup"], TG)
        self.assertIn("ENCRYPTED", r["note"])

    def test_sync_tolerance_is_not_the_binding_constraint(self):
        """Why SYNC_MAX_ERRS stays at 4, measured rather than assumed.

        Detection of a full call was identical for tolerances 1 through 6 at
        every noise level from 0.40 to 0.56 — the C4FM front end and the BPTC
        fail well before the sync search does — while false accepts are already
        0/6000 at tolerance 4. Tightening it would therefore be an unvalidated
        change with no measured benefit, so it was not made."""
        self.assertEqual(dmr.SYNC_MAX_ERRS, 4)
        bursts = self._superframe()
        for tol in (1, 4):
            dmr.SYNC_MAX_ERRS = tol
            try:
                m = _only_voice_call(decode_dmr(
                    encode_dmr(bursts, FS, noise=0.40, seed=3), FS))
                self.assertEqual(m["talkgroup"], TG, "tolerance %d" % tol)
            finally:
                dmr.SYNC_MAX_ERRS = 4


if __name__ == "__main__":
    unittest.main()
