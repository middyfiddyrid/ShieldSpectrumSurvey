"""Auto-router + plain-English interpretation for a captured signal.

The field need: a loud signal is coming from inside the client's house — the
operator clicks it and must learn WHAT it is and WHAT it's transmitting, pager or
not, without first knowing the protocol. This module is the "interpret it" brain:

- route_decode(): given the IQ + the iq_features, try every decoder whose feature
  signature matches (POCSAG / FLEX / OOK-FSK device / analog VIDEO / DTMF / CW)
  and return the first that yields real messages. Each decoder has its own honesty
  gate, so a mismatch returns nothing rather than a hallucinated decode.
- interpret(): a plain-English verdict on what the device is and whether its
  content is readable — readable content for the decodable majority, and an
  honest "identify + capture + locate" for encrypted/unknown digital.

No new DSP here — pure orchestration over the existing decoders. numpy-free.
"""

import logging

log = logging.getLogger("survey.decode_router")

# protocols that are DATA (decode, don't listen — they'd be hash as audio).
# 'video' is deliberately NOT here: an analog video carrier is still worth
# hearing (the audio subcarrier / the FM buzz), and its decoder only needs a
# fraction of a second of IQ, so it wants neither the longer paging capture nor
# the audio suppression that this list turns on.
DATA_MODES = ("pocsag", "flex", "ook", "dmr")
# everything the router/UI understands as an explicit decode preference
DECODE_MODES = ("pocsag", "flex", "ook", "dmr", "p25", "nxdn", "video", "dtmf", "cw")


def _decoders():
    """Lazy import so a missing optional module never breaks analyze."""
    from . import pocsag
    from .pocsag import looks_like_pocsag
    from .ook import decode_ook, looks_like_ook
    from .tones import decode_dtmf, decode_cw, looks_like_dtmf, looks_like_cw
    from .flex import decode_flex, looks_like_flex
    from .video import decode_video, looks_like_video
    from .dmr import decode_dmr, looks_like_dmr
    from .p25 import decode_p25, looks_like_p25
    from .nxdn import decode_nxdn, looks_like_nxdn
    return {
        "pocsag": (lambda iq, fs: pocsag.decode_pocsag(iq, fs), looks_like_pocsag),
        "flex": (lambda iq, fs: decode_flex(iq, fs), looks_like_flex),
        "ook": (lambda iq, fs: decode_ook(iq, fs), looks_like_ook),
        # DMR reports ENCRYPTED traffic as a first-class message rather than as a
        # failed decode — "DMR, colour code 1, talkgroup 4021, encrypted" is a
        # finding, so the router must not treat it as nothing to show.
        "dmr": (lambda iq, fs: decode_dmr(iq, fs), looks_like_dmr),
        # P25 was MISSING here entirely — decode_p25/looks_like_p25 existed and
        # were tested, but nothing ever called them, so ANALYZE on a P25 signal
        # answered "no decodable protocol matched" when P25 had simply never been
        # tried. Same first-class encryption treatment as DMR.
        "p25": (lambda iq, fs: decode_p25(iq, fs), looks_like_p25),
        # NXDN (NEXEDGE / IDAS) — the third digital LMR mode, and the one most
        # easily missed: an NXDN48 carrier is only 6.25 kHz wide, so it reads as
        # narrowband analog to the feature extractor. Same first-class encryption
        # treatment as DMR and P25.
        "nxdn": (lambda iq, fs: decode_nxdn(iq, fs), looks_like_nxdn),
        "video": (lambda iq, fs: decode_video(iq, fs), looks_like_video),
        "dtmf": (lambda iq, fs: decode_dtmf(iq, fs), looks_like_dtmf),
        "cw": (lambda iq, fs: decode_cw(iq, fs), looks_like_cw),
    }


# most-structured first; the prefer'd protocol is always tried first. 'video'
# sits after the bit-level decoders and before the tone decoders: its gate wants
# a WIDE continuous carrier, which is disjoint from the narrowband keyed shapes
# above it, so the ordering here is about grouping, not contention.
_ORDER = ("pocsag", "flex", "dmr", "p25", "nxdn", "ook", "video", "dtmf", "cw")


def route_decode(iq, fs, feats, prefer=None):
    """Try the decoders whose signature matches `feats`; return the first with
    real messages. `prefer` (an explicit operator mode) is tried first and is NOT
    feature-gated. Returns {protocol, messages, note, tried:[...]} — protocol is
    None when nothing decoded (honesty gates held)."""
    decs = _decoders()
    order = list(_ORDER)
    if prefer in decs:
        order = [prefer] + [p for p in order if p != prefer]
    tried = []
    for name in order:
        run, looks = decs[name]
        if name != prefer:
            # feature-gate the auto path; skip decoders that don't match the shape
            try:
                if not (feats and looks(feats)):
                    continue
            except Exception:  # noqa: BLE001
                continue
        tried.append(name)
        try:
            r = run(iq, fs)
        except Exception:  # noqa: BLE001
            log.exception("decoder %s failed", name)
            continue
        if r and r.get("messages"):
            r.setdefault("protocol", name.upper())
            r["tried"] = tried
            # Surface encryption at the TOP level. DMR/P25 report it per message,
            # and without this a decoded-but-encrypted call reaches interpret()
            # looking like ordinary readable content — the UI would then offer
            # "readable content below" for traffic nobody can read.
            r["encrypted"] = any(m.get("encrypted") for m in r["messages"]
                                 if isinstance(m, dict))
            return r
    return {"protocol": None, "messages": [], "tried": tried,
            "note": "no decodable protocol matched its signature"}


def interpret(feats, decoded_protocol=None, encrypted=False):
    """Plain-English field verdict: what the device is and whether its content is
    readable. `readable` ∈ {decoded, listen, maybe_decode, encrypted,
    identify_only, none, unknown} drives the UI hint.

    encrypted=True means the protocol decoded but its PAYLOAD is not readable —
    a DMR/P25 call with the privacy bit set. That is a genuine TSCM finding (who
    is operating in this space), so it must not read as a failure; but it must
    not read as readable content either."""
    if not feats:
        return {"summary": "No IQ — connect the Pluto to interrogate this signal.",
                "readable": "unknown"}
    if not feats.get("signal_present"):
        return {"summary": "Noise floor — no real emitter at this frequency.",
                "readable": "none"}
    if decoded_protocol and encrypted:
        return {"summary": f"Identified as {decoded_protocol}, and the call "
                "metadata below is readable — but the voice payload is ENCRYPTED, "
                "so its content cannot be recovered by any means here. Who is "
                "operating in this space, and on what talkgroup, is still a "
                "finding.",
                "readable": "encrypted"}
    if decoded_protocol:
        if str(decoded_protocol).upper().startswith("ANALOG-VIDEO"):
            # Video "decodes" to a sync verdict + a still, not to readable content —
            # and a raster is not by itself a camera. Say exactly that.
            return {"summary": "Analog video sync structure found — NTSC/PAL line "
                    "timing recovered from this carrier (details below). That is "
                    "evidence of a VIDEO RASTER, not a confirmed camera: a monitor "
                    "emanation or a relayed feed reads the same. LOCATE it.",
                    "readable": "decoded"}
        return {"summary": f"Decoded as {decoded_protocol} — readable content below.",
                "readable": "decoded"}
    cls = feats.get("modulation_class")
    bw = feats.get("occupied_bw_khz", 0)
    rate = feats.get("symbol_rate_kbd")
    if cls == "analog_voice":
        # Bandwidth + continuity only. An unmodulated carrier, an LO leak and a
        # narrowband telemetry link all land here too (tests/test_identify.py
        # feeds a pure CW tone and gets this class), so this must not read as a
        # finding — it is persisted to signals.analysis_summary and replayed in
        # the ledger and the client report long after the panel is gone.
        return {"summary": "Continuous narrowband signal — compatible with analog "
                "FM/AM voice, but a dead carrier or telemetry link measures the "
                "same. PRESS LISTEN to validate: room audio confirms it, silence "
                "does not clear it.",
                "readable": "listen"}
    if cls == "analog_video":
        # We reach here only when the video decoder did NOT confirm sync (a
        # confirmation takes the decoded_protocol branch above). So this is still
        # a bandwidth-and-continuity SHAPE, and must not read as a finding: the
        # class also catches a wideband data link or a strong out-of-band splatter.
        # 'listen' (not 'maybe_decode'): this is ANALOG, so listening really does
        # produce audio — the FM buzz and any audio subcarrier are both diagnostic.
        # The DISSEMINATE row offers the video decode off the CLASS, so the
        # operator gets both actions without being told audio is impossible.
        return {"summary": "Wideband continuous FM — compatible with analog video "
                "or a baby-monitor-style camera; NOT confirmed, and no NTSC/PAL "
                "sync was recovered from this capture. Re-ANALYZE in VIDEO mode "
                "while the signal is strong (a field lock needs ~70 ms of clean "
                "carrier), and LISTEN for an audio subcarrier.",
                "readable": "listen"}
    if cls == "fsk":
        r = f" (~{rate} kBd)" if rate else ""
        return {"summary": f"{feats.get('fsk_states')}-level FSK data{r} — device "
                "telemetry / control link. The device decoder can read the bit pattern.",
                "readable": "maybe_decode"}
    if cls == "ook":
        return {"summary": "On-off keyed / bursty — beacon, remote, sensor, or Morse. "
                "If you HEAR beeping, switch to CW and listen. The device decoder reads "
                "the bit pattern if it repeats; a keyed carrier in an odd band is worth "
                "locating (could be a device ID or beacon).",
                "readable": "maybe_decode"}
    if cls in ("ofdm_wideband", "continuous_digital"):
        return {"summary": "Wideband digital (Wi-Fi / cellular / OFDM class) — NOT a "
                "covert-audio signature; content is not readable (encrypted/proprietary). "
                "Identify + locate; capture IQ as evidence if this is the intruder.",
                "readable": "encrypted"}
    return {"summary": "Unknown modulation — capture IQ + locate; identification only.",
            "readable": "identify_only"}
