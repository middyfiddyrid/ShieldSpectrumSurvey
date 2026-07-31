"""Guided field experiments — airplane-mode, inventory subtract, DECT unplug,
key-fob press. Pure logic + DB notes; no content demod, no jamming.

Each experiment records an outcome on the signal (or session) as structured
notes the operator can re-open. Language stays pass/fail of the *test*, not
"threat confirmed."
"""

from __future__ import annotations

import json
import time
from typing import Any

# Allowed experiment kinds
KINDS = (
    "phone_test",        # cellular uplink correlation
    "inventory_subtract",  # power off known gear
    "dect_unplug",
    "keyfob_press",
)


def validate_kind(kind: str) -> str:
    k = (kind or "").strip().lower()
    if k not in KINDS:
        raise ValueError(f"unknown experiment kind {kind!r}; use one of {KINDS}")
    return k


def build_record(kind: str, body: dict) -> dict[str, Any]:
    """Normalize a POST body into a storable experiment record."""
    kind = validate_kind(kind)
    outcome = (body.get("outcome") or "").strip().lower()
    # outcomes are free-ish but we normalize common ones
    allowed = {
        "phone_test": ("energy_dropped", "energy_remains", "inconclusive", "aborted"),
        "inventory_subtract": ("residue_gone", "residue_remains", "inconclusive", "aborted"),
        "dect_unplug": ("energy_dropped", "energy_remains", "inconclusive", "aborted"),
        "keyfob_press": ("correlated", "uncorrelated", "inconclusive", "aborted"),
    }
    if outcome and outcome not in allowed[kind]:
        # allow custom but tag
        outcome = outcome or "inconclusive"
    notes = (body.get("notes") or "").strip()
    label = (body.get("label") or "").strip()  # e.g. "front door fob"
    rec = {
        "kind": kind,
        "outcome": outcome or "inconclusive",
        "notes": notes,
        "label": label,
        "utc": time.time(),
        "operator": (body.get("operator") or "").strip(),
    }
    if kind == "phone_test":
        rec["before_count"] = body.get("before_count")
        rec["after_count"] = body.get("after_count")
    if kind == "inventory_subtract":
        rec["powered_off"] = body.get("powered_off") or []
    return rec


def summarize_outcome(rec: dict) -> str:
    kind = rec.get("kind", "")
    o = rec.get("outcome", "")
    labels = {
        ("phone_test", "energy_dropped"):
            "Phone test: uplink energy dropped when phones silent — consistent with client phones.",
        ("phone_test", "energy_remains"):
            "Phone test: energy REMAINED with phones silent — investigate fixed cellular device.",
        ("inventory_subtract", "residue_gone"):
            "Inventory subtract: residue gone after powering off known gear — likely client-owned.",
        ("inventory_subtract", "residue_remains"):
            "Inventory subtract: residue REMAINS after known gear off — prioritize DF.",
        ("dect_unplug", "energy_dropped"):
            "DECT unplug: energy dropped — consistent with cordless base.",
        ("dect_unplug", "energy_remains"):
            "DECT unplug: energy REMAINED — investigate DECT-band device.",
        ("keyfob_press", "correlated"):
            f"Key-fob press correlated"
            + (f" with “{rec.get('label')}”" if rec.get("label") else "")
            + " — mark known remote.",
        ("keyfob_press", "uncorrelated"):
            "Key-fob press: burst did NOT match known remotes — leave flagged.",
    }
    s = labels.get((kind, o))
    if s:
        return s
    return f"{kind}: {o}" + (f" — {rec['notes']}" if rec.get("notes") else "")


def append_experiment_json(existing: str | None, rec: dict) -> str:
    """Append one experiment to a JSON list stored on the signal row."""
    try:
        data = json.loads(existing) if existing else []
        if not isinstance(data, list):
            data = []
    except (TypeError, ValueError):
        data = []
    data.append(rec)
    return json.dumps(data)


def parse_experiments(raw: str | None) -> list[dict]:
    try:
        data = json.loads(raw) if raw else []
        return data if isinstance(data, list) else []
    except (TypeError, ValueError):
        return []


def next_step_hint(kind: str) -> dict:
    """UI copy for starting each experiment."""
    copy = {
        "phone_test": {
            "title": "Phone correlation test",
            "steps": [
                "Note current cellular-uplink hits (or count busy phones).",
                "All phones / tablets → airplane mode (or Faraday pouch).",
                "Wait 30–60 s; re-check uplink bands or re-sweep.",
                "Record: energy dropped vs remains.",
            ],
        },
        "inventory_subtract": {
            "title": "Inventory subtract",
            "steps": [
                "Mark client-owned Wi-Fi/BLE/LAN gear in inventory.",
                "Power off known APs, cameras, TVs, dongles listed.",
                "Re-scan inventory + re-check RF hits on those channels.",
                "Record: residue gone vs remains (DF residue).",
            ],
        },
        "dect_unplug": {
            "title": "DECT base unplug",
            "steps": [
                "Locate cordless phone bases.",
                "Unplug all bases (and baby monitors if DECT).",
                "Re-check 1880–1930 MHz (or your DECT allocation).",
                "Record: energy dropped vs remains.",
            ],
        },
        "keyfob_press": {
            "title": "Key-fob / remote press",
            "steps": [
                "Operator watches 315/433/915 (or the hit frequency).",
                "Press each known client remote once; note time/label.",
                "If the RF burst aligns → correlated (clear as known).",
                "Bursts with no press → leave flagged.",
            ],
        },
    }
    return copy.get(kind, {"title": kind, "steps": []})
