"""NXDN (NEXEDGE / IDAS) frame + metadata decoder — the third digital LMR mode.

NXDN is the Icom / JVC KENWOOD digital land-mobile standard: 4-level FSK, FDMA,
in a 6.25 kHz channel (NXDN48 — 2400 sym/s, 4800 bit/s) or a 12.5 kHz channel
(NXDN96 — 4800 sym/s, 9600 bit/s). On a sweep it sits alongside DMR and P25 as
the third thing a private security detail, a utility crew or a surveillance team
is likely to be carrying, and it is the one most often mistaken for analog FM
because a 6.25 kHz NXDN48 carrier is narrower than an analog channel.

This module is the FRAME LAYER. It takes a recovered DIBIT stream (2 bits per
symbol, from survey/c4fm.py at either symbol rate) and pulls out WHO is talking
to WHOM and WHETHER IT IS ENCRYPTED. It does NOT do audio — the AMBE+2 vocoder
lives behind survey/ambe.py; we stop at locating, counting and handing over the
voice-frame bits.

What comes out
--------------
  * RAN    — the 6-bit Radio Access Number, NXDN's colour code. Identifies the
             system (a conventional access code, or Site Code mod 64 on a
             trunked system) independently of frequency.
  * LICH   — RF channel type (RCCH / RTCH / RDCH / RTCH_C), functional channel
             (SACCH / UDCH / CAC), the steal flags, and the link direction.
  * IDs    — 16-bit Source Unit ID and 16-bit Destination Group-or-Unit ID, plus
             the 3-bit Call Type that says which of those the destination is.
  * CIPHER — the 2-bit Cipher Type and the 6-bit Key ID. 00 is the ONLY value
             that means clear; 01/10/11 are Scramble / DES / AES. A VCALL_IV
             message's 64-bit initialisation vector is reported too: the spec
             forbids an all-zero IV, so seeing one means a key is actually
             loaded — and on a capture that started mid-call it may be the only
             encryption evidence on air.

ENCRYPTION IS A RESULT, NOT A FAILURE (this is the point of the module). A field
line reading "NXDN96, RAN 1, group call from unit 2621 to group 105, Cipher Type
3 (AES), key ID 0x07 — ENCRYPTED" is a complete, defensible finding: the
operator now knows a digital radio system is live on site, which system it is,
who is keyed up on it, and that the traffic cannot be read. That goes in the
ledger as a positive identification. It is NEVER reported as a failed decode, as
silence, or as "no signal" — those are different facts and conflating them would
misrepresent the sweep.

Chain implemented here
----------------------
  dibits -> FSW lock (20-bit frame sync 0xCDF59, bit-error tolerant)
         -> per-symbol descramble (PN x^9+x^4+1, re-seeded every frame)
         -> LICH: 7 bits + even parity over its 4 MSBs, carried one bit per
            symbol on the outer rails only (that rail restriction is a second,
            scrambler-independent gate)
         -> the LICH names the rest of the frame:
              RTCH/RDCH voice : SACCH(60) + two 144-bit slots, each slot either
                                VCH (2x72 EHR or 1x144 EFR) or FACCH1(144)
              RTCH/RDCH data  : UDCH or FACCH2 (348)
              RCCH            : CAC outbound(300) / long or short CAC inbound(252)
         -> per channel: deinterleave -> depuncture -> K=5 R=1/2 Viterbi ->
            CRC-6 / 12 / 15 / 16 -> SR(structure + RAN) + layer-3 message
         -> frame-grid filter (a transmission's frames all share one phase
            modulo 192 symbols; coincidences do not)
         -> SACCH superframe reassembly (4 x 18 bits -> one 72-bit message)
         -> VCALL parse -> call assembly

Sources, and their licences (this is a closed-source commercial product)
-----------------------------------------------------------------------
EVERY constant below comes from ONE document: the NXDN Forum's own

    NXDN Technical Specifications, Part 1: Air Interface, Sub-part A: Common Air
    Interface — NXDN TS 1-A Version 1.3, November 2011.
    Copyright (c) 2007-2012 JVC KENWOOD Corporation and Icom Incorporated.

which the NXDN Steering Committee released for public distribution. Section and
table numbers are cited inline against each constant. What is taken is FACTUAL
protocol data — polynomials, field widths, bit assignments — not the document's
text, figures or any expression, and no code was copied from anywhere. NO GPL
source was consulted: dsd/dsd-fme, op25 and jmbe were deliberately not opened,
so nothing here can be a derivative of them. (Precedent in this codebase:
mbelib is ISC and usable; mbelib-neo, op25 and jmbe are GPL and are not.)
Nothing here required a constant that exists only inside GPL code — the
published specification was sufficient for the whole chain.

VALIDATION STATUS — read this before quoting a result in a report
-----------------------------------------------------------------
NOTHING IN THIS MODULE HAS BEEN VALIDATED AGAINST A REAL OFF-AIR NXDN SIGNAL.
There is no off-air NXDN capture anywhere in this project, and no result here
has ever been cross-checked against a Kenwood or Icom radio.

Everything asserted is one of two things, and the distinction matters:

  A. INDEPENDENTLY ANCHORED — correct by construction against the published
     specification, not by agreeing with our own encoder:
       - FSW 0xCDF59 is Table 4.4-2's ten symbols, and tests/test_nxdn.py
         rebuilds it from that symbol list rather than trusting the hex.
       - The preamble 0x5775FD is Table 4.4-1, rebuilt the same way.
       - The convolutional code is section 4.5.1.1's K=5, R=1/2 with
         G1 = 1 + D^3 + D^4 and G2 = 1 + D + D^2 + D^4. In octal that is
         (23, 35) — the classic K=5 code whose FREE DISTANCE IS 7 — and the
         tests compute the free distance from the trellis and pin it at exactly
         7. A wrong generator changes that number and fails loudly.
       - The four CRC polynomials, the four puncturing matrices, the four
         interleaving depths, the 384-bit frame layout, the LICH bit map, the
         scrambler polynomial and its seed, the 16-bit ID widths, the Call Type
         and Cipher Type tables: all cited to their section/table below.

  B. SYNTHETIC ONLY — proven to round-trip through this module's own encoder
     and c4fm.encode_c4fm() IQ, which proves the layers agree with each other,
     NOT that they agree with a real radio. Every extracted field (RAN, unit
     IDs, call type, cipher type, key ID, voice-frame count) is in this class.

ASSUMED, and the most likely things to be wrong off-air:
  - The scrambler's OUTPUT TAP and shift direction are read off Figure 4.6-1 as
    "output = S0 before the shift; feedback S4 xor S0 into S8". The polynomial
    and the seed are stated in text, but a one-step phase error in the PN
    sequence is not something a figure can rule out. If this is wrong, the FSW
    still locks and the LICH's rail test still passes, but every CRC fails —
    i.e. it fails LOUDLY, and `stats["lich_ok"] > 0 with zero crc_ok` is its
    signature.
  - The CRC shift-register topology is the standard "XOR the input bit with the
    register MSB, shift, conditionally XOR the polynomial" form, seeded all-ones
    per section 4.5.5 with no final complement. Section 4.5.5 describes this in
    words and a figure, not in equations.
  - Section 6.4.1.1's body text says the VCALL Call Type is at "Octet 6; Bits
    7-5", which contradicts its own Figure 6.4-1 (Octet 2, bits 7-5) and would
    place Call Type inside the destination ID. The FIGURE is followed.
  - EHR vs EFR (2x72-bit vs 1x144-bit voice frames per slot) is not in the LICH;
    it is only in the VCALL's Transmission Mode. Without a decoded VCALL this
    module assumes EHR and SAYS SO in `voice_mode` ("EHR (assumed)").

Not implemented, and said rather than faked: trunking control-channel message
semantics beyond VCALL/TX_REL naming, data-call reassembly, the RCCH collision
control (E) and guard fields, and any attempt at the vocoder.

False positives are the failure mode that would actually damage a client report,
so the gate is FOUR independent stages and all four must clear: the 20-bit FSW;
the LICH (outer-rail restriction plus its parity bit); the surviving frames
sitting on ONE 192-symbol frame grid; and then a channel CRC. The 20-bit sync is
deliberately NOT carrying this — at ~2e-4 per position it cannot, and a decoder
that leaned on it would false-lock its way through any long sweep. The strength
is in the three structural tests behind it. tests/test_nxdn.py MEASURES the
result rather than asserting it: 6 million random dibits produce ZERO reported
frames, and noise, analog FM, narrowband analog FM at the NXDN48 channel width,
an unmodulated carrier, random C4FM, a real DMR transmission and a real P25 call
(with and without status symbols) all produce ZERO NXDN frames.

Constraints: numpy only, no new dependencies. Receive-only. survey/c4fm.py (IQ ->
dibits, owned separately) is imported LAZILY inside decode_nxdn(), following the
decode_router._decoders() pattern, so this module imports and its whole frame
layer tests cleanly whether or not c4fm is present.
"""

import numpy as np

# ------------------------------------------------------------ modulation (§2.3, §3)
# Table 2.3-1 Transfer Method Specifications. NXDN names its two rates by BIT
# rate; the SYMBOL rate is half that because 4-level FSK carries a dibit per
# symbol. survey/c4fm.py takes the symbol rate, so both forms are kept here.
SYMBOL_RATE_NXDN48 = 2400.0        # 4800 bit/s, 6.25 kHz channel
SYMBOL_RATE_NXDN96 = 4800.0        # 9600 bit/s, 12.5 kHz channel
SYMBOL_RATES = (SYMBOL_RATE_NXDN96, SYMBOL_RATE_NXDN48)
CHANNEL_KHZ = {SYMBOL_RATE_NXDN48: 6.25, SYMBOL_RATE_NXDN96: 12.5}
RATE_NAMES = {SYMBOL_RATE_NXDN48: "NXDN48 (4800 bit/s, 6.25 kHz)",
              SYMBOL_RATE_NXDN96: "NXDN96 (9600 bit/s, 12.5 kHz)"}

# Table 3.3-1 Symbol Mapping. The dibit -> rail map is IDENTICAL to TIA-102 C4FM
# and ETSI DMR (01 -> +3, 00 -> +1, 10 -> -1, 11 -> -3), which is why c4fm.py
# serves all three protocols unchanged. Only the deviations differ.
DEVIATION_OUTER_HZ = {SYMBOL_RATE_NXDN48: 1050.0, SYMBOL_RATE_NXDN96: 2400.0}
DEVIATION_INNER_HZ = {SYMBOL_RATE_NXDN48: 350.0, SYMBOL_RATE_NXDN96: 800.0}

# §3.4 / §3.5. NXDN's transmit chain is a SQUARE-ROOT raised cosine H(f) followed
# by a sinc P(f); the receiver uses the same H(f) plus an inverse-sinc D(f). The
# roll-off is stated explicitly in both sections: alpha = 0.2. That is the SAME
# roll-off figure TIA-102 uses for P25 C4FM, so c4fm.RRC_BETA needs no override —
# but the filters are NOT the same shape (TIA-102 specifies a full raised cosine
# at the transmitter, NXDN a root raised cosine plus the sinc/inverse-sinc pair),
# so this is a coincidence of one number, not an equivalence. Checked, not assumed.
RRC_BETA = 0.2

# ------------------------------------------------------------ frame geometry (§4.4)
FRAME_BITS = 384                   # one frame: 80 ms at 4800 bit/s, 40 ms at 9600
FRAME_DIBITS = FRAME_BITS // 2     # 192 symbols

# Table 4.4-2 Frame Sync Word: 10 symbols, -3 +1 -3 +3 -3 -3 +3 +3 -1 +3.
# tests/test_nxdn.py rebuilds this hex from that symbol list, so the value here
# is checked against the spec rather than trusted.
FSW = 0xCDF59
FSW_BITS = 20
FSW_DIBITS = FSW_BITS // 2         # 10 symbols
# 20 bits is a much shorter anchor than DMR's or P25's 48, so it CANNOT carry the
# honesty gate alone: P(a random 20-bit window lands within 2 bits) ~ 2.0e-4.
# It is the cheap first stage only; the LICH and a channel CRC are the other two.
FSW_MAX_BIT_ERRS = 2

# Table 4.4-1 Preamble: 24 bits (12 symbols) prepended at the start of an RDCH
# transmission. Not a gate — it is generated by the encoder so a synthetic
# capture gives the timing loop something to acquire on, as a real one does.
PREAMBLE = 0x5775FD
PREAMBLE_BITS = 24

# Figures 4.4-2 / 4.4-4 and Figure 4.6-2. Offsets are BIT offsets from the start
# of the frame (i.e. from the first FSW bit).
LICH_OFFSET_BITS = 20
LICH_BITS = 16                     # 8 symbols, one control bit per symbol
LICH_DIBITS = LICH_BITS // 2
SACCH_OFFSET_BITS = 36
SACCH_BITS = 60
SLOT1_OFFSET_BITS = 96             # first 144-bit slot: VCH pair or FACCH1
SLOT2_OFFSET_BITS = 240            # second 144-bit slot
SLOT_BITS = 144
USER_OFFSET_BITS = 36              # RTCH/RDCH data: UDCH or FACCH2
USER_BITS = 348
CAC_OFFSET_BITS = 36
CAC_OUT_BITS = 300                 # RCCH outbound
CAC_IN_BITS = 252                  # RCCH inbound (long or short)

# §7.1 Voice Coding Method. EHR: 49-bit AMBE+2 frame + 23 FEC bits = 72 bits, two
# per slot. EFR (9600 bit/s only): 88-bit frame + 56 FEC = 144 bits, one per slot.
VCH_BITS_EHR = 72
VCH_BITS_EFR = 144

# ------------------------------------------------------------ LICH (§5.2, §4.5.3)
# Table 5.2-1: 7 control bits, MSB first —
#   bits 6..5 RF Channel Type | 4..3 Functional Channel Type | 2..1 Option | 0 Direction
RF_RCCH = 0b00
RF_RTCH = 0b01
RF_RDCH = 0b10
RF_RTCH_C = 0b11
RF_NAMES = {RF_RCCH: "RCCH (control channel)", RF_RTCH: "RTCH (trunked traffic)",
            RF_RDCH: "RDCH (conventional traffic)",
            RF_RTCH_C: "RTCH_C (composite control)"}
TRAFFIC_RF = (RF_RTCH, RF_RDCH, RF_RTCH_C)

USC_SACCH_SINGLE = 0b00            # SACCH, non-superframe structure
USC_UDCH = 0b01
USC_SACCH_SUPER = 0b10             # SACCH, superframe structure
USC_SACCH_IDLE = 0b11              # SACCH superframe / Idle
USC_NAMES = {USC_SACCH_SINGLE: "SACCH (single)", USC_UDCH: "UDCH",
             USC_SACCH_SUPER: "SACCH (superframe)",
             USC_SACCH_IDLE: "SACCH (superframe) / idle"}
SACCH_USC = (USC_SACCH_SINGLE, USC_SACCH_SUPER, USC_SACCH_IDLE)

CAC_OUT = 0b00                     # outbound CAC (the only outbound CAC type)
CAC_LONG_IN = 0b01                 # inbound Long CAC
CAC_SHORT_IN = 0b11                # inbound Short CAC

# Steal Flag (Option field) when the USC is a SACCH. NOTE the sense: 0b10 steals
# the SECOND 144-bit slot, 0b01 steals the FIRST.
STEAL_NONE = 0b11                  # both slots are VCH
STEAL_SECOND = 0b10                # slot 2 is FACCH1, slot 1 is VCH
STEAL_FIRST = 0b01                 # slot 1 is FACCH1, slot 2 is VCH
STEAL_BOTH = 0b00                  # both slots are FACCH1
# ... and when the USC is UDCH: 0b11 = UDCH, 0b00 = FACCH2, others reserved.
UDCH_NO_STEAL = 0b11
UDCH_FACCH2 = 0b00

DIRECTION_NAMES = {0: "inbound (subscriber -> repeater)",
                   1: "outbound (repeater -> subscriber)"}

# §4.5.3: 1 bit of EVEN parity whose range is the 4 MOST SIGNIFICANT bits of the
# 7-bit control word, then each of the resulting 8 bits is sent as one SYMBOL:
# a 1 becomes dibit 0b11 (-3) and a 0 becomes dibit 0b01 (+3). Both are OUTER
# rails, so every LICH symbol has its low bit set — and the per-symbol scrambler
# only ever flips the HIGH bit, so that restriction survives scrambling. It is a
# free, scrambler-independent 8-bit structural test and this module uses it.
LICH_BIT_TO_DIBIT = {0: 0b01, 1: 0b11}
LICH_PARITY_MASK = 0b1111000       # the 4 MSBs of the 7-bit control word
LICH_MAX_RAIL_ERRS = 1             # of 8 symbols

# ------------------------------------------------------------ SR (§6.3)
SR_BITS = 8                        # Structure(2) | Radio Access Number(6)
RAN_BITS = 6
# Table 6.3-2, SACCH on RTCH/RDCH. The 72-bit superframe message is cut into four
# 18-bit parts and the structure field numbers them BACKWARDS: 0b11 is the head.
SACCH_STRUCTURE_PART = {0b11: 0, 0b10: 1, 0b01: 2, 0b00: 3}
SACCH_PARTS = 4
SACCH_PART_BITS = 18
SACCH_MESSAGE_BITS = SACCH_PARTS * SACCH_PART_BITS      # 72 = 9 octets

# ------------------------------------------------------------ FEC (§4.5)
# All six functional channels share ONE convolutional code (sections 4.5.1.1,
# 4.5.1.2, 4.5.1.3, 4.5.2.1, 4.5.2.2, 4.5.2.3 all state it identically):
#   constraint length K = 5, rate R = 1/2,
#   G1(D) = 1 + D^3 + D^4        G2(D) = 1 + D + D^2 + D^4
#   "the output bits must be read alternately in the order of G1, G2"
# As 5-bit taps with D^0 in the MSB that is (23, 35) octal — the standard K=5
# code with FREE DISTANCE 7. tests/test_nxdn.py derives the free distance from
# the trellis and pins it at exactly 7, so a wrong generator cannot pass.
CONV_K = 5
CONV_STATES = 1 << (CONV_K - 1)    # 16
CONV_G1_TAPS = (0, 3, 4)
CONV_G2_TAPS = (0, 1, 2, 4)
CONV_TAIL_BITS = 4                 # "four tail bits, all set equal to zero"

# §4.5.5 CRC Code. Polynomials written WITHOUT the leading x^width term. The
# shift register is seeded to all ones and its value when the last message bit
# has been entered IS the CRC (no final complement).
CRC16_POLY = 0x1021      # x^16 + x^12 + x^5 + 1                     (CAC)
CRC15_POLY = 0x4CC5      # x^15+x^14+x^11+x^10+x^7+x^6+x^2+1  (UDCH / FACCH2)
CRC12_POLY = 0x80F       # x^12 + x^11 + x^3 + x^2 + x + 1           (FACCH1)
CRC6_POLY = 0x27         # x^6 + x^5 + x^2 + x + 1                   (SACCH)

# §4.6 Scrambler Method. PN generator x^9 + x^4 + 1, re-seeded EVERY FRAME with
# Table 4.6-1's default. Scrambling multiplies each SYMBOL by +1 (PN bit 0) or
# -1 (PN bit 1) — and negating a 4-FSK rail maps +3<->-3 and +1<->-1, which is
# exactly dibit ^ 0b10. So descrambling is one XOR, and it is its own inverse.
SCRAMBLER_INIT = 0b011100100       # S8..S0 = 0,1,1,1,0,0,1,0,0
SCRAMBLER_TAP = 4                  # feedback = S4 xor S0 (see Figure 4.6-1)
SCRAMBLER_PERIOD = (1 << 9) - 1    # 511; a maximal-length sequence
# Figure 4.6-2 Scramble Range: everything from the first LICH symbol to the end
# of the frame is scrambled; the FSW, preamble, guard and post fields are not.
SCRAMBLE_START_DIBIT = FSW_DIBITS  # 10
SCRAMBLE_DIBITS = FRAME_DIBITS - FSW_DIBITS      # 182 -> S0 .. S181

# ------------------------------------------------------------ layer 3 (§6.4, §6.5)
MSG_VCALL = 0b000001               # Voice Call            (Table 6.4-1)
MSG_VCALL_REC_REQ = 0b000010
MSG_VCALL_IV = 0b000011            # Voice Call Initialization Vector
MSG_VCALL_ASSGN = 0b000100
MSG_VCALL_ASSGN_DUP = 0b000101
MSG_TX_REL_EX = 0b000111
MSG_TX_REL = 0b001000              # Transmission Release
MSG_DCALL_HEADER = 0b001001
MSG_DCALL_REC_REQ = 0b001010
MSG_DCALL_DATA = 0b001011
MSG_DCALL_ACK = 0b001100
MSG_DCALL_ASSGN_DUP = 0b001101
MSG_DCALL_ASSGN = 0b001110
MSG_HEAD_DLY = 0b001111
MSG_IDLE = 0b010000
MSG_DISC = 0b010001
# Names as read on a TRAFFIC channel. The same 6-bit values mean different
# messages on a trunking control channel (VCALL_REQ/RESP share 000001 with
# VCALL, for instance), so this table is only applied where the LICH said the
# frame is RTCH/RDCH; on RCCH the type is reported as a number plus raw hex.
MSG_NAMES = {
    MSG_VCALL: "VCALL (voice call)",
    MSG_VCALL_REC_REQ: "VCALL_REC_REQ (voice call paging)",
    MSG_VCALL_IV: "VCALL_IV (encryption initialisation vector)",
    MSG_VCALL_ASSGN: "VCALL_ASSGN (traffic channel assignment)",
    MSG_VCALL_ASSGN_DUP: "VCALL_ASSGN_DUP",
    MSG_TX_REL_EX: "TX_REL_EX (transmission released)",
    MSG_TX_REL: "TX_REL (transmission released)",
    MSG_DCALL_HEADER: "DCALL (data call header)",
    MSG_DCALL_REC_REQ: "DCALL_REC_REQ (data call paging)",
    MSG_DCALL_DATA: "DCALL (data call user data)",
    MSG_DCALL_ACK: "DCALL_ACK",
    MSG_DCALL_ASSGN_DUP: "DCALL_ASSGN_DUP",
    MSG_DCALL_ASSGN: "DCALL_ASSGN",
    MSG_HEAD_DLY: "HEAD_DLY",
    MSG_IDLE: "IDLE",
    MSG_DISC: "DISC / DISC_REQ (disconnect)",
    0b111000: "SDCALL_REQ (short data header)",
    0b111001: "SDCALL_REQ (short data)",
    0b111010: "SDCALL_IV (short data init vector)",
    0b111011: "SDCALL_RESP",
    0b110000: "STAT_INQ_REQ", 0b110001: "STAT_INQ_RESP",
    0b110010: "STAT_REQ", 0b110011: "STAT_RESP",
    0b110100: "REM_CON_REQ (remote control)",
    0b110101: "REM_CON_RESP",
}
# Messages that carry the full VCALL octet layout of Figure 6.4-1.
VCALL_MESSAGES = (MSG_VCALL,)

# Table 6.5-8 Call Type (3 bits).
CALL_TYPE_NAMES = {
    0b000: "broadcast call (unidirectional group)",
    0b001: "conference call (group)",
    0b010: "unspecified",
    0b011: "reserved",
    0b100: "individual call",
    0b101: "reserved",
    0b110: "interconnect (PSTN) call",
    0b111: "speed-dial (PSTN) call",
}
GROUP_CALL_TYPES = (0b000, 0b001)          # destination ID is a GROUP ID
UNIT_CALL_TYPES = (0b100,)                 # destination ID is a UNIT ID

# Table 6.5-24 Cipher Type (2 bits). 00 is the ONLY value that means clear.
CIPHER_CLEAR = 0b00
CIPHER_NAMES = {
    0b00: "unencrypted (clear)",
    0b01: "Scramble mode (voice inversion / proprietary scrambler)",
    0b10: "DES",
    0b11: "AES",
}

# Table 6.5-9 Voice Call Option -> Transmission Mode (bits 2..0).
TXMODE_NAMES = {0b000: "4800 bit/s EHR", 0b010: "9600 bit/s EHR",
                0b011: "9600 bit/s EFR"}
TXMODE_EFR = 0b011

MAX_FRAMES = 512                   # bound the work on a long capture
MIN_FRAMES_WITHOUT_CRC = 3         # honesty gate — see decode_nxdn_dibits()


# ---------------------------------------------------------------- dibit helpers
def dibits_to_bits(dibits) -> np.ndarray:
    """Dibit array (values 0..3) -> bit array, MSB of each dibit first."""
    d = np.asarray(dibits, dtype=np.uint8)
    out = np.empty(d.size * 2, dtype=np.uint8)
    out[0::2] = (d >> 1) & 1
    out[1::2] = d & 1
    return out


def bits_to_dibits(bits) -> np.ndarray:
    """Bit array (even length) -> dibit array, MSB-first pairs."""
    b = np.asarray(bits, dtype=np.uint8)
    if b.size % 2:
        b = b[:b.size - 1]
    return ((b[0::2] << 1) | b[1::2]).astype(np.uint8)


def invert_dibits(dibits) -> np.ndarray:
    """C4FM polarity flip: every rail negates, i.e. dibit ^ 0b10. NOT a bit-stream
    complement — this mirrors the operation survey/c4fm.py documents on its own
    output, and it is also (not coincidentally) the scrambler's operation."""
    return (np.asarray(dibits, dtype=np.uint8) ^ 0b10).astype(np.uint8)


def _bits_to_int(bits) -> int:
    """MSB-first bit sequence -> integer."""
    v = 0
    for b in bits:
        v = (v << 1) | (int(b) & 1)
    return v


def _int_to_bits(value: int, width: int) -> list:
    return [(value >> k) & 1 for k in range(width - 1, -1, -1)]


def _popcount(x: int) -> int:
    return bin(x).count("1")


def _bits_hex(bits) -> str:
    """MSB-first bit list -> hex string, left-padded to whole nibbles."""
    return "%0*X" % ((len(bits) + 3) // 4, _bits_to_int(bits))


def _bits_to_octets(bits) -> list:
    return [_bits_to_int(bits[i:i + 8]) for i in range(0, len(bits) - 7, 8)]


# bit distance between two dibit values — the FSW search metric
_DIBIT_ERR = np.array([[_popcount(a ^ b) for b in range(4)] for a in range(4)],
                      dtype=np.uint8)

# frame sync expressed as 10 dibits (derived, never hand-typed)
FSW_DIBIT_PATTERN = bits_to_dibits(_int_to_bits(FSW, FSW_BITS))
assert FSW_DIBIT_PATTERN.size == FSW_DIBITS


# ---------------------------------------------------------------- scrambler (§4.6)
def scrambler_sequence(n: int) -> np.ndarray:
    """First `n` bits of the NXDN scrambling PN sequence.

    x^9 + x^4 + 1 in the Fibonacci form Figure 4.6-1 draws: the register shifts
    S8 -> S7 -> ... -> S0, S0 is the output stage, and S4 xor S0 is fed back into
    S8. The seed is Table 4.6-1. Reading the OUTPUT TAP off a figure is the one
    judgement call in this function and it is flagged in the module docstring —
    a phase error here fails every CRC rather than corrupting a field quietly.

    The sequence is maximal-length (period 511) and tests/test_nxdn.py asserts
    that, which is the structural property a wrong tap set would break."""
    reg = SCRAMBLER_INIT
    out = np.empty(max(0, int(n)), dtype=np.uint8)
    for i in range(out.size):
        b = reg & 1                                   # S0 = output stage
        out[i] = b
        fb = ((reg >> SCRAMBLER_TAP) & 1) ^ b
        reg = (reg >> 1) | (fb << 8)
    return out


_SCRAMBLE_MASK = None


def _scramble_mask() -> np.ndarray:
    """The 182-symbol per-frame XOR mask, cached. A PN bit of 1 inverts the
    symbol, and inverting a rail is dibit ^ 0b10."""
    global _SCRAMBLE_MASK
    if _SCRAMBLE_MASK is None:
        _SCRAMBLE_MASK = (scrambler_sequence(SCRAMBLE_DIBITS) << 1).astype(np.uint8)
    return _SCRAMBLE_MASK


def scramble_frame_dibits(frame_dibits) -> np.ndarray:
    """Apply (or remove — it is an involution) the scrambler to one 192-symbol
    frame, leaving the 10 FSW symbols alone. Anything shorter than a full frame
    is masked as far as it goes, which is what lets a truncated capture still
    surface its LICH."""
    d = np.array(frame_dibits, dtype=np.uint8, copy=True)
    n = min(d.size - SCRAMBLE_START_DIBIT, SCRAMBLE_DIBITS)
    if n > 0:
        d[SCRAMBLE_START_DIBIT:SCRAMBLE_START_DIBIT + n] ^= _scramble_mask()[:n]
    return d


# ---------------------------------------------------------------- CRC (§4.5.5)
def nxdn_crc(bits, poly: int, width: int) -> int:
    """CRC over an MSB-first bit sequence.

    Section 4.5.5: "The encoder enters the data to the shift register from the
    highest order, and generates the shift register value at the time when the
    last bit is entered. Default values of the shift register shall be set to 1."
    That is the classic feedback form — XOR the incoming bit with the register's
    top bit, shift left, and conditionally XOR the polynomial — seeded all ones,
    with no final complement. `poly` omits the leading x^width term."""
    mask = (1 << width) - 1
    reg = mask
    top = 1 << (width - 1)
    for b in bits:
        fb = ((reg & top) >> (width - 1)) ^ (int(b) & 1)
        reg = (reg << 1) & mask
        if fb:
            reg ^= poly
    return reg


# ------------------------------------------------- convolutional code (§4.5.x)
def _conv_out(state: int, bit: int):
    """(G1 output, G2 output) for input `bit` from `state`.

    The state holds the four previous inputs with the MOST RECENT in bit 0, so
    bit k of the state is D^(k+1)."""
    reg = (bit & 1) | (state << 1)                    # reg bit k is D^k
    o1 = 0
    for t in CONV_G1_TAPS:
        o1 ^= (reg >> t) & 1
    o2 = 0
    for t in CONV_G2_TAPS:
        o2 ^= (reg >> t) & 1
    return o1, o2


# (state, input) -> (out1, out2, next_state); 32 entries, built once from the
# generator polynomials so the encoder and the Viterbi cannot disagree.
_CONV_TABLE = [[None, None] for _ in range(CONV_STATES)]
for _s in range(CONV_STATES):
    for _b in (0, 1):
        _o1, _o2 = _conv_out(_s, _b)
        _CONV_TABLE[_s][_b] = (_o1, _o2, ((_s << 1) | _b) & (CONV_STATES - 1))
del _s, _b, _o1, _o2


def conv_encode(bits) -> list:
    """Rate-1/2 K=5 convolutional encoding, G1 then G2 for each input bit. The
    caller appends the four zero tail bits (the spec puts them in the message,
    not in the encoder)."""
    state, out = 0, []
    for b in bits:
        o1, o2, state = _CONV_TABLE[state][int(b) & 1]
        out.append(o1)
        out.append(o2)
    return out


def conv_decode(code_bits, valid=None):
    """Viterbi over the 16-state trellis. Returns (decoded_bits, path_metric).

    `valid` is an optional per-code-bit mask; a 0 marks a bit that puncturing
    erased, and an erased bit contributes NOTHING to the metric (rather than
    being guessed at, which would bias the survivor selection). The path is
    terminated in state 0 because the message carries four zero tail bits; if
    state 0 is somehow unreachable the best surviving state is used instead and
    the caller's CRC decides."""
    n = len(code_bits) // 2
    if n == 0:
        return [], 0
    if valid is None:
        valid = [1] * (2 * n)
    inf = 1 << 30
    metric = [inf] * CONV_STATES
    metric[0] = 0
    back = []
    for t in range(n):
        r0, r1 = int(code_bits[2 * t]) & 1, int(code_bits[2 * t + 1]) & 1
        m0, m1 = int(valid[2 * t]), int(valid[2 * t + 1])
        new = [inf] * CONV_STATES
        prev = [0] * CONV_STATES
        for s in range(CONV_STATES):
            ms = metric[s]
            if ms >= inf:
                continue
            row = _CONV_TABLE[s]
            for b in (0, 1):
                o1, o2, ns = row[b]
                cost = ms
                if m0 and o1 != r0:
                    cost += 1
                if m1 and o2 != r1:
                    cost += 1
                if cost < new[ns]:
                    new[ns] = cost
                    prev[ns] = s
        metric = new
        back.append(prev)
    s = 0 if metric[0] < inf else int(min(range(CONV_STATES), key=lambda i: metric[i]))
    best = metric[s]
    out = []
    for t in range(n - 1, -1, -1):
        out.append(s & 1)                             # the input bit IS the new LSB
        s = back[t][s]
    out.reverse()
    return out, best


# -------------------------------------------------- puncturing + interleaving
def puncture(code_bits, matrix) -> list:
    """Erase the code bits marked 0 in the 2 x P puncturing matrix.

    Row 0 gates the G1 outputs, row 1 the G2 outputs, and the column advances
    once per INPUT bit (section 4.5.4 works the CAC example through explicitly:
    with [1111111 / 1011101], X4 and X12 are the erased bits)."""
    row1, row2 = matrix
    p = len(row1)
    out = []
    for i in range(len(code_bits) // 2):
        j = i % p
        if row1[j]:
            out.append(int(code_bits[2 * i]) & 1)
        if row2[j]:
            out.append(int(code_bits[2 * i + 1]) & 1)
    return out


def depuncture(punctured, matrix, n_input_bits: int):
    """Inverse of puncture(): (code_bits, valid_mask), with a 0 inserted at every
    erased position and the mask marking it as carrying no information."""
    row1, row2 = matrix
    p = len(row1)
    code = [0] * (2 * n_input_bits)
    valid = [0] * (2 * n_input_bits)
    k = 0
    for i in range(n_input_bits):
        j = i % p
        if row1[j]:
            if k < len(punctured):
                code[2 * i] = int(punctured[k]) & 1
                valid[2 * i] = 1
            k += 1
        if row2[j]:
            if k < len(punctured):
                code[2 * i + 1] = int(punctured[k]) & 1
                valid[2 * i + 1] = 1
            k += 1
    return code, valid


def interleave(bits, depth: int) -> list:
    """Figure 4.5-11: write the punctured stream row-major into a
    `depth` x (len/depth) matrix, read it column-major."""
    n = len(bits)
    if depth <= 0 or n % depth:
        raise ValueError("interleave depth %d does not divide %d bits" % (depth, n))
    cols = n // depth
    out = [0] * n
    for r in range(depth):
        for c in range(cols):
            out[c * depth + r] = int(bits[r * cols + c]) & 1
    return out


def deinterleave(bits, depth: int) -> list:
    n = len(bits)
    if depth <= 0 or n % depth:
        raise ValueError("interleave depth %d does not divide %d bits" % (depth, n))
    cols = n // depth
    out = [0] * n
    for r in range(depth):
        for c in range(cols):
            out[r * cols + c] = int(bits[c * depth + r]) & 1
    return out


# ------------------------------------------------------- channel coding profiles
# One row per functional channel, all from section 4.5. `message` is the layer-3
# payload in bits; `sr` says whether the 8-bit SR (structure + RAN) precedes it;
# `null` is the run of zero bits section 4.5.1 inserts before the CRC on the CAC.
# The arithmetic is checked in tests: sr + message + null + crc + 4 tail bits,
# doubled by the rate-1/2 code, punctured, must equal `bits` exactly.
CHANNELS = {
    "SACCH": dict(                                                  # §4.5.2.1
        bits=SACCH_BITS, message=SACCH_PART_BITS, sr=True, null=0,
        crc=6, crc_poly=CRC6_POLY, depth=5,
        puncture=((1, 1, 1, 1, 1, 1), (1, 1, 0, 1, 1, 0))),
    "FACCH1": dict(                                                 # §4.5.2.2
        bits=SLOT_BITS, message=80, sr=False, null=0,
        crc=12, crc_poly=CRC12_POLY, depth=9,
        puncture=((1, 1), (0, 1))),
    "UDCH": dict(                                                   # §4.5.2.3
        bits=USER_BITS, message=176, sr=True, null=0,
        crc=15, crc_poly=CRC15_POLY, depth=29,
        puncture=((1,) * 7, (1, 0, 1, 1, 1, 0, 1))),
    "CAC_OUT": dict(                                                # §4.5.1.1
        bits=CAC_OUT_BITS, message=144, sr=True, null=3,
        crc=16, crc_poly=CRC16_POLY, depth=25,
        puncture=((1,) * 7, (1, 0, 1, 1, 1, 0, 1))),
    "CAC_LONG": dict(                                               # §4.5.1.2
        bits=CAC_IN_BITS, message=128, sr=True, null=0,
        crc=16, crc_poly=CRC16_POLY, depth=21,
        puncture=((1,) * 13, (0, 1, 1, 0, 0, 0, 1, 1, 1, 0, 1, 1, 1))),
    "CAC_SHORT": dict(                                              # §4.5.1.3
        bits=CAC_IN_BITS, message=96, sr=True, null=2,
        crc=16, crc_poly=CRC16_POLY, depth=21,
        puncture=None),                                             # no puncturing
}
# FACCH2 is UDCH's coding exactly (section 4.5.2.3 defines them together); only
# the LICH steal flag distinguishes them on air.
CHANNELS["FACCH2"] = dict(CHANNELS["UDCH"])


def _channel_input_bits(prof: dict) -> int:
    """Number of bits fed to the convolutional encoder for this channel."""
    return ((SR_BITS if prof["sr"] else 0) + prof["message"] + prof["null"]
            + prof["crc"] + CONV_TAIL_BITS)


def encode_channel(name: str, message_bits, sr: int = 0) -> list:
    """Layer-3 message (+ SR) -> the channel's on-air bits.

    Shares the CRC, the convolutional encoder, the puncturing matrix and the
    interleaver with the decoder, so a wrong polynomial or a wrong depth fails
    in tests/test_nxdn.py rather than round-tripping through matched bugs."""
    prof = CHANNELS[name]
    msg = list(int(b) & 1 for b in message_bits)[:prof["message"]]
    msg += [0] * (prof["message"] - len(msg))
    body = (_int_to_bits(sr & 0xFF, SR_BITS) if prof["sr"] else []) + msg
    body += [0] * prof["null"]
    body += _int_to_bits(nxdn_crc(body, prof["crc_poly"], prof["crc"]), prof["crc"])
    body += [0] * CONV_TAIL_BITS
    if len(body) != _channel_input_bits(prof):
        raise AssertionError("%s: built %d bits, expected %d"
                             % (name, len(body), _channel_input_bits(prof)))
    coded = conv_encode(body)
    if prof["puncture"] is not None:
        coded = puncture(coded, prof["puncture"])
    if len(coded) != prof["bits"]:
        raise AssertionError("%s: punctured to %d bits, expected %d"
                             % (name, len(coded), prof["bits"]))
    return interleave(coded, prof["depth"])


def decode_channel(name: str, on_air_bits):
    """On-air bits -> dict(ok, sr, ran, structure, message, crc, metric).

    `ok` is True ONLY when the CRC recomputed over the recovered body matches the
    transmitted one. Nothing downstream reads `message` unless ok is True — a
    Viterbi decoder always produces SOME output, so the CRC is what separates a
    real frame from a confident hallucination."""
    prof = CHANNELS[name]
    out = {"channel": name, "ok": False, "sr": None, "ran": None,
           "structure": None, "message": None, "crc": None, "metric": None}
    bits = list(int(b) & 1 for b in on_air_bits)
    if len(bits) < prof["bits"]:
        out["truncated"] = True
        return out
    deint = deinterleave(bits[:prof["bits"]], prof["depth"])
    n_in = _channel_input_bits(prof)
    if prof["puncture"] is None:
        code, valid = deint, [1] * len(deint)
    else:
        code, valid = depuncture(deint, prof["puncture"], n_in)
    body, metric = conv_decode(code, valid)
    out["metric"] = int(metric)
    if len(body) < n_in:
        return out
    pos = 0
    if prof["sr"]:
        sr = _bits_to_int(body[:SR_BITS])
        out["sr"] = sr
        out["structure"] = (sr >> RAN_BITS) & 0x3
        out["ran"] = sr & ((1 << RAN_BITS) - 1)
        pos = SR_BITS
    out["message"] = body[pos:pos + prof["message"]]
    covered = body[:n_in - CONV_TAIL_BITS - prof["crc"]]
    got = _bits_to_int(body[n_in - CONV_TAIL_BITS - prof["crc"]:n_in - CONV_TAIL_BITS])
    out["crc"] = got
    out["ok"] = nxdn_crc(covered, prof["crc_poly"], prof["crc"]) == got
    if not out["ok"]:
        out["message"] = None
        out["ran"] = None
        out["structure"] = None
    return out


# ---------------------------------------------------------------- LICH (§4.5.3)
def lich_parity(control7: int) -> int:
    """Even parity over the 4 most significant bits of the 7-bit control word."""
    return _popcount(control7 & LICH_PARITY_MASK) & 1


def pack_lich(rf_type: int, fn_type: int, option: int, direction: int) -> int:
    """The 7-bit LICH control word (Table 5.2-1 field order)."""
    return (((rf_type & 0x3) << 5) | ((fn_type & 0x3) << 3)
            | ((option & 0x3) << 1) | (direction & 1))


def encode_lich_dibits(control7: int) -> np.ndarray:
    """7 control bits -> the 8 on-air symbols: parity appended, then one bit per
    symbol on the outer rails (1 -> 0b11 / -3, 0 -> 0b01 / +3)."""
    word8 = ((control7 & 0x7F) << 1) | lich_parity(control7)
    return np.array([LICH_BIT_TO_DIBIT[(word8 >> k) & 1] for k in range(7, -1, -1)],
                    dtype=np.uint8)


def decode_lich(dibits):
    """8 descrambled LICH symbols -> the parsed link information, or None.

    Two independent checks and BOTH must pass:
      1. every symbol must be an OUTER rail (low bit set). The scrambler only
         flips the high bit, so this holds whatever the PN phase is — which
         makes it the one LICH test that is immune to a scrambler mistake.
         LICH_MAX_RAIL_ERRS of the 8 may violate it before we give up.
      2. the even parity bit over the 4 MSBs must check out.
    A random 8-symbol window clears both with probability ~1.8 %, which is why
    the frame still has to produce a valid channel CRC before anything is
    reported."""
    d = np.asarray(dibits, dtype=np.uint8)
    if d.size < LICH_DIBITS:
        return None
    d = d[:LICH_DIBITS]
    rail_errs = int(np.count_nonzero((d & 1) == 0))
    if rail_errs > LICH_MAX_RAIL_ERRS:
        return None
    word8 = _bits_to_int([(int(v) >> 1) & 1 for v in d])
    control7 = (word8 >> 1) & 0x7F
    if lich_parity(control7) != (word8 & 1):
        return None
    rf = (control7 >> 5) & 0x3
    fn = (control7 >> 3) & 0x3
    return {"raw": control7, "rf_type": rf, "rf_name": RF_NAMES[rf],
            "fn_type": fn, "option": (control7 >> 1) & 0x3,
            "direction": control7 & 1,
            "direction_name": DIRECTION_NAMES[control7 & 1],
            "rail_errors": rail_errs,
            "fn_name": (USC_NAMES[fn] if rf in TRAFFIC_RF else "CAC")}


def frame_layout(lich: dict):
    """LICH -> [(channel_profile_name, bit_offset), ...] plus the voice slots.

    Returns (channels, voice_slot_offsets). `channels` are the FEC-protected
    fields to decode; `voice_slot_offsets` are the 144-bit slots that carry VCH
    rather than FACCH1. An unrecognised/reserved combination yields ([], []) —
    "not assessed", never a guess."""
    rf, fn, opt = lich["rf_type"], lich["fn_type"], lich["option"]
    if rf == RF_RCCH:
        if lich["direction"] == 1:
            return [("CAC_OUT", CAC_OFFSET_BITS)], []
        if fn == CAC_LONG_IN:
            return [("CAC_LONG", CAC_OFFSET_BITS)], []
        if fn == CAC_SHORT_IN:
            return [("CAC_SHORT", CAC_OFFSET_BITS)], []
        return [], []
    if fn == USC_UDCH:
        if opt == UDCH_NO_STEAL:
            return [("UDCH", USER_OFFSET_BITS)], []
        if opt == UDCH_FACCH2:
            return [("FACCH2", USER_OFFSET_BITS)], []
        return [], []
    if fn not in SACCH_USC:
        return [], []
    chans = [("SACCH", SACCH_OFFSET_BITS)]
    if fn == USC_SACCH_IDLE:
        # "It is not mandatory to receive from the next to SACCH" — the two slots
        # are idle, so they are neither decoded as FACCH1 nor counted as voice.
        return chans, []
    slots = [(SLOT1_OFFSET_BITS, opt in (STEAL_FIRST, STEAL_BOTH)),
             (SLOT2_OFFSET_BITS, opt in (STEAL_SECOND, STEAL_BOTH))]
    voice = []
    for off, stolen in slots:
        if stolen:
            chans.append(("FACCH1", off))
        else:
            voice.append(off)
    return chans, voice


# ---------------------------------------------------------------- layer 3 (§6.4)
def cipher_name(cipher_type: int) -> str:
    return CIPHER_NAMES.get(cipher_type & 0x3, "unknown")


def call_type_name(call_type: int) -> str:
    return CALL_TYPE_NAMES.get(call_type & 0x7, "reserved")


def parse_vcall(message_bits) -> dict:
    """VCALL (Figure 6.4-1) -> dict. Needs at least 8 octets.

    Octet 0 = F1|F2|Message Type(6); 1 = CC Option; 2 = Call Type(3)|Voice Call
    Option(5); 3-4 = Source Unit ID; 5-6 = Destination Group-or-Unit ID;
    7 = Cipher Type(2)|Key ID(6).

    The figure is followed, not section 6.4.1.1's body text, which puts Call Type
    at "Octet 6" — inside the destination ID. See the module docstring."""
    o = _bits_to_octets(message_bits)
    if len(o) < 8:
        return None
    call_type = (o[2] >> 5) & 0x7
    opt = o[2] & 0x1F
    txmode = opt & 0x7
    cipher = (o[7] >> 6) & 0x3
    dst = (o[5] << 8) | o[6]
    return {
        "call_type": call_type, "call_type_name": call_type_name(call_type),
        "duplex": bool(opt & 0x10),
        "transmission_mode": txmode,
        "transmission_mode_name": TXMODE_NAMES.get(txmode, "reserved"),
        "efr": txmode == TXMODE_EFR,
        "source_id": (o[3] << 8) | o[4],
        "destination_id": dst,
        "group_id": dst if call_type in GROUP_CALL_TYPES else None,
        "target_unit_id": dst if call_type in UNIT_CALL_TYPES else None,
        "cipher_type": cipher, "cipher_name": cipher_name(cipher),
        "key_id": o[7] & 0x3F,
        "encrypted": cipher != CIPHER_CLEAR,
    }


def parse_message(message_bits, traffic: bool = True) -> dict:
    """Any layer-3 message -> dict(message_type, name, vcall, raw).

    Only VCALL is field-parsed. Everything else is NAMED (on a traffic channel,
    where the Table 6.4-1 reading is unambiguous) and handed up as hex for an
    analyst — the same choice dmr.py makes for CSBK opcodes. Guessing at a
    layout we have not implemented would put invented IDs in a client report."""
    bits = list(int(b) & 1 for b in message_bits)
    if len(bits) < 8:
        return None
    mtype = _bits_to_int(bits[2:8])
    out = {"message_type": mtype, "flag1": bits[0], "flag2": bits[1],
           "message_name": (MSG_NAMES.get(mtype, "unknown (0x%02X)" % mtype)
                            if traffic else "control-channel type 0x%02X" % mtype),
           "vcall": None, "iv": None, "raw": _bits_hex(bits)}
    if traffic and mtype in VCALL_MESSAGES:
        out["vcall"] = parse_vcall(bits)
    elif traffic and mtype == MSG_VCALL_IV and len(bits) >= 72:
        # Figure 6.4-2 / §6.5.41: octets 1-8 are the 64-bit Initialization
        # Vector for DES or AES. Its PRESENCE is itself an encryption finding —
        # §6.5.41 forbids an all-zero value, so a real IV means a real key is
        # loaded, not merely that a Cipher Type field was set.
        out["iv"] = _bits_hex(bits[8:72])
    return out


# ---------------------------------------------------------------- frame locating
def find_fsw(dibits, max_bit_errs: int = FSW_MAX_BIT_ERRS):
    """Every dibit index whose next 10 symbols are within `max_bit_errs` BITS of
    the 20-bit frame sync. Vectorised: 10 numpy passes, not a Python slide."""
    d = np.asarray(dibits, dtype=np.uint8)
    n = d.size
    if n < FSW_DIBITS:
        return []
    span = n - FSW_DIBITS + 1
    err = np.zeros(span, dtype=np.int16)
    for k in range(FSW_DIBITS):
        err += _DIBIT_ERR[d[k:k + span], FSW_DIBIT_PATTERN[k]]
    return [int(i) for i in np.nonzero(err <= max_bit_errs)[0]]


def _slice_voice(frame_bits, offsets, efr: bool):
    """The 144-bit slots that were NOT stolen -> individual VCH frames.

    EHR packs two 72-bit AMBE+2 frames into a slot, EFR one 144-bit frame. This
    is the HANDOVER POINT for survey/ambe.py — we return FEC-protected vocoder
    bits and deliberately go no further."""
    width = VCH_BITS_EFR if efr else VCH_BITS_EHR
    frames = []
    for off in offsets:
        for k in range(SLOT_BITS // width):
            start = off + k * width
            chunk = frame_bits[start:start + width]
            if len(chunk) == width:
                frames.append(np.asarray(chunk, dtype=np.uint8))
    return frames


def decode_frame(dibits, pos: int, efr: bool = False) -> dict:
    """One frame at dibit index `pos`. Returns a frame dict, or None if the LICH
    did not validate (which is the second of the three honesty gates).

    Descrambling happens here, per frame, because the scrambler is re-seeded
    every frame — so a lost frame never desynchronises the next one."""
    d = np.asarray(dibits, dtype=np.uint8)[pos:pos + FRAME_DIBITS]
    if d.size < FSW_DIBITS + LICH_DIBITS:
        return None
    d = scramble_frame_dibits(d)
    lich = decode_lich(d[FSW_DIBITS:FSW_DIBITS + LICH_DIBITS])
    if lich is None:
        return None
    bits = list(dibits_to_bits(d))
    frame = {"offset_dibit": int(pos), "lich": lich,
             "truncated": d.size < FRAME_DIBITS,
             "channels": {}, "ran": None, "structure": None,
             "crc_ok": 0, "crc_failed": 0,
             "voice_frame_count": 0, "voice_bits": [],
             "voice_mode": None, "messages": []}
    chans, voice_offsets = frame_layout(lich)
    traffic = lich["rf_type"] in TRAFFIC_RF
    for name, off in chans:
        res = decode_channel(name, bits[off:off + CHANNELS[name]["bits"]])
        frame["channels"][name] = res
        if res["ok"]:
            frame["crc_ok"] += 1
            if res["ran"] is not None and frame["ran"] is None:
                frame["ran"] = res["ran"]
                frame["structure"] = res["structure"]
            # A SACCH part is 18 bits — a quarter of a message — so it is
            # reassembled by the caller, not parsed here.
            if name != "SACCH":
                msg = parse_message(res["message"], traffic=traffic)
                if msg:
                    msg["channel"] = name
                    frame["messages"].append(msg)
        else:
            frame["crc_failed"] += 1
    if voice_offsets:
        frame["voice_bits"] = _slice_voice(bits, voice_offsets, efr)
        frame["voice_frame_count"] = len(frame["voice_bits"])
        frame["voice_mode"] = "EFR" if efr else "EHR (assumed)"
    return frame


def _scan_frames(dibits, locks, efr: bool, max_frames: int):
    """Walk the FSW locks, decoding each frame and stepping past what it used."""
    frames, last_end = [], -1
    for pos in locks:
        if pos <= last_end:
            continue                          # inside a frame we already consumed
        f = decode_frame(dibits, pos, efr=efr)
        if f is None:
            continue                          # FSW locked but the LICH is not NXDN
        frames.append(f)
        last_end = pos + FRAME_DIBITS - 1
        if len(frames) >= max_frames:
            break
    return frames


# ---------------------------------------------------------- superframe assembly
def assemble_sacch(frames) -> list:
    """SACCH superframe reassembly: four 18-bit parts -> one 72-bit message.

    Table 6.3-2 numbers the parts BACKWARDS (structure 0b11 is the head, 0b00 the
    tail), and a sweep routinely starts mid-superframe, so a set is emitted only
    when all four parts were captured with the SAME RAN. Partial sets are
    dropped rather than zero-padded — a half-message would decode to a plausible
    but wrong unit ID."""
    out, buf, buf_ran = [], {}, None
    for f in frames:
        # A NON-superframe SACCH carries an 18-bit message in its own right and
        # its structure field is always 0b00; feeding it to the reassembler would
        # look like a 4/4 part and corrupt whatever superframe was in flight.
        if f["lich"]["fn_type"] == USC_SACCH_SINGLE:
            continue
        res = f["channels"].get("SACCH")
        if not res or not res["ok"] or res["message"] is None:
            continue
        part = SACCH_STRUCTURE_PART.get(res["structure"])
        if part is None:
            continue
        if part == 0 or res["ran"] != buf_ran:
            buf, buf_ran = {}, res["ran"]
        buf[part] = (res["message"], f)
        if len(buf) == SACCH_PARTS:
            bits = []
            for k in range(SACCH_PARTS):
                bits.extend(buf[k][0])
            msg = parse_message(bits, traffic=True)
            if msg:
                msg["channel"] = "SACCH"
                msg["ran"] = buf_ran
                msg["frames"] = [buf[k][1]["offset_dibit"] for k in range(SACCH_PARTS)]
                out.append(msg)
            buf, buf_ran = {}, None
    return out


def frame_grid_phase(frames):
    """(kept_frames, phase) — keep only the frames that sit on ONE frame grid.

    A real NXDN transmission emits 384-bit frames back to back, so every frame in
    it has the SAME offset modulo 192 symbols; a fade that costs a frame does not
    change that. Two independent false locks, on the other hand, land at
    unrelated offsets and essentially never share a phase. So grouping by
    `offset % FRAME_DIBITS` and keeping the largest group is a structural test
    that no FEC can substitute for — and it is what stops a single lucky
    FSW+LICH+CRC coincidence in a long capture from becoming a call record.

    A one-symbol tolerance is allowed either side of the phase. That is not
    slack for its own sake: a long capture can lose or gain a symbol in the
    timing loop, and without the tolerance the frames after a slip would be
    discarded as if they were coincidences. It costs a factor of 3 in the
    coincidence probability (3/192 rather than 1/192 of phase space) and buys
    tolerance of exactly the impairment a real receiver has.

    When nothing shares a phase the largest group is a SINGLE frame, and that is
    what comes back — the caller's gate is what then refuses to believe one
    frame on one CRC alone."""
    if not frames:
        return [], None
    best = None
    for f0 in frames:
        p = f0["offset_dibit"] % FRAME_DIBITS
        grp = [f for f in frames
               if ((f["offset_dibit"] - p + 1) % FRAME_DIBITS) <= 2]
        key = (len(grp), sum(f["crc_ok"] for f in grp))
        if best is None or key > best[0]:
            best = (key, p, grp)
    return best[2], best[1]


def _wants_efr(frames) -> bool:
    """True if any VCALL in the capture declared the 9600 bit/s EFR vocoder, in
    which case each 144-bit slot is ONE voice frame rather than two."""
    for m in assemble_sacch(frames):
        if m.get("vcall") and m["vcall"]["efr"]:
            return True
    for f in frames:
        for m in f["messages"]:
            if m.get("vcall") and m["vcall"]["efr"]:
                return True
    return False


def _single_sacch_present(frames) -> bool:
    """True if any frame carried a non-superframe SACCH. Its 18-bit message is
    too short to be a VCALL, so it contributes only its RAN — worth saying."""
    return any(f["lich"]["fn_type"] == USC_SACCH_SINGLE
               and f["channels"].get("SACCH", {}).get("ok")
               for f in frames)


# ---------------------------------------------------------------- call assembly
def _blank_call(ran, rate) -> dict:
    return {"protocol": "NXDN", "ran": ran, "rate": rate,
            "rate_name": RATE_NAMES.get(rate, "unknown"),
            "call_type": None, "call_type_name": None,
            "source_id": None, "destination_id": None,
            "group_id": None, "target_unit_id": None,
            "cipher_type": None, "cipher_name": None, "key_id": None,
            "encrypted": None, "transmission_mode_name": None,
            "voice_mode": None, "crc_ok": 0, "fields_verified": False,
            "init_vector": None,
            "channels": [], "frames": 0, "voice_frames": 0, "released": False}


def _merge_vcall(call: dict, vc: dict, channel: str):
    for key in ("call_type", "call_type_name", "source_id", "destination_id",
                "group_id", "target_unit_id", "transmission_mode_name"):
        if vc.get(key) is not None:
            call[key] = vc[key]
    # Cipher Type is the field finding; it is authoritative wherever it appears.
    call["cipher_type"] = vc["cipher_type"]
    call["cipher_name"] = vc["cipher_name"]
    call["key_id"] = vc["key_id"]
    call["encrypted"] = vc["encrypted"]
    if channel not in call["channels"]:
        call["channels"].append(channel)


def _describe(call: dict) -> str:
    """The one line an operator reads in the field. Encrypted is a RESULT.

    A record with NO verified channel FEC says so in its FIRST clause. That case
    is real — the framing gate (sync + LICH + frame grid) can confirm an NXDN
    transmission while every CRC fails to noise — but it carries no fields at
    all, and it must never read like a call that merely happened to be missing
    its talkgroup."""
    if not call["fields_verified"]:
        return ("NXDN %s framing present (%d frame(s) on the frame grid) but NO "
                "channel FEC verified — a digital LMR transmission is live here; "
                "RAN, unit IDs and encryption state are NOT recovered. Capture "
                "longer or closer." % (call["rate_name"], call["frames"]))
    parts = ["NXDN %s, RAN %s" % (call["rate_name"],
                                  call["ran"] if call["ran"] is not None else "unknown")]
    if call["call_type_name"]:
        parts.append(call["call_type_name"])
    if call["source_id"] is not None:
        parts.append("from unit %d" % call["source_id"])
    if call["group_id"] is not None:
        parts.append("to group %d" % call["group_id"])
    elif call["target_unit_id"] is not None:
        parts.append("to unit %d" % call["target_unit_id"])
    elif call["destination_id"] is not None:
        parts.append("to ID %d" % call["destination_id"])
    if call["encrypted"] is True and call["cipher_type"] is not None:
        parts.append("Cipher Type %d (%s), key ID 0x%02X — ENCRYPTED, content not "
                     "recoverable; identify and locate"
                     % (call["cipher_type"], call["cipher_name"], call["key_id"] or 0))
        if call["init_vector"]:
            parts.append("initialisation vector %s seen (a key is loaded)"
                         % call["init_vector"])
    elif call["encrypted"] is True:
        parts.append("ENCRYPTED — a DES/AES initialisation vector (%s) was sent "
                     "but no VCALL named the cipher; content not recoverable"
                     % call["init_vector"])
    elif call["encrypted"] is False:
        parts.append("Cipher Type 0 (clear) — unencrypted, voice frames available "
                     "to the vocoder")
    else:
        parts.append("encryption state unknown (no VCALL in this capture)")
    if call["voice_frames"]:
        parts.append("%d AMBE+2 voice frame(s) located" % call["voice_frames"])
    return ", ".join(parts)


def assemble_calls(frames, rate) -> list:
    """Frames + reassembled SACCH messages -> call records.

    A capture that starts mid-call (no VCALL yet) still yields a record carrying
    the RAN and the voice-frame count — that is the normal field case, and it is
    labelled "encryption state unknown" rather than being allowed to read as
    clear."""
    calls, cur = [], None
    sacch_msgs = {}
    for m in assemble_sacch(frames):
        # key the reassembled message to the frame that completed it
        sacch_msgs.setdefault(m["frames"][-1], []).append(m)

    def close():
        nonlocal cur
        if cur is not None and (cur["frames"] or cur["voice_frames"]):
            cur["summary"] = _describe(cur)
            calls.append(cur)
        cur = None

    for f in frames:
        ran = f["ran"]
        if cur is None or (ran is not None and cur["ran"] is not None
                           and ran != cur["ran"]):
            close()
            cur = _blank_call(ran, rate)
        if cur["ran"] is None and ran is not None:
            cur["ran"] = ran
        cur["frames"] += 1
        cur["crc_ok"] += f["crc_ok"]
        cur["fields_verified"] = cur["crc_ok"] > 0
        cur["voice_frames"] += f["voice_frame_count"]
        if f["voice_mode"]:
            # "EHR (assumed)" until a VCALL's Transmission Mode says otherwise —
            # the word "assumed" stays in the string on purpose.
            cur["voice_mode"] = f["voice_mode"]
        released = False
        for m in list(f["messages"]) + sacch_msgs.get(f["offset_dibit"], []):
            if m.get("vcall"):
                _merge_vcall(cur, m["vcall"], m["channel"])
            if m.get("iv"):
                # A live DES/AES initialisation vector. It cannot decrypt
                # anything — it is evidence that a key is loaded.
                cur["init_vector"] = m["iv"]
                if cur["encrypted"] is None:
                    cur["encrypted"] = True
            if m["message_type"] in (MSG_TX_REL, MSG_TX_REL_EX):
                released = True
        if released:
            # TX_REL ends the transmission. Closing here keeps a SECOND call on
            # the same RAN from overwriting the first one's unit IDs.
            cur["released"] = True
            close()
    close()
    return calls


# ---------------------------------------------------------------- top level
def _empty_result(note: str, rate=None) -> dict:
    return {"protocol": "NXDN", "messages": [], "calls": [], "frames": [],
            "note": note,
            "stats": {"sync": False, "sync_count": 0, "frames": 0, "lich_ok": 0,
                      "grid_phase": None, "crc_ok": 0, "crc_failed": 0,
                      "rans": [], "encrypted": None, "inverted": False,
                      "voice_frames": 0, "symbol_rate": rate}}


def decode_nxdn_dibits(dibits, max_bit_errs: int = FSW_MAX_BIT_ERRS,
                       symbol_rate: float = SYMBOL_RATE_NXDN96,
                       max_frames: int = MAX_FRAMES, try_invert: bool = True,
                       efr: bool = False) -> dict:
    """PRIMARY ENTRY POINT: a recovered dibit stream -> NXDN call metadata.

    `dibits` is a numpy array or sequence of 0..3 values at 2400 Bd (NXDN48) or
    4800 Bd (NXDN96) — exactly what survey/c4fm.py produces. `symbol_rate` is
    recorded in the result and used for labelling; the frame layer itself is
    rate-independent. Returns

      {"protocol":"NXDN", "frames":[...], "calls":[...], "messages":[...],
       "note", "stats"}

    where `messages` mirrors `calls` for decode_router compatibility and every
    call carries a plain-English `summary`.

    HONESTY GATE, four independent stages, all of which must clear:
      1. the 20-bit FSW must lock within `max_bit_errs` bits;
      2. the LICH must be on the outer rails and its parity must check;
      3. the surviving frames must sit on ONE 192-symbol frame grid
         (frame_grid_phase) — coincidences do not share a phase;
      4. and then either MIN_FRAMES_WITHOUT_CRC frames on that grid, or two
         verified channel CRCs, or one CRC plus a second grid-mate.
    Stage 1 alone is weak (a 20-bit sync is ~2e-4 per position) and that is
    deliberate: the strength is in 2-4, which are structural rather than
    threshold-based. Noise, analog FM, a DMR burst or a P25 call return an EMPTY
    result with a clear note — never a fabricated RAN or unit ID."""
    d = np.asarray(dibits, dtype=np.uint8)
    if d.size == 0:
        return _empty_result("empty dibit stream", symbol_rate)
    if int(d.max()) > 3:
        raise ValueError("dibit stream must contain only values 0..3")

    # Try both C4FM polarities: which rail is "positive" depends on the LO offset
    # of the capture, and a polarity-flipped stream matches nothing. Every frame
    # still has to clear all four gates, so this costs no honesty.
    frames, inverted, n_locks = [], False, 0
    for inv in ((False, True) if try_invert else (False,)):
        stream = invert_dibits(d) if inv else d
        locks = find_fsw(stream, max_bit_errs)
        n_locks = max(n_locks, len(locks))
        if not locks:
            continue
        cand = _scan_frames(stream, locks, efr, max_frames)
        crc_new = sum(f["crc_ok"] for f in cand)
        crc_old = sum(f["crc_ok"] for f in frames)
        if (crc_new, len(cand)) > (crc_old, len(frames)):
            frames, inverted = cand, inv
        if frames and not inv and crc_old + crc_new:
            break                              # normal polarity already validated
    if not frames:
        if not n_locks:
            return _empty_result("no NXDN frame sync found", symbol_rate)
        return _empty_result("NXDN frame sync candidates found but no LICH "
                             "validated — not NXDN", symbol_rate)

    # EHR vs EFR is not in the LICH — it is only in the VCALL's Transmission
    # Mode, which we cannot read until the frames are decoded. So slice the voice
    # slots as EHR first, and if a VCALL then says EFR, redo the scan with the
    # right frame width rather than reporting twice as many half-frames.
    if not efr and _wants_efr(frames):
        stream = invert_dibits(d) if inverted else d
        frames = _scan_frames(stream, find_fsw(stream, max_bit_errs), True,
                              max_frames) or frames
        efr = True

    n_locked = len(frames)
    frames, phase = frame_grid_phase(frames)
    crc_ok = sum(f["crc_ok"] for f in frames)
    crc_failed = sum(f["crc_failed"] for f in frames)
    # Gates 3 and 4, and they are what actually hold the line on a LONG capture:
    # a 20-bit FSW plus a 7-bit LICH plus a 6-bit CRC is only ~1e-8 per position,
    # but a million-symbol sweep has a million positions. So believe a frame set
    # only when it looks like a TRANSMISSION rather than like a coincidence —
    # several frames on one grid, or two independent CRCs inside one frame.
    # MEASURED on this implementation: 6 million random dibits produce ZERO
    # reported frames (tests/test_nxdn.py, TestNegatives).
    if not (len(frames) >= MIN_FRAMES_WITHOUT_CRC
            or crc_ok >= 2
            or (crc_ok >= 1 and len(frames) >= 2)):
        return _empty_result(
            "%d NXDN frame(s) locked sync + LICH (%d channel CRC(s) verified) but "
            "they do not form a frame grid — not reported" % (n_locked, crc_ok),
            symbol_rate)

    calls = assemble_calls(frames, symbol_rate)
    rans = sorted({f["ran"] for f in frames if f["ran"] is not None})
    enc = None
    for c in calls:                            # any encrypted call dominates
        if c["encrypted"] is True:
            enc = True
            break
        if c["encrypted"] is False:
            enc = False
    stats = {"sync": True, "sync_count": n_locks, "frames": len(frames),
             "lich_ok": n_locked, "grid_phase": phase,
             "crc_ok": crc_ok, "crc_failed": crc_failed,
             "rans": rans, "encrypted": enc, "inverted": inverted,
             "voice_frames": sum(f["voice_frame_count"] for f in frames),
             "symbol_rate": symbol_rate,
             "rf_types": sorted({f["lich"]["rf_name"] for f in frames})}
    if calls:
        note = "; ".join(c["summary"] for c in calls[:4])
    elif crc_ok:
        note = ("NXDN sync + LICH + channel CRC verified (RAN %s) but no VCALL "
                "in this capture — system identified, call detail not recovered"
                % (", ".join(str(r) for r in rans) or "unknown"))
    else:
        note = ("NXDN sync + LICH locked on %d frames but no channel CRC passed "
                "— framing identified, no field believed" % len(frames))
    if _single_sacch_present(frames):
        note += " (non-superframe SACCH present: RAN only, no 72-bit message)"
    return {"protocol": "NXDN", "frames": frames, "calls": calls,
            "messages": calls, "note": note, "stats": stats}


def _as_dibit_array(res):
    """(array, None) if `res` really is a 1-D stream of dibits, else (None, why).

    The adapter crosses a module boundary we do not own, so it VALIDATES instead
    of coercing: anything that is not a one-dimensional array-like of integers in
    0..3 comes back as an honest reason string. A neighbouring module changing
    shape must degrade this decoder to an empty result with a note, never to a
    traceback out of a sweep."""
    try:
        arr = np.asarray(res)
    except Exception:                                              # noqa: BLE001
        return None, "a value that is not array-like"
    if arr.dtype == object or not np.issubdtype(arr.dtype, np.number):
        return None, "a non-numeric array (dtype %s)" % (arr.dtype,)
    if arr.ndim != 1:
        return None, "a %d-D array, expected a 1-D dibit stream" % arr.ndim
    if arr.size == 0:
        return None, "an empty dibit stream"
    if not np.all(np.isfinite(arr)):
        return None, "a dibit stream containing non-finite values"
    if np.any(arr != np.floor(arr)):
        return None, "non-integer symbol values"
    lo, hi = int(arr.min()), int(arr.max())
    if lo < 0 or hi > 3:
        return None, "symbol values outside the dibit range 0..3 (%d..%d)" % (lo, hi)
    return arr.astype(np.uint8), None


def _c4fm_dibits(iq, fs: float, symbol_rate: float):
    """Lazy bridge to survey/c4fm.py (owned separately), mirroring the
    decode_router._decoders() lazy-import pattern so this module imports and its
    whole frame layer tests cleanly whether or not c4fm is present.

    Adapter, not a contract: c4fm.demod_c4fm(iq, fs, symbol_rate=...) returns a
    result dict whose 'dibits' is the symbol stream and 'quality' the confidence.
    A bare array or an (array, quality) tuple is also accepted, and a decoder
    that will not take a symbol_rate is retried without it so a signature change
    on the other side degrades to an honest note instead of a traceback. Returns
    (None, None, reason) if the module or a usable entry point is missing."""
    try:
        from . import c4fm
    except ImportError:
        return None, None, "survey/c4fm.py not available — cannot demodulate IQ"
    for name in ("demod_c4fm", "dibits_from_iq", "c4fm_demod", "decode_c4fm",
                 "symbols_from_iq", "recover_dibits"):
        fn = getattr(c4fm, name, None)
        if not callable(fn):
            continue
        try:
            res = fn(iq, fs, symbol_rate=symbol_rate)
        except ValueError:
            raise                              # too-short capture propagates
        except TypeError:
            # NXDN48's 2400 Bd is NOT the C4FM default, so a front end that
            # cannot be told the symbol rate can only serve NXDN96 — say so
            # rather than silently demodulating at the wrong rate.
            if symbol_rate != SYMBOL_RATE_NXDN96:
                return None, None, ("survey/c4fm.py does not accept a symbol rate, "
                                    "so %g Bd cannot be demodulated" % symbol_rate)
            try:
                res = fn(iq, fs)
            except Exception as exc:                               # noqa: BLE001
                return None, None, "c4fm.%s() failed: %s" % (name, exc)
        except Exception as exc:                                   # noqa: BLE001
            return None, None, "c4fm.%s() failed: %s" % (name, exc)
        quality = None
        if isinstance(res, dict):
            quality = res.get("quality")
            res = res.get("dibits")
        elif isinstance(res, (tuple, list)) and res and not np.isscalar(res[0]):
            quality = res[1] if len(res) > 1 else None
            res = res[0]
        if res is None:
            continue
        arr, why = _as_dibit_array(res)
        if arr is None:
            return None, None, "c4fm.%s() returned %s" % (name, why)
        return arr, quality, None
    return None, None, "survey/c4fm.py exposes no recognised dibit entry point"


def decode_nxdn(iq, fs: float, symbol_rate=None, **kwargs) -> dict:
    """IQ entry point, matching decode_p25 / decode_dmr.

    With `symbol_rate` unset BOTH NXDN rates are tried — 4800 Bd (NXDN96, the
    12.5 kHz channel) first, then 2400 Bd (NXDN48, 6.25 kHz) — and whichever
    produces more CRC-verified frames wins. That costs a second C4FM demod on
    signals that are not NXDN96, which is the right trade: guessing the channel
    width from `iq_features` is exactly the kind of inference that made
    looks_like_p25 reject real P25 (see that gate's comment block).

    If c4fm is missing this returns an EMPTY result with the reason in `note` —
    it does not raise and it does not pretend. Only a too-short capture raises
    ValueError, mirroring demod.demodulate()."""
    iq = np.asarray(iq, dtype=np.complex64)
    if iq.size < 16:
        raise ValueError("capture too short to decode")
    rates = SYMBOL_RATES if symbol_rate is None else (float(symbol_rate),)
    best, best_err = None, None
    for rate in rates:
        dibits, quality, err = _c4fm_dibits(iq, fs, rate)
        if dibits is None:
            best_err = best_err or err
            continue
        res = decode_nxdn_dibits(dibits, symbol_rate=rate, **kwargs)
        res["stats"]["c4fm_quality"] = quality
        if best is None:
            best = res
            continue
        score = (res["stats"]["crc_ok"], res["stats"]["frames"])
        if score > (best["stats"]["crc_ok"], best["stats"]["frames"]):
            best = res
    if best is None:
        return _empty_result(best_err or "C4FM symbol recovery unavailable")
    return best


def looks_like_nxdn(feats: dict) -> bool:
    """Cheap pre-gate from identify.iq_features(): NXDN is 4-level FSK at 2400
    symbols/s in a 6.25 kHz channel or 4800 symbols/s in a 12.5 kHz channel, and
    it is continuous while someone is keyed up.

    Feature gates only pick which decoder to TRY — the FSW + LICH + CRC gates
    inside decode_nxdn_dibits decide what is reported.

    MEASURED, 2026-08-03 (and learned the hard way on P25 first): iq_features
    CANNOT see RRC-shaped 4-level FSK as FSK. The waveform is continuous-phase
    and pulse-shaped, so the instantaneous-frequency histogram is smeared rather
    than four discrete rails, and the extractor returns
    modulation_class='analog_voice', fsk_states=0, fsk_shift_khz=0.0 on a
    known-good synthetic C4FM call at every sample rate tried. Requiring those
    would mean NXDN could NEVER be tried, on a signal decode_nxdn handles
    perfectly. So FSK evidence is CONFIRMING here, never required.

    NXDN48 makes this worse, not better: a 6.25 kHz channel is NARROWER than an
    analog FM voice channel, so the bandwidth test has to reach down to ~1.5 kHz
    and cannot exclude much. That is the right trade — the three-stage gate
    inside the decoder is far stronger than any feature test, so a loose
    pre-gate cannot manufacture a false call."""
    if not feats:
        return False
    # Explicit False rejects (iq_features sets it, and an empty channel must not
    # reach the decoder); a MISSING key is "not stated", not "no signal".
    if feats.get("signal_present") is False:
        return False
    bw = feats.get("occupied_bw_khz", 0.0) or 0.0
    # 6.25 kHz through 12.5 kHz channels, with room for a mis-tuned capture and
    # for the wider measurement a noisy environment gives. An unmodulated carrier
    # measures far below this and a wideband/OFDM signal far above.
    if not (1.5 <= bw <= 25.0):
        return False
    rate = feats.get("symbol_rate_kbd", 0.0) or 0.0
    # 0.0 means "could not measure", which is the NORMAL reading for a shaped
    # 4-FSK — treat it as no information rather than as a mismatch.
    if rate and not any(abs(rate - r / 1000.0) <= 0.35 * (r / 1000.0)
                        for r in SYMBOL_RATES):
        return False
    return True


# ---------------------------------------------------------------- synth / encoder
def make_vcall(source_id: int, destination_id: int, call_type: int = 0b001,
               cipher_type: int = CIPHER_CLEAR, key_id: int = 0,
               transmission_mode: int = 0b010, duplex: bool = False,
               cc_option: int = 0, message_type: int = MSG_VCALL) -> list:
    """Build the 64-bit VCALL message of Figure 6.4-1 (inverse of parse_vcall)."""
    o = [0] * 8
    o[0] = message_type & 0x3F
    o[1] = cc_option & 0xFF
    o[2] = ((call_type & 0x7) << 5) | (0x10 if duplex else 0) | (transmission_mode & 0x7)
    o[3], o[4] = (source_id >> 8) & 0xFF, source_id & 0xFF
    o[5], o[6] = (destination_id >> 8) & 0xFF, destination_id & 0xFF
    o[7] = ((cipher_type & 0x3) << 6) | (key_id & 0x3F)
    bits = []
    for v in o:
        bits += _int_to_bits(v, 8)
    return bits


def make_message(message_type: int, payload_octets=()) -> list:
    """A minimal layer-3 message: the type byte followed by `payload_octets`."""
    bits = _int_to_bits(message_type & 0x3F, 8)
    for v in payload_octets:
        bits += _int_to_bits(int(v) & 0xFF, 8)
    return bits


def pack_sr(ran: int, structure: int = 0) -> int:
    """SR information octet: Structure(2) | Radio Access Number(6) — Figure 6.3-1."""
    return ((structure & 0x3) << RAN_BITS) | (ran & 0x3F)


def encode_frame_dibits(rf_type: int, fn_type: int, option: int, direction: int,
                        *, ran: int = 0, sacch_part=None, sacch_structure: int = 0,
                        facch1=None, payload=None, voice_fill: int = 0) -> np.ndarray:
    """Synthesize ONE 384-bit NXDN frame as 192 dibits (test / self-check path).

    `payload` is the layer-3 message for a UDCH/FACCH2 or CAC frame; `sacch_part`
    is the 18-bit SACCH quarter and `facch1` the 80-bit FACCH1 message on a
    traffic frame. Which fields exist is decided by frame_layout() from the same
    LICH the decoder will read, so the encoder cannot lay out a frame the decoder
    would not.

    Shares the LICH coder, the CRC, the convolutional encoder, the puncturing
    matrices, the interleaver and the scrambler with the decoder, so a wrong
    polynomial, depth or seed fails loudly in tests/test_nxdn.py rather than
    round-tripping through matched bugs at the top level only."""
    lich = {"rf_type": rf_type, "fn_type": fn_type, "option": option,
            "direction": direction}
    bits = [0] * FRAME_BITS
    bits[0:FSW_BITS] = _int_to_bits(FSW, FSW_BITS)

    def place(off, seq):
        bits[off:off + len(seq)] = [int(b) & 1 for b in seq]

    place(LICH_OFFSET_BITS,
          dibits_to_bits(encode_lich_dibits(pack_lich(rf_type, fn_type, option,
                                                      direction))))
    chans, voice_offsets = frame_layout(lich)
    sr = pack_sr(ran, sacch_structure)
    for name, off in chans:
        if name == "SACCH":
            place(off, encode_channel(name, sacch_part or [], sr=sr))
        elif name == "FACCH1":
            place(off, encode_channel(name, facch1 or []))
        else:
            place(off, encode_channel(name, payload or [], sr=sr))
    for off in voice_offsets:
        # Stand-in AMBE+2 frames: this module never claims to encode voice, it
        # only proves the bits land where the decoder looks for them.
        rng = np.random.default_rng(voice_fill * 1009 + off)
        place(off, rng.integers(0, 2, SLOT_BITS).tolist())
    return scramble_frame_dibits(bits_to_dibits(bits))


def encode_call(ran: int, source_id: int, destination_id: int, *,
                call_type: int = 0b001, cipher_type: int = CIPHER_CLEAR,
                key_id: int = 0, transmission_mode: int = 0b010,
                rf_type: int = RF_RDCH, direction: int = 1,
                superframes: int = 1, facch1_first: bool = True,
                preamble: bool = True, tail_release: bool = True) -> np.ndarray:
    """Synthesize a complete NXDN voice call as dibits.

    Layout mirrors a real transmission: an optional preamble, an opening frame
    whose first slot is stolen for a FACCH1 VCALL (how a call announces itself
    on a conventional RDCH), then `superframes` x 4 frames carrying the same
    VCALL spread across a SACCH superframe with voice in both slots, then a
    TX_REL. This is what tests/test_nxdn.py round-trips: known RAN / unit IDs /
    call type / cipher type in, exact recovery out — including the encrypted
    case, which must come back as an encrypted RESULT and not a decode failure."""
    vcall = make_vcall(source_id, destination_id, call_type=call_type,
                       cipher_type=cipher_type, key_id=key_id,
                       transmission_mode=transmission_mode)
    efr = (transmission_mode & 0x7) == TXMODE_EFR
    parts = []
    if preamble:
        parts.append(bits_to_dibits(_int_to_bits(PREAMBLE, PREAMBLE_BITS)))
    if facch1_first:
        parts.append(encode_frame_dibits(
            rf_type, USC_SACCH_SINGLE, STEAL_FIRST, direction, ran=ran,
            sacch_part=[0] * SACCH_PART_BITS, sacch_structure=0b00,
            facch1=vcall))
    # The 72-bit VCALL fills a SACCH superframe exactly (9 octets); split it into
    # the four 18-bit parts the spec's Figure 6.3-3 describes.
    sf_bits = list(vcall) + [0] * (SACCH_MESSAGE_BITS - len(vcall))
    quarters = [sf_bits[k * SACCH_PART_BITS:(k + 1) * SACCH_PART_BITS]
                for k in range(SACCH_PARTS)]
    structures = [s for s, p in sorted(SACCH_STRUCTURE_PART.items(),
                                       key=lambda kv: kv[1])]
    for n in range(max(1, superframes)):
        for k in range(SACCH_PARTS):
            parts.append(encode_frame_dibits(
                rf_type, USC_SACCH_SUPER, STEAL_NONE, direction, ran=ran,
                sacch_part=quarters[k], sacch_structure=structures[k],
                voice_fill=n * 16 + k + 1))
    if tail_release:
        parts.append(encode_frame_dibits(
            rf_type, USC_SACCH_SINGLE, STEAL_BOTH, direction, ran=ran,
            sacch_part=[0] * SACCH_PART_BITS, sacch_structure=0b00,
            facch1=make_message(MSG_TX_REL)))
    del efr                                    # the LICH does not carry it; the
    # VCALL's Transmission Mode does, and the decoder re-slices the voice frames
    # once it has read that (see decode_nxdn_dibits).
    return np.concatenate(parts).astype(np.uint8)
