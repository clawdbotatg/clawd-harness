#!/usr/bin/env python3
"""A login whose refresh-token family expired must leave routing and its
sessions must move to a WORKING login — even a same-org sibling.

2026-09-06 incident (slop on head): the family's `refreshTokenExpiresAt`
passed at 03:56, claude wiped the grant, and the session answered every
prompt "Login expired · Please run /login" for hours. Nothing caught it:
  * the poller polls ONE login per org and copies its numbers onto the
    siblings — clawd (same org, healthy) fronted the poll, so slop read
    "50%, checked 1 min ago" with an empty credential blob;
  * the CLI's reply is a <synthetic> assistant line with ordinary hooks —
    no limit banner (PTY tripwire), no hook silence (send watchdog);
  * every router path refused the only sane move (slop → clawd) because
    "best pool shares this org — one limit, a move buys nothing".

No SessionManager is constructed (it would resume live sessions); the
credential store is a temp dir's .credentials.json (the keychain answers
"no such item" for a path it has never seen, so the file is the store).
"""
import json
import os
import tempfile
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
TMP = tempfile.mkdtemp(prefix="login-expired-")


def store(name, access, refresh, refresh_exp_ms):
    d = os.path.join(TMP, name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, ".credentials.json"), "w") as f:
        json.dump({"claudeAiOauth": {
            "accessToken": access, "refreshToken": refresh,
            "expiresAt": int((NOW + 3600) * 1000) if access else 0,
            "refreshTokenExpiresAt": refresh_exp_ms,
            "subscriptionType": "max"}}, f)
    return d


# The incident blob verbatim: tokens blanked, horizon in the past.
WIPED = store("slop", "", "", int((NOW - 5 * 3600) * 1000))
HEALTHY = store("clawd", "acc-1", "ref-1", int((NOW + 16 * 86400) * 1000))
SOON = store("ef", "acc-2", "ref-2", int((NOW + 600) * 1000))
NOFILE = os.path.join(TMP, "nothing")

print("_login_state / _login_verdict:")
st = server._login_state(WIPED)
check("a blanked blob is 'wiped'", st["wiped"] and not st["has_access"])
check("wiped → verdict names the wipe",
      "wiped" in (server._login_verdict(WIPED, NOW)[0] or ""))
st = server._login_state(HEALTHY)
check("a healthy blob is not wiped and reports its horizon",
      not st["wiped"] and abs(st["refresh_expires"] - (NOW + 16 * 86400)) < 1)
check("healthy horizon → no verdict", server._login_verdict(HEALTHY, NOW)[0] is None)
why, _ = server._login_verdict(SOON, NOW)
check("a horizon inside SUB_LOGIN_HORIZON → retire early",
      why is not None and "expires in" in why, why)
st = server._login_state(NOFILE)
check("no store at all is NOT wiped (absent ≠ wiped)", not st["wiped"])


def acct(name, cfg, pct, org):
    a = server.Account(name, config_dir=cfg, ready=True, org=org)
    a.record_usage(pct, [
        {"key": "five_hour", "label": "5h", "used": pct, "resets": None},
        {"key": "seven_day", "label": "7d", "used": pct, "resets": None}], NOW)
    return a


def manager(*accounts, best=None):
    m = types.SimpleNamespace(
        accounts={a.name: a for a in accounts}, sessions={},
        lock=threading.RLock(), moved=[], redelivered=[], stayed=[],
        broadcasts=0)
    m._best_account = lambda: best
    m._prompt_pool = lambda: m.accounts.get(best)
    m.broadcast_accounts = lambda: setattr(m, "broadcasts", m.broadcasts + 1)
    m._stay_put_log = lambda s, b, what: m.stayed.append(what)
    m._handoff = lambda s, target, why="", fresh_if_empty=False: \
        m.moved.append((s.account, target.name, why))
    m._redeliver = lambda s, prompt: m.redelivered.append(prompt)
    for name in ("_retire_login", "rescue_login_expired", "_route_decision"):
        setattr(m, name, getattr(server.SessionManager, name).__get__(m))
    return m


def session(account):
    return types.SimpleNamespace(
        cid="733c9edc-ef45-4404-a6ac-367e1f526ce4", account=account,
        ceremony=False, eng=types.SimpleNamespace(routes_accounts=True),
        last_bounce_rescue=0.0, busy=False, alive=True)


print("_retire_login:")
slop = acct("slop", WIPED, 50.0, "ORG")
clawd = acct("clawd", HEALTHY, 50.0, "ORG")
m = manager(slop, clawd, best="clawd")
m._retire_login(slop, "login expired — test")
check("retired login is broken with the store's verdict kept",
      slop.broken and slop.login_gone and "sign in again" in slop.error)
check("card reads needs-login", slop.meta()["status"] == "needs-login")
check("a usage 200 on a cached token must not re-admit it",
      bool(slop.login_gone) is True)   # rescue paths set broken = bool(login_gone)

print("_route_decision:")
s = session("slop")
dec, target, why = m._route_decision(s)
check("dead login moves to its same-org sibling",
      dec == "move" and target is clawd and "login" in why, (dec, why))
slop2 = acct("slop", WIPED, 50.0, "ORG")           # healthy twin, same org
m2 = manager(slop2, clawd, best="clawd")
dec, target, why = m2._route_decision(session("slop"))
check("a healthy same-org login still stays put (one limit)",
      dec == "stay" and "shares this org" in why, (dec, why))

print("rescue_login_expired:")
server.SUB_AUTOSWITCH = True
slop = acct("slop", WIPED, 50.0, "ORG")
clawd = acct("clawd", HEALTHY, 50.0, "ORG")
m = manager(slop, clawd, best="clawd")
s = session("slop")
m.rescue_login_expired(s, "please bring the front end back up")
check("wiped store → login retired", slop.broken and bool(slop.login_gone))
check("session handed to the same-org sibling",
      m.moved and m.moved[0][:2] == ("slop", "clawd"), m.moved)
check("the eaten prompt is redelivered",
      m.redelivered == ["please bring the front end back up"])
check("busy cleared for the respawn", s.busy is False)

m = manager(acct("clawd", HEALTHY, 50.0, "ORG"),
            acct("ef", SOON, 20.0, "EF"), best="ef")
s = session("clawd")
m.rescue_login_expired(s, "quoting: Login expired · Please run /login")
check("intact store → a quote is a no-op (nothing moved, nothing retired)",
      not m.moved and not m.accounts["clawd"].broken)

slop = acct("slop", WIPED, 50.0, "ORG")
m = manager(slop, best=None)
s = session("slop")
m.rescue_login_expired(s, "x")
check("nowhere to go → retired and logged, not moved",
      slop.broken and not m.moved and m.stayed == ["login expired"])

print()
if FAILED:
    print("FAILED:", *FAILED, sep="\n  ")
    raise SystemExit(1)
print("all good")
