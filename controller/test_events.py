#!/usr/bin/env python3
"""Reactor test — fleet hook frames → higher-level events (edge-triggered).

Run: python3 -m controller.test_events
"""
import tempfile

from .events import Reactor
from .ledger import TaskLedger


def hook(cid, event, busy=False, waiting=False, data=None):
    return {"type": "hook", "cid": cid, "event": event, "busy": busy,
            "waiting": waiting, "data": data or {}}


def main():
    failures = []

    def check(name, fn):
        try:
            fn()
            print(f"  ✓ {name}")
        except Exception as e:
            print(f"  ✗ {name}: {e}")
            failures.append(name)

    ledger = TaskLedger(tempfile.mktemp(suffix=".jsonl"))
    r = Reactor(ledger)
    got = []
    r.on_event(got.append)

    def t_blocked_rising_edge():
        got.clear()
        # working → blocked (rising edge) fires once
        r.feed("self", hook("c1", "PreToolUse", busy=True, waiting=False))
        r.feed("self", hook("c1", "Notification", busy=True, waiting=True,
                            data={"message": "permission needed"}))
        blocked = [e for e in got if e["kind"] == "blocked"]
        assert len(blocked) == 1, got
        assert blocked[0]["summary"] == "permission needed", blocked
        # another hook while still waiting must NOT refire blocked
        r.feed("self", hook("c1", "Notification", busy=True, waiting=True))
        assert len([e for e in got if e["kind"] == "blocked"]) == 1, "no refire on the plateau"
    check("blocked fires on the rising edge, once", t_blocked_rising_edge)

    def t_turn_done_with_task():
        got.clear()
        tk = ledger.create_task("do a thing")
        ledger.assign(tk["id"], "c2", "self")
        r.feed("self", hook("c2", "Stop", busy=False, waiting=False,
                            data={"last": "all set"}))
        td = [e for e in got if e["kind"] == "turn_done"]
        assert len(td) == 1 and td[0]["task"] == tk["id"], td
        assert td[0]["summary"] == "all set", td
    check("Stop → turn_done carries the task link + answer", t_turn_done_with_task)

    def t_ended():
        got.clear()
        r.feed("self", hook("c3", "SessionEnd", data={"reason": "done"}))
        assert any(e["kind"] == "ended" for e in got), got
    check("SessionEnd → ended event", t_ended)

    def t_non_hook_ignored():
        got.clear()
        r.feed("self", {"type": "sessions", "sessions": []})
        assert got == [], got
    check("non-hook frames produce no events", t_non_hook_ignored)

    def t_recent_feed():
        assert len(r.recent()) >= 3, "notifications accumulate in the ring"
    check("recent() returns the event ring", t_recent_feed)

    print()
    if failures:
        print(f"FAILED: {failures}")
        return 1
    print("PASSED: Reactor (hook → higher-level event)")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
