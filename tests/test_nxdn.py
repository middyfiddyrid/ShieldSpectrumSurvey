"""Unit tests for the NXDN (NEXEDGE / IDAS) frame layer.

Four layers of assertion, deliberately:

1. SPEC ANCHORS that are NOT self-consistency. Every one of these would still
   fail if the whole encoder/decoder pair agreed with itself on a wrong value:
     - the Frame Sync Word and the Preamble are REBUILT here from the symbol
       lists in NXDN TS 1-A Tables 4.4-2 and 4.4-1 and compared against the hex
       constants the module carries;
     - the convolutional code's FREE DISTANCE is COMPUTED from the trellis by
       Dijkstra and pinned at exactly 7 — that is the published distance of the
       K=5 (23,35) code the spec's G1/G2 define, so a wrong generator polynomial
       changes the number and fails loudly here rather than mis-decoding a real
       signal;
     - the scrambler's PN sequence is checked to be maximal-length (period 511),
       which a wrong tap set breaks;
     - the LICH is checked to put every one of its 8 symbols on an OUTER rail,
       the structural property the decoder's cheapest gate depends on;
     - each channel's bit arithmetic (SR + message + null + CRC + 4 tail bits,
       doubled, punctured) is checked to land on the exact on-air field width
       the frame format gives it.
2. ROUND TRIPS through the real chain: build frames, encode to C4FM IQ with
   c4fm.encode_c4fm(), decode, and assert the exact RAN, source unit ID,
   destination ID, call type and CIPHER STATE come back — at BOTH symbol rates,
   under noise, at a real capture rate, with inverted polarity and with a
   carrier offset.
3. ENCRYPTION AS A RESULT: every Cipher Type value must come back as an
   encrypted finding with its key ID, never as a failed decode.
4. NEGATIVES, which carry the most weight: noise, analog FM, an unmodulated
   carrier, random C4FM, a REAL DMR burst and a REAL P25 call must all produce
   ZERO NXDN frames, and 6 million random dibits must produce zero reported
   frames. A false talkgroup in a client report is far worse than a miss.

All headless — no hardware, and NOTHING here has seen a real off-air NXDN
signal. Every positive result in this file is synthetic: our encoder decoded by
our decoder. See the survey/nxdn.py docstring for what that does and does not
prove.
"""

import heapq
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from survey import c4fm, nxdn                                       # noqa: E402
from survey.nxdn import (CHANNELS, CIPHER_CLEAR, FRAME_BITS, FRAME_DIBITS,
                         FSW, FSW_BITS, LICH_DIBITS, MSG_TX_REL, MSG_VCALL,
                         PREAMBLE, PREAMBLE_BITS, RF_RCCH, RF_RDCH, RF_RTCH,
                         SACCH_PART_BITS, SLOT1_OFFSET_BITS,
                         STEAL_BOTH, STEAL_FIRST, STEAL_NONE, STEAL_SECOND,
                         SYMBOL_RATE_NXDN48, SYMBOL_RATE_NXDN96,
                         USC_SACCH_SINGLE, USC_SACCH_SUPER, USC_UDCH,
                         UDCH_FACCH2, UDCH_NO_STEAL, VCH_BITS_EFR,
                         VCH_BITS_EHR, conv_decode, conv_encode,
                         decode_channel, decode_lich, decode_nxdn,
                         decode_nxdn_dibits, encode_call, encode_channel,
                         encode_frame_dibits, encode_lich_dibits, make_message,
                         make_vcall, nxdn_crc, pack_lich, parse_vcall)  # noqa: E402

FS = 48000.0
RAN, SRC, DST = 5, 2621, 105

# Table 3.3-1 symbol mapping, written out here rather than imported so the sync
# words below are rebuilt from the SPEC's symbols and not from our own tables.
_SYMBOL_TO_DIBIT = {3: 0b01, 1: 0b00, -1: 0b10, -3: 0b11}


def _symbols_to_int(symbols) -> int:
    v = 0
    for s in symbols:
        v = (v << 2) | _SYMBOL_TO_DIBIT[s]
    return v


def _call_iq(fs=FS, symbol_rate=SYMBOL_RATE_NXDN96, **enc):
    """A synthetic NXDN call as C4FM IQ at the given symbol rate."""
    c4fm_kw = {k: enc.pop(k) for k in list(enc)
               if k in ("noise", "seed", "invert", "freq_offset", "shaping")}
    dibits = encode_call(**enc)
    return c4fm.encode_c4fm(dibits, fs, symbol_rate=symbol_rate,
                            deviation=nxdn.DEVIATION_OUTER_HZ[symbol_rate],
                            beta=nxdn.RRC_BETA, **c4fm_kw)


def _one_call(result):
    calls = result["calls"]
    assert len(calls) == 1, "expected 1 call, got %r" % (result["note"],)
    return calls[0]


class TestSpecAnchors(unittest.TestCase):
    """Values rebuilt from NXDN TS 1-A, not from this module's own encoder."""

    def test_frame_sync_word_matches_table_4_4_2(self):
        # Table 4.4-2: 10 symbols, transmission order left to right.
        self.assertEqual(_symbols_to_int([-3, 1, -3, 3, -3, -3, 3, 3, -1, 3]), FSW)
        self.assertEqual(FSW, 0xCDF59)
        self.assertEqual(FSW.bit_length() <= FSW_BITS, True)
        self.assertEqual(nxdn.FSW_DIBIT_PATTERN.size, 10)

    def test_preamble_matches_table_4_4_1(self):
        # Table 4.4-1: first 3 symbols +3 +3 +3, last 9 -3 +3 -3 +3 +3 -3 -3 -3 +3
        self.assertEqual(
            _symbols_to_int([3, 3, 3, -3, 3, -3, 3, 3, -3, -3, -3, 3]), PREAMBLE)
        self.assertEqual(PREAMBLE, 0x5775FD)
        self.assertEqual(PREAMBLE_BITS, 24)

    def test_the_preamble_is_not_confusable_with_the_frame_sync(self):
        # Both are fixed patterns transmitted back to back at the start of an
        # RDCH transmission; if they were close, every preamble would false-lock.
        pre = nxdn.bits_to_dibits(nxdn._int_to_bits(PREAMBLE, PREAMBLE_BITS))
        for start in range(pre.size - nxdn.FSW_DIBITS + 1):
            window = pre[start:start + nxdn.FSW_DIBITS]
            errs = sum(bin(int(a) ^ int(b)).count("1")
                       for a, b in zip(window, nxdn.FSW_DIBIT_PATTERN))
            self.assertGreater(errs, nxdn.FSW_MAX_BIT_ERRS)

    def test_frame_and_field_widths_match_the_frame_format(self):
        # Figures 4.4-2 / 4.4-4 and 4.6-2: the fields must tile 384 bits exactly.
        self.assertEqual(FRAME_BITS, 384)
        self.assertEqual(FRAME_DIBITS, 192)
        self.assertEqual(nxdn.FSW_BITS + nxdn.LICH_BITS + nxdn.SACCH_BITS
                         + 2 * nxdn.SLOT_BITS, FRAME_BITS)
        self.assertEqual(nxdn.FSW_BITS + nxdn.LICH_BITS + nxdn.USER_BITS,
                         FRAME_BITS)
        self.assertEqual(nxdn.SACCH_OFFSET_BITS + nxdn.SACCH_BITS,
                         SLOT1_OFFSET_BITS)
        self.assertEqual(SLOT1_OFFSET_BITS + nxdn.SLOT_BITS,
                         nxdn.SLOT2_OFFSET_BITS)
        # §7.1: EHR is two 72-bit frames per slot, EFR one 144-bit frame.
        self.assertEqual(2 * VCH_BITS_EHR, nxdn.SLOT_BITS)
        self.assertEqual(VCH_BITS_EFR, nxdn.SLOT_BITS)

    def test_symbol_rates_and_deviations_match_tables_2_3_1_and_3_3_1(self):
        self.assertEqual(SYMBOL_RATE_NXDN48, 2400.0)
        self.assertEqual(SYMBOL_RATE_NXDN96, 4800.0)
        self.assertEqual(nxdn.DEVIATION_OUTER_HZ[SYMBOL_RATE_NXDN48], 1050.0)
        self.assertEqual(nxdn.DEVIATION_INNER_HZ[SYMBOL_RATE_NXDN48], 350.0)
        self.assertEqual(nxdn.DEVIATION_OUTER_HZ[SYMBOL_RATE_NXDN96], 2400.0)
        self.assertEqual(nxdn.DEVIATION_INNER_HZ[SYMBOL_RATE_NXDN96], 800.0)
        # the outer rail is exactly 3x the inner one at both rates, which is what
        # lets c4fm.py's {+-3, +-1} slicer serve NXDN unchanged
        for rate in (SYMBOL_RATE_NXDN48, SYMBOL_RATE_NXDN96):
            self.assertEqual(nxdn.DEVIATION_OUTER_HZ[rate],
                             3 * nxdn.DEVIATION_INNER_HZ[rate])

    def test_rrc_roll_off_is_0_2_and_that_was_checked_not_assumed(self):
        # §3.4 and §3.5 both state alpha = 0.2. It happens to equal the TIA-102
        # C4FM figure c4fm.py already uses, which is exactly why it had to be
        # read out of the NXDN spec rather than inherited.
        self.assertEqual(nxdn.RRC_BETA, 0.2)
        self.assertEqual(nxdn.RRC_BETA, c4fm.RRC_BETA)

    def test_nxdn_uses_the_same_dibit_to_rail_map_as_p25_and_dmr(self):
        # Table 3.3-1 vs c4fm.DIBIT_FOR_LEVEL — this identity is the reason one
        # symbol-recovery layer serves all three protocols.
        for level, dibit in c4fm.DIBIT_FOR_LEVEL.items():
            self.assertEqual(_SYMBOL_TO_DIBIT[level], dibit)


class TestConvolutionalCode(unittest.TestCase):
    """§4.5: K=5, R=1/2, G1 = 1+D^3+D^4, G2 = 1+D+D^2+D^4 — the (23,35) code."""

    def test_generators_are_23_and_35_octal(self):
        def octal(taps):
            v = 0
            for t in taps:
                v |= 1 << (nxdn.CONV_K - 1 - t)      # D^0 in the MSB
            return v
        self.assertEqual(octal(nxdn.CONV_G1_TAPS), 0o23)
        self.assertEqual(octal(nxdn.CONV_G2_TAPS), 0o35)

    def test_free_distance_is_exactly_7(self):
        """THE anchor for this code. Computed from the trellis, not asserted.

        The free distance is the minimum output weight of any path that leaves
        the all-zero state and returns to it. For the K=5 (23,35) code that is a
        published 7; a wrong generator polynomial changes it, and a code with a
        smaller free distance would silently correct fewer errors on real
        traffic while every synthetic round trip still passed."""
        inf = float("inf")
        pq, dist, best = [], {}, inf
        for b in (1,):                                # must diverge
            o1, o2, ns = nxdn._CONV_TABLE[0][b]
            heapq.heappush(pq, (o1 + o2, ns))
        while pq:
            w, s = heapq.heappop(pq)
            if s == 0:
                best = w
                break
            if dist.get(s, inf) <= w:
                continue
            dist[s] = w
            for b in (0, 1):
                o1, o2, nxt = nxdn._CONV_TABLE[s][b]
                heapq.heappush(pq, (w + o1 + o2, nxt))
        self.assertEqual(best, 7)

    def test_a_wrong_generator_would_change_the_free_distance(self):
        # Pin the mechanism, not just the number: perturbing one tap must move
        # the distance, otherwise the test above is not actually discriminating.
        saved = nxdn.CONV_G2_TAPS
        try:
            nxdn.CONV_G2_TAPS = (0, 1, 2, 3)          # x^4 tap removed
            table = [[nxdn._conv_out(s, b) + (((s << 1) | b) & 15,)
                      for b in (0, 1)] for s in range(16)]
            inf = float("inf")
            pq, dist, best = [(sum(table[0][1][:2]), table[0][1][2])], {}, inf
            while pq:
                w, s = heapq.heappop(pq)
                if s == 0:
                    best = w
                    break
                if dist.get(s, inf) <= w:
                    continue
                dist[s] = w
                for b in (0, 1):
                    o1, o2, nxt = table[s][b]
                    heapq.heappush(pq, (w + o1 + o2, nxt))
            self.assertNotEqual(best, 7)
        finally:
            nxdn.CONV_G2_TAPS = saved

    def test_encode_decode_roundtrip_and_tail_termination(self):
        rng = np.random.default_rng(0)
        msg = rng.integers(0, 2, 40).tolist() + [0] * nxdn.CONV_TAIL_BITS
        got, metric = conv_decode(conv_encode(msg))
        self.assertEqual(got, msg)
        self.assertEqual(metric, 0)

    def test_viterbi_corrects_errors_up_to_the_free_distance(self):
        rng = np.random.default_rng(1)
        msg = rng.integers(0, 2, 60).tolist() + [0] * nxdn.CONV_TAIL_BITS
        coded = conv_encode(msg)
        for n_err in (1, 2, 3):
            bad = list(coded)
            # spread the errors out; dfree=7 means <=3 in one span is correctable
            for i in rng.choice(len(coded), n_err, replace=False):
                bad[int(i)] ^= 1
            self.assertEqual(conv_decode(bad)[0], msg, "%d errors" % n_err)

    def test_puncturing_erases_the_positions_the_spec_names(self):
        # §4.5.4 works the CAC matrix through explicitly: with
        # [1111111 / 1011101], X4 and X12 are the erased bits.
        matrix = CHANNELS["CAC_OUT"]["puncture"]
        # 14 input bits = 2 puncturing periods = X1..X28 in the spec's numbering
        _code, valid = nxdn.depuncture([1] * 24, matrix, 14)
        erased = [i + 1 for i, v in enumerate(valid) if not v]
        self.assertEqual(erased, [4, 12, 18, 26])     # X4 and X12, then repeat
        self.assertEqual(len(nxdn.puncture([1] * 28, matrix)), 24)

    def test_depuncture_marks_erasures_as_carrying_no_information(self):
        matrix = CHANNELS["SACCH"]["puncture"]
        coded = conv_encode([1, 0, 1, 1, 0, 0] * 6)
        kept = nxdn.puncture(coded, matrix)
        rebuilt, valid = nxdn.depuncture(kept, matrix, len(coded) // 2)
        self.assertEqual(len(rebuilt), len(coded))
        self.assertEqual(sum(valid), len(kept))
        for i, v in enumerate(valid):
            if v:
                self.assertEqual(rebuilt[i], coded[i])

    def test_interleave_is_a_permutation_and_invertible(self):
        for depth, n in ((5, 60), (9, 144), (29, 348), (25, 300), (21, 252)):
            bits = np.random.default_rng(depth).integers(0, 2, n).tolist()
            woven = nxdn.interleave(bits, depth)
            self.assertEqual(sorted(woven), sorted(bits))
            self.assertEqual(nxdn.deinterleave(woven, depth), bits)


class TestScrambler(unittest.TestCase):
    """§4.6: PN x^9 + x^4 + 1, seeded per frame from Table 4.6-1."""

    def test_sequence_is_maximal_length(self):
        # A 9-stage LFSR with a primitive polynomial has period 2^9-1 = 511, and
        # visits all 511 non-zero states. A wrong tap set gives a shorter period.
        seq = nxdn.scrambler_sequence(3 * nxdn.SCRAMBLER_PERIOD)
        p = nxdn.SCRAMBLER_PERIOD
        self.assertEqual(p, 511)
        self.assertTrue(np.array_equal(seq[:p], seq[p:2 * p]))
        self.assertTrue(np.array_equal(seq[:p], seq[2 * p:3 * p]))
        # a maximal-length sequence has exactly 2^(n-1) ones per period
        self.assertEqual(int(seq[:p].sum()), 256)

    def test_seed_matches_table_4_6_1(self):
        # S8..S0 = 0 1 1 1 0 0 1 0 0
        self.assertEqual([(nxdn.SCRAMBLER_INIT >> k) & 1 for k in range(8, -1, -1)],
                         [0, 1, 1, 1, 0, 0, 1, 0, 0])

    def test_scrambling_is_an_involution_that_only_flips_the_rail_sign(self):
        rng = np.random.default_rng(2)
        d = rng.integers(0, 4, FRAME_DIBITS).astype(np.uint8)
        s = nxdn.scramble_frame_dibits(d)
        self.assertTrue(np.array_equal(nxdn.scramble_frame_dibits(s), d))
        # the FSW is outside the scramble range (Figure 4.6-2)
        self.assertTrue(np.array_equal(s[:nxdn.FSW_DIBITS], d[:nxdn.FSW_DIBITS]))
        # and every change is a sign inversion, i.e. the LOW bit never moves
        self.assertTrue(np.array_equal(s & 1, d & 1))

    def test_scramble_range_is_182_symbols(self):
        # Figure 4.6-2 labels the range S0..S181: the whole frame bar the FSW.
        self.assertEqual(nxdn.SCRAMBLE_DIBITS, 182)
        self.assertEqual(nxdn.SCRAMBLE_START_DIBIT + nxdn.SCRAMBLE_DIBITS,
                         FRAME_DIBITS)


class TestCRC(unittest.TestCase):
    """§4.5.5 — four polynomials, shift register seeded all ones."""

    def test_polynomials_match_the_stated_generators(self):
        def poly(*powers):
            v = 0
            for p in powers:
                v |= 1 << p
            return v
        self.assertEqual(nxdn.CRC16_POLY, poly(12, 5, 0))
        self.assertEqual(nxdn.CRC15_POLY, poly(14, 11, 10, 7, 6, 2, 0))
        self.assertEqual(nxdn.CRC12_POLY, poly(11, 3, 2, 1, 0))
        self.assertEqual(nxdn.CRC6_POLY, poly(5, 2, 1, 0))

    def test_crc_detects_every_single_bit_error(self):
        rng = np.random.default_rng(3)
        for poly, width in ((nxdn.CRC6_POLY, 6), (nxdn.CRC12_POLY, 12),
                            (nxdn.CRC15_POLY, 15), (nxdn.CRC16_POLY, 16)):
            bits = rng.integers(0, 2, 64).tolist()
            good = nxdn_crc(bits, poly, width)
            for i in range(len(bits)):
                bad = list(bits)
                bad[i] ^= 1
                self.assertNotEqual(nxdn_crc(bad, poly, width), good,
                                    "width %d bit %d" % (width, i))

    def test_crc_is_seeded_all_ones_not_zero(self):
        # A zero seed would make any run of leading zeros invisible. The spec
        # says the register defaults to 1, and this is what that buys.
        for poly, width in ((nxdn.CRC6_POLY, 6), (nxdn.CRC16_POLY, 16)):
            self.assertNotEqual(nxdn_crc([0] * 16, poly, width),
                                nxdn_crc([0] * 24, poly, width))


class TestChannelCoding(unittest.TestCase):
    """Every functional channel: the bit arithmetic and an exact round trip."""

    def test_bit_arithmetic_lands_on_the_on_air_field_width(self):
        # sr + message + null + crc + 4 tail, doubled by the rate-1/2 code,
        # punctured, must equal the field width the frame format gives it.
        expect = {"SACCH": 60, "FACCH1": 144, "UDCH": 348, "FACCH2": 348,
                  "CAC_OUT": 300, "CAC_LONG": 252, "CAC_SHORT": 252}
        for name, prof in CHANNELS.items():
            n_in = nxdn._channel_input_bits(prof)
            coded = 2 * n_in
            if prof["puncture"] is None:
                kept = coded
            else:
                row1, row2 = prof["puncture"]
                per = sum(row1) + sum(row2)
                self.assertEqual(coded % (2 * len(row1)), 0, name)
                kept = coded // (2 * len(row1)) * per
            self.assertEqual(kept, prof["bits"], name)
            self.assertEqual(kept, expect[name], name)
            self.assertEqual(kept % prof["depth"], 0, name)

    def test_every_channel_roundtrips_exactly(self):
        for name, prof in CHANNELS.items():
            msg = np.random.default_rng(len(name)).integers(
                0, 2, prof["message"]).tolist()
            sr = nxdn.pack_sr(RAN, 0b10)
            got = decode_channel(name, encode_channel(name, msg, sr=sr))
            self.assertTrue(got["ok"], name)
            self.assertEqual(list(got["message"]), msg, name)
            if prof["sr"]:
                self.assertEqual(got["ran"], RAN, name)
                self.assertEqual(got["structure"], 0b10, name)
            else:
                self.assertIsNone(got["ran"], name)

    def test_channels_survive_bit_errors_then_fail_honestly(self):
        # The CRC is the gate every field downstream depends on: past what the
        # code can fix it must report failure, not a plausible payload.
        rng = np.random.default_rng(4)
        msg = rng.integers(0, 2, CHANNELS["FACCH1"]["message"]).tolist()
        on_air = encode_channel("FACCH1", msg)
        for n_err in (1, 2):
            bad = list(on_air)
            for i in rng.choice(len(on_air), n_err, replace=False):
                bad[int(i)] ^= 1
            got = decode_channel("FACCH1", bad)
            self.assertTrue(got["ok"], "%d errors" % n_err)
            self.assertEqual(list(got["message"]), msg)
        wrecked = list(rng.integers(0, 2, len(on_air)))
        got = decode_channel("FACCH1", wrecked)
        self.assertFalse(got["ok"])
        self.assertIsNone(got["message"])

    def test_random_bits_do_not_pass_a_channel_crc(self):
        # The CRC widths ARE the false-accept rates (1/2^width), so this is a
        # measurement rather than a hope. 400 random SACCH fields at 1/64 gives
        # an expectation around 6 — anything wildly above that means the CRC or
        # the Viterbi termination is not doing what it claims.
        rng = np.random.default_rng(5)
        passed = sum(1 for _ in range(400)
                     if decode_channel("SACCH",
                                       rng.integers(0, 2, 60).tolist())["ok"])
        self.assertLess(passed, 25)
        passed = sum(1 for _ in range(200)
                     if decode_channel("FACCH1",
                                       rng.integers(0, 2, 144).tolist())["ok"])
        self.assertEqual(passed, 0)              # 1/4096 x 200


class TestLICH(unittest.TestCase):
    """§5.2 / §4.5.3 — 7 control bits, even parity over the 4 MSBs, one bit per
    symbol on the outer rails."""

    def test_every_lich_symbol_is_an_outer_rail(self):
        # This is the decoder's cheapest and most scrambler-independent gate, so
        # it has to hold for EVERY legal control word.
        for word in range(128):
            d = encode_lich_dibits(word)
            self.assertEqual(d.size, LICH_DIBITS)
            self.assertTrue(np.all((d & 1) == 1), "control word 0x%02X" % word)
            self.assertTrue(np.all((d == 0b01) | (d == 0b11)))

    def test_the_outer_rail_property_survives_scrambling(self):
        # The scrambler flips the HIGH bit of a dibit; if it touched the low bit
        # the gate above would be worthless on air.
        for word in (0x00, 0x2B, 0x55, 0x7F):
            d = np.zeros(FRAME_DIBITS, dtype=np.uint8)
            d[nxdn.FSW_DIBITS:nxdn.FSW_DIBITS + LICH_DIBITS] = encode_lich_dibits(word)
            s = nxdn.scramble_frame_dibits(d)
            lich = s[nxdn.FSW_DIBITS:nxdn.FSW_DIBITS + LICH_DIBITS]
            self.assertTrue(np.all((lich & 1) == 1))

    def test_roundtrip_of_every_field_combination(self):
        for rf in range(4):
            for fn in range(4):
                for opt in range(4):
                    for direction in (0, 1):
                        word = pack_lich(rf, fn, opt, direction)
                        got = decode_lich(encode_lich_dibits(word))
                        self.assertIsNotNone(got)
                        self.assertEqual((got["rf_type"], got["fn_type"],
                                          got["option"], got["direction"]),
                                         (rf, fn, opt, direction))
                        self.assertEqual(got["raw"], word)

    def test_parity_covers_only_the_four_most_significant_bits(self):
        # §4.5.3: "Range of parity shall be 4 most significant bits."
        for word in range(128):
            self.assertEqual(nxdn.lich_parity(word),
                             bin(word >> 3).count("1") & 1)

    def test_a_flipped_parity_bit_is_rejected(self):
        d = encode_lich_dibits(pack_lich(RF_RDCH, USC_SACCH_SUPER, STEAL_NONE, 1))
        d[-1] ^= 0b10                            # flip the parity symbol's sign
        self.assertIsNone(decode_lich(d))

    def test_inner_rail_symbols_are_rejected(self):
        d = encode_lich_dibits(pack_lich(RF_RTCH, USC_UDCH, UDCH_NO_STEAL, 0))
        d[0] = 0b00                              # +1: not a legal LICH symbol
        d[3] = 0b10                              # -1
        self.assertIsNone(decode_lich(d))        # 2 rail errors > LICH_MAX_RAIL_ERRS

    def test_random_symbols_rarely_pass(self):
        # ~1.8 % by construction (8 rail bits, allowing one error, x a parity
        # bit). Pinned so a future loosening of LICH_MAX_RAIL_ERRS is visible.
        rng = np.random.default_rng(6)
        ok = sum(1 for _ in range(4000)
                 if decode_lich(rng.integers(0, 4, LICH_DIBITS).astype(np.uint8))
                 is not None)
        self.assertLess(ok, 4000 * 0.05)

    def test_frame_layout_follows_the_steal_flags(self):
        def layout(fn, opt, rf=RF_RDCH, direction=1):
            return nxdn.frame_layout({"rf_type": rf, "fn_type": fn,
                                      "option": opt, "direction": direction})
        chans, voice = layout(USC_SACCH_SUPER, STEAL_NONE)
        self.assertEqual([c[0] for c in chans], ["SACCH"])
        self.assertEqual(voice, [SLOT1_OFFSET_BITS, nxdn.SLOT2_OFFSET_BITS])
        chans, voice = layout(USC_SACCH_SUPER, STEAL_FIRST)
        self.assertEqual([c[0] for c in chans], ["SACCH", "FACCH1"])
        self.assertEqual(chans[1][1], SLOT1_OFFSET_BITS)
        self.assertEqual(voice, [nxdn.SLOT2_OFFSET_BITS])
        chans, voice = layout(USC_SACCH_SUPER, STEAL_SECOND)
        self.assertEqual(chans[1][1], nxdn.SLOT2_OFFSET_BITS)
        self.assertEqual(voice, [SLOT1_OFFSET_BITS])
        chans, voice = layout(USC_SACCH_SUPER, STEAL_BOTH)
        self.assertEqual([c[0] for c in chans], ["SACCH", "FACCH1", "FACCH1"])
        self.assertEqual(voice, [])
        self.assertEqual(layout(USC_UDCH, UDCH_NO_STEAL)[0][0], ("UDCH", 36))
        self.assertEqual(layout(USC_UDCH, UDCH_FACCH2)[0][0], ("FACCH2", 36))
        self.assertEqual(layout(0, 0, rf=RF_RCCH, direction=1)[0][0][0], "CAC_OUT")
        self.assertEqual(layout(0b01, 0, rf=RF_RCCH, direction=0)[0][0][0],
                         "CAC_LONG")
        self.assertEqual(layout(0b11, 0, rf=RF_RCCH, direction=0)[0][0][0],
                         "CAC_SHORT")


class TestVCALL(unittest.TestCase):
    """Figure 6.4-1 field layout, Tables 6.5-8 and 6.5-24."""

    def test_field_offsets(self):
        vc = parse_vcall(make_vcall(0xBEEF, 0x1234, call_type=0b100,
                                    cipher_type=0b10, key_id=0x2A,
                                    transmission_mode=0b011))
        self.assertEqual(vc["source_id"], 0xBEEF)
        self.assertEqual(vc["destination_id"], 0x1234)
        self.assertEqual(vc["target_unit_id"], 0x1234)   # individual call
        self.assertIsNone(vc["group_id"])
        self.assertEqual(vc["call_type"], 0b100)
        self.assertEqual(vc["cipher_type"], 0b10)
        self.assertEqual(vc["key_id"], 0x2A)
        self.assertTrue(vc["efr"])
        self.assertTrue(vc["encrypted"])

    def test_group_call_destination_is_a_group_id(self):
        vc = parse_vcall(make_vcall(1, 999, call_type=0b001))
        self.assertEqual(vc["group_id"], 999)
        self.assertIsNone(vc["target_unit_id"])
        self.assertFalse(vc["encrypted"])

    def test_full_16_bit_id_range(self):
        vc = parse_vcall(make_vcall(0xFFFF, 0xFFFF))
        self.assertEqual((vc["source_id"], vc["destination_id"]), (0xFFFF, 0xFFFF))

    def test_message_type_is_read_from_the_low_six_bits_of_octet_0(self):
        msg = nxdn.parse_message(make_vcall(1, 2))
        self.assertEqual(msg["message_type"], MSG_VCALL)
        self.assertIn("VCALL", msg["message_name"])
        rel = nxdn.parse_message(make_message(MSG_TX_REL))
        self.assertEqual(rel["message_type"], MSG_TX_REL)
        self.assertIsNone(rel["vcall"])

    def test_cipher_type_table(self):
        self.assertEqual(nxdn.cipher_name(0b00), "unencrypted (clear)")
        self.assertIn("DES", nxdn.cipher_name(0b10))
        self.assertIn("AES", nxdn.cipher_name(0b11))
        self.assertEqual(CIPHER_CLEAR, 0)


class TestFrameRoundTrip(unittest.TestCase):
    """One frame at a time, at the dibit level."""

    def test_a_frame_is_exactly_192_symbols(self):
        d = encode_frame_dibits(RF_RDCH, USC_SACCH_SUPER, STEAL_NONE, 1,
                                ran=RAN, sacch_part=[1] * SACCH_PART_BITS,
                                sacch_structure=0b11)
        self.assertEqual(d.size, FRAME_DIBITS)

    def test_frame_sync_is_where_the_decoder_looks_for_it(self):
        d = encode_frame_dibits(RF_RDCH, USC_SACCH_SUPER, STEAL_NONE, 1, ran=RAN)
        self.assertIn(0, nxdn.find_fsw(d))

    def test_decode_frame_recovers_lich_and_ran(self):
        d = encode_frame_dibits(RF_RTCH, USC_SACCH_SUPER, STEAL_NONE, 1,
                                ran=42, sacch_part=[0] * SACCH_PART_BITS,
                                sacch_structure=0b01)
        f = nxdn.decode_frame(d, 0)
        self.assertIsNotNone(f)
        self.assertEqual(f["lich"]["rf_type"], RF_RTCH)
        self.assertEqual(f["ran"], 42)
        self.assertEqual(f["structure"], 0b01)
        self.assertEqual(f["crc_ok"], 1)
        self.assertEqual(f["crc_failed"], 0)

    def test_voice_frames_are_located_and_counted_not_decoded(self):
        d = encode_frame_dibits(RF_RDCH, USC_SACCH_SUPER, STEAL_NONE, 1, ran=RAN)
        f = nxdn.decode_frame(d, 0)
        self.assertEqual(f["voice_frame_count"], 4)          # 2 slots x 2 EHR
        self.assertTrue(all(v.size == VCH_BITS_EHR for v in f["voice_bits"]))
        self.assertIn("EHR", f["voice_mode"])
        f = nxdn.decode_frame(d, 0, efr=True)
        self.assertEqual(f["voice_frame_count"], 2)          # 2 slots x 1 EFR
        self.assertTrue(all(v.size == VCH_BITS_EFR for v in f["voice_bits"]))

    def test_a_stolen_slot_carries_no_voice(self):
        d = encode_frame_dibits(RF_RDCH, USC_SACCH_SINGLE, STEAL_BOTH, 1,
                                ran=RAN, facch1=make_vcall(SRC, DST))
        f = nxdn.decode_frame(d, 0)
        self.assertEqual(f["voice_frame_count"], 0)
        self.assertEqual(len(f["messages"]), 2)              # both FACCH1 copies
        self.assertEqual(f["messages"][0]["vcall"]["source_id"], SRC)

    def test_a_udch_data_frame_decodes(self):
        payload = np.random.default_rng(7).integers(0, 2, 176).tolist()
        d = encode_frame_dibits(RF_RDCH, USC_UDCH, UDCH_NO_STEAL, 1, ran=3,
                                payload=payload)
        f = nxdn.decode_frame(d, 0)
        self.assertEqual(f["ran"], 3)
        self.assertEqual(list(f["channels"]["UDCH"]["message"]), payload)
        self.assertEqual(f["voice_frame_count"], 0)

    def test_an_rcch_control_frame_decodes(self):
        payload = np.random.default_rng(8).integers(0, 2, 144).tolist()
        d = encode_frame_dibits(RF_RCCH, 0, 0, 1, ran=17, payload=payload)
        f = nxdn.decode_frame(d, 0)
        self.assertEqual(f["ran"], 17)
        self.assertTrue(f["channels"]["CAC_OUT"]["ok"])
        self.assertEqual(list(f["channels"]["CAC_OUT"]["message"]), payload)


class TestFullChain(unittest.TestCase):
    """Frames -> C4FM IQ -> symbols -> frames -> call records."""

    def test_clear_call_at_nxdn96(self):
        r = decode_nxdn(_call_iq(ran=RAN, source_id=SRC, destination_id=DST,
                                 superframes=2), FS)
        c = _one_call(r)
        self.assertEqual(c["ran"], RAN)
        self.assertEqual(c["source_id"], SRC)
        self.assertEqual(c["group_id"], DST)
        self.assertEqual(c["call_type_name"], "conference call (group)")
        self.assertIs(c["encrypted"], False)
        self.assertEqual(r["stats"]["symbol_rate"], SYMBOL_RATE_NXDN96)
        self.assertIn("clear", c["summary"])

    def test_clear_call_at_nxdn48(self):
        # 2400 Bd in a 6.25 kHz channel — the rate c4fm.py is NOT defaulted to,
        # so this proves the symbol_rate argument really is plumbed through.
        iq = _call_iq(symbol_rate=SYMBOL_RATE_NXDN48, ran=1, source_id=SRC,
                      destination_id=DST, superframes=2)
        r = decode_nxdn(iq, FS)
        c = _one_call(r)
        self.assertEqual(r["stats"]["symbol_rate"], SYMBOL_RATE_NXDN48)
        self.assertEqual((c["ran"], c["source_id"], c["group_id"]), (1, SRC, DST))
        self.assertIn("6.25 kHz", c["summary"])

    def test_individual_call_destination_is_a_unit_not_a_group(self):
        r = decode_nxdn(_call_iq(ran=2, source_id=1000, destination_id=2000,
                                 call_type=0b100, superframes=2), FS)
        c = _one_call(r)
        self.assertEqual(c["target_unit_id"], 2000)
        self.assertIsNone(c["group_id"])
        self.assertIn("to unit 2000", c["summary"])

    def test_recovers_through_decimation_from_a_real_capture_rate(self):
        r = decode_nxdn(_call_iq(fs=2_400_000.0, ran=RAN, source_id=SRC,
                                 destination_id=DST, superframes=2), 2_400_000.0)
        c = _one_call(r)
        self.assertEqual((c["ran"], c["source_id"], c["group_id"]), (RAN, SRC, DST))

    def test_recovers_under_noise(self):
        r = decode_nxdn(_call_iq(noise=0.06, seed=11, ran=RAN, source_id=SRC,
                                 destination_id=DST, superframes=3), FS)
        c = _one_call(r)
        self.assertEqual((c["ran"], c["source_id"], c["group_id"]), (RAN, SRC, DST))

    def test_recovers_with_inverted_polarity(self):
        r = decode_nxdn(_call_iq(invert=True, ran=RAN, source_id=SRC,
                                 destination_id=DST, superframes=2), FS)
        self.assertTrue(r["stats"]["inverted"])
        c = _one_call(r)
        self.assertEqual((c["ran"], c["source_id"], c["group_id"]), (RAN, SRC, DST))

    def test_recovers_with_a_carrier_offset(self):
        r = decode_nxdn(_call_iq(freq_offset=300.0, ran=RAN, source_id=SRC,
                                 destination_id=DST, superframes=2), FS)
        c = _one_call(r)
        self.assertEqual((c["ran"], c["source_id"], c["group_id"]), (RAN, SRC, DST))

    def test_every_ran_roundtrips(self):
        for ran in (0, 1, 7, 31, 63):
            r = decode_nxdn_dibits(encode_call(ran=ran, source_id=SRC,
                                               destination_id=DST))
            self.assertEqual(_one_call(r)["ran"], ran, "RAN %d" % ran)

    def test_the_sacch_superframe_is_reassembled_from_four_frames(self):
        # The VCALL is spread across four SACCH quarters; no single frame has it.
        d = encode_call(ran=RAN, source_id=SRC, destination_id=DST,
                        facch1_first=False, tail_release=False)
        r = decode_nxdn_dibits(d)
        c = _one_call(r)
        self.assertEqual(c["channels"], ["SACCH"])
        self.assertEqual((c["source_id"], c["group_id"]), (SRC, DST))

    def test_a_partial_superframe_yields_no_call_detail(self):
        # Drop the last quarter: the message must NOT be emitted half-built.
        d = encode_call(ran=RAN, source_id=SRC, destination_id=DST,
                        facch1_first=False, tail_release=False)
        r = decode_nxdn_dibits(d[:3 * FRAME_DIBITS])
        self.assertTrue(r["frames"])                  # framing still identified
        for c in r["calls"]:
            self.assertIsNone(c["source_id"])
            self.assertIsNone(c["group_id"])
            self.assertIsNone(c["encrypted"])         # NOT reported as clear
            self.assertIn("unknown", c["summary"])
        self.assertEqual(r["stats"]["rans"], [RAN])   # the RAN is still real

    def test_voice_frames_are_counted_and_handed_over_not_decoded(self):
        r = decode_nxdn_dibits(encode_call(ran=RAN, source_id=SRC,
                                           destination_id=DST, superframes=2))
        self.assertEqual(r["stats"]["voice_frames"], 34)   # 8x4 + 2 unstolen slot
        widths = {v.size for f in r["frames"] for v in f["voice_bits"]}
        self.assertEqual(widths, {VCH_BITS_EHR})
        c = _one_call(r)
        self.assertIn("AMBE+2", c["summary"])
        # the word "assumed" must survive: nothing in the LICH says EHR
        self.assertEqual(c["voice_mode"], "EHR (assumed)")

    def test_efr_mode_reslices_the_voice_slots(self):
        # EHR vs EFR is only in the VCALL, so the decoder must go back and redo
        # the slicing once it has read it.
        r = decode_nxdn_dibits(encode_call(ran=RAN, source_id=SRC,
                                           destination_id=DST,
                                           transmission_mode=0b011,
                                           superframes=2))
        widths = {v.size for f in r["frames"] for v in f["voice_bits"]}
        self.assertEqual(widths, {VCH_BITS_EFR})
        c = _one_call(r)
        self.assertEqual(c["transmission_mode_name"], "9600 bit/s EFR")
        self.assertEqual(c["voice_mode"], "EFR")

    def test_transmission_release_closes_the_call(self):
        r = decode_nxdn_dibits(encode_call(ran=RAN, source_id=SRC,
                                           destination_id=DST))
        self.assertTrue(_one_call(r)["released"])

    def test_frames_off_the_grid_are_not_merged_into_the_call(self):
        # Splice a second, unrelated frame at a phase that is not a multiple of
        # 192 symbols. The grid filter must drop it rather than let its LICH
        # contribute to the call record.
        good = encode_call(ran=RAN, source_id=SRC, destination_id=DST,
                           superframes=2)
        stray = encode_frame_dibits(RF_RTCH, USC_SACCH_SUPER, STEAL_NONE, 0,
                                    ran=63, sacch_part=[1] * SACCH_PART_BITS,
                                    sacch_structure=0b11)
        spliced = np.concatenate([good, np.zeros(7, dtype=np.uint8), stray])
        r = decode_nxdn_dibits(spliced)
        self.assertNotIn(63, r["stats"]["rans"])
        self.assertEqual(_one_call(r)["ran"], RAN)


class TestEncryptionIsAFirstClassResult(unittest.TestCase):
    """'NXDN, RAN 1, from unit 2621, ENCRYPTED' is a finding, not a failure."""

    def _encrypted(self, cipher_type, key_id=0x15, **kw):
        r = decode_nxdn(_call_iq(ran=1, source_id=SRC, destination_id=DST,
                                 cipher_type=cipher_type, key_id=key_id,
                                 superframes=2, **kw), FS)
        return r, _one_call(r)

    def test_every_non_zero_cipher_type_reports_as_encrypted(self):
        for cipher in (0b01, 0b10, 0b11):
            r, c = self._encrypted(cipher)
            self.assertIs(c["encrypted"], True, "cipher %d" % cipher)
            self.assertEqual(c["cipher_type"], cipher)
            self.assertEqual(c["key_id"], 0x15)
            self.assertIn("ENCRYPTED", c["summary"])
            self.assertIn("ENCRYPTED", r["note"])
            self.assertIs(r["stats"]["encrypted"], True)

    def test_an_encrypted_call_is_still_fully_identified(self):
        # The whole point: encryption hides the AUDIO, not the metadata.
        _r, c = self._encrypted(0b11)
        self.assertEqual(c["ran"], 1)
        self.assertEqual(c["source_id"], SRC)
        self.assertEqual(c["group_id"], DST)
        self.assertIn("AES", c["cipher_name"])
        self.assertIn("from unit %d" % SRC, c["summary"])

    def test_an_encrypted_call_still_yields_its_voice_frames(self):
        # They are ciphertext, but their presence and count are the evidence
        # that someone is actually talking rather than just registered.
        _r, c = self._encrypted(0b10)
        self.assertGreater(c["voice_frames"], 0)

    def test_a_vcall_iv_alone_is_enough_to_report_encryption(self):
        """The hard field case: the sweep started after the VCALL, so the only
        thing on air is the initialisation vector. §6.5.41 forbids an all-zero
        IV, so its presence means a DES/AES key is loaded — and that must be
        reported as encrypted rather than as "state unknown"."""
        iv = list(range(1, 9))
        frames = [encode_frame_dibits(RF_RDCH, USC_SACCH_SINGLE, STEAL_FIRST, 1,
                                      ran=4, sacch_part=[0] * SACCH_PART_BITS,
                                      facch1=make_message(nxdn.MSG_VCALL_IV, iv),
                                      voice_fill=k) for k in range(3)]
        r = decode_nxdn_dibits(np.concatenate(frames))
        c = _one_call(r)
        self.assertIs(c["encrypted"], True)
        self.assertEqual(c["init_vector"], "0102030405060708")
        self.assertEqual(c["ran"], 4)
        self.assertIn("ENCRYPTED", c["summary"])
        self.assertIn("initialisation vector", c["summary"])

    def test_a_vcall_iv_alongside_a_vcall_names_both(self):
        r = decode_nxdn(_call_iq(ran=1, source_id=SRC, destination_id=DST,
                                 cipher_type=0b11, key_id=9, superframes=2), FS)
        c = _one_call(r)
        self.assertIs(c["encrypted"], True)
        self.assertIn("AES", c["summary"])
        self.assertIsNone(c["init_vector"])       # this synthetic call sends none

    def test_encryption_is_never_reported_as_a_failed_decode(self):
        r, _c = self._encrypted(0b11)
        self.assertTrue(r["messages"])
        self.assertTrue(r["stats"]["crc_ok"])

    def test_clear_traffic_is_not_called_encrypted(self):
        r = decode_nxdn(_call_iq(ran=1, source_id=SRC, destination_id=DST,
                                 cipher_type=CIPHER_CLEAR, superframes=2), FS)
        self.assertIs(_one_call(r)["encrypted"], False)
        self.assertNotIn("ENCRYPTED", r["note"])

    def test_unknown_is_reported_as_unknown_not_as_clear(self):
        # Voice frames with no VCALL anywhere: the honest answer is "unknown".
        d = np.concatenate([
            encode_frame_dibits(RF_RDCH, USC_SACCH_SUPER, STEAL_NONE, 1, ran=8,
                                sacch_part=[0] * SACCH_PART_BITS,
                                sacch_structure=s, voice_fill=k)
            for k, s in enumerate((0b11, 0b10, 0b01))])
        r = decode_nxdn_dibits(d)
        c = _one_call(r)
        self.assertIsNone(c["encrypted"])
        self.assertIn("unknown", c["summary"])
        self.assertEqual(c["ran"], 8)


class TestNegatives(unittest.TestCase):
    """Nothing here may produce an NXDN frame. A fabricated unit ID in a sweep
    report is far worse than an empty result."""

    def _assert_nothing(self, iq, fs, label):
        r = decode_nxdn(iq, fs)
        self.assertEqual(r["messages"], [], "%s produced messages" % label)
        self.assertEqual(r["calls"], [], "%s produced calls" % label)
        self.assertEqual(r["frames"], [], "%s produced frames" % label)
        self.assertTrue(r["note"])

    def test_noise(self):
        rng = np.random.default_rng(20)
        n = 400_000
        self._assert_nothing((rng.standard_normal(n)
                              + 1j * rng.standard_normal(n)).astype(np.complex64),
                             2_400_000.0, "noise")

    def test_analog_fm_voice(self):
        t = np.arange(240_000) / 2_400_000.0
        dev = 3000.0 * np.sin(2 * np.pi * 1000.0 * t)
        iq = np.exp(1j * 2 * np.pi * np.cumsum(dev) / 2_400_000.0).astype(np.complex64)
        self._assert_nothing(iq, 2_400_000.0, "analog FM")

    def test_narrowband_analog_fm_at_the_nxdn48_channel_width(self):
        # The hard case: a 6.25 kHz NXDN48 carrier is NARROWER than analog FM, so
        # looks_like_nxdn cannot exclude narrowband analog and the decoder's own
        # gates are all that stand between this and a fake call.
        t = np.arange(200_000) / 480_000.0
        dev = 1000.0 * np.sin(2 * np.pi * 800.0 * t)
        iq = np.exp(1j * 2 * np.pi * np.cumsum(dev) / 480_000.0).astype(np.complex64)
        self._assert_nothing(iq, 480_000.0, "narrowband analog FM")

    def test_unmodulated_carrier(self):
        t = np.arange(96_000) / FS
        self._assert_nothing(np.exp(1j * 2 * np.pi * 1200.0 * t).astype(np.complex64),
                             FS, "carrier")

    def test_random_c4fm_symbols_are_not_nxdn(self):
        # correct modulation, correct symbol rate, no NXDN framing — the FSW +
        # LICH + grid + CRC gates are all that stand between this and a fake
        # decode.
        d = np.random.default_rng(21).integers(0, 4, 40_000).astype(np.uint8)
        self._assert_nothing(c4fm.encode_c4fm(d, FS), FS, "random C4FM")

    def test_six_million_random_dibits_produce_no_frames(self):
        """The headline false-positive measurement. Do not weaken this.

        A 20-bit frame sync is only ~2e-4 per position, so a long capture WILL
        false-lock; ~10 of those also clear the LICH. What stops them becoming a
        report is the frame-grid test plus the CRC requirement, and this is the
        test that measures it end to end."""
        rng = np.random.default_rng(22)
        reported = 0
        for _ in range(12):
            d = rng.integers(0, 4, 500_000).astype(np.uint8)
            r = decode_nxdn_dibits(d)
            reported += len(r["frames"]) + len(r["messages"])
        self.assertEqual(reported, 0)

    def test_a_single_frame_with_one_crc_is_not_enough(self):
        """The gate branch that a long sweep depends on.

        One frame that cleared the FSW, the LICH and ONE 6-bit CRC is a ~1e-8
        coincidence per position — which a million-symbol capture reaches. So a
        lone frame must need two independent CRCs, and a frame carrying only a
        SACCH has just one."""
        lone = encode_frame_dibits(RF_RDCH, USC_SACCH_SUPER, STEAL_NONE, 1,
                                   ran=RAN, sacch_part=[1] * SACCH_PART_BITS,
                                   sacch_structure=0b11)
        r = decode_nxdn_dibits(lone)
        self.assertEqual(r["calls"], [])
        self.assertEqual(r["frames"], [])
        self.assertIn("frame grid", r["note"])
        # ... but two frames on the grid, one CRC each, ARE believed
        r = decode_nxdn_dibits(np.concatenate([lone, lone]))
        self.assertEqual(len(r["frames"]), 2)
        self.assertEqual(r["stats"]["rans"], [RAN])

    def test_the_scrambler_is_load_bearing_and_fails_loudly_if_wrong(self):
        """Disabling the descrambler is exactly the failure mode the module
        docstring flags as most likely off-air (a mis-read scrambler tap).

        It must fail LOUDLY: no channel CRC may pass, no field may be reported,
        and the summary must say the framing was identified but nothing else —
        never a call record that merely looks short of a talkgroup."""
        good = encode_call(ran=RAN, source_id=SRC, destination_id=DST)
        saved = nxdn._SCRAMBLE_MASK
        try:
            nxdn._SCRAMBLE_MASK = np.zeros(nxdn.SCRAMBLE_DIBITS, dtype=np.uint8)
            r = decode_nxdn_dibits(good)
            self.assertEqual(r["stats"]["crc_ok"], 0)
            for c in r["calls"]:
                self.assertFalse(c["fields_verified"])
                self.assertIsNone(c["ran"])
                self.assertIsNone(c["source_id"])
                self.assertIsNone(c["encrypted"])
                self.assertIn("NO channel FEC verified", c["summary"])
                self.assertIn("NOT recovered", c["summary"])
            self.assertIn("NO channel FEC verified", r["note"])
        finally:
            nxdn._SCRAMBLE_MASK = saved
        c = _one_call(decode_nxdn_dibits(good))
        self.assertTrue(c["fields_verified"])
        self.assertEqual(c["source_id"], SRC)

    def test_the_lich_gate_is_load_bearing(self):
        """Corrupt only the LICH symbols of every frame: the FSW still locks and
        the SACCH CRCs would still pass, but nothing may be reported because the
        frame layout is then unknown."""
        d = np.array(encode_call(ran=RAN, source_id=SRC, destination_id=DST),
                     copy=True)
        for pos in nxdn.find_fsw(d):
            lo = pos + nxdn.FSW_DIBITS
            d[lo:lo + LICH_DIBITS] = 0b00        # inner rail: never a legal LICH
        r = decode_nxdn_dibits(d)
        self.assertEqual(r["frames"], [])
        self.assertEqual(r["calls"], [])

    def test_a_lone_planted_frame_sync_is_not_reported(self):
        # Splice real FSWs into random C4FM: the sync matches, nothing else
        # validates, so the honesty gate must reject them.
        rng = np.random.default_rng(23)
        d = rng.integers(0, 4, 8000).astype(np.uint8)
        for at in (500, 1500, 2500, 3500):
            d[at:at + nxdn.FSW_DIBITS] = nxdn.FSW_DIBIT_PATTERN
        r = decode_nxdn_dibits(d)
        self.assertEqual(r["calls"], [])
        self.assertEqual(r["frames"], [])

    def test_the_nxdn_sync_is_far_from_the_dmr_and_p25_sync_words(self):
        # All three protocols are C4FM at the same rails and share c4fm.py, so
        # the sync words are the only thing separating them at the front door.
        # NXDN's is 20 bits, so compare it against every 20-bit window of theirs.
        try:
            from survey.dmr import SYNC_PATTERNS
            from survey.p25 import FRAME_SYNC
        except Exception as exc:                                # noqa: BLE001
            raise unittest.SkipTest("dmr/p25 unavailable: %s" % exc)
        others = dict(SYNC_PATTERNS)
        others["p25"] = FRAME_SYNC
        inv = int("10" * (FSW_BITS // 2), 2)      # dibit-wise polarity flip mask
        for name, pattern in others.items():
            for shift in range(0, 48 - FSW_BITS + 1, 2):        # dibit-aligned
                window = (pattern >> shift) & ((1 << FSW_BITS) - 1)
                d = bin(window ^ FSW).count("1")
                di = bin(window ^ FSW ^ inv).count("1")
                self.assertGreater(min(d, di), nxdn.FSW_MAX_BIT_ERRS,
                                   "%s at shift %d" % (name, shift))

    def test_a_real_dmr_transmission_is_not_decoded_as_nxdn(self):
        # Guarded: survey/dmr.py is another module's territory, so a change to
        # its encoder API skips this rather than failing the NXDN suite.
        try:
            from survey import dmr
            lc = dmr.pack_full_lc(dmr.FLCO_GROUP_VOICE, 0, 0, 4021, 2621102) + [0] * 24
            bursts = []
            for _ in range(4):
                bursts.append(dmr.build_data_burst("bs_data", 1,
                                                   dmr.DT_VOICE_LC_HEADER, lc))
                bursts.append(dmr.build_voice_burst("bs_voice", voice_seed=3))
            iq = dmr.encode_dmr(bursts, FS)
        except Exception as exc:                                # noqa: BLE001
            raise unittest.SkipTest("dmr encoder unavailable: %s" % exc)
        self._assert_nothing(iq, FS, "DMR")

    def test_a_real_p25_call_is_not_decoded_as_nxdn(self):
        try:
            from survey import p25
            dibits = p25.encode_call(0x293, 4021, 2621102, ldu_pairs=3)
        except Exception as exc:                                # noqa: BLE001
            raise unittest.SkipTest("p25 encoder unavailable: %s" % exc)
        self._assert_nothing(c4fm.encode_c4fm(dibits, FS), FS, "P25 call")

    def test_a_real_p25_call_with_status_symbols_is_not_decoded_as_nxdn(self):
        # Status symbols shift P25's bit stream by one dibit in 36, which is a
        # different set of 20-bit windows to false-lock on.
        try:
            from survey import p25
            dibits = p25.encode_call(0x293, 4021, 2621102, ldu_pairs=2, status=True)
        except Exception as exc:                                # noqa: BLE001
            raise unittest.SkipTest("p25 encoder unavailable: %s" % exc)
        self._assert_nothing(c4fm.encode_c4fm(dibits, FS), FS, "P25 + status")

    def test_too_short_raises(self):
        with self.assertRaises(ValueError):
            decode_nxdn(np.zeros(8, dtype=np.complex64), FS)

    def test_out_of_range_symbols_raise(self):
        with self.assertRaises(ValueError):
            decode_nxdn_dibits(np.array([0, 1, 2, 3, 4], dtype=np.uint8))


class TestGate(unittest.TestCase):
    """looks_like_nxdn() only decides what to TRY; it must not veto real NXDN."""

    def test_the_gate_passes_the_measured_c4fm_feature_signature(self):
        # MEASURED: identify.iq_features reports RRC-shaped 4-FSK as
        # 'analog_voice' with fsk_states 0. Requiring FSK evidence, as an
        # obvious-looking gate would, means NXDN could never be tried.
        self.assertTrue(nxdn.looks_like_nxdn(
            {"modulation_class": "analog_voice", "fsk_states": 0,
             "fsk_shift_khz": 0.0, "occupied_bw_khz": 11.0,
             "symbol_rate_kbd": 0.0, "signal_present": True}))
        self.assertTrue(nxdn.looks_like_nxdn(
            {"modulation_class": "analog_voice", "fsk_states": 0,
             "occupied_bw_khz": 5.5, "symbol_rate_kbd": 0.0}))

    def test_the_gate_passes_a_clean_fsk_reading(self):
        for rate in (2.4, 4.8):
            self.assertTrue(nxdn.looks_like_nxdn(
                {"modulation_class": "fsk", "fsk_states": 4,
                 "symbol_rate_kbd": rate, "occupied_bw_khz": 12.5}))

    def test_the_gate_rejects_the_obvious_non_matches(self):
        self.assertFalse(nxdn.looks_like_nxdn({}))
        self.assertFalse(nxdn.looks_like_nxdn(
            {"signal_present": False, "occupied_bw_khz": 12.5}))
        self.assertFalse(nxdn.looks_like_nxdn(
            {"occupied_bw_khz": 20000.0}))               # wideband OFDM
        self.assertFalse(nxdn.looks_like_nxdn(
            {"occupied_bw_khz": 0.2}))                   # a bare tone
        self.assertFalse(nxdn.looks_like_nxdn(
            {"occupied_bw_khz": 12.5, "symbol_rate_kbd": 38.4}))

    def test_the_gate_does_not_reject_a_real_synthetic_call(self):
        # The regression that bit P25: a gate that rejects a signal the decoder
        # handles perfectly. Prove both halves agree on the same waveform.
        iq = _call_iq(ran=RAN, source_id=SRC, destination_id=DST, superframes=2)
        feats = None
        try:
            from survey.identify import iq_features
            feats = iq_features(iq, FS)
        except Exception:                                       # noqa: BLE001
            raise unittest.SkipTest("identify.iq_features unavailable")
        self.assertTrue(nxdn.looks_like_nxdn(feats),
                        "gate rejected a call the decoder decodes: %r" % (feats,))
        self.assertTrue(decode_nxdn(iq, FS)["calls"])


class TestRouterRegistration(unittest.TestCase):
    def test_nxdn_is_routable(self):
        from survey import decode_router
        self.assertIn("nxdn", decode_router.DECODE_MODES)
        self.assertIn("nxdn", decode_router._ORDER)
        self.assertIn("nxdn", decode_router._decoders())

    def test_router_decodes_a_preferred_nxdn_capture(self):
        from survey.decode_router import route_decode
        iq = _call_iq(ran=RAN, source_id=SRC, destination_id=DST,
                      cipher_type=0b11, key_id=3, superframes=2)
        r = route_decode(iq, FS, None, prefer="nxdn")
        self.assertTrue(r["messages"])
        self.assertEqual((r["protocol"] or "").upper(), "NXDN")
        self.assertTrue(r["encrypted"])
        self.assertIn("ENCRYPTED", r["messages"][0]["summary"])

    def test_router_honesty_gate_holds_on_noise(self):
        from survey.decode_router import route_decode
        rng = np.random.default_rng(24)
        n = 200_000
        iq = (rng.standard_normal(n) + 1j * rng.standard_normal(n)).astype(np.complex64)
        r = route_decode(iq, 1_000_000.0, None, prefer="nxdn")
        self.assertEqual(r["messages"], [])
        self.assertIsNone(r["protocol"])

    def test_the_existing_decoders_are_still_registered(self):
        # this module's ONE router edit must not have displaced anybody
        from survey import decode_router
        for name in ("pocsag", "flex", "ook", "dmr", "p25", "video", "dtmf", "cw"):
            self.assertIn(name, decode_router._ORDER, name)
            self.assertIn(name, decode_router.DECODE_MODES, name)


if __name__ == "__main__":
    unittest.main()
