"""Local confirmed-signature library — match against YOUR field captures.

Operator saves a de-identified shape after confirming a device (clear/flag with
notes). Later sweeps score local refs higher than generic wiki alone.

No client address/PII in the ref payload. Pure file/JSON under data/sig_library/.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any

import numpy as np


def library_dir(base_dir: str) -> str:
    d = os.path.join(base_dir, "data", "sig_library")
    os.makedirs(d, exist_ok=True)
    return d


def _index_path(base_dir: str) -> str:
    return os.path.join(library_dir(base_dir), "index.json")


def load_index(base_dir: str) -> list[dict]:
    path = _index_path(base_dir)
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh) or {}
        return list(data.get("refs") or [])
    except (OSError, ValueError):
        return []


def save_index(base_dir: str, refs: list[dict]) -> None:
    path = _index_path(base_dir)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"version": 1, "refs": refs}, fh, indent=2)


def make_ref_from_signal(sig: dict, label: str, notes: str = "",
                         site_type: str = "") -> dict[str, Any]:
    """Build a library entry from a signals-row-like dict (+ optional behavior)."""
    beh = sig.get("behavior") if isinstance(sig.get("behavior"), dict) else {}
    return {
        "id": uuid.uuid4().hex[:12],
        "label": (label or "").strip() or "unnamed",
        "notes": (notes or "").strip()[:500],
        "site_type": (site_type or "").strip()[:80],
        "center_mhz": float(sig.get("center_mhz") or 0),
        "bw_khz": float(sig["bw_khz"]) if sig.get("bw_khz") is not None else None,
        "persistence": float(sig["persistence"]) if sig.get("persistence") is not None else None,
        "behavior_class": beh.get("class_id") or "",
        "modulation_class": (sig.get("_features") or {}).get("modulation_class")
            if isinstance(sig.get("_features"), dict) else "",
        "created_utc": time.time(),
        # no address / client name
    }


def add_ref(base_dir: str, ref: dict) -> dict:
    refs = load_index(base_dir)
    refs.append(ref)
    save_index(base_dir, refs)
    return ref


def delete_ref(base_dir: str, ref_id: str) -> bool:
    refs = load_index(base_dir)
    n = len(refs)
    refs = [r for r in refs if r.get("id") != ref_id]
    if len(refs) == n:
        return False
    save_index(base_dir, refs)
    return True


def match_local(center_mhz: float, bw_khz, behavior_class: str,
                refs: list[dict], top_n: int = 5) -> list[dict]:
    """Score local refs by frequency proximity + BW + behavior class."""
    out = []
    bw = float(bw_khz) if bw_khz is not None else None
    for ref in refs:
        score = 0.0
        reasons = []
        rc = float(ref.get("center_mhz") or 0)
        if not rc:
            continue
        # within 5% of center or 2 MHz, whichever larger for wideband
        tol = max(2.0, abs(rc) * 0.02)
        dist = abs(center_mhz - rc)
        if dist <= tol:
            score += 50.0 * (1.0 - dist / tol)
            reasons.append(f"within {dist:.3f} MHz of saved “{ref.get('label')}”")
        else:
            continue  # out of band for this ref
        rb = ref.get("bw_khz")
        if bw and rb:
            ratio = abs(np.log10(max(bw, 1) / max(float(rb), 1)))
            bws = max(0.0, 1.0 - ratio)
            score += 25.0 * bws
            if bws > 0.5:
                reasons.append("similar bandwidth")
        if behavior_class and ref.get("behavior_class") == behavior_class:
            score += 20.0
            reasons.append(f"same behavior class ({behavior_class})")
        out.append({
            "id": ref.get("id"),
            "label": ref.get("label"),
            "notes": ref.get("notes") or "",
            "site_type": ref.get("site_type") or "",
            "center_mhz": rc,
            "score": round(score, 1),
            "reasons": reasons,
            "phrase": (
                f"Matches your saved signature “{ref.get('label')}”"
                + (f" ({ref.get('site_type')})" if ref.get("site_type") else "")
                + f" — score {score:.0f}"
            ),
        })
    out.sort(key=lambda x: -x["score"])
    return out[:top_n]
