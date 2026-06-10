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
    return devices, f"{len(devices)} access points seen"


def scan_ble(seconds: float = 8.0) -> tuple[list[dict], str]:
    try:
        import asyncio

        from bleak import BleakScanner
    except ImportError:
        return [], ("BLE scanning needs the 'bleak' package: pip install bleak. "
                    "(Alternatively inventory devices manually with nRF Connect.)")

    async def run():
        found = {}

        def cb(device, adv):
            rssi = getattr(adv, "rssi", None)
            cur = found.get(device.address)
            if cur is None or (rssi or -200) > (cur["rssi_dbm"] or -200):
                name = device.name or adv.local_name or ""
                mfg = ""
                if adv.manufacturer_data:
                    mfg = "mfg:" + ",".join(f"0x{k:04x}" for k in adv.manufacturer_data)
                found[device.address] = {
                    "kind": "ble", "identifier": device.address.lower(),
                    "name": name, "rssi_dbm": float(rssi) if rssi is not None else None,
                    "channel": "", "freq_mhz": 2440.0,   # BLE hops 2402-2480
                    "extra": mfg}

        scanner = BleakScanner(detection_callback=cb)
        await scanner.start()
        await asyncio.sleep(seconds)
        await scanner.stop()
        return list(found.values())

    try:
        import asyncio
        devices = asyncio.run(run())
    except Exception as e:  # noqa: BLE001 - adapter off, WinRT errors, etc.
        return [], f"BLE scan failed: {e}"
    return devices, f"{len(devices)} BLE advertisers seen"


def upsert_inventory(db, session_id: int, devices: list[dict]):
    now = time.time()
    for d in devices:
        db.execute(
            "INSERT INTO inventory (session_id,kind,identifier,name,rssi_dbm,channel,"
            "freq_mhz,extra,first_seen,last_seen) VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(session_id,kind,identifier) DO UPDATE SET "
            "name=CASE WHEN excluded.name!='' THEN excluded.name ELSE inventory.name END, "
            "rssi_dbm=MAX(COALESCE(inventory.rssi_dbm,-200), COALESCE(excluded.rssi_dbm,-200)), "
            "channel=excluded.channel, freq_mhz=excluded.freq_mhz, "
            "extra=excluded.extra, last_seen=excluded.last_seen",
            (session_id, d["kind"], d["identifier"], d.get("name", ""),
             d.get("rssi_dbm"), d.get("channel", ""), d.get("freq_mhz"),
             d.get("extra", ""), now, now))


def inventory_rows(db, session_id: int) -> list[dict]:
    rows = db.query("SELECT * FROM inventory WHERE session_id=? "
                    "ORDER BY owned ASC, rssi_dbm DESC", (session_id,))
    for r in rows:
        r["loud_unowned"] = (not r["owned"]) and r["rssi_dbm"] is not None \
            and r["rssi_dbm"] >= LOUD_RSSI_DBM
    return rows
