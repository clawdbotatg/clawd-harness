#!/usr/bin/env python3
"""🔑 'sign in to X': the session row's `loginCta` names a SIGNED-OUT login
only when it would be the router's pick for this session's next prompt.

Austin, 2026-09-14: logins die 30 days after each sign-in (server-side,
rotation doesn't extend it). He does not want to chase every dead login —
only the high-leverage one: when the session is burning a pool that is
dead, hot, or far behind, and the better pool is merely signed out, show a
yellow button in the session's top-right pill that opens that login's
sign-in ceremony. The rule reuses the router's own thresholds
(SUB_EXHAUSTED / SUB_HOT / SUB_HYSTERESIS) so the button agrees with what
prompt-time routing would do if the login were alive.

No SessionManager is constructed (it would resume live sessions).
"""
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
server.SUB_AUTOSWITCH = True
server.SUB_REQUIRE_FABLE = False


def acct(name, pct, org, dead=False, checked=None, weekly_reset=None,
         walled=0.0):
    a = server.Account(name, config_dir=f"/nope/{name}", ready=True, org=org)
    a.record_usage(pct, [
        {"key": "five_hour", "label": "5h", "used": pct, "resets": None},
        {"key": "seven_day", "label": "7d", "used": pct,
         "resets": weekly_reset}], checked or NOW)
    if dead:
        a.broken = True
        a.login_gone = "login expired — test"
    a.walled_until = walled
    return a


def manager(*accounts):
    m = types.SimpleNamespace(accounts={a.name: a for a in accounts},
                              sessions={}, lock=threading.RLock())
    for name in ("login_cta", "_dead_pool_estimate", "_route_decision",
                 "_prompt_pool", "_candidates", "_pick_pool", "_pool_key",
                 "_routable_first"):
        setattr(m, name, getattr(server.SessionManager, name).__get__(m))
    return m


def session(account, ceremony=False, routes=True):
    return types.SimpleNamespace(
        cid="733c9edc-ef45-4404-a6ac-367e1f526ce4", account=account,
        ceremony=ceremony, eng=types.SimpleNamespace(routes_accounts=routes),
        busy=False, alive=True)


print("_dead_pool_estimate:")
m = manager()
fresh_week = acct("x", 70.0, "X", weekly_reset=(
    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW + 3 * 86400))))
check("a recent reading whose weekly reset is still ahead is trusted",
      m._dead_pool_estimate(fresh_week, NOW) == 70.0)
passed = acct("y", 70.0, "Y", checked=NOW - 7200, weekly_reset=(
    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - 3600))))
check("a reading from before its weekly reset is UNKNOWN (never 'empty')",
      m._dead_pool_estimate(passed, NOW) is None)
old = acct("z", 70.0, "Z", checked=NOW - server.USAGE_STALE_TRUST - 60)
check("a reading older than USAGE_STALE_TRUST is unknown",
      m._dead_pool_estimate(old, NOW) is None)
blank = server.Account("b", config_dir="/nope/b", ready=True, org="B")
check("no reading at all → unknown", m._dead_pool_estimate(blank, NOW) is None)

print("login_cta:")
# current pool hot, a signed-out login with headroom → fire
m = manager(acct("clawd", 98.0, "ORG"), acct("ef", 20.0, "EF", dead=True))
cta = m.login_cta(session("clawd"))
check("hot pool + signed-out cool login → CTA names it",
      cta and cta["name"] == "ef" and "98%" in cta["reason"], cta)
# current healthy, dead login only a little better → silence
m = manager(acct("clawd", 40.0, "ORG"), acct("ef", 30.0, "EF", dead=True))
check("under hysteresis → no CTA (not housekeeping)",
      m.login_cta(session("clawd")) is None)
# current healthy, dead login far ahead → fire with the gap
m = manager(acct("clawd", 60.0, "ORG"), acct("ef", 10.0, "EF", dead=True))
cta = m.login_cta(session("clawd"))
check("SUB_HYSTERESIS points behind → CTA with the gap",
      cta and cta["name"] == "ef" and "50 points" in cta["reason"], cta)
# a working same-org sibling covers the dead login → never
m = manager(acct("clawd", 98.0, "ORG"), acct("slop", 5.0, "ORG", dead=True))
check("signed-out login with a working same-org sibling → no CTA (one limit)",
      m.login_cta(session("clawd")) is None)
# the router would already move to a live cool pool → compare against THAT
m = manager(acct("clawd", 98.0, "ORG"), acct("sub2", 30.0, "AG"),
            acct("ef", 25.0, "EF", dead=True))
check("a live cool pool the router would move to beats the dead one → no CTA",
      m.login_cta(session("clawd")) is None)
m = manager(acct("clawd", 98.0, "ORG"), acct("sub2", 60.0, "AG"),
            acct("ef", 5.0, "EF", dead=True))
cta = m.login_cta(session("clawd"))
check("…unless the dead one is still far ahead of the move target",
      cta and cta["name"] == "ef" and "55 points" in cta["reason"], cta)
# the session's own login died → fire on any usable signed-out pool
m = manager(acct("slop", 50.0, "ORG", dead=True), acct("ef", 80.0, "EF", dead=True))
cta = m.login_cta(session("slop"))
check("session parked on a dead login, nothing live → CTA on the best dead pool",
      cta and cta["name"] == "slop" and "no working login" in cta["reason"], cta)
# two signed-out logins: the one with most headroom wins
m = manager(acct("clawd", 98.0, "ORG"), acct("ef", 60.0, "EF", dead=True),
            acct("sub3", 10.0, "S3", dead=True))
cta = m.login_cta(session("clawd"))
check("several signed-out logins → the one with the most headroom",
      cta and cta["name"] == "sub3", cta)
# the 2026-09-14 incident: a signed-out login with only an OLD reading must
# never be offered over a working pool — other boxes burn that org
m = manager(acct("clawd", 60.0, "ORG"),
            acct("ef", 5.0, "EF", dead=True, checked=NOW - 3 * 86400))
check("stale-reading signed-out login vs a working pool → no CTA (no guessing)",
      m.login_cta(session("clawd")) is None)
m = manager(acct("clawd", 98.0, "ORG"),
            acct("ef", 5.0, "EF", dead=True, checked=NOW - 3 * 86400))
check("…even when the working pool is hot",
      m.login_cta(session("clawd")) is None)
m = manager(acct("slop", 50.0, "ORG", dead=True),
            acct("ef", 5.0, "EF", dead=True, checked=NOW - 3 * 86400))
cta = m.login_cta(session("slop"))
check("…but with NOTHING live, an unknown login is still offered",
      cta and cta["name"] == "slop" and cta["pct"] == 50.0, cta)
check("the CTA carries the org for the client's cross-machine check",
      cta and cta["org"] == "ORG", cta)
# a walled dead login is not a candidate
m = manager(acct("clawd", 98.0, "ORG"),
            acct("ef", 5.0, "EF", dead=True, walled=NOW + 3600))
check("a CLI-walled signed-out login is not offered",
      m.login_cta(session("clawd")) is None)
# carve-outs
m = manager(acct("clawd", 98.0, "ORG"), acct("ef", 5.0, "EF", dead=True))
check("ceremony session → no CTA", m.login_cta(session("clawd", ceremony=True)) is None)
check("non-routing engine → no CTA", m.login_cta(session("clawd", routes=False)) is None)
server.SUB_AUTOSWITCH = False
check("router off → no CTA", m.login_cta(session("clawd")) is None)
server.SUB_AUTOSWITCH = True
# nothing signed out at all
m = manager(acct("clawd", 98.0, "ORG"), acct("ef", 5.0, "EF"))
check("every login alive → no CTA", m.login_cta(session("clawd")) is None)

print("the row never breaks:")
class Boom:
    def login_cta(self, s):
        raise RuntimeError("boom")
row = types.SimpleNamespace(manager=Boom(), cid="deadbeef")
check("a failing login_cta renders as None",
      server.ClaudeSession._login_cta(row) is None)
row = types.SimpleNamespace(manager=types.SimpleNamespace(), cid="deadbeef")
check("a manager without login_cta renders as None",
      server.ClaudeSession._login_cta(row) is None)

print()
if FAILED:
    print("FAILED:", *FAILED, sep="\n  ")
    raise SystemExit(1)
print("all good")
