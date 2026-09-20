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

print("\nthe wall is whichever window is full (2026-09-20):")
am = acct("austinmax", 98.0, "ATG", 5 * 3600, 2 * 86400)
am.usage["windows"][0]["used"] = 3.0        # 5h has room; the weekly is the wall
m = manager(am, sub4)
until = m._mark_pool_walled(am, "session")  # the scan misread the banner
weekly_reset = server._parse_reset(am.usage["windows"][1]["resets"])
check("quarantine runs to the WEEKLY reset, not the 5h one",
      abs(until - (weekly_reset + server.WALL_RESET_GRACE)) < 1,
      f"until={until - NOW:.0f}s")
check("the recorded kind follows the full window", am.wall_kind == "weekly")
both = acct("both", 100.0, "B", 30 * 60, 3 * 86400)   # 5h AND 7d full
m = manager(both, sub4)
until = m._mark_pool_walled(both, "session")
check("every full window must reset — the later one wins",
      abs(until - (server._parse_reset(both.usage["windows"][1]["resets"])
                   + server.WALL_RESET_GRACE)) < 1)
check("a snapshot with no full window keeps the scan's kind (the 09-03 case)",
      ef.wall_kind == "session")

print("\nthe sweep's rescue retypes what the dead plan ate:")
dead = acct("austinmax", 98.0, "ATG", 5 * 3600, 2 * 86400)
dead.walled_until = NOW + 3600
good = acct("sub4b", 16.0, "OTHER2", 40 * 60, 4 * 86400)
m = manager(dead, good)
for name in ("_handoff_sweep", "_finish_rescue"):
    setattr(m, name, getattr(server.SessionManager, name).__get__(m))
m._rebalance_win = lambda name, best: None
m._blind_log = lambda *args: None
m._blind_alternative = lambda *args, **kwargs: None


class Eng2:
    routes_accounts = True

    def bg_probe(self, s):
        return False


def parked(cid, hook_count, hooks_at_prompt, last_prompt):
    return types.SimpleNamespace(
        cid=cid, account="austinmax", alive=True, busy=False, bg=False,
        ceremony=False, last_handoff=0.0, last_active=0.0, eng=Eng2(),
        hook_count=hook_count, hooks_at_prompt=hooks_at_prompt,
        last_prompt=last_prompt)


eaten = parked("eaten", 5, 5, "write the handoff")   # no hook since the send
ran = parked("ran", 6, 5, "already answered")        # a Stop came — it ran
m.sessions = {eaten.cid: eaten, ran.cid: ran}
sent2 = []


def handoff2(old, target, why=None, fresh_if_empty=False):
    fresh = types.SimpleNamespace(
        cid=old.cid, account=target.name, alive=True, _started_evt=Ready(),
        send_message=lambda text: sent2.append((old.cid, text)))
    m.sessions[old.cid] = fresh


m._handoff = handoff2
old_sleep = server.time.sleep
try:
    server.time.sleep = lambda _seconds: None
    m._handoff_sweep()
finally:
    server.time.sleep = old_sleep
deadline = time.time() + 3
while time.time() < deadline and not sent2:
    time.sleep(0.05)
check("both parked sessions were moved",
      all(m.sessions[c].account == "sub4b" for c in ("eaten", "ran")))
check("the eaten prompt is retyped on the replacement",
      sent2 == [("eaten", "write the handoff")], f"sent={sent2}")

print()
if FAILED:
    print(f"FAILED: {len(FAILED)} — {FAILED}")
    raise SystemExit(1)
print("all good")
