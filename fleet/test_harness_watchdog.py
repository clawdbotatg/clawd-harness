#!/usr/bin/env python3
"""test_harness_watchdog.py — the worker plain-starts a harness that stays down.

2026-09-05: clawd-heart's harness exited 0 for its graceful self-restart and
launchd never respawned it; the box sat headless 40 min. The worker's stats
loop is the one thing on the box that sees the harness gone, so it starts the
service after HARNESS_KICK_AFTER of continuous failure. Guards:
  1. no start before the threshold, one start after it, rate-limited after;
  2. a successful connect resets the clock;
  3. a relay node (no harness) and FLEET_HARNESS_KICK=0 never start anything;
  4. the command is a plain START on both platforms — never kickstart -k,
     never restart/stop/kill (the 08-09 mid-keystroke incident).
Exits non-zero on any failure.
"""
import sys
import worker as workermod

FAILED = []


def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        FAILED.append(msg)


class R:
    returncode = 0
    stdout = stderr = ""


def make(kind="machine"):
    w = workermod.Worker("ws://127.0.0.1:9", "t", "m", "host", "ws://127.0.0.1:9", "t", kind=kind)
    calls = []
    run = lambda cmd, **kw: (calls.append(cmd), R())[1]
    return w, calls, run


workermod.HARNESS_KICK = True
workermod.HARNESS_KICK_AFTER = 90.0
workermod.HARNESS_KICK_EVERY = 300.0

# 1. threshold + rate limit
w, calls, run = make()
check(w._harness_watchdog("refused", now=1000, run=run) is False and not calls, "first failure only starts the clock")
check(w._harness_watchdog("refused", now=1050, run=run) is False and not calls, "50s down: no start yet")
check(w._harness_watchdog("refused", now=1100, run=run) is True and len(calls) == 1, "100s down: one start issued")
check(w._harness_watchdog("refused", now=1200, run=run) is False and len(calls) == 1, "still down 100s later: rate-limited")
check(w._harness_watchdog("refused", now=1450, run=run) is True and len(calls) == 2, "300s after the start, still down: start again")

# 2. reset on connect
w._harness_watchdog_ok()
check(w._harness_watchdog("refused", now=2000, run=run) is False and len(calls) == 2, "after a good connect the clock restarts")
check(w._harness_watchdog("refused", now=2080, run=run) is False and len(calls) == 2, "80s into the new outage: nothing")

# 3. never on a relay node / when opted out
w2, calls2, run2 = make(kind="relay")
for t in (0, 100, 1000):
    w2._harness_watchdog("refused", now=t, run=run2)
check(not calls2, "relay node never starts a harness")
workermod.HARNESS_KICK = False
w3, calls3, run3 = make()
for t in (0, 100, 1000):
    w3._harness_watchdog("refused", now=t, run=run3)
check(not calls3, "FLEET_HARNESS_KICK=0 never starts a harness")
workermod.HARNESS_KICK = True

# 4. plain start on both platforms
mac = workermod.harness_start_cmd("", platform="darwin")
lin = workermod.harness_start_cmd("", platform="linux")
check(mac[:2] == ["launchctl", "kickstart"] and "-k" not in mac and mac[-1].endswith("/com.clawd.harness"), f"darwin: plain kickstart {mac}")
check(lin == ["systemctl", "--user", "start", "clawd-harness.service"], f"linux: systemctl start {lin}")
check(workermod.harness_start_cmd("", platform="win32") is None, "unknown platform: no command")
custom = workermod.harness_start_cmd("com.example.h", platform="darwin")
check(custom[-1].endswith("/com.example.h"), "FLEET_HARNESS_SERVICE overrides the label")
for cmd in (mac, lin, custom):
    check(not any(x in ("-k", "restart", "stop", "kill", "bootout") for x in cmd), f"never a kill verb: {cmd}")

print()
if FAILED:
    print(f"{len(FAILED)} FAILED"); sys.exit(1)
print("all passed")
