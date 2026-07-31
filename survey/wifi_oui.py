"""Curated Wi-Fi OUI → vendor class (TSCM-relevant subset).

BSSID first 3 octets (public OUI) often name the chipset/OEM class:
cameras, TVs, mesh APs, IoT SoCs. Not a serial number — "consistent with
Espressif/Wyze/Samsung gear", never "this exact unit".

Unknown OUIs return None; never invent a brand.
"""

from __future__ import annotations

# Uppercase, no separators: "AABBCC"
WIFI_OUI_VENDORS: dict[str, str] = {
    # Cameras / NVR / doorbells
    "D0039B": "Wyze (camera/IoT)",
    "2CAA8E": "Wyze (camera/IoT)",
    "7C78B2": "Reolink (camera)",
    "EC71DB": "Reecam / IP camera class",
    "18B430": "Nest / Google (camera/thermostat)",
    "64B5C6": "Nest / Google",
    "F4F5E8": "Google Nest",
    "001A11": "Google",
    "3C5AB4": "Google",
    "F0C77F": "Amazon (Ring/Echo class)",
    "74C246": "Amazon",
    "40B4CD": "Amazon",
    "A002DC": "Amazon",
    "000C43": "Roku",
    "B0A737": "Roku",
    "D8D5B9": "Roku",
    "C8D083": "Apple",
    "F0D1A9": "Apple",
    "ACBC32": "Apple",
    "001DD8": "Ubiquiti / UniFi",
    "802AA8": "Ubiquiti",
    "FCECDA": "Ubiquiti",
    "B4FBE4": "Ubiquiti",
    "E063DA": "Ubiquiti",
    "04421A": "ASUS",
    "2C56DC": "ASUS",
    "04D4C4": "ASUS",
    "50C7BF": "TP-Link",
    "C0C9E3": "TP-Link",
    "98DAC4": "TP-Link",
    "14CC20": "TP-Link",
    "B09575": "TP-Link",
    "A0F3C1": "TP-Link",
    "001E58": "D-Link",
    "1C7EE5": "D-Link",
    "C8D3A3": "D-Link",
    "001E2A": "Netgear",
    "A040A0": "Netgear",
    "28C68E": "Netgear",
    "C40415": "Netgear",
    "E0469A": "Netgear",
    "0024B2": "Netgear",
    "001B11": "D-Link / camera class",
    "000C29": "VMware (not a radio — ignore if seen)",
    # IoT SoCs (often cheap cams / bugs / ESP)
    "0CF3EE": "Espressif (ESP32 IoT)",
    "246F28": "Espressif (ESP32 IoT)",
    "A4CF12": "Espressif (ESP32 IoT)",
    "68C63A": "Espressif (ESP32 IoT)",
    "84F3EB": "Espressif (ESP32 IoT)",
    "30AEA4": "Espressif (ESP32 IoT)",
    "CC50E3": "Espressif (ESP32 IoT)",
    "B4E62D": "Espressif (ESP32 IoT)",
    "24A160": "Espressif (ESP32 IoT)",
    "DC4F22": "Espressif (ESP32 IoT)",
    # Samsung / LG / Sony TVs & phones
    "B8278E": "Samsung",
    "002537": "Samsung",
    "5C497D": "Samsung",
    "8C7712": "Samsung",
    "D0C1B1": "Samsung",
    "001E75": "Samsung",
    "001C62": "LG Electronics",
    "00E091": "LG Electronics",
    "A47733": "Google / Chromecast class",
    "F4F5D8": "Google",
    # Sonos / audio
    "5CAAFD": "Sonos",
    "8C4B14": "Sonos",
    "B8E937": "Sonos",
    # Eero / mesh
    "F8BBBF": "Amazon eero",
    "FC65DE": "Amazon eero",
    # Raspberry Pi
    "B827EB": "Raspberry Pi",
    "DCA632": "Raspberry Pi",
    "E45F01": "Raspberry Pi",
    # Hikvision / Dahua class (surveillance)
    "4447CC": "Hikvision (camera/NVR class)",
    "C056E3": "Hikvision (camera/NVR class)",
    "BCAD28": "Hikvision (camera/NVR class)",
    "3CEF8C": "Zhejiang Dahua (camera class)",
    "4C11BF": "Zhejiang Dahua (camera class)",
    # Philips Hue
    "001788": "Philips Hue",
    "ECB5FA": "Philips Hue",
}


def normalize_mac(mac: str) -> str:
    return (mac or "").replace(":", "").replace("-", "").replace(".", "").upper()


def oui_prefix(mac: str) -> str:
    n = normalize_mac(mac)
    return n[:6] if len(n) >= 6 else ""


def vendor_for_bssid(bssid: str) -> str | None:
    """Return curated vendor class string, or None if unknown."""
    p = oui_prefix(bssid)
    if not p:
        return None
    return WIFI_OUI_VENDORS.get(p)


def annotate_wifi_device(dev: dict) -> dict:
    """Add vendor field + fold into extra for inventory display."""
    v = vendor_for_bssid(dev.get("identifier") or "")
    if v:
        dev["vendor"] = v
        extra = (dev.get("extra") or "").strip()
        if v not in extra:
            dev["extra"] = (extra + " · " + v).strip(" ·")
    else:
        dev.setdefault("vendor", "")
    return dev
