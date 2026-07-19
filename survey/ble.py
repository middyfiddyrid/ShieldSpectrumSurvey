"""Bluetooth Low Energy enrichment — squeeze the maximum identifying detail out of
every advertiser we hear, and ALWAYS pin down the MAC address type.

WHY THIS EXISTS
===============
`inventory.scan_ble` (via bleak) hands us a MAC, an RSSI, maybe a name, and some
raw manufacturer bytes. On its own that is nearly useless to a client ("what IS
b8:27:eb:… and should I worry?"). This module turns one advertisement into a full
plain-language dossier:

  * MAC ADDRESS TYPE — public (real global vendor OUI) vs one of the three random
    kinds (static / resolvable-private / non-resolvable). This is the single most
    important BLE-TSCM fact: a *resolvable-private* address means the device is
    ROTATING its identity every ~15 min (a phone, a watch, an AirTag in Find-My) —
    you cannot track it by MAC, and its vendor is hidden BY DESIGN. A *public*
    address exposes the true manufacturer via the OUI.
  * VENDOR — OUI lookup for public addresses; the Bluetooth-SIG company id in the
    manufacturer data for everyone (a phone with a random MAC still says "Apple").
  * TRACKERS — AirTag / Find My / Tile / Samsung SmartTag / Chipolo detection, the
    highest-value BLE find on a sweep (these are what gets planted on a person/car).
  * SERVICES, TX power, appearance, connectable, advertised name.

Everything here is PURE LOGIC over the fields bleak already gives us — no radio, no
platform calls — so it is fully unit-tested in tests/test_ble.py. The address-type
math is derived from the MAC bytes and is deterministic regardless of OS; the
optional `address_type` hint (when the platform reports it) only CONFIRMS it.

Reference tables are a curated, TSCM-relevant SUBSET (the full IEEE OUI list is
~35k rows and the BT-SIG company list ~3k). Unknown ids are reported honestly as
"unrecognized (id 0x….)", never guessed. Extend via survey/ble_extra.json if present.
"""

from __future__ import annotations

import json
import os

# ---------------------------------------------------------------------------
# Curated reference tables (TSCM-relevant subset; unknown ids reported honestly)
# ---------------------------------------------------------------------------

# Bluetooth SIG "Company Identifiers" (manufacturer-data prefix, little-endian u16).
COMPANY_IDS = {
    0x004C: "Apple",
    0x0006: "Microsoft",
    0x00E0: "Google",
    0x0075: "Samsung Electronics",
    0x0157: "Tile",
    0x0001: "Ericsson",
    0x000F: "Broadcom",
    0x0059: "Nordic Semiconductor",
    0x0499: "Ruuvi Innovations",
    0x05A7: "Sonos",
    0x0110: "Chipolo",
    0x0171: "Amazon",
    0x00D2: "Fitbit",
    0x0087: "Garmin",
    0x03DA: "Samsung SmartThings",
    0x0060: "Logitech",
    0x004F: "GN Netcom (Jabra)",
    0x038F: "Xiaomi",
    0x02E5: "Espressif (ESP32)",
    0x0500: "Espressif",
    0x0131: "Cypress",
    0x0072: "Realtek",
    0x0117: "Bose",
    0x00C4: "LG Electronics",
    # HID / keyboards / mice / remotes (TSCM: nRF24-class look-alikes)
    0x005D: "Razer",
    0x00E8: "Google (Chromecast/Nest remote class)",
    0x0822: "MuteMe / accessory",
    0x0D28: "SteelSeries",
    0x03F0: "HP Inc",
    0x0002: "Intel",
    0x00A7: "Harman International",
    0x008A: "Polar Electro",
    0x0154: "Swatch Group",
    0x01D7: "Qualcomm",
    0x02B0: "Tesla Motors",
    0x05C1: "Vizio",
    0x06D5: "Roku",
    0x0A7C: "Anker",
}

# Company ids that often mean keyboard/mouse/remote (not exhaustive)
HID_COMPANY_IDS = {
    0x0060,  # Logitech
    0x005D,  # Razer
    0x0006,  # Microsoft
    0x03F0,  # HP
    0x0D28,  # SteelSeries
}

AV_REMOTE_COMPANY_IDS = {
    0x004C,  # Apple TV remote / Continuity
    0x00E0,  # Google
    0x0171,  # Amazon Fire
    0x06D5,  # Roku
    0x05C1,  # Vizio
    0x00C4,  # LG
    0x0075,  # Samsung
}

# First-3-octet OUI prefixes (uppercase, colon-less) -> vendor. Meaningful ONLY for
# PUBLIC addresses; a random address's leading bytes are not a real OUI.
OUI_VENDORS = {
    "B827EB": "Raspberry Pi Foundation",
    "DCA632": "Raspberry Pi (Trading)",
    "E45F01": "Raspberry Pi (Trading)",
    "001A11": "Google",
    "3C5AB4": "Google",
    "F4F5D8": "Google",
    "AC233F": "Shenzhen Minew (BLE beacons)",
    "0CF3EE": "Espressif (ESP32)",
    "246F28": "Espressif (ESP32)",
    "A4CF12": "Espressif (ESP32)",
    "8C4B14": "Sonos",
    "5CAAFD": "Sonos",
    "B8278E": "Samsung",
    "002537": "Samsung",
    "F0C77F": "Amazon (Ring/Echo)",
    "68C63A": "Espressif",
    "D0039B": "Wyze Labs (camera)",
    "2CAA8E": "Wyze Labs (camera)",
    "7C78B2": "Reolink (camera)",
    "EC71DB": "Shenzhen Reecam / IP cam",
    "001788": "Philips Hue",
    "00170088": "Philips Hue",
}

# 16-bit assigned service UUIDs (as 4-hex, uppercase) -> name; TSCM-relevant first.
SERVICE_UUIDS = {
    "180F": "Battery",
    "180A": "Device Information",
    "1812": "HID (Human Interface Device)",
    "1800": "Generic Access",
    "1801": "Generic Attribute",
    "180D": "Heart Rate",
    "181A": "Environmental Sensing",
    "FE2C": "Google Fast Pair / Find My Device class",
    "FD6F": "Apple Find My network class",
    "FE9F": "Google Eddystone class",
    "FEE0": "Xiaomi",
    "FFF0": "Generic proprietary",
    "1800": "Generic Access",
    "1801": "Generic Attribute",
    "1811": "Alert Notification",
    "1812": "HID (keyboard/mouse/remote)",
    "110B": "Audio Sink (A2DP)",
    "111E": "Handsfree",
    "FD5A": "Samsung SmartTag (Find)",
    "FD44": "Apple (Find My / AirTag)",
    "FEED": "Tile tracker",
    "FE9F": "Google / Nest",
    "FDCD": "Xiaomi Mi devices",
    "FE07": "Sonos",
    "FEAA": "Google Eddystone beacon",
    "181C": "User Data",
    "FE95": "Xiaomi",
}

# Bluetooth "Appearance" category (top 6 bits of the 16-bit value) -> label.
APPEARANCE_CATEGORY = {
    0: "Unknown", 1: "Phone", 2: "Computer", 3: "Watch", 4: "Clock", 5: "Display",
    6: "Remote control", 7: "Eye-glasses", 8: "Tag", 9: "Keyring", 10: "Media player",
    11: "Barcode scanner", 15: "HID (keyboard/mouse)", 20: "Sensor", 21: "Light fixture",
    22: "Fan", 23: "HVAC", 24: "Air conditioning", 32: "Wearable audio (earbuds)",
    81: "Motion sensor", 0x0A: "Media player",
}

# Apple "continuity" manufacturer-data first byte -> what it advertises.
APPLE_MSG_TYPES = {
    0x02: "iBeacon",
    0x05: "AirDrop",
    0x07: "AirPods / proximity pairing",
    0x09: "AirPlay target",
    0x0A: "AirPlay source",
    0x0C: "Handoff",
    0x0D: "Wi-Fi settings",
    0x0E: "Instant Hotspot",
    0x0F: "Wi-Fi Join",
    0x10: "Nearby (activity)",
    0x12: "Find My (offline finding — AirTag / lost device)",
}

# Company ids whose mere presence indicates a location TRACKER (highest TSCM value).
TRACKER_COMPANY = {0x0157: "Tile", 0x0110: "Chipolo"}
TRACKER_SERVICE = {"FD5A": "Samsung SmartTag", "FD44": "Apple AirTag / Find My",
                   "FEED": "Tile"}


def _load_extra():
    """Optionally fold in an operator-maintained survey/ble_extra.json with keys
    'company_ids' / 'oui_vendors' / 'service_uuids' (string keys). Silent if absent."""
    path = os.path.join(os.path.dirname(__file__), "ble_extra.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            extra = json.load(fh) or {}
    except (FileNotFoundError, ValueError):
        return
    for cid, name in (extra.get("company_ids") or {}).items():
        COMPANY_IDS.setdefault(int(cid, 0), name)
    for oui, name in (extra.get("oui_vendors") or {}).items():
        OUI_VENDORS.setdefault(oui.replace(":", "").upper(), name)
    for uu, name in (extra.get("service_uuids") or {}).items():
        SERVICE_UUIDS.setdefault(uu.upper(), name)


_load_extra()


# ---------------------------------------------------------------------------
# MAC address classification — the core BLE-TSCM fact
# ---------------------------------------------------------------------------

def _mac_bytes(mac: str) -> list[int]:
    hexs = mac.replace("-", ":").split(":")
    if len(hexs) != 6:
        raise ValueError(f"not a 48-bit MAC: {mac!r}")
    return [int(h, 16) for h in hexs]


def classify_mac(mac: str, address_type: str | None = None) -> dict:
    """Determine the BLE address kind and, for public addresses, the vendor OUI.

    Returns {address_kind, kind_label, is_random, rotating, oui, oui_vendor,
    inferred, note}. `address_type` ('public'|'random') is the platform's report
    when available; when None we infer from the address bits and say so.

    BLE random-address sub-type lives in the top two bits of the most-significant
    octet: 11=static, 01=resolvable-private (rotating), 00=non-resolvable. A
    public address is a globally-unique IEEE OUI (locally-administered bit clear)."""
    b = _mac_bytes(mac)
    msb = b[0]
    oui = "".join(f"{x:02X}" for x in b[:3])
    top2 = (msb & 0xC0) >> 6
    locally_admin = bool(msb & 0x02)

    # decide public vs random
    if address_type == "public":
        is_random = False
    elif address_type == "random":
        is_random = True
    else:
        # infer: a resolvable/non-resolvable private address has top2 in {01,00} AND
        # is locally administered; a global OUI is not locally administered.
        is_random = locally_admin or top2 in (0b01, 0b00)
    inferred = address_type not in ("public", "random")

    if not is_random:
        vendor = OUI_VENDORS.get(oui)
        return {
            "address_kind": "public", "kind_label": "public (global vendor address)",
            "is_random": False, "rotating": False, "oui": oui, "oui_vendor": vendor,
            "inferred": inferred,
            "note": (f"Public address — the first three bytes ({':'.join(oui[i:i+2] for i in range(0,6,2))}) "
                     f"are a real manufacturer OUI"
                     + (f" registered to {vendor}." if vendor else ", not in our curated OUI list.")),
        }

    if top2 == 0b01:
        kind, label = "random_resolvable", "resolvable-private (rotating identity)"
        rotating = True
        note = ("Resolvable-private random address — this device ROTATES its MAC "
                "(~every 15 min) to defeat tracking. Phones, watches, and AirTags in "
                "Find-My use this. You cannot follow it by MAC across a sweep, and its "
                "true vendor is hidden by design (check the manufacturer id below).")
    elif top2 == 0b00:
        kind, label = "random_nonresolvable", "non-resolvable-private (rotating, anonymous)"
        rotating = True
        note = ("Non-resolvable random address — fully anonymous, changes frequently, "
                "no vendor recoverable from the address.")
    elif top2 == 0b11:
        kind, label = "random_static", "static-random (fixed but not a vendor OUI)"
        rotating = False
        note = ("Static-random address — stable for the device's lifetime (or until "
                "reboot) but NOT a manufacturer OUI, so the address alone names no vendor. "
                "Common on BLE beacons/sensors. Trackable while it stays put.")
    else:
        kind, label = "random_reserved", "random (reserved bit pattern)"
        rotating = False
        note = "Random address with a reserved top-bit pattern."

    return {
        "address_kind": kind, "kind_label": label, "is_random": True,
        "rotating": rotating, "oui": None, "oui_vendor": None, "inferred": inferred,
        "note": note,
    }


# ---------------------------------------------------------------------------
# Advertisement payload decode
# ---------------------------------------------------------------------------

def decode_manufacturer(company_id: int, data: bytes | None) -> dict:
    """Name the manufacturer and, for Apple, decode the continuity sub-type (which
    is what tells an AirTag / Find-My beacon apart from AirPods)."""
    company = COMPANY_IDS.get(company_id, f"unrecognized (id 0x{company_id:04X})")
    hints: list[str] = []
    is_tracker = company_id in TRACKER_COMPANY
    if is_tracker:
        hints.append(f"{TRACKER_COMPANY[company_id]} location tracker")
    if company_id == 0x004C and data:
        msg = data[0]
        desc = APPLE_MSG_TYPES.get(msg)
        if desc:
            hints.append(f"Apple {desc}")
        if msg == 0x12:                     # Find My offline-finding
            is_tracker = True
            hints.append("possible AirTag / lost-item beacon")
    if company_id == 0x0006 and data:       # Microsoft "swift pair" / CDP
        hints.append("Microsoft device (Swift Pair / nearby)")
    return {"company_id": company_id, "company": company, "hints": hints,
            "is_tracker": is_tracker}


def service_names(uuids) -> list[dict]:
    """Map advertised service UUIDs to names. Accepts full 128-bit UUID strings or
    16-bit shorts; the 16-bit assigned range lives in xxxxXXXX-0000-1000-8000-...."""
    out = []
    for u in uuids or []:
        s = str(u).upper().replace("0X", "")
        short = None
        if len(s) >= 8 and "-" in s:                       # 128-bit form
            short = s[4:8]
        elif len(s) == 4:
            short = s
        elif len(s) == 8:
            short = s[4:8]
        name = SERVICE_UUIDS.get(short) if short else None
        out.append({"uuid": short or s, "name": name, "tracker": short in TRACKER_SERVICE})
    return out


def appearance_label(appearance: int | None) -> str | None:
    if appearance is None:
        return None
    return APPEARANCE_CATEGORY.get((appearance >> 6) & 0x3FF, f"category {appearance >> 6}")


def rssi_proximity(rssi_dbm) -> str:
    if rssi_dbm is None:
        return "unknown"
    if rssi_dbm >= -50:
        return "very close (same room / on you)"
    if rssi_dbm >= -70:
        return "near (this room or next)"
    if rssi_dbm >= -85:
        return "moderate (elsewhere in the building)"
    return "far / weak"


# ---------------------------------------------------------------------------
# One advertisement -> full dossier
# ---------------------------------------------------------------------------

def summarize_advertisement(adv: dict) -> dict:
    """Turn one heard advertisement into an enriched, client-readable record.

    `adv` fields (all optional except mac): mac, name, rssi_dbm, address_type,
    tx_power, appearance, connectable, manufacturer_data {company_id:int -> bytes},
    service_uuids [str], service_data {uuid:bytes}. Returns the classified address,
    decoded manufacturers/services, tracker flag, a compact `info` string (for the
    existing inventory table), a plain-language `summary`, and a `tscm_note`."""
    mac = adv["mac"]
    macinfo = classify_mac(mac, adv.get("address_type"))

    mfrs = []
    is_tracker = False
    is_hid = False
    is_av_remote = False
    for cid, data in (adv.get("manufacturer_data") or {}).items():
        d = decode_manufacturer(int(cid), bytes(data) if data else b"")
        mfrs.append(d)
        is_tracker = is_tracker or d["is_tracker"]
        try:
            icid = int(cid)
        except (TypeError, ValueError):
            icid = -1
        if icid in HID_COMPANY_IDS:
            is_hid = True
        if icid in AV_REMOTE_COMPANY_IDS:
            is_av_remote = True

    svcs = service_names(adv.get("service_uuids"))
    if any(s["tracker"] for s in svcs):
        is_tracker = True
    if any("HID" in (s.get("name") or "") for s in svcs):
        is_hid = True

    appearance = appearance_label(adv.get("appearance"))
    name = adv.get("name") or ""
    rssi = adv.get("rssi_dbm")

    # best single vendor guess: public OUI first, else the manufacturer id
    vendor = macinfo.get("oui_vendor")
    if not vendor and mfrs:
        vendor = next((m["company"] for m in mfrs if not m["company"].startswith("unrecognized")), None)

    # compact one-liner for the inventory "Info" column
    bits = [macinfo["kind_label"]]
    if vendor:
        bits.append(vendor)
    if is_tracker:
        bits.append("⚑ TRACKER")
    if is_hid:
        bits.append("⌨ HID")
    if is_av_remote:
        bits.append("📺 AV remote class")
    hinttxt = [h for m in mfrs for h in m["hints"]]
    if hinttxt:
        bits.append(" / ".join(hinttxt))
    named_svcs = [s["name"] for s in svcs if s["name"]]
    if named_svcs:
        bits.append("svc: " + ", ".join(named_svcs))
    if appearance and appearance != "Unknown":
        bits.append(appearance)
    info = " · ".join(bits)

    # plain-language summary + TSCM note
    who = vendor or (appearance if appearance and appearance != "Unknown" else None) or "unidentified device"
    summary_parts = [f"{who}" + (f" '{name}'" if name else "")]
    summary_parts.append(macinfo["kind_label"])
    if rssi is not None:
        summary_parts.append(f"{rssi:.0f} dBm — {rssi_proximity(rssi)}")
    summary = "; ".join(summary_parts) + "."

    tscm = []
    if is_tracker:
        tscm.append("LOCATION TRACKER present — the top BLE concern on a sweep. "
                    "Confirm whether the client owns it; an unowned AirTag/Tile/SmartTag "
                    "on a person or vehicle is a stalking device.")
    if macinfo["rotating"]:
        tscm.append("Rotating (private) address — cannot be tracked by MAC and its "
                    "vendor is hidden; identify it by manufacturer id, name, and behaviour.")
    if not name and rssi is not None and rssi >= -60 and not is_tracker:
        tscm.append("Loud, un-named advertiser close by — worth identifying by hand.")
    tscm_note = " ".join(tscm)

    return {
        "mac": mac,
        "address_kind": macinfo["address_kind"],
        "address_kind_label": macinfo["kind_label"],
        "address_inferred": macinfo["inferred"],
        "rotating": macinfo["rotating"],
        "oui": macinfo["oui"],
        "vendor": vendor,
        "name": name,
        "rssi_dbm": rssi,
        "proximity": rssi_proximity(rssi),
        "tx_power": adv.get("tx_power"),
        "connectable": adv.get("connectable"),
        "appearance": appearance,
        "manufacturers": mfrs,
        "services": svcs,
        "is_tracker": is_tracker,
        "is_hid": is_hid,
        "is_av_remote": is_av_remote,
        "info": info,
        "summary": summary,
        "tscm_note": tscm_note,
        "address_note": macinfo["note"],
    }
