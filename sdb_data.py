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

# ABC's `state=` filter is intermittently ignored (load-balancer serves the
# national set on some responses) — seen live: `state=nsw` returned QLD + VIC
# + ACT items ~half the time. The card's cardBody.source IS stable, so map
# source → state and filter by it.
_STATE_BY_PREFIX = {
    "nsw": ["nsw rural fire service", "fire and rescue nsw",
            "nsw national parks", "forestry corporation of nsw"],
    "act": ["act emergency services agency"],
    "qld": ["queensland fire department"],
    "vic": ["vic state emergency service", "vic other agencies",
            "emergency management victoria"],
    "tas": ["tas state emergency", "tas other agencies"],
    "sa":  ["sa state emergency", "sa other agencies"],
    "wa":  ["wa state emergency", "wa other agencies"],
    "nt":  ["nt emergency services"],
}
_PREFIX_TO_STATE = {s[:3]: st for st, srcs in _STATE_BY_PREFIX.items() for s in srcs}
# sources whose state we can't derive (keep when BoM, drop otherwise)
_BOM_SRC_PREFIX = "australian government"
_UNKNOWN_SRC = "zz"


def _state_of_source(src: str) -> str:
    sl = (src or "").lower()
    for prefix, st in _PREFIX_TO_STATE.items():
        if sl.startswith(prefix):
            return st
    if sl.startswith(_BOM_SRC_PREFIX) or sl.startswith("bureau of meteorology"):
        return "bom"
    return _UNKNOWN_SRC


# rough lon/lat bounding boxes (min_lon, min_lat, max_lon, max_lat) for
# source-less items (BoM / unknown). Border overlaps resolved by check order.
_STATE_BOX_ORDER = ["act", "tas", "vic", "nsw", "sa", "qld", "nt", "wa"]
_STATE_BOX = {
    "wa":  (112.9, -39.5, 129.5, -11.9),
    "nt":  (129.0, -26.1, 138.1, -11.7),
    "qld": (136.7, -24.2, 153.6, -10.7),
    "nsw": (140.4, -39.2, 153.6, -28.2),
    "vic": (140.4, -39.2, 150.2, -36.4),
    "tas": (144.1, -43.7, 148.6, -40.5),
    "sa":  (129.0, -38.2, 141.0, -25.9),
    "act": (148.95, -35.45, 149.45, -35.10),
}


def _state_of_coord(lon: float, lat: float) -> str:
    for st in _STATE_BOX_ORDER:
        x0, y0, x1, y1 = _STATE_BOX[st]
        if x0 <= lon <= x1 and y0 <= lat <= y1:
            return st
    return _UNKNOWN_SRC


def _collect_points(o, out: list) -> None:
    """Recursively find [lon, lat] pairs in a GeoJSON geometry structure."""
    if isinstance(o, (list, tuple)):
        if (len(o) == 2
                and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in o)):
            lon, lat = o
            if -180 <= lon <= 180 and -90 <= lat <= 90:
                out.append((lon, lat))
        for v in o:
            _collect_points(v, out)
    elif isinstance(o, dict):
        for v in o.values():
            _collect_points(v, out)


def geo_sig(geo: dict | None) -> str:
    """A stable, render-independent signature of a card's geometry.

    Point      -> "lon.lat"
    Polygon-ish -> rounded bbox "minlon.minlat.maxlon.maxlat"
    None/other -> ""
    """
    if not isinstance(geo, dict):
        return ""
    pts: list = []
    _collect_points(geo, pts)
    if not pts:
        return ""
    if len(pts) == 1:
        # 2dp (~1km) — coarse enough that ABC's per-render coordinate jitter
        # doesn't change the key, fine enough that two genuinely distinct
        # fires (separate roads, >1km apart) still differ.
        lon, lat = pts[0]
        return f"{lon:.2f},{lat:.2f}"
    lons = [p[0] for p in pts]
    lats = [p[1] for p in pts]
    return f"{min(lons):.2f},{min(lats):.2f},{max(lons):.2f},{max(lats):.2f}"


def filter_by_state(items: list[dict], states: list[str]) -> list[dict]:
    """Drop items whose emergency service is not in the requested states.

    ABC's `state=` query filter is unreliable (intermittently returns the
    national set), so we filter on cardBody.source instead. Items with a
    BoM/unknown source are kept when watching a single state, or routed by
    their coordinates when watching several.
    """
    requested = {s.lower() for s in states}
    out = []
    for it in items:
        st = _state_of_source(it.get("source", ""))
        if st in requested:
            out.append(it)
            continue
        if st not in ("bom", _UNKNOWN_SRC):
            continue  # other-state service -> drop
        if len(requested) == 1:
            out.append(it)  # single-state watch: keep source-less items
            continue
        coords = it.get("coords")
        if coords and len(coords) >= 2:
            if _state_of_coord(coords[0], coords[1]) in requested:
                out.append(it)
    return out


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
        "geo_sig": geo_sig(geo),
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
        "geo_sig": "",
    }


def bom_warnings(geohash: str) -> list[dict]:
    """Warnings for a BoM location geohash; [] if the geohash is unknown.

    A typo'd/invalid geohash returns HTTP 400 — swallow it per-location so
    one bad geohash can't take out the whole weather fetch.
    """
    try:
        d = _get(f"{BOM_BASE}/locations/{geohash}/warnings")
    except Exception:
        return []
    return [_norm_bom_warning(w) for w in d.get("data", [])]


# ── Composite "watch these areas" ─────────────────────────────────────────────

def fetch_abc(states: list[str], geohashes: list[str]) -> list[dict]:
    """Pull ABC emergencies for the given states and/or geohash prefixes."""
    out: list[dict] = []
    for s in states:
        out.extend(abc_by_state(s))
    if geohashes:
        out.extend(abc_by_geohash(geohashes))
    # ABC's `state=` query is intermittently answered with the national
    # feed by its load balancer — re-filter on the card's own source so
    # QLD/VIC etc. don't leak into an NSW-only watch.
    if states:
        out = filter_by_state(out, states)
    return out


def fetch_bom_warnings(geohashes: list[str]) -> list[dict]:
    out: list[dict] = []
    for gh in geohashes:
        out.extend(bom_warnings(gh))
    return out
