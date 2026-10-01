# supply-drop-weather

Australian emergency & severe-weather publisher for
[Supply Drop BBS](https://github.com/Mesh-America/supply-drop-bbs).

A **process transport plugin**: it runs *inside* the BBS process (spawned
and supervised by the BBS), holds one persistent logged-in "publisher"
session, polls public Australian emergency data sources, and posts new
incidents to BBS rooms. No separate service, no API keys, no BBS source
changes.

## Data sources (all keyless, verified reachable from AU residential egress)

| Source | Covers | Endpoint |
|--------|--------|----------|
| **ABC Emergency** | Aggregator of every AU state/territory service: bushfire, grass fire, flood, storm, wind, earthquake, heat | `abc.net.au/emergency-web/api/emergencySearch?state=nsw` |
| **BOM Data API** | Severe weather warnings: thunderstorm, flood rain, extreme heat, fire danger | `api.weather.bom.gov.au/v1/locations/{geohash}/warnings` |

## How it works

1. BBS spawns `supply-drop-weather.py` as a process transport plugin.
2. The plugin opens a pseudo-session and logs in as the publisher account.
3. A poll loop (default 300s) fetches ABC + BOM, dedupes against a state
   file, and posts each **new** incident as the publisher:
   - fire / fire-danger types -> `Fire Danger` room
   - everything else -> `Emergency` room
4. Dedup key is **content-based** — normalised headline + a signature of
   the card's geometry (ABC cards), so the same fire keeps the same key even
   though ABC re-renders cards and re-mints their `id`s. A warning posts
   exactly once; a failed post retries next cycle; cleared items are pruned.

## Install (one command, run as root on the BBS host)

```sh
sudo python3 install_weather_plugin.py '<publisher-password>'
```

What it does: stops the BBS (~15s) -> creates the `weather` publisher
account (sysop) -> creates `Emergency` + `Fire Danger` rooms (reads their
real IDs) -> installs the plugin to `/opt/sdb-weather/` -> writes
`/etc/supply-drop-weather.env` (0640) -> drops `plugins.d/weather.toml`
with the resolved room IDs -> adds a systemd `EnvironmentFile=` drop-in ->
restarts and verifies from the journal that the plugin logged in.

### Options (env vars in front of the command)

| Var | Default | Meaning |
|-----|---------|---------|
| `SDB_PUB_USER` | `weather` | publisher BBS account name |
| `SDB_EMERGENCY_ROOM` | `Emergency` | main room name |
| `SDB_FIRE_ROOM` | `Fire Danger` | fire room name (set to the same name for a single room) |
| `SDB_STATES` | `nsw` | ABC states, comma list (nsw,vic,act,qld,sa,wa,tas,nt) |
| `SDB_BOM_GEO` | `r38zgrx` | BoM location geohash(es), comma list — must be a valid BoM geohash (Wagga = `r38zgrx`) |
| `SDB_SEVERE_ONLY` | `0` | `1` = only moderate/major/extreme ABC levels |
| `SDB_INTERVAL` | `300` | poll seconds (don't go much under 120) |
| `SDB_FIRE_ROOM_ID`/`SDB_ROOM_ID` | auto | pin room IDs instead of auto-detect |

## Manual install (without the helper)

1. `supply-drop-bbs user create --sysop <user>` (BBS must be stopped)
2. `supply-drop-bbs room create Emergency` / `room create 'Fire Danger'`
3. copy `supply-drop-weather.py` + `sdb_data.py` to a path readable by the
   BBS user (e.g. `/opt/sdb-weather/`)
4. write `/etc/supply-drop-weather.env`:
   `SDB_PUB_USER=...`, `SDB_PUB_PASS=***
5. drop a `[[plugins.process]]` entry in `/etc/supply-drop-bbs/plugins.d/`
   (see the `weather.toml` the installer generates)
6. add a systemd `EnvironmentFile=` drop-in for the BBS service, restart.

## State, rotation, testing

- **Dedup state**: `/var/lib/supply-drop-bbs/weather-plugin-state.json`
  (override with `SDB_WEATHER_STATE`). Wipe it to re-post everything active.
- **Rotate publisher password**: `supply-drop-bbs user set-password weather`
  (BBS stopped), update `SDB_PUB_PASS=*** in `/etc/supply-drop-weather.env`,
  `systemctl restart supply-drop-bbs`.
- **Dry run / test**: the plugin supports `--once` (single poll) and
  `--fake` (synthetic data) for verification against a scratch BBS.
- **Poll cadence**: ABC/BOM are both low-churn; 300s is plenty. 60s is the
  practical floor to be polite to the public endpoints.

## Testing performed

Verified against a throwaway BBS (isolated data dir, `--fake` data, 2s poll):
publisher login, `C <room>` switching, multi-line post composition, BBS
`Message posted.` confirmation, room routing (bushfire -> fire room, flood ->
emergency room), and dedup (each synthetic event posted exactly once). Also
verified the live ABC and BOM endpoints return real NSW incidents.

## License

Apache-2.0 (matching Supply Drop BBS). See [LICENSE](LICENSE).
