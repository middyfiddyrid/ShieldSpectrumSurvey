"""New-to-environment (novelty) detection.

A recurring address accrues many sweeps over time. A signal is "novel to the
address" when no PRIOR sweep at the same address ever logged an emitter within
tolerance of its frequency. Built entirely on the Signal Wiki cross-session
history (the `captures` table, archived per sweep by sigcapture), joined to
`sessions.address`. Pure SQL + dicts — no radio, no numpy.

Novelty is informational only: it never feeds back into anomaly_score or signal
classification (those stay frequency/baseline-only).
"""

import time

TOL_MHZ = 0.25


def _fmt_date(utc):
    if not utc:
        return None
    return time.strftime("%Y-%m-%d", time.localtime(utc))


def compute_novelty(db, address, center_mhz, before_utc, tol_mhz=TOL_MHZ,
                    cur_peak_dbm=None, cur_bw_khz=None):
    """Was a signal at `center_mhz` seen at `address` before `before_utc`?

    Returns a dict describing novelty + what is DIFFERENT vs prior sightings:
    new frequency, new since the last visit date, and power/BW deltas.
    `before_utc` is normally the current session's created_utc, which excludes
    this visit's own observations.
    """
    # Prior sightings of THIS emitter (freq-scoped) at this address.
    rows = db.query(
        "SELECT c.center_mhz, c.bw_khz, c.peak_dbm, c.captured_utc, c.session_id "
        "FROM captures c JOIN sessions s ON c.session_id=s.id "
        "WHERE s.address=? AND ABS(c.center_mhz - ?) <= ? AND c.captured_utc < ? "
        "ORDER BY c.captured_utc ASC",
        (address, center_mhz, tol_mhz, before_utc))
    prior_sessions = sorted({r["session_id"] for r in rows})
    is_novel = len(prior_sessions) == 0
    first_seen = rows[0]["captured_utc"] if rows else None
    last_seen = rows[-1]["captured_utc"] if rows else None

    # Most recent PRIOR visit to this address (any frequency) — the baseline a
    # novel signal is "new since".
    last_visit = db.query_one(
        "SELECT MAX(c.captured_utc) AS t FROM captures c "
        "JOIN sessions s ON c.session_id=s.id "
        "WHERE s.address=? AND c.captured_utc < ?", (address, before_utc))
    last_visit_utc = last_visit["t"] if last_visit else None

    diffs = {"new_freq": is_novel, "new_since": None,
             "power_delta_db": None, "bw_delta_khz": None}
    reasons = []
    if is_novel:
        if last_visit_utc:
            diffs["new_since"] = _fmt_date(last_visit_utc)
            reasons.append(f"new frequency — not seen at this address "
                           f"(prior sweep {diffs['new_since']})")
        else:
            reasons.append("new frequency — first sweep recorded at this address")
    else:
        prior_peaks = [r["peak_dbm"] for r in rows if r["peak_dbm"] is not None]
        prior_bws = [r["bw_khz"] for r in rows if r["bw_khz"]]
        if cur_peak_dbm is not None and prior_peaks:
            base = sum(prior_peaks) / len(prior_peaks)
            diffs["power_delta_db"] = round(cur_peak_dbm - base, 1)
            if abs(diffs["power_delta_db"]) >= 10:
                reasons.append(f"power changed {diffs['power_delta_db']:+.0f} dB "
                               f"vs prior sightings")
        if cur_bw_khz is not None and prior_bws:
            base_bw = sum(prior_bws) / len(prior_bws)
            diffs["bw_delta_khz"] = round(cur_bw_khz - base_bw, 1)
            if base_bw and abs(diffs["bw_delta_khz"]) >= max(base_bw, 1) * 0.5:
                reasons.append(f"bandwidth changed {diffs['bw_delta_khz']:+.0f} kHz "
                               f"vs prior sightings")

    return {
        "center_mhz": round(center_mhz, 5),
        "is_novel_to_address": is_novel,
        "first_seen_utc": first_seen,
        "last_seen_utc": last_seen,
        "prior_sessions": len(prior_sessions),
        "variant_count": len(rows),
        "differences": diffs,
        "novelty_reason": "; ".join(reasons),
        "timeline": [
            {"session_id": r["session_id"], "captured_utc": r["captured_utc"],
             "peak_dbm": r["peak_dbm"], "bw_khz": r["bw_khz"]} for r in rows],
    }


def mark_session_novelty(db, session_id):
    """After a sweep, tag every detection in the session with its novelty vs the
    address's PRIOR sweep history. Stores is_novel_to_address + novelty_reason on
    the signals row. Returns the count of signals marked novel."""
    sess = db.query_one("SELECT address, created_utc FROM sessions WHERE id=?",
                        (session_id,))
    if not sess:
        return 0
    address, created = sess["address"], sess["created_utc"]
    signals = db.query(
        "SELECT id, center_mhz, peak_dbm, bw_khz FROM signals WHERE session_id=?",
        (session_id,))
    novel = 0
    for s in signals:
        nov = compute_novelty(db, address, s["center_mhz"], created,
                              cur_peak_dbm=s["peak_dbm"], cur_bw_khz=s["bw_khz"])
        is_novel = 1 if nov["is_novel_to_address"] else 0
        db.execute(
            "UPDATE signals SET is_novel_to_address=?, novelty_reason=? WHERE id=?",
            (is_novel, nov["novelty_reason"], s["id"]))
        novel += is_novel
    return novel
