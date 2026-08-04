"""Per-signal verdict for the truth panel (window 4).

The operator's rule, stated plainly: window 4 either REVEALS THE CONTENT of the
signal, or the signal is a FAIL. There is no middle category. Every analyzed
signal resolves to exactly WIN or FAIL, and a sweep's headline number is

    did-the-job % = WINS / (WINS + FAILS)

WIN means human-meaningful payload was actually rendered:
  * audio   — analog voice/room audio demodulated to a WAV the operator can hear
  * text    — a paging/keying protocol decoded to its message (POCSAG/FLEX/DTMF/CW)
  * data    — a data protocol decoded to its payload
  * image   — an analog-video raster recovered to a still

Everything else is a FAIL. Each FAIL carries:
  * reason_code — WHAT stopped the content from being revealed
  * cause       — WHO owns it:
        "code"    — this software failed and could do better (a decoder threw,
                    a capture errored, a clear signal we could have rendered but
                    didn't). These are ON US.
        "physics" — no software can reveal it: the payload is ENCRYPTED, or the
                    emitter is OUT OF BAND for the radio, or its bandwidth is
                    WIDER THAN THE CAPTURE window. Unfixable by anyone, including
                    any open-source tool. Still counts as a FAIL — the operator's
                    rule is content-or-fail — but the cause is labelled honestly
                    so the score is never mistaken for a fixable defect.
        "no-signal" — the channel was empty; there was no content to reveal.

cause NEVER changes the WIN/FAIL score. It is an autopsy, not a second grade.
The point is the opposite of hiding: every miss is a miss, counted, AND labelled
with why it missed and whether anything could ever change it.

Anti-fudge: a "physics" cause is not something this module gets to assert for
free — each one is derived from an objective, checkable input (the radio's tuning
range, the occupied bandwidth vs the capture sample rate, the decoder's own
encryption evidence, the measured SNR), so a code failure cannot be laundered
into a physics excuse.
"""

from __future__ import annotations

# Protocols whose DECODED MESSAGES *are* the content — recovering them reveals
# the payload, so a clean decode is a WIN.
PAGING_DATA_PROTOCOLS = {"POCSAG", "FLEX", "OOK", "OOK-FSK", "DTMF", "CW", "ADS-B"}

# Protocols that carry VOICE: the decoded frame layer is only metadata
# (talkgroup, radio ID). The content is the voice, so it is a WIN only if audio
# was actually rendered — a clear call we identified but never turned into sound
# is still a FAIL (cause "code": that is a gap we own), and an encrypted call is
# a FAIL (cause "physics").
DIGITAL_VOICE_PROTOCOLS = {"DMR", "P25", "NXDN", "TETRA", "DSTAR"}


def score_signal(
    *,
    feats: dict | None,
    routed: dict | None,
    audio_present: bool,
    image_present: bool,
    in_range: bool,
    radio_lo_mhz: float | None,
    radio_hi_mhz: float | None,
    center_mhz: float,
    capture_fs_hz: float | None,
    capture_error: bool = False,
) -> dict:
    """Reduce one analyzed signal to a WIN/FAIL verdict for window 4.

    Pure: no I/O, no state. Inputs are exactly what the analyze endpoint already
    computes — see survey/server.py.
    """
    protocol = (routed or {}).get("protocol")
    proto_u = str(protocol).upper() if protocol else None
    encrypted = bool((routed or {}).get("encrypted"))
    messages = (routed or {}).get("messages") or []

    def win(kind: str) -> dict:
        return {"verdict": "WIN", "content_kind": kind,
                "reason_code": None, "cause": None, "reason": None}

    def fail(code: str, cause: str, reason: str) -> dict:
        return {"verdict": "FAIL", "content_kind": None,
                "reason_code": code, "cause": cause, "reason": reason}

    def na(code: str, reason: str) -> dict:
        # NOT a WIN or a FAIL: no capture was attempted, so the code was never
        # tested. Excluded from the scorecard entirely — scoring a setup problem
        # against the demod would be a dishonest denominator.
        return {"verdict": "NA", "content_kind": None,
                "reason_code": code, "cause": "setup", "reason": reason}

    # --- FAILs we own first: the capture itself never produced usable IQ ---
    if capture_error:
        return fail("capture_error", "code",
                    "the capture failed before any content could be recovered")

    if not in_range:
        # Genuinely out of band ONLY when the radio's range is known and the
        # frequency falls outside it — that is a physics FAIL. If the range is
        # unknown, no monitor radio captured at all (not connected / busy): NA.
        if radio_lo_mhz is not None and radio_hi_mhz is not None:
            return fail("out_of_band", "physics",
                        f"{center_mhz:.3f} MHz is outside "
                        f"{radio_lo_mhz:.0f}-{radio_hi_mhz:.0f} MHz — the front "
                        f"end cannot receive it, so there is no content to reveal")
        return na("no_capture",
                  "no monitor radio captured this signal (not connected, or busy "
                  "on a sweep) — nothing was demodulated, so this does not score")

    # --- nothing was there ---
    if not feats or not feats.get("signal_present"):
        snr = (feats or {}).get("snr_db")
        s = f" (SNR {snr} dB)" if snr is not None else ""
        return fail("empty", "no-signal",
                    f"no emitter above the noise floor{s} — an empty channel has "
                    f"no content to reveal")

    # --- WINs: content actually rendered ---
    if image_present:
        return win("image")
    if audio_present:
        return win("audio")

    if proto_u and messages:
        if encrypted:
            return fail("encrypted", "physics",
                        f"{protocol} decoded — talkgroup/radio metadata is "
                        f"readable, but the payload is ENCRYPTED and cannot be "
                        f"recovered by any software; LOCATE it instead")
        if proto_u in PAGING_DATA_PROTOCOLS:
            return win("text")
        if proto_u in DIGITAL_VOICE_PROTOCOLS:
            # identified the call but produced no audio the operator can hear
            return fail("voice_not_rendered", "code",
                        f"{protocol} identified but its clear voice was not "
                        f"rendered to audio — content not revealed")
        # some other protocol with a real message payload
        return win("data")

    # --- signal is real, in band, but nothing was demodulated/decoded ---
    # Distinguish a physics limit (too wide to fit the capture) from a code gap.
    occ_bw_khz = float((feats or {}).get("occupied_bw_khz") or 0.0)
    if capture_fs_hz and occ_bw_khz > 0.0:
        # a signal occupying more than the usable capture bandwidth cannot be
        # coherently demodulated from this window — objective, checkable.
        usable_khz = 0.8 * float(capture_fs_hz) / 1e3
        if occ_bw_khz > usable_khz:
            return fail("too_wide", "physics",
                        f"the signal is ~{occ_bw_khz:.0f} kHz wide but the "
                        f"capture window is only ~{usable_khz:.0f} kHz — it does "
                        f"not fit, so content cannot be recovered here; capture "
                        f"wider or step in")

    return fail("no_content", "code",
                "a real in-band signal, but nothing was demodulated or decoded — "
                "no content revealed")


def tally(verdicts: list[dict]) -> dict:
    """Roll a sweep's per-signal verdicts into the scorecard.

    Headline `did_job_pct` is WINS / total, per the operator's content-or-fail
    rule. The breakdown exists so every FAIL is explained and its cause visible;
    it does not alter the headline.
    """
    # NA verdicts (no capture attempted) never entered the demod — exclude them
    # from the scored denominator entirely.
    scored = [v for v in verdicts if v.get("verdict") in ("WIN", "FAIL")]
    total = len(scored)
    wins = sum(1 for v in scored if v.get("verdict") == "WIN")
    fails = total - wins
    verdicts = scored
    by_cause: dict[str, int] = {}
    by_reason: dict[str, int] = {}
    for v in verdicts:
        if v.get("verdict") != "FAIL":
            continue
        by_cause[v.get("cause") or "?"] = by_cause.get(v.get("cause") or "?", 0) + 1
        rc = v.get("reason_code") or "?"
        by_reason[rc] = by_reason.get(rc, 0) + 1
    return {
        "total": total,
        "wins": wins,
        "fails": fails,
        "did_job_pct": round(100.0 * wins / total, 1) if total else 0.0,
        # of the FAILs: how many are on the code vs unfixable physics vs no-signal
        "fails_code": by_cause.get("code", 0),
        "fails_physics": by_cause.get("physics", 0),
        "fails_no_signal": by_cause.get("no-signal", 0),
        "fail_reasons": by_reason,
    }
