#!/usr/bin/env python3
"""sdb_data — shared Australian emergency / weather data sources.

Pure stdlib (urllib). Importable by the BBS process-transport plugin and the
standalone sdb-weather-watch.py.

Sources
-------
ABC Emergency  (https://www.abc.net.au/emergency-web/api)
    Aggregates bushfire / flood / storm / earthquake / heat from every
    state+territory emergency service. Search by state or geohash prefix.
BoM Data API   (https://api.weather.bom.gov.au)
    Official weather warnings (severe), observations, daily forecast
    (incl. fire_danger_category). Locations keyed by geohash.

Both return lists of ``dict`` with a common shape::

    {source, type, level, level_text, title, status, size, updated,
     coords: [lon,lat]|None, id}

``level`` is the Australian Warning System rank when available:
``extreme`` > ``severe`` > ``moderate`` > ``minor`` (else ``unknown``).
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request

USER_AGENT = "sdb-weather/0.2 (VK2JNC Riverina ops)"

ABC_BASE = "https://www.abc.net.au/emergency-web/api"
BOM_BASE = "https://api.weather.bom.gov.au/v1"

# Australian Warning System ordering, for --severe-only / --min-level filters.
LEVEL_RANK = {"extreme": 4, "severe": 3, "moderate": 2, "minor": 1, "unknown": 0}


def _get(url: str, timeout: int = 25):
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


# ── ABC Emergency ─────────────────────────────────────────────────────────────

def _norm_abc(e: dict) -> dict:
    lab = e.get("eventLabelPrepared") or {}
    lvl = e.get("alertLevelInfoPrepared") or {}
    ts = e.get("emergencyTimestampPrepared") or {}
    cb = e.get("cardBody") or {}
    geo = e.get("geometry") or {}
    coords = geo.get("coordinates") if geo.get("type") == "Point" else None
    return {
        "source": cb.get("source") or "ABC Emergency",
        "type": lab.get("labelText") or lab.get("icon") or "Emergency",
        "level": (lvl.get("level") or "unknown").lower(),
        "level_text": lvl.get("text"),
        "title": e.get("headline") or "(no headline)",
        "status": cb.get("status"),
        "size": cb.get("size"),
        "updated": ts.get("updatedTime") or ts.get("date"),
        "coords": coords,
        "id": e.get("id"),
    }


def abc_by_state(state: str) -> list[dict]:
    d = _get(f"{ABC_BASE}/emergencySearch?state={urllib.parse.quote(state.lower())}")
    return [_norm_abc(e) for e in d.get("emergencies", [])]


def abc_by_geohash(geohashes: list[str]) -> list[dict]:
    gh = urllib.parse.quote(json.dumps(geohashes))
    d = _get(f"{ABC_BASE}/emergencySearch?geohashes={gh}")
    return [_norm_abc(e) for e in d.get("emergencies", [])]


# ── BoM Data API ──────────────────────────────────────────────────────────────

def bom_location(geohash: str) -> dict | None:
    d = _get(f"{BOM_BASE}/locations/{geohash}")
    return d.get("data") or None


def bom_resolve(postcode_or_place: str) -> list[dict]:
    """Return matching locations (name, geohash, lat, lon)."""
    d = _get(f"{BOM_BASE}/locations?search={urllib.parse.quote(postcode_or_place)}")
    return d.get("data", [])


def _norm_bom_warning(w: dict) -> dict:
    grp = (w.get("warning_group_type") or "unknown").lower()
    # BoM warning_group_type is already an AWS-style rank; map synonyms.
    rank = {"extreme": "extreme", "severe": "severe", "major": "severe",
            "significant": "moderate", "moderate": "moderate",
            "minor": "minor"}.get(grp, grp)
    return {
        "source": "Bureau of Meteorology",
        "type": w.get("type") or "Warning",
        "level": rank,
        "level_text": w.get("warning_group_type"),
        "title": w.get("title") or "(untitled warning)",
        "status": w.get("area_text") or w.get("state"),
        "size": None,
        "updated": w.get("issue_time"),
        "coords": None,
        "id": w.get("id"),
        "expires": w.get("expiry_time"),
        "area_id": w.get("area_id"),
    }


def bom_warnings(geohash: str) -> list[dict]:
    d = _get(f"{BOM_BASE}/locations/{geohash}/warnings")
    return [_norm_bom_warning(w) for w in d.get("data", [])]


# ── Composite "watch these areas" ─────────────────────────────────────────────

def fetch_abc(states: list[str], geohashes: list[str]) -> list[dict]:
    """Pull ABC emergencies for the given states and/or geohash prefixes."""
    out: list[dict] = []
    for s in states:
        out.extend(abc_by_state(s))
    if geohashes:
        out.extend(abc_by_geohash(geohashes))
    return out


def fetch_bom_warnings(geohashes: list[str]) -> list[dict]:
    out: list[dict] = []
    for gh in geohashes:
        out.extend(bom_warnings(gh))
    return out
