"""Wireless device inventory — WiFi APs and BLE advertisers seen from the
laptop's own adapters, so the operator can mark which devices the client owns
and flag loud transmitters nobody accounts for.

WiFi: `netsh wlan show networks mode=bssid` (built into Windows, no deps).
BLE:  `bleak` if installed (pip install bleak); reports a clear message if not.
      An nRF dongle + nRF Connect can supplement manually; this module covers
      the standard laptop adapter case.

Inventory rows are per-session, deduped by (kind, identifier); RSSI keeps the
strongest seen. 'owned' is the operator's client-accounting flag.
"""

import re
import subprocess
import time

LOUD_RSSI_DBM = -55.0   # stronger than this and unowned -> flag for follow-up


def wifi_channel_to_mhz(channel: int, band_hint: str = "") -> float | None:
    if "6 ghz" in band_hint.lower():
        return 5950.0 + 5.0 * channel
    if 1 <= channel <= 14:
        return 2407.0 + 5.0 * channel if channel < 14 else 2484.0
    if 32 <= channel <= 177:
        return 5000.0 + 5.0 * channel
    if channel > 177:                      # 6 GHz numbering without a band hint
        return 5950.0 + 5.0 * channel
    return None


def _signal_pct_to_dbm(pct: float) -> float:
    # netsh reports quality %; standard approximation: dBm = pct/2 - 100
    return round(pct / 2.0 - 100.0, 1)


def scan_wifi() -> tuple[list[dict], str]:
    """Returns (devices, message). Each device: identifier=BSSID, name=SSID,
    rssi_dbm, channel, freq_mhz, extra."""
    try:
        out = subprocess.run(
            ["netsh", "wlan", "show", "networks", "mode=bssid"],
            capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired) as e:
        return [], f"netsh scan failed: {e}"
    devices = []
    ssid, bssid = "", None
    cur = {}
    band_hint = ""
    for raw in out.splitlines():
        line = raw.strip()
        m = re.match(r"^SSID \d+\s*:\s*(.*)$", line)
        if m:
            ssid = m.group(1).strip() or "(hidden)"
            continue
        m = re.match(r"^BSSID \d+\s*:\s*([0-9a-fA-F:]{17})", line)
        if m:
            if cur.get("identifier"):
                devices.append(cur)
            bssid = m.group(1).lower()
            cur = {"kind": "wifi", "identifier": bssid, "name": ssid,
                   "rssi_dbm": None, "channel": "", "freq_mhz": None, "extra": ""}
            band_hint = ""
            continue
        if not bssid:
            continue
        m = re.match(r"^Signal\s*:\s*(\d+)\s*%", line)
        if m:
            cur["rssi_dbm"] = _signal_pct_to_dbm(float(m.group(1)))
            continue
        m = re.match(r"^Band\s*:\s*(.+)$", line)
        if m:
            band_hint = m.group(1)
            cur["extra"] = (cur["extra"] + " " + band_hint).strip()
            continue
        m = re.match(r"^Channel\s*:\s*(\d+)", line)
        if m:
            ch = int(m.group(1))
            cur["channel"] = str(ch)
            cur["freq_mhz"] = wifi_channel_to_mhz(ch, band_hint)
            continue
        m = re.match(r"^Radio type\s*:\s*(.+)$", line)
        if m:
            cur["extra"] = (cur["extra"] + " " + m.group(1).strip()).strip()
    if cur.get("identifier"):
        devices.append(cur)
    if not devices:
        return [], ("no WiFi networks seen — is the WLAN adapter on? "
                    "(netsh needs the wireless service running)")
    # curated OUI → vendor class (cameras, TVs, mesh, ESP32, …)
    from .wifi_oui import annotate_wifi_device
    devices = [annotate_wifi_device(d) for d in devices]
    n_vend = sum(1 for d in devices if d.get("vendor"))
    msg = f"{len(devices)} access points seen"
    if n_vend:
        msg += f" ({n_vend} with known OUI/vendor class)"
    return devices, msg


def _ble_raw(seconds: float = 8.0) -> tuple[list[dict], str]:
    """Collect one advertisement dict per BLE device (strongest RSSI seen). Shared
    by scan_ble (inventory rows) and scan_ble_details (full enrichment)."""
    try:
        import asyncio  # noqa: F401

        from bleak import BleakScanner
    except ImportError:
        return [], ("BLE scanning needs the 'bleak' package: pip install bleak. "
                    "(Alternatively inventory devices manually with nRF Connect.)")

    async def run():
        found = {}

        def cb(device, adv):
            rssi = getattr(adv, "rssi", None)
            cur = found.get(device.address)
            if cur is not None and (rssi or -200) <= (cur["rssi_dbm"] or -200):
                return
            # bleak may report the platform address type on WinRT via details
            addr_type = None
            details = getattr(device, "details", None)
            if isinstance(details, dict):
                bt = details.get("props", {}).get("AddressType") or details.get("AddressType")
                if isinstance(bt, str):
                    addr_type = "random" if "random" in bt.lower() else (
                        "public" if "public" in bt.lower() else None)
            found[device.address] = {
                "mac": device.address.lower(),
                "name": device.name or getattr(adv, "local_name", "") or "",
                "rssi_dbm": float(rssi) if rssi is not None else None,
                "address_type": addr_type,
                "tx_power": getattr(adv, "tx_power", None),
                "manufacturer_data": dict(getattr(adv, "manufacturer_data", {}) or {}),
                "service_uuids": list(getattr(adv, "service_uuids", []) or []),
            }

        scanner = BleakScanner(detection_callback=cb)
        await scanner.start()
        await asyncio.sleep(seconds)
        await scanner.stop()
        return list(found.values())

    try:
        import asyncio
        advs = asyncio.run(run())
    except Exception as e:  # noqa: BLE001 - adapter off, WinRT errors, etc.
        return [], f"BLE scan failed: {e}"
    return advs, f"{len(advs)} BLE advertisers seen"


def scan_ble(seconds: float = 8.0) -> tuple[list[dict], str]:
    """Inventory rows (deduped, stored). Each row's `extra` carries the enriched
    one-line dossier (vendor, address type, tracker flag, services) from survey.ble."""
    from . import ble
    advs, message = _ble_raw(seconds)
    devices = []
    for a in advs:
        summ = ble.summarize_advertisement(a)
        devices.append({
            "kind": "ble", "identifier": a["mac"],
            "name": a["name"] or (summ["vendor"] or ""),
            "rssi_dbm": a["rssi_dbm"], "channel": "", "freq_mhz": 2440.0,
            "extra": summ["info"]})
    return devices, message


def scan_ble_details(seconds: float = 8.0) -> tuple[list[dict], str]:
    """Full per-advertiser dossiers for the standalone BLE sweep view — every field
    survey.ble can extract, sorted trackers-first then loudest. Not stored."""
    from . import ble
    advs, message = _ble_raw(seconds)
    records = [ble.summarize_advertisement(a) for a in advs]
    records.sort(key=lambda r: (not r["is_tracker"], -(r["rssi_dbm"] or -200)))
    return records, message


def upsert_inventory(db, session_id: int, devices: list[dict]):
    now = time.time()
    for d in devices:
        db.execute(
            "INSERT INTO inventory (session_id,kind,identifier,name,rssi_dbm,channel,"
            "freq_mhz,extra,vendor,first_seen,last_seen) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(session_id,kind,identifier) DO UPDATE SET "
            "name=CASE WHEN excluded.name!='' THEN excluded.name ELSE inventory.name END, "
            "rssi_dbm=MAX(COALESCE(inventory.rssi_dbm,-200), COALESCE(excluded.rssi_dbm,-200)), "
            "channel=excluded.channel, freq_mhz=excluded.freq_mhz, "
            "extra=excluded.extra, "
            "vendor=CASE WHEN excluded.vendor!='' THEN excluded.vendor ELSE inventory.vendor END, "
            "last_seen=excluded.last_seen",
            (session_id, d["kind"], d["identifier"], d.get("name", ""),
             d.get("rssi_dbm"), d.get("channel", ""), d.get("freq_mhz"),
             d.get("extra", ""), d.get("vendor", "") or "", now, now))


def inventory_rows(db, session_id: int) -> list[dict]:
    rows = db.query("SELECT * FROM inventory WHERE session_id=? "
                    "ORDER BY owned ASC, rssi_dbm DESC", (session_id,))
    for r in rows:
        r["loud_unowned"] = (not r["owned"]) and r["rssi_dbm"] is not None \
            and r["rssi_dbm"] >= LOUD_RSSI_DBM
    return rows
