"""Unit tests for the P25 Phase 1 frame decoder.

Synthesize known P25 frames with p25.encode_call() / encode_frame_dibits()
(which share every FEC routine with the decoder, so a wrong BCH generator, a
wrong RS convention or a wrong shortened-code table fails loudly), then
round-trip them through decode_p25_dibits and assert EXACT recovery of NAC,
DUID, talkgroup, source radio ID, ALGID, key ID and message indicator.

Three things this file exists to prove:
  1. Exact metadata recovery from clean synthetic frames.
  2. FEC really corrects — injected bit and symbol errors are repaired at every
     layer (BCH on the NID, Golay/Hamming on the hexbits, RS on the block).
  3. ENCRYPTED traffic is a RESULT. An AES-256 call must come back as a decoded
     call carrying ALGID 0x84, not as a failure or an empty message list.

And the negatives, which matter more than the positives on a TSCM tool: random
noise, an analog-FM-derived symbol stream, a 2-FSK paging stream and a bare
planted sync word must NOT manufacture a frame. A false "P25 system detected"
in a client report is worse than a miss.

All headless — no hardware, no IQ except in the analog-FM negative and the
entry-point smoke test. The decoder's primary input is a dibit stream, so these
tests do not depend on survey/c4fm.py existing.
"""

import unittest

import numpy as np

from survey import p25
from survey.p25 import (FRAME_SYNC, ALGID_CLEAR, DUID_HDU, DUID_TDU, DUID_LDU1,
                        DUID_LDU2, DUID_TSDU, DUID_TDULC, LCO_GROUP_VOICE,
                        LCO_UNIT_VOICE, NID_MAX_ERRS, bch63_16_encode,
                        bch63_16_decode, nid_encode, decode_nid, rs_encode,
                        rs_decode, golay18_encode, golay18_decode,
                        hamming10_encode, hamming10_decode, crc16_p25,
                        trellis_half_encode, trellis_half_decode,
                        encode_call, encode_frame_dibits, decode_p25_dibits,
                        decode_p25, looks_like_p25, make_link_control,
                        make_encryption_sync, make_hdu_header, make_tsbk,
                        dibits_to_bits, bits_to_dibits)

NAC = 0x293               # the NAC used across the positive tests
TGID = 1001
SRC = 9114712


def _only_call(result):
    calls = result["calls"]
    assert len(calls) == 1, f"expected 1 call, got {len(calls)}: {calls}"
    return calls[0]


def _frames_by_duid(result):
    return {f["duid_name"]: f for f in result["frames"]}


class TestConstants(unittest.TestCase):
    def test_frame_sync(self):
        self.assertEqual(FRAME_SYNC, 0x5575F5FF77FF)
        self.assertEqual(p25.FRAME_SYNC_DIBITS.size, 24)
        # the published sync, re-derived: 0101 0101 0111 0101 1111 ...
        self.assertEqual(list(p25.FRAME_SYNC_DIBITS[:8]), [1, 1, 1, 1, 1, 3, 1, 1])
        self.assertEqual(p25._bits_to_int(dibits_to_bits(p25.FRAME_SYNC_DIBITS)),
                         FRAME_SYNC)

    def test_duids(self):
        self.assertEqual((DUID_HDU, DUID_TDU, DUID_LDU1, DUID_TSDU, DUID_LDU2,
                          p25.DUID_PDU, DUID_TDULC),
                         (0x0, 0x3, 0x5, 0x7, 0xA, 0xC, 0xF))

    def test_algid_clear(self):
        self.assertEqual(ALGID_CLEAR, 0x80)
        self.assertIn("clear", p25.algid_name(0x80))
        self.assertIn("AES-256", p25.algid_name(0x84))
        self.assertIn("ADP", p25.algid_name(0xAA))
        self.assertIn("unknown", p25.algid_name(0x5C))

    def test_ldu_layout_budget(self):
        # 9 voice frames + 6x40-bit LC/ES chunks + 32-bit LSD = 1568 data bits
        self.assertEqual(p25.LDU_PAYLOAD_BITS, 1568)
        self.assertEqual(sum(1 for k, _ in p25.LDU_LAYOUT if k == "V"), 9)
        self.assertEqual(p25.LC_PAYLOAD_BITS, 240)


class TestBCH(unittest.TestCase):
    """BCH(63,16,23) — the NID's protection, and the strongest honesty gate."""

    def test_generator_is_degree_47(self):
        self.assertEqual(p25.BCH_GEN_DEG, 47)

    def test_minimum_distance_is_23(self):
        # Exhaustive over all 65536 codewords. This single assertion pins the
        # field (x^6+x+1), the root set (alpha^1..alpha^21) and the systematic
        # layout all at once — a wrong generator cannot reach d=23.
        cb = p25._nid_codebook()
        w = p25._POPCNT8[cb].sum(axis=1)
        self.assertEqual(int(w[0]), 0)
        self.assertEqual(int(w[1:].min()), 23)

    def test_systematic_layout(self):
        for msg in (0x0000, 0xABCD, 0xFFFF, (NAC << 4) | DUID_LDU1):
            cw = bch63_16_encode(msg)
            self.assertEqual(cw >> 47, msg)
            got, dist = bch63_16_decode(cw)
            self.assertEqual((got, dist), (msg, 0))

    def test_nid_roundtrip(self):
        for nac in (0x000, 0x293, 0x4D2, 0xFFF, 0xF7E):
            for duid in (DUID_HDU, DUID_TDU, DUID_LDU1, DUID_TSDU, DUID_LDU2,
                         DUID_TDULC):
                info = decode_nid(nid_encode(nac, duid))
                self.assertIsNotNone(info)
                self.assertEqual((info["nac"], info["duid"]), (nac, duid))
                self.assertEqual(info["bit_errors"], 0)
                self.assertTrue(info["parity_ok"])

    def test_corrects_up_to_eleven_bit_errors(self):
        rng = np.random.default_rng(0)
        base = nid_encode(NAC, DUID_LDU1)
        for nerr in range(1, NID_MAX_ERRS + 1):
            for _ in range(6):
                bad = base
                for b in rng.choice(64, size=nerr, replace=False):
                    bad ^= 1 << int(b)
                info = decode_nid(bad)
                self.assertIsNotNone(info, f"{nerr} errors should be correctable")
                self.assertEqual((info["nac"], info["duid"]), (NAC, DUID_LDU1),
                                 f"{nerr} errors mis-corrected")

    def test_random_words_are_rejected(self):
        # A garbage NID must not become a plausible NAC. ~0.55% of random 63-bit
        # words fall inside the code's correction radius; assert the gate holds
        # for the overwhelming majority (and the sync gate multiplies on top).
        rng = np.random.default_rng(3)
        rejected = 0
        for _ in range(200):
            word = int(rng.integers(0, 1 << 32)) | (int(rng.integers(0, 1 << 32)) << 32)
            if decode_nid(word) is None:
                rejected += 1
        self.assertGreater(rejected, 180, "BCH gate is letting noise through")


class TestBlockCodes(unittest.TestCase):
    def test_extended_golay_weight_distribution(self):
        # {0, 8, 12, 16, 24} uniquely identifies the extended binary Golay
        # [24,12,8] — an external anchor, not encoder/decoder self-consistency.
        weights = set()
        for d in range(1 << 12):
            weights.add(bin((d << 12) | p25._extended_golay24_parity(d)).count("1"))
        self.assertEqual(sorted(weights), [0, 8, 12, 16, 24])

    def test_golay18_properties(self):
        cb = p25.GOLAY18_CODEBOOK
        self.assertEqual(len(set(cb)), 64)
        self.assertEqual(min(bin(c).count("1") for c in cb[1:]), 8)
        for h in range(64):
            self.assertEqual(golay18_decode(golay18_encode(h)), (h, 0))

    def test_golay18_corrects_three_bit_errors(self):
        rng = np.random.default_rng(1)
        for h in range(64):
            for _ in range(4):
                w = golay18_encode(h)
                for b in rng.choice(18, size=3, replace=False):
                    w ^= 1 << int(b)
                got, ne = golay18_decode(w)
                self.assertEqual(got, h)
                self.assertEqual(ne, 3)

    def test_hamming10_properties(self):
        cb = p25.HAMMING10_CODEBOOK
        self.assertEqual(len(set(cb)), 64)
        self.assertEqual(min(bin(c).count("1") for c in cb[1:]), 3)
        for h in range(64):
            self.assertEqual(hamming10_decode(hamming10_encode(h)), (h, 0))
            for b in range(10):
                got, ne = hamming10_decode(hamming10_encode(h) ^ (1 << b))
                self.assertEqual(got, h)
                self.assertEqual(ne, 1)


class TestReedSolomon(unittest.TestCase):
    """The three P25 RS codes over GF(64), each proven to its full capability."""

    CODES = ((36, 20, 16), (24, 12, 12), (24, 16, 8))

    def test_roundtrip_and_full_correction(self):
        rng = np.random.default_rng(2)
        for (n, k, nroots) in self.CODES:
            t = nroots // 2
            with self.subTest(code=f"RS({n},{k})"):
                for _ in range(40):
                    msg = [int(v) for v in rng.integers(0, 64, k)]
                    cw = rs_encode(msg, nroots)
                    self.assertEqual(len(cw), n)
                    ok, dec, ne = rs_decode(cw, nroots)
                    self.assertTrue(ok)
                    self.assertEqual((dec, ne), (cw, 0))
                    bad = list(cw)
                    for pos in rng.choice(n, size=t, replace=False):
                        bad[int(pos)] ^= int(rng.integers(1, 64))
                    ok, dec, ne = rs_decode(bad, nroots)
                    self.assertTrue(ok, f"RS({n},{k}) failed at t={t}")
                    self.assertEqual(dec, cw)
                    self.assertEqual(ne, t)

    def test_beyond_capability_never_miscorrects_silently(self):
        # Past t errors the decoder must report failure. It may not always
        # detect (no code can), but it must never hand back a DIFFERENT word
        # while claiming success — that is how a fabricated talkgroup would get
        # into a client report.
        rng = np.random.default_rng(4)
        bad_success = 0
        for _ in range(200):
            msg = [int(v) for v in rng.integers(0, 64, 12)]
            cw = rs_encode(msg, 12)
            corrupt = list(cw)
            for pos in rng.choice(24, size=7, replace=False):
                corrupt[int(pos)] ^= int(rng.integers(1, 64))
            ok, dec, _ = rs_decode(corrupt, 12)
            if ok and dec != cw:
                bad_success += 1
        self.assertEqual(bad_success, 0)


class TestTrellisAndCRC(unittest.TestCase):
    def test_trellis_roundtrip(self):
        rng = np.random.default_rng(5)
        for _ in range(20):
            src = np.append(rng.integers(0, 4, 48).astype(np.uint8), 0)
            coded = trellis_half_encode(src)
            self.assertEqual(coded.size, 2 * src.size)
            got, err = trellis_half_decode(coded)
            self.assertEqual(list(got), list(src))
            self.assertEqual(err, 0)

    def test_trellis_free_distance_is_five(self):
        # Two paths that diverge and remerge differ in at least 5 output bits,
        # so the Viterbi fixes 2 bit errors per event. External check on the
        # state table, independent of the round trip.
        pc = p25._popcount
        nw = p25.TRELLIS_NEXT
        best = min(pc(nw[s][d] ^ nw[s][e]) + pc(nw[d][a] ^ nw[e][a])
                   for s in range(4) for d in range(4) for e in range(4)
                   for a in range(4) if d != e)
        self.assertEqual(best, 5)

    def test_trellis_corrects_bit_errors(self):
        rng = np.random.default_rng(6)
        src = np.append(rng.integers(0, 4, 48).astype(np.uint8), 0)
        bits = dibits_to_bits(trellis_half_encode(src))
        bits[7] ^= 1
        bits[101] ^= 1
        got, err = trellis_half_decode(bits_to_dibits(bits))
        self.assertEqual(list(got), list(src))
        self.assertGreater(err, 0)

    def test_tsbk_crc_gate(self):
        blk = make_tsbk(tgid=4321, source_id=555666, channel=0x100)
        full = blk + p25._int_to_bits(crc16_p25(blk), 16)
        parsed = p25.parse_tsbk(full)
        self.assertIsNotNone(parsed)
        self.assertEqual((parsed["tgid"], parsed["source_id"]), (4321, 555666))
        for flip in (0, 37, 79, 95):
            broken = list(full)
            broken[flip] ^= 1
            self.assertIsNone(p25.parse_tsbk(broken), f"CRC missed bit {flip}")


class TestFieldParsers(unittest.TestCase):
    def test_link_control_group_voice(self):
        bits = make_link_control(tgid=TGID, source_id=SRC, emergency=True,
                                 priority=5)
        lc = p25.parse_link_control(bits)
        self.assertEqual(lc["lco"], LCO_GROUP_VOICE)
        self.assertEqual((lc["tgid"], lc["source_id"]), (TGID, SRC))
        self.assertTrue(lc["emergency"])
        self.assertEqual(lc["priority"], 5)
        self.assertFalse(lc["protected_flag"])

    def test_link_control_unit_to_unit(self):
        lc = p25.parse_link_control(make_link_control(
            lco=LCO_UNIT_VOICE, source_id=SRC, target_id=4242,
            protected_flag=True))
        self.assertEqual(lc["source_id"], SRC)
        self.assertEqual(lc["target_id"], 4242)
        self.assertTrue(lc["protected_flag"])

    def test_encryption_sync(self):
        mi = bytes(range(1, 10))
        es = p25.parse_encryption_sync(make_encryption_sync(0x84, 0xBEEF, mi))
        self.assertEqual(es["algid"], 0x84)
        self.assertEqual(es["kid"], 0xBEEF)
        self.assertEqual(es["mi"], mi.hex())
        self.assertTrue(es["encrypted"])
        self.assertIn("AES-256", es["algid_name"])

    def test_hdu_header(self):
        hdr = p25.parse_hdu_header(make_hdu_header(algid=0x80, kid=0, tgid=TGID))
        self.assertEqual(hdr["tgid"], TGID)
        self.assertFalse(hdr["encrypted"])


class TestFrameDecode(unittest.TestCase):
    def test_clear_call_exact_recovery(self):
        d = encode_call(NAC, TGID, SRC, algid=ALGID_CLEAR)
        r = decode_p25_dibits(d)
        c = _only_call(r)
        self.assertEqual(c["nac"], NAC)
        self.assertEqual(c["nac_hex"], "0x293")
        self.assertEqual(c["tgid"], TGID)
        self.assertEqual(c["source_id"], SRC)
        self.assertEqual(c["algid"], ALGID_CLEAR)
        self.assertIs(c["encrypted"], False)
        self.assertEqual(c["duids"], ["HDU", "LDU1", "LDU2", "TDULC"])
        self.assertEqual(r["stats"]["frames"], 4)
        self.assertEqual(r["stats"]["frames_fec_ok"], 4)
        self.assertEqual(r["stats"]["nacs"], ["0x293"])
        self.assertIs(r["stats"]["encrypted"], False)

    def test_encrypted_call_is_a_result_not_a_failure(self):
        # The headline behaviour of this module. An AES-256 call must come back
        # as a DECODED CALL that names the algorithm — never as an empty result,
        # a failed decode, or silence.
        mi = bytes([0xDE, 0xAD, 0xBE, 0xEF, 0x01, 0x02, 0x03, 0x04, 0x05])
        d = encode_call(NAC, TGID, SRC, algid=0x84, kid=0x1234, mi=mi)
        r = decode_p25_dibits(d)
        self.assertTrue(r["calls"], "encrypted traffic must not decode to nothing")
        self.assertEqual(r["messages"], r["calls"])   # router-facing alias
        c = _only_call(r)
        self.assertIs(c["encrypted"], True)
        self.assertEqual(c["algid"], 0x84)
        self.assertIn("AES-256", c["algid_name"])
        self.assertEqual(c["kid"], 0x1234)
        self.assertEqual(c["mi"], mi.hex())
        self.assertEqual((c["tgid"], c["source_id"]), (TGID, SRC))
        self.assertIn("ENCRYPTED", c["summary"])
        self.assertIn("0x84", c["summary"])
        self.assertIn("talkgroup 1001", c["summary"])
        self.assertIs(r["stats"]["encrypted"], True)
        # and the FEC genuinely passed — this is not a partial/degraded read
        self.assertEqual(r["stats"]["frames_fec_ok"], r["stats"]["frames"])

    def test_algid_variants_named(self):
        for algid, needle in ((0x81, "DES-OFB"), (0x83, "Triple DES"),
                              (0x85, "AES-128"), (0x9F, "DES-XL"),
                              (0xAA, "ADP"), (0x5C, "unknown")):
            with self.subTest(algid=hex(algid)):
                r = decode_p25_dibits(encode_call(NAC, TGID, SRC, algid=algid))
                c = _only_call(r)
                self.assertIs(c["encrypted"], True)
                self.assertEqual(c["algid"], algid)
                self.assertIn(needle, c["algid_name"])

    def test_each_duid_round_trips(self):
        lc = make_link_control(tgid=TGID, source_id=SRC)
        es = make_encryption_sync(0x84, 0x1111, b"\x11" * 9)
        hdu = make_hdu_header(algid=0x84, kid=0x1111, tgid=TGID)
        cases = {
            "HDU": encode_frame_dibits(DUID_HDU, NAC, hdu_bits=hdu),
            "LDU1": encode_frame_dibits(DUID_LDU1, NAC, lc_bits=lc),
            "LDU2": encode_frame_dibits(DUID_LDU2, NAC, es_bits=es),
            "TDU": encode_frame_dibits(DUID_TDU, NAC),
            "TDULC": encode_frame_dibits(DUID_TDULC, NAC, lc_bits=lc),
        }
        for name, dib in cases.items():
            with self.subTest(duid=name):
                r = decode_p25_dibits(dib)
                self.assertEqual(r["stats"]["frames"], 1)
                f = r["frames"][0]
                self.assertEqual(f["duid_name"], name)
                self.assertEqual(f["nac"], NAC)
                self.assertEqual(f["nid_bit_errors"], 0)
                self.assertTrue(f["nid_parity_ok"])
                self.assertTrue(f["fec_ok"], f"{name} payload FEC failed")
                if name in ("LDU1", "TDULC"):
                    self.assertEqual((f["tgid"], f["source_id"]), (TGID, SRC))
                if name == "LDU2":
                    self.assertEqual(f["algid"], 0x84)
                if name == "HDU":
                    self.assertEqual(f["tgid"], TGID)

    def test_unit_to_unit_call(self):
        lc = make_link_control(lco=LCO_UNIT_VOICE, source_id=SRC, target_id=4242)
        r = decode_p25_dibits(encode_frame_dibits(DUID_LDU1, NAC, lc_bits=lc))
        c = _only_call(r)
        self.assertEqual(c["source_id"], SRC)
        self.assertEqual(c["target_id"], 4242)
        self.assertIn("radio ID 9114712", c["summary"])

    def test_status_symbols_are_stripped(self):
        d = encode_call(0x4D2, 777, 12345, algid=ALGID_CLEAR, status=True)
        r = decode_p25_dibits(d)
        c = _only_call(r)
        self.assertTrue(r["stats"]["status_symbols"])
        self.assertEqual((c["nac"], c["tgid"], c["source_id"]), (0x4D2, 777, 12345))
        self.assertEqual(r["stats"]["frames_fec_ok"], 4)

    def test_status_symbol_phase_is_autodetected(self):
        # The same call, with and without status symbols, must decode identically
        # without the caller telling the decoder which stream it has.
        plain = decode_p25_dibits(encode_call(NAC, TGID, SRC, status=False))
        wire = decode_p25_dibits(encode_call(NAC, TGID, SRC, status=True))
        for key in ("nac", "tgid", "source_id", "algid", "encrypted"):
            self.assertEqual(_only_call(plain)[key], _only_call(wire)[key], key)
        self.assertFalse(plain["stats"]["status_symbols"])
        self.assertTrue(wire["stats"]["status_symbols"])

    def test_tsdu_channel_grant(self):
        d = encode_frame_dibits(DUID_TSDU, 0x0AB,
                                tsbks=[make_tsbk(tgid=4321, source_id=555666,
                                                 channel=0x100)])
        r = decode_p25_dibits(d)
        self.assertEqual(r["stats"]["frames"], 1)
        c = _only_call(r)
        self.assertEqual(c["nac"], 0x0AB)
        self.assertEqual((c["tgid"], c["source_id"]), (4321, 555666))
        self.assertEqual(c["channel"], 0x100)
        self.assertEqual(c["duids"], ["TSDU"])

    def test_tsdu_multiple_blocks(self):
        blocks = [make_tsbk(tgid=100, source_id=11, last_block=False),
                  make_tsbk(tgid=200, source_id=22, last_block=False),
                  make_tsbk(tgid=300, source_id=33, last_block=True)]
        r = decode_p25_dibits(encode_frame_dibits(DUID_TSDU, NAC, tsbks=blocks))
        self.assertEqual(len(r["frames"][0]["tsbks"]), 3)
        self.assertEqual([c["tgid"] for c in r["calls"]], [100, 200, 300])

    def test_tsdu_does_not_swallow_the_next_frame(self):
        # A single-block TSDU is shorter than the 3-block maximum; the scanner
        # must not skip past a following frame's sync.
        d = np.concatenate([
            encode_frame_dibits(DUID_TSDU, NAC, tsbks=[make_tsbk(tgid=7, source_id=8)]),
            encode_frame_dibits(DUID_LDU1, NAC,
                                lc_bits=make_link_control(tgid=TGID, source_id=SRC))])
        r = decode_p25_dibits(d)
        self.assertEqual([f["duid_name"] for f in r["frames"]], ["TSDU", "LDU1"])

    def test_voice_frames_handed_over_not_decoded(self):
        # We stop at the voice BITS — the vocoder is another module's job.
        pattern = [[(i * 37 + k) % 2 for k in range(144)] for i in range(9)]
        d = encode_frame_dibits(DUID_LDU1, NAC, voice=pattern,
                                lc_bits=make_link_control(tgid=TGID, source_id=SRC))
        f = decode_p25_dibits(d)["frames"][0]
        self.assertEqual(f["voice_frame_count"], 9)
        self.assertEqual(len(f["voice_bits"]), 9)
        for i, vf in enumerate(f["voice_bits"]):
            self.assertEqual(vf.size, 144)
            self.assertEqual(list(vf), pattern[i], f"voice frame {i} corrupted")
        self.assertNotIn("audio", f)

    def test_keep_voice_false_suppresses_bits(self):
        d = encode_frame_dibits(DUID_LDU1, NAC,
                                lc_bits=make_link_control(tgid=TGID, source_id=SRC))
        f = decode_p25_dibits(d, keep_voice=False)["frames"][0]
        self.assertNotIn("voice_bits", f)
        self.assertEqual(f["voice_frame_count"], 9)

    def test_mid_call_capture_without_hdu(self):
        # The normal field case: the sweep starts after the call did.
        lc = make_link_control(tgid=TGID, source_id=SRC)
        es = make_encryption_sync(0xAA, 0x2222, b"\xAA" * 9)
        d = np.concatenate([encode_frame_dibits(DUID_LDU1, NAC, lc_bits=lc),
                            encode_frame_dibits(DUID_LDU2, NAC, es_bits=es)])
        c = _only_call(decode_p25_dibits(d))
        self.assertEqual((c["tgid"], c["source_id"]), (TGID, SRC))
        self.assertIs(c["encrypted"], True)
        self.assertIn("ADP", c["algid_name"])

    def test_two_calls_in_one_capture(self):
        d = np.concatenate([encode_call(NAC, TGID, SRC, algid=ALGID_CLEAR),
                            encode_call(NAC, 2002, 7654321, algid=0x84)])
        r = decode_p25_dibits(d)
        self.assertEqual(len(r["calls"]), 2)
        self.assertEqual([c["tgid"] for c in r["calls"]], [TGID, 2002])
        self.assertEqual([c["encrypted"] for c in r["calls"]], [False, True])

    def test_tdu_terminates_a_call(self):
        d = np.concatenate([
            encode_frame_dibits(DUID_LDU1, NAC,
                                lc_bits=make_link_control(tgid=TGID, source_id=SRC)),
            encode_frame_dibits(DUID_TDU, NAC),
            encode_frame_dibits(DUID_LDU1, NAC,
                                lc_bits=make_link_control(tgid=555, source_id=1))])
        r = decode_p25_dibits(d)
        self.assertEqual([c["tgid"] for c in r["calls"]], [TGID, 555])


class TestErrorCorrectionInChain(unittest.TestCase):
    def test_nid_bit_errors_corrected_end_to_end(self):
        rng = np.random.default_rng(11)
        base = encode_call(NAC, TGID, SRC, algid=0x84, kid=7)
        bits = dibits_to_bits(base)
        for pos in rng.choice(np.arange(48, 112), size=NID_MAX_ERRS, replace=False):
            bits[int(pos)] ^= 1                    # 11 errors in the HDU's NID
        r = decode_p25_dibits(bits_to_dibits(bits))
        hdu = _frames_by_duid(r)["HDU"]
        self.assertEqual(hdu["nac"], NAC)
        self.assertEqual(hdu["nid_bit_errors"], NID_MAX_ERRS)
        self.assertEqual(_only_call(r)["algid"], 0x84)

    def test_link_control_symbol_errors_corrected(self):
        # Corrupt whole hexbits in the LDU1 LC block: Hamming absorbs some, RS
        # repairs the rest. Exact talkgroup and source must still come out.
        lc = make_link_control(tgid=TGID, source_id=SRC)
        d = encode_frame_dibits(DUID_LDU1, NAC, lc_bits=lc)
        bits = dibits_to_bits(d)
        # LC bits live in the six 40-bit chunks; hit the first chunk hard.
        start = 48 + 64 + 144 + 144            # sync + NID + two voice frames
        rng = np.random.default_rng(12)
        for pos in rng.choice(40, size=8, replace=False):
            bits[start + int(pos)] ^= 1
        f = decode_p25_dibits(bits_to_dibits(bits))["frames"][0]
        self.assertTrue(f["fec_ok"], "RS should have repaired the LC block")
        self.assertEqual((f["tgid"], f["source_id"]), (TGID, SRC))
        self.assertGreater(f["fec"]["block_bit_errors"], 0)

    def test_encryption_sync_survives_symbol_errors(self):
        es = make_encryption_sync(0x84, 0xABCD, b"\x0F" * 9)
        d = encode_frame_dibits(DUID_LDU2, NAC, es_bits=es)
        bits = dibits_to_bits(d)
        start = 48 + 64 + 144 + 144
        for pos in (1, 5, 12, 19, 27, 33):
            bits[start + pos] ^= 1
        f = decode_p25_dibits(bits_to_dibits(bits))["frames"][0]
        self.assertTrue(f["fec_ok"])
        self.assertEqual(f["algid"], 0x84)
        self.assertEqual(f["kid"], 0xABCD)

    def test_shredded_payload_reports_nac_and_fails_honestly(self):
        # Destroy the LC block outright. The anchored legs (sync + NID) still
        # give NAC and DUID; the payload must report failure rather than invent
        # a talkgroup.
        lc = make_link_control(tgid=TGID, source_id=SRC)
        d = encode_frame_dibits(DUID_LDU1, NAC, lc_bits=lc)
        bits = dibits_to_bits(d)
        rng = np.random.default_rng(13)
        start = 48 + 64
        for pos in rng.choice(np.arange(start, bits.size), size=400, replace=False):
            bits[int(pos)] ^= 1
        r = decode_p25_dibits(bits_to_dibits(bits))
        f = r["frames"][0]
        self.assertEqual(f["nac"], NAC)
        self.assertEqual(f["duid_name"], "LDU1")
        self.assertFalse(f["fec_ok"])
        self.assertIsNone(f["tgid"])
        self.assertIsNone(f["source_id"])
        c = _only_call(r)
        self.assertIsNone(c["tgid"])
        self.assertIsNone(c["encrypted"])
        self.assertIn("unknown", c["summary"])

    def test_truncated_capture_is_not_a_decode(self):
        d = encode_call(NAC, TGID, SRC, algid=0x84)
        r = decode_p25_dibits(d[:80])              # sync + NID + a sliver
        self.assertEqual(r["stats"]["frames"], 1)
        f = r["frames"][0]
        self.assertEqual(f["nac"], NAC)
        self.assertTrue(f.get("truncated"))
        self.assertFalse(f["fec_ok"])
        self.assertIsNone(f["tgid"])


class TestNegatives(unittest.TestCase):
    """False positives are worse than misses. Nothing here may produce a frame."""

    def test_random_dibits_never_sync(self):
        for seed in (1, 2, 3, 4, 5, 6):
            with self.subTest(seed=seed):
                rng = np.random.default_rng(seed)
                noise = rng.integers(0, 4, 120000).astype(np.uint8)
                r = decode_p25_dibits(noise)
                self.assertEqual(r["frames"], [])
                self.assertEqual(r["calls"], [])
                self.assertEqual(r["messages"], [])
                self.assertFalse(r["stats"]["sync"])

    def test_analog_fm_derived_stream_never_syncs(self):
        # What a C4FM slicer produces when it is pointed at an ANALOG FM voice
        # carrier: a slowly-varying, heavily-correlated 4-level stream. It must
        # not manufacture a P25 system — that is the exact mislabel the ledger
        # cannot afford.
        for seed in (5, 9, 17):
            with self.subTest(seed=seed):
                r = decode_p25_dibits(_analog_fm_dibits(seed=seed))
                self.assertEqual(r["frames"], [])
                self.assertEqual(r["calls"], [])
                self.assertIn("no P25 frame sync", r["note"])

    def test_two_fsk_paging_stream_never_syncs(self):
        # A POCSAG-like NRZ stream read two bits at a time: alternating preamble
        # plus BCH codewords. Structured, but not P25.
        rng = np.random.default_rng(21)
        bits = list([1, 0] * 288)
        for _ in range(40):
            w = int(rng.integers(0, 1 << 32))
            bits += [(w >> k) & 1 for k in range(31, -1, -1)]
        r = decode_p25_dibits(bits_to_dibits(np.array(bits, dtype=np.uint8)))
        self.assertEqual(r["calls"], [])

    def test_constant_streams_never_sync(self):
        for value in (0, 1, 2, 3):
            with self.subTest(dibit=value):
                r = decode_p25_dibits(np.full(20000, value, dtype=np.uint8))
                self.assertEqual(r["calls"], [])
                self.assertEqual(r["frames"], [])

    def test_planted_sync_without_a_valid_nid_is_rejected(self):
        # The sync gate alone is not enough, and this proves the BCH gate is
        # doing independent work: a perfect sync followed by a NID that is not
        # near any codeword yields NO frame.
        rng = np.random.default_rng(31)
        for _ in range(50):
            nid = int(rng.integers(0, 1 << 32)) | (int(rng.integers(0, 1 << 32)) << 32)
            if decode_nid(nid) is None:
                break
        else:
            self.fail("could not find a NID outside the correction radius")
        bits = p25._int_to_bits(FRAME_SYNC, 48) + p25._int_to_bits(nid, 64)
        stream = np.concatenate([
            rng.integers(0, 4, 500).astype(np.uint8),
            bits_to_dibits(np.array(bits, dtype=np.uint8)),
            rng.integers(0, 4, 500).astype(np.uint8)])
        r = decode_p25_dibits(stream)
        self.assertEqual(r["frames"], [])
        self.assertEqual(r["calls"], [])
        self.assertIn("no NID passed BCH", r["note"])

    def test_empty_and_invalid_input(self):
        self.assertEqual(decode_p25_dibits(np.zeros(0, dtype=np.uint8))["calls"], [])
        with self.assertRaises(ValueError):
            decode_p25_dibits(np.array([0, 1, 2, 7], dtype=np.uint8))

    def test_sync_tolerance_does_not_slide(self):
        # A sync with more than the allowed bit errors must not lock.
        d = encode_call(NAC, TGID, SRC)
        bits = dibits_to_bits(d)
        for pos in (0, 3, 7, 11, 15, 19, 23, 27):        # 8 bit errors in the FS
            bits[pos] ^= 1
        r = decode_p25_dibits(bits_to_dibits(bits))
        self.assertNotIn("HDU", _frames_by_duid(r))      # first frame is gone


class TestGate(unittest.TestCase):
    def test_looks_like_p25(self):
        self.assertTrue(looks_like_p25(
            {"modulation_class": "fsk", "fsk_states": 4, "fsk_shift_khz": 3.6,
             "symbol_rate_kbd": 4.8, "occupied_bw_khz": 12.5}))
        # noisy 4-level eye that measured as 2-level still passes the pre-gate
        self.assertTrue(looks_like_p25(
            {"modulation_class": "fsk", "fsk_states": 2, "fsk_shift_khz": 1.8,
             "symbol_rate_kbd": 4.6, "occupied_bw_khz": 11.0}))
        # POCSAG-rate paging must NOT be routed to P25
        self.assertFalse(looks_like_p25(
            {"modulation_class": "fsk", "fsk_states": 2, "fsk_shift_khz": 9.0,
             "symbol_rate_kbd": 1.2, "occupied_bw_khz": 20.0}))
        self.assertFalse(looks_like_p25(
            {"modulation_class": "ofdm_wideband", "fsk_states": 1,
             "fsk_shift_khz": 0.0, "symbol_rate_kbd": 0.0,
             "occupied_bw_khz": 20000.0}))
        self.assertFalse(looks_like_p25({}))


class TestIQEntryPoint(unittest.TestCase):
    """decode_p25(iq, fs) delegates C4FM symbol recovery to survey/c4fm.py via a
    LAZY import, so this module works standalone whether or not c4fm exists."""

    def test_too_short_raises(self):
        with self.assertRaises(ValueError):
            decode_p25(np.zeros(8, dtype=np.complex64), 48000.0)

    def test_noise_iq_is_an_honest_empty_result_not_a_crash(self):
        rng = np.random.default_rng(41)
        iq = (rng.standard_normal(4096) + 1j * rng.standard_normal(4096)).astype(np.complex64)
        r = decode_p25(iq, 48000.0)                  # must not raise either way
        self.assertEqual(r["protocol"], "P25")
        self.assertIsInstance(r["messages"], list)
        self.assertEqual(r["messages"], [])
        try:
            from survey import c4fm                  # noqa: F401
        except ImportError:
            self.assertIn("c4fm", r["note"])         # honest about WHY, no crash

    def test_misbehaving_c4fm_degrades_honestly(self):
        """The adapter crosses a module boundary we do not own. If c4fm changes
        shape, this decoder must return an EMPTY result naming the reason — never
        raise out of a live sweep. Each stand-in below is a shape that would have
        reached a numpy coercion in a trusting adapter."""
        import survey

        class _Stub:
            def __init__(self, payload):
                self._payload = payload

            def demod_c4fm(self, iq, fs):
                return self._payload

        rng = np.random.default_rng(43)
        iq = (rng.standard_normal(2048) + 1j * rng.standard_normal(2048)).astype(np.complex64)
        cases = {
            "dict with no dibits key": {"note": "nothing here"},
            "dict whose dibits is a dict": {"dibits": {"not": "an array"}, "quality": 1.0},
            "bare dict return": {"quality": 0.5},
            "a string": "definitely not dibits",
            "a 2-D array": np.zeros((4, 4), dtype=np.uint8),
            "out-of-range symbols": np.array([0, 1, 2, 9], dtype=np.uint8),
            "float non-integer symbols": np.array([0.0, 1.5, 2.0], dtype=np.float64),
            "an empty stream": np.zeros(0, dtype=np.uint8),
        }
        original = getattr(survey, "c4fm", None)
        try:
            for label, payload in cases.items():
                with self.subTest(shape=label):
                    survey.c4fm = _Stub(payload)
                    r = decode_p25(iq, 48000.0)      # must not raise
                    self.assertEqual(r["protocol"], "P25")
                    self.assertEqual(r["messages"], [])
                    self.assertEqual(r["calls"], [])
                    self.assertTrue(r["note"], "an empty result must say why")
        finally:
            if original is not None:
                survey.c4fm = original
            else:                                                  # pragma: no cover
                delattr(survey, "c4fm")

    def test_c4fm_exception_becomes_a_note_not_a_crash(self):
        import survey

        class _Boom:
            def demod_c4fm(self, iq, fs):
                raise RuntimeError("libiio exploded")

        original = getattr(survey, "c4fm", None)
        try:
            survey.c4fm = _Boom()
            r = decode_p25(np.zeros(1024, dtype=np.complex64), 48000.0)
            self.assertEqual(r["messages"], [])
            self.assertIn("libiio exploded", r["note"])
        finally:
            if original is not None:
                survey.c4fm = original
            else:                                                  # pragma: no cover
                delattr(survey, "c4fm")

    def test_end_to_end_through_c4fm_iq(self):
        """The real path once survey/c4fm.py lands: P25 frames -> C4FM IQ ->
        c4fm symbol recovery -> this frame layer. Skipped (not failed) if the
        c4fm synth API is not present, since that module is owned elsewhere."""
        try:
            from survey import c4fm
        except ImportError:                                        # pragma: no cover
            self.skipTest("survey/c4fm.py not available yet")
        if not hasattr(c4fm, "encode_c4fm"):                       # pragma: no cover
            self.skipTest("c4fm.encode_c4fm() not available")
        fs = 48000.0
        d = encode_call(NAC, TGID, SRC, algid=0x84, kid=0x1234,
                        mi=bytes(range(9)), status=True)
        iq = c4fm.encode_c4fm(d, fs)
        r = decode_p25(iq, fs)
        self.assertTrue(r["calls"], f"end-to-end decode produced nothing: {r['note']}")
        c = r["calls"][0]
        self.assertEqual((c["nac"], c["tgid"], c["source_id"]), (NAC, TGID, SRC))
        self.assertIs(c["encrypted"], True)
        self.assertEqual(c["algid"], 0x84)
        self.assertEqual(c["kid"], 0x1234)

    def test_end_to_end_survives_inverted_polarity(self):
        """A flipped LO offset inverts the C4FM rails. The frame layer must still
        lock — polarity is a demod artefact, not a decode failure."""
        try:
            from survey import c4fm
        except ImportError:                                        # pragma: no cover
            self.skipTest("survey/c4fm.py not available yet")
        if not hasattr(c4fm, "encode_c4fm"):                       # pragma: no cover
            self.skipTest("c4fm.encode_c4fm() not available")
        fs = 48000.0
        d = encode_call(NAC, TGID, SRC, algid=ALGID_CLEAR)
        iq = c4fm.encode_c4fm(d, fs, invert=True)
        r = decode_p25(iq, fs)
        self.assertTrue(r["calls"], f"inverted decode produced nothing: {r['note']}")
        self.assertEqual(r["calls"][0]["tgid"], TGID)


def _analog_fm_dibits(seconds: float = 1.2, fs: float = 48000.0, seed: int = 5):
    """Analog FM voice -> the 4-level symbol stream a C4FM slicer would emit.

    Built from the app's own FM discriminator so the negative test exercises a
    realistic slicer output (correlated, audio-shaped) rather than white noise."""
    from survey.demod import _fm_discriminator
    rng = np.random.default_rng(seed)
    n = int(fs * seconds)
    t = np.arange(n) / fs
    audio = (0.6 * np.sin(2 * np.pi * 440.0 * t) +
             0.3 * np.sin(2 * np.pi * 1180.0 * t) +
             0.15 * np.sin(2 * np.pi * 97.0 * t) +
             0.08 * rng.standard_normal(n))
    iq = np.exp(1j * 2 * np.pi * np.cumsum(2500.0 * audio) / fs).astype(np.complex64)
    base = _fm_discriminator(iq).astype(np.float64)
    sps = fs / 4800.0
    idx = (np.arange(int(base.size / sps)) * sps).round().astype(int)
    samp = base[idx[idx < base.size]]
    samp = samp - 0.5 * (np.percentile(samp, 90) + np.percentile(samp, 10))
    outer = float(np.percentile(np.abs(samp), 90)) or 1.0
    rails = np.array([2.0, 1.0, -1.0, -2.0]) * (outer / 2.0)
    return np.argmin(np.abs(samp[:, None] - rails[None, :]), axis=1).astype(np.uint8)


if __name__ == "__main__":
    unittest.main()
