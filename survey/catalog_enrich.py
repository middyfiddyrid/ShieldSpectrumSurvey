"""Optional, offline-first catalog enrichment for an RF observation.

Local matching remains authoritative and immediate. When explicitly enabled in
config.yaml, this module performs a short, read-only MediaWiki search and caches
the result. Network errors never prevent or downgrade the local assessment.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import re
import time
from urllib.parse import quote

import requests


log = logging.getLogger("survey.catalog_enrich")
_TAG_RE = re.compile(r"<[^>]+>")


def _clean(value: str) -> str:
    return " ".join(html.unescape(_TAG_RE.sub(" ", value or "")).split())


def _queries(center_mhz: float, candidates: list[dict]) -> list[str]:
    terms = []
    for candidate in candidates[:3]:
        name = str(candidate.get("name") or "").strip()
        if name and name.lower() not in {"unknown", "unmatched spectral energy"}:
            terms.append(f'"{name}"')
    terms.extend((f'"{center_mhz:.3f} MHz"', f'"{center_mhz:.1f} MHz"'))
    return list(dict.fromkeys(terms))


def _cache_path(cfg, key: str) -> str:
    cache_dir = cfg.get("cache", "dir", default="cache") or "cache"
    if not os.path.isabs(cache_dir):
        root = os.path.dirname(getattr(cfg, "path", "") or os.getcwd())
        cache_dir = os.path.join(root, cache_dir)
    return os.path.join(cache_dir, "catalog_enrichment", f"{key}.json")


def _read_cache(path: str, max_age_hours: float) -> dict | None:
    try:
        if time.time() - os.path.getmtime(path) > max_age_hours * 3600:
            return None
        with open(path, "r", encoding="utf-8") as fh:
            value = json.load(fh)
        value["status"] = "cache"
        return value
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return None


def _write_cache(path: str, value: dict) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        pending = path + ".tmp"
        with open(pending, "w", encoding="utf-8") as fh:
            json.dump(value, fh, ensure_ascii=False, indent=2)
        os.replace(pending, path)
    except OSError:
        log.exception("could not cache catalog enrichment")


def enrich_catalog(center_mhz: float, candidates: list[dict], cfg) -> dict:
    """Return optional MediaWiki corroboration without weakening local results."""
    if not cfg.get("catalog_enrichment", "enabled", default=False):
        return {"enabled": False, "status": "disabled", "items": [], "errors": []}

    sources = [source for source in
               (cfg.get("catalog_enrichment", "sources", default=[]) or [])
               if source.get("enabled", True)]
    queries = _queries(center_mhz, candidates)
    key_data = json.dumps({"mhz": round(center_mhz, 4), "queries": queries,
                           "sources": sources}, sort_keys=True).encode("utf-8")
    cache_path = _cache_path(cfg, hashlib.sha256(key_data).hexdigest()[:24])
    max_age = float(cfg.get("catalog_enrichment", "cache_hours", default=168) or 168)
    cached = _read_cache(cache_path, max_age)
    if cached is not None:
        return cached

    timeout = max(0.5, min(10.0, float(cfg.get(
        "catalog_enrichment", "timeout_seconds", default=3.0) or 3.0)))
    limit = max(1, min(20, int(cfg.get(
        "catalog_enrichment", "max_results", default=6) or 6)))
    items, errors, successful_sources = [], [], 0
    seen = set()
    search = " OR ".join(queries[:5])
    for source in sources:
        name = str(source.get("name") or "MediaWiki").strip()
        api_url = str(source.get("api_url") or "").strip()
        page_base = str(source.get("page_base_url") or "").strip()
        if not api_url.lower().startswith("https://") or not page_base.lower().startswith("https://"):
            errors.append(f"{name}: HTTPS endpoint required")
            continue
        try:
            response = requests.get(api_url, params={
                "action": "query", "list": "search", "srsearch": search,
                "srlimit": limit, "format": "json", "utf8": 1,
            }, timeout=timeout, headers={
                "User-Agent": "Sentry-RF-Intelligence/1.0 (field signal identification)"
            })
            response.raise_for_status()
            rows = (response.json().get("query") or {}).get("search") or []
            successful_sources += 1
            for row in rows:
                title = _clean(str(row.get("title") or ""))
                if not title:
                    continue
                identity = (name.lower(), title.lower())
                if identity in seen:
                    continue
                seen.add(identity)
                items.append({
                    "source": name,
                    "title": title,
                    "snippet": _clean(str(row.get("snippet") or ""))[:320],
                    "url": page_base.rstrip("/") + "/" + quote(
                        title.replace(" ", "_"), safe="()_-/"),
                })
                if len(items) >= limit:
                    break
        except (requests.RequestException, ValueError, TypeError) as exc:
            errors.append(f"{name}: {exc}")
        if len(items) >= limit:
            break

    result = {
        "enabled": True,
        "status": "online" if successful_sources else "offline",
        "query": search,
        "items": items[:limit],
        "errors": errors,
        "checked_utc": time.time(),
    }
    if successful_sources:
        _write_cache(cache_path, result)
    return result
