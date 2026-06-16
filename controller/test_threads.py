#!/usr/bin/env python3
"""Unit test for the PM thread store (multiple conversations).

Asserts the per-project-sessions analog for the chat: spawn threads, keep their
histories isolated, switch between them, clear (wipe context) and archive (hide,
restorable), and that all of it round-trips through disk so a daemon restart
keeps your conversations.

Run:  python3 -m controller.test_threads
"""
import os
import tempfile

from .threads import Threads


def main():
    failures = []

    def check(name, fn):
        try:
            fn()
            print(f"  ✓ {name}")
        except Exception as e:
            print(f"  ✗ {name}: {e}")
            failures.append(name)

    path = tempfile.mktemp(suffix=".threads.json")

    def t_starts_with_one():
        th = Threads(path)
        assert len(th.live()) == 1 and th.current, th.summary()
    check("boots with one live thread", t_starts_with_one)

    def t_isolated_histories():
        th = Threads(path)
        a = th.current
        th.record("me", "work on alpha")
        th.save_state("bankr", {"history": [{"role": "user", "content": "alpha"}]})
        b = th.new()                          # new thread, auto-selected
        assert th.current == b and b != a
        th.record("me", "work on beta")
        th.save_state("bankr", {"history": [{"role": "user", "content": "beta"}]})
        # each thread sees only its own state + transcript
        assert th.state_for("bankr", a)["history"][0]["content"] == "alpha"
        assert th.state_for("bankr", b)["history"][0]["content"] == "beta"
        assert th.messages(a)[0]["text"] == "work on alpha"
        assert th.messages(b)[0]["text"] == "work on beta"
        # title locked from the first user message
        assert th.get(a)["title"] == "work on alpha", th.get(a)["title"]
    check("threads keep isolated history + transcript + titles", t_isolated_histories)

    def t_switch():
        th = Threads(path)
        live = th.live()
        assert th.select(live[0]) and th.current == live[0]
        assert th.select(live[1]) and th.current == live[1]
        assert th.select("nope") is False
    check("switching between threads", t_switch)

    def t_clear():
        th = Threads(path)
        th.select(th.live()[0])
        cur = th.current
        assert th.messages(cur)                # has content
        th.clear()
        assert th.messages(cur) == [] and th.state_for("bankr", cur) == {}
        assert th.get(cur)["title"] == "New thread"
    check("clear wipes a thread's context but keeps the slot", t_clear)

    def t_archive_moves_current():
        th = Threads(path)
        before = set(th.live())
        cur = th.current
        th.archive()                           # archive current
        assert cur not in set(th.live())       # hidden from live
        assert th.current != cur and th.current in set(th.live())  # moved to another
        assert th.summary()["archived_count"] >= 1
        # selecting an archived thread restores it
        th.select(cur)
        assert cur in set(th.live()) and th.current == cur
    check("archive hides current + moves on; select restores", t_archive_moves_current)

    def t_persist_roundtrip():
        th = Threads(path)
        th.clear()
        tid = th.new(title="persisted")
        th.record("me", "remember me")
        th.save_state("claude-code", {"history": [], "session_id": "sess-123"})
        th.persist()                           # caller persists after a turn (Router.chat)
        cur = th.current
        # reopen from disk → same threads, current, history
        th2 = Threads(path)
        assert th2.current == cur
        assert th2.get(tid)["title"] == "persisted"
        assert th2.messages(tid)[0]["text"] == "remember me"
        assert th2.state_for("claude-code", tid)["session_id"] == "sess-123"
        # ids keep climbing — no collision after restart
        new_id = th2.new()
        assert new_id not in {tid}
    check("threads round-trip through disk (survive restart)", t_persist_roundtrip)

    try:
        os.remove(path)
    except OSError:
        pass

    print()
    if failures:
        print(f"FAILED: {failures}")
        return 1
    print("PASSED: PM thread store")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
