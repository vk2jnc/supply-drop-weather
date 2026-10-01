#!/usr/bin/env python3
"""install_weather_plugin.py — install the weather/emergency publisher plugin.

Run ONCE, as root (sudo), on the BBS host:

    sudo python3 install_weather_plugin.py '<publisher-password>'

Optional env overrides:
    SDB_PUB_USER        publisher BBS account name   (default: weather)
    SDB_EMERGENCY_ROOM  name of the emergency room   (default: Emergency)
    SDB_FIRE_ROOM       name of the fire room        (default: Fire Danger)
    SDB_STATES        ABC states, comma-sep        (default: nsw)
    SDB_BOM_GEO       BoM geohash prefixes         (default: r38zgrx  = Wagga)
    SDB_INTERVAL      poll interval seconds        (default: 300)

Steps (idempotent where possible):
    1. stop supply-drop-bbs
    2. create publisher BBS account if missing (PTY password)
    3. create Emergency / Fire Danger rooms if missing; read their ids
    4. install plugin + data module to /opt/sdb-weather (supply-drop:supply-drop)
    5. write /etc/supply-drop-weather.env (root:supply-drop, 0640)
    6. write /etc/supply-drop-bbs/plugins.d/weather.toml (room ids resolved)
    7. systemd drop-in: EnvironmentFile for the plugin creds
    8. daemon-reload + start
    9. verify: journal shows plugin login, rooms present
"""

import os
import shutil
import sqlite3
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SERVICE = "supply-drop-bbs"
DATA_DIR = "/var/lib/supply-drop-bbs"
PLUGIN_DIR = "/opt/sdb-weather"
ENV_FILE = "/etc/supply-drop-weather.env"
DROPPIN = "/etc/supply-drop-bbs/plugins.d/weather.toml"
UNIT_D = "/etc/systemd/system/supply-drop-bbs.service.d/weather.conf"

PUB_USER = os.environ.get("SDB_PUB_USER", "weather")
WEATHER_ROOM = os.environ.get("SDB_EMERGENCY_ROOM", os.environ.get("SDB_WEATHER_ROOM", "Emergency"))
FIRE_ROOM = os.environ.get("SDB_FIRE_ROOM", "Fire Danger")
STATES = os.environ.get("SDB_STATES", "nsw")
BOM_GEO = os.environ.get("SDB_BOM_GEO", "r38zgrx")
INTERVAL = os.environ.get("SDB_INTERVAL", "300")


def step(msg):
    print(f"\n== {msg} ==", flush=True)


def sh(args, **kw):
    print("  $", " ".join(args), flush=True)
    return subprocess.run(args, **kw)


def main() -> int:
    if os.geteuid() != 0:
        print("run as root:  sudo python3 install_weather_plugin.py '<pub-pass>'", file=sys.stderr)
        return 2
    if len(sys.argv) < 2 or not sys.argv[1]:
        print("usage: sudo python3 install_weather_plugin.py '<publisher-password>'", file=sys.stderr)
        return 2
    pub_pass = sys.argv[1]

    step("1. stop BBS")
    sh(["systemctl", "stop", SERVICE], check=False)

    step("2. publisher account")
    listed = sh(["supply-drop-bbs", "user", "list"], capture_output=True, text=True,
                check=False).stdout
    if PUB_USER in listed:
        print(f"  user '{PUB_USER}' exists — leaving as-is")
    else:
        r = subprocess.run(
            [sys.executable, os.path.join(HERE, "spawn_passworded.py"), pub_pass,
             "supply-drop-bbs", "user", "create", PUB_USER])
        if r.returncode != 0:
            print(f"  FAILED to create user (exit {r.returncode})", file=sys.stderr)
            sh(["systemctl", "start", SERVICE], check=False)
            return 1
        print(f"  created user '{PUB_USER}'")

    step("3. rooms")
    con = sqlite3.connect(f"file:{DATA_DIR}/bbs.sqlite?mode=ro", uri=True)
    names = [r[0] for r in con.execute("SELECT name FROM rooms")]
    con.close()
    if WEATHER_ROOM not in names:
        sh(["supply-drop-bbs", "room", "create", WEATHER_ROOM,
            "--description", "Emergency & severe weather warnings (auto-posted)"])
    if FIRE_ROOM not in names:
        sh(["supply-drop-bbs", "room", "create", FIRE_ROOM,
            "--description", "Fire danger / fire emergency warnings (auto)"])
    con = sqlite3.connect(f"file:{DATA_DIR}/bbs.sqlite?mode=ro", uri=True)
    room_ids = {n: i for i, n in con.execute("SELECT id,name FROM rooms")}
    con.close()
    weather_id = room_ids[WEATHER_ROOM]
    fire_id = room_ids[FIRE_ROOM]
    print(f"  rooms: {WEATHER_ROOM}={weather_id}  {FIRE_ROOM}={fire_id}")

    step("4. install plugin files -> " + PLUGIN_DIR)
    os.makedirs(PLUGIN_DIR, exist_ok=True)
    for f in ("supply-drop-weather.py", "sdb_data.py"):
        shutil.copy2(os.path.join(HERE, f), os.path.join(PLUGIN_DIR, f))
    for f in ("supply-drop-weather.py", "sdb_data.py"):
        os.chmod(os.path.join(PLUGIN_DIR, f), 0o755)
    shutil.chown(PLUGIN_DIR, "supply-drop", "supply-drop")
    for f in ("supply-drop-weather.py", "sdb_data.py"):
        shutil.chown(os.path.join(PLUGIN_DIR, f), "supply-drop", "supply-drop")

    step("5. env file " + ENV_FILE)
    with open(ENV_FILE, "w") as fh:
        fh.write(f"SDB_PUB_USER={PUB_USER}\nSDB_PUB_PASS={pub_pass}\n"
                 f"SDB_WEATHER_STATE={DATA_DIR}/weather-plugin-state.json\n")
    os.chmod(ENV_FILE, 0o640)
    shutil.chown(ENV_FILE, "root", "supply-drop")

    step("6. plugin drop-in " + DROPPIN)
    os.makedirs(os.path.dirname(DROPPIN), exist_ok=True)
    with open(DROPPIN, "w") as fh:
        fh.write(f'''# Weather/emergency publisher — installed by install_weather_plugin.py
[[plugins.process]]
name = "weather"
command = "{PLUGIN_DIR}/supply-drop-weather.py"
args = [
  "--room", "{weather_id}",
  "--fire-room", "{fire_id}",
  "--states", "{STATES}",
  "--bom-geohashes", "{BOM_GEO}",
  "--interval", "{INTERVAL}",
]
''')

    step("7. systemd drop-in " + UNIT_D)
    os.makedirs(os.path.dirname(UNIT_D), exist_ok=True)
    with open(UNIT_D, "w") as fh:
        fh.write("[Service]\n"
                 f"EnvironmentFile={ENV_FILE}\n")

    step("8. daemon-reload + start")
    sh(["systemctl", "daemon-reload"], check=False)
    sh(["systemctl", "start", SERVICE], check=False)
    time.sleep(8)

    step("9. verify")
    ok = sh(["systemctl", "is-active", SERVICE], capture_output=True, text=True)
    print(f"  service: {ok.stdout.strip()}")
    j = sh(["journalctl", "-u", SERVICE, "--since", "30 seconds ago", "-o", "cat",
            "--no-pager"], capture_output=True, text=True, check=False).stdout
    weather_lines = [l for l in j.splitlines() if "plugin=weather" in l or "weather" in l.lower()]
    for l in weather_lines[-12:]:
        print("   |", l[:150])
    if not weather_lines:
        print("  WARNING: no weather-plugin log lines yet — check `journalctl -u "
              "supply-drop-bbs` manually")
    else:
        print("  plugin is logging (see lines above)")
    print("\nDone. Verify a post appears by running the BBS or checking the "
          f"'{WEATHER_ROOM}' room; dedup state lives in {DATA_DIR}/weather-plugin-state.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
