"""P25 Phase 1 (APCO-25 FDMA / C4FM) frame + metadata decoder — demod #5.

P25 Phase 1 is the digital land-mobile standard used by public safety, federal
agencies, utilities, and — the reason it matters to a TSCM sweep — by private
security, corporate protection details, and surveillance teams operating on the
client's site. This module is the FRAME LAYER: it takes a recovered DIBIT stream
(2 bits/symbol, 4800 sym/s on air) and pulls out WHO is talking to WHOM and
WHETHER IT IS ENCRYPTED. It does NOT do audio — the IMBE/AMBE vocoder lives
behind survey/ambe.py; we stop at handing the voice-frame bits over.

What comes out (survey/p25.py is the honest half of the answer):
  * NAC   — 12-bit Network Access Code, the system's "colour code". Identifies
            the radio system independently of frequency.
  * DUID  — 4-bit Data Unit ID: HDU / LDU1 / LDU2 / TDU / TDULC / TSDU / PDU.
  * TGID  — talkgroup, and the SOURCE RADIO ID, from the Link Control word.
  * ALGID / KID / MI — the encryption state. ALGID 0x80 means CLEAR; anything
            else is an encrypted call and the algorithm is named.

ENCRYPTION IS A RESULT, NOT A FAILURE (this is the point of the module). A field
line reading "P25, NAC 0x293, talkgroup 1001, source 9114712, ALGID 0x84
(AES-256), ENCRYPTED" is a complete, defensible finding: the operator now knows a
digital radio system is live on site, which system it is, who is keyed up on it,
and that the traffic cannot be read. That is written to the ledger as a positive
identification. It is NEVER reported as a failed decode, as silence, or as "no
signal" — those are different facts and conflating them would misrepresent the
sweep.

Chain implemented here
----------------------
  dibits -> frame sync (48-bit 0x5575F5FF77FF, bit-error tolerant)
         -> NID: NAC(12) + DUID(4) under BCH(63,16,23) + parity
         -> per-DUID payload:
              HDU    : Golay(18,6,8) hexbits -> RS(36,20,17) -> MI/MFID/ALGID/KID/TGID
              LDU1   : 9 IMBE voice frames (de-interleaved per Table 5-1)
                       + Hamming(10,6,3) -> RS(24,12,13) -> LCW
              LDU2   : 9 IMBE voice frames (de-interleaved per Table 5-1)
                       + Hamming(10,6,3) -> RS(24,16,9)  -> ESW
              TDU    : terminator, no payload
              TDULC  : Golay(18,6,8) hexbits -> RS(24,12,13) -> LCW
              TSDU   : 1/2-rate trellis -> TSBK(96) + CRC-16 -> channel grants
              PDU    : recognised and reported, NOT parsed (data packets)
         -> call assembly (HDU/LDU1/LDU2/TDULC merged into one call record)

VALIDATION STATUS — read this before quoting a result in a report
-----------------------------------------------------------------
NOTHING IN THIS MODULE HAS BEEN VALIDATED AGAINST A REAL OFF-AIR P25 SIGNAL.
That has not changed and no test below changes it.

What HAS changed is the kind of evidence behind parts of it. Most of the
behavioural testing is still synthetic frames built by this module's own encoder
and round-tripped through the decoder (tests/test_p25.py) — including a full
synthetic chain (P25 frames -> c4fm.encode_c4fm() IQ -> c4fm symbol recovery ->
this frame layer, with noise, a carrier offset, status symbols and inverted
polarity). That is a much longer chain than a bare unit test, but it is still
OUR waveform decoded by OUR receiver: it proves the layers agree, not that they
agree with a Motorola or Harris radio. The frame GEOMETRY, the voice interleave,
the short block codes and the Reed-Solomon generators, however, are no longer in
that category: they are pinned directly to the published tables in
TIA-102.BAAA-A and now fail if this module drifts from the standard, whether or
not the encoder drifts with it.

Read the two lists below with that distinction in mind. Round-tripping through
our own encoder is worth NOTHING on its own — a matched pair of bugs round-trips
perfectly — so anything in the "anchored" list earns its place by agreeing with a
source OUTSIDE this module, and anything that cannot be checked that way is in
the "assumed" list no matter how many synthetic tests pass.

PROVENANCE OF THE ANCHORED VALUES (this is a closed-source commercial product).
Every table cited below was read from TIA-102.BAAA-A "Project 25 FDMA Common Air
Interface", the published specification this module implements. No GPL source
was consulted or copied: the interleave schedule and the generator matrices also
exist in op25, SDRTrunk and DSD, all GPL, and taking them from there would have
relicensed this product. What is reproduced here are the functional constants an
implementer needs — bit orderings and generator polynomials — not the standard's
text; the document itself is TIA copyright and is neither vendored nor
redistributed with the app. Contrast survey/imbe_tables.py, which ships mbelib's
ISC-licensed tables and therefore carries an attribution obligation discharged
in THIRD_PARTY_NOTICES.md. Nothing added here creates such an obligation.
Specifically:

  Independently anchored (correct by construction, not by self-consistency):
    - Frame sync word 0x5575F5FF77FF is the published TIA-102 value.
    - THE IMBE VOICE-FRAME INTERLEAVE is now implemented and is transcribed from
      TIA-102.BAAA-A section 5.3.1, Table 5-1 ("Interleaving Schedule for Voice
      Word"). It is not a round-trip claim: the same permutation is printed a
      second time, symbol by symbol, in that document's sections 10.3 and 10.4
      (the transmit-order annex) for all 18 voice frames of a superframe, and
      the two tables agree entry for entry. The table is also checked to be a
      true bijection over all 144 code-word bits, and to reproduce the design
      property the standard states in prose — "the minimum separation between
      any two bits of the same error correction code is 3 symbols" — which it
      does, exactly 3 symbols (5 bits) for every one of c_0..c_7. Measured
      consequence: any burst of 5 adjacent channel bits lands at most ONE error
      in each code word, and any burst of 17 lands at most 3 in any Golay word
      (its full correction capacity); un-de-interleaved, a burst of N puts all N
      errors into a single code word. Voice bits handed to the vocoder are in
      code-word order c_0..c_7, MSB-first, which is the input layout
      survey/ambe.py documents.
    - THE LDU FRAME GEOMETRY is likewise anchored, not invented: LDU_LAYOUT
      (VC1, VC2, LC 1-4, VC3, LC 5-8, VC4, ... VC8, LSD, VC9) and the
      status-symbol phase were derived mechanically from the section 10.3/10.4
      transmit-order annex and match TIA-102.BAAA-A Figure 8-3 / 8-4: 840 data
      symbols per LDU, a status symbol every 36th transmitted symbol at offset
      35, 24 sync symbols, 32 NID symbols.
    - The LC / ES hexbits are NOT bit-interleaved. Checked against the same
      annex: each (10,6,3) short Hamming word is transmitted as its 6 info bits
      MSB-first followed by its 4 parity bits, and consecutive words simply run
      in order. The only "interleaving" the LC gets is being split into six
      40-bit chunks spaced between the voice frames, which LDU_LAYOUT does.
    - BCH(63,16,23) generator is DERIVED here from the minimal polynomials of
      alpha^1..alpha^21 over GF(2^6) (x^6+x+1) — not copied from a table — and the
      64-entry codebook is checked to have minimum distance exactly 23.
    - THE SHORT BLOCK CODES ARE NOW THE STANDARD'S OWN MATRICES, not merely
      codes of the same parameters. The extended Golay [24,12,8] generator is
      TIA-102.BAAA-A Table 5-3 (its first parity row IS g(x) = 6165 octal), the
      (18,6,8) is derived from it by the section 5.7 rule and then checked
      against Table 5-3's separately-printed (18,6,8) column, and the (10,6,3)
      Hamming is Table 5-4. This matters more than it looks: the extended Golay
      code is unique up to equivalence, so the previous locally-built matrices
      had the correct weight distribution {0,8,12,16,24} and the correct minimum
      distances 8 and 3 and passed every self-consistency test — while mapping
      information onto DIFFERENT codewords than a real transmitter uses. Section
      5.8 is explicit that the (10,6,3) shortening is deliberately not the
      obvious one, "not just the left hand columns and top rows... to improve
      the likelihood of error detection". Weight distribution and minimum
      distance are still asserted, they are just no longer the only evidence.
    - RS(36,20,17) / RS(24,12,13) / RS(24,16,9) are proven to correct their full
      t = 8 / 6 / 4 symbol errors, AND all three generator polynomials now match
      TIA-102.BAAA-A section 5.9 coefficient for coefficient (the standard prints
      them outright, in octal). The field is the standard's a^6 + a + 1.
    - The LCW / ESW / HDU octet layouts are the published field orders.

  STILL ASSUMED — what to suspect first when this meets real traffic:
    - (CLOSED) The Reed-Solomon first-consecutive-root convention used to head
      this list as the largest open risk. Section 5.9 states it outright:
      g(x) = (x + a)(x + a^2)...(x + a^R), i.e. narrow-sense, FCR = 1 — which is
      what this module already used. Pinned by test vectors now, both as
      generator coefficients and as a root check. Kept here, struck through
      rather than deleted, so the risk register shows what was retired and why.
    - TDULC is parsed as RS(24,12,13) + Golay(18,6,8) with any trailing null
      padding ignored; the total TDULC frame length is not asserted anywhere.
    - The 1/2-rate trellis state table and the TSBK CRC-16 variant follow the
      widely-used open implementations, not a first-principles derivation.
    - NOT OURS, BUT IT LANDS HERE: the de-interleaved 144 bits we hand over are
      correct in ORDER, but what survey/ambe.py then does with the 88 recovered
      information bits still uses that module's own field convention (it reads
      b_0 as the first 8 bits of the 88). TIA-102.BABA section 7.1 does not
      work that way — it puts only the SIX most significant bits of b_0 into
      u_0(11..6) and scatters the rest by priority, so a real frame decoded
      through the current convention yields a wrong pitch even when every FEC
      check passes. Fixing that is survey/ambe.py's job, not this module's; see
      the hand-off note in the report accompanying this change. Nothing in p25.py
      depends on it — we stop at the bits.

Consequence: an off-air capture may lock the sync and produce a clean NAC/DUID
(those legs are anchored) while the LC/ES payload fails FEC. That is the expected
partial-success mode and it is reported as exactly that — NAC + DUID + "payload
FEC failed" — not as a decode with invented fields. Nothing is emitted that did
not pass its own check.

False positives are the failure mode that would actually damage a client report,
so they are measured rather than asserted: 3 million random dibits produce ZERO
frames, and an analog-FM-derived symbol stream, a 2-FSK paging stream, a constant
stream and a bare planted sync word all produce zero (tests/test_p25.py,
TestNegatives). Both gates are independent — the 48-bit sync and the BCH NID —
and a lock has to clear both.

Constraints: numpy only, no new dependencies. Receive-only, like the rest of the
app. survey/c4fm.py (IQ -> dibits, owned separately) is imported LAZILY inside
decode_p25() following the decode_router._decoders() pattern, so this module
imports and its whole frame layer tests cleanly whether or not c4fm exists yet.
"""

import numpy as np

# ---------------------------------------------------------------- P25 constants
# TIA-102.BAAA frame synchronisation word: 48 bits, MSB-first, sent as 24 dibits.
FRAME_SYNC = 0x5575F5FF77FF
FRAME_SYNC_BITS = 48
SYNC_DIBITS = FRAME_SYNC_BITS // 2          # 24
# A real C4FM eye is never clean. Tolerate a few BIT errors across the 48-bit
# sync. This is the cheapest gate and it is enormously selective: the chance a
# random 48-bit window lands within 4 bits of the sync is ~7.6e-10, so even a
# 100k-dibit noise capture false-locks with probability ~1e-4 — and such a lock
# still has to pass the BCH NID gate below.
SYNC_MAX_BIT_ERRS = 4

NID_BITS = 64                                # 63-bit BCH codeword + 1 parity bit
NID_DIBITS = NID_BITS // 2                   # 32
NID_MAX_ERRS = 11                            # BCH(63,16,23) corrects t = 11

# Status symbols: the transmitter inserts one dibit of channel status after every
# 70 bits (35 dibits) of the data stream, counting from the start of the frame
# sync. Every P25 frame length is a whole multiple of 36 dibits, so the phase
# resets at each sync — which is why we can strip per-frame instead of tracking
# the whole transmission.
STATUS_PERIOD_DIBITS = 36
STATUS_DATA_DIBITS = 35

# ---- Data Unit IDs -------------------------------------------------------
DUID_HDU = 0x0        # Header Data Unit — opens a call, carries the crypto header
DUID_TDU = 0x3        # Terminator, no Link Control
DUID_LDU1 = 0x5       # Voice + Link Control (talkgroup / source ID live here)
DUID_TSDU = 0x7       # Trunking Signalling Data Unit (control channel)
DUID_LDU2 = 0xA       # Voice + Encryption Sync (ALGID / KID / MI live here)
DUID_PDU = 0xC        # Packet Data Unit — recognised, not parsed
DUID_TDULC = 0xF      # Terminator WITH Link Control

DUID_NAMES = {
    DUID_HDU: "HDU", DUID_TDU: "TDU", DUID_LDU1: "LDU1", DUID_TSDU: "TSDU",
    DUID_LDU2: "LDU2", DUID_PDU: "PDU", DUID_TDULC: "TDULC",
}
VOICE_DUIDS = (DUID_LDU1, DUID_LDU2)

# ---- Algorithm IDs -------------------------------------------------------
# 0x80 is the ONLY value that means "you can listen". Everything else is an
# encrypted call; naming the algorithm is the deliverable.
ALGID_CLEAR = 0x80
ALGID_NAMES = {
    0x00: "ACCORDION 1.3 (Type 1)",
    0x01: "BATON, auto even (Type 1)",
    0x02: "FIREFLY Type 1",
    0x03: "MAYFLY Type 1",
    0x04: "SAVILLE (Type 1)",
    0x41: "BATON, auto odd (Type 1)",
    0x80: "unencrypted (clear)",
    0x81: "DES-OFB (56-bit)",
    0x83: "Triple DES (two-key)",
    0x84: "AES-256 (OFB)",
    0x85: "AES-128 (OFB)",
    0x88: "AES-128 (CBC)",
    0x9F: "DES-XL",
    0xA0: "DVI-XL",
    0xA1: "DVP-XL",
    0xAA: "ADP / RC4 (Motorola Advanced Digital Privacy)",
}

# ---- Link Control opcodes we parse --------------------------------------
LCO_GROUP_VOICE = 0x00        # Group Voice Channel User  -> TGID + source ID
LCO_UNIT_VOICE = 0x03         # Unit-to-Unit Voice User   -> target + source ID
LCO_CALL_TERM = 0x0F          # Call Termination / Cancellation -> source ID

# ---- TSBK opcodes we parse ----------------------------------------------
TSBK_GRP_VCH_GRANT = 0x00     # Group Voice Channel Grant -> TGID + source ID
TSBK_GRP_VCH_GRANT_UPD = 0x02
TSBK_BITS = 96                # incl. the trailing 16-bit CRC
TSBK_ENCODED_BITS = 196       # after 1/2-rate trellis coding
MAX_TSBKS = 3                 # a TSDU carries 1..3 blocks; LB flags the last

# ---- payload sizes, in DATA bits following the NID ----------------------
HDU_PAYLOAD_BITS = 648        # 36 hexbits x Golay(18,6,8)
LC_PAYLOAD_BITS = 240         # 24 hexbits x Hamming(10,6,3)  (LDU1)
ES_PAYLOAD_BITS = 240         # 24 hexbits x Hamming(10,6,3)  (LDU2)
TDULC_PAYLOAD_BITS = 432      # 24 hexbits x Golay(18,6,8)
VOICE_FRAME_BITS = 144        # one FEC-protected IMBE frame
VOICE_FRAMES_PER_LDU = 9
LSD_BITS = 32                 # low-speed data, carried but not interpreted here
# LDU1/LDU2 interleave the six 40-bit LC/ES chunks and the LSD between the nine
# voice frames. Offsets are DATA-bit offsets from the end of the NID.
LDU_LAYOUT = (
    ("V", 144), ("V", 144), ("C", 40), ("V", 144), ("C", 40), ("V", 144),
    ("C", 40), ("V", 144), ("C", 40), ("V", 144), ("C", 40), ("V", 144),
    ("C", 40), ("V", 144), ("L", 32), ("V", 144),
)
LDU_PAYLOAD_BITS = sum(n for _, n in LDU_LAYOUT)     # 1568

# ------------------------------------------- IMBE voice-frame transport interleave
# TIA-102.BAAA-A section 5.3 builds each 144-bit voice code word from EIGHT code
# words c_0..c_7: four (23,12,7) Golay, three (15,11,3) Hamming, and c_7 = u_7
# riding bare. Section 5.3.1 then INTERLEAVES those 144 bits across the 72
# transmitted dibit symbols so a fade cannot wipe out one code word — "in order
# to resist fades". Without undoing that on receive, a real off-air frame hands
# the vocoder a shuffled bag of bits and every Golay/Hamming decode fails.
IMBE_CODEWORD_WIDTHS = (23, 23, 23, 23, 15, 15, 15, 7)   # c_0..c_7, sums to 144
assert sum(IMBE_CODEWORD_WIDTHS) == VOICE_FRAME_BITS

# TIA-102.BAAA-A Table 5-1, "Interleaving Schedule for Voice Word", transcribed
# as (code word, bit index) for each transmitted symbol's Bit 1 (MSB) then Bit 0.
# Bit index 22/14/6 is the MSB of its code word, bit 0 the LSB, per the standard's
# own numbering. This table is INDEPENDENTLY CONFIRMED, entry for entry, by the
# per-symbol transmit order listed in the same document's sections 10.3 and 10.4
# for all 18 voice frames of a superframe — two physically separate tables in the
# standard that agree exactly (tests/test_p25.py, TestVoiceInterleave).
VOICE_INTERLEAVE_TABLE = (
    (0, 22), (1, 21),   # symbol  0: c_0(22) c_1(21)
    (2, 20), (3, 19),   # symbol  1: c_2(20) c_3(19)
    (4, 10), (5,  1),   # symbol  2: c_4(10) c_5(1)
    (1, 20), (0, 21),   # symbol  3: c_1(20) c_0(21)
    (3, 18), (2, 19),   # symbol  4: c_3(18) c_2(19)
    (5,  0), (4,  9),   # symbol  5: c_5(0) c_4(9)
    (0, 20), (1, 19),   # symbol  6: c_0(20) c_1(19)
    (2, 18), (3, 17),   # symbol  7: c_2(18) c_3(17)
    (4,  8), (6, 14),   # symbol  8: c_4(8) c_6(14)
    (1, 18), (0, 19),   # symbol  9: c_1(18) c_0(19)
    (3, 16), (2, 17),   # symbol 10: c_3(16) c_2(17)
    (6, 13), (4,  7),   # symbol 11: c_6(13) c_4(7)
    (0, 18), (1, 17),   # symbol 12: c_0(18) c_1(17)
    (2, 16), (3, 15),   # symbol 13: c_2(16) c_3(15)
    (4,  6), (6, 12),   # symbol 14: c_4(6) c_6(12)
    (1, 16), (0, 17),   # symbol 15: c_1(16) c_0(17)
    (3, 14), (2, 15),   # symbol 16: c_3(14) c_2(15)
    (6, 11), (4,  5),   # symbol 17: c_6(11) c_4(5)
    (0, 16), (1, 15),   # symbol 18: c_0(16) c_1(15)
    (2, 14), (3, 13),   # symbol 19: c_2(14) c_3(13)
    (4,  4), (6, 10),   # symbol 20: c_4(4) c_6(10)
    (1, 14), (0, 15),   # symbol 21: c_1(14) c_0(15)
    (3, 12), (2, 13),   # symbol 22: c_3(12) c_2(13)
    (6,  9), (4,  3),   # symbol 23: c_6(9) c_4(3)
    (0, 14), (1, 13),   # symbol 24: c_0(14) c_1(13)
    (2, 12), (3, 11),   # symbol 25: c_2(12) c_3(11)
    (4,  2), (6,  8),   # symbol 26: c_4(2) c_6(8)
    (1, 12), (0, 13),   # symbol 27: c_1(12) c_0(13)
    (3, 10), (2, 11),   # symbol 28: c_3(10) c_2(11)
    (6,  7), (4,  1),   # symbol 29: c_6(7) c_4(1)
    (0, 12), (1, 11),   # symbol 30: c_0(12) c_1(11)
    (2, 10), (3,  9),   # symbol 31: c_2(10) c_3(9)
    (4,  0), (6,  6),   # symbol 32: c_4(0) c_6(6)
    (1, 10), (0, 11),   # symbol 33: c_1(10) c_0(11)
    (3,  8), (2,  9),   # symbol 34: c_3(8) c_2(9)
    (6,  5), (5, 14),   # symbol 35: c_6(5) c_5(14)
    (0, 10), (1,  9),   # symbol 36: c_0(10) c_1(9)
    (2,  8), (3,  7),   # symbol 37: c_2(8) c_3(7)
    (5, 13), (6,  4),   # symbol 38: c_5(13) c_6(4)
    (1,  8), (0,  9),   # symbol 39: c_1(8) c_0(9)
    (3,  6), (2,  7),   # symbol 40: c_3(6) c_2(7)
    (6,  3), (5, 12),   # symbol 41: c_6(3) c_5(12)
    (0,  8), (1,  7),   # symbol 42: c_0(8) c_1(7)
    (2,  6), (3,  5),   # symbol 43: c_2(6) c_3(5)
    (5, 11), (6,  2),   # symbol 44: c_5(11) c_6(2)
    (1,  6), (0,  7),   # symbol 45: c_1(6) c_0(7)
    (3,  4), (2,  5),   # symbol 46: c_3(4) c_2(5)
    (6,  1), (5, 10),   # symbol 47: c_6(1) c_5(10)
    (0,  6), (1,  5),   # symbol 48: c_0(6) c_1(5)
    (2,  4), (3,  3),   # symbol 49: c_2(4) c_3(3)
    (5,  9), (6,  0),   # symbol 50: c_5(9) c_6(0)
    (1,  4), (0,  5),   # symbol 51: c_1(4) c_0(5)
    (3,  2), (2,  3),   # symbol 52: c_3(2) c_2(3)
    (7,  6), (5,  8),   # symbol 53: c_7(6) c_5(8)
    (0,  4), (1,  3),   # symbol 54: c_0(4) c_1(3)
    (2,  2), (3,  1),   # symbol 55: c_2(2) c_3(1)
    (5,  7), (7,  5),   # symbol 56: c_5(7) c_7(5)
    (1,  2), (0,  3),   # symbol 57: c_1(2) c_0(3)
    (3,  0), (2,  1),   # symbol 58: c_3(0) c_2(1)
    (7,  4), (5,  6),   # symbol 59: c_7(4) c_5(6)
    (0,  2), (1,  1),   # symbol 60: c_0(2) c_1(1)
    (2,  0), (4, 14),   # symbol 61: c_2(0) c_4(14)
    (5,  5), (7,  3),   # symbol 62: c_5(5) c_7(3)
    (1,  0), (0,  1),   # symbol 63: c_1(0) c_0(1)
    (4, 13), (3, 22),   # symbol 64: c_4(13) c_3(22)
    (7,  2), (5,  4),   # symbol 65: c_7(2) c_5(4)
    (0,  0), (2, 22),   # symbol 66: c_0(0) c_2(22)
    (3, 21), (4, 12),   # symbol 67: c_3(21) c_4(12)
    (5,  3), (7,  1),   # symbol 68: c_5(3) c_7(1)
    (2, 21), (1, 22),   # symbol 69: c_2(21) c_1(22)
    (4, 11), (3, 20),   # symbol 70: c_4(11) c_3(20)
    (7,  0), (5,  2),   # symbol 71: c_7(0) c_5(2)
)


def _build_voice_permutation():
    """wire bit index -> position in the VECTOR-ORDERED 144-bit frame.

    "Vector order" is c_0 then c_1 ... then c_7, each MSB-first — which is the
    layout survey/ambe.py's unpack_imbe_frame() documents as its required input
    ("Callers pass vector-ordered frame bits"). So this permutation IS the
    contract between the two modules, and it is derived from the standard's
    table rather than agreed informally between them."""
    offsets, pos = [], 0
    for w in IMBE_CODEWORD_WIDTHS:
        offsets.append(pos)
        pos += w
    perm = np.empty(VOICE_FRAME_BITS, dtype=np.int64)
    for wire, (cw, bit) in enumerate(VOICE_INTERLEAVE_TABLE):
        # bit index counts DOWN from the code word MSB, per the standard
        perm[wire] = offsets[cw] + (IMBE_CODEWORD_WIDTHS[cw] - 1 - bit)
    if sorted(perm.tolist()) != list(range(VOICE_FRAME_BITS)):
        raise AssertionError("voice interleave table is not a permutation")
    return perm


# VOICE_DEINTERLEAVE[w] = where wire bit w belongs in the vector-ordered frame.
VOICE_DEINTERLEAVE = _build_voice_permutation()
VOICE_DEINTERLEAVE.flags.writeable = False


def deinterleave_voice_frame(bits144) -> np.ndarray:
    """Received (wire-order) 144 voice bits -> vector order c_0..c_7, MSB-first.

    This is the step that was missing. Our own encoder and decoder were
    symmetric without it, so every synthetic round trip passed while a real
    frame — which IS interleaved — would have failed every Golay and Hamming
    decode in the vocoder."""
    b = np.asarray(bits144, dtype=np.uint8).ravel()
    if b.size != VOICE_FRAME_BITS:
        raise ValueError(f"voice frame needs {VOICE_FRAME_BITS} bits, got {b.size}")
    out = np.empty(VOICE_FRAME_BITS, dtype=np.uint8)
    out[VOICE_DEINTERLEAVE] = b
    return out


def interleave_voice_frame(bits144) -> np.ndarray:
    """Vector-ordered 144 voice bits -> wire order. Exact inverse of
    deinterleave_voice_frame(); used by the synth encoder so the bits it puts on
    the air are laid out the way a real transmitter lays them out."""
    b = np.asarray(bits144, dtype=np.uint8).ravel()
    if b.size != VOICE_FRAME_BITS:
        raise ValueError(f"voice frame needs {VOICE_FRAME_BITS} bits, got {b.size}")
    return b[VOICE_DEINTERLEAVE]


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


_POPCNT8 = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)
# bit distance between two dibit values — the sync search metric
_DIBIT_ERR = np.array([[_popcount(a ^ b) for b in range(4)] for a in range(4)],
                      dtype=np.uint8)

# frame sync expressed as 24 dibits (derived, never hand-typed)
FRAME_SYNC_DIBITS = bits_to_dibits(_int_to_bits(FRAME_SYNC, FRAME_SYNC_BITS))
assert FRAME_SYNC_DIBITS.size == SYNC_DIBITS


# ---------------------------------------------------------------- GF(2^6) field
# Primitive polynomial x^6 + x + 1. Used by the NID BCH code and by every P25
# Reed-Solomon code (all of which are shortened RS over GF(64)).
GF_POLY = 0x43
_EXP = [0] * 126
_LOG = [0] * 64
_x = 1
for _i in range(63):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x40:
        _x ^= GF_POLY
for _i in range(63, 126):
    _EXP[_i] = _EXP[_i - 63]
del _x, _i


def _gmul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


def _ginv(a: int) -> int:
    return _EXP[(63 - _LOG[a]) % 63]


def _gpow(a: int, k: int) -> int:
    if a == 0:
        return 0 if k else 1
    return _EXP[(_LOG[a] * k) % 63]


# ------------------------------------------------------------ BCH(63,16,23) NID
def _gf2_polymul(a: int, b: int) -> int:
    r = 0
    for i in range(b.bit_length()):
        if (b >> i) & 1:
            r ^= a << i
    return r


def _gf2_polydiv(num: int, den: int):
    """(quotient, remainder) of GF(2) polynomials held as integer bit masks."""
    q, r = 0, num
    dd = den.bit_length() - 1
    while r.bit_length() - 1 >= dd and r:
        sh = (r.bit_length() - 1) - dd
        q |= 1 << sh
        r ^= den << sh
    return q, r


def _minimal_poly(i: int) -> int:
    """Minimal polynomial over GF(2) of alpha^i, as a bit mask (bit k = x^k)."""
    coset, e = [], i % 63
    while e not in coset:
        coset.append(e)
        e = (e * 2) % 63
    coeffs = [1]                                   # coefficients in GF(64)
    for e in coset:
        root = _EXP[e]
        new = [0] * (len(coeffs) + 1)
        for k, c in enumerate(coeffs):
            new[k + 1] ^= c                        # multiply by x
            new[k] ^= _gmul(c, root)               # multiply by alpha^e
        coeffs = new
    m = 0
    for k, c in enumerate(coeffs):
        if c not in (0, 1):                        # must collapse to GF(2)
            raise AssertionError("minimal polynomial left GF(2)")
        if c:
            m |= 1 << k
    return m


def _build_bch_generator() -> int:
    """g(x) = lcm of the minimal polynomials of alpha^1, alpha^3 .. alpha^21.

    Derived rather than table-copied: BCH(63,16,23) is BY DEFINITION the code
    whose generator has alpha^1..alpha^22 among its roots, and the odd-power
    minimal polynomials supply all of them. The resulting degree is 47 (= 63-16)
    and tests/test_p25.py pins the code's minimum distance at exactly 23, so a
    wrong field or a wrong root set fails loudly instead of quietly weakening the
    correction."""
    g = 1
    for i in range(1, 23, 2):
        mp = _minimal_poly(i)
        # lcm(g, mp) = g * mp / gcd(g, mp)
        a, b = g, mp
        while b:
            a, b = b, _gf2_polydiv(a, b)[1]
        q, r = _gf2_polydiv(_gf2_polymul(g, mp), a)
        if r:
            raise AssertionError("BCH generator lcm did not divide evenly")
        g = q
    return g


BCH_GEN = _build_bch_generator()      # degree 47
BCH_GEN_DEG = BCH_GEN.bit_length() - 1


def bch63_16_encode(msg16: int) -> int:
    """16 information bits (NAC<<4 | DUID) -> 63-bit systematic BCH codeword.

    Message occupies bits 62..47; the degree-46 remainder fills bits 46..0."""
    reg = (msg16 & 0xFFFF) << BCH_GEN_DEG
    return reg | _gf2_polydiv(reg, BCH_GEN)[1]


def nid_encode(nac: int, duid: int) -> int:
    """NAC(12) + DUID(4) -> the full 64-bit NID (63-bit BCH + even parity)."""
    msg = ((nac & 0xFFF) << 4) | (duid & 0xF)
    cw = bch63_16_encode(msg)
    return (cw << 1) | (_popcount(cw) & 1)          # bit 0 = even parity


_NID_CODEBOOK = None


def _nid_codebook() -> np.ndarray:
    """(65536, 8) uint8 table: every valid 63-bit NID codeword, big-endian bytes.

    The code is linear, so all 65536 codewords are XOR combinations of the 16
    basis codewords — built with 16 vectorised numpy passes, not 65536 divisions.
    Cached; ~512 kB."""
    global _NID_CODEBOOK
    if _NID_CODEBOOK is None:
        basis = [bch63_16_encode(1 << i) for i in range(16)]
        idx = np.arange(1 << 16, dtype=np.uint64)
        words = np.zeros(1 << 16, dtype=np.uint64)
        for i in range(16):
            sel = ((idx >> np.uint64(i)) & np.uint64(1)).astype(bool)
            words[sel] ^= np.uint64(basis[i])
        _NID_CODEBOOK = np.stack(
            [((words >> np.uint64(8 * k)) & np.uint64(0xFF)).astype(np.uint8)
             for k in range(7, -1, -1)], axis=1)
    return _NID_CODEBOOK


def bch63_16_decode(cw63: int):
    """(message16, bit_errors) by MINIMUM-DISTANCE search over all 2^16 codewords.

    This is exact maximum-likelihood decoding of BCH(63,16,23) — strictly at
    least as strong as a syndrome/Berlekamp decoder, which is only defined out to
    t = 11, and it never mis-corrects a word that a Berlekamp decoder would have
    handled. The caller applies the acceptance threshold; this function only
    reports the closest codeword and how far away it was, so a garbage word comes
    back with a large distance rather than a plausible-looking NAC."""
    cb = _nid_codebook()
    rb = np.array([(cw63 >> (8 * k)) & 0xFF for k in range(7, -1, -1)],
                  dtype=np.uint8)
    dist = _POPCNT8[cb ^ rb].sum(axis=1)
    j = int(np.argmin(dist))
    return j, int(dist[j])


def decode_nid(nid64: int):
    """64-bit NID -> dict(nac, duid, bit_errors, parity_ok) or None if the closest
    BCH codeword is further than the code can correct."""
    cw63 = (nid64 >> 1) & 0x7FFFFFFFFFFFFFFF
    parity = nid64 & 1
    msg, errs = bch63_16_decode(cw63)
    if errs > NID_MAX_ERRS:
        return None
    corrected = bch63_16_encode(msg)
    return {"nac": (msg >> 4) & 0xFFF, "duid": msg & 0xF,
            "bit_errors": errs,
            "parity_ok": (_popcount(corrected) & 1) == parity}


# ------------------------------------------------- short block codes (64 words)
# TIA-102.BAAA-A section 5.7, Table 5-3 "Golay Generator Matrices", the (24,12,8)
# column. The standard prints each row as two octal groups — the 12-bit identity
# half then the 12-bit parity half — so only the parity halves are needed here.
# Row 1 is 6165 octal, which is g(x) = x^11+x^10+x^6+x^5+x^4+x^2+1 itself.
#
# This is NOT a "textbook" Golay matrix: the extended Golay code is unique up to
# equivalence, so ANY correct generator gives weight distribution {0,8,12,16,24}
# and passes every self-consistency check — while mapping information bits onto
# DIFFERENT codewords. Only the transmitter's actual matrix decodes a real frame.
GOLAY24_PARITY_ROWS = (0o6165, 0o3073, 0o7550, 0o3664, 0o1732, 0o6631,
                       0o3315, 0o1547, 0o6706, 0o5227, 0o4476, 0o4353)


def _extended_golay24_parity(data12: int) -> int:
    """12 information bits -> 12 parity bits of the extended binary Golay [24,12,8].

    Generator taken from TIA-102.BAAA-A Table 5-3. tests/test_p25.py pins both
    the matrix itself and the resulting weight distribution {0, 8, 12, 16, 24}."""
    par = 0
    for i in range(12):
        if (data12 >> (11 - i)) & 1:
            par ^= GOLAY24_PARITY_ROWS[i]
    return par


def _build_golay18() -> list:
    """(18,6,8) shortened Golay, exactly as TIA-102.BAAA-A section 5.7 defines it:
    "formed by deleting the left most 6 bits from the (24,12,8) extended Golay
    code" — i.e. the [24,12,8] codewords whose top 6 information bits are zero,
    with those 6 all-zero coordinates removed. 64 codewords, min distance 8.

    Checked in tests against Table 5-3's own (18,6,8) column, which the standard
    prints separately: the six rows there are the last six rows of the (24,12,8)
    matrix truncated to 18 bits, and this construction reproduces them."""
    out = []
    for d6 in range(64):
        full = (d6 << 12) | _extended_golay24_parity(d6)
        out.append(full & 0x3FFFF)                 # keep the low 18 coordinates
    return out


# TIA-102.BAAA-A section 5.8, Table 5-4, the (10,6,3) shortened Hamming column:
# the 4 parity bits generated by each of the 6 information bits, MSB row first.
# The standard is explicit that this shortening is NOT the obvious one — "The
# shortened code is determined by deleting particular rows and columns from the
# standard Hamming code, not just the left hand columns and top rows. The reason
# for this is to improve the likelihood of error detection for the shortened
# code." So a locally-invented (10,6,3) of the same minimum distance is a
# different code and mis-maps every hexbit off-air.
HAMMING10_PARITY_ROWS = (0b1110, 0b1101, 0b1011, 0b0111, 0b0011, 0b1100)


def _build_hamming10() -> list:
    """(10,6,3) shortened Hamming from TIA-102.BAAA-A Table 5-4, systematic:
    6 information bits MSB-first, then the 4 parity bits — which is also the
    order the standard's transmit-order annex puts them on the air."""
    out = []
    for d6 in range(64):
        par = 0
        for i in range(6):
            if (d6 >> (5 - i)) & 1:
                par ^= HAMMING10_PARITY_ROWS[i]
        out.append((d6 << 4) | par)
    return out


GOLAY18_CODEBOOK = _build_golay18()
HAMMING10_CODEBOOK = _build_hamming10()


def _closest(codebook, received: int, width: int):
    """(data6, bit_errors) — exhaustive minimum-distance decode of a 64-word block
    code. 64 comparisons; correctness is not in question and there is no syndrome
    table to get wrong."""
    best_i, best_d = 0, width + 1
    for i, cw in enumerate(codebook):
        d = _popcount(cw ^ received)
        if d < best_d:
            best_i, best_d = i, d
            if d == 0:
                break
    return best_i, best_d


def golay18_encode(hexbit: int) -> int:
    return GOLAY18_CODEBOOK[hexbit & 0x3F]


def golay18_decode(word18: int):
    return _closest(GOLAY18_CODEBOOK, word18 & 0x3FFFF, 18)


def hamming10_encode(hexbit: int) -> int:
    return HAMMING10_CODEBOOK[hexbit & 0x3F]


def hamming10_decode(word10: int):
    return _closest(HAMMING10_CODEBOOK, word10 & 0x3FF, 10)


# ------------------------------------------------------ Reed-Solomon over GF(64)
def _rs_generator(nroots: int) -> list:
    """g(x) = prod_{i=1..nroots} (x - alpha^i). Narrow-sense (first consecutive
    root = 1) — flagged in the module docstring as the assumption most likely to
    differ from TIA. g[k] is the coefficient of x^k."""
    g = [1]
    for i in range(1, nroots + 1):
        new = [0] * (len(g) + 1)
        for k, c in enumerate(g):
            new[k + 1] ^= c
            new[k] ^= _gmul(c, _EXP[i])
        g = new
    return g


def rs_encode(msg, nroots: int) -> list:
    """Systematic RS over GF(64): message symbols followed by `nroots` parity
    symbols. msg[0] is the highest-order symbol."""
    g = _rs_generator(nroots)
    gd = [g[nroots - i] for i in range(nroots + 1)]      # descending; gd[0] == 1
    r = list(msg) + [0] * nroots
    for i in range(len(msg)):
        c = r[i]
        if c:
            for j in range(1, nroots + 1):
                r[i + j] ^= _gmul(gd[j], c)
    return list(msg) + r[len(msg):]


def rs_decode(codeword, nroots: int):
    """(ok, corrected_symbols, n_symbol_errors) — syndromes, Berlekamp-Massey,
    Chien search, Forney magnitudes.

    The corrected word is RE-VERIFIED against all `nroots` syndromes before it is
    returned. Beyond t = nroots//2 errors the decoder reports failure rather than
    handing back a confidently wrong payload — which is the whole point: a P25
    frame whose RS fails must surface as "payload FEC failed", not as a call
    record with a fabricated talkgroup."""
    cw = list(codeword)
    n = len(cw)
    synd = []
    for i in range(1, nroots + 1):
        s, a = 0, _EXP[i]
        for c in cw:
            s = _gmul(s, a) ^ c
        synd.append(s)
    if not any(synd):
        return True, cw, 0

    lam, b = [1], [1]
    L, m, bb = 0, 1, 1
    for r_ in range(nroots):
        d = synd[r_]
        for i in range(1, L + 1):
            if i < len(lam):
                d ^= _gmul(lam[i], synd[r_ - i])
        if d == 0:
            m += 1
            continue
        coef = _gmul(d, _ginv(bb))
        newlam = list(lam) + [0] * max(0, m + len(b) - len(lam))
        for i, bc in enumerate(b):
            newlam[i + m] ^= _gmul(coef, bc)
        if 2 * L <= r_:
            old = list(lam)
            L, b, bb, m = r_ + 1 - L, old, d, 1
        else:
            m += 1
        lam = newlam

    if L == 0 or L > nroots // 2:
        return False, list(codeword), -1
    errpos = []
    for i in range(n):
        xinv = _EXP[(63 - ((n - 1 - i) % 63)) % 63]
        v = 0
        for k in range(len(lam) - 1, -1, -1):
            v = _gmul(v, xinv) ^ lam[k]
        if v == 0:
            errpos.append(i)
    if len(errpos) != L:
        return False, list(codeword), -1

    omega = [0] * nroots
    for i in range(nroots):
        s = 0
        for j in range(0, min(i, len(lam) - 1) + 1):
            s ^= _gmul(synd[i - j], lam[j])
        omega[i] = s
    out = list(cw)
    for i in errpos:
        xinv = _EXP[(63 - ((n - 1 - i) % 63)) % 63]
        num = 0
        for k in range(nroots - 1, -1, -1):
            num = _gmul(num, xinv) ^ omega[k]
        den = 0
        for k in range(1, len(lam), 2):
            den ^= _gmul(lam[k], _gpow(xinv, k - 1))
        if den == 0:
            return False, list(codeword), -1
        out[i] ^= _gmul(num, _ginv(den))
    for i in range(1, nroots + 1):                  # re-verify, never trust BM alone
        s, a = 0, _EXP[i]
        for c in out:
            s = _gmul(s, a) ^ c
        if s:
            return False, list(codeword), -1
    return True, out, len(errpos)


# ------------------------------------------------ hexbit <-> protected-bit layer
def _hexbits_to_bits(hexbits) -> list:
    bits = []
    for h in hexbits:
        bits.extend(_int_to_bits(h & 0x3F, 6))
    return bits


def _bits_to_hexbits(bits) -> list:
    return [_bits_to_int(bits[i:i + 6]) for i in range(0, len(bits) - 5, 6)]


def _protect_hexbits(hexbits, encoder, width: int) -> list:
    bits = []
    for h in hexbits:
        bits.extend(_int_to_bits(encoder(h), width))
    return bits


def _recover_hexbits(bits, decoder, width: int):
    """Protected bit stream -> (hexbits, total_bit_errors)."""
    hexbits, errs = [], 0
    for i in range(0, len(bits) - width + 1, width):
        h, e = decoder(_bits_to_int(bits[i:i + width]))
        hexbits.append(h)
        errs += e
    return hexbits, errs


def _rs_block_decode(bits, decoder, width: int, k: int, nroots: int):
    """Protected bits -> (ok, information_bits, stats). One function for all three
    P25 RS-protected payloads; `ok` is False unless the RS decode verified.

    A truncated capture is rejected up front: a short block would be decoded as a
    DIFFERENTLY-shortened RS code, and while its syndromes would almost certainly
    fail anyway, refusing it outright removes the question."""
    n = k + nroots
    stats = {"block_bit_errors": 0, "rs_symbol_errors": -1, "rs_ok": False}
    if len(bits) < n * width:
        stats["truncated"] = True
        return False, [], stats
    hexbits, blk_errs = _recover_hexbits(bits[:n * width], decoder, width)
    ok, sym, nerr = rs_decode(hexbits, nroots)
    stats = {"block_bit_errors": blk_errs, "rs_symbol_errors": nerr, "rs_ok": ok}
    if not ok:
        return False, [], stats
    return True, _hexbits_to_bits(sym[:k]), stats


def _rs_block_encode(info_bits, encoder, width: int, nroots: int) -> list:
    hexbits = _bits_to_hexbits(info_bits)
    return _protect_hexbits(rs_encode(hexbits, nroots), encoder, width)


# ---------------------------------------------------------- 1/2-rate trellis + CRC
# P25 1/2-rate trellis: 4 states (the state IS the previous input dibit); each
# input dibit emits a 4-bit constellation word sent as two dibits, MSB dibit
# first. Free distance is 5 bits (asserted in tests) so the Viterbi decoder fixes
# up to 2 bit errors per divergence.
TRELLIS_NEXT = ((0x2, 0xC, 0x1, 0xF),
                (0xE, 0x0, 0xD, 0x3),
                (0x9, 0x7, 0xA, 0x4),
                (0x5, 0xB, 0x6, 0x8))


def trellis_half_encode(dibits) -> np.ndarray:
    """Input dibits (plus the caller's flush dibit) -> 2x as many coded dibits."""
    out, state = [], 0
    for d in dibits:
        d = int(d) & 3
        w = TRELLIS_NEXT[state][d]
        out.append((w >> 2) & 3)
        out.append(w & 3)
        state = d
    return np.array(out, dtype=np.uint8)


def trellis_half_decode(coded_dibits):
    """Viterbi over the 4-state trellis. Returns (input_dibits, bit_errors).

    Terminates on state 0 when the encoder's flush dibit survived; otherwise it
    falls back to the best surviving state and the caller's CRC decides."""
    cd = np.asarray(coded_dibits, dtype=np.uint8)
    nsteps = cd.size // 2
    if nsteps == 0:
        return np.zeros(0, dtype=np.uint8), 0
    INF = 1 << 20
    metric = [0, INF, INF, INF]
    paths = [[], [], [], []]
    for t in range(nsteps):
        rx = ((int(cd[2 * t]) & 3) << 2) | (int(cd[2 * t + 1]) & 3)
        new_metric = [INF] * 4
        new_paths = [None] * 4
        for s in range(4):
            if metric[s] >= INF:
                continue
            for d in range(4):
                cost = metric[s] + _popcount(TRELLIS_NEXT[s][d] ^ rx)
                if cost < new_metric[d]:            # next state == input dibit
                    new_metric[d] = cost
                    new_paths[d] = paths[s] + [d]
        metric = new_metric
        paths = [p if p is not None else [] for p in new_paths]
    end = 0 if metric[0] < INF else int(np.argmin(metric))
    return np.array(paths[end], dtype=np.uint8), int(metric[end])


def crc16_p25(bits) -> int:
    """P25 CRC-16: x^16 + x^12 + x^5 + 1, zero seed, final complement — the
    variant used by the established open P25 implementations (flagged in the
    module docstring as inherited rather than derived)."""
    poly = (1 << 12) | (1 << 5) | 1
    crc = 0
    for b in bits:
        crc = ((crc << 1) | (int(b) & 1)) & 0x1FFFF
        if crc & 0x10000:
            crc = (crc & 0xFFFF) ^ poly
    return (~crc) & 0xFFFF


# ---------------------------------------------------------------- field parsers
def algid_name(algid: int) -> str:
    return ALGID_NAMES.get(algid & 0xFF, "unknown/proprietary")


def parse_link_control(bits):
    """72-bit Link Control Word -> dict.

    Octet 0 = P(1) | SF(1) | LCO(6); octet 1 = MFID; octets 2..8 = arguments.
    P is the LCW's own 'protected' flag — a second, independent encryption
    indicator alongside the ALGID carried in the HDU/LDU2."""
    if len(bits) < 72:
        return None
    o = [_bits_to_int(bits[i * 8:(i + 1) * 8]) for i in range(9)]
    lc = {"protected_flag": bool(o[0] & 0x80), "implicit_mfid": bool(o[0] & 0x40),
          "lco": o[0] & 0x3F, "mfid": o[1], "tgid": None, "source_id": None,
          "target_id": None, "emergency": False, "priority": 0,
          "raw": bytes(o).hex()}
    if lc["lco"] == LCO_GROUP_VOICE:
        svc = o[2]
        lc["emergency"] = bool(svc & 0x80)
        lc["service_protected"] = bool(svc & 0x40)
        lc["priority"] = svc & 0x07
        lc["tgid"] = (o[4] << 8) | o[5]
        lc["source_id"] = (o[6] << 16) | (o[7] << 8) | o[8]
    elif lc["lco"] == LCO_UNIT_VOICE:
        svc = o[2]
        lc["emergency"] = bool(svc & 0x80)
        lc["service_protected"] = bool(svc & 0x40)
        lc["priority"] = svc & 0x07
        lc["target_id"] = (o[3] << 16) | (o[4] << 8) | o[5]
        lc["source_id"] = (o[6] << 16) | (o[7] << 8) | o[8]
    elif lc["lco"] == LCO_CALL_TERM:
        lc["source_id"] = (o[6] << 16) | (o[7] << 8) | o[8]
    return lc


def parse_encryption_sync(bits):
    """96-bit Encryption Sync Word (LDU2) -> MI(72) + ALGID(8) + KID(16)."""
    if len(bits) < 96:
        return None
    o = [_bits_to_int(bits[i * 8:(i + 1) * 8]) for i in range(12)]
    algid = o[9]
    return {"mi": bytes(o[0:9]).hex(), "algid": algid,
            "algid_name": algid_name(algid), "kid": (o[10] << 8) | o[11],
            "encrypted": algid != ALGID_CLEAR}


def parse_hdu_header(bits):
    """120-bit HDU header -> MI(72) + MFID(8) + ALGID(8) + KID(16) + TGID(16)."""
    if len(bits) < 120:
        return None
    o = [_bits_to_int(bits[i * 8:(i + 1) * 8]) for i in range(15)]
    algid = o[10]
    return {"mi": bytes(o[0:9]).hex(), "mfid": o[9], "algid": algid,
            "algid_name": algid_name(algid), "kid": (o[11] << 8) | o[12],
            "tgid": (o[13] << 8) | o[14], "encrypted": algid != ALGID_CLEAR}


def parse_tsbk(bits):
    """96-bit TSBK -> dict, or None if the CRC-16 fails.

    Layout: LB(1) | P(1) | opcode(6) | MFID(8) | arguments(64) | CRC(16)."""
    if len(bits) < TSBK_BITS:
        return None
    if crc16_p25(bits[:80]) != _bits_to_int(bits[80:96]):
        return None
    o = [_bits_to_int(bits[i * 8:(i + 1) * 8]) for i in range(10)]
    tsbk = {"last_block": bool(o[0] & 0x80), "protected_flag": bool(o[0] & 0x40),
            "opcode": o[0] & 0x3F, "mfid": o[1], "tgid": None, "source_id": None,
            "channel": None, "raw": bytes(o[:10]).hex()}
    if tsbk["opcode"] in (TSBK_GRP_VCH_GRANT, TSBK_GRP_VCH_GRANT_UPD):
        # args: service options(8) | channel(16) | group address(16) | source(24)
        tsbk["emergency"] = bool(o[2] & 0x80)
        tsbk["channel"] = (o[3] << 8) | o[4]
        tsbk["tgid"] = (o[5] << 8) | o[6]
        tsbk["source_id"] = (o[7] << 16) | (o[8] << 8) | o[9]
    return tsbk


# ---------------------------------------------------------------- frame locating
def find_sync(dibits, max_bit_errs: int = SYNC_MAX_BIT_ERRS):
    """Every dibit index whose next 24 dibits are within `max_bit_errs` BITS of
    the 48-bit frame sync. Vectorised: 24 numpy passes, not a Python slide."""
    d = np.asarray(dibits, dtype=np.uint8)
    n = d.size
    if n < SYNC_DIBITS:
        return []
    span = n - SYNC_DIBITS + 1
    err = np.zeros(span, dtype=np.int16)
    for k in range(SYNC_DIBITS):
        err += _DIBIT_ERR[d[k:k + span], FRAME_SYNC_DIBITS[k]]
    return [int(i) for i in np.nonzero(err <= max_bit_errs)[0]]


def strip_status_symbols(dibits, offset: int = 0) -> np.ndarray:
    """Drop the channel-status dibit that follows every 35 data dibits, counting
    from `offset` (normally the frame-sync position)."""
    d = np.asarray(dibits, dtype=np.uint8)
    idx = np.arange(d.size)
    keep = ((idx - offset) % STATUS_PERIOD_DIBITS) != STATUS_DATA_DIBITS
    keep |= idx < offset
    return d[keep]


def _data_dibits(dibits, sync_pos: int, count: int, status: bool,
                 allow_short: bool = False):
    """`count` payload dibits starting immediately after the sync word at
    `sync_pos`, skipping status symbols when `status` is True. Returns None if
    the capture runs out, unless `allow_short` (then: whatever is there).

    Data index j sits at transmitted index j + j//35 relative to the sync, which
    is why the status phase can be resolved per frame: every P25 frame length is
    a whole multiple of 36 dibits, so the phase restarts at each sync."""
    d = np.asarray(dibits, dtype=np.uint8)
    if not status:
        start = sync_pos + SYNC_DIBITS
        end = start + count
        if end > d.size:
            if not allow_short:
                return None
            end = d.size
        return d[start:end]
    j = np.arange(SYNC_DIBITS, SYNC_DIBITS + count)
    idx = sync_pos + j + (j // STATUS_DATA_DIBITS)
    if idx.size and int(idx[-1]) >= d.size:
        if not allow_short:
            return None
        idx = idx[idx < d.size]
    return d[idx]


# ---------------------------------------------------------------- payload decode
def _split_ldu(payload_bits):
    """LDU payload bits -> (nine 144-bit voice frames, 240 LC/ES bits, 32 LSD bits).

    Voice frames come back DE-INTERLEAVED, i.e. in code-word order c_0..c_7 —
    the layout the vocoder needs. A frame that is short (truncated capture) is
    passed through untouched rather than zero-padded, so it stays the wrong
    length and downstream consumers skip it instead of decoding padding."""
    voice, ctrl, lsd, pos = [], [], [], 0
    for kind, nbits in LDU_LAYOUT:
        chunk = payload_bits[pos:pos + nbits]
        pos += nbits
        if kind == "V":
            vf = np.asarray(chunk, dtype=np.uint8)
            voice.append(deinterleave_voice_frame(vf)
                         if vf.size == VOICE_FRAME_BITS else vf)
        elif kind == "C":
            ctrl.extend(int(b) for b in chunk)
        else:
            lsd.extend(int(b) for b in chunk)
    return voice, ctrl, lsd


def _decode_payload(duid: int, payload_bits, keep_voice: bool) -> dict:
    """Per-DUID payload parse. Every branch either produces verified fields or
    sets fec_ok False — nothing is guessed."""
    out = {"fec_ok": None, "fec": {}}
    bits = list(int(b) for b in payload_bits)

    if duid == DUID_HDU:
        ok, info, st = _rs_block_decode(bits[:HDU_PAYLOAD_BITS], golay18_decode,
                                        18, 20, 16)
        out["fec"], out["fec_ok"] = st, ok
        if ok:
            hdr = parse_hdu_header(info)
            if hdr:
                out.update(hdr)
        return out

    if duid in VOICE_DUIDS:
        voice, ctrl, lsd = _split_ldu(bits[:LDU_PAYLOAD_BITS])
        if keep_voice:
            # Handover point for the vocoder (survey/ambe.py). We deliberately
            # stop here: these are FEC-protected IMBE frames, not audio. They
            # ARE de-interleaved — vector order c_0..c_7, MSB-first, which is
            # exactly what ambe.unpack_imbe_frame() asks its callers for.
            out["voice_bits"] = voice
        out["voice_frame_count"] = len(voice)
        out["lsd_bits"] = lsd
        if duid == DUID_LDU1:
            ok, info, st = _rs_block_decode(ctrl, hamming10_decode, 10, 12, 12)
            out["fec"], out["fec_ok"] = st, ok
            if ok:
                lc = parse_link_control(info)
                if lc:
                    out["link_control"] = lc
                    out["tgid"] = lc["tgid"]
                    out["source_id"] = lc["source_id"]
                    out["lco"] = lc["lco"]
        else:
            ok, info, st = _rs_block_decode(ctrl, hamming10_decode, 10, 16, 8)
            out["fec"], out["fec_ok"] = st, ok
            if ok:
                es = parse_encryption_sync(info)
                if es:
                    out.update(es)
        return out

    if duid == DUID_TDULC:
        ok, info, st = _rs_block_decode(bits[:TDULC_PAYLOAD_BITS], golay18_decode,
                                        18, 12, 12)
        out["fec"], out["fec_ok"] = st, ok
        if ok:
            lc = parse_link_control(info)
            if lc:
                out["link_control"] = lc
                out["tgid"] = lc["tgid"]
                out["source_id"] = lc["source_id"]
                out["lco"] = lc["lco"]
        return out

    if duid == DUID_TSDU:
        tsbks, pos, ok_any = [], 0, False
        for _ in range(MAX_TSBKS):
            if pos + TSBK_ENCODED_BITS > len(bits):
                break
            coded = bits_to_dibits(bits[pos:pos + TSBK_ENCODED_BITS])
            pos += TSBK_ENCODED_BITS
            dib, terr = trellis_half_decode(coded)
            blk = list(dibits_to_bits(dib))[:TSBK_BITS]
            tsbk = parse_tsbk(blk)
            if tsbk is None:
                break                       # CRC gate: stop rather than guess
            tsbk["trellis_bit_errors"] = terr
            tsbks.append(tsbk)
            ok_any = True
            if tsbk["last_block"]:
                break
        out["tsbks"] = tsbks
        out["fec_ok"] = ok_any
        # A TSDU carries 1..3 blocks; only the blocks that passed CRC were really
        # here, so the frame's true length follows the count (this is what keeps
        # the scanner from swallowing the NEXT frame's sync).
        out["payload_dibits"] = len(tsbks) * (TSBK_ENCODED_BITS // 2)
        for t in tsbks:                     # surface the first grant's identifiers
            if t.get("tgid") is not None:
                out["tgid"] = t["tgid"]
                out["source_id"] = t["source_id"]
                break
        return out

    # TDU carries no payload; PDU is recognised but deliberately not parsed.
    out["fec_ok"] = True if duid == DUID_TDU else None
    return out


_PAYLOAD_DIBITS = {
    DUID_HDU: HDU_PAYLOAD_BITS // 2,
    DUID_LDU1: LDU_PAYLOAD_BITS // 2,
    DUID_LDU2: LDU_PAYLOAD_BITS // 2,
    DUID_TDULC: TDULC_PAYLOAD_BITS // 2,
    DUID_TSDU: (TSBK_ENCODED_BITS * MAX_TSBKS) // 2,
    DUID_TDU: 0,
    DUID_PDU: 0,
}


def decode_frame(dibits, sync_pos: int, status=None, keep_voice: bool = True):
    """One frame at `sync_pos`. Returns a frame dict, or None if the NID failed.

    `status` selects status-symbol stripping; None auto-resolves it per frame by
    decoding the NID both ways and keeping whichever is closer to a valid BCH
    codeword. That is a free and reliable discriminator because the first status
    symbol lands INSIDE the NID (transmitted dibit 35 of the frame)."""
    d = np.asarray(dibits, dtype=np.uint8)
    modes = (False, True) if status is None else (bool(status),)
    best = None
    for mode in modes:
        nid_d = _data_dibits(d, sync_pos, NID_DIBITS, mode)
        if nid_d is None:
            continue
        info = decode_nid(_bits_to_int(dibits_to_bits(nid_d)))
        if info is None:
            continue
        if best is None or info["bit_errors"] < best[1]["bit_errors"]:
            best = (mode, info)
    if best is None:
        return None
    mode, info = best

    frame = {"offset_dibit": sync_pos, "status_symbols": mode,
             "nac": info["nac"], "nac_hex": f"0x{info['nac']:03X}",
             "duid": info["duid"],
             "duid_name": DUID_NAMES.get(info["duid"], f"reserved(0x{info['duid']:X})"),
             "nid_bit_errors": info["bit_errors"], "nid_parity_ok": info["parity_ok"],
             "tgid": None, "source_id": None,
             # TDU carries nothing to check, so it is trivially fec_ok; a
             # reserved/unhandled DUID stays None — "not assessed", not "passed".
             "fec_ok": True if info["duid"] == DUID_TDU else None}
    want = _PAYLOAD_DIBITS.get(info["duid"], 0)
    frame["payload_dibits"] = want
    if want:
        # NID first, then the payload — one contiguous read so status stripping
        # keeps its phase across the whole frame. allow_short lets a capture that
        # ends mid-frame still report its NAC/DUID; the payload FEC then fails
        # honestly rather than the whole frame vanishing.
        both = _data_dibits(d, sync_pos, NID_DIBITS + want, mode, allow_short=True)
        payload = dibits_to_bits(both[NID_DIBITS:])
        if payload.size < want * 2:
            frame["truncated"] = True
        frame.update(_decode_payload(info["duid"], payload, keep_voice))
    return frame


# ---------------------------------------------------------------- call assembly
def _blank_call(frame) -> dict:
    return {"nac": frame["nac"], "nac_hex": frame["nac_hex"], "tgid": None,
            "source_id": None, "target_id": None, "algid": None,
            "algid_name": None, "kid": None, "mi": None, "encrypted": None,
            "emergency": False, "lc_protected_flag": None, "duids": [],
            "frames": 0, "voice_frames": 0}


def _merge_frame(call: dict, frame: dict):
    call["frames"] += 1
    name = frame["duid_name"]
    if name not in call["duids"]:
        call["duids"].append(name)
    call["voice_frames"] += frame.get("voice_frame_count", 0)
    for key in ("tgid", "source_id"):
        if frame.get(key) is not None and call[key] is None:
            call[key] = frame[key]
    lc = frame.get("link_control")
    if lc:
        if lc.get("target_id") is not None and call["target_id"] is None:
            call["target_id"] = lc["target_id"]
        if lc.get("emergency"):
            call["emergency"] = True
        if call["lc_protected_flag"] is None:
            call["lc_protected_flag"] = lc.get("protected_flag")
    if frame.get("algid") is not None:
        # ALGID from an HDU or LDU2 is authoritative — it is the field finding.
        call["algid"] = frame["algid"]
        call["algid_name"] = frame["algid_name"]
        call["encrypted"] = frame["encrypted"]
        if frame.get("kid") is not None:
            call["kid"] = frame["kid"]
        if frame.get("mi"):
            call["mi"] = frame["mi"]


def _describe(call: dict) -> str:
    """The one line an operator reads in the field. Encrypted is a RESULT."""
    parts = [f"P25 Phase 1, NAC {call['nac_hex']}"]
    if call["tgid"] is not None:
        parts.append(f"talkgroup {call['tgid']}")
    if call["source_id"] is not None:
        parts.append(f"radio ID {call['source_id']}")
    if call["target_id"] is not None:
        parts.append(f"to radio {call['target_id']}")
    if call["encrypted"] is True:
        parts.append(f"ALGID 0x{call['algid']:02X} ({call['algid_name']})"
                     f", key ID 0x{(call['kid'] or 0):04X} — ENCRYPTED, "
                     "content not recoverable; identify and locate")
    elif call["encrypted"] is False:
        parts.append(f"ALGID 0x{call['algid']:02X} (clear) — unencrypted, "
                     "voice frames available to the vocoder")
    elif call["lc_protected_flag"]:
        parts.append("Link Control 'protected' flag set but no ALGID seen "
                     "(no HDU/LDU2 captured) — treat as probably encrypted")
    else:
        parts.append("encryption state unknown (no HDU/LDU2 in this capture)")
    if call["emergency"]:
        parts.append("EMERGENCY bit set")
    return ", ".join(parts)


def assemble_calls(frames) -> list:
    """Frames in capture order -> call records. HDU opens a call, LDU1 fills in
    talkgroup/source, LDU2 fills in the crypto state, TDU/TDULC closes it. A
    stream that starts mid-call (no HDU) still yields a record — that is the
    normal field case."""
    calls, cur = [], None

    def close():
        nonlocal cur
        if cur is not None:
            cur["summary"] = _describe(cur)
            calls.append(cur)
            cur = None

    for f in frames:
        duid = f["duid"]
        if duid == DUID_TSDU:
            for t in f.get("tsbks", []):
                if t.get("tgid") is None:
                    continue
                rec = _blank_call(f)
                rec["tgid"] = t["tgid"]
                rec["source_id"] = t["source_id"]
                rec["emergency"] = bool(t.get("emergency"))
                rec["duids"] = ["TSDU"]
                rec["frames"] = 1
                rec["channel"] = t.get("channel")
                rec["opcode"] = t.get("opcode")
                rec["summary"] = _describe(rec)
                calls.append(rec)
            continue
        if duid == DUID_TDU:
            close()
            continue
        if duid == DUID_PDU:
            continue
        if cur is None or cur["nac"] != f["nac"] or duid == DUID_HDU:
            close()
            cur = _blank_call(f)
        _merge_frame(cur, f)
        if duid == DUID_TDULC:
            close()
    close()
    return calls


# ---------------------------------------------------------------- top level
def _empty_result(note: str) -> dict:
    return {"protocol": "P25", "messages": [], "calls": [], "frames": [],
            "note": note,
            "stats": {"sync": False, "sync_count": 0, "frames": 0,
                      "frames_fec_ok": 0, "nacs": [], "encrypted": None,
                      "status_symbols": None, "inverted": False}}


def _scan_frames(stream, locks, status, keep_voice, max_frames):
    """Walk the sync locks, decoding each frame and stepping past what it used."""
    frames, last_end = [], -1
    for pos in locks:
        if pos <= last_end:
            continue                        # inside a frame we already consumed
        f = decode_frame(stream, pos, status=status, keep_voice=keep_voice)
        if f is None:
            continue                        # sync locked but the NID is not P25
        frames.append(f)
        # Advance past the frame we just consumed, using the length the payload
        # decode actually accounted for (a TSDU's length depends on its block
        # count). Status symbols stretch the frame on the wire by 1 dibit in 36.
        used = SYNC_DIBITS + NID_DIBITS + f.get("payload_dibits", 0)
        if f["status_symbols"]:
            used += used // STATUS_DATA_DIBITS
        last_end = pos + used - 1
        if len(frames) >= max_frames:
            break
    return frames


def invert_dibits(dibits) -> np.ndarray:
    """C4FM polarity flip: the deviation rails negate, so +3<->-3 and +1<->-1,
    which is exactly the dibit MSB flipping. NOT a bit-stream complement — this
    mirrors the same operation survey/c4fm.py documents on its own output."""
    return (np.asarray(dibits, dtype=np.uint8) ^ 0b10).astype(np.uint8)


def decode_p25_dibits(dibits, max_bit_errs: int = SYNC_MAX_BIT_ERRS,
                      status=None, keep_voice: bool = True,
                      max_frames: int = 512, try_invert: bool = True) -> dict:
    """PRIMARY ENTRY POINT: a recovered dibit stream -> P25 call metadata.

    `dibits` is a numpy array or sequence of 0..3 values at 4800 sym/s — exactly
    what survey/c4fm.py produces. Returns
      {"protocol":"P25", "frames":[...], "calls":[...], "messages":[...],
       "note", "stats"}
    where `messages` mirrors `calls` for decode_router compatibility, and every
    call carries a plain-English `summary`.

    Honesty gate: a frame is emitted ONLY if (1) the 48-bit sync locked within
    `max_bit_errs` bits AND (2) its NID landed within the BCH code's correction
    radius. Payload fields appear only when their own RS/CRC verified. Noise, an
    analog-FM-derived stream, or a non-P25 digital mode returns an EMPTY result
    with a clear note — never a fabricated NAC or talkgroup."""
    d = np.asarray(dibits, dtype=np.uint8)
    if d.size == 0:
        return _empty_result("empty dibit stream")
    if int(d.max()) > 3:
        raise ValueError("dibit stream must contain only values 0..3")

    # Try both C4FM polarities: which rail is "positive" depends on the LO offset
    # of the capture, and a polarity-flipped stream matches nothing. Inverting
    # only doubles the (1e-9-scale) chance of a false sync, so this costs no
    # honesty — and every frame still has to clear the BCH gate.
    frames, inverted, n_locks = [], False, 0
    for inv in ((False, True) if try_invert else (False,)):
        stream = invert_dibits(d) if inv else d
        locks = find_sync(stream, max_bit_errs)
        n_locks = max(n_locks, len(locks))
        if not locks:
            continue
        cand = _scan_frames(stream, locks, status, keep_voice, max_frames)
        if len(cand) > len(frames):
            frames, inverted = cand, inv
        if frames and not inv:
            break                            # normal polarity already worked
    if not frames:
        if not n_locks:
            return _empty_result("no P25 frame sync found")
        return _empty_result("frame sync candidates found but no NID passed BCH "
                             "— not P25")

    calls = assemble_calls(frames)
    nacs = sorted({f["nac"] for f in frames})
    fec_ok = sum(1 for f in frames if f.get("fec_ok"))
    enc = None
    for c in calls:                          # any encrypted call dominates the verdict
        if c["encrypted"] is True:
            enc = True
            break
        if c["encrypted"] is False:
            enc = False
    stats = {"sync": True, "sync_count": n_locks, "frames": len(frames),
             "frames_fec_ok": fec_ok, "nacs": [f"0x{n:03X}" for n in nacs],
             "encrypted": enc, "inverted": inverted,
             "status_symbols": frames[0]["status_symbols"],
             "duids": sorted({f["duid_name"] for f in frames})}
    if calls:
        note = "; ".join(c["summary"] for c in calls[:4])
    else:
        note = ("P25 sync + NID locked (NAC {}) but no Link Control or Encryption "
                "Sync passed FEC — system identified, call detail not recovered"
                .format(", ".join(stats["nacs"])))
    return {"protocol": "P25", "frames": frames, "calls": calls,
            "messages": calls, "note": note, "stats": stats}


def _c4fm_dibits(iq, fs: float):
    """Lazy bridge to survey/c4fm.py (owned by another agent), mirroring the
    decode_router._decoders() lazy-import pattern so this module imports and
    tests cleanly before c4fm exists.

    Adapter, not a contract: c4fm.demod_c4fm(iq, fs) returns a result dict whose
    'dibits' is the symbol stream and 'quality' the sync-quality metric. We also
    accept a bare array or an (array, quality) tuple so a signature change on the
    other side degrades to an honest note instead of a traceback. Returns
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
            res = fn(iq, fs)
        except ValueError:
            raise                            # too-short capture propagates
        except Exception as exc:                                   # noqa: BLE001
            return None, None, f"c4fm.{name}() failed: {exc}"
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
            return None, None, f"c4fm.{name}() returned {why}"
        return arr, quality, None
    return None, None, "survey/c4fm.py exposes no recognised dibit entry point"


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
        return None, f"a non-numeric array (dtype {arr.dtype})"
    if arr.ndim != 1:
        return None, f"a {arr.ndim}-D array, expected a 1-D dibit stream"
    if arr.size == 0:
        return None, "an empty dibit stream"
    if not np.all(np.isfinite(arr)):
        return None, "a dibit stream containing non-finite values"
    if np.any(arr != np.floor(arr)):
        return None, "non-integer symbol values"
    lo, hi = int(arr.min()), int(arr.max())
    if lo < 0 or hi > 3:
        return None, f"symbol values outside the dibit range 0..3 ({lo}..{hi})"
    return arr.astype(np.uint8), None


def decode_p25(iq, fs: float, **kwargs) -> dict:
    """IQ entry point, matching decode_pocsag / decode_flex.

    Delegates the C4FM symbol recovery to survey/c4fm.py (lazy import) and then
    runs the frame layer. If c4fm is missing this returns an EMPTY result with
    the reason in `note` — it does not raise and it does not pretend. Only
    too-short input raises ValueError, mirroring demod.demodulate()."""
    iq = np.asarray(iq, dtype=np.complex64)
    if iq.size < 16:
        raise ValueError("capture too short to decode")
    dibits, quality, err = _c4fm_dibits(iq, fs)
    if dibits is None:
        return _empty_result(err or "C4FM symbol recovery unavailable")
    result = decode_p25_dibits(dibits, **kwargs)
    result["stats"]["c4fm_quality"] = quality
    return result


def looks_like_p25(feats: dict) -> bool:
    """Cheap pre-gate from identify.iq_features(): P25 Phase 1 is C4FM — 4-level
    FSK, 4800 symbols/s, +/-1.8 kHz outer deviation (3.6 kHz peak-to-peak shift),
    in a 12.5 kHz channel, and continuous while someone is keyed up.

    Feature gates only pick which decoder to TRY — the sync + BCH gates inside
    decode_p25_dibits decide what is reported.

    MEASURED, 2026-08-03: iq_features CANNOT see C4FM as FSK. C4FM is
    continuous-phase and RRC pulse-shaped, so the instantaneous-frequency
    histogram is smeared rather than four discrete rails, and the extractor
    returns modulation_class='analog_voice', fsk_states=0, fsk_shift_khz=0.0 on a
    known-good synthetic P25 call at every sample rate tried (48k/96k/240k).
    Requiring those, as this gate originally did, meant P25 could NEVER be tried
    — decode_p25 worked perfectly on the same signal the gate rejected.

    So FSK evidence is now treated as CONFIRMING, never as required. The gate
    keeps only what actually holds for C4FM: a real signal, in a channel-width
    band. That costs a C4FM demod on some signals that turn out not to be P25,
    which is the right trade — decode_p25_dibits' own sync + BCH gates are far
    stronger than any feature test (measured: 3 million random dibits produce
    ZERO frames), so a loose pre-gate cannot manufacture a false call.
    """
    if not feats:
        return False
    # Explicit False rejects (iq_features sets it, and an empty channel must not
    # reach the decoder); a MISSING key is "not stated", not "no signal" — a
    # caller hand-building a feature dict should not be silently vetoed.
    if feats.get("signal_present") is False:
        return False
    bw = feats.get("occupied_bw_khz", 0.0) or 0.0
    # 12.5 kHz channel; allow for a mis-tuned capture and for the wider
    # measurement a noisy environment gives. An unmodulated carrier measures far
    # below this, and a wideband/OFDM signal far above, so both still fall out.
    if not (2.0 <= bw <= 25.0):
        return False
    rate = feats.get("symbol_rate_kbd", 0.0) or 0.0
    # 0.0 means "could not measure", which is the NORMAL reading for C4FM — treat
    # it as no information rather than as a mismatch.
    if rate and abs(rate - 4.8) > 0.35 * 4.8:
        return False
    return True


# ---------------------------------------------------------------- synth / encoder
def encode_frame_dibits(duid: int, nac: int, *, lc_bits=None, es_bits=None,
                        hdu_bits=None, tsbks=None, voice=None,
                        lsd_bits=None, status: bool = False) -> np.ndarray:
    """Synthesize ONE P25 frame as dibits (test/self-check path).

    Shares every FEC routine with the decoder — Golay/Hamming codebooks, the RS
    generator and the BCH generator — so a wrong polynomial, a wrong field
    ordering or a wrong shortening fails loudly in tests/test_p25.py rather than
    round-tripping through matched bugs at the top level only.

    `voice` frames are given in VECTOR order (c_0..c_7, MSB-first) and are
    interleaved onto the wire here, so the dibits this returns are laid out the
    way a real transmitter lays them out. Note that sharing the permutation with
    the decoder is exactly the kind of symmetry that proves nothing on its own —
    the interleave is pinned against the standard's table in tests/test_p25.py,
    not against this function."""
    bits = _int_to_bits(FRAME_SYNC, FRAME_SYNC_BITS)
    bits += _int_to_bits(nid_encode(nac, duid), NID_BITS)

    if duid == DUID_HDU:
        info = list(hdu_bits or [0] * 120)[:120]
        info += [0] * (120 - len(info))
        bits += _rs_block_encode(info, golay18_encode, 18, 16)
    elif duid in VOICE_DUIDS:
        if duid == DUID_LDU1:
            info = list(lc_bits or [0] * 72)[:72]
            info += [0] * (72 - len(info))
            ctrl = _rs_block_encode(info, hamming10_encode, 10, 12)
        else:
            info = list(es_bits or [0] * 96)[:96]
            info += [0] * (96 - len(info))
            ctrl = _rs_block_encode(info, hamming10_encode, 10, 8)
        vframes = list(voice or [])
        while len(vframes) < VOICE_FRAMES_PER_LDU:
            vframes.append([0] * VOICE_FRAME_BITS)
        lsd = list(lsd_bits or [0] * LSD_BITS)[:LSD_BITS]
        lsd += [0] * (LSD_BITS - len(lsd))
        vi = ci = 0
        for kind, nbits in LDU_LAYOUT:
            if kind == "V":
                # `voice` is supplied in VECTOR order (c_0..c_7); the transmitter
                # interleaves it onto the air per TIA-102.BAAA-A Table 5-1.
                chunk = list(vframes[vi])[:nbits]
                chunk += [0] * (nbits - len(chunk))
                bits += [int(b) for b in interleave_voice_frame(chunk)]
                vi += 1
            elif kind == "C":
                bits += ctrl[ci:ci + nbits]
                ci += nbits
            else:
                bits += lsd
    elif duid == DUID_TDULC:
        info = list(lc_bits or [0] * 72)[:72]
        info += [0] * (72 - len(info))
        bits += _rs_block_encode(info, golay18_encode, 18, 12)
    elif duid == DUID_TSDU:
        for blk in (tsbks or []):
            payload = list(blk)[:80]
            payload += [0] * (80 - len(payload))
            full = payload + _int_to_bits(crc16_p25(payload), 16)
            # 48 data dibits + one flush dibit -> 98 coded dibits (196 bits)
            src = np.concatenate([bits_to_dibits(full), np.zeros(1, dtype=np.uint8)])
            bits += [int(b) for b in dibits_to_bits(trellis_half_encode(src))]
    # TDU/PDU: sync + NID only.

    d = bits_to_dibits(bits)
    if not status:
        return d
    # Re-insert the channel-status dibits the transmitter adds every 35 dibits.
    out = []
    for i, v in enumerate(d):
        out.append(int(v))
        if (i + 1) % STATUS_DATA_DIBITS == 0:
            out.append(1)                    # status dibit value is not interpreted
    return np.array(out, dtype=np.uint8)


def make_link_control(lco: int = LCO_GROUP_VOICE, mfid: int = 0, tgid: int = 0,
                      source_id: int = 0, target_id: int = 0,
                      protected_flag: bool = False, emergency: bool = False,
                      priority: int = 0) -> list:
    """Build a 72-bit Link Control Word (inverse of parse_link_control)."""
    o = [0] * 9
    o[0] = (0x80 if protected_flag else 0) | (lco & 0x3F)
    o[1] = mfid & 0xFF
    svc = (0x80 if emergency else 0) | (priority & 0x07)
    if lco == LCO_GROUP_VOICE:
        o[2] = svc
        o[4] = (tgid >> 8) & 0xFF
        o[5] = tgid & 0xFF
        o[6], o[7], o[8] = (source_id >> 16) & 0xFF, (source_id >> 8) & 0xFF, source_id & 0xFF
    elif lco == LCO_UNIT_VOICE:
        o[2] = svc
        o[3], o[4], o[5] = (target_id >> 16) & 0xFF, (target_id >> 8) & 0xFF, target_id & 0xFF
        o[6], o[7], o[8] = (source_id >> 16) & 0xFF, (source_id >> 8) & 0xFF, source_id & 0xFF
    elif lco == LCO_CALL_TERM:
        o[6], o[7], o[8] = (source_id >> 16) & 0xFF, (source_id >> 8) & 0xFF, source_id & 0xFF
    bits = []
    for v in o:
        bits += _int_to_bits(v, 8)
    return bits


def make_encryption_sync(algid: int = ALGID_CLEAR, kid: int = 0, mi: bytes = b"") -> list:
    """Build a 96-bit Encryption Sync Word (inverse of parse_encryption_sync)."""
    mi = (bytes(mi) + b"\x00" * 9)[:9]
    bits = []
    for v in mi:
        bits += _int_to_bits(v, 8)
    bits += _int_to_bits(algid & 0xFF, 8)
    bits += _int_to_bits((kid >> 8) & 0xFF, 8)
    bits += _int_to_bits(kid & 0xFF, 8)
    return bits


def make_hdu_header(algid: int = ALGID_CLEAR, kid: int = 0, tgid: int = 0,
                    mfid: int = 0, mi: bytes = b"") -> list:
    """Build a 120-bit HDU header (inverse of parse_hdu_header)."""
    mi = (bytes(mi) + b"\x00" * 9)[:9]
    o = list(mi) + [mfid & 0xFF, algid & 0xFF, (kid >> 8) & 0xFF, kid & 0xFF,
                    (tgid >> 8) & 0xFF, tgid & 0xFF]
    bits = []
    for v in o:
        bits += _int_to_bits(v, 8)
    return bits


def make_tsbk(opcode: int = TSBK_GRP_VCH_GRANT, mfid: int = 0, tgid: int = 0,
              source_id: int = 0, channel: int = 0, last_block: bool = True,
              emergency: bool = False) -> list:
    """Build the 80 pre-CRC bits of a TSBK (encode_frame_dibits appends the CRC)."""
    o = [0] * 10
    o[0] = (0x80 if last_block else 0) | (opcode & 0x3F)
    o[1] = mfid & 0xFF
    if opcode in (TSBK_GRP_VCH_GRANT, TSBK_GRP_VCH_GRANT_UPD):
        o[2] = 0x80 if emergency else 0
        o[3], o[4] = (channel >> 8) & 0xFF, channel & 0xFF
        o[5], o[6] = (tgid >> 8) & 0xFF, tgid & 0xFF
        o[7], o[8], o[9] = (source_id >> 16) & 0xFF, (source_id >> 8) & 0xFF, source_id & 0xFF
    bits = []
    for v in o:
        bits += _int_to_bits(v, 8)
    return bits


def encode_call(nac: int, tgid: int, source_id: int, *, algid: int = ALGID_CLEAR,
                kid: int = 0, mi: bytes = b"", status: bool = False,
                terminator: int = DUID_TDULC, ldu_pairs: int = 1,
                voice_fill: int = 0) -> np.ndarray:
    """Synthesize a complete P25 call as dibits: HDU, LDU1/LDU2 pairs, terminator.

    This is what tests/test_p25.py round-trips: known NAC / talkgroup / source /
    ALGID in, exact recovery out — including the encrypted case, which must come
    back as an encrypted RESULT and not as a decode failure."""
    parts = [encode_frame_dibits(
        DUID_HDU, nac, status=status,
        hdu_bits=make_hdu_header(algid=algid, kid=kid, tgid=tgid, mi=mi))]
    lc = make_link_control(tgid=tgid, source_id=source_id,
                           protected_flag=algid != ALGID_CLEAR)
    es = make_encryption_sync(algid=algid, kid=kid, mi=mi)
    voice = [[(voice_fill >> (k % 8)) & 1 for k in range(VOICE_FRAME_BITS)]
             for _ in range(VOICE_FRAMES_PER_LDU)]
    for _ in range(max(1, ldu_pairs)):
        parts.append(encode_frame_dibits(DUID_LDU1, nac, lc_bits=lc, voice=voice,
                                         status=status))
        parts.append(encode_frame_dibits(DUID_LDU2, nac, es_bits=es, voice=voice,
                                         status=status))
    if terminator == DUID_TDULC:
        parts.append(encode_frame_dibits(DUID_TDULC, nac, lc_bits=lc, status=status))
    elif terminator is not None:
        parts.append(encode_frame_dibits(DUID_TDU, nac, status=status))
    return np.concatenate(parts).astype(np.uint8)
