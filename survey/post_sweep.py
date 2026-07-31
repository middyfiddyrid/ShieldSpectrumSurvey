"""Content-free post-sweep consolidation and operator guidance.

Detector rows are detections, not physical-device counts. This module builds a
conservative likely-emitter overview without deleting evidence or silently
demodulating communications.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from math import isfinite


CATEGORY_ORDER = (
    "client_inventory", "known_environment", "other_inventory",
    "unexplained_watch", "priority_anomaly",
)
CATEGORY_META = {
    "client_inventory": {"label": "Accounted for by client inventory", "color": "#2ecc71"},
    "known_environment": {"label": "Known / expected environment", "color": "#3498db"},
    "other_inventory": {"label": "Other wireless inventory", "color": "#1abc9c"},
    "unexplained_watch": {"label": "Unexplained - watch list", "color": "#f39c12"},
    "priority_anomaly": {"label": "Priority anomaly", "color": "#e74c3c"},
}


def _num(value, default=0.0):
    try:
        value = float(value)
        return value if isfinite(value) else default
    except (TypeError, ValueError):
        return default


def _behavior(row):
    b = row.get("behavior") or {}
    if not isinstance(b, dict):
        return "", 0.0
    return str(b.get("class_id") or ""), _num(b.get("confidence"))


def _bandwidth_compatible(a, b):
    aw = max(1.0, _num(a.get("bw_khz"), 1.0))
    bw = max(1.0, _num(b.get("bw_khz"), 1.0))
    return max(aw, bw) / min(aw, bw) <= 4.0


def _behavior_compatible(a, b):
    ac, aq = _behavior(a)
    bc, bq = _behavior(b)
    # Strong conflicting observations stay separate: this is the disguised or
    # adjacent-transmitter safeguard.
    return not (ac and bc and ac != bc and aq >= 0.75 and bq >= 0.75)


def _same_emitter(a, b):
    dist = abs(_num(a.get("center_mhz")) - _num(b.get("center_mhz")))
    aw = max(1.0, _num(a.get("bw_khz"), 1.0))
    bw = max(1.0, _num(b.get("bw_khz"), 1.0))
    tolerance_mhz = max(0.05, min(0.50, max(aw, bw) / 2000.0))
    return (dist <= tolerance_mhz and _bandwidth_compatible(a, b)
            and _behavior_compatible(a, b))


def consolidate_emitters(rows):
    """Group repeat detections while retaining conflicting nearby variants."""
    ordered = sorted((deepcopy(dict(r)) for r in rows), key=lambda r: (
        -_num(r.get("anomaly_score")), -_num(r.get("snr_db")),
        _num(r.get("center_mhz"))))
    groups = []
    for row in ordered:
        target = next((g for g in groups if _same_emitter(row, g["best"])), None)
        if target is None:
            target = {"best": row, "detections": []}
            groups.append(target)
        target["detections"].append(row)

    for group in groups:
        detections = group["detections"]
        group["detection_count"] = len(detections)
        group["sweep_count"] = len({d.get("sweep_id") for d in detections
                                     if d.get("sweep_id") is not None})
        group["rooms"] = sorted({str(d.get("room") or "") for d in detections if d.get("room")})
        group["phases"] = sorted({str(d.get("phase") or "") for d in detections if d.get("phase")})
        group["repeat_confirmed"] = group["sweep_count"] >= 2
        group["frequency_mhz"] = _num(group["best"].get("center_mhz"))
        group["signal_id"] = group["best"].get("id")
        group["variants_preserved"] = sum(
            1 for other in ordered
            if other.get("id") != group["best"].get("id")
            and abs(_num(other.get("center_mhz")) - group["frequency_mhz"]) <= 0.5
            and not _same_emitter(other, group["best"])
        )
    return groups


def _inventory_state(row):
    matches = row.get("inventory_matches") or []
    if not isinstance(matches, list):
        return None
    if any(bool(m.get("owned")) for m in matches if isinstance(m, dict)):
        return "client"
    if any(isinstance(m, dict) for m in matches):
        return "other"
    return None


def substantive_assessment(group, live_result=None):
    """Conservative, content-free alert gate for one likely emitter."""
    row = group["best"]
    if _inventory_state(row) == "client":
        return False, "accounted for by client inventory"
    triage = str(row.get("triage_status") or "").lower()
    if triage in ("flagged", "suspect"):
        return True, "operator marked it for investigation"

    bclass, confidence = _behavior(row)
    score = _num(row.get("anomaly_score"))
    snr = _num(row.get("snr_db"), _num(row.get("peak_dbm")) - _num(row.get("floor_dbm")))
    attribution = str(row.get("attribution_status") or "").lower()
    live_present = bool((live_result or {}).get("signal_present"))
    live_snr = _num((live_result or {}).get("snr_db"))

    # The wide-net 0.58 key-fob-like label is a shape hint, never proof.
    if bclass == "keyfob_like" and confidence < 0.75:
        return False, "weak bursty sub-GHz shape; needs correlation before alerting"
    if bclass == "gps_anomaly":
        return True, "detectable energy in the GPS receive band"
    spatial = max(_num(row.get("interior_delta_db")), _num(row.get("localization_score")))
    if (score >= 80 and bool(row.get("caution"))
            and group.get("repeat_confirmed") and spatial >= 10):
        return True, "high caution score confirmed by repeat/spatial evidence"
    if attribution == "client_space" and score >= 50:
        return True, "unexplained signal attributed to the client space"
    if (live_present and live_snr >= 10 and score >= 55
            and str(row.get("classification")) == "UNVERIFIED"):
        return True, "unexplained signal confirmed live on the second radio"
    if (bclass == "analog_audio_bug" and confidence >= 0.80 and snr >= 10
            and attribution in ("client_space", "inconclusive")):
        return True, "strong content-free analog room-audio signature"
    if group.get("repeat_confirmed"):
        return False, "repeatable but below the substantive alert threshold"
    return False, "one-pass unexplained signal retained on the watch list"


def _category_for(group, substantive):
    row = group["best"]
    inv = _inventory_state(row)
    if inv == "client":
        return "client_inventory"
    if substantive:
        return "priority_anomaly"
    if inv == "other":
        return "other_inventory"
    if (str(row.get("classification") or "") in ("VERIFIED", "PLAUSIBLE")
            and not bool(row.get("caution"))):
        return "known_environment"
    if str(row.get("attribution_status") or "") in ("third_party", "infrastructure"):
        return "known_environment"
    return "unexplained_watch"


def build_ambient_overview(rows, live_results=None):
    live_results = live_results or {}
    groups = consolidate_emitters(rows)
    counts = Counter()
    priority = []
    for group in groups:
        live = live_results.get(group.get("signal_id")) or {}
        substantive, reason = substantive_assessment(group, live)
        category = _category_for(group, substantive)
        counts[category] += 1
        group.update(substantive=substantive, substantive_reason=reason,
                     category=category, category_label=CATEGORY_META[category]["label"],
                     live_check=live)
        group.pop("detections", None)
        if substantive:
            priority.append(group)
    categories = [{"id": key, **CATEGORY_META[key], "count": counts[key]}
                  for key in CATEGORY_ORDER]
    priority.sort(key=lambda g: -_num(g["best"].get("anomaly_score")))
    raw_count, unique_count = len(rows), len(groups)
    return {
        "raw_detection_count": raw_count,
        "unique_emitter_count": unique_count,
        "duplicate_reduction_count": max(0, raw_count - unique_count),
        "client_accounted_count": counts["client_inventory"],
        "priority_count": counts["priority_anomaly"],
        "watch_count": counts["unexplained_watch"],
        "categories": categories, "emitters": groups,
        "priority_emitters": priority,
        "method_note": (
            "Counts are likely emitters, not raw hits. Repeated detections are "
            "consolidated; conflicting bandwidth or high-confidence behavior "
            "is kept separate as a possible disguised/adjacent transmitter."),
    }


def candidates_for_live_check(overview, limit=12):
    candidates = [g for g in overview.get("emitters", [])
                  if g.get("category") in ("priority_anomaly", "unexplained_watch")]
    candidates.sort(key=lambda g: (
        not g.get("substantive"),
        -_num((g.get("best") or {}).get("anomaly_score")),
        -_num((g.get("best") or {}).get("snr_db"))))
    return candidates[:max(0, int(limit))]
