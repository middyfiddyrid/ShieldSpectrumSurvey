"""Per-detection "now what?" guidance for the live ledger and results lists.

An anomaly row is only useful if the operator knows the NEXT MOVE for it —
listen, run the phone correlation test, check the Wi-Fi inventory, DF it, or
cross it off. This encodes the layered playbook from
SIGNAL_IDENTIFICATION_GUIDE.md (quick decision table) as a pure function over
one signals row: no radio, no DB, no network — cheap enough to compute on
every /api/signals poll.

Kinds drive the UI affordance:
  done      — already triaged; nothing left to do
  review    — an ANALYZE verdict is in; decide and cross off
  urgent    — energy where none should be (GPS band): escalate now
  clear     — matched/expected; one-click cross-off
  test      — a physical/behavioral confirmation (airplane-mode, DECT base)
  inventory — correlate against the wireless inventory
  listen    — demodulate it (suggested mode in "mode")
  identify  — run the signature matcher

Pure python, unit tested with synthetic rows.
"""

from .allocations import allocations_containing
from .behavior import classify_behavior
from .rf_match import match_inventory, summarize_matches

# ISM/U-NII spans where "wide = Wi-Fi/camera class" reasoning applies (MHz)
_WIDEBAND_ISM = ((2400.0, 2500.0), (5150.0, 5900.0))


def _step(kind, action, detail, mode=None):
    out = {"kind": kind, "action": action, "detail": detail}
    if mode:
        out["mode"] = mode
    return out


def next_step_for(sig: dict) -> dict:
    """sig: one signals-table row (dict). Missing keys are tolerated so the
    live ledger can call this on mid-sweep rows postprocess hasn't scored yet."""
    center = float(sig.get("center_mhz") or 0.0)
    bw_khz = sig.get("bw_khz")
    label = (sig.get("matched_label") or sig.get("display_label") or "").strip()
    lower = (label + " " + (sig.get("match_reason") or "")).lower()
    caution = bool(sig.get("caution"))
    tier = sig.get("identity_tier") or ""
    classification = sig.get("classification") or ""
    persistence = sig.get("persistence")
    behavior = sig.get("behavior") or {}
    bclass = (behavior.get("class_id") if isinstance(behavior, dict) else "") or ""
    inv = sig.get("inventory_matches") or []

    # 1 — already decided: the row is crossed off (or escalated), say so.
    triage = (sig.get("triage_status") or "unreviewed").strip()
    if triage and triage != "unreviewed":
        word = {"cleared": "cleared", "suspect": "marked suspect",
                "flagged": "FLAGGED", "ask_client": "ask client"}.get(triage, triage)
        return _step("done", f"✓ {word}",
                     f"Triaged: {word}" + (f" — {sig.get('triage_notes')}"
                                           if sig.get("triage_notes") else ""))

    # 2 — an ANALYZE verdict is already in: the operator listened/decoded it.
    #     Surface the verdict and prompt the decision instead of re-suggesting
    #     the analysis they already ran.
    if sig.get("analysis_summary"):
        return _step("review", "DECIDE",
                     "Analyzed — " + sig["analysis_summary"]
                     + " Clear it or flag it.")

    allocs = allocations_containing(center)

    # 3 — GPS band: legitimate GPS is below the noise floor. ANY detectable
    #     energy here means a repeater, jammer, or spoofer — top priority.
    if (bclass == "gps_anomaly"
            or any(a.category == "gps" or a.band_tag == "gps" for a in allocs)
            or "gps" in lower):
        return _step("urgent", "⚠ DF NOW",
                     "Energy in the GPS band — should be below noise. "
                     "DF + physical search immediately.")

    # 4 — identified against the baseline: nothing to hunt, cross it off.
    if (tier == "IDENTIFIED" or classification == "VERIFIED") and not caution:
        return _step("clear", "✓ CLEAR",
                     (f"Matches {label} — " if label else "Matched to baseline — ")
                     + "cross off.")

    # 4b — inventory already pinned a Wi-Fi AP or BLE device to this hit:
    #      cheapest confirm is mark owned / power-cycle that gear.
    if inv and isinstance(inv, list) and inv[0].get("kind") == "wifi":
        top = inv[0]
        owned = "owned" if top.get("owned") else "NOT owned"
        return _step("inventory", "MATCH AP",
                     f"RF aligns with Wi-Fi “{top.get('name') or top.get('identifier')}” "
                     f"({owned}). Power-cycle that AP/client or mark ownership; "
                     f"if energy remains, DF the residue.")
    if inv and any(m.get("is_tracker") for m in inv if isinstance(m, dict)):
        return _step("inventory", "⚑ TRACKER",
                     "BLE inventory shows a location tracker co-band with this "
                     "RF energy — confirm client ownership; unowned tracker = "
                     "priority physical find.")

    # 5 — cellular uplink: expected when phones are present; the airplane-mode
    #     correlation test separates client phones from a cellular exfil device.
    if (bclass == "cellular_uplink" or any(a.uplink for a in allocs)
            or "uplink" in lower):
        return _step("test", "PHONE TEST",
                     "Cellular uplink — run the phone correlation test: all "
                     "phones to airplane mode; legit uplinks drop, a planted "
                     "cellular device keeps transmitting.")

    # 6 — DECT: cordless phone infrastructure lives here (and DECT room bugs).
    if (bclass == "dect_cordless"
            or any(a.band_tag == "dect" for a in allocs) or "dect" in lower):
        return _step("test", "CHECK DECT",
                     "DECT band — locate the cordless phone bases; unplug them "
                     "and re-sweep. Persisting energy = investigate.")

    # 6b — behavioral class drives the next move when band alone is ambiguous.
    if bclass == "video_like":
        return _step("inventory", "CAM-CLASS",
                     "Continuous wideband (video/cam class) — match Wi-Fi inventory, "
                     "power off known cameras/TVs/APs, DF anything left. "
                     "RF alone cannot name the brand.")
    if bclass == "wifi_data":
        return _step("inventory", "INVENTORY",
                     "Wi-Fi/data class — identifiers are BSSID/SSID/OUI in the "
                     "wireless inventory, not the spectrum peak.")
    if bclass == "ble_class":
        return _step("inventory", "BLE SCAN",
                     "Bluetooth-class look — run BLE inventory for MAC / vendor / "
                     "trackers; RF envelope will not name the device.")
    if bclass == "keyfob_like":
        return _step("identify", "IDENTIFY",
                     "Key-fob / remote / sensor class — IDENTIFY the OOK/FSK "
                     "shape, then press known remotes to correlate.")
    if bclass == "hid_keepalive":
        return _step("inventory", "PERIPH OFF",
                     "Mouse/keyboard-class look — power off wireless peripherals "
                     "and dongles; residue is a lead (nRF24-class can mimic this).")
    if bclass == "analog_audio_bug":
        return _step("listen", "🎧 LISTEN",
                     "Analog audio / narrow FM class — LISTEN in NFM; clear room "
                     "audio = classic bug signature. No digital ID expected.",
                     mode="nfm")

    # 7 — MHz-wide in 2.4/5 GHz: Wi-Fi/video class; the RF trace alone won't
    #     name it, the wireless inventory will.
    if bw_khz and bw_khz >= 1000.0 and any(lo <= center <= hi
                                           for lo, hi in _WIDEBAND_ISM):
        return _step("inventory", "INVENTORY",
                     "Wide 2.4/5 GHz emitter — match against the Wi-Fi/BLE "
                     "inventory. Unknown high-power here = possible camera or "
                     "rogue AP.")

    # 8 — narrowband: one LISTEN answers it. Clear room audio = analog bug.
    if bw_khz is not None and bw_khz <= 25.0:
        in_ism = any(a.band_tag in ("ism_433", "ism_915") for a in allocs)
        # Width alone, on the sweep row, before any ANALYZE has run — so this
        # prompts the check, it does not report a result.
        return _step("listen", "🎧 LISTEN",
                     "Narrowband"
                     + (" in a band commonly used by analog bugs (ISM)" if in_ism else "")
                     + " — LISTEN in NFM to tell them apart: room audio would "
                       "confirm an audio bug; a steady dead carrier looks "
                       "identical on the sweep until you listen.", mode="nfm")

    # 9 — bursty/intermittent: audio won't help; the signature matcher will.
    if persistence is not None and persistence < 0.25:
        return _step("identify", "IDENTIFY",
                     "Bursty/intermittent — run IDENTIFY for a signature match "
                     "(keyfob, sensor, digital device).")

    # 10 — plausible & un-cautioned: probably a legit device; verify cheaply.
    if classification == "PLAUSIBLE" and not caution:
        return _step("clear", "SPOT-CHECK",
                     (f"Likely normal ({label}) — " if label else "Likely normal — ")
                     + "quick listen or inventory check, then clear.")

    # 11 — everything else: the standard anomaly ladder.
    return _step("listen", "🎧 LISTEN",
                 "Unexplained — LISTEN first, then IDENTIFY; if it persists, "
                 "DF walk it.")


def device_hint_for(sig: dict) -> str:
    """Cheap, instant 'what kind of thing is this' — signature ID wins, then
    behavioral class, then bare band guess for the live sweep chart."""
    dc = (sig.get("id_device_class") or "").strip()
    if dc:
        return dc
    behavior = sig.get("behavior")
    if isinstance(behavior, dict):
        bid = behavior.get("class_id") or ""
        if bid and bid not in ("unknown", "ism_unclassified") and behavior.get("label"):
            return behavior["label"]
    try:
        center = float(sig.get("center_mhz") or 0.0)
    except (TypeError, ValueError):
        return ""
    allocs = allocations_containing(center)
    if any(getattr(a, "uplink", False) for a in allocs):
        return "Cell phone (uplink)"
    tags = {a.band_tag for a in allocs}
    cats = {a.category for a in allocs}
    if "dect" in tags:
        return "Cordless phone (DECT)"
    if tags & {"ism_2400", "ism_5800"}:
        return "WiFi / Bluetooth"
    if tags & {"ism_433", "ism_915"}:
        return "ISM device (remote / sensor / bug)"
    if "gps" in tags or "gps" in cats:
        return "GPS band"
    if "cellular" in cats:
        return "Cellular"
    if "broadcast" in cats:
        return "Broadcast (FM / AM / TV)"
    if "ham" in cats:
        return "Amateur radio"
    if "aviation" in cats:
        return "Aviation"
    return ""


def _fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def blinded_cue_for(sig: dict) -> dict:
    """Content-BLIND 'does this matter to the client' cue. Two content-free reads:
      * carries_audio — a room-bug audio SIGNATURE, judged from SHAPE only
        (narrow + persistent + analog band), NEVER from listening to content.
      * space — whether the emission appears to originate INSIDE the client's
        space, from spatial evidence only (interior-vs-ambient delta, room
        localization, novelty, or an operator attribution) — never content.
    Lets an operator WITHOUT listen privilege still know if a signal is
    client-relevant, so nobody has to intercept a communication to triage it."""
    center = _fnum(sig.get("center_mhz")) or 0.0
    bw = _fnum(sig.get("bw_khz"))
    persistence = _fnum(sig.get("persistence")) or 0.0
    allocs = allocations_containing(center)
    analog_band = (0 < center < 1000.0) or any(
        a.band_tag in ("ism_433", "ism_915") or a.category == "land_mobile"
        for a in allocs)
    carries_audio = bool(bw is not None and bw <= 30.0 and persistence >= 0.3
                         and analog_band)

    attrib = (sig.get("attribution_status") or "").strip()
    if attrib == "client_space":
        space = "client"
    elif attrib in ("third_party", "infrastructure"):
        space = "not_client"
    else:
        interior = _fnum(sig.get("interior_delta_db"))
        loc = _fnum(sig.get("localization_score"))
        pts = 0
        if interior is not None and interior >= 10.0:
            pts += 2
        if loc is not None and loc >= 0.5:
            pts += 1
        if sig.get("is_novel_to_address"):
            pts += 1
        if pts >= 2:
            space = "likely_client"
        elif interior is not None and interior < 3.0:
            space = "likely_not"
        else:
            space = "unknown"

    space_txt = {
        "client": "Confirmed in the client's space",
        "likely_client": "Appears to originate inside the client's space",
        "not_client": "Attributed to a third party — not the client",
        "likely_not": "Looks like it's from outside / not client-related",
        "unknown": "Location not yet established",
    }[space]
    audio_txt = ("carries audio — room-bug signature" if carries_audio
                 else "no voice-bug audio signature (data / not analog audio)")
    return {"carries_audio": carries_audio, "space": space,
            "listenable": carries_audio,
            "listen_permitted": (attrib == "client_space"),
            "summary": f"{space_txt}; {audio_txt}."}


def enrich_signal_rows(rows: list, inventory: list | None = None,
                       base_dir: str | None = None) -> list:
    """Attach next_step + snr_db + device_hint + blinded cue + behavior class
    + inventory correlation + local signature library hits to /api/signals rows.

    `inventory` is optional session Wi-Fi/BLE rows; when present, each RF hit
    gets inventory_matches (BSSID/SSID/BLE MAC candidates). Behavior runs even
    without inventory so the ledger always shows a device-class guess.
    """
    inv = inventory or []
    local_refs = []
    if base_dir:
        try:
            from .sig_library import load_index, match_local
            local_refs = load_index(base_dir)
        except Exception:  # noqa: BLE001
            local_refs = []
    for r in rows:
        try:
            if r.get("peak_dbm") is not None and r.get("floor_dbm") is not None:
                r["snr_db"] = round(r["peak_dbm"] - r["floor_dbm"], 1)
            else:
                r["snr_db"] = None
            # behavior BEFORE next_step / device_hint so those can use it
            try:
                r["behavior"] = classify_behavior(r)
            except Exception:  # noqa: BLE001
                r["behavior"] = None
            try:
                center = float(r.get("center_mhz") or 0.0)
                r["inventory_matches"] = (
                    match_inventory(center, r.get("bw_khz"), inv) if inv else [])
                r["inventory_summary"] = summarize_matches(r["inventory_matches"])
            except Exception:  # noqa: BLE001
                r["inventory_matches"] = []
                r["inventory_summary"] = ""
            try:
                if local_refs:
                    from .sig_library import match_local
                    bclass = (r.get("behavior") or {}).get("class_id") or ""
                    r["local_matches"] = match_local(
                        float(r.get("center_mhz") or 0), r.get("bw_khz"),
                        bclass, local_refs)
                    r["local_summary"] = (
                        r["local_matches"][0]["phrase"] if r["local_matches"] else "")
                else:
                    r["local_matches"] = []
                    r["local_summary"] = ""
            except Exception:  # noqa: BLE001
                r["local_matches"] = []
                r["local_summary"] = ""
            # surface experiment summaries if present
            try:
                from .experiments import parse_experiments, summarize_outcome
                exps = parse_experiments(r.get("experiments_json"))
                r["experiments"] = exps
                r["experiment_summary"] = (
                    summarize_outcome(exps[-1]) if exps else "")
            except Exception:  # noqa: BLE001
                r["experiments"] = []
                r["experiment_summary"] = ""
            r["next_step"] = next_step_for(r)
            r["device_hint"] = device_hint_for(r)
            r["cue"] = blinded_cue_for(r)
        except Exception:  # noqa: BLE001 — guidance must never break the list
            r["next_step"] = None
            r["device_hint"] = ""
            r["cue"] = None
            r.setdefault("behavior", None)
            r.setdefault("inventory_matches", [])
            r.setdefault("inventory_summary", "")
            r.setdefault("local_matches", [])
            r.setdefault("local_summary", "")
            r.setdefault("experiments", [])
            r.setdefault("experiment_summary", "")
    return rows
