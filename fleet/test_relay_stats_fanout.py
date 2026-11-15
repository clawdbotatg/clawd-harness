#!/usr/bin/env python3
"""Integration test: a worker's stats report fans out only when it would change
what a browser paints.

Every worker reports every ~10 s, and each report used to rebuild the roster,
rewrite the roster file and send it to every browser — though the only thing
that moved was usually `build.chan` (which no browser reads) or a load sample
that prints the same. Now (audit 2026-11-09, F5):

  1. a report that would paint the same card sends nothing to browsers;
  2. a report that changes a count or a printed load figure still does;
  3. a report that changes only what tools/shipcheck.py reads (the build, the
     channel deadlines) reaches the roster FILE at once, without a fan-out;
  4. nothing is lost: the heartbeat roster (PING_EVERY) carries the rest.

The whole run stays inside the relay's first PING_EVERY (20 s), so a `machines`
frame seen here can only have come from a join or a stats report.

Run: python3 fleet/test_relay_stats_fanout.py
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import quote

import fleet_ws

HERE = Path(__file__).resolve().parent
PORT = "8815"
RELAY = f"ws://127.0.0.1:{PORT}"
TOKEN, WTOK = "fanout-test-token", "fanout-test-worker"
TMP = Path(tempfile.mkdtemp(prefix="clawd-fleet-fanout-"))
ROSTER = TMP / "roster.json"

ENV = {
    **os.environ,
    "FLEET_PORT": PORT,
    "FLEET_BIND": "127.0.0.1",
    "FLEET_MOBILE_TOKEN": TOKEN,
    "FLEET_WORKER_TOKEN": WTOK,
    "FLEET_WORKER_ALLOW": "",
    "FLEET_REQUIRE_PASSKEY": "0",
    # never the box's real files
    "FLEET_ROSTER_FILE": str(ROSTER),
    "FLEET_PREFS_FILE": str(TMP / "prefs.json"),
    "FLEET_SESSIONS_FILE": str(TMP / "sessions.json"),
    "FLEET_PUSH_SUBS_FILE": str(TMP / "push.json"),
    "FLEET_PASSKEY_FILE": str(TMP / "passkeys.json"),
    "FLEET_TODOS_FILE": str(TMP / "todos.json"),
    "FLEET_TODO_BRIDGE_FILE": str(TMP / "todo-bridge.token"),
}

GB = 1073741824


def dial(url):
    sock, rfile, wfile = fleet_ws.client_connect(url)
    lock, inbox = threading.Lock(), []

    def reader():
        while True:
            try:
                msg = fleet_ws.ws_read_message(rfile)
            except Exception:
                return
            if msg is None:
                return
            kind, data = msg
            if kind == "ping":
                fleet_ws.ws_send(wfile, lock, data, opcode=0xA, mask=True)
                continue
            if kind in ("pong", "close"):
                continue
            try:
                inbox.append(json.loads(data.decode()))
            except Exception:
                pass

    threading.Thread(target=reader, daemon=True).start()

    def send(obj):
        fleet_ws.ws_send(wfile, lock, json.dumps(obj), opcode=0x1, mask=True)

    return sock, send, inbox


def wait_for(pred, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        got = pred()
        if got:
            return got
        time.sleep(0.05)
    return None


def stats(projects=2, sessions=3, active=1, cpu=12.1, load1=0.97, ram_used=8.02 * GB,
          code="aaaaaaaaaaaa", chan_next=1900000000, chan_last=1900000500):
    return {"type": "stats", "projects": projects, "sessions": sessions, "active": active,
            "sys": {"cpu": {"pct": cpu, "load1": load1, "cores": 8},
                    "ram": {"pct": 50.2, "used": int(ram_used), "total": 16 * GB},
                    "disk": {"pct": 61.0, "used": 300 * GB, "total": 500 * GB, "free": 200 * GB}},
            "build": {"code": code, "ttl": 604800, "idle": 600, "started": 1800000000,
                      "chan": {"n": 1, "next": chan_next, "last": chan_last}},
            "harnessBuild": "bbbbbbbbbbbb"}


def main():
    proc = subprocess.Popen([sys.executable, "relay.py"], env=ENV, cwd=str(HERE),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.2)
    born = time.time()
    checks, socks = [], []

    def check(name, cond):
        checks.append((name, bool(cond)))
        print(f"  {'✓' if cond else '✗ FAIL'} {name}")

    def on_disk():
        try:
            return json.loads(ROSTER.read_text())["machines"][0]["stats"]
        except Exception:
            return None

    try:
        ms, _msend, inbox = dial(f"{RELAY}/ws?role=mobile&t={quote(TOKEN)}")
        socks.append(ms)
        ws, wsend, _ = dial(f"{RELAY}/ws?role=worker&machine=fanbox&host=fan.local&t={quote(WTOK)}")
        socks.append(ws)

        def rosters():
            return [f for f in inbox if f.get("type") == "machines"]

        def quiet(send_frame, secs=1.5):
            """Send a report; True iff no roster reaches the browser."""
            inbox.clear()
            wsend(send_frame)
            time.sleep(secs)
            return not rosters()

        def loud(send_frame):
            inbox.clear()
            wsend(send_frame)
            got = wait_for(rosters)
            return got[-1]["machines"][0]["stats"] if got else None

        # the first report always goes out
        first = loud(stats())
        check("first report reaches the browser", first and first["sessions"] == 3)
        check("…and the roster file", wait_for(lambda: (on_disk() or {}).get("sessions") == 3))

        # 1. nothing a browser paints changed
        check("identical report → no fan-out", quiet(stats()))
        check("load jitter below what the page prints → no fan-out",
              quiet(stats(cpu=12.3, load1=0.98, ram_used=8.03 * GB)))
        check("only build.chan moved → no fan-out", quiet(stats(chan_next=1900000100, chan_last=1900000900)))

        # 3. …but shipcheck's file has it at once
        check("build.chan change is in the roster file",
              wait_for(lambda: ((on_disk() or {}).get("build") or {}).get("chan", {}).get("next") == 1900000100, 0.5))
        check("new worker code → no fan-out",
              quiet(stats(code="cccccccccccc", chan_next=1900000100, chan_last=1900000900)))
        check("new worker code is in the roster file",
              wait_for(lambda: ((on_disk() or {}).get("build") or {}).get("code") == "cccccccccccc", 0.5))

        # 2. something a browser paints changed
        kw = dict(code="cccccccccccc", chan_next=1900000100, chan_last=1900000900)
        got = loud(stats(active=2, **kw))
        check("a count change fans out", got and got["active"] == 2)
        got = loud(stats(active=2, cpu=47.0, load1=3.76, **kw))
        check("a printed load change fans out", got and got["sys"]["cpu"]["pct"] == 47.0)
        check("…and carries the newest build along", got and got["build"]["code"] == "cccccccccccc")
        got = loud(stats(active=2, cpu=47.0, load1=3.76, ram_used=9.5 * GB, **kw))
        check("a printed RAM change fans out", got and got["sys"]["ram"]["used"] == int(9.5 * GB))
        got = loud({"type": "stats", "projects": 2, "sessions": 3, "active": 2})
        check("losing the load sample fans out", got and "sys" not in got)

        check("(test stayed inside the first heartbeat)", time.time() - born < 17)
    finally:
        for s in socks:
            try:
                s.close()
            except Exception:
                pass
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()

    bad = [n for n, ok in checks if not ok]
    if bad:
        print(f"\nFAILED: {len(bad)} check(s): {bad}")
        sys.exit(1)
    print(f"\nPASSED: {len(checks)} checks — stats fan out only when a browser would repaint")


if __name__ == "__main__":
    main()
