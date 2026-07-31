"""Auto-router + plain-English interpretation for a captured signal.

The field need: a loud signal is coming from inside the client's house — the
operator clicks it and must learn WHAT it is and WHAT it's transmitting, pager or
not, without first knowing the protocol. This module is the "interpret it" brain:

- route_decode(): given the IQ + the iq_features, try every decoder whose feature
  signature matches (POCSAG / FLEX / OOK-FSK device / DTMF / CW) and return the
  first that yields real messages. Each decoder has its own honesty gate, so a
  mismatch returns nothing rather than a hallucinated decode.
- interpret(): a plain-English verdict on what the device is and whether its
  content is readable — readable content for the decodable majority, and an
  honest "identify + capture + locate" for encrypted/unknown digital.

No new DSP here — pure orchestration over the existing decoders. numpy-free.
"""

import logging

log = logging.getLogger("survey.decode_router")

# protocols that are DATA (decode, don't listen — they'd be hash as audio)
DATA_MODES = ("pocsag", "flex", "ook")
# everything the router/UI understands as an explicit decode preference
DECODE_MODES = ("pocsag", "flex", "ook", "dtmf", "cw")


def _decoders():
    """Lazy import so a missing optional module never breaks analyze."""
    from . import pocsag
    from .pocsag import looks_like_pocsag
    from .ook import decode_ook, looks_like_ook
    from .tones import decode_dtmf, decode_cw, looks_like_dtmf, looks_like_cw
    from .flex import decode_flex, looks_like_flex
    return {
        "pocsag": (lambda iq, fs: pocsag.decode_pocsag(iq, fs), looks_like_pocsag),
        "flex": (lambda iq, fs: decode_flex(iq, fs), looks_like_flex),
        "ook": (lambda iq, fs: decode_ook(iq, fs), looks_like_ook),
        "dtmf": (lambda iq, fs: decode_dtmf(iq, fs), looks_like_dtmf),
        "cw": (lambda iq, fs: decode_cw(iq, fs), looks_like_cw),
    }


# most-structured first; the prefer'd protocol is always tried first
_ORDER = ("pocsag", "flex", "ook", "dtmf", "cw")


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
            return r
    return {"protocol": None, "messages": [], "tried": tried,
            "note": "no decodable protocol matched its signature"}


def interpret(feats, decoded_protocol=None):
    """Plain-English field verdict: what the device is and whether its content is
    readable. `readable` ∈ {decoded, listen, maybe_decode, encrypted,
    identify_only, none, unknown} drives the UI hint."""
    if not feats:
        return {"summary": "No IQ capture available — passive frequency-only "
                           "identification. Pause the sweep or open the receiver "
                           "to interrogate this signal.",
                "readable": "unknown"}
    if not feats.get("signal_present"):
        return {"summary": "Noise floor — no real emitter at this frequency.",
                "readable": "none"}
    if decoded_protocol:
        return {"summary": f"Decoded as {decoded_protocol} — readable content below.",
                "readable": "decoded"}
    cls = feats.get("modulation_class")
    bw = feats.get("occupied_bw_khz", 0)
    rate = feats.get("symbol_rate_kbd")
    if cls == "analog_voice":
        return {"summary": "Narrowband analog voice — classic audio-bug signature. "
                "PRESS LISTEN: if it's a room bug you'll hear the room.",
                "readable": "listen"}
    if cls == "analog_video":
        return {"summary": "Wideband continuous FM — possible analog video / "
                "baby-monitor-style camera. Listen and watch the waterfall.",
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
