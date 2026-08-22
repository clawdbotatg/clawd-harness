#!/usr/bin/env python3
"""Freshness is a ranking tier, not a filter: a stale-but-cool pool beats a
fresh-but-hot one.

Why this exists: on 2026-08-22 heart spawned a new session onto a plan the
poller had JUST read at 100% ("You've hit your weekly limit"), and the limit
tripwire fired twice and went silent, while the one pool on the box with
headroom (sub3, 83%) sat one reading away — a reading 2h old, because its
idle sessions hold the grant (single-consumer rule) and never renewed the
access token. `_best_account`'s 3×TTL filter dropped it, the candidate set
collapsed to the dead pools, and promise 3 of EXPECTATIONS.md broke on a box
that had a working login with headroom. Root cause v3's symptom (stale data
silently shrinking the roster), a different cause.

Pins:
  * every fresh pool hot + a stale cool one → the stale one is routed to,
  * a fresh cool pool still wins over a stale one with MORE headroom,
  * a stale reading past USAGE_STALE_TRUST is a guess and is not used,
  * a stale pool that is itself hot is never a fallback,
  * the prompt-time picker (_prompt_pool) applies the same rule, and never
    falls back to a stale sibling of an org that has a fresh reading.

Pure ranking helpers only — no SessionManager, no registry, nothing spawns.

    python3 test_stale_route.py
"""
import sys
import time
import types

import server

NOW = time.time()


def acct(name, pct, age=0.0, org="", fable=True, reset_in_h=48):
    a = server.Account(name, config_dir=f"/tmp/{name}", ready=True, org=org)
    resets = server.datetime.datetime.fromtimestamp(
        NOW + reset_in_h * 3600, server.datetime.timezone.utc).isoformat()
    windows = [{"key": "seven_day", "label": "7d", "used": pct, "resets": resets}]
    if fable:
        windows.append({"key": "weekly_scoped_fable", "label": "7d fable",
                        "used": pct, "resets": resets})
    a.usage = {"pct": pct, "windows": windows,
               "checkedAt": NOW - age, "goodAt": NOW - age}
    a.fable_seen = (NOW - age) if fable else 0.0   # a sighting is sticky; none for a fable-less plan
    return a


def mgr(*accounts):
    m = types.SimpleNamespace(
        accounts={a.name: a for a in accounts},
        lock=server.threading.RLock(),
        _stranded_warned=False, _stale_route_noted="")
    for meth in ("_route_key", "_pool_key", "_routable_first", "_candidates",
                 "_pick_pool", "_best_account", "_prompt_pool"):
        setattr(m, meth, getattr(server.SessionManager, meth).__get__(m))
    return m


fails = 0


def check(label, cond):
    global fails
    print(("ok   " if cond else "FAIL ") + label)
    if not cond:
        fails += 1


H = 3600
STALE = 3 * server.USAGE_TTL + 60                # just past the fresh bar

# --- the heart incident, verbatim ------------------------------------------
m = mgr(acct("clawd", 100.0, org="ef"),
        acct("slop", 99.0, org="slop"),
        acct("sub3", 83.0, age=2 * H, org="clawd"))
check("heart 08-22: fresh pools 100/99, stale sub3 83% (2h) → sub3",
      m._best_account() == "sub3")
check("…and the prompt-time picker agrees",
      m._prompt_pool().name == "sub3")

# --- fresh cool still beats stale, even with less headroom -------------------
m = mgr(acct("a", 80.0), acct("b", 20.0, age=2 * H))
check("fresh 80% beats stale 20%", m._best_account() == "a")

# --- only when every fresh pool is hot -------------------------------------
m = mgr(acct("a", 97.0), acct("b", 50.0, age=STALE))
check("fresh at SUB_HOT → stale cool wins", m._best_account() == "b")
m = mgr(acct("a", 96.0), acct("b", 50.0, age=STALE))
check("fresh just under SUB_HOT → fresh wins", m._best_account() == "a")

# --- a stale reading has a horizon ----------------------------------------
m = mgr(acct("a", 100.0), acct("b", 50.0, age=server.USAGE_STALE_TRUST + 60))
check("stale past USAGE_STALE_TRUST is not a fallback (hot fresh returned)",
      m._best_account() == "a")

# --- a hot stale pool is never a fallback -----------------------------------
m = mgr(acct("a", 100.0), acct("b", 98.0, age=2 * H))
check("stale but hot → not a fallback", m._best_account() == "a")

# --- nothing fresh at all: stale cool still routes -------------------------
m = mgr(acct("a", 60.0, age=2 * H), acct("b", 30.0, age=3 * H))
check("no fresh readings: best stale cool routes", m._best_account() in ("a", "b"))

# --- among stale, the normal policy applies (reset-soonest) ----------------
m = mgr(acct("hot", 100.0),
        acct("late", 30.0, age=2 * H, reset_in_h=100),
        acct("soon", 60.0, age=2 * H, reset_in_h=10))
check("stale tier ranks by reset-soonest like the fresh tier",
      m._best_account() == "soon")

# --- the fable gate still applies inside the stale tier --------------------
m = mgr(acct("hot", 100.0),
        acct("nofable", 10.0, age=2 * H, fable=False),
        acct("fable", 70.0, age=2 * H))
check("stale tier: capable pool beats a fable-less one with more headroom",
      m._best_account() == "fable")

# --- prompt pool: an org with a fresh reading never uses a stale sibling ---
m = mgr(acct("ef1", 100.0, org="ef"),
        acct("ef2", 40.0, age=2 * H, org="ef"),     # sibling's old copy
        acct("other", 100.0, org="x"))
check("_prompt_pool: stale sibling of a fresh-read org is ignored",
      m._prompt_pool().name in ("ef1", "other"))

# --- the log fires once per stale target and re-arms on a fresh pick -------
m = mgr(acct("a", 100.0), acct("b", 50.0, age=2 * H))
m._best_account(); noted1 = m._stale_route_noted
m._best_account(); noted2 = m._stale_route_noted
check("stale route noted once (no re-log while unchanged)",
      noted1 == "b" and noted2 == "b")
m.accounts["a"].usage["pct"] = 10.0
m._best_account()
check("fresh pick re-arms the note", m._stale_route_noted == "")

print("\nALL OK" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
