"""Behavioral RF classifiers — "what KIND of thing does this look like?"

Radios do not receive a product name. They receive shape + timing + (sometimes)
modulation class. This module turns those into an honest device-behavior label:

  video_like, keyfob_like, hid_keepalive, wifi_data, ble_class,
  analog_audio_bug, cellular_uplink, narrow_beacon, unknown

Rules of the road (TSCM honesty):
  * Labels are "consistent with …", never "detected Samsung TV / hidden cam #3".
  * A real signature ID (id_device_class) and a protocol match (Wi-Fi BSSID / BLE
    company id) always outrank this shape guess.
  * Pure python, no radio, no DB — cheap enough for every /api/signals poll.

Inputs are the fields a signals-table row already has (center_mhz, bw_khz,
persistence, peak/floor SNR) plus optional IQ features from identify.iq_features
(duty_cycle, modulation_class, continuity).
"""

from __future__ import annotations

from .allocations import allocations_containing

# Confidence bands used by the UI (0..1)
_HIGH = 0.78
_MED = 0.58
_LOW = 0.38

# Honest "could also be" overlaps — always show next to class labels
COULD_ALSO_BE = {
    "video_like": [
        "Busy Wi-Fi AP / mesh backhaul",
        "Analog AV sender / FPV VTX",
        "Wi-Fi camera continuous uplink",
        "Microwave oven spill (2.4 only, intermittent)",
    ],
    "wifi_data": [
        "Client phone/laptop Wi-Fi",
        "IoT camera burst traffic",
        "Neighbor AP on same channel",
    ],
    "ble_class": [
        "Phone / watch / earbuds",
        "Location tracker (AirTag/Tile)",
        "BLE beacon / sensor",
    ],
    "keyfob_like": [
        "Door/car remote",
        "Alarm / weather sensor",
        "TPMS / utility telemetry",
        "Planted OOK beacon (rare)",
    ],
    "hid_keepalive": [
        "Wireless mouse / keyboard dongle",
        "Proprietary 2.4 GHz HID",
        "nRF24-class link (legit or covert — inventory subtract)",
    ],
    "analog_audio_bug": [
        "Wireless mic / IFB",
        "Analog room bug",
        "Narrow FM telemetry / LMR edge",
    ],
    "cellular_uplink": [
        "Client phones / tablets",
        "Hotspot / modem",
        "Cellular exfil device (only after phone-silent test fails)",
    ],
    "dect_cordless": [
        "Cordless phone base",
        "DECT baby monitor",
        "DECT-band bug (after bases unplugged)",
    ],
    "narrow_beacon": [
        "Telemetry / pager remnant",
        "Unmodulated carrier / LO leak",
        "Planted beacon",
    ],
    "gps_anomaly": [
        "GPS repeater",
        "Jammer / spoofer",
        "Test equipment",
    ],
    "ism_unclassified": [
        "Any ISM device — inventory + IDENTIFY required",
    ],
    "unknown": [
        "Insufficient shape — do not invent a device class",
    ],
}


def _f(v, default=None):
    try:
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _snr(sig: dict, feats: dict | None) -> float | None:
    if feats and feats.get("snr_db") is not None:
        return _f(feats["snr_db"])
    peak, floor = _f(sig.get("peak_dbm")), _f(sig.get("floor_dbm"))
    if peak is not None and floor is not None:
        return peak - floor
    return _f(sig.get("snr_db"))


def _duty(sig: dict, feats: dict | None) -> float | None:
    if feats and feats.get("duty_cycle") is not None:
        return _f(feats["duty_cycle"])
    # sweep "persistence" is the fraction of passes the peak was hit — a
    # usable duty proxy when IQ is not available
    return _f(sig.get("persistence"))


def _continuity(sig: dict, feats: dict | None) -> str:
    if feats and feats.get("continuity"):
        return str(feats["continuity"])
    d = _duty(sig, feats)
    if d is None:
        return "unknown"
    if d > 0.85:
        return "continuous"
    if d < 0.35:
        return "bursty"
    return "intermittent"


def _mod(feats: dict | None) -> str:
    return (feats or {}).get("modulation_class") or ""


def _in_band(center: float, lo: float, hi: float) -> bool:
    return lo <= center <= hi


def classify_behavior(sig: dict, feats: dict | None = None) -> dict:
    """Return a behavior dossier for one detection.

    sig: signals-row-like dict (center_mhz, bw_khz, persistence, …)
    feats: optional iq_features() output

    Always returns a dict with:
      class_id, label, confidence, evidence[list[str]], phrase, next_checks[list]
    """
    center = _f(sig.get("center_mhz"), 0.0) or 0.0
    bw = _f(sig.get("bw_khz"))
    duty = _duty(sig, feats)
    cont = _continuity(sig, feats)
    snr = _snr(sig, feats)
    mod = _mod(feats)
    allocs = allocations_containing(center) if center else []
    tags = {getattr(a, "band_tag", "") for a in allocs}
    cats = {getattr(a, "category", "") for a in allocs}
    uplink = any(getattr(a, "uplink", False) for a in allocs)

    evidence: list[str] = []
    next_checks: list[str] = []

    def _out(class_id, label, conf, phrase, ev=None, checks=None):
        return {
            "class_id": class_id,
            "label": label,
            "confidence": round(conf, 2),
            "evidence": list(ev or evidence),
            "phrase": phrase,
            "next_checks": list(checks or next_checks),
            "could_also_be": list(COULD_ALSO_BE.get(class_id, COULD_ALSO_BE["unknown"])),
        }

    # --- cellular uplink: scheduled bursts toward a tower -------------------
    if uplink or "uplink" in (sig.get("matched_label") or "").lower():
        evidence.append("inside a cellular UPLINK allocation")
        if cont == "bursty" or (duty is not None and duty < 0.5):
            evidence.append(f"{cont} occupancy (phones are packetized)")
        next_checks = [
            "Airplane-mode test: all phones silent → legit uplinks drop",
            "DF: moves with people = phones; fixed fixture = investigate",
        ]
        return _out(
            "cellular_uplink", "Cell phone uplink",
            _HIGH if cont in ("bursty", "intermittent", "unknown") else _MED,
            "Consistent with a phone transmitting toward a tower — not a "
            "unique handset ID; confirm with the airplane-mode test.",
        )

    # --- GPS: any energy is abnormal ---------------------------------------
    if "gps" in tags or "gps" in cats:
        evidence.append("energy in the GPS band (should be below noise)")
        next_checks = ["DF immediately", "Physical search for repeater/spoofer"]
        return _out(
            "gps_anomaly", "GPS-band energy",
            _HIGH,
            "GPS receivers do not transmit — any visible energy here is "
            "consistent with a repeater, jammer, or spoofer.",
        )

    # --- DECT cordless -----------------------------------------------------
    if "dect" in tags:
        evidence.append("DECT cordless-phone allocation")
        next_checks = ["Unplug cordless bases and re-sweep", "Inventory cordless handsets"]
        return _out(
            "dect_cordless", "Cordless phone (DECT)",
            _HIGH,
            "Consistent with DECT cordless phone infrastructure (or a DECT-band bug). "
            "Unplug known bases to see what remains.",
        )

    is_24 = _in_band(center, 2400.0, 2500.0) or "ism_2400" in tags
    is_58 = _in_band(center, 5150.0, 5925.0) or "ism_5800" in tags
    is_ism_sub = (
        _in_band(center, 300.0, 960.0)
        or tags & {"ism_433", "ism_915", "ism_868"}
    )
    wide = bw is not None and bw >= 1000.0
    very_wide = bw is not None and bw >= 4000.0
    narrow = bw is not None and bw <= 30.0
    mid = bw is not None and 30.0 < bw < 1000.0
    burst_ms = _f((feats or {}).get("burst_period_ms"))
    # multi-pass duty proxy already in persistence; surface in evidence later

    # --- video / continuous wide ISM (cam-like) ----------------------------
    # Steady wide emission in 2.4/5.8 is the classic AV-sender / FPV / Wi-Fi
    # camera LOOK. Not a brand. Requires high duty + width.
    # Cam-class ladder: base → +IQ → (+ inventory handled in next_step)
    if (is_24 or is_58) and wide and cont == "continuous":
        evidence.append(f"{'5.8' if is_58 else '2.4'} GHz ISM")
        evidence.append(f"wide (~{bw:.0f} kHz) continuous emission")
        conf = _MED
        if mod in ("analog_video", "ofdm_wideband", "continuous_digital"):
            evidence.append(f"IQ class {mod}")
            conf = _HIGH
        if duty is not None and duty >= 0.95:
            evidence.append(f"duty {duty:.0%} (near-continuous)")
            conf = min(0.92, conf + 0.08)
        if snr is not None and snr >= 20:
            evidence.append(f"strong SNR {snr:.0f} dB")
            conf = min(0.92, conf + 0.05)
        next_checks = [
            "Match against Wi-Fi inventory (BSSID / SSID / OUI)",
            "Power-cycle known TVs/cameras/APs; see what dies",
            "DF walk to the fixture; inventory what is left when known gear is off",
        ]
        return _out(
            "video_like", "Video / continuous wideband (cam-class)",
            conf,
            "Consistent with a continuous video-class or always-on wideband link "
            "(Wi-Fi camera, analog AV sender, FPV VTX) — NOT a unique camera serial. "
            "Confirm by inventory subtraction and DF.",
            checks=next_checks,
        )

    # --- Wi-Fi data class (wide, bursty/intermittent 2.4/5) -----------------
    if (is_24 or is_58) and wide and cont in ("bursty", "intermittent"):
        evidence.append(f"{'5.8' if is_58 else '2.4'} GHz, ~{bw:.0f} kHz, {cont}")
        if mod == "ofdm_wideband":
            evidence.append("OFDM-like IQ shape")
            conf = _HIGH
        else:
            conf = _MED
        next_checks = [
            "Wi-Fi scan: match channel/BSSID",
            "Mark client-owned APs/clients; flag loud unowned",
        ]
        return _out(
            "wifi_data", "Wi-Fi / wideband data",
            conf,
            "Consistent with Wi-Fi or similar packetized wideband data. "
            "The identifier lives in the Wi-Fi inventory (BSSID, SSID, vendor OUI), "
            "not in the spectrum peak alone.",
            checks=next_checks,
        )

    # --- BLE class (2.4, relatively narrow, bursty) ------------------------
    if is_24 and not very_wide and cont in ("bursty", "intermittent") and (
            bw is None or bw <= 2000.0):
        # BLE ads are ~1 MHz channels, low duty; without IQ this is soft
        if bw is not None and bw <= 2000.0:
            evidence.append(f"2.4 GHz, {cont}, width ~{bw:.0f} kHz")
            conf = _MED if (bw <= 1200.0) else _LOW
            next_checks = [
                "Run BLE inventory (MAC / company id / tracker flags)",
                "Look for AirTag/Tile/Find-My style trackers first",
            ]
            return _out(
                "ble_class", "Bluetooth-class (2.4 GHz)",
                conf,
                "Consistent with Bluetooth / BLE-class traffic. Real IDs "
                "(MAC type, vendor, tracker signatures) come from the BLE "
                "inventory, not the RF envelope.",
                checks=next_checks,
            )

    # --- key-fob / remote / sensor (sub-GHz OOK/FSK, rare bursts) ----------
    if is_ism_sub and (cont == "bursty" or (duty is not None and duty < 0.25)):
        evidence.append(f"sub-GHz ISM @ {center:.3f} MHz")
        evidence.append(f"{cont} / low duty")
        if duty is not None:
            evidence.append(f"duty/persistence {duty:.0%}")
        if narrow or mid or bw is None:
            if mod in ("ook", "fsk"):
                evidence.append(f"IQ class {mod}")
                conf = _HIGH
            else:
                conf = _MED
            if burst_ms is not None and burst_ms > 50:
                evidence.append(f"IQ burst spacing ~{burst_ms:.0f} ms")
            next_checks = [
                "Press known client keyfobs/remotes; watch which bursts match",
                "IDENTIFY for OOK/FSK signature match",
                "If it fires with no one present → investigate",
            ]
            return _out(
                "keyfob_like", "Key fob / remote / sensor",
                conf,
                "Consistent with a short-burst remote, alarm sensor, or key fob "
                "(OOK/FSK class). Brand is rarely in the clear — confirm by "
                "correlating button presses and inventory of known remotes.",
                checks=next_checks,
            )

    # --- HID keepalive (mice/keyboards: periodic short bursts, often 2.4) --
    # Heuristic: mid-narrow bandwidth, intermittent; IQ burst period ~5–50 ms helps
    if is_24 and cont == "intermittent" and bw is not None and 200.0 <= bw <= 3000.0:
        evidence.append("2.4 GHz intermittent mid-width (proprietary 2.4 / HID class)")
        conf = _LOW + 0.1
        if burst_ms is not None and 5.0 <= burst_ms <= 50.0:
            evidence.append(f"IQ burst period ~{burst_ms:.1f} ms (keepalive-like)")
            conf = _MED
        next_checks = [
            "Inventory wireless mice/keyboards; power them off and re-check",
            "BLE/Wi-Fi inventory for dongles; DF to the desk",
        ]
        return _out(
            "hid_keepalive", "Wireless mouse / keyboard class",
            conf,
            "Consistent with a proprietary 2.4 GHz HID link (mouse/keyboard dongle) "
            "— spectrally similar to some nRF24 bugs. Confirm by powering off known "
            "peripherals; anything left is a lead.",
            checks=next_checks,
        )

    # --- analog audio bug class (narrow, persistent, sub-GHz / LMR) --------
    if narrow and cont in ("continuous", "intermittent") and center < 1000.0:
        evidence.append(f"narrow (~{bw:.0f} kHz) @ {center:.3f} MHz")
        if duty is not None and duty >= 0.3:
            evidence.append(f"persistence/duty {duty:.0%}")
        if mod == "analog_voice":
            evidence.append("IQ class analog_voice")
            conf = _HIGH
        else:
            conf = _MED
        next_checks = [
            "LISTEN in NFM (admin/audited if content)",
            "DF to fixture; check for planted transmitter",
        ]
        return _out(
            "analog_audio_bug", "Analog audio / narrow FM class",
            conf,
            "Consistent with a narrowband analog voice link (wireless mic or "
            "classic audio bug class). There is usually NO digital identifier — "
            "proof is listen shape + DF + still-present when known mics are off.",
            checks=next_checks,
        )

    # --- continuous narrow beacon ------------------------------------------
    if narrow and cont == "continuous":
        evidence.append(f"continuous narrow carrier @ {center:.3f} MHz")
        next_checks = ["LISTEN", "DF", "Check for beacons / telemetry / bugs"]
        return _out(
            "narrow_beacon", "Narrow continuous carrier",
            _MED,
            "Consistent with a steady beacon, telemetry, or unmodulated carrier. "
            "IDENTIFY + LISTEN to separate benign telemetry from a planted device.",
            checks=next_checks,
        )

    # --- fall through: band-only soft hint ---------------------------------
    if is_24 or is_58:
        evidence.append(f"{'5 GHz' if is_58 else '2.4 GHz'} ISM energy")
        return _out(
            "ism_unclassified", "ISM-band emission",
            _LOW,
            "In an unlicensed ISM band — could be Wi-Fi, Bluetooth, video, or "
            "proprietary. Run IDENTIFY + wireless inventory for real IDs.",
            checks=["Wi-Fi/BLE inventory", "IDENTIFY signature", "DF if it persists"],
        )

    if snr is not None and snr >= 8:
        evidence.append(f"SNR ~{snr:.0f} dB, {cont}, bw={bw}")
        return _out(
            "unknown", "Unclassified emission",
            _LOW,
            "No strong behavioral match yet — use IDENTIFY (modulation), "
            "LISTEN, inventory, and DF. Spectrum alone rarely names the product.",
            checks=["IDENTIFY", "LISTEN", "Inventory", "DF"],
        )

    return _out(
        "unknown", "Unclassified",
        0.0,
        "Not enough shape yet to guess a device class.",
        checks=["Re-sweep closer / higher gain", "IDENTIFY with SDR IQ"],
    )


def behavior_from_identify(center_mhz: float, bw_khz, feats: dict | None) -> dict:
    """Convenience wrapper used by /api/identify."""
    return classify_behavior(
        {"center_mhz": center_mhz, "bw_khz": bw_khz,
         "persistence": (feats or {}).get("duty_cycle")},
        feats,
    )
