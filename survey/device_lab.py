"""Device Capture Lab — guided characterization of a KNOWN test/bench device.

Walk the tech through: photo → identify/guess freqs → antenna → three-range RF
captures (10 ft / next room / near-field) → optional TX interaction (ISM) →
demod on the test article → commit package.

This is for devices YOU are testing on the bench or with the client present —
not bulk ambient interception. Demod is intentional on the DUT.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from typing import Any

# ---------------------------------------------------------------------------
# Curated device-type catalog: expected RF classes (not product serials)
# ---------------------------------------------------------------------------

DEVICE_CATALOG: list[dict[str, Any]] = [
    {
        "id": "keyfob_315",
        "labels": ["key fob", "car remote", "garage remote", "door remote", "fob"],
        "name": "Key fob / remote (315 MHz class)",
        "freq_guess_mhz": [315.0, 318.0, 390.0],
        "bw_khz_typical": 50,
        "bands": "sub-GHz ISM / remote",
        "modulation": "ook/fsk short bursts",
        "notes": "Press button during near-field capture. Rare short bursts.",
    },
    {
        "id": "keyfob_433",
        "labels": ["433", "ism remote", "alarm sensor", "weather station"],
        "name": "433 MHz remote / sensor class",
        "freq_guess_mhz": [433.92, 434.0, 433.0],
        "bw_khz_typical": 50,
        "bands": "ISM 433",
        "modulation": "ook/fsk",
        "notes": "Common EU/Asia remotes and sensors.",
    },
    {
        "id": "keyfob_915",
        "labels": ["915", "lora", "ism 900"],
        "name": "902–928 MHz ISM device class",
        "freq_guess_mhz": [915.0, 902.0, 928.0],
        "bw_khz_typical": 125,
        "bands": "ISM 915",
        "modulation": "fsk/ook/lora-class",
        "notes": "US ISM; good band for authorized TX probe (Part 15).",
    },
    {
        "id": "wifi_camera",
        "labels": ["wifi camera", "ip camera", "security cam", "nest cam", "wyze", "ring"],
        "name": "Wi-Fi camera class",
        "freq_guess_mhz": [2412.0, 2437.0, 2462.0, 5180.0, 5745.0],
        "bw_khz_typical": 20000,
        "bands": "2.4 / 5 GHz Wi-Fi",
        "modulation": "ofdm wideband",
        "notes": "Continuous or bursty wide energy; inventory BSSID helps.",
    },
    {
        "id": "analog_av",
        "labels": ["analog camera", "wireless av", "fpv", "baby monitor analog", "av sender"],
        "name": "Analog AV / FPV video class",
        "freq_guess_mhz": [900.0, 1200.0, 2400.0, 5800.0, 5865.0],
        "bw_khz_typical": 8000,
        "bands": "900 / 1.2 / 2.4 / 5.8 GHz video",
        "modulation": "wide FM / analog video",
        "notes": "Near-continuous wide carrier when powered.",
    },
    {
        "id": "ble_tracker",
        "labels": ["airtag", "tile", "smarttag", "tracker", "find my"],
        "name": "BLE tracker / Find-My class",
        "freq_guess_mhz": [2402.0, 2426.0, 2480.0],
        "bw_khz_typical": 1000,
        "bands": "2.4 GHz BLE",
        "modulation": "ble advertising bursts",
        "notes": "Use Bluetooth Detail Sweep; RF envelope alone is weak.",
    },
    {
        "id": "ble_hid",
        "labels": ["mouse", "keyboard", "dongle", "logitech", "wireless mouse"],
        "name": "Wireless mouse/keyboard (HID) class",
        "freq_guess_mhz": [2405.0, 2425.0, 2450.0, 2475.0],
        "bw_khz_typical": 1000,
        "bands": "2.4 GHz proprietary / BLE",
        "modulation": "packetized keepalives",
        "notes": "Periodic keepalives; power off to confirm.",
    },
    {
        "id": "dect",
        "labels": ["cordless phone", "dect", "handset"],
        "name": "DECT cordless class",
        "freq_guess_mhz": [1880.0, 1900.0, 1920.0, 1921.536],
        "bw_khz_typical": 1728,
        "bands": "DECT 1880–1930 MHz",
        "modulation": "tdma digital",
        "notes": "Unplug bases to prove attribution.",
    },
    {
        "id": "wireless_mic",
        "labels": ["wireless mic", "lav", "ifb", "bodypack"],
        "name": "Wireless microphone class",
        "freq_guess_mhz": [470.0, 518.0, 600.0, 650.0, 900.0],
        "bw_khz_typical": 200,
        "bands": "UHF TV / ISM depending on model",
        "modulation": "narrow FM / digital wireless",
        "notes": "Listen NFM only on intentional test articles.",
    },
    {
        "id": "cellular_modem",
        "labels": ["cellular", "lte modem", "hotspot", "phone", "5g"],
        "name": "Cellular phone/modem class",
        "freq_guess_mhz": [700.0, 850.0, 1700.0, 1900.0, 2100.0],
        "bw_khz_typical": 5000,
        "bands": "cellular uplink blocks (location-dependent)",
        "modulation": "cellular uplink bursts",
        "notes": "No content decode. Use airplane-mode test for phones.",
    },
    {
        "id": "wifi_ap",
        "labels": ["router", "access point", "mesh", "eero", "wifi ap"],
        "name": "Wi-Fi AP / mesh class",
        "freq_guess_mhz": [2412.0, 2437.0, 2462.0, 5180.0, 5745.0],
        "bw_khz_typical": 20000,
        "bands": "2.4 / 5 / 6 GHz",
        "modulation": "ofdm",
        "notes": "Scan Wi-Fi inventory for BSSID/SSID/OUI.",
    },
    {
        "id": "unknown_box",
        "labels": ["unknown", "box", "device", "gadget", "bug", "transmitter"],
        "name": "Unknown / generic transmitter",
        "freq_guess_mhz": [433.92, 915.0, 2437.0],
        "bw_khz_typical": 100,
        "bands": "start ISM then expand",
        "modulation": "unknown — IDENTIFY after capture",
        "notes": "Photo + multi-distance RF; correct freq when energy appears.",
    },
]

# LibreSDR / B220 front-panel: RXA = right (ch0), RXB = left (ch1)
# Operator doctrine: RXA = omni, RXB = directional (LPDA/Yagi).
LIBRE_PORTS = {
    "RXA": {
        "id": "RXA",
        "side": "right",  # channel 0
        "label": "Libre RXA (right)",
        "sma_hint": "RX2-A or TX/RX-A receive side",
        "role": "omni",
        "default_antenna": "omni_whip",
        "blurb": "Wide coverage / room survey — use an omni whip or rubber duck.",
    },
    "RXB": {
        "id": "RXB",
        "side": "left",  # channel 1
        "label": "Libre RXB (left)",
        "sma_hint": "RX2-B",
        "role": "directional",
        "default_antenna": "lpda",
        "blurb": "Bearing / DF — use LPDA, Yagi, or panel; peak toward the DUT.",
    },
}

# Far-field antennas (pick one per port on the antenna setup step)
OMNI_ANTENNAS = [
    {"id": "omni_whip", "name": "Whip / monopole (stock)", "kind": "omni"},
    {"id": "omni_duck_24", "name": "Rubber duck 2.4 GHz", "kind": "omni"},
    {"id": "omni_duck_dual", "name": "Rubber duck dual-band 2.4/5", "kind": "omni"},
    {"id": "omni_discone", "name": "Discone / wideband omni", "kind": "omni"},
]

DIR_ANTENNAS = [
    {"id": "lpda", "name": "Log-periodic (LPDA)", "kind": "directional"},
    {"id": "yagi", "name": "Yagi", "kind": "directional"},
    {"id": "patch", "name": "Patch / panel", "kind": "directional"},
    {"id": "helix", "name": "Helix (circular)", "kind": "directional"},
]

# Near-field probes — tech assigns which physical probe is on the NF path
NEAR_FIELD_ANTENNAS = [
    {
        "id": "hfield_loop",
        "name": "H-field loop / sniffer loop",
        "use": "Magnetic near-field; PCB traces, coils, small transmitters.",
        "svg": "loop",
    },
    {
        "id": "efield_stub",
        "name": "E-field stub / pin probe",
        "use": "Electric near-field; close approach to enclosures / cables.",
        "svg": "stub",
    },
    {
        "id": "current_clamp",
        "name": "Current clamp / RF current probe",
        "use": "Common-mode on cables / power leads.",
        "svg": "clamp",
    },
    {
        "id": "nf_patch",
        "name": "Small patch / puck (close range)",
        "use": "Compact patch held ≤1 ft from DUT.",
        "svg": "patch",
    },
    {
        "id": "nf_whip_close",
        "name": "Short whip (hand-held close)",
        "use": "Quick close-in with a short monopole — not a true H-field probe.",
        "svg": "whip",
    },
    {
        "id": "other_nf",
        "name": "Other / custom NF probe",
        "use": "Describe in notes (DIY loop, commercial sniffer kit, …).",
        "svg": "other",
    },
]

# Back-compat for meta.antenna_types (flat list of names)
ANTENNA_TYPES = (
    [a["name"] for a in OMNI_ANTENNAS]
    + [a["name"] for a in DIR_ANTENNAS]
    + [a["name"] for a in NEAR_FIELD_ANTENNAS]
)

DISTANCES = (
    {"id": "ten_ft", "label": "10 feet",
     "default_port": "RXA", "default_mode": "omni",
     "instruction":
     "Place the DUT about 10 feet (3 m) from the OMNI on Libre RXA. Same room, "
     "line-of-sight if possible. Keep antenna upright; do not re-aim between presses."},
    {"id": "next_room", "label": "Next room over",
     "default_port": "RXA", "default_mode": "omni",
     "instruction":
     "Move so at least one wall is between the DUT and the OMNI on Libre RXA. "
     "Optional: also take a bearing with Libre RXB (directional) and note peak heading."},
    {"id": "near_field", "label": "Near-field (≤ 1 foot)",
     "default_port": "RXB", "default_mode": "near_field",
     "instruction":
     "Assign a near-field probe (loop / stub / clamp / …) on the chosen port. "
     "Bring the probe within ~1 foot (30 cm) of the DUT. For remotes, press/activate now."},
)


def port_to_side(port_id: str) -> str:
    """RXA → right, RXB → left (UHD channel mapping)."""
    p = (port_id or "RXA").upper().replace(" ", "")
    if p in ("RXB", "LEFT", "B"):
        return "left"
    return "right"


def side_to_port(side: str) -> str:
    return "RXB" if (side or "").lower() == "left" else "RXA"


def spectrum_preview(iq, fs: float, max_bins: int = 256) -> dict:
    """Downsampled PSD for the lab UI (freqs_mhz relative + power_db)."""
    import numpy as np
    from .sweep.sdr_source import welch_psd
    iq = np.asarray(iq, dtype=np.complex64)
    if iq.size < 64:
        return {"freqs_mhz": [], "power_db": [], "peak_db": None, "floor_db": None}
    nper = min(1024, max(64, iq.size // 4))
    f_bb, psd = welch_psd(iq, fs, nperseg=nper)
    # downsample
    step = max(1, len(psd) // max_bins)
    f = f_bb[::step]
    p = psd[::step]
    return {
        "freqs_mhz": [round(float(x) / 1e6, 4) for x in f],  # baseband offset MHz
        "power_db": [round(float(x), 1) for x in p],
        "peak_db": round(float(np.max(p)), 1),
        "floor_db": round(float(np.median(p)), 1),
        "snr_db": round(float(np.max(p) - np.median(p)), 1),
    }


def lab_dir(base_dir: str) -> str:
    d = os.path.join(base_dir, "data", "device_lab")
    os.makedirs(d, exist_ok=True)
    return d


def _pkg_path(base_dir: str, pkg_id: str) -> str:
    return os.path.join(lab_dir(base_dir), f"{pkg_id}.json")


def list_packages(base_dir: str) -> list[dict]:
    out = []
    for name in sorted(os.listdir(lab_dir(base_dir))):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(lab_dir(base_dir), name), "r", encoding="utf-8") as fh:
                p = json.load(fh)
            out.append({
                "id": p.get("id"),
                "name": p.get("name") or p.get("device_name") or "",
                "status": p.get("status"),
                "created_utc": p.get("created_utc"),
                "center_mhz": p.get("center_mhz"),
            })
        except (OSError, ValueError):
            continue
    return out


def load_package(base_dir: str, pkg_id: str) -> dict | None:
    path = _pkg_path(base_dir, pkg_id)
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save_package(base_dir: str, pkg: dict) -> dict:
    pkg["updated_utc"] = time.time()
    path = _pkg_path(base_dir, pkg["id"])
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(pkg, fh, indent=2)
    return pkg


def new_package(radio: dict, session_id: int | None = None) -> dict:
    return {
        "id": uuid.uuid4().hex[:12],
        "status": "in_progress",  # in_progress | ready_to_commit | committed
        "created_utc": time.time(),
        "updated_utc": time.time(),
        "session_id": session_id,
        "step": "photo",
        "tech_log": [],
        "radio": radio or {},
        "antenna": "",
        "antenna_notes": "",
        "device_name": "",
        "device_description": "",  # free-text: what it is, ports, brand, how it behaves
        "device_type_id": "",
        "device_type_name": "",
        "photo_path": "",
        "photo_notes": "",
        # LibreSDR: RXA = omni (right), RXB = directional (left)
        "rxa_antenna": "omni_whip",
        "rxb_antenna": "lpda",
        "nf_antenna": "hfield_loop",
        "rx_port": "RXA",
        "rx_port_label": "Libre RXA — omni",
        "tx_port_label": "TX/RX-A for stimulate probe",
        "lookup": {},
        "center_mhz": None,
        "bw_khz": None,
        "freq_guesses_mhz": [],
        "freq_confirmed": False,
        "captures": {},  # distance_id -> {peak_dbm, features, spectrum_note, utc}
        "stimulate": None,
        "demod": None,
        "commit_notes": "",
    }


def log_step(pkg: dict, msg: str) -> None:
    pkg.setdefault("tech_log", []).append({
        "utc": time.time(), "msg": msg, "step": pkg.get("step")})


def match_catalog(text: str) -> list[dict]:
    """Score catalog entries from free-text / vision labels."""
    t = (text or "").lower()
    hits = []
    for ent in DEVICE_CATALOG:
        score = 0
        for lab in ent["labels"]:
            if lab in t:
                score += 10 + len(lab)
        if ent["name"].lower() in t:
            score += 15
        if score:
            hits.append({**ent, "score": score})
    hits.sort(key=lambda x: -x["score"])
    return hits


def populate_from_catalog(pkg: dict, catalog_id: str) -> dict:
    ent = next((e for e in DEVICE_CATALOG if e["id"] == catalog_id), None)
    if not ent:
        ent = next(e for e in DEVICE_CATALOG if e["id"] == "unknown_box")
    pkg["device_type_id"] = ent["id"]
    pkg["device_type_name"] = ent["name"]
    pkg["freq_guesses_mhz"] = list(ent["freq_guess_mhz"])
    if pkg.get("center_mhz") is None and ent["freq_guess_mhz"]:
        pkg["center_mhz"] = ent["freq_guess_mhz"][0]
    if pkg.get("bw_khz") is None:
        pkg["bw_khz"] = ent["bw_khz_typical"]
    pkg["lookup"] = {
        "bands": ent["bands"],
        "modulation": ent["modulation"],
        "notes": ent["notes"],
        "catalog_id": ent["id"],
    }
    return pkg


def save_photo_b64(base_dir: str, pkg_id: str, data_url: str) -> str:
    """Save data:image/jpeg;base64,... to device_lab photos dir. Returns rel path."""
    m = re.match(r"^data:image/(jpeg|jpg|png|webp);base64,(.+)$", data_url, re.I | re.S)
    if not m:
        raise ValueError("need a data:image/...;base64 photo")
    ext = m.group(1).lower().replace("jpg", "jpeg")
    if ext == "jpeg":
        ext = "jpg"
    import base64
    raw = base64.b64decode(m.group(2))
    if len(raw) < 100:
        raise ValueError("photo too small")
    if len(raw) > 8_000_000:
        raise ValueError("photo too large (max 8 MB)")
    photos = os.path.join(lab_dir(base_dir), "photos")
    os.makedirs(photos, exist_ok=True)
    fname = f"{pkg_id}_{int(time.time())}.{ext}"
    path = os.path.join(photos, fname)
    with open(path, "wb") as fh:
        fh.write(raw)
    return os.path.join("data", "device_lab", "photos", fname).replace("\\", "/")


def next_instruction(pkg: dict) -> dict:
    """What the tech should do right now."""
    step = pkg.get("step") or "photo"
    radio = pkg.get("radio") or {}
    rname = radio.get("model") or radio.get("driver") or "connected radio"
    guides = {
        "photo": {
            "title": "1 · Photograph the device",
            "do": [
                "Place the DUT on a plain surface with label/ports visible.",
                "Allow camera access when the browser asks.",
                "Capture a clear still (or upload a photo).",
                "Optionally type make/model if you already know it.",
            ],
            "why": "Visual ID + notes seed frequency guesses before RF.",
        },
        "identify": {
            "title": "2 · Identify / guess RF class",
            "do": [
                "Review catalog matches from the photo/description.",
                "Pick the best device class (or Unknown).",
                "Edit center frequency if you know the exact channel.",
            ],
            "why": "Wrong band wastes capture time; easy to correct next.",
        },
        "antenna": {
            "title": "3 · Libre ports & antennas",
            "do": [
                f"Radio: {rname}.",
                "Libre RXA = OMNI (whip/duck) — 10 ft & next-room default.",
                "Libre RXB = DIRECTIONAL (LPDA/Yagi) — aim at DUT when peaking.",
                "Near-field: pick a loop / stub / clamp and say which port it is on.",
                "TX probe later: TX/RX-A only.",
            ],
            "why": "Omni survey vs directional peak; NF probes are a third class.",
        },
        "freq_confirm": {
            "title": "4 · Confirm frequency",
            "do": [
                f"Proposed center: {pkg.get('center_mhz')} MHz "
                f"(bw ~{pkg.get('bw_khz')} kHz).",
                "Power the DUT ON. If remote, prepare to press during near-field.",
                "Correct frequency/BW now if the guess is wrong.",
            ],
            "why": "Captures tune here; spectrum display proves energy.",
        },
        "capture_ten_ft": {
            "title": "5 · Capture @ 10 feet (Libre RXA / omni)",
            "do": [DISTANCES[0]["instruction"],
                   "Watch the spectrum graph — a real peak means capture is working."],
            "why": "Same-room omni reference.",
        },
        "capture_next_room": {
            "title": "6 · Capture @ next room (Libre RXA / omni)",
            "do": [DISTANCES[1]["instruction"],
                   "Compare SNR bars to the 10 ft capture."],
            "why": "Through-wall attenuation.",
        },
        "capture_near_field": {
            "title": "7 · Near-field — assign NF probe",
            "do": [DISTANCES[2]["instruction"],
                   "Select the near-field antenna type (pics below), then capture.",
                   "Spectrum should jump if the probe is live."],
            "why": "Strongest signature for ID/demod.",
        },
        "stimulate": {
            "title": "8 · Optional TX interaction (ISM only)",
            "do": [
                "TX antenna on TX/RX-A only; keep RX on RXA or RXB separate.",
                "Authority required. Null ≠ clean bill.",
            ],
            "why": "Some devices react to stimulus; many will not.",
        },
        "demod": {
            "title": "9 · Demod / analyze the DUT",
            "do": [
                "Intentional test article — demod is for lab ID, not ambient bulk intercept.",
                "Run ANALYZE at the confirmed center (modulation + candidates).",
                "Listen only if policy allows for this DUT (known test tone / audio bug class).",
            ],
            "why": "Close the loop: shape + demod class on a controlled device.",
        },
        "ready_to_commit": {
            "title": "10 · Ready to commit",
            "do": [
                "Review the package summary.",
                "Add final notes (serial, battery state, firmware if known).",
                "Click COMMIT to freeze the package and optionally save a signature library entry.",
            ],
            "why": "Reusable bench reference for later sweeps.",
        },
        "committed": {
            "title": "Committed",
            "do": ["Package frozen. Start a new capture for another DUT."],
            "why": "",
        },
    }
    g = guides.get(step, guides["photo"])
    return {"step": step, "title": g["title"], "do": g["do"], "why": g["why"],
            "status": pkg.get("status"), "package_id": pkg.get("id")}


def advance_after_distance(pkg: dict, distance_id: str) -> str:
    order = ["ten_ft", "next_room", "near_field"]
    try:
        i = order.index(distance_id)
    except ValueError:
        return pkg.get("step") or "capture_ten_ft"
    if i + 1 < len(order):
        return "capture_" + order[i + 1]
    return "stimulate"
