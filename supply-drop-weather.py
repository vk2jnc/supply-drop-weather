#!/usr/bin/env python3
"""supply-drop-weather — Australian emergency / severe-weather BBS plugin.

A Supply Drop **process transport** plugin. It holds ONE persistent
"publisher" session logged in as a service account, and posts a message to a
BBS room whenever a new emergency or weather warning is detected.

Data sources (see sdb_data.py):
  * ABC Emergency  — bushfire / flood / storm / earthquake / heat, all AUST
  * BoM Data API   — official weather warnings + fire danger

Design
------
The BBS spawns this process and talks line-delimited JSON over stdin/stdout
(the documented process-transport protocol). We:
  1. emit {"t":"ready"},
  2. open a single publisher connection ({"t":"open","id":"pub"}),
  3. log in: when the BBS sends the password prompt we send the credentials,
  4. run a poll loop that posts new items as multi-line messages
     ("E" -> text -> ".") and waits for the "Message posted" confirmation.

No BBS source changes. The BBS restarts us on crash (`restart_on_crash`) and
shows our stderr under `supply-drop-bbs plugin logs weather`.

Credentials come from env (set in the systemd drop-in, not this file):
  SDB_PUB_USER, SDB_PUB_PASS
"""
from __future__ import annotations

import json
import os
import re
import socket
import sys
import threading
import time
import traceback

try:
    import sdb_data
except Exception:  # pragma: no cover - import safety
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import sdb_data

VERSION = "0.3.0"
PUB_ID = "pub"
STATE_FILE = os.environ.get(
    "SDB_WEATHER_STATE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "weather-plugin-state.json"),
)

# ── args (set via `args` in the plugin's [[plugins.process]] config) ──────────
import argparse
_ap = argparse.ArgumentParser(add_help=False)
_ap.add_argument("--room", type=int, default=6, help="Emergency room id")
_ap.add_argument("--fire-room", type=int, default=None,
                 help="Fire Danger room id (else same as --room)")
_ap.add_argument("--states", default="nsw",
                 help="Comma-sep ABC states, e.g. nsw,vic (default: nsw)")
_ap.add_argument("--geohashes", default="", help="Comma-sep ABC geohash prefixes, e.g. r1r0,r38")
_ap.add_argument("--bom-geohashes", default="r1r0",
                 help="Comma-sep BoM geohash prefixes for weather warnings (default: r1r0 = Wagga)")
_ap.add_argument("--interval", type=int, default=300, help="Poll interval seconds")
_ap.add_argument("--severe-only", action="store_true",
                 help="Only post severe/extreme (BoM) & high (ABC) items")
_ap.add_argument("--min-level", default="", help="ABC: post at/above this level (minor/moderate/severe/extreme)")
_ap.add_argument("--notify-expiry", action="store_true", help="Post CLEARED when an item disappears")
_ap.add_argument("--fake", action="store_true", help="TEST: synthesize fake emergencies (no network)")
_ap.add_argument("--once", action="store_true", help="TEST: single poll then exit")
_args, _ = _ap.parse_known_args()

STATES = [s.strip() for s in _args.states.split(",") if s.strip()]
ABC_GEO = [g.strip() for g in _args.geohashes.split(",") if g.strip()]
BOM_GEO = [g.strip() for g in _args.bom_geohashes.split(",") if g.strip()]
FIRE_ROOM = _args.fire_room if _args.fire_room is not None else _args.room
USER = os.environ.get("SDB_PUB_USER")
PASS = os.environ.get("SDB_PUB_PASS")

# ── logging (stderr → `plugin logs weather`) ─────────────────────────────────
def log(msg: str) -> None:
    sys.stderr.write(f"[weather {time.strftime('%H:%M:%S')}] {msg}\n")
    sys.stderr.flush()

# ── state (dedup across restarts) ────────────────────────────────────────────
def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"posted": {}}

def save_state(st: dict) -> None:
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(st, f)
    except Exception as e:
        log(f"state save failed: {e}")

# ── formatting / routing / filtering ─────────────────────────────────────────
FIRE_TYPES = {"bushfire", "grass fire", "fire", "burn"}

def is_fire(item: dict) -> bool:
    return item["type"].strip().lower() in FIRE_TYPES

def item_room(item: dict) -> int:
    return FIRE_ROOM if is_fire(item) else _args.room

def passes_filter(item: dict) -> bool:
    if _args.severe_only:
        if item.get("level") not in ("severe", "extreme", "high"):
            return False
    if _args.min_level:
        rank = sdb_data.LEVEL_RANK.get(item.get("level", ""), 0)
        floor = sdb_data.LEVEL_RANK.get(_args.min_level.lower(), 0)
        if rank < floor:
            return False
    return True

def dedup_key(item: dict) -> str:
    # Stable across ABC re-renders. Deliberately excludes item["updated"] —
    # ABC refreshes that timestamp whenever it re-renders the feed, which
    # made the old key (with |updated) change every poll and re-post the
    # whole feed every 5 min. A genuinely re-issued warning keeps the same
    # id + title, so it stays deduped (that's what we want).
    src = item.get("source", "")[:20]
    ident = item.get("id") or item.get("title")
    return f"{src}|{ident}"

def render(item: dict) -> str:
    lvl = (item.get("level_text") or item.get("level") or "").strip()
    lines = []
    head = f"*NEW* {lvl} — {item['type']}: {item['title']}".strip()
    lines.append(head)
    if item.get("status"):
        lines.append(f"Status: {item['status']}")
    if item.get("size"):
        lines.append(f"Size: {item['size']}")
    if item.get("source"):
        lines.append(f"Source: {item['source']}")
    if item.get("updated"):
        lines.append(f"Updated: {item['updated'][:19].replace('T', ' ')}")
    return "\n".join(lines)

# ── fake data for testing ────────────────────────────────────────────────────
_fake_n = {"i": 0}
def fake_items() -> list[dict]:
    _fake_n["i"] += 1
    n = _fake_n["i"]
    return [{
        "source": "TEST (fake)", "type": "Bushfire" if n % 2 else "Flood",
        "level": "moderate", "level_text": "Advice",
        "title": f"FAKE EMERGENCY #{n}", "status": "Active",
        "size": "10 ha", "updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "coords": [147.36, -35.12], "id": f"FAKE-{n}",
    }]

# ── publisher session (the BBS-side socket) ──────────────────────────────────
class Publisher:
    """A logged-in BBS session the plugin controls over stdin/stdout IPC."""

    def __init__(self, emit):
        self._emit = emit          # callable(json_obj) -> write a plugin->bbs line
        self.connected = False     # we have an open `open` session
        self.logged_in = False
        self._login_sent = False
        self.handshake = False     # login in progress (don't re-open meanwhile)
        self._open_ts = time.time()
        self._busy = threading.Lock()  # serialises posts
        self._confirm = threading.Event()

    def open_session(self) -> None:
        # Guard: never re-open while a login is already in flight.
        # The poll loop calls relogin() on every wake until logged_in, which
        # used to re-emit `open` + `login` a second time mid-handshake —
        # the BBS re-issued the password prompt and the login state machine
        # got out of sync (double login, session flapping).
        if self.handshake:
            return
        self.handshake = True
        self.connected = True
        self._login_sent = False
        self._open_ts = time.time()
        self._emit({"t": "open", "id": PUB_ID})
        time.sleep(0.3)
        self.send_login()

    def send_login(self) -> None:
        """Send `login <user>` exactly once per (re)connect."""
        if not self.connected or self._login_sent:
            return
        self._login_sent = True
        self.send_line(f"login {USER}")

    def relogin(self) -> None:
        if not self.connected:
            self.open_session()
        else:
            # session dropped mid-flight: log out in, then log back in
            self._login_sent = False
            self.send_login()

    # BBS -> plugin (called by stdin reader)
    def on_send(self, text: str) -> None:
        if os.environ.get("SDB_WEATHER_DEBUG") == "1":
            log(f">> RECV {text!r}")
        if self.logged_in:
            # Only treat as post-confirmation while a post is in flight.
            if "Message posted" in text:
                self._confirm.set()
        else:
            # Login handshake: respond to the password prompt, then mark in.
            low = text.lower()
            if "password" in low:
                self._emit({"t": "recv", "id": PUB_ID, "line": PASS or ""})
            elif "welcome" in low:
                self.logged_in = True
                self.handshake = False
                log(f"publisher logged in as {USER}")

    def on_kick(self) -> None:
        log("publisher session dropped; will re-open + re-login")
        self.connected = False
        self.logged_in = False
        self.handshake = False
        self._login_sent = False
        self._confirm.clear()

    # plugin -> BBS
    def send_line(self, line: str) -> None:
        if os.environ.get("SDB_WEATHER_DEBUG") == "1":
            log(f"<< SEND {line!r}")
        self._emit({"t": "recv", "id": PUB_ID, "line": line})

    def go_room(self, room: int) -> None:
        self.send_line(f"C {room}")

    def post(self, room: int, text: str, wait_s: int = 30) -> bool:
        if not self.logged_in:
            log("post skipped: publisher not logged in")
            return False
        with self._busy:
            self._confirm.clear()
            self.go_room(room)
            self.send_line("E")
            for ln in text.split("\n"):
                self.send_line(ln)
            self.send_line(".")
            ok = self._confirm.wait(timeout=wait_s)
            return ok

# ── poll loop ─────────────────────────────────────────────────────────────────
def gather() -> list[dict]:
    if _args.fake:
        return fake_items()
    items: list[dict] = []
    try:
        items += sdb_data.fetch_abc(STATES, ABC_GEO)
    except Exception as e:
        log(f"ABC fetch failed: {e}")
    try:
        items += sdb_data.fetch_bom_warnings(BOM_GEO)
    except Exception as e:
        log(f"BoM fetch failed: {e}")
    return items

def poll_once(pub: Publisher, st: dict) -> int:
    items = gather()
    seen = set()
    posted = 0
    for it in items:
        it["_room"] = item_room(it)
        key = dedup_key(it)
        seen.add(key)
        if key in st["posted"]:
            continue
        if not passes_filter(it):
            continue
        text = f"{render(it)}\n[watch: {it.get('source') or 'emergency'}]"
        if pub.post(it["_room"], text):
            st["posted"][key] = {"title": it.get("title"), "room": it["_room"],
                                 "t": time.time()}
            posted += 1
            log(f"POSTED room {it['_room']}: {it['title']}")
        else:
            log(f"POST FAILED (will retry next poll): {it['title']}")
    # expiry notices
    if _args.notify_expiry:
        for key in [k for k in st["posted"] if k not in seen]:
            meta = st["posted"].pop(key)
            title = meta.get("title", "warning")
            room = meta.get("room", _args.room)
            if pub.post(room, f"CLEARED — {title} no longer active [watch: emergency]"):
                log(f"CLEARED room {room}: {title}")
    save_state(st)
    log(f"poll complete: {len(items)} items, {posted} new posted")
    return posted

def poll_loop(pub: Publisher, st: dict) -> None:
    if _args.once:
        poll_once(pub, st)
        return
    while True:
        # wait until the publisher is usable; re-open if the session dropped
        for _ in range(30):
            if pub.connected and pub.logged_in:
                break
            pub.relogin()
            time.sleep(2)
        try:
            poll_once(pub, st)
        except Exception:
            log("poll error:\n" + traceback.format_exc(limit=3))
        time.sleep(max(30, _args.interval))

# ── IPC plumbing (stdin reader / stdout writer) ──────────────────────────────
def emit(obj: dict) -> None:
    line = json.dumps(obj, ensure_ascii=True)
    sys.stdout.write(line + "\n")
    sys.stdout.flush()

def stdin_reader(pub: Publisher) -> None:
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except Exception:
            log(f"bad stdin line: {raw[:120]}")
            continue
        t = msg.get("t")
        if t == "send":
            pub.on_send(msg.get("text", ""))
        elif t == "kick":
            if msg.get("id") == PUB_ID:
                pub.on_kick()
        elif t == "shutdown":
            log("shutdown received; exiting")
            sys.exit(0)

def main() -> None:
    if not USER or not PASS:
        log("ERROR: SDB_PUB_USER / SDB_PUB_PASS not set")
        # keep running so `plugin logs` shows the error; do not open a session
        emit({"t": "ready", "version": VERSION})
        time.sleep(3600)
        return

    st = load_state()
    pub = Publisher(emit)
    log(f"starting (room={_args.room} fire_room={FIRE_ROOM} states={STATES} "
        f"abc_geo={ABC_GEO} bom_geo={BOM_GEO} interval={_args.interval}s "
        f"severe_only={_args.severe_only} fake={_args.fake})")

    # start the stdin reader first so we can react to BBS sends
    threading.Thread(target=stdin_reader, args=(pub,), daemon=True).start()

    # ready, then open the publisher connection and start the login
    emit({"t": "ready", "version": VERSION})
    pub.open_session()

    # poll loop in its own thread
    threading.Thread(target=poll_loop, args=(pub, st), daemon=True).start()

    # block forever (the BBS sends `shutdown` to stop us)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
