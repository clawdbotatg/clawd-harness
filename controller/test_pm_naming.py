#!/usr/bin/env python3
"""PM thread AI naming — the chat analog of session naming (title + running tldr).

Pins the contract without any live gateway:
  - name_at_prompt cadence matches sessions: 1, then every 3
  - transcript_tail flattens you:/pm: turns, newest kept
  - Threads.set_name updates title+desc, locks the title, survives persist/reload
  - clear() blanks the tldr with the rest of the thread
  - an unconfigured gateway means generate_thread_name degrades to (None, None)

Run:  python3 -m controller.test_pm_naming
"""
import tempfile

from . import naming
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

    def cadence():
        fires = [n for n in range(1, 13) if naming.name_at_prompt(n)]
        assert fires == [1, 3, 6, 9, 12], fires

    def tail():
        msgs = [{"who": "me", "text": "fix the relay"},
                {"who": "bot", "text": "on it\nspawning a session"},
                {"who": "me", "text": ""}]                    # empty turns dropped
        t = naming.transcript_tail(msgs)
        assert t == "you: fix the relay\npm: on it spawning a session", repr(t)

    def set_name_roundtrip():
        path = tempfile.mktemp(suffix=".json")
        th = Threads(path)
        tid = th.current
        th.record("me", "please rework the passkey flow for the whole fleet now")
        assert th.set_name(tid, title="passkey rework", desc="auditing the relay ceremony")
        row = th.summary()["threads"][0]
        assert row["title"] == "passkey rework" and row["desc"] == "auditing the relay ceremony", row
        th.record("me", "unrelated later question")           # locked title must hold
        assert th.get(tid)["title"] == "passkey rework"
        th2 = Threads(path)                                   # desc survives a restart
        assert th2.get(tid)["desc"] == "auditing the relay ceremony"

    def clear_blanks_desc():
        th = Threads(tempfile.mktemp(suffix=".json"))
        tid = th.current
        th.set_name(tid, title="t", desc="d")
        th.clear(tid)
        assert th.get(tid)["desc"] == "" and th.get(tid)["title"] == "New thread"

    def unconfigured_degrades():
        old = (naming.BANKR_API_KEY, naming.BANKR_BASE_URL)
        naming.BANKR_API_KEY, naming.BANKR_BASE_URL = "", ""
        try:
            assert not naming.configured()
            assert naming.generate_thread_name("you: hi") == (None, None)
        finally:
            naming.BANKR_API_KEY, naming.BANKR_BASE_URL = old

    check("cadence: 1 then every 3", cadence)
    check("transcript tail flattens turns", tail)
    check("set_name: title+desc, locked, persisted", set_name_roundtrip)
    check("clear blanks the tldr", clear_blanks_desc)
    check("unconfigured gateway degrades to None", unconfigured_degrades)

    if failures:
        raise SystemExit(f"FAILED: {failures}")
    print("PM thread naming: all checks passed")


if __name__ == "__main__":
    main()
