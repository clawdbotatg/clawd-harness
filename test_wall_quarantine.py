#!/usr/bin/env python3
"""A real CLI wall overrides a stale-low usage reading and survives restart.

2026-09-03 incident: ef painted "You've hit your session limit" while the
usage endpoint's last good snapshot still said 47%. `rescue_limit_wall` asked
the ordinary router for its best pool; reset-soonest selected ef again, so the
rescue stayed put. A harness restart then forgot the terminal evidence and
every new session spawned on ef too, despite sub4 being fresh at 59%.

This test recreates those numbers without constructing a real SessionManager
(which would resume live sessions). No processes, credentials, or registry
files are touched.
"""
import datetime
import threading
import time
import types

import server


FAILED = []


def check(label, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + label
          + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(label)


NOW = time.time()


def iso(seconds):
    return datetime.datetime.fromtimestamp(
        NOW + seconds, datetime.timezone.utc).isoformat()


def acct(name, pct, org, session_reset, weekly_reset):
    a = server.Account(name, config_dir=f"/tmp/{name}", ready=True, org=org)
    a.record_usage(pct, [
        {"key": "five_hour", "label": "5h", "used": pct,
         "resets": iso(session_reset)},
        {"key": "seven_day", "label": "7d", "used": pct,
         "resets": iso(weekly_reset)},
        {"key": "weekly_scoped_fable", "label": "7d fable", "used": pct,
         "resets": iso(weekly_reset)},
    ], NOW)
    return a


def manager(*accounts):
    m = types.SimpleNamespace(
        accounts={a.name: a for a in accounts}, sessions={},
        lock=threading.RLock(), _stranded_warned=False,
        _stale_route_noted="", saved=0, moved=[])
    m.save_registry = lambda: setattr(m, "saved", m.saved + 1)
    m.broadcast_accounts = lambda: None
    for name in ("_route_key", "_routable_first", "_candidates", "_pick_pool",
                 "_best_account", "_mark_pool_walled"):
        setattr(m, name, getattr(server.SessionManager, name).__get__(m))
    return m


ef = acct("ef", 47.0, "EF", 30 * 60, 2 * 86400)
ef_alias = acct("sub3", 47.0, "EF", 30 * 60, 2 * 86400)
sub4 = acct("sub4", 59.0, "OTHER", 40 * 60, 5 * 86400)
m = manager(ef, ef_alias, sub4)

print("router quarantine:")
check("lagging percentages initially route to ef", m._best_account() == "ef")
until = m._mark_pool_walled(ef, "session")
check("confirmed wall makes another organization the next spawn",
      m._best_account() == "sub4")
check("all aliases of the walled organization are quarantined",
      ef.walled_until == until and ef_alias.walled_until == until)
check("quarantine expires at the cached 5h reset (+ grace)",
      abs(until - (server._parse_reset(ef.usage["windows"][0]["resets"])
                   + server.WALL_RESET_GRACE)) < 1)
check("wall state is registry-durable",
      ef.to_registry()["walled_until"] == until
      and ef.to_registry()["wall_kind"] == "session" and m.saved == 1)

ef.walled_until = ef_alias.walled_until = NOW - 1
check("pool automatically re-enters routing after reset", m._best_account() == "ef")

print("\nlimit rescue:")
ef.walled_until = ef_alias.walled_until = 0
m = manager(ef, ef_alias, sub4)
sent = []


class Ready:
    def wait(self, timeout):
        return True


class Eng:
    routes_accounts = True


s = types.SimpleNamespace(
    cid="incident-cid", account="ef", config_dir="/tmp/ef", alive=True,
    ceremony=False, eng=Eng(), last_bounce_rescue=0.0, _limit_kind="session",
    busy=False, hook_count=0, hooks_at_prompt=0,
    last_prompt="deploy this to heart", transcript_path="")
m.sessions[s.cid] = s


def handoff(old, target, why="", fresh_if_empty=False):
    fresh = types.SimpleNamespace(
        cid=old.cid, account=target.name, alive=True, _started_evt=Ready(),
        send_message=sent.append)
    m.sessions[old.cid] = fresh
    m.moved.append((target.name, fresh_if_empty))


m._handoff = handoff
m._stay_put_log = lambda *args: None
m.rescue_limit_wall = server.SessionManager.rescue_limit_wall.__get__(m)

old_fetch, old_sleep = server._fetch_usage, server.time.sleep
try:
    # The exact production discrepancy: the live endpoint 429 corroborates the
    # terminal wall while the retained last-good snapshot still says 47%.
    server._fetch_usage = lambda *args, **kwargs: server.RATE_LIMITED
    server.time.sleep = lambda _seconds: None
    m.rescue_limit_wall(s)
finally:
    server._fetch_usage, server.time.sleep = old_fetch, old_sleep

check("rescue chooses sub4 instead of selecting ef again",
      m.moved == [("sub4", True)], f"moves={m.moved}")
check("the eaten question is reposted exactly once on the replacement",
      sent == ["deploy this to heart"], f"sent={sent}")
check("a zero-turn wall requests a fresh replacement, not broken --resume",
      m.moved and m.moved[0][1] is True)

print()
if FAILED:
    print(f"FAILED: {len(FAILED)} — {FAILED}")
    raise SystemExit(1)
print("all good")
