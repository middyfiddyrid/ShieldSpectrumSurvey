"""Correlation engine.

Matches every detected signal against the baseline and classifies it:

  VERIFIED   — matches a specific licensed record (callsign-level), or sits in a
               cellular DOWNLINK block whose carrier has towers observed nearby
  PLAUSIBLE  — falls inside a known allocation (ISM, cellular, WiFi) with no
               specific licensee match. ISM/uplink hits carry a caution marker —
               that's where bugs hide; plausible is not a pass there.
  UNVERIFIED — no database or allocation match. These are the anomalies.

Also computes the two strongest localization metrics the app produces:
interior-vs-ambient delta and room-vs-house delta, and folds everything into a
0-100 anomaly score used to rank the final list.

Pure python, no I/O — unit tested with synthetic data in tests/test_correlation.py.
"""

from dataclasses import dataclass, field
from statistics import mean
from typing import Optional

# Match tolerance: ± half the expected channel bandwidth for known services.
# These override a record's own channel_bw_khz when the service is recognized.
SERVICE_TOLERANCE_KHZ = {
    "FM": 200.0,        # ±100 kHz
    "AM": 10.0,         # ±5 kHz
    "TV": 6000.0,       # anywhere inside the 6 MHz channel
    "NFM": 25.0,        # ±12.5 kHz
    "LMR": 25.0,
    "PAGING": 25.0,
    "NOAA": 25.0,
    "CELL": None,       # anywhere inside the carrier's owned block
    "ISM": None,        # anywhere inside the allocation
    "U-NII": None,
    "DECT": None,
}

BASE_SCORE = {"UNVERIFIED": 60.0, "PLAUSIBLE_CAUTION": 35.0, "PLAUSIBLE": 15.0, "VERIFIED": 0.0}


@dataclass
class Classification:
    classification: str
    matched_label: str = ""
    match_reason: str = ""
    caution: bool = False
    nearest_label: str = ""
    nearest_distance_mhz: Optional[float] = None


def _row_center(row: dict) -> float:
    return (row["freq_low_mhz"] + row["freq_high_mhz"]) / 2.0


def _row_is_band(row: dict) -> bool:
    return (row["freq_high_mhz"] - row["freq_low_mhz"]) > 0.001


def _tolerance_mhz(row: dict) -> float:
    tol = SERVICE_TOLERANCE_KHZ.get((row.get("service") or "").upper(), row.get("channel_bw_khz"))
    if tol is None:
        return 0.0  # band rows match by containment, not tolerance
    return (tol or 25.0) / 2.0 / 1000.0


def _matches(row: dict, freq_mhz: float) -> bool:
    if _row_is_band(row):
        return row["freq_low_mhz"] <= freq_mhz <= row["freq_high_mhz"]
    return abs(freq_mhz - _row_center(row)) <= _tolerance_mhz(row)


def _label(row: dict) -> str:
    bits = [b for b in (row.get("callsign"), row.get("label"), row.get("licensee")) if b]
    return bits[0] if bits else row.get("service", "?")


def classify_signal(freq_mhz: float, baseline_rows: list[dict]) -> Classification:
    """baseline_rows: normalized rows from the session baseline table.
    confidence field: 'record' (callsign-level), 'block' (carrier block w/ observed
    towers), 'allocation' (static table)."""
    records = [r for r in baseline_rows if r.get("confidence") == "record" and _matches(r, freq_mhz)]
    if records:
        # prefer the closest record by center distance
        best = min(records, key=lambda r: abs(freq_mhz - _row_center(r)))
        return Classification(
            "VERIFIED", _label(best),
            f"matches licensed record {_label(best)} "
            f"({best.get('service', '?')}, {best.get('source', '?')})",
            caution=False)

    blocks = [r for r in baseline_rows if r.get("confidence") == "block" and _matches(r, freq_mhz)]
    if blocks:
        best = blocks[0]
        if best.get("uplink"):
            return Classification(
                "PLAUSIBLE", _label(best),
                f"inside {_label(best)} — but this is an UPLINK block; "
                "a cellular bug transmits here, not the tower", caution=True)
        return Classification(
            "VERIFIED", _label(best),
            f"inside {_label(best)} downlink block; carrier towers observed nearby "
            f"({best.get('source', '?')})", caution=False)

    allocs = [r for r in baseline_rows if r.get("confidence") == "allocation" and _matches(r, freq_mhz)]
    if allocs:
        # caution allocations dominate the classification when overlapping
        caution_rows = [r for r in allocs if r.get("caution")]
        best = caution_rows[0] if caution_rows else allocs[0]
        caution = bool(caution_rows)
        reason = f"inside {_label(best)} allocation — no specific licensee match"
        if caution:
            reason += "; common hiding band for surveillance devices — treat as suspect until localized"
        if (best.get("category") == "gps"):
            reason = (f"ENERGY IN GPS BAND ({_label(best)}): legitimate GPS is below the noise "
                      "floor — a detectable carrier here suggests a repeater, jammer, or spoofer")
            caution = True
        return Classification("PLAUSIBLE", _label(best), reason, caution=caution)

    # UNVERIFIED — report the nearest known thing and why it didn't match
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
    return Classification("UNVERIFIED", "", reason, caution=True,
                          nearest_label=_label(nearest) if nearest else "",
                          nearest_distance_mhz=ndist)


# ---------------------------------------------------------------------------
# Localization metrics
# ---------------------------------------------------------------------------

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
    """Interior minus exterior amplitude. If the signal wasn't detected outside at
    all, the delta is measured against the ambient noise floor — absence outside
    is the strongest 'locally sourced' indicator there is."""
    m = _find_match(sig["center_mhz"], ambient_signals, _signal_match_tol_mhz(sig))
    if m is not None:
        return (sig.get("peak_dbm") or 0.0) - (m.get("peak_dbm") or 0.0)
    if ambient_floor_dbm is not None:
        return (sig.get("peak_dbm") or 0.0) - ambient_floor_dbm
    return None


def compute_room_localization(signals_by_room: dict[str, list[dict]]) -> dict:
    """Group signals across rooms by frequency; for each frequency group return
    {key_mhz: {strongest_room, delta_db, per_room: {room: peak}}}.
    delta_db = strongest room peak minus the mean of the other rooms (or the
    other rooms' floor if absent there)."""
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
        # absent-in-room counts as that room's floor; approximate with min seen - 6 dB
        absent_floor = min(per.values()) - 6.0
        for room in rooms:
            if room not in per:
                others.append(absent_floor)
        delta = per[strongest] - (mean(others) if others else per[strongest])
        out[key] = {"strongest_room": strongest, "delta_db": round(delta, 1), "per_room": per}
    return out


# ---------------------------------------------------------------------------
# Anomaly score
# ---------------------------------------------------------------------------

def anomaly_score(classification: Classification, persistence: Optional[float],
                  interior_delta_db: Optional[float], localization_delta_db: Optional[float],
                  interior_flag_db: float = 10.0, room_flag_db: float = 12.0,
                  gps_band: bool = False) -> float:
    if classification.classification == "VERIFIED":
        base = BASE_SCORE["VERIFIED"]
    elif classification.classification == "UNVERIFIED":
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
            score += 5.0          # steady carrier — classic continuous bug
        elif persistence <= 0.2 and classification.caution:
            score += 8.0          # bursty in a caution band — hopper/burst transmitter
    if gps_band:
        score += 15.0
    return round(min(score, 100.0), 1)
