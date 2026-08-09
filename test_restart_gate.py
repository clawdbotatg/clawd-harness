#!/usr/bin/env python3
"""The graceful-restart gate: what may hold a restart back, and for how long.

Why this exists: on 2026-08-09 a subscription-routing fix sat unapplied on
clawd-head for 30+ minutes. The box had the commit; the RUNNING process was
stale, because `busy_count()` counted a session that was merely **blocked on
an interactive permission prompt** as mid-turn. That session was parked on a
human and could have sat there all day — meanwhile the harness kept spawning
new sessions onto the exact plan the pending fix existed to avoid.

The lesson isn't "restart more aggressively", it's that the wait had no floor
and no ceiling. So:

  * `waiting` (parked on a human) must NOT hold a restart,
  * genuinely mid-turn work and background shells still must,
  * and the wait expires, because code that can never land is its own outage.

Pure state logic — constructs no SessionManager, spawns nothing, and never
touches the live registry (see the scratch-registry trap).

    python3 test_restart_gate.py
"""
import sys
import threading
import time
import types

import server


def sess(cid, busy=False, waiting=False, bg="", alive=True, title=""):
    s = types.SimpleNamespace(cid=cid, busy=busy, waiting=waiting, bg=bg,
                              alive=alive, title=title)
    return s


def mgr(*sessions, pending=True, since=None):
    m = types.SimpleNamespace(
        sessions={s.cid: s for s in sessions},
        lock=threading.RLock(),
        _restart_lock=threading.Lock(),
        _restarting=False,
        restart_pending=pending,
        restart_reason="test",
        restart_since=time.time() if since is None else since)
    for meth in ("restart_blockers", "restart_state"):
        setattr(m, meth, getattr(server.SessionManager, meth).__get__(m))
    return m


CASES = []


def case(fn):
    CASES.append(fn)
    return fn


@case
def test_a_prompt_blocked_session_does_not_hold_the_restart():
    """THE regression. `waiting` sessions still carry busy=True — that is what
    turned a pending restart into a permanent one."""
    m = mgr(sess("blocked1", busy=True, waiting=True))
    assert m.restart_blockers() == [], \
        "a session parked on a human must not count as mid-turn"


@case
def test_a_real_mid_turn_session_still_holds_it():
    """The protection that matters is unchanged: SIGTERM during a turn drops a
    partial reply and cancels an in-flight tool call."""
    m = mgr(sess("working", busy=True))
    assert [s.cid for s in m.restart_blockers()] == ["working"]


@case
def test_background_work_still_holds_it():
    """A turn resumes; a background shell does not — nothing restarts it."""
    m = mgr(sess("bgshell", busy=False, bg="shell"))
    assert [s.cid for s in m.restart_blockers()] == ["bgshell"]


@case
def test_dead_sessions_never_hold_it():
    m = mgr(sess("zombie", busy=True, alive=False))
    assert m.restart_blockers() == []


@case
def test_the_clawd_head_mix_reduces_to_the_real_workers():
    """The actual 08-09 shape: 4 'busy' sessions, only some genuinely working."""
    m = mgr(sess("working", busy=True),
            sess("prompted", busy=True, waiting=True),
            sess("idle"),
            sess("bg", bg="agent"))
    assert sorted(s.cid for s in m.restart_blockers()) == ["bg", "working"]


@case
def test_state_frame_reports_blockers_not_raw_busy():
    """The banner's count and its 'restart now' button must agree about what
    is being waited on — so the frame carries blockers, not busy_count."""
    m = mgr(sess("working", busy=True, title="Fix the thing"),
            sess("prompted", busy=True, waiting=True))
    st = m.restart_state()
    assert st["busy"] == 1, "the prompted session must not inflate the count"
    assert [b["title"] for b in st["blockers"]] == ["Fix the thing"]
    assert st["maxWait"] == server.RESTART_MAX_WAIT


@case
def test_no_pending_restart_reports_no_blockers():
    """Don't walk the session list to describe a restart nobody asked for."""
    st = mgr(sess("working", busy=True), pending=False).restart_state()
    assert st["pending"] is False and st["busy"] == 0 and st["blockers"] == []


@case
def test_the_wait_has_a_ceiling():
    """A machine somebody actually uses can be never-quiet. The ceiling is the
    difference between 'deferred' and 'never applied'."""
    assert server.RESTART_MAX_WAIT > 0, "a wait with no ceiling can never land"
    fresh, stale = mgr(sess("w", busy=True)), mgr(
        sess("w", busy=True), since=time.time() - server.RESTART_MAX_WAIT - 1)
    assert fresh.restart_state()["waitedFor"] < 5
    assert stale.restart_state()["waitedFor"] >= server.RESTART_MAX_WAIT


if __name__ == "__main__":
    failed = 0
    for fn in CASES:
        try:
            fn()
            print(f"  ok   {fn.__name__}")
        except Exception as e:
            failed += 1
            print(f"  FAIL {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(CASES) - failed}/{len(CASES)} passed")
    sys.exit(1 if failed else 0)
