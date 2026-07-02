"""Correlation engine.

Matches every detected signal against the baseline and classifies it for
field display:

  IDENTIFIED  — specific licensee/callsign or carrier block with observed towers
  KNOWN_BAND  — inside a known allocation only (no specific party)
  ANOMALY     — no baseline match

Legacy ``classification`` column values (VERIFIED / PLAUSIBLE / UNVERIFIED) are
still populated for triage queries and older reports.

Pure python — unit tested in tests/test_correlation.py.
"""

from dataclasses import dataclass
from statistics import mean
from typing import Optional

SERVICE_TOLERANCE_KHZ = {
    "FM": 200.0,
    "AM": 10.0,
    "TV": 6000.0,
    "NFM": 25.0,
    "LMR": 25.0,
    "PAGING": 25.0,
    "NOAA": 25.0,
    "CELL": None,
    "ISM": None,
    "U-NII": None,
    "DECT": None,
}

IDENTITY_TIERS = ("IDENTIFIED", "KNOWN_BAND", "ANOMALY")
LEGACY_FROM_TIER = {
    "IDENTIFIED": "VERIFIED",
    "KNOWN_BAND": "PLAUSIBLE",
    "ANOMALY": "UNVERIFIED",
}

BASE_SCORE = {"UNVERIFIED": 60.0, "PLAUSIBLE_CAUTION": 35.0, "PLAUSIBLE": 15.0, "VERIFIED": 0.0}


@dataclass
class Classification:
    classification: str          # legacy: VERIFIED | PLAUSIBLE | UNVERIFIED
    matched_label: str = ""
    match_reason: str = ""
    caution: bool = False
    nearest_label: str = ""
    nearest_distance_mhz: Optional[float] = None
    identity_tier: str = "ANOMALY"
    display_label: str = ""
    group_key: str = ""
    group_label: str = ""


def _row_center(row: dict) -> float:
    return (row["freq_low_mhz"] + row["freq_high_mhz"]) / 2.0


def _row_is_band(row: dict) -> bool:
    return (row["freq_high_mhz"] - row["freq_low_mhz"]) > 0.001


def _tolerance_mhz(row: dict) -> float:
    tol = SERVICE_TOLERANCE_KHZ.get((row.get("service") or "").upper(), row.get("channel_bw_khz"))
    if tol is None:
        return 0.0
    return (tol or 25.0) / 2.0 / 1000.0


def _matches(row: dict, freq_mhz: float) -> bool:
    if _row_is_band(row):
        return row["freq_low_mhz"] <= freq_mhz <= row["freq_high_mhz"]
    return abs(freq_mhz - _row_center(row)) <= _tolerance_mhz(row)


def _label(row: dict) -> str:
    bits = [b for b in (row.get("callsign"), row.get("label"), row.get("licensee")) if b]
    return bits[0] if bits else row.get("service", "?")


def _group_from_row(row: dict) -> tuple[str, str, str]:
    """Return (group_key, group_label, display_label) for UI grouping."""
    conf = row.get("confidence", "")
    svc = (row.get("service") or "").upper()
    cat = (row.get("category") or "").lower()
    party = _label(row)

    if conf == "record":
        if svc == "FM":
            return "broadcast:FM", "FM stations", party
        if svc in ("TV", "DT"):
            return "broadcast:TV", "TV stations", party
        if svc == "AM":
            return "broadcast:AM", "AM stations", party
        if svc == "LMR":
            raw = row.get("raw_json") or row.get("raw") or {}
            if isinstance(raw, str):
                try:
                    import json
                    raw = json.loads(raw)
                except Exception:  # noqa: BLE001
                    raw = {}
            svc_code = (raw.get("service_code") or "").upper()
            lbl_low = (row.get("label") or "").lower()
            if svc_code == "PW" or "public safety" in lbl_low:
                return "lm:public_safety", "Public safety", party
            if svc_code in ("IG", "GB", "YG") or "business" in lbl_low:
                return "lm:business", "Business LMR", party
            return "lm:land_mobile", "Land mobile", party
        if row.get("callsign"):
            cs = row["callsign"]
            return f"named:{cs}", party, party
        return f"record:{svc}", party, party

    if conf == "block":
        carrier = (row.get("licensee") or party).strip() or "Cellular"
        if row.get("uplink"):
            return f"band:cell_uplink:{carrier}", f"{carrier} uplink", party
        return f"cell:{carrier}", f"{carrier} cellular", party

    if conf == "allocation":
        return f"band:{cat}:{svc}", party, party

    return "other", party, party


def _finish(identity_tier: str, display_label: str, group_key: str, group_label: str,
            match_reason: str, caution: bool = False,
            nearest_label: str = "", nearest_distance_mhz: Optional[float] = None
            ) -> Classification:
    legacy = LEGACY_FROM_TIER[identity_tier]
    return Classification(
        classification=legacy,
        matched_label=display_label,
        match_reason=match_reason,
        caution=caution,
        nearest_label=nearest_label,
        nearest_distance_mhz=nearest_distance_mhz,
        identity_tier=identity_tier,
        display_label=display_label,
        group_key=group_key,
        group_label=group_label,
    )


def classify_signal(freq_mhz: float, baseline_rows: list[dict]) -> Classification:
    records = [r for r in baseline_rows if r.get("confidence") == "record" and _matches(r, freq_mhz)]
    if records:
        best = min(records, key=lambda r: abs(freq_mhz - _row_center(r)))
        gk, gl, dl = _group_from_row(best)
        return _finish(
            "IDENTIFIED", dl, gk, gl,
            f"matches licensed record {dl} ({best.get('service', '?')}, {best.get('source', '?')})")

    blocks = [r for r in baseline_rows if r.get("confidence") == "block" and _matches(r, freq_mhz)]
    if blocks:
        best = blocks[0]
        gk, gl, dl = _group_from_row(best)
        if best.get("uplink"):
            return _finish(
                "KNOWN_BAND", dl, gk, gl,
                f"inside {dl} — UPLINK block; a cellular bug transmits here, not the tower",
                caution=True)
        return _finish(
            "IDENTIFIED", dl, gk, gl,
            f"inside {dl} downlink block; carrier towers observed nearby "
            f"({best.get('source', '?')})")

    allocs = [r for r in baseline_rows if r.get("confidence") == "allocation" and _matches(r, freq_mhz)]
    if allocs:
        caution_rows = [r for r in allocs if r.get("caution")]
        best = caution_rows[0] if caution_rows else allocs[0]
        caution = bool(caution_rows)
        gk, gl, dl = _group_from_row(best)
        reason = f"inside {dl} allocation — no specific licensee match"
        if caution:
            reason += "; common hiding band for surveillance devices — treat as suspect until localized"
        if best.get("category") == "gps":
            reason = (
                f"ENERGY IN GPS BAND ({dl}): legitimate GPS is below the noise floor — "
                "a detectable carrier here suggests a repeater, jammer, or spoofer")
            caution = True
        return _finish("KNOWN_BAND", dl, gk, gl, reason, caution=caution)

    nearest, ndist = None, None
    for r in baseline_rows:
        if _row_is_band(r):
            d = max(r["freq_low_mhz"] - freq_mhz, freq_mhz - r["freq_high_mhz"], 0.0)
        else:
            d = abs(freq_mhz - _row_center(r))
        if d > 0 and (ndist is None or d < ndist):
            nearest, ndist = r, d
    reason = "no database or allocation match"
    if nearest is not None:
        reason += (f"; nearest known: {_label(nearest)} "
                   f"{ndist:.3f} MHz away (outside its tolerance)")
    return _finish(
        "ANOMALY", "ANOMALY", "anomaly", "Anomaly", reason, caution=True,
        nearest_label=_label(nearest) if nearest else "",
        nearest_distance_mhz=ndist)


def _signal_match_tol_mhz(sig: dict) -> float:
    return max((sig.get("bw_khz") or 100.0) / 1000.0, 0.2)


def _find_match(freq_mhz: float, candidates: list[dict], tol_mhz: float) -> Optional[dict]:
    best, bd = None, None
    for c in candidates:
        d = abs(c["center_mhz"] - freq_mhz)
        if d <= tol_mhz and (bd is None or d < bd):
            best, bd = c, d
    return best


def compute_interior_delta(sig: dict, ambient_signals: list[dict],
                           ambient_floor_dbm: Optional[float]) -> Optional[float]:
    m = _find_match(sig["center_mhz"], ambient_signals, _signal_match_tol_mhz(sig))
    if m is not None:
        return (sig.get("peak_dbm") or 0.0) - (m.get("peak_dbm") or 0.0)
    if ambient_floor_dbm is not None:
        return (sig.get("peak_dbm") or 0.0) - ambient_floor_dbm
    return None


def compute_room_localization(signals_by_room: dict[str, list[dict]]) -> dict:
    rooms = [r for r in signals_by_room if signals_by_room[r]]
    if len(rooms) < 2:
        return {}
    groups: dict[float, dict] = {}
    for room in rooms:
        for sig in signals_by_room[room]:
            tol = _signal_match_tol_mhz(sig)
            key = next((k for k in groups if abs(k - sig["center_mhz"]) <= tol), None)
            if key is None:
                key = round(sig["center_mhz"], 3)
                groups[key] = {"per_room": {}}
            cur = groups[key]["per_room"].get(room)
            if cur is None or sig["peak_dbm"] > cur:
                groups[key]["per_room"][room] = sig["peak_dbm"]
    out = {}
    for key, g in groups.items():
        per = g["per_room"]
        strongest = max(per, key=per.get)
        others = [v for r, v in per.items() if r != strongest]
        absent_floor = min(per.values()) - 6.0
        for room in rooms:
            if room not in per:
                others.append(absent_floor)
        delta = per[strongest] - (mean(others) if others else per[strongest])
        out[key] = {"strongest_room": strongest, "delta_db": round(delta, 1), "per_room": per}
    return out


def anomaly_score(classification: Classification, persistence: Optional[float],
                  interior_delta_db: Optional[float], localization_delta_db: Optional[float],
                  interior_flag_db: float = 10.0, room_flag_db: float = 12.0,
                  gps_band: bool = False) -> float:
    tier = classification.identity_tier or LEGACY_FROM_TIER.get(classification.classification, "ANOMALY")
    if tier == "IDENTIFIED":
        base = BASE_SCORE["VERIFIED"]
    elif tier == "ANOMALY":
        base = BASE_SCORE["UNVERIFIED"]
    else:
        base = BASE_SCORE["PLAUSIBLE_CAUTION" if classification.caution else "PLAUSIBLE"]

    score = base
    if interior_delta_db is not None and interior_delta_db >= interior_flag_db:
        score += min(25.0, 10.0 + (interior_delta_db - interior_flag_db) * 1.5)
    if localization_delta_db is not None and localization_delta_db >= room_flag_db:
        score += min(20.0, 8.0 + (localization_delta_db - room_flag_db) * 1.2)
    if persistence is not None:
        if persistence >= 0.8:
            score += 5.0
        elif persistence <= 0.2 and classification.caution:
            score += 8.0
    if gps_band:
        score += 15.0
    return round(min(score, 100.0), 1)