#!/usr/bin/env python3
"""iq_replay.py — push a recorded SigMF file back through the decode chain.

THE JOB, STATED EXACTLY
survey/ambe.py names two assumptions that synthetic tests cannot touch:

  1. the physical bit ORDER inside the 88-bit IMBE information vector — the field
     WIDTHS and the quantizer arithmetic are sourced, the bit POSITIONS are this
     codebase's own convention;
  2. transport de-interleaving, which is absent.

Neither can be settled by more synthetic vectors: a synthetic frame is built by
the same convention it is then checked against, so it always agrees with itself.
The only evidence that counts is a REAL off-air frame. This tool is the
instrument for reading that evidence — it takes one recording and reports, in
detail, how far up the FEC ladder each frame climbed.

WHY FEC IS THE MEASUREMENT
Every stage of the P25/DMR ladder is an independent arithmetic check on a
hypothesis about bit layout. Random bits do not satisfy a Reed-Solomon syndrome
or a BPTC product code by accident. So a stage that passes CONSISTENTLY across
many frames of real traffic is evidence that everything below it — sync
alignment, status-symbol stripping, frame geometry, vector partition, polynomial
choice, scrambler seeding — is right. A stage that fails consistently while the
one below it passes localises the wrong assumption to exactly that stage. That
is why the FEC ladder is the headline output here and the decoded text is not.

    py tools/iq_replay.py --file recordings/iq/xxx.sigmf-data --decoder p25
    py tools/iq_replay.py --file xxx --decoder auto --offset-seconds 4 --seconds 3
    py tools/iq_replay.py --file xxx --decoder p25 --invert-polarity both
    py tools/iq_replay.py --file xxx --decoder dmr --symbol-rate-search auto

The sweep flags exist so a wrong assumption can be BISECTED offline. Recording
again to test a hypothesis is both slow and unsound — the air is different on the
second pass, so an improvement could be the new hypothesis or could be a stronger
burst, and you cannot tell which. Sweeping against a fixed file is a controlled
experiment; re-recording is not.

WHAT THIS TOOL WILL NOT DO
It will not call a decode successful because a message came out. Every number it
prints is a count of checks that passed, with the denominator next to it.
"""

import argparse
import os
import sys

import numpy as np

try:
    sys.stdout.reconfigure(encoding="utf-8")   # Windows console is cp1252 by default
except Exception:                                                  # noqa: BLE001
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from survey import ambe, c4fm, dmr, identify, p25                  # noqa: E402
from survey.decode_router import interpret, route_decode           # noqa: E402
from survey.iqfile import IQFileError, SigMFReader                 # noqa: E402

DECODERS = ("auto", "p25", "dmr", "video", "pocsag", "flex", "ook")
DEFAULT_SECONDS = 2.0
# 2 s at 4800 Bd is ~9600 symbols — around 11 P25 frames or 30 DMR bursts, which
# is enough for per-stage counts to mean something and small enough to iterate on.
BIG_SLICE_SAMPLES = 40_000_000     # ~160 MB as complex64; warn past this


# ---------------------------------------------------------------- input
def load_slice(path: str, offset_s: float, seconds: float, strict: bool):
    """Bounded read of the recording. Returns (iq, reader.describe())."""
    with SigMFReader(path, strict=strict) as r:
        fs = r.sample_rate
        start = int(round(max(0.0, offset_s) * fs))
        count = r.sample_count - start if seconds is None else int(round(seconds * fs))
        if count > BIG_SLICE_SAMPLES:
            print(f"  NOTE: reading {count:,} samples "
                  f"({count * 8 / 1e6:,.0f} MB) into memory — narrow it with "
                  "--seconds if this is more than the machine likes.")
        iq = r.read(count, start)
        info = r.describe()
    return iq, info


def print_recording(info: dict, iq, offset_s: float):
    print("=" * 78)
    print("SHIELD TSCM — IQ REPLAY")
    print("=" * 78)
    print(f"  file          : {info['data_path']}")
    print(f"  recorded      : {info['utc_start']}  ({info['source'] or '?'}"
          f"{', ' + info['hardware'] if info['hardware'] else ''})")
    print(f"  center        : {info['center_hz'] / 1e6:.6f} MHz")
    print(f"  sample rate   : {info['sample_rate']:,.0f} S/s   "
          f"({info['datatype']})")
    print(f"  gain at record: {info['gain_db']} dB ({info['gain_mode'] or '?'})")
    if info["note"]:
        print(f"  operator note : {info['note']}")
    print(f"  whole file    : {info['samples']:,} samples "
          f"({info['seconds']:.3f} s)")
    print(f"  this slice    : {iq.size:,} samples from t={offset_s:g} s "
          f"({iq.size / float(info['sample_rate'] or 1):.3f} s)")
    ex = info.get("extra") or {}
    if ex.get("clipped_samples"):
        print(f"  RECORD-TIME CLIPPING: {ex['clipped_samples']:,} samples "
              f"({ex.get('clipped_fraction', 0) * 100:.3f} %) — a decode failure "
              "against this file may be the capture, not the decoder")
    for prob in info.get("problems") or []:
        print(f"  !! {prob}")
    print()


# ---------------------------------------------------------------- front end
def c4fm_trial(iq, fs, invert: bool, symbol_rate: float) -> dict:
    """One C4FM demod attempt. Never raises on a bad slice — an unusable capture
    comes back with quality 0.0 and a note, exactly as c4fm.py promises."""
    try:
        return c4fm.demod_c4fm(iq, fs, symbol_rate=symbol_rate, invert=invert)
    except ValueError as e:
        return {"dibits": np.zeros(0, dtype=np.uint8), "quality": 0.0,
                "fit": 0.0, "coverage": 0.0, "rails_used": 0, "evm": 1.0,
                "n_symbols": 0, "sps": 0.0, "note": str(e)}


def print_c4fm(res: dict, label: str = ""):
    print(f"  C4FM SYMBOL RECOVERY{(' — ' + label) if label else ''}")
    print(f"    symbols recovered : {res.get('n_symbols', 0):,} "
          f"@ {res.get('sps', 0):.2f} samples/symbol")
    print(f"    sync quality      : {res.get('quality', 0.0):.3f}   "
          f"(fit {res.get('fit', 0.0):.3f} x rail coverage "
          f"{res.get('coverage', 0.0):.3f})")
    print(f"    rails occupied    : {res.get('rails_used', 0)} of 4     "
          f"EVM {res.get('evm', 1.0):.3f}   DC bias {res.get('dc', 0.0):+.3f}")
    if res.get("note"):
        print(f"    note              : {res['note']}")
    # Said here rather than at the bottom, because everything downstream is
    # conditional on it: with three rails or fewer this is not C4FM, and any
    # frame counts below are noise passing a probabilistic gate.
    if res.get("rails_used", 0) < 4:
        print("    ^^ fewer than 4 rails means this is not a 4-level C4FM eye. "
              "Treat every frame count below as suspect.")


# ---------------------------------------------------------------- P25 ladder
def p25_ladder(dibits, max_bit_errs: int = p25.SYNC_MAX_BIT_ERRS) -> dict:
    """Walk the dibit stream and account for EVERY sync lock, stage by stage.

    decode_p25_dibits() already returns the verdict; this walks the same ground
    to get the DENOMINATORS, which is what a validation run needs. Both dibit
    polarities are tried and the better one kept, mirroring the decoder — a
    polarity-flipped stream matches nothing at all, so reporting only the normal
    polarity would look like a total failure for a trivially fixable reason.
    """
    best = None
    for inv in (False, True):
        stream = p25.invert_dibits(dibits) if inv else np.asarray(dibits, np.uint8)
        locks = list(p25.find_sync(stream, max_bit_errs))
        r = {"inverted": inv, "sync_locks": len(locks), "nid_pass": 0,
             "nid_fail": 0, "nid_bit_errors": [], "duids": {}, "nacs": {},
             "rs_pass": 0, "rs_fail": 0, "rs_not_assessed": 0,
             "inner_bit_corrections": 0, "rs_symbol_errors": [],
             "trellis_blocks": 0, "voice_frames": 0, "frames": []}
        last_end = -1
        for pos in locks:
            if pos <= last_end:
                continue                     # inside a frame already consumed
            f = p25.decode_frame(stream, pos, status=None, keep_voice=True)
            if f is None:
                r["nid_fail"] += 1           # sync locked, BCH(63,16) rejected it
                continue
            r["nid_pass"] += 1
            r["nid_bit_errors"].append(int(f["nid_bit_errors"]))
            r["duids"][f["duid_name"]] = r["duids"].get(f["duid_name"], 0) + 1
            r["nacs"][f["nac_hex"]] = r["nacs"].get(f["nac_hex"], 0) + 1
            fec = f.get("fec") or {}
            if "rs_ok" in fec:
                if fec["rs_ok"]:
                    r["rs_pass"] += 1
                    r["rs_symbol_errors"].append(int(fec.get("rs_symbol_errors", 0)))
                else:
                    r["rs_fail"] += 1
                r["inner_bit_corrections"] += int(fec.get("block_bit_errors", 0))
            elif f.get("tsbks") is not None:
                r["trellis_blocks"] += len(f["tsbks"])
            else:
                r["rs_not_assessed"] += 1
            r["voice_frames"] += int(f.get("voice_frame_count", 0))
            r["frames"].append(f)
            used = p25.SYNC_DIBITS + p25.NID_DIBITS + f.get("payload_dibits", 0)
            if f["status_symbols"]:
                used += used // p25.STATUS_DATA_DIBITS
            last_end = pos + used - 1
        # Score by frames that got a NID through, then by RS passes: those are
        # the two checks a noise lock cannot fake.
        score = (r["nid_pass"], r["rs_pass"])
        if best is None or score > (best["nid_pass"], best["rs_pass"]):
            best = r
    return best


def p25_voice_fec(frames) -> dict:
    """Run the IMBE FEC over every voice frame the P25 layer handed up.

    This is the stage the operator actually came for. p25.py stops at the coded
    144-bit frames on purpose ("these are FEC-protected IMBE frames, not audio");
    ambe.unpack_imbe_frame() is the next rung, and it is the LAST one that is
    pure arithmetic — Golay(23,12) x4, Hamming(15,11) x3, and the Lehmer
    scrambler seeded from u_0.

    READ THE SCOPE CAREFULLY. A high clean rate here validates the frame
    geometry, the vector partition of the 144 bits, the polynomials and the
    scrambler seeding, because a wrong u_0 descrambles vectors 1..6 to noise and
    the Golay decoders burn their whole budget. It does NOT by itself validate
    the SEMANTIC placement of fields inside the resulting 88 information bits —
    that sits below the FEC and is the assumption ambe.py flags. See
    pitch_plausibility() for the cheapest evidence available on that.
    """
    total = clean = 0
    errors, reports = [], []
    for f in frames:
        for bits in (f.get("voice_bits") or []):
            b = np.asarray(bits, dtype=np.uint8).ravel()
            if b.size != ambe.IMBE_FRAME_BITS:
                continue
            rep = ambe.unpack_imbe_frame(b)
            total += 1
            errors.append(int(rep["errors"]))
            if rep["fec_ok"]:
                clean += 1
                reports.append(rep)
    return {"total": total, "clean": clean, "errors": errors, "reports": reports}


def pitch_plausibility(reports) -> dict:
    """Evidence about the 88-bit FIELD ORDER, from FEC-clean frames only.

    The reasoning: if bits[0:8] really is the IMBE pitch index, then across
    consecutive 20 ms frames of a real human talking it must behave like pitch —
    mostly inside the legal 0..207 range, and changing SLOWLY, because a voice
    does not jump an octave every 20 ms. If our field placement is wrong, those
    eight bits are a slice of something else (amplitude bits, most likely) and
    will look like uniform noise: ~19% of values landing in the reserved band
    (208..255 is 48 of 256) and a large mean frame-to-frame step (~85 for
    uniform bytes).

    This is INDICATIVE, not decisive. Silence and encrypted traffic both produce
    noise-like values legitimately, and a short capture has no statistical power.
    It is offered because it is free once the frames are FEC-clean, and because
    it is the only cheap discriminator that exists before someone builds a
    reference decode to compare against.
    """
    if not reports:
        return {"n": 0}
    b0 = [int(ambe.bits_to_int(r["info_bits"][:ambe.IMBE_PITCH_BITS]))
          for r in reports]
    reserved = sum(1 for v in b0 if v >= ambe.IMBE_PITCH_RESERVED)
    steps = [abs(b - a) for a, b in zip(b0, b0[1:])]
    return {
        "n": len(b0),
        "reserved": reserved,
        "reserved_frac": reserved / float(len(b0)),
        "median": float(np.median(b0)),
        "min": min(b0), "max": max(b0),
        "mean_step": float(np.mean(steps)) if steps else 0.0,
        # what the same numbers would look like if these 8 bits were noise
        "uniform_reserved_frac": 48.0 / 256.0,
        "uniform_mean_step": 256.0 / 3.0,
    }


def print_p25(iq, fs, res: dict, verbose: bool):
    dibits = np.asarray(res.get("dibits"), dtype=np.uint8)
    print("  P25 PHASE 1 FRAME LADDER")
    if dibits.size == 0:
        print("    no symbols to work with — nothing above this line locked.")
        return {}
    lad = p25_ladder(dibits)
    voice = p25_voice_fec(lad["frames"])
    pitch = pitch_plausibility(voice["reports"])

    nid_seen = lad["nid_pass"] + lad["nid_fail"]
    rs_seen = lad["rs_pass"] + lad["rs_fail"]
    mean_nid = (float(np.mean(lad["nid_bit_errors"]))
                if lad["nid_bit_errors"] else 0.0)
    print(f"    dibit polarity    : {'INVERTED' if lad['inverted'] else 'normal'}"
          "   (both tried; the better one is shown)")
    print(f"    1. frame sync 48b : {lad['sync_locks']:>6} locks "
          f"(<= {p25.SYNC_MAX_BIT_ERRS} bit errors allowed)")
    print(f"    2. NID BCH(63,16) : {lad['nid_pass']:>6} / {nid_seen} locks passed"
          f"   mean {mean_nid:.2f} bit errors corrected")
    print(f"    3. inner Golay18/ : {lad['inner_bit_corrections']:>6} bit "
          f"corrections over {rs_seen} FEC-bearing frames")
    print( "       Hamming10")
    print(f"    4. Reed-Solomon   : {lad['rs_pass']:>6} / {rs_seen} frames verified"
          + (f"   mean {np.mean(lad['rs_symbol_errors']):.2f} symbol errors"
             if lad["rs_symbol_errors"] else ""))
    if lad["trellis_blocks"]:
        print(f"    4b. trellis+CRC16 : {lad['trellis_blocks']:>6} TSBK blocks "
              "passed CRC (control channel)")
    if voice["total"]:
        pct = 100.0 * voice["clean"] / voice["total"]
        mean_e = float(np.mean(voice["errors"])) if voice["errors"] else 0.0
        print(f"    5. IMBE Golay23/  : {voice['clean']:>6} / {voice['total']} "
              f"voice frames FEC-CLEAN ({pct:.1f} %)")
        print(f"       Hamming15         mean {mean_e:.2f} bit corrections/frame "
              f"(mute gate at {ambe.IMBE_ERR_LIMIT})")
    else:
        print("    5. IMBE           :      0 voice frames reached the vocoder "
              "layer (no LDU1/LDU2 in this slice)")

    print()
    print(f"    NAC(s)            : "
          f"{', '.join(f'{k} x{v}' for k, v in sorted(lad['nacs'].items())) or '-'}")
    print(f"    DUID(s)           : "
          f"{', '.join(f'{k} x{v}' for k, v in sorted(lad['duids'].items())) or '-'}")

    if pitch.get("n"):
        print()
        print("    FIELD-ORDER EVIDENCE (88-bit IMBE vector — the open question)")
        print(f"      pitch index b0 over {pitch['n']} clean frames: "
              f"median {pitch['median']:.0f}, range {pitch['min']}..{pitch['max']}")
        print(f"      reserved (>= {ambe.IMBE_PITCH_RESERVED}): "
              f"{pitch['reserved']} frames = {pitch['reserved_frac'] * 100:.1f} %"
              f"   [random bits would give ~{pitch['uniform_reserved_frac'] * 100:.0f} %]")
        print(f"      mean frame-to-frame step: {pitch['mean_step']:.1f}"
              f"   [random bits would give ~{pitch['uniform_mean_step']:.0f}]")
        print("      Low reserved fraction AND a small step = these 8 bits behave "
              "like pitch, which supports the field order.")
        print("      Both near the random figures = either the field order is "
              "wrong, or the call is silent/encrypted. Indicative only.")

    # The decoder's own verdict, so the operator sees what the app would say.
    out = p25.decode_p25_dibits(dibits)
    print()
    print(f"    decoder verdict   : {out['note']}")
    if out["stats"].get("encrypted"):
        print("      (encrypted payload — FEC still validates the transport, "
              "which is exactly what this exercise measures)")
    if verbose:
        for f in lad["frames"][:20]:
            print(f"      @{f['offset_dibit']:>7} {f['duid_name']:<22} "
                  f"NAC {f['nac_hex']} nid_err={f['nid_bit_errors']} "
                  f"fec_ok={f.get('fec_ok')} {f.get('fec') or ''}")
    return {"sync_locks": lad["sync_locks"], "nid_pass": lad["nid_pass"],
            "rs_pass": lad["rs_pass"], "rs_seen": rs_seen,
            "voice_clean": voice["clean"], "voice_total": voice["total"],
            "quality": res.get("quality", 0.0)}


# ---------------------------------------------------------------- DMR ladder
# Voice-burst AMBE extraction. dmr.py deliberately LOCATES the AMBE frames and
# does not decode them, so this offset convention belongs to the replay harness,
# not to dmr.py: a DMR voice burst is [info 108][sync-or-EMB 48][info 108], and
# the three 72-bit AMBE frames run straight across the two info halves, so the
# middle 48 bits are simply removed and the remaining 216 split into three. If
# the AMBE FEC fails here while the DMR transport FEC below passes cleanly, THIS
# convention is a candidate for the fault as much as the vocoder is.
def dmr_voice_bits(burst_bits):
    b = np.asarray(burst_bits, dtype=np.uint8).ravel()
    if b.size != dmr.BURST_BITS:
        return []
    payload = np.concatenate([b[:dmr.SYNC_OFFSET_BITS],
                              b[dmr.SYNC_OFFSET_BITS + dmr.SYNC_BITS:]])
    n = ambe.AMBE_FRAME_BITS
    return [payload[i * n:(i + 1) * n] for i in range(payload.size // n)]


def dmr_voice_fec(dibits, bursts, inverted: bool) -> dict:
    """AMBE+2 FEC over the voice bursts. Same scope caveat as the P25 side:
    ambe.py implements the FEC and NOT the 2450 parameter quantizer, so a clean
    frame here means "these really are AMBE voice frames", not "audio"."""
    stream = dmr.invert_dibits(dibits) if inverted else np.asarray(dibits, np.uint8)
    total = clean = 0
    errors = []
    for b in bursts:
        if b["kind"] not in ("voice", "voice_emb"):
            continue
        bits = dmr.burst_bits(stream, b["index"])
        if bits is None:
            continue
        for frame in dmr_voice_bits(bits):
            rep = ambe.unpack_ambe_frame(frame)
            total += 1
            errors.append(int(rep["errors"]))
            clean += 1 if rep["fec_ok"] else 0
    return {"total": total, "clean": clean, "errors": errors}


def print_dmr(iq, fs, res: dict, verbose: bool):
    dibits = np.asarray(res.get("dibits"), dtype=np.uint8)
    print("  DMR BURST LADDER")
    if dibits.size < dmr.BURST_DIBITS:
        print("    no symbols to work with — nothing above this line locked.")
        return {}
    out = dmr.decode_dmr(iq, fs, symbols=res)
    st = out["stats"]
    bursts = out.get("bursts") or []
    voice = dmr_voice_fec(dibits, bursts, bool(st.get("inverted")))

    pol_note = ("resolved by FEC" if st.get("polarity_resolved") else
                "UNRESOLVED — no FEC block distinguished the two, so the "
                "voice/data split below is provisional")
    print(f"    receive polarity  : "
          f"{'INVERTED' if st.get('inverted') else 'normal'}   ({pol_note})")
    print(f"    1. sync 48b       : {st['syncs']:>6} matches "
          f"(<= {dmr.SYNC_MAX_ERRS} bit errors allowed)")
    print(f"    2. Golay(20,8)    : {st['slot_type_ok']:>6} / {st['data_bursts']} "
          "data bursts decoded a slot type")
    print( "       slot type          (weak on its own — 33 % of random 20-bit "
           "words decode; the BPTC below is the real gate)")
    bptc_seen = st["bptc_ok"] + st["bptc_failed"]
    print(f"    3. BPTC(196,96)   : {st['bptc_ok']:>6} / {bptc_seen} product "
          "codes verified")
    print(f"    4. EMB (16,7,6)   : {st['emb_ok']:>6} embedded signalling words "
          "validated + colour-code corroborated")
    if voice["total"]:
        pct = 100.0 * voice["clean"] / voice["total"]
        mean_e = float(np.mean(voice["errors"])) if voice["errors"] else 0.0
        print(f"    5. AMBE+2 Golay24/: {voice['clean']:>6} / {voice['total']} "
              f"voice frames FEC-CLEAN ({pct:.1f} %)")
        print(f"       Golay23           mean {mean_e:.2f} bit corrections/frame "
              f"(mute gate at {ambe.AMBE_ERR_LIMIT})")
        print("       ^ harness-side extraction (see dmr_voice_bits) — a failure "
              "here can be the offsets, not the vocoder")
    else:
        print("    5. AMBE+2         :      0 voice bursts in this slice")

    print()
    print(f"    colour code(s)    : "
          f"{', '.join(str(c) for c in st['colour_codes']) or '-'}")
    dts = {}
    for b in bursts:
        if b.get("data_type_name"):
            dts[b["data_type_name"]] = dts.get(b["data_type_name"], 0) + 1
    print(f"    data type(s)      : "
          f"{', '.join(f'{k} x{v}' for k, v in sorted(dts.items())) or '-'}")
    print(f"    bursts            : {st['bursts']} "
          f"({st['voice_bursts']} voice, {st['data_bursts']} data)")
    print()
    print(f"    decoder verdict   : {out['note']}")
    for m in (out.get("messages") or [])[:6]:
        print(f"      - {m.get('text')}")
    if verbose:
        for b in bursts[:20]:
            print(f"      @{b['index']:>7} {b['kind']:<10} sync={b['sync']} "
                  f"cc={b['colour_code']} dt={b['data_type_name']} "
                  f"bptc_ok={b['bptc_ok']}")
    return {"sync_locks": st["syncs"], "bptc_ok": st["bptc_ok"],
            "bptc_seen": bptc_seen, "emb_ok": st["emb_ok"],
            "voice_clean": voice["clean"], "voice_total": voice["total"],
            "quality": res.get("quality", 0.0)}


# ---------------------------------------------------------------- auto path
def run_auto(iq, fs, feats, verbose: bool):
    print("  SIGNAL FEATURES (identify.iq_features)")
    for k in ("signal_present", "snr_db", "modulation_class", "occupied_bw_khz",
              "symbol_rate_kbd", "fsk_states", "fsk_shift_khz", "duty_cycle",
              "continuity"):
        print(f"    {k:<18}: {feats.get(k)}")
    print()
    res = route_decode(iq, fs, feats)
    verdict = interpret(feats, res.get("protocol"), bool(res.get("encrypted")))
    print("  ROUTER")
    print(f"    tried             : {', '.join(res.get('tried') or []) or '-'}")
    print(f"    protocol          : {res.get('protocol') or 'none'}")
    print(f"    note              : {res.get('note')}")
    for m in (res.get("messages") or [])[:8]:
        txt = m.get("text") or m.get("summary") if isinstance(m, dict) else str(m)
        print(f"      - {txt}")
    print()
    print("  VERDICT")
    print(f"    readable          : {verdict['readable']}")
    print(f"    {verdict['summary']}")
    print()
    # Two reasons to run the C4FM front end here regardless of the router:
    #   * decode_router has no P25 entry at all (its _ORDER is pocsag / flex /
    #     dmr / ook / video / dtmf / cw), so a P25 signal auto-routes to nothing
    #     and "protocol: none" would read as "not P25";
    #   * iq_features' FSK detector is measured on the raw capture and reads 0
    #     states on a clean narrow-deviation C4FM eye, so looks_like_c4fm() is
    #     not something to gate a BENCH tool on. The frame sync is the real test
    #     and it is cheap — run it and let the lock count answer.
    print("  C4FM FRAME PROBE (the router has no P25 entry, and the feature gate "
          "is not reliable enough to skip this on a bench file)")
    sym = c4fm_trial(iq, fs, invert=False, symbol_rate=c4fm.SYMBOL_RATE)
    dib = np.asarray(sym.get("dibits"), dtype=np.uint8)
    p25_locks = len(p25.find_sync(dib)) if dib.size else 0
    dmr_locks = (len(dmr.find_sync_bursts(dib)) if dib.size >= dmr.BURST_DIBITS
                 else 0)
    print(f"    rails {sym.get('rails_used', 0)}/4, quality "
          f"{sym.get('quality', 0.0):.3f}, P25 sync locks {p25_locks}, "
          f"DMR sync matches {dmr_locks}")
    print()
    if p25_locks:
        print_c4fm(sym)
        print()
        print_p25(iq, fs, sym, verbose)
    elif dmr_locks:
        print_c4fm(sym)
        print()
        print_dmr(iq, fs, sym, verbose)
    else:
        print("    no P25 or DMR frame sync in this slice — nothing to run the "
              "ladder against. Re-run with --decoder p25 or dmr to see the "
              "front-end detail anyway.")
    return res


def run_other(iq, fs, name: str, feats):
    """pocsag / flex / ook / video — run the named decoder, feature gate bypassed."""
    res = route_decode(iq, fs, feats, prefer=name)
    print(f"  {name.upper()} DECODE")
    print(f"    tried             : {', '.join(res.get('tried') or [])}")
    print(f"    protocol          : {res.get('protocol') or 'none'}")
    print(f"    note              : {res.get('note')}")
    msgs = res.get("messages") or []
    print(f"    messages          : {len(msgs)}")
    for m in msgs[:20]:
        print(f"      - {m.get('text') if isinstance(m, dict) else m}")
    return res


# ---------------------------------------------------------------- sweeps
def symbol_rate_grid(spec: str) -> list:
    """--symbol-rate-search: 'auto', 'LO:HI:N', or a comma-separated list.

    Why sweep it at all: a real radio pair has a crystal offset, and a C4FM
    receiver locked to exactly 4800.00 Bd against a transmitter running 4799.5
    accumulates timing error across a frame. The Gardner loop absorbs a lot of
    that, but the failure mode when it does not is a stream that syncs and then
    fails FEC — which looks exactly like a wrong field order. Cheap to rule out."""
    s = (spec or "").strip().lower()
    if not s:
        return []
    if s == "auto":
        return list(np.linspace(c4fm.SYMBOL_RATE * 0.99,
                                c4fm.SYMBOL_RATE * 1.01, 9))
    if ":" in s:
        lo, hi, n = s.split(":")
        return list(np.linspace(float(lo), float(hi), int(n)))
    return [float(x) for x in s.split(",") if x.strip()]


def run_sweep(iq, fs, decoder: str, polarities, rates, verbose: bool):
    """Grid over (polarity x symbol rate), one compact row per trial.

    The point is to BISECT: if one cell of the grid suddenly climbs the ladder,
    the assumption that cell varies was the wrong one, and you found it without
    another field trip."""
    print("  PARAMETER SWEEP")
    print(f"    {'polarity':<10} {'sym rate':>10} {'quality':>8} {'rails':>6} "
          f"{'sync':>6} {'stage-4':>10} {'voice FEC':>12}")
    rows = []
    for inv in polarities:
        for rate in rates:
            sym = c4fm_trial(iq, fs, invert=inv, symbol_rate=rate)
            dib = np.asarray(sym.get("dibits"), dtype=np.uint8)
            if decoder == "p25":
                if dib.size:
                    lad = p25_ladder(dib)
                    voice = p25_voice_fec(lad["frames"])
                    sync, stage4 = lad["sync_locks"], (lad["rs_pass"],
                                                       lad["rs_pass"] + lad["rs_fail"])
                else:
                    lad, voice, sync, stage4 = {}, {"clean": 0, "total": 0}, 0, (0, 0)
            else:
                if dib.size >= dmr.BURST_DIBITS:
                    out = dmr.decode_dmr(iq, fs, symbols=sym)
                    st = out["stats"]
                    voice = dmr_voice_fec(dib, out.get("bursts") or [],
                                          bool(st.get("inverted")))
                    sync = st["syncs"]
                    stage4 = (st["bptc_ok"], st["bptc_ok"] + st["bptc_failed"])
                else:
                    voice, sync, stage4 = {"clean": 0, "total": 0}, 0, (0, 0)
            vpair = (voice["clean"], voice["total"])
            rows.append({"inverted": inv, "rate": rate,
                         "quality": sym.get("quality", 0.0),
                         "rails": sym.get("rails_used", 0), "sync": sync,
                         "stage4": stage4, "voice": vpair})
            print(f"    {'inverted' if inv else 'normal':<10} {rate:>10.1f} "
                  f"{sym.get('quality', 0.0):>8.3f} {sym.get('rails_used', 0):>6} "
                  f"{sync:>6} {f'{stage4[0]}/{stage4[1]}':>10} "
                  f"{f'{vpair[0]}/{vpair[1]}':>12}")
    best = max(rows, key=lambda r: (r["stage4"][0], r["voice"][0], r["sync"],
                                    r["quality"])) if rows else None
    if best:
        print()
        if decoder == "p25" and len({r["inverted"] for r in rows}) > 1:
            # Not a bug when the two polarity halves match: decode_p25_dibits
            # already tries BOTH dibit polarities internally, so flipping the
            # discriminator on top of that lands on the same stream. Keeping the
            # rows is still worth it — agreement PROVES the internal retry works,
            # and disagreement is itself the finding.
            byrate = {}
            for r in rows:
                byrate.setdefault(r["rate"], []).append(
                    (r["sync"], r["stage4"], r["voice"]))
            agree = all(len(set(v)) == 1 for v in byrate.values())
            print("    (P25 tries both dibit polarities inside the ladder, so the "
                  "normal/inverted halves should agree — they "
                  + ("do." if agree else "DO NOT here, which is itself a finding: "
                     "the internal polarity retry is not covering this stream.")
                  + ")")
        print(f"    BEST: {'inverted' if best['inverted'] else 'normal'} polarity "
              f"@ {best['rate']:.1f} Bd — stage-4 {best['stage4'][0]}/"
              f"{best['stage4'][1]}, voice FEC {best['voice'][0]}/{best['voice'][1]}")
        if best["stage4"][0] == 0 and best["sync"] == 0:
            print("    Nothing locked anywhere in the grid. The problem is upstream "
                  "of these parameters — check the capture itself (clipping, level, "
                  "centre frequency, dropped buffers) before touching the decoder.")
    return rows


# ---------------------------------------------------------------- main
def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Replay a recorded SigMF IQ file through the decode chain.")
    p.add_argument("--file", required=True,
                   help="path to the recording (.sigmf-data, .sigmf-meta or base)")
    p.add_argument("--decoder", default="auto", choices=DECODERS)
    p.add_argument("--offset-seconds", type=float, default=0.0,
                   help="skip this far into the recording (default 0)")
    p.add_argument("--seconds", type=float, default=DEFAULT_SECONDS,
                   help=f"length of the slice to decode (default {DEFAULT_SECONDS}; "
                        "0 = to the end of the file)")
    p.add_argument("--invert-polarity", default="auto",
                   choices=("auto", "normal", "inverted", "both"),
                   help="C4FM discriminator polarity; 'both' sweeps it")
    p.add_argument("--symbol-rate-search", default="",
                   help="sweep the symbol rate: 'auto' (4800 +/-1%%, 9 steps), "
                        "'LO:HI:N', or a comma-separated list of rates")
    p.add_argument("--allow-truncated", action="store_true",
                   help="read a truncated or unclosed recording anyway (it is "
                        "still reported, just not fatal)")
    p.add_argument("--verbose", action="store_true",
                   help="also list individual frames/bursts")
    args = p.parse_args(argv)

    try:
        iq, info = load_slice(args.file, args.offset_seconds,
                              None if args.seconds == 0 else args.seconds,
                              strict=not args.allow_truncated)
    except IQFileError as e:
        print(f"  cannot replay this file: {e}")
        return 2
    print_recording(info, iq, args.offset_seconds)
    fs = float(info["sample_rate"])
    if iq.size < 256:
        print("  slice is too short to analyse (need >= 256 samples). Check "
              "--offset-seconds against the file length.")
        return 2

    rates = symbol_rate_grid(args.symbol_rate_search)
    pol = {"auto": [False], "normal": [False], "inverted": [True],
           "both": [False, True]}[args.invert_polarity]
    sweeping = bool(rates) or len(pol) > 1

    if args.decoder in ("p25", "dmr"):
        if sweeping:
            run_sweep(iq, fs, args.decoder, pol, rates or [c4fm.SYMBOL_RATE],
                      args.verbose)
            print()
            print("  Now re-run without the sweep flags at the winning settings "
                  "for the full ladder.")
            return 0
        sym = c4fm_trial(iq, fs, invert=pol[0], symbol_rate=c4fm.SYMBOL_RATE)
        print_c4fm(sym)
        print()
        if args.decoder == "p25":
            print_p25(iq, fs, sym, args.verbose)
        else:
            print_dmr(iq, fs, sym, args.verbose)
        print()
        print("  READ THE LADDER, NOT THE VERDICT. Each stage is an independent "
              "arithmetic check; a stage that passes consistently on real air "
              "confirms every assumption below it, and the first stage that fails "
              "while the one under it passes is where the wrong assumption lives.")
        return 0

    try:
        feats = identify.iq_features(iq, fs)
    except ValueError as e:
        print(f"  cannot extract features: {e}")
        return 2
    if args.decoder == "auto":
        run_auto(iq, fs, feats, args.verbose)
    else:
        run_other(iq, fs, args.decoder, feats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
