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

# sentinel so "caller passed last_visit_utc=None" (a genuinely first-ever visit)
# is distinguishable from "caller did not pass it at all"
_UNSET = object()


def _fmt_date(utc):
    if not utc:
        return None
    return time.strftime("%Y-%m-%d", time.localtime(utc))


def last_visit_utc_for(db, address, before_utc):
    """Most recent PRIOR observation at this address (any frequency) — the
    baseline a novel signal is "new since". Depends only on the address and the
    cutoff, so a whole-session pass computes it ONCE and hands it to every
    compute_novelty call instead of re-running this scan per signal."""
    row = db.query_one(
        "SELECT MAX(c.captured_utc) AS t FROM captures c "
        "JOIN sessions s ON c.session_id=s.id "
        "WHERE s.address=? AND c.captured_utc < ?", (address, before_utc))
    return row["t"] if row else None


def compute_novelty(db, address, center_mhz, before_utc, tol_mhz=TOL_MHZ,
                    cur_peak_dbm=None, cur_bw_khz=None, last_visit_utc=_UNSET):
    """Was a signal at `center_mhz` seen at `address` before `before_utc`?

    Returns a dict describing novelty + what is DIFFERENT vs prior sightings:
    new frequency, new since the last visit date, and power/BW deltas.
    `before_utc` is normally the current session's created_utc, which excludes
    this visit's own observations. Pass `last_visit_utc` to reuse a value already
    computed for this address (see last_visit_utc_for); omit it and this looks it
    up itself, so single-signal callers are unchanged.
    """
    # Prior sightings of THIS emitter (freq-scoped) at this address.
    # Expressed as a BETWEEN range, NOT ABS(c.center_mhz - ?) <= ?: a function of
    # the column is not sargable, so the ABS form could never use
    # idx_captures_freq and every call fell back to a full scan of captures
    # (22k rows and growing, 15k of them at the operator's own address).
    rows = db.query(
        "SELECT c.center_mhz, c.bw_khz, c.peak_dbm, c.captured_utc, c.session_id "
        "FROM captures c JOIN sessions s ON c.session_id=s.id "
        "WHERE c.center_mhz BETWEEN ? AND ? AND s.address=? AND c.captured_utc < ? "
        "ORDER BY c.captured_utc ASC",
        (center_mhz - tol_mhz, center_mhz + tol_mhz, address, before_utc))
    prior_sessions = sorted({r["session_id"] for r in rows})
    is_novel = len(prior_sessions) == 0
    first_seen = rows[0]["captured_utc"] if rows else None
    last_seen = rows[-1]["captured_utc"] if rows else None

    if last_visit_utc is _UNSET:
        last_visit_utc = last_visit_utc_for(db, address, before_utc)

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
    # address-wide and identical for every signal — hoisted out of the loop so a
    # 1500-signal session runs this scan once instead of 1500 times
    last_visit = last_visit_utc_for(db, address, created)
    novel = 0
    updates = []
    for s in signals:
        nov = compute_novelty(db, address, s["center_mhz"], created,
                              cur_peak_dbm=s["peak_dbm"], cur_bw_khz=s["bw_khz"],
                              last_visit_utc=last_visit)
        is_novel = 1 if nov["is_novel_to_address"] else 0
        updates.append((is_novel, nov["novelty_reason"], s["id"]))
        novel += is_novel
    # ONE transaction, not one per signal. db.execute() opens a fresh connection
    # and commits every call, so the old per-signal UPDATE cost a full fsync each
    # time: on 2026-08-06 a 1483-signal session spent ~11 MINUTES here while the
    # sweep still read as running and ABORT/START were refused.
    if updates:
        db.executemany(
            "UPDATE signals SET is_novel_to_address=?, novelty_reason=? WHERE id=?",
            updates)
    return novel
