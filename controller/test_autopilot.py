#!/usr/bin/env python3
"""Autopilot + notes: the middle-manager loop, no real claude anywhere.

Asserts the runaway guards as hard requirements:
  - blocked → ONE triage turn; per-cid cooldown suppresses the echo storm
  - the PM's own recent action on a cid suppresses the event (own-echo)
  - turn_done on a task-linked session → verify turn; per-task cooldown + cap
  - hourly budget hard-stops turns and notifies the operator once
  - kill switch (set_enabled) persists and stops everything
  - autonomy=readonly pauses the autopilot outright
  - escalate: digest items batch into ONE push; urgency='now' bypasses
  - notes: remember/forget/priorities round-trip; render is bounded

Run:  python3 -m controller.test_autopilot
"""
import os
import sys
import tempfile
import time

from .autopilot import Autopilot
from .ledger import TaskLedger
from .notes import NotesStore


class FakeGuard:
    autonomy = "auto"


def main():
    failures = []

    def check(name, fn):
        try:
            fn()
            print(f"  ✓ {name}")
        except Exception as e:
            print(f"  ✗ {name}: {e}")
            failures.append(name)

    turns = []          # (kind, prompt)
    pings = []          # notify() messages

    def run_pm(kind, prompt):
        turns.append((kind, prompt))
        return f"handled {kind}"

    ledger = TaskLedger(tempfile.mktemp(suffix=".jsonl"))
    guard = FakeGuard()
    toggle = tempfile.mktemp(suffix=".txt")
    ap = Autopilot(run_pm, verbs=None, ledger=ledger, guard=guard,
                   notify=pings.append, enabled=True, toggle_path=toggle,
                   cooldown_s=60, verify_cooldown_s=60, own_action_s=60,
                   max_per_hour=3, max_per_day=5, digest_window_s=60)
    # drive _handle directly — deterministic, no worker thread

    def t_triage():
        ap._handle({"kind": "blocked", "machine": "m1", "cid": "c1",
                    "summary": "permission prompt"})
        assert len(turns) == 1 and turns[0][0] == "triage", turns
        assert "m1/c1" in turns[0][1] and "sweep protocol" in turns[0][1], turns[0][1]
    check("blocked → one triage turn with evidence-first prompt", t_triage)

    def t_cooldown():
        ap._handle({"kind": "blocked", "machine": "m1", "cid": "c1",
                    "summary": "again"})
        assert len(turns) == 1, turns          # suppressed by per-cid cooldown
    check("per-cid cooldown suppresses the echo storm", t_cooldown)

    def t_own_echo():
        ledger.audit("ask", {"machine": "m1", "cid": "c-own"}, {"ok": True})
        ap._handle({"kind": "blocked", "machine": "m1", "cid": "c-own",
                    "summary": "our own send parked it"})
        assert len(turns) == 1, turns          # own recent write → skipped
    check("own-action echo is skipped", t_own_echo)

    def t_verify():
        t = ledger.create_task("add a README", project="p1", machine="m1",
                               acceptance="README.md exists with usage docs")
        ledger.assign(t["id"], "c2", "m1")     # → in_progress
        ap._handle({"kind": "turn_done", "machine": "m1", "cid": "c2",
                    "task": t["id"], "summary": "done: wrote things"})
        assert len(turns) == 2 and turns[1][0] == "verify", turns
        assert t["id"] in turns[1][1] and "README.md exists" in turns[1][1], turns[1][1]
        # verify cooldown: an immediate second Stop doesn't re-verify
        ap._handle({"kind": "turn_done", "machine": "m1", "cid": "c2",
                    "task": t["id"], "summary": "another turn"})
        assert len(turns) == 2, turns
    check("turn_done on task-linked session → verify turn (cooldown capped)", t_verify)

    def t_no_task_no_verify():
        ap._handle({"kind": "turn_done", "machine": "m1", "cid": "c-free",
                    "summary": "chatty turn"})
        assert len(turns) == 2, turns
    check("turn_done without a task is ignored", t_no_task_no_verify)

    def t_budget():
        ap._handle({"kind": "blocked", "machine": "m2", "cid": "c3",
                    "summary": "third turn — at the hourly cap"})
        assert len(turns) == 3, turns
        before = len(pings)
        ap._handle({"kind": "blocked", "machine": "m2", "cid": "c4",
                    "summary": "over budget"})
        assert len(turns) == 3, turns                       # hard stop
        assert len(pings) == before + 1 and "budget exhausted" in pings[-1], pings
        ap._handle({"kind": "blocked", "machine": "m2", "cid": "c5",
                    "summary": "still over"})
        assert len(pings) == before + 1, pings              # notified ONCE
    check("hourly budget hard-stops turns; operator notified once", t_budget)

    def t_digest():
        r1 = ap.escalate({"question": "merge to main?", "machine": "m1",
                          "cid": "c1", "urgency": "digest"})
        r2 = ap.escalate({"question": "postgres or sqlite?", "machine": "m1",
                          "cid": "c2", "urgency": "digest",
                          "url": "https://h.atg.link/#/x"})
        assert r1["ok"] and r2["queued"] == 2, (r1, r2)
        before = len(pings)
        n = ap.flush_digest()
        assert n == 2 and len(pings) == before + 1, (n, pings)
        assert "merge to main?" in pings[-1] and "postgres or sqlite?" in pings[-1]
        assert "2 item(s)" in pings[-1], pings[-1]
        assert ap.flush_digest() == 0                       # empty → no push
    check("escalations batch into ONE digest push", t_digest)

    def t_now():
        before = len(pings)
        ap.escalate({"question": "disk full on m2!", "machine": "m2",
                     "urgency": "now"})
        assert len(pings) == before + 1 and "disk full" in pings[-1], pings
    check("urgency='now' bypasses the digest", t_now)

    def t_kill_switch():
        ap.set_enabled(False)
        with open(toggle) as f:
            assert f.read().strip() == "0"
        n = len(turns)
        ap._last_turn_for.clear()
        ap._turns.clear()                                   # budget refilled
        ap._handle({"kind": "blocked", "machine": "m1", "cid": "c9",
                    "summary": "should be ignored"})
        assert len(turns) == n, turns
        # a fresh autopilot honors the persisted toggle over its default
        ap2 = Autopilot(run_pm, None, ledger, guard, enabled=True,
                        toggle_path=toggle)
        assert ap2.enabled is False
        ap.set_enabled(True)
    check("kill switch stops turns and persists across restarts", t_kill_switch)

    def t_readonly():
        guard.autonomy = "readonly"
        n = len(turns)
        ap._handle({"kind": "blocked", "machine": "m1", "cid": "c10",
                    "summary": "paused"})
        assert len(turns) == n and ap.status()["paused"] == "autonomy=readonly"
        guard.autonomy = "auto"
    check("autonomy=readonly pauses the autopilot", t_readonly)

    def t_notes():
        notes = NotesStore(tempfile.mktemp(suffix=".json"))
        assert notes.remember("machine:m1", "worker needs manual restarts")["ok"]
        assert notes.set_priorities(["ship slop-computer", "fleet health"])["ok"]
        text = notes.render()
        assert "1. ship slop-computer" in text and "[machine:m1]" in text, text
        # bounded render: flood it and check the cap holds
        for i in range(80):
            notes.remember("general", f"note {i} " + "x" * 250)
        assert len(notes.render()) <= 2400, len(notes.render())
        # forget round-trip + reload from disk
        r = notes.forget("machine:m1", 0)
        assert r["ok"], r
        again = NotesStore(notes.path)
        assert "machine:m1" not in again.dump()["notes"]
        assert again.dump()["priorities"][0] == "ship slop-computer"
    check("notes: remember/priorities/forget round-trip; render bounded", t_notes)

    print()
    if failures:
        print(f"FAILED: {failures}")
        return 1
    print("PASSED: autopilot + notes (the middle-manager loop)")
    return 0


if __name__ == "__main__":
    rc = main()
    sys.stdout.flush()
    os._exit(rc)
