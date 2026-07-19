"""Correlate RF detections with wireless inventory (Wi-Fi / BLE).

The spectrum peak does not carry a product name. Wi-Fi and Bluetooth *do*
carry identifiers (BSSID, SSID, OUI, BLE company id, tracker signatures).
This module links an RF hit to inventory rows the operator already scanned:

  * Wi-Fi — match by channel center frequency (tight)
  * BLE  — soft class match when RF is in 2.4 GHz ISM (BLE hops; no single
           center, so we attach "BLE devices present" rather than a fake
           freq pin)

Returns honest match objects with score + reason. Never invents a brand from
RF alone.
"""

from __future__ import annotations

# How close (MHz) an RF center must be to a Wi-Fi AP's channel center to count
# as a channel hit. 20 MHz Wi-Fi → ±10 MHz is natural; we allow a bit more for
# detector BW error and channel bonding edges.
WIFI_CHANNEL_TOL_MHZ = 12.0
# 2.4 GHz ISM used for BLE soft-matches
BLE_BAND = (2400.0, 2483.5)
WIFI_24 = (2400.0, 2500.0)
WIFI_5 = (5150.0, 5925.0)


def _f(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _wifi_band(freq_mhz: float | None) -> str:
    if freq_mhz is None:
        return ""
    if WIFI_24[0] <= freq_mhz <= WIFI_24[1]:
        return "2.4 GHz"
    if WIFI_5[0] <= freq_mhz <= WIFI_5[1]:
        return "5/6 GHz"
    return ""


def match_wifi(center_mhz: float, bw_khz, devices: list[dict],
               tol_mhz: float = WIFI_CHANNEL_TOL_MHZ) -> list[dict]:
    """Rank Wi-Fi inventory rows by frequency proximity to the RF detection."""
    out = []
    bw_mhz = (_f(bw_khz) or 0.0) / 1000.0
    # widen tolerance a bit for very wide detections (40/80 MHz class)
    eff_tol = max(tol_mhz, bw_mhz * 0.6 if bw_mhz else tol_mhz)
    for d in devices:
        if (d.get("kind") or "").lower() != "wifi":
            continue
        f = _f(d.get("freq_mhz"))
        if f is None:
            continue
        dist = abs(f - center_mhz)
        if dist > eff_tol:
            continue
        # score 1.0 at exact center, 0 at the edge of tolerance
        score = max(0.0, 1.0 - dist / eff_tol)
        name = (d.get("name") or "").strip() or "(hidden SSID)"
        ident = d.get("identifier") or ""
        extra = (d.get("extra") or "").strip()
        vendor = (d.get("vendor") or "").strip()
        if not vendor and ident:
            try:
                from .wifi_oui import vendor_for_bssid
                vendor = vendor_for_bssid(ident) or ""
            except Exception:  # noqa: BLE001
                vendor = ""
        owned = bool(d.get("owned"))
        rssi = d.get("rssi_dbm")
        reason = (f"Wi-Fi channel center {f:.1f} MHz is {dist:.1f} MHz from "
                  f"this detection ({center_mhz:.3f} MHz)")
        label = f"{name} [{ident}]" if ident else name
        if vendor:
            label = f"{vendor} · " + label
        if owned:
            label = "✓ owned · " + label
        out.append({
            "kind": "wifi",
            "identifier": ident,
            "name": name,
            "vendor": vendor,
            "label": label,
            "freq_mhz": f,
            "channel": d.get("channel") or "",
            "rssi_dbm": rssi,
            "owned": owned,
            "score": round(score, 3),
            "distance_mhz": round(dist, 3),
            "reason": reason,
            "extra": extra,
            "band": _wifi_band(f),
            "phrase": (
                f"Matches Wi-Fi AP “{name}”"
                + (f" — {vendor}" if vendor else "")
                + (f" ({ident})" if ident else "")
                + (f" on ch {d.get('channel')}" if d.get("channel") else "")
                + (f", {rssi:.0f} dBm" if rssi is not None else "")
                + (" — marked client-owned" if owned
                   else " — NOT marked owned; verify with client")
            ),
        })
    out.sort(key=lambda m: (-m["score"], m.get("rssi_dbm") is None,
                            -(m.get("rssi_dbm") or -200)))
    return out


def match_ble(center_mhz: float, devices: list[dict]) -> list[dict]:
    """Soft-match: RF is in the 2.4 GHz ISM → attach nearby BLE advertisers.

    BLE frequency-hops across the band, so we cannot pin one advertiser to one
    RF center. Instead we surface the BLE inventory as co-located candidates
    when the detection sits in the BLE band, scored by RSSI (loudest first).
    """
    if not (BLE_BAND[0] <= center_mhz <= BLE_BAND[1]):
        return []
    out = []
    for d in devices:
        if (d.get("kind") or "").lower() != "ble":
            continue
        rssi = d.get("rssi_dbm")
        # map RSSI to 0..1 soft score (very loud → high)
        if rssi is None:
            score = 0.25
        else:
            # -40 dBm → ~1.0, -90 dBm → ~0.0
            score = max(0.05, min(1.0, (float(rssi) + 90.0) / 50.0))
        name = (d.get("name") or "").strip() or "unnamed BLE"
        ident = d.get("identifier") or ""
        owned = bool(d.get("owned"))
        extra = (d.get("extra") or "").strip()
        is_tracker = "tracker" in extra.lower() or "⚑" in extra
        # boost trackers — TSCM priority
        if is_tracker:
            score = min(1.0, score + 0.15)
        label = f"{name} [{ident}]" if ident else name
        if is_tracker:
            label = "⚑ TRACKER · " + label
        if owned:
            label = "✓ owned · " + label
        out.append({
            "kind": "ble",
            "identifier": ident,
            "name": name,
            "label": label,
            "freq_mhz": d.get("freq_mhz") or 2440.0,
            "channel": "",
            "rssi_dbm": rssi,
            "owned": owned,
            "score": round(score, 3),
            "distance_mhz": None,
            "reason": ("RF is in the 2.4 GHz ISM where BLE advertises; "
                       "BLE hops — this is a co-band inventory candidate, "
                       "not a hard frequency pin"),
            "extra": extra,
            "is_tracker": is_tracker,
            "band": "2.4 GHz",
            "phrase": (
                f"BLE device “{name}”"
                + (f" ({ident})" if ident else "")
                + (f", {rssi:.0f} dBm" if rssi is not None else "")
                + (" — LOCATION TRACKER" if is_tracker else "")
                + (" — marked client-owned" if owned
                   else " — NOT marked owned")
            ),
        })
    out.sort(key=lambda m: (not m.get("is_tracker"), -m["score"]))
    return out


def match_inventory(center_mhz: float, bw_khz, inventory: list[dict],
                    top_n: int = 6) -> list[dict]:
    """Combine Wi-Fi (hard) + BLE (soft) matches for one RF center."""
    if not inventory or not center_mhz:
        return []
    wifi = match_wifi(center_mhz, bw_khz, inventory)
    ble = match_ble(center_mhz, inventory)
    # Prefer hard Wi-Fi channel hits, then BLE. Cap list for UI.
    combined = wifi + ble
    return combined[:top_n]


def summarize_matches(matches: list[dict]) -> str:
    """One-line summary for ledger / lightbox."""
    if not matches:
        return ""
    top = matches[0]
    n = len(matches)
    more = f" (+{n - 1} more)" if n > 1 else ""
    return top["phrase"] + more
