# supply-drop-weather — Build & Config Handover Log

**Author:** Jamos (built with Hermes Agent)
**Host:** controlroom (BBS host), `jamie` user / `supply-drop` service user
**Date:** 2026-10-01 (AEST)
**Version at handover:** v0.3.2 (GitHub tag `v0.3.2`)
**Live at handover:** **v0.3.0** (deployed 20:09 AEST) — only the
`updated`-stamp dedup fix. v0.3.1–v0.3.2 (content-key, state filter, BoM
geohash, geo-2dp) are built, verified, and pushed but NOT yet deployed
(see §8 item 0 + the Deploy section)
**Repo:** https://github.com/vk2jnc/supply-drop-weather

---

## 1. What it is

A **process-transport plugin** for Supply Drop BBS 1.4.2 "Lantea". It runs
*inside* the BBS process (BBS spawns and supervises it, auto-restart on
crash) and posts new Australian emergency & severe-weather warnings into
two BBS rooms as the account `weather`:

| Room | Name | Receives |
|------|------|----------|
| 8 | Emergency | floods, storms, heat, earthquakes, non-fire |
| 9 | Fire Danger | bushfire / grass fire / smoke advice |

(Verify with `supply-drop-bbs room list`. The IDs are set in
`weather.toml` args — update if you ever recreate the rooms.)

Data sources (all keyless, verified reachable from the Exetel residential
egress — BoM's datacenter-IP block does not apply here):

- **ABC Emergency** `https://www.abc.net.au/emergency-web/api/emergencySearch?state=nsw`
  — aggregator covering NSW RFS + state services; carries fire + non-fire.
- **BoM Data API** `https://api.weather.bom.gov.au/locations/<geohash>/forecasts/warnings`
  — official weather warnings. Wagga geohash = `r38zgrx` (the 4-char `r1r0`
  region code is NOT valid for the warnings endpoint — it 400s).

---

## 2. Architecture

```
BBS process (supply-drop-bbs, systemd user service)
  └─ process-transport host (bbs-process-transport)
       └─ spawns: supply-drop-weather.py  (this plugin)
            ├─ stdin/stdout JSON lines  (IPC: send/kick/shutdown)
            ├─ sdb_data.py              (ABC + BoM fetchers, normalizers)
            ├─ telnet session to 127.0.0.1:2323  (pseudo-user login)
            │    → `C #Fire Danger` / `C #Emergency`
            │    → compose: <ctrl-E> <title> ... <ctrl-C>
            ├─ state: /var/lib/supply-drop-bbs/weather-plugin-state.json
            └─ dedup: content-based key, versioned (v3)
```

**Why process-transport** (not a standalone script or telnet client):
- No second service to babysit — BBS owns lifecycle, crash-restart, logging
  (shows in `journalctl -u supply-drop-bbs` and `plugin list`).
- The BBS telnet bridge (`:2323`) is *already* running (mesh transport
  side-effect); the plugin just opens a session like any client.
- Credentials via systemd `EnvironmentFile` (the plugin config has no
  `env` field — a known BBS limitation).

**Why it logs in as a real account** (`weather`, sysop): the web admin /
mesh rooms gate posting by room privileges; a dedicated sysop account is
the simplest working path in 1.4.2 (no per-user room ACLs).

---

## 3. Files (repo layout)

| File | Purpose |
|------|---------|
| `supply-drop-weather.py` | The plugin (26 KB): IPC loop, telnet publisher, poll loop, dedup, state |
| `sdb_data.py` | Data layer: ABC `fetch_abc`, BoM `fetch_bom_warnings`, normalizers, `geo_sig`, `filter_by_state` |
| `install_weather_plugin.py` | One-shot root installer (account, rooms, files, systemd drop-in) |
| `spawn_passworded.py` | PTY helper that feeds a password to `user create`'s prompt (used by installer) |
| `clean_weather_duplicates.py` | Post-bug room cleanup: keep one copy per event, delete dupes (dry-run by default) |
| `weather.toml` | `plugins.d` drop-in template (`[[plugins.process]]` shape — mandatory) |
| `README.md` | Public README |

Installed locations on the BBS host:
```
/opt/sdb-weather/supply-drop-weather.py
/opt/sdb-weather/sdb_data.py
/etc/supply-drop-bbs/plugins.d/weather.toml
/etc/supply-drop-weather.env          (0640 root:supply-drop, SDB_PUB_USER/PASS)
/etc/systemd/system/supply-drop-bbs.service.d/weather.env.conf
```

---

## 4. How the dedup works (the part that took a whole day)

Key lessons, in the order they bit:

1. **v0.2.0 bug — `updated` in the key.** ABC re-stamps every card's
   `updated` on re-render → same fires looked "new" every poll → ~60
   duplicates per 5 min. **v0.3.0** (deployed 20:09 AEST, LIVE at
   handover) changed the key to `(source, id)`. This is what stopped the
   57–176/poll storm.
2. **v0.3.1 — content key.** ABC *also* re-mints the `id` of a card on
   re-render (CAMPBELLS CREEK posted 6× in 25 min). Key became
   `(source, normalized headline, geometry signature)`. Also added the
   `state` filter (QLD/VIC/NT leak) and the BoM geohash fix. **NOT yet
   deployed at handover.**
3. **v0.3.2 — geometry precision.** ABC's per-render coordinate jitter
   (3rd decimal) still flipped the key on some polls. Point signatures
   now round to 2 dp (~1 km). **NOT yet deployed at handover.**
4. **BoM re-issue policy:** BoM keys include the issue timestamp, so a
   *genuinely updated* warning (e.g. escalated to Severe) posts again.

`dedup_key` (v0.3.2, current in repo):
```python
(src=source[:20]) | (bom ? issue-time : geo_sig(geometry)) | normalized_title
```

**State file** `weather-plugin-state.json`:
```json
{"v": 4, "posted": {"<dedup-key>": {"title": "...", "room": 8, "t": 1790800000.0}}}
```
`_STATE_VERSION` was introduced in v0.3.1 (=3) and bumped to **4** in
v0.3.2. The live v0.3.0 state file has **no `v` field**, so the first
deploy of v0.3.1+ detects the mismatch, discards it, and re-seeds once
(one clean post of all currently-active items, then `0 new posted`).
That re-seed is the expected one-time cost of deploying the fix.

---

## 5. Bugs found & fixed during build (full list)

| # | Bug | Symptom | Fix |
|---|-----|---------|-----|
| 1 | Double-login at startup (poll loop re-sent `login` during handshake) | Flaky first post | `handshake` guard in `open_session`/`on_send` |
| 2 | `plugins.d/*.toml` bare top-level keys | Plugin silently not loaded | `[[plugins.process]]` table-array shape |
| 3 | `updated` in dedup key | 57–176 re-posts/poll | Content-based key |
| 4 | ABC `state=nsw` intermittently ignored (load balancer serves national feed) | QLD/VIC/NT fires posted into NSW BBS (179-item polls, `AURUKUN`, `SA-VIC Border`) | `filter_by_state()` on `cardBody.source` + coordinate fallback |
| 5 | ABC re-mints card `id` on re-render | Same fire posted 6× in 25 min | Key on headline + geometry, not id |
| 6 | BoM geohash `r1r0` invalid for warnings endpoint | `BoM fetch failed: 400` every poll since 20:15 AEST | `r38zgrx` (real Wagga location geohash); invalid geohashes now return `[]` instead of killing the poll |
| 7 | Geometry jitter 3rd decimal flipped keys | 4–5 "new" per poll | 2 dp rounding |
| 8 | Test-harness false alarm: read-only SQLite connection didn't see un-checkpointed WAL | "Posts not persisting" panic | `PRAGMA wal_checkpoint(TRUNCATE)` before reads; BBS was fine all along |
| 9 | PTY helper returned 124 on child exit (Linux PTY EIO quirk) | First `user create` looked like a timeout | Catch EIO, reap child, return real exit code |
| 10 | Web admin shows message times in UTC (`slice(0,16)` bug) | 02:53 instead of 12:53 AEST | Upstream frontend bug, documented — no data loss, display only |

---

## 6. Configuration

### `weather.toml` (installed at `/etc/supply-drop-bbs/plugins.d/`)
```toml
[[plugins.process]]
name    = "weather"
command = "/opt/sdb-weather/supply-drop-weather.py"
args    = [
  "--room", "8",            # Emergency room id
  "--fire-room", "9",       # Fire Danger room id
  "--states", "nsw",
  "--bom-geohashes", "r38zgrx",   # NOT r1r0 — see bug #6
  "--interval", "300",
]
restart = "always"
```

### `/etc/supply-drop-weather.env` (0640 root:supply-drop)
```
SDB_PUB_USER=weather
SDB_PUB_PASS=<strong random — openssl rand -base64 18>
```
Wired via systemd drop-in `/etc/systemd/system/supply-drop-bbs.service.d/weather.env.conf`:
```
[Service]
EnvironmentFile=-/etc/supply-drop-weather.env
```
(`-` prefix so a missing file doesn't stop the BBS.)

### Poll interval
Default 300 s (5 min) in the plugin. To change, add `--interval 120` to
the `args` array in `weather.toml` and `systemctl restart supply-drop-bbs`.

### Coverage overrides (all optional)
| Arg | Default | Notes |
|-----|---------|-------|
| `--states` | `nsw` | comma-sep, e.g. `nsw,act` |
| `--bom-geohashes` | `r38zgrx` | comma-sep; must be valid 7-char location geohashes |
| `--interval` | `300` | seconds |
| `--room` / `--fire-room` | `8` / `9` | room IDs (verify: `room list`) |
| `--once` | off | single poll then exit (debug) |

---

## 7. Operations runbook

### Deploy a new version
```sh
# 1. copy the two code files
sudo cp ~/workspace/supply-drop-weather/sdb_data.py /opt/sdb-weather/
sudo cp ~/workspace/supply-drop-weather/supply-drop-weather.py /opt/sdb-weather/
# 2. FIX the BoM geohash in the live toml (r1r0 is invalid → 400 every poll)
sudo sed -i 's/"r1r0"/"r38zgrx"/' /etc/supply-drop-bbs/plugins.d/weather.toml
# 3. restart + watch
sudo systemctl restart supply-drop-bbs
journalctl -u supply-drop-bbs -f | grep weather
```
Steps 1+2 are both required: the code's own default is also `r38zgrx`, but
the live toml explicitly overrides it to `r1r0`, so without step 2 the BoM
leg keeps 400-ing.

If the dedup key format changed (it has — v0.3.0→v0.3.2): the new code
bumps `_STATE_VERSION` (now 4). The first poll detects the live state
file is a different version, discards it, and re-seeds once — expected:
one clean post of all currently-active items (~55), then `0 new posted`.

### Verify it's working
```sh
journalctl -u supply-drop-bbs --since "10 min ago" | grep 'poll complete'
# healthy:   poll complete: ~56 items, 0 new posted
# re-seed:   state vN reset: 55 items now considered known   (once)
# broken:    BoM fetch failed / no polls / `plugin` status error
supply-drop-bbs plugin list
```

### Rotate the publisher password
```sh
# (needs the BBS briefly down — it rewrites the users table)
sudo systemctl stop supply-drop-bbs
sudo supply-drop-bbs user set-password weather     # run as supply-drop user via sudo -u
# update /etc/supply-drop-weather.env
sudo systemctl start supply-drop-bbs
```

### Clean the rooms of duplicates (post-bug)
```sh
# DRY RUN first (required):
sudo SDB_ADMIN_USER=admin SDB_ADMIN_PASSWORD=*** \
     python3 ~/workspace/supply-drop-weather/clean_weather_duplicates.py
# then, if the numbers look right:
sudo SDB_ADMIN_USER=admin SDB_ADMIN_PASSWORD=*** \
     python3 ~/workspace/supply-drop-weather/clean_weather_duplicates.py --apply
```
Only deletes `sender == "weather"` rows in rooms named Emergency /
Fire Danger; keeps the earliest row per normalized-content group.

### Reset the dedup state (force re-post everything active)
```sh
sudo systemctl stop supply-drop-bbs
sudo rm /var/lib/supply-drop-bbs/weather-plugin-state.json
sudo systemctl start supply-drop-bbs
```

### Uninstall
```sh
sudo systemctl stop supply-drop-bbs
sudo rm /etc/supply-drop-bbs/plugins.d/weather.toml \
       /etc/systemd/system/supply-drop-bbs.service.d/weather.env.conf \
       /etc/supply-drop-weather.env
sudo rm -r /opt/sdb-weather
sudo systemctl daemon-reload
sudo systemctl start supply-drop-bbs
# optional: delete the account + rooms
sudo supply-drop-bbs user delete weather
```

---

## 8. Known limitations / open items

0. **Live is v0.3.0, not the latest.** At handover the BBS is running
   v0.3.0 (only the `updated`-stamp dedup fix — that's what stopped the
   57–176/poll storm). It does NOT yet have: the content-key (ABC
   re-minted-id → 0–5 "new"/poll churn), the state filter (QLD/VIC/NT
   leak), or the BoM geohash fix (`BoM fetch failed: 400` still fires
   every poll; live toml still says `r1r0`). Deploy v0.3.2 (below) to
   pick all of these up. Expected on deploy: one state re-seed (≈55
   posts), then `0 new posted`, and the BoM 400 gone.
1. **Web admin UTC display** — messages show `02:53` instead of `12:53`
   AEST (upstream `MessagesPage.vue` does `timestamp.slice(0,16)` instead
   of `fmtLocal()`). Data is correct; display bug only. Upstream PR
   candidate.
2. **No true push-to-all-users.** BBS 1.4.2 has a `System` room and a
   notification bus, but the mesh transport doesn't fan out `MessagePosted`
   to online sessions (catch-all `_ => {}`). Users see posts via the web
   admin / telnet clients. Upstream patch candidate: arm `MessagePosted`
   → `notify()` on active sessions.
3. **QLD/VIC/NT filter is heuristic** — by `cardBody.source` prefix +
   coordinate fallback. A genuinely new service not in the prefix table
   falls to coordinates (kept only if watching multiple states). Extend
   `_PREFIX_TO_STATE` as needed.
4. **ABC feed variance** — the item count legitimately oscillates
   (56–179) depending on which load-balanced response you hit; the state
   filter + content key make this a non-issue for posting, but `poll
   complete` numbers will look noisy.
5. **Publisher account is sysop** — a dedicated machine account with its
   own password; acceptable, but if upstream adds per-user room ACLs it
   should be downgraded.
6. **No RFS direct feed** — RFS major-incidents GeoJSON
   (`https://www.rfs.nsw.gov.au/media/13803605/major-incidents.geojson`)
   was verified live (48 incidents) but ABC's feed already carries RFS;
   adding it would only add a ~seconds lead time. Parked.

---

## 9. Build timeline (2026-10-01)

| Time (AEST) | Event |
|-------------|-------|
| ~14:00 | Researched BBS plugin API + web admin capabilities (no push-to-all; System room + DMs) |
| ~15:00 | Verified ABC Emergency API live from host (no key); verified BoM Data API + RFS GeoJSON |
| ~16:00 | Wrote `sdb_data.py` (ABC+BoM fetchers/normalizers) and `supply-drop-weather.py` v0.1 |
| ~16:30 | First e2e on scratch BBS (`/tmp/sdb-test`, isolated): login → room change → post OK. "Missing posts" = WAL checkpoint artifact (bug #8) |
| ~17:00 | v0.2.0: dedup key with `updated` → duplicate storm discovered in journal |
| ~18:00 | Published GitHub repo `vk2jnc/supply-drop-weather` (main, Apache-2.0) |
| ~18:30 | Installer written + PTY password helper; `user create` race (bug #9) fixed |
| ~19:00 | Live install: `weather` account (sysop), rooms Emergency(8) + Fire Danger(9), plugin v0.3.0 deployed, first post 12:53 AEST (55 warnings) |
| 13:18–21:00 | Duplicate storm observed (bugs #3 #4 #5 #6) — journal forensics |
| ~20:09 | **v0.3.0 deployed** (`updated`-stamp fix) — 57–176/poll storm stops; residual 0–5/poll churn + BoM 400 remain |
| ~21:45 | v0.3.1 written (content key + state filter + BoM geohash) — built + verified, **not deployed** |
| ~23:00 | v0.3.2 written (geo 2 dp, state v4) — built + verified stable (0 new/0 gone), **not deployed** |
| 23:15 | Cleanup script + this handover written |

**Handover state:** live = v0.3.0. To go to v0.3.2: run the Deploy steps
in §7 (copy 2 files + fix toml geohash + restart), then the room cleanup
in §7.

---

## 10. Credentials & secrets inventory

| Secret | Where | Who needs it |
|--------|-------|--------------|
| `weather` account password | `/etc/supply-drop-weather.env` (0640) | plugin only |
| admin web password | (not stored anywhere on the host) | humans / cleanup script via env at runtime |
| No API keys | — | ABC + BoM are keyless from AU egress |

**Secrets hygiene:** nothing in the repo; nothing in journal (BBS logs
only `publisher logged in as weather`). If the env file is ever committed
or leaked, rotate the `weather` password (section 7).
