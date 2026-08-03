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


# --------------------------------------------------------------------------
# TIA-102.BAAA-A voice interleave — SECOND SOURCE.
#
# survey/p25.py transcribes the permutation from section 5.3.1, Table 5-1
# ("Interleaving Schedule for Voice Word"). What follows is the SAME permutation
# taken from a physically different table in the same standard: the transmit-order
# annex, sections 10.3 and 10.4, which lists every symbol of LDU1 and LDU2 one at
# a time. All 18 voice frames of a superframe are listed there and all 18 carry
# this identical schedule. Two independent tables agreeing entry-for-entry is the
# closest thing to external ground truth available without an off-air capture.
#
# Read as: 72 transmitted symbols in order, each "<Bit 1 label> <Bit 0 label>".
_ANNEX_ROWS = (
    "c_0(22) c_1(21)  c_2(20) c_3(19)  c_4(10) c_5(1)",
    "c_1(20) c_0(21)  c_3(18) c_2(19)  c_5(0) c_4(9)",
    "c_0(20) c_1(19)  c_2(18) c_3(17)  c_4(8) c_6(14)",
    "c_1(18) c_0(19)  c_3(16) c_2(17)  c_6(13) c_4(7)",
    "c_0(18) c_1(17)  c_2(16) c_3(15)  c_4(6) c_6(12)",
    "c_1(16) c_0(17)  c_3(14) c_2(15)  c_6(11) c_4(5)",
    "c_0(16) c_1(15)  c_2(14) c_3(13)  c_4(4) c_6(10)",
    "c_1(14) c_0(15)  c_3(12) c_2(13)  c_6(9) c_4(3)",
    "c_0(14) c_1(13)  c_2(12) c_3(11)  c_4(2) c_6(8)",
    "c_1(12) c_0(13)  c_3(10) c_2(11)  c_6(7) c_4(1)",
    "c_0(12) c_1(11)  c_2(10) c_3(9)  c_4(0) c_6(6)",
    "c_1(10) c_0(11)  c_3(8) c_2(9)  c_6(5) c_5(14)",
    "c_0(10) c_1(9)  c_2(8) c_3(7)  c_5(13) c_6(4)",
    "c_1(8) c_0(9)  c_3(6) c_2(7)  c_6(3) c_5(12)",
    "c_0(8) c_1(7)  c_2(6) c_3(5)  c_5(11) c_6(2)",
    "c_1(6) c_0(7)  c_3(4) c_2(5)  c_6(1) c_5(10)",
    "c_0(6) c_1(5)  c_2(4) c_3(3)  c_5(9) c_6(0)",
    "c_1(4) c_0(5)  c_3(2) c_2(3)  c_7(6) c_5(8)",
    "c_0(4) c_1(3)  c_2(2) c_3(1)  c_5(7) c_7(5)",
    "c_1(2) c_0(3)  c_3(0) c_2(1)  c_7(4) c_5(6)",
    "c_0(2) c_1(1)  c_2(0) c_4(14)  c_5(5) c_7(3)",
    "c_1(0) c_0(1)  c_4(13) c_3(22)  c_7(2) c_5(4)",
    "c_0(0) c_2(22)  c_3(21) c_4(12)  c_5(3) c_7(1)",
    "c_2(21) c_1(22)  c_4(11) c_3(20)  c_7(0) c_5(2)",
)


def _annex_labels():
    """The annex rows -> 144 (code word, bit index) pairs in transmitted order."""
    import re
    out = []
    for row in _ANNEX_ROWS:
        for lbl in row.split():
            m = re.fullmatch(r"c_(\d)\((\d+)\)", lbl)
            assert m, f"bad label {lbl!r}"
            out.append((int(m.group(1)), int(m.group(2))))
    assert len(out) == 144
    return out


def _reference_permutation():
    """Wire index -> vector-order index, built HERE from the annex labels.

    Deliberately a second implementation, written from the standard's own
    numbering rather than by calling anything in survey/p25.py, so it can be used
    to check p25 without asking p25 to check itself."""
    widths = (23, 23, 23, 23, 15, 15, 15, 7)
    offsets, pos = [], 0
    for w in widths:
        offsets.append(pos)
        pos += w
    perm = [0] * 144
    for wire, (cw, bit) in enumerate(_annex_labels()):
        perm[wire] = offsets[cw] + (widths[cw] - 1 - bit)
    return perm


def _reference_interleave(vector_bits):
    """Vector-ordered 144 bits -> wire order, using only the reference table."""
    perm = _reference_permutation()
    return [int(vector_bits[perm[w]]) for w in range(144)]


def _reference_deinterleave(wire_bits):
    """Wire-order 144 bits -> vector order, using only the reference table."""
    perm = _reference_permutation()
    out = [0] * 144
    for w in range(144):
        out[perm[w]] = int(wire_bits[w])
    return out


class TestVoiceInterleave(unittest.TestCase):
    """The transport de-interleave — TIA-102.BAAA-A section 5.3.1.

    This is the shortfall that a symmetric round trip physically cannot catch:
    our encoder and our decoder agreed with each other while both disagreed with
    every real transmitter. So none of these tests is a round trip through
    p25.encode_frame_dibits(). Each one measures the permutation against the
    standard, against a second implementation written here, or against a
    structural property that only the correct table has."""

    # ---- ground truth ----------------------------------------------------
    def test_table_matches_the_standards_second_table(self):
        """Table 5-1 (what p25.py transcribes) == annex 10.3/10.4 (transcribed
        here). A single-entry transposition in either copy fails this."""
        self.assertEqual(list(p25.VOICE_INTERLEAVE_TABLE), _annex_labels())

    def test_permutation_matches_the_reference_implementation(self):
        self.assertEqual(list(p25.VOICE_DEINTERLEAVE), _reference_permutation())

    def test_code_word_widths_are_the_standards(self):
        # 4x(23,12,7) Golay + 3x(15,11,3) Hamming + 7 bare = 144.
        self.assertEqual(p25.IMBE_CODEWORD_WIDTHS,
                         (23, 23, 23, 23, 15, 15, 15, 7))
        self.assertEqual(sum(p25.IMBE_CODEWORD_WIDTHS), p25.VOICE_FRAME_BITS)

    # ---- it is a permutation, and a NON-TRIVIAL one ----------------------
    def test_is_a_bijection(self):
        perm = list(p25.VOICE_DEINTERLEAVE)
        self.assertEqual(len(perm), 144)
        self.assertEqual(sorted(perm), list(range(144)),
                         "de-interleave is not a bijection — bits are being "
                         "dropped or duplicated")

    def test_is_not_the_identity(self):
        """Guards the failure mode where someone 'implements' the interleave as
        a no-op and every symmetric test still passes."""
        self.assertNotEqual(list(p25.VOICE_DEINTERLEAVE), list(range(144)))
        moved = sum(1 for i, v in enumerate(p25.VOICE_DEINTERLEAVE) if i != v)
        self.assertGreater(moved, 130, "almost nothing actually moves")

    def test_every_code_word_bit_appears_exactly_once(self):
        seen = {}
        for cw, bit in p25.VOICE_INTERLEAVE_TABLE:
            self.assertLess(bit, p25.IMBE_CODEWORD_WIDTHS[cw],
                            f"c_{cw}({bit}) is out of range for its code word")
            self.assertNotIn((cw, bit), seen, f"c_{cw}({bit}) transmitted twice")
            seen[(cw, bit)] = True
        self.assertEqual(len(seen), 144)
        for cw, width in enumerate(p25.IMBE_CODEWORD_WIDTHS):
            got = sum(1 for c, _ in seen if c == cw)
            self.assertEqual(got, width, f"c_{cw} has {got} bits, expected {width}")

    def test_interleave_and_deinterleave_are_true_inverses(self):
        rng = np.random.default_rng(7)
        for _ in range(50):
            v = rng.integers(0, 2, 144).astype(np.uint8)
            wire = p25.interleave_voice_frame(v)
            self.assertEqual(list(p25.deinterleave_voice_frame(wire)), list(v))

    def test_wrong_length_is_rejected(self):
        for n in (0, 88, 143, 145):
            with self.assertRaises(ValueError):
                p25.deinterleave_voice_frame(np.zeros(n, dtype=np.uint8))
            with self.assertRaises(ValueError):
                p25.interleave_voice_frame(np.zeros(n, dtype=np.uint8))

    # ---- the SEPARATION property: the entire point of interleaving -------
    def test_minimum_separation_is_three_symbols(self):
        """TIA-102.BAAA-A states the design property in prose: 'The minimum
        separation between any two bits of the same error correction code is 3
        symbols.' Reproducing a stated property is real evidence — a mis-copied
        table would almost certainly break it."""
        where = {}
        for wire, (cw, _) in enumerate(p25.VOICE_INTERLEAVE_TABLE):
            where.setdefault(cw, []).append(wire)
        for cw, wires in sorted(where.items()):
            gaps_sym = [(b // 2) - (a // 2) for a, b in zip(wires, wires[1:])]
            gaps_bit = [b - a for a, b in zip(wires, wires[1:])]
            self.assertEqual(min(gaps_sym), 3,
                             f"c_{cw}: min symbol separation {min(gaps_sym)}, "
                             "the standard says 3")
            self.assertEqual(min(gaps_bit), 5, f"c_{cw}: min bit separation")

    def test_a_burst_of_channel_errors_is_spread_across_code_words(self):
        """The measurable payoff, and the asymmetric part: compare what a burst
        does WITH the de-interleave against what it does WITHOUT it.

        Golay(23,12,7) corrects 3 bit errors, Hamming(15,11,3) corrects 1. A
        contiguous fade on the wire must not exceed those budgets."""
        owner = [cw for cw, _ in p25.VOICE_INTERLEAVE_TABLE]

        def worst(burst, codes):
            return max(max([0] + [sum(1 for t in range(s, s + burst)
                                      if owner[t] == c) for c in codes])
                       for s in range(0, 144 - burst + 1))

        golay, hamming = range(4), range(4, 7)
        # Every protected code word survives any burst up to 5 bits: at most one
        # error each, which even the Hamming words correct.
        self.assertEqual(worst(5, range(7)), 1)
        # All four Golay words still correct a burst as long as 17 bits.
        self.assertEqual(worst(17, golay), 3)
        self.assertGreater(worst(18, golay), 3)      # and 18 is genuinely too far
        # Hamming words (t=1) run out at 6 — the honest limit, asserted not hidden.
        self.assertEqual(worst(5, hamming), 1)
        self.assertEqual(worst(6, hamming), 2)

        # WITHOUT the de-interleave every one of those bits belongs to the same
        # code word, so the identical fade is fatal. This is the whole point.
        for burst in (6, 12, 17):
            flat = [min(burst, 23)]
            self.assertGreater(flat[0], worst(burst, golay),
                               "de-interleaving is not actually spreading errors")
        self.assertEqual(min(17, 23), 17)            # un-interleaved: 17 in one word
        self.assertEqual(worst(17, golay), 3)        # interleaved: 3

    # ---- asymmetric: each side checked against the reference, not each other
    def test_externally_interleaved_frames_deinterleave_to_the_original(self):
        """Apply a KNOWN interleave (built here from the standard's labels) to a
        frame, then require p25 to recover it. p25's own encoder is never called.

        144 one-hot probes localise a fault to a single bit position rather than
        just saying 'something moved'."""
        for pos in range(144):
            vector = np.zeros(144, dtype=np.uint8)
            vector[pos] = 1
            wire = _reference_interleave(vector)
            got = p25.deinterleave_voice_frame(np.array(wire, dtype=np.uint8))
            self.assertEqual(list(got), list(vector),
                             f"bit at vector position {pos} was not recovered")

    def test_reference_deinterleave_agrees_on_random_frames(self):
        rng = np.random.default_rng(11)
        for _ in range(40):
            wire = rng.integers(0, 2, 144).astype(np.uint8)
            self.assertEqual(list(p25.deinterleave_voice_frame(wire)),
                             _reference_deinterleave(wire))
            vector = rng.integers(0, 2, 144).astype(np.uint8)
            self.assertEqual(list(p25.interleave_voice_frame(vector)),
                             _reference_interleave(vector))

    def test_encoder_puts_interleaved_bits_on_the_wire(self):
        """Encode side, checked against the reference permutation — NOT against
        our decoder. Pulls the nine voice regions straight out of the emitted
        dibits and compares them with the reference interleave."""
        rng = np.random.default_rng(3)
        frames = [rng.integers(0, 2, 144).astype(np.uint8) for _ in range(9)]
        d = encode_frame_dibits(DUID_LDU1, NAC, voice=frames,
                                lc_bits=make_link_control(tgid=TGID, source_id=SRC))
        bits = list(dibits_to_bits(d))
        payload = bits[48 + 64:]
        pos, vi = 0, 0
        for kind, nbits in p25.LDU_LAYOUT:
            if kind == "V":
                on_air = payload[pos:pos + nbits]
                self.assertEqual(on_air, _reference_interleave(frames[vi]),
                                 f"voice frame {vi} is not interleaved on the wire")
                # and it must NOT be the raw vector order
                self.assertNotEqual(on_air, list(frames[vi]))
                vi += 1
            pos += nbits
        self.assertEqual(vi, 9)

    def test_decoder_deinterleaves_a_stream_it_did_not_encode(self):
        """Decode side. The wire stream is assembled HERE: the voice regions are
        laid out with the reference interleave, so nothing in p25 chose their
        positions. The decoder must hand back vector order."""
        rng = np.random.default_rng(13)
        frames = [rng.integers(0, 2, 144).astype(np.uint8) for _ in range(9)]
        lc = make_link_control(tgid=TGID, source_id=SRC)
        ctrl = p25._rs_block_encode(lc, hamming10_encode, 10, 12)

        bits = p25._int_to_bits(FRAME_SYNC, 48)
        bits += p25._int_to_bits(nid_encode(NAC, DUID_LDU1), 64)
        vi = ci = 0
        for kind, nbits in p25.LDU_LAYOUT:
            if kind == "V":
                bits += _reference_interleave(frames[vi])
                vi += 1
            elif kind == "C":
                bits += ctrl[ci:ci + nbits]
                ci += nbits
            else:
                bits += [0] * nbits

        r = decode_p25_dibits(bits_to_dibits(bits))
        f = _frames_by_duid(r)["LDU1"]
        self.assertTrue(f["fec_ok"])
        self.assertEqual(f["tgid"], TGID)
        self.assertEqual(len(f["voice_bits"]), 9)
        for i, (got, want) in enumerate(zip(f["voice_bits"], frames)):
            self.assertEqual(list(got), list(want),
                             f"voice frame {i} was not de-interleaved")

    def test_a_no_op_deinterleave_would_fail_this_suite(self):
        """Explicitly demonstrates the bug this work closes: if the decoder
        skipped de-interleaving, the bits it returned would differ from the
        transmitted logical frame for essentially every frame."""
        rng = np.random.default_rng(17)
        vector = rng.integers(0, 2, 144).astype(np.uint8)
        wire = p25.interleave_voice_frame(vector)
        differing = int(np.count_nonzero(np.asarray(wire) != vector))
        self.assertGreater(differing, 40,
                           "wire and vector order are nearly identical — the "
                           "interleave would be undetectable off-air")

    # ---- cross-module: the hand-off convention actually matches ----------
    def test_handover_matches_the_vocoders_expected_bit_order(self):
        """p25 de-interleaves into 'vector order'; ambe.unpack_imbe_frame() says
        it wants 'vector-ordered frame bits'. That agreement has to be MEASURED,
        not assumed — it is a contract between two modules owned separately.

        Skipped rather than failed if ambe is mid-edit: this file owns p25."""
        try:
            from survey import ambe
            pack, unpack = ambe.pack_imbe_frame, ambe.unpack_imbe_frame
            info_bits, frame_bits = ambe.IMBE_INFO_BITS, ambe.IMBE_FRAME_BITS
        except Exception as exc:                                   # noqa: BLE001
            self.skipTest(f"survey/ambe.py unavailable or changed: {exc}")
        if frame_bits != p25.VOICE_FRAME_BITS:
            self.skipTest("ambe IMBE frame geometry changed")

        rng = np.random.default_rng(23)
        info = rng.integers(0, 2, info_bits).astype(np.uint8)
        coded = np.asarray(pack(info), dtype=np.uint8)      # vector order, 144

        d = encode_frame_dibits(DUID_LDU1, NAC, voice=[coded] * 9,
                                lc_bits=make_link_control(tgid=TGID, source_id=SRC))
        f = _frames_by_duid(decode_p25_dibits(d))["LDU1"]

        rep = unpack(f["voice_bits"][0])
        self.assertEqual(rep["errors"], 0,
                         "vocoder FEC had to correct a clean frame — p25 and "
                         "ambe disagree about the 144-bit vector order")
        self.assertTrue(rep["fec_ok"])
        self.assertEqual(list(np.asarray(rep["info_bits"])), list(info))

        # ...and the same frame WITHOUT de-interleaving must NOT come out clean.
        # This is what proves the de-interleave is load-bearing rather than
        # cosmetic: feed the wire order straight in and the FEC falls apart.
        wire = p25.interleave_voice_frame(coded)
        bad = unpack(wire)
        self.assertGreater(bad["errors"], rep["errors"],
                           "skipping the de-interleave cost nothing — then the "
                           "de-interleave is not doing anything")


class TestLduGeometry(unittest.TestCase):
    """LDU_LAYOUT and the status-symbol phase, checked against the transmit-order
    annex (TIA-102.BAAA-A sections 10.3 / 10.4) rather than against our encoder.

    The annex lists all 864 transmitted symbols of an LDU one at a time. Reduced
    to run lengths of (frame sync | NID | voice | LC-or-ES | LSD), with the
    status symbols removed, it reads exactly as the tuple below — which is the
    geometry LDU_LAYOUT has to reproduce."""

    # (kind, number of DATA symbols) after removing the 24 status symbols.
    ANNEX_RUNS = (("F", 24), ("N", 32), ("V", 144), ("C", 20), ("V", 72),
                  ("C", 20), ("V", 72), ("C", 20), ("V", 72), ("C", 20),
                  ("V", 72), ("C", 20), ("V", 72), ("C", 20), ("V", 72),
                  ("L", 16), ("V", 72))

    def test_layout_reproduces_the_annex_run_lengths(self):
        runs = [("F", p25.SYNC_DIBITS), ("N", p25.NID_DIBITS)]
        for kind, nbits in p25.LDU_LAYOUT:
            nsym = nbits // 2
            if runs and runs[-1][0] == kind:
                runs[-1] = (kind, runs[-1][1] + nsym)
            else:
                runs.append((kind, nsym))
        self.assertEqual(tuple(runs), self.ANNEX_RUNS)

    def test_an_ldu_is_840_data_symbols_and_864_on_air(self):
        data = 2 * (p25.SYNC_DIBITS + p25.NID_DIBITS) + p25.LDU_PAYLOAD_BITS
        self.assertEqual(data, 1680)                 # bits
        self.assertEqual(data // 2, 840)             # data symbols
        status = (data // 2) // p25.STATUS_DATA_DIBITS
        self.assertEqual(status, 24)                 # annex: 24 status symbols
        self.assertEqual(data // 2 + status, 864)    # 180 ms at 4800 Bd

    def test_status_symbol_phase_matches_the_annex(self):
        """The annex puts a status symbol at transmitted index 35 and every 36th
        symbol after that, for all 24 of them."""
        self.assertEqual(p25.STATUS_PERIOD_DIBITS, 36)
        self.assertEqual(p25.STATUS_DATA_DIBITS, 35)
        expected = [35 + 36 * k for k in range(24)]
        self.assertEqual(expected[-1], 863)
        d = encode_frame_dibits(DUID_LDU1, NAC, status=True,
                                lc_bits=make_link_control(tgid=TGID))
        self.assertEqual(d.size, 864)
        stripped = p25.strip_status_symbols(d, 0)
        self.assertEqual(stripped.size, 840)

    def test_nine_voice_frames_per_ldu(self):
        self.assertEqual(sum(1 for k, _ in p25.LDU_LAYOUT if k == "V"), 9)
        self.assertEqual(p25.VOICE_FRAMES_PER_LDU, 9)
        # Figure 8-3: VC1, VC2, then LC in six 40-bit chunks between the rest,
        # with the LSD sitting between VC8 and VC9.
        kinds = [k for k, _ in p25.LDU_LAYOUT]
        self.assertEqual(kinds[:3], ["V", "V", "C"])
        self.assertEqual(kinds[-3:], ["V", "L", "V"])


class TestPublishedGeneratorMatrices(unittest.TestCase):
    """The short block codes, pinned to TIA-102.BAAA-A Tables 5-3 and 5-4.

    These codes were previously built here as "mathematically equivalent" codes
    of the right length and minimum distance. That is not good enough and the
    failure is invisible to every synthetic test: the extended Golay code is
    unique up to equivalence, so ANY valid generator reproduces the {0,8,12,16,24}
    weight distribution and corrects the same number of errors — while mapping
    information onto DIFFERENT codewords. Encoder and decoder sharing a wrong
    matrix round-trip perfectly and decode a real radio into garbage.

    So these tests compare against the matrices as PRINTED in the standard."""

    # Table 5-3, "Golay Generator Matrices", exactly as printed: each row is the
    # 12-bit identity half then the 12-bit parity half, both in octal.
    TABLE_5_3_24_12 = ("4000 6165", "2000 3073", "1000 7550", "0400 3664",
                       "0200 1732", "0100 6631", "0040 3315", "0020 1547",
                       "0010 6706", "0004 5227", "0002 4476", "0001 4353")
    # Table 5-3, the separately-printed (18,6,8) column: 6-bit identity, 12-bit
    # parity. The standard says this code is the (24,12,8) with the leftmost 6
    # bits deleted; these rows are the independent statement of that.
    TABLE_5_3_18_6 = ("40 3315", "20 1547", "10 6706",
                      "04 5227", "02 4476", "01 4353")
    # Table 5-4, "(10,6,3) short code", printed in binary as 6 identity columns
    # followed by 4 parity columns.
    TABLE_5_4_10_6 = ("1 0 0  0 0 0  1 1 1 0", "0 1 0  0 0 0  1 1 0 1",
                      "0 0 1  0 0 0  1 0 1 1", "0 0 0  1 0 0  0 1 1 1",
                      "0 0 0  0 1 0  0 0 1 1", "0 0 0  0 0 1  1 1 0 0")

    @staticmethod
    def _octal_row(row, kbits):
        ident, parity = row.split()
        return (int(ident, 8) << 12) | int(parity, 8), int(ident, 8), kbits

    def test_golay_generator_polynomial_is_the_standards(self):
        """Section 5.7: g(x) = x^11+x^10+x^6+x^5+x^4+x^2+1 = 6165 octal, and the
        standard notes this is literally the first parity row of the matrix."""
        self.assertEqual(0o6165, 0xC75)
        self.assertEqual(p25.GOLAY24_PARITY_ROWS[0], 0o6165)

    def test_golay24_matrix_is_table_5_3(self):
        rows = [self._octal_row(r, 12)[0] for r in self.TABLE_5_3_24_12]
        # left 12x12 half is the identity, as the standard states
        for i, cw in enumerate(rows):
            self.assertEqual(cw >> 12, 1 << (11 - i))
        self.assertEqual(list(p25.GOLAY24_PARITY_ROWS),
                         [cw & 0xFFF for cw in rows])
        # and the module's encoder reproduces the matrix row for row
        for i in range(12):
            self.assertEqual(p25._extended_golay24_parity(1 << (11 - i)),
                             rows[i] & 0xFFF)

    def test_golay24_weight_distribution_still_identifies_the_code(self):
        counts = {}
        for m in range(4096):
            cw = (m << 12) | p25._extended_golay24_parity(m)
            w = bin(cw).count("1")
            counts[w] = counts.get(w, 0) + 1
        self.assertEqual(counts, {0: 1, 8: 759, 12: 2576, 16: 759, 24: 1})

    def test_golay18_matches_the_standards_own_shortened_column(self):
        """Table 5-3 prints the (18,6,8) rows separately. The module derives them
        by the section 5.7 rule instead ("deleting the left most 6 bits from the
        (24,12,8) code"), so this checks a derivation against a printed table."""
        want = []
        for r in self.TABLE_5_3_18_6:
            ident, parity = r.split()
            want.append((int(ident, 8) << 12) | int(parity, 8))
        for i, row in enumerate(want):
            self.assertEqual(row >> 12, 1 << (5 - i))      # identity half
            self.assertEqual(golay18_encode(1 << (5 - i)), row,
                             f"(18,6,8) basis row {i} differs from Table 5-3")
        # the printed (18,6) rows are the last six (24,12) rows, truncated
        full = [self._octal_row(r, 12)[0] for r in self.TABLE_5_3_24_12]
        self.assertEqual(want, [cw & 0x3FFFF for cw in full[6:]])

    def test_hamming10_matrix_is_table_5_4(self):
        for i, row in enumerate(self.TABLE_5_4_10_6):
            bits = [int(c) for c in row.split()]
            self.assertEqual(len(bits), 10)
            self.assertEqual(bits[:6], [1 if j == i else 0 for j in range(6)])
            want = 0
            for b in bits:
                want = (want << 1) | b
            self.assertEqual(hamming10_encode(1 << (5 - i)), want,
                             f"(10,6,3) basis row {i} differs from Table 5-4")
        self.assertEqual(list(p25.HAMMING10_PARITY_ROWS),
                         [0b1110, 0b1101, 0b1011, 0b0111, 0b0011, 0b1100])

    def test_hamming10_is_not_the_naive_shortening(self):
        """Section 5.8 warns the shortening is deliberately not the obvious one
        ('not just the left hand columns and top rows'). Pin that, so nobody
        'simplifies' it back to a same-distance code that fails off-air."""
        naive = (0b0011, 0b0101, 0b0110, 0b1001, 0b1010, 0b1100)
        self.assertNotEqual(tuple(p25.HAMMING10_PARITY_ROWS), naive)

    # TIA-102.BAAA-A section 5.9 prints the three Reed-Solomon generator
    # polynomials outright, coefficients in octal, ascending powers of x. It also
    # states the root convention: g(x) = (x + a)(x + a^2)...(x + a^R) — first
    # consecutive root a^1, i.e. NARROW-SENSE. That was the module's single
    # largest documented assumption ("the most likely thing to be wrong off-air")
    # and these vectors settle it.
    TABLE_5_9 = {
        16: ("60 73 46 51 73 05 42 64 33 22 27 21 23 02 35 34 1"),   # (36,20,17)
        12: ("50 41 02 74 11 60 34 71 03 55 05 71 1"),               # (24,12,13)
        8:  ("26 06 24 57 60 45 75 67 1"),                           # (24,16,9)
    }

    def test_reed_solomon_generators_are_section_5_9(self):
        for nroots, printed in sorted(self.TABLE_5_9.items()):
            want = [int(tok, 8) for tok in printed.split()]
            self.assertEqual(len(want), nroots + 1)
            self.assertEqual(want[-1], 1, "generator must be monic")
            self.assertEqual(p25._rs_generator(nroots), want,
                             f"RS(nroots={nroots}) generator differs from "
                             "TIA-102.BAAA-A section 5.9")

    def test_reed_solomon_root_convention_is_narrow_sense(self):
        """Section 5.9: g(x) = (x + a)(x + a^2) ... (x + a^R). Check it directly —
        every alpha^i for i = 1..R must be a root, and alpha^0 must NOT be."""
        for nroots in (8, 12, 16):
            g = p25._rs_generator(nroots)
            for i in range(1, nroots + 1):
                acc = 0
                for k in range(len(g) - 1, -1, -1):
                    acc = p25._gmul(acc, p25._EXP[i]) ^ g[k]
                self.assertEqual(acc, 0, f"alpha^{i} is not a root (nroots={nroots})")
            acc = 0
            for k in range(len(g) - 1, -1, -1):
                acc = p25._gmul(acc, p25._EXP[0]) ^ g[k]
            self.assertNotEqual(acc, 0, "alpha^0 is a root — that is FCR=0, not "
                                        "the narrow-sense code the standard specifies")

    def test_galois_field_is_the_standards(self):
        # Section 5.9: "the extension Galois Field GF(2^6) generated by the
        # primitive characteristic polynomial a^6 + a + 1" = 0x43.
        self.assertEqual(p25.GF_POLY, 0x43)

    def test_distances_are_unchanged_by_using_the_real_matrices(self):
        words18 = [golay18_encode(d) for d in range(64)]
        d18 = min(bin(a ^ b).count("1")
                  for i, a in enumerate(words18) for b in words18[i + 1:])
        self.assertEqual(d18, 8)
        words10 = [hamming10_encode(d) for d in range(64)]
        d10 = min(bin(a ^ b).count("1")
                  for i, a in enumerate(words10) for b in words10[i + 1:])
        self.assertEqual(d10, 3)


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
