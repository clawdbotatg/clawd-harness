#!/usr/bin/env python3
"""test_login_push.py — the phone rings when a subscription login needs a
human (2026-09-06: logins die ~30 d after each sign-in; the harness moves the
sessions, the sign-in is the one thing only a person can do).

  1. a login flipping ready → needs-login pings once, not on every frame;
  2. a login already dead when the worker boots does NOT ping (a worker
     restart must never re-fire old alerts);
  3. a ready login inside the 3-day horizon pings once a day, not per frame;
  4. a far-off horizon, or no push subscribers, pings nothing.
"""
import sys
import time
import types

import worker as workermod

FAILED = []


def check(label, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + label
          + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(label)


def make(subs=True):
    w = types.SimpleNamespace(machine="clawd-head", _acct_notify={}, sent=[],
                              push_subs=[{"endpoint": "https://push/x"}] if subs else [],
                              vapid=types.SimpleNamespace(can_send=True))
    w.maybe_notify_accounts = workermod.Worker.maybe_notify_accounts.__get__(w)
    w._send_push_all = lambda payload=None: w.sent.append(payload)
    return w


def frame(**accts):
    return {"type": "accounts", "accounts": [dict(name=n, **a) for n, a in accts.items()]}


def settle(w):
    t0 = time.time()
    while time.time() - t0 < 1.0 and any(th.name.startswith("Thread") and th.is_alive()
                                          for th in __import__("threading").enumerate()
                                          if th is not __import__("threading").main_thread()):
        time.sleep(0.02)
    return w.sent


NOW = time.time()
ok = {"status": "ready", "orgName": "slop org", "loginExpiresAt": NOW + 20 * 86400}
dead = {"status": "needs-login", "orgName": "slop org",
        "error": "login expired — the client wiped its credential blob; sign in again"}

print("flip ready → needs-login:")
w = make()
w.maybe_notify_accounts(frame(slop=ok)); settle(w)
w.maybe_notify_accounts(frame(slop=dead)); settle(w)
w.maybe_notify_accounts(frame(slop=dead)); settle(w)
check("exactly one ping for the flip", len(w.sent) == 1, len(w.sent))
check("the ping names the machine, the org and the reason",
      w.sent and b"clawd-head" in w.sent[0] and b"slop org" in w.sent[0]
      and b"wiped" in w.sent[0])

print("already dead at boot:")
w = make()
w.maybe_notify_accounts(frame(slop=dead)); settle(w)
w.maybe_notify_accounts(frame(slop=dead)); settle(w)
check("no ping — a worker restart never re-fires old alerts", not w.sent)

print("horizon warning:")
soon = dict(ok, loginExpiresAt=NOW + 2 * 86400 + 3600)
w = make()
for _ in range(3):
    w.maybe_notify_accounts(frame(sub4=soon)); settle(w)
check("inside 3 days → one ping per day, not per frame", len(w.sent) == 1, len(w.sent))
check("says how long is left", w.sent and b"expires in 2 days" in w.sent[0], w.sent)
w._acct_notify["sub4"]["warned"] = NOW - 86401
w.maybe_notify_accounts(frame(sub4=soon)); settle(w)
check("a day later it rings again", len(w.sent) == 2)

print("nothing to say:")
w = make()
w.maybe_notify_accounts(frame(a=ok, b=dict(ok, loginExpiresAt=None))); settle(w)
check("far horizon / unknown horizon → silent", not w.sent)
w = make(subs=False)
w.maybe_notify_accounts(frame(a=soon)); settle(w)
check("no subscribers → silent", not w.sent)

print()
if FAILED:
    print("FAILED:", *FAILED, sep="\n  ")
    sys.exit(1)
print("all good")
