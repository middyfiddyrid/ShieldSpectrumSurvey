"""Expected-vs-found verification.

The baseline is a prediction. After sweeping, every specific licensed record
(callsign-level: FCC FM/AM/TV, ULS, RadioReference — not band allocations,
which may legitimately be quiet) is checked against what was actually
received. A predicted signal that never showed up gets an expectation level
and a concrete reason, so the report can say "we expected KXYZ at 98.5 and
here is why it is absent" instead of leaving the gap unexplained.

Expectation levels (heuristic, distance/service based):
  strong — full-power broadcast close enough that a working receive chain
           should hear it; absence demands an explanation
  medium — broadcast at the fringe, or AM (poor match for a stock whip)
  weak   — land-mobile/paging records: legitimately intermittent; absence
           during a finite sweep window is normal

Pure logic + DB reads; unit tested with synthetic data.
"""

import re

from .correlation import _matches  # same record-matching rules as classification

BROADCAST_STRONG_MI = 30.0
BROADCAST_MEDIUM_MI = 50.0
AM_STRONG_MI = 15.0

# ERP thresholds (kW). Below LOW_POWER a station is an LPFM/translator that is
# faint even a few miles out; a full-power class-B/C is tens of kW. Distance-only
# expectation over-flagged these tiny stations as "strong — investigate", so we
# grade by POWER first (the FCC record carries ERP) and only then by distance.
FULL_POWER_KW = 6.0
LOW_POWER_KW = 1.0


def parse_erp_kw(record: dict):
    """Pull the ERP out of the FCC record (stored like '0.432  kW' / '8.3 kW').
    Returns kW as float, or None if unknown/unparseable."""
    raw = record.get("erp")
    if raw is None or raw == "":
        return None
    if isinstance(raw, (int, float)):
        return float(raw) or None
    m = re.search(r"([\d.]+)", str(raw))
    if not m:
        return None
    try:
        val = float(m.group(1))
    except ValueError:
        return None
    return val if val > 0 else None


def expectation_level(record: dict) -> tuple[str, str]:
    """Returns (level, basis). record = baseline row with confidence='record'.

    POWER-AWARE: a distant full-power station is more expected than a nearby
    low-power translator. The old distance-only version labelled every FM within
    30 mi 'strong' — so 50-watt translators and LPFMs got flagged as strong action
    items when they are genuinely faint, making the report cry wolf."""
    service = (record.get("service") or "").upper()
    dist = record.get("distance_mi")
    if service == "FM" or service == "TV":
        erp = parse_erp_kw(record)
        pwr = f", {erp:g} kW" if erp is not None else ""
        # low-power translator / LPFM: faint even close; never a "strong" action item
        if erp is not None and erp < LOW_POWER_KW:
            if dist is not None and dist <= 8.0:
                return "medium", f"low-power {service}{pwr} — only receivable close in ({dist:.0f} mi)"
            return "weak", f"low-power {service}{pwr} translator/LPFM — faint at {dist:.0f} mi, absence expected" if dist \
                else f"low-power {service}{pwr} — faint; absence expected"
        # moderate power: expected close, fringe otherwise
        if erp is not None and erp < FULL_POWER_KW:
            if dist is not None and dist <= 15.0:
                return "medium", f"moderate-power {service}{pwr} {dist:.0f} mi away"
            return "weak", f"moderate-power {service}{pwr} at distance ({dist:.0f} mi)" if dist \
                else f"moderate-power {service}{pwr}, distance unknown"
        # full power (or unknown ERP): grade by distance as before
        if dist is None:
            return "medium", f"{service} broadcast{pwr}, distance unknown"
        if dist <= BROADCAST_STRONG_MI:
            return "strong", f"full-power {service} broadcast{pwr} {dist:.0f} mi away"
        if dist <= BROADCAST_MEDIUM_MI:
            return "medium", f"{service} broadcast{pwr} at the fringe ({dist:.0f} mi)"
        return "weak", f"{service} broadcast{pwr} beyond reliable range ({dist:.0f} mi)"
    if service == "AM":
        if dist is not None and dist <= AM_STRONG_MI:
            return "medium", (f"AM station {dist:.0f} mi away — receivable, but the "
                              "stock whip is a poor match below 2 MHz")
        return "weak", "AM at distance — absence on a short whip is expected"
    # land mobile / paging / county records transmit intermittently
    return "weak", f"{record.get('label') or service or 'land mobile'} — intermittent service"


def _record_swept(record: dict, segments: list[dict]) -> bool:
    lo, hi = record["freq_low_mhz"], record["freq_high_mhz"]
    return any(seg["low_mhz"] <= hi and seg["high_mhz"] >= lo for seg in segments)


def expected_for_range(baseline_rows: list[dict], low_mhz: float,
                       high_mhz: float) -> list[dict]:
    """The specific licensed records (confidence='record') whose frequency
    overlaps [low,high] — i.e. what we EXPECT to hear in the segment the radio
    is on right now. Each entry starts status='looking'; the live sweep loop
    flips it to 'found' on detection. Pure (no DB); unit tested.

    Carries the fields correlation._matches needs (freq_low/high, service,
    channel_bw_khz) so the caller can match detections back to these entries
    with the exact same rule the report uses.
    """
    out = []
    for rec in baseline_rows:
        if (rec.get("confidence") or "") != "record":
            continue
        lo = rec.get("freq_low_mhz")
        hi = rec.get("freq_high_mhz")
        if lo is None or hi is None:
            continue
        if lo <= high_mhz and hi >= low_mhz:        # overlap, not center-only
            level, basis = expectation_level(rec)
            out.append({
                "freq_low_mhz": lo, "freq_high_mhz": hi,
                "center_mhz": round((lo + hi) / 2.0, 4),
                "service": rec.get("service"),
                "channel_bw_khz": rec.get("channel_bw_khz"),
                "callsign": rec.get("callsign"),
                "label": rec.get("label") or rec.get("callsign") or rec.get("service"),
                "distance_mi": rec.get("distance_mi"),
                "expectation": level, "basis": basis,
                "status": "looking",
            })
    order = {"strong": 0, "medium": 1, "weak": 2}
    out.sort(key=lambda r: (order.get(r["expectation"], 3), r["center_mhz"]))
    return out


def _absence_reason(record: dict, level: str, swept: bool) -> str:
    if not swept:
        return ("frequency was NOT covered by any completed sweep segment — "
                "module not swept, phase aborted early, or range gap")
    service = (record.get("service") or "").upper()
    if service in ("FM", "TV") and level == "strong":
        return ("full-power station swept but not detected — most likely it fell under the "
                "threshold of a wide first-look/ambient sweep (higher noise floor, coarser "
                "resolution than a targeted pass). Re-run a NARROW sweep of just this band to "
                "confirm; if still absent, check the antenna/connector on this band or the "
                "station is off-air (verify with a car radio)")
    if service in ("FM", "TV"):
        return ("swept but below detection threshold — expected for a lower-power or fringe "
                "station on a wide ambient sweep; a targeted band sweep would likely catch it")
    if service == "AM":
        return ("swept but not detected — expected with a short whip below 2 MHz; "
                "use a loop antenna if AM verification matters for this job")
    return ("swept but silent during the sweep window — normal for intermittent "
            "land-mobile/paging traffic; not a receive-chain concern")


def expected_vs_found(db, session_id: int) -> dict:
    records = db.query(
        "SELECT * FROM baseline WHERE session_id=? AND confidence='record' "
        "ORDER BY freq_low_mhz", (session_id,))
    segments = db.query(
        "SELECT seg.low_mhz, seg.high_mhz FROM segments seg "
        "JOIN sweeps w ON seg.sweep_id=w.id "
        "WHERE w.session_id=? AND w.status IN ('done','aborted') "
        "AND seg.status='done'", (session_id,))
    signals = db.query(
        "SELECT center_mhz, peak_dbm, phase, room FROM signals WHERE session_id=?",
        (session_id,))

    rows, counts = [], {"found": 0, "missing_strong": 0, "missing_medium": 0,
                        "missing_weak": 0, "not_swept": 0}
    for rec in records:
        level, basis = expectation_level(rec)
        hits = [s for s in signals if _matches(rec, s["center_mhz"])]
        swept = _record_swept(rec, segments)
        found = bool(hits)
        if found:
            counts["found"] += 1
            best = max(hits, key=lambda s: s["peak_dbm"] or -200)
            reason = (f"received at {best['peak_dbm']:.0f} dBm "
                      f"({best['phase']}{', ' + best['room'] if best['room'] else ''})")
        else:
            if not swept:
                counts["not_swept"] += 1
            else:
                counts[f"missing_{level}"] += 1
            reason = _absence_reason(rec, level, swept)
        rows.append({
            "freq_low_mhz": rec["freq_low_mhz"], "freq_high_mhz": rec["freq_high_mhz"],
            "service": rec["service"], "callsign": rec["callsign"],
            "label": rec["label"], "licensee": rec["licensee"],
            "distance_mi": rec["distance_mi"], "source": rec["source"],
            "expectation": level, "basis": basis,
            "found": found, "swept": swept, "reason": reason,
        })

    # order: missing-strong first (the action items), then medium, weak, found
    order = {"strong": 0, "medium": 1, "weak": 2}
    rows.sort(key=lambda r: (r["found"], order.get(r["expectation"], 3),
                             r["freq_low_mhz"]))
    counts["total_records"] = len(records)
    return {"rows": rows, "counts": counts}
