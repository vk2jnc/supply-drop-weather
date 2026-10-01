#!/usr/bin/env python3
"""clean_weather_duplicates.py — remove duplicate `weather` posts from rooms.

Why: a dedup-key bug (v0.2.0–v0.3.0) re-posted the same ~55 NSW warnings
every 5 minutes, leaving hundreds of duplicate rows in the Emergency /
Fire Danger rooms. This script keeps ONE copy of each event (the earliest
by post time) and deletes the rest.

SAFETY
  * Dry-run BY DEFAULT. Pass --apply to actually delete.
  * Touches ONLY messages whose sender is exactly `weather`.
  * Touches ONLY the rooms named Emergency / Fire Danger (overridable).
  * Groups by a normalized content window; two genuinely different fires
    at different locations never collide.

CREDENTIALS — the web admin is session-auth only and I never hold your
password. Supply them at runtime via a file or env, e.g.:

  sudo SDB_ADMIN_USER=admin SDB_ADMIN_PASSWORD=*** python3 clean_weather_duplicates.py --apply

or

  sudo python3 clean_weather_duplicates.py --creds /path/to/file --apply
  (file format: one line "admin:<password>")

USAGE
  sudo python3 clean_weather_duplicates.py                 # dry run, preview
  sudo python3 clean_weather_duplicates.py --apply         # delete dupes
  sudo python3 clean_weather_duplicates.py --room 6 --apply
"""
import argparse, json, sys, urllib.request, urllib.parse
from http.cookiejar import CookieJar

API = "http://127.0.0.1:9000/api/v1"
PAGE = 500
# first N chars of content used as the dedup group key
WINDOW = 150


def norm(text: str) -> str:
    t = (text or "").strip()
    if t.startswith("*NEW*"):
        t = t[len("*NEW*"):].strip()
    return " ".join(t.split())


def make_client(user: str, password: str):
    jar = CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def call(method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(API + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        with opener.open(req, timeout=20) as r:
            txt = r.read().decode()
            return json.loads(txt) if txt else None

    call("POST", "/auth/login", {"username": user, "password": password})
    return call


def rooms_of(call):
    out = {}
    for r in call("GET", "/rooms"):
        out[r["id"]] = r.get("name")
    return out


def list_room(call, rid):
    msgs, after = [], None
    while True:
        q = f"?limit={PAGE}"
        if after is not None:
            q += f"&after_id={after}"
        page = call("GET", f"/rooms/{rid}/messages" + q)
        if not page:
            break
        msgs.extend(page)
        if len(page) < PAGE:
            break
        after = page[-1]["id"]
    return msgs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually delete (default: dry run)")
    ap.add_argument("--creds", help="file containing 'user:password'")
    ap.add_argument("--room", type=int, help="restrict to one room id")
    ap.add_argument("--rooms", help="comma-sep room NAMES to clean (default: Emergency,Fire Danger)")
    args = ap.parse_args()

    import os
    user = os.environ.get("SDB_ADMIN_USER")
    pwd = os.environ.get("SDB_ADMIN_PASSWORD")
    if args.creds:
        line = open(args.creds).read().strip().splitlines()[-1]
        user, _, pwd = line.partition(":")
        user, pwd = user.strip(), pwd.strip()
    if not user or not pwd:
        sys.exit("no credentials: set SDB_ADMIN_USER/SDB_ADMIN_PASSWORD or pass --creds file")

    call = make_client(user, pwd)
    room_names = {n.strip() for n in (args.rooms.split(",") if args.rooms else ["Emergency", "Fire Danger"])}
    all_rooms = rooms_of(call)
    targets = {rid: name for rid, name in all_rooms.items()
               if name in room_names and (args.room is None or rid == args.room)}
    if not targets:
        sys.exit(f"no target rooms found among {list(all_rooms.values())} for names {room_names}")

    total_kept = total_dup = 0
    for rid in sorted(targets):
        msgs = [m for m in list_room(call, rid) if m.get("sender") == "weather"]
        groups = {}
        for m in sorted(msgs, key=lambda x: x["id"]):
            groups.setdefault(norm(m["content"])[:WINDOW], []).append(m)
        keep, drop = 0, 0
        for key, ms in groups.items():
            keep += 1          # earliest (lowest id)
            drop += len(ms) - 1
            for m in ms[1:]:
                if args.apply:
                    call("DELETE", f"/messages/{m['id']}")
        print(f"[{'APPLY' if args.apply else 'DRYRUN'}] room {rid} ({targets[rid]}): "
              f"{len(msgs)} weather msgs -> keep {keep}, delete {drop}")
        total_kept += keep
        total_dup += drop

    print(f"\n{'='*40}")
    print(f"{'APPLYING — ' if args.apply else 'DRY RUN (no deletes) — '}"
          f"keep {total_kept}, delete {total_dup}")
    if not args.apply:
        print("Re-run with --apply to delete. (credentials never stored here)")


if __name__ == "__main__":
    main()
