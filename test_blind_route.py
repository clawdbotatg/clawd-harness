#!/usr/bin/env python3
"""Every measured pool dead ≠ nowhere to go: an unmeasured healthy login is
the fallback, and a stopped VM's custody record must not freeze a login.

Why this exists: on 2026-09-06 (12:12, head) a ＋ session for picowallet
spawned onto `clawd` — the ONLY pool with a fresh reading, and that reading
was 100% — and painted "You've hit your session limit · resets 2pm". The
banner tripwire fired, confirmed the wall, and logged `nowhere better to go
(router's best: none; fresh: -; stale-cool: -)`. Two healthy logins sat on
the box the whole time: sub2 (austingriffith 20x, 35% weekly) and sub4
(clawd org, 28%). Neither had a usable reading because their access tokens
had expired and the poller would not refresh them — both were named by
cont custody records for VMs that had been STOPPED for a day (auditor) and
a week (research). cont's own rule honors only RUNNING VMs and reclaims the
rest; the harness honored every record forever. sub2 had been out of
routing for 32 h.

Pins:
  * `_blind_alternative` returns a ready, unwalled login whose reading is
    absent or long-stale, never one whose FRESH reading is hot, never a
    walled/broken one, never the excluded org/name; None when nothing fits.
  * a stale reading is ranked by `_route_key` like any other (capable,
    cool, soonest weekly reset first).
  * `_vm_custody_stale` names records whose VM is not running; unknown
    (tart absent) means no record is stale; `_vm_custody_dirs` still
    returns every record (the refresh gate stays conservative).

Pure helpers only — no SessionManager, no registry, nothing spawns.

    python3 test_blind_route.py
"""
import os
import sys
import tempfile
import time
import types

import server

NOW = time.time()
H = 3600


def acct(name, pct=None, age=0.0, org="", fable=True, reset_in_h=48,
         walled=False, broken=False):
    a = server.Account(name, config_dir=f"/tmp/{name}", ready=True, org=org)
    if pct is not None:
        resets = server.datetime.datetime.fromtimestamp(
            NOW + reset_in_h * 3600, server.datetime.timezone.utc).isoformat()
        windows = [{"key": "seven_day", "label": "7d", "used": pct, "resets": resets}]
        if fable:
            windows.append({"key": "weekly_scoped_fable", "label": "7d fable",
                            "used": pct, "resets": resets})
        a.usage = {"pct": pct, "windows": windows,
                   "checkedAt": NOW - age, "goodAt": NOW - age}
        a.fable_seen = (NOW - age) if fable else 0.0
    a.walled_until = NOW + H if walled else 0.0
    a.broken = broken
    return a


def mgr(*accounts):
    m = types.SimpleNamespace(
        accounts={a.name: a for a in accounts},
        lock=server.threading.RLock(),
        _stranded_warned=False, _stale_route_noted="")
    for meth in ("_route_key", "_routable_first", "_candidates", "_pick_pool",
                 "_best_account", "_blind_alternative"):
        setattr(m, meth, getattr(server.SessionManager, meth).__get__(m))
    return m


fails = 0


def check(label, cond):
    global fails
    print(("ok   " if cond else "FAIL ") + label)
    if not cond:
        fails += 1


OLD = server.USAGE_STALE_TRUST + H              # past the stale-cool bar

# --- the head incident, verbatim -------------------------------------------
m = mgr(acct("clawd", 100.0, org="slop"),                  # fresh, walled by the CLI
        acct("slop", 50.0, age=3 * H, org="slop", walled=True),
        acct("ef", 99.0, org="ef", walled=True),
        acct("sub2", 4.0, age=32 * H, org="ag", reset_in_h=6 * 24),
        acct("sub4", 28.0, age=OLD, org="cl", reset_in_h=4 * 24))
m.accounts["clawd"].walled_until = NOW + H
check("head 09-06: the measured router has nothing", m._best_account() is None)
alt = m._blind_alternative(exclude_org="slop", exclude_name="clawd")
check("…blind fallback finds a login (not 'nowhere to go')", alt is not None)
check("…and it is the one whose weekly window resets soonest (sub4)",
      alt and alt.name == "sub4")

# --- the spawn case: the only fresh reading is 100% -------------------------
m = mgr(acct("clawd", 100.0, org="slop"),
        acct("sub2", 4.0, age=32 * H, org="ag"))
check("spawn: _best_account still hands back the fresh 100% pool (unchanged)",
      m._best_account() == "clawd")
check("spawn: blind alternative skips the fresh-hot pool → sub2",
      (m._blind_alternative(exclude_name="clawd") or acct("-")).name == "sub2")

# --- exclusions --------------------------------------------------------------
m = mgr(acct("a", None, org="o1"), acct("b", None, org="o2", walled=True),
        acct("c", None, org="o3", broken=True), acct("d", 98.0, org="o4"),
        acct("e", 98.0, age=OLD, org="o5"))
check("unmeasured ready login qualifies", m._blind_alternative().name in ("a", "e"))
check("walled login never", m._blind_alternative(exclude_org="o1", exclude_name="e") is None
      or m._blind_alternative(exclude_org="o1", exclude_name="e").name not in ("b", "c", "d"))
check("fresh-hot login never", all(
    (m._blind_alternative(exclude_name=x) or acct("-")).name != "d" for x in ("a", "e")))
check("a long-stale HOT reading is still a bet (it may have reset)",
      m._blind_alternative(exclude_org="o1").name == "e")
check("exclude_org honored", m._blind_alternative(exclude_org="o1", exclude_name="e") is None)
check("nothing qualifies → None",
      mgr(acct("x", 100.0, org="o"), acct("y", None, org="o", walled=True))
      ._blind_alternative() is None)

# --- custody records vs running VMs -----------------------------------------
tmp = tempfile.mkdtemp()
for vm, d in (("auditor", "/Users/u/.clawd-accounts/sub2"),
              ("auditor2", "/Users/u/.clawd-accounts/clawd"),
              ("research", "/Users/u/.clawd-accounts/sub4/")):
    with open(os.path.join(tmp, vm), "w") as fh:
        fh.write(d + "\n")
server.VM_CUSTODY_DIR = tmp
orig = server._running_vms
server._running_vms = lambda: {"auditor2"}
check("custody dirs: every record, running or not (refresh gate stays conservative)",
      server._vm_custody_dirs() == {"/Users/u/.clawd-accounts/sub2",
                                    "/Users/u/.clawd-accounts/clawd",
                                    "/Users/u/.clawd-accounts/sub4"})
stale = dict(server._vm_custody_stale())
check("stale custody: the stopped VMs' records, not the running one",
      set(stale) == {"auditor", "research"}
      and stale["auditor"] == "/Users/u/.clawd-accounts/sub2")
server._running_vms = lambda: None
check("tart can't answer → nothing is called stale (unknown ≠ stopped)",
      server._vm_custody_stale() == [])
server._running_vms = orig
server.VM_CUSTODY_DIR = os.path.join(tmp, "missing")
check("no ledger → no records", server._vm_custody_dirs() == set()
      and server._vm_custody_stale() == [])

print(f"\n{'ALL OK' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
