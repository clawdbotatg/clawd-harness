#!/usr/bin/env python3
"""A fresh codex counts as ready when its title spinner stops.

Why this exists: codex 0.160 fires SessionStart only with the FIRST prompt, so
wait_ready burned its whole 30s on every new codex session. The 🔍 reviewer's
tab sat blank; when Austin typed "." in that window, the brief typed behind it
was queued until his turn ended (2026-10-02). Codex spins a braille glyph in
the window title (OSC 0) while it starts up and drops it ~1.6s in, when it
takes input — that flip now sets _started_evt.

Bytes below are shaped like a real 0.160 capture. Stand-in session; spawns
nothing.

    python3 test_codex_ready.py
"""
import sys
import threading
import types

import server

FAILED = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def fake():
    return types.SimpleNamespace(cid="deadbeef-cafe", engine="codex", _title_raw=b"",
                                 _title_spun=False, _started_evt=threading.Event())


def feed(s, data, chunk=None):
    scan = server.ClaudeSession._scan_for_title_ready
    step = chunk or len(data)
    for i in range(0, len(data), step):
        scan(s, data[i:i + step])


PLAIN = b"\x1b]0;cx\x07"
SPIN = ["\x1b]0;⠙ cx\x07".encode(), "\x1b]0;⠹ cx\x07".encode()]
STARTUP = (b"\x1b[?2004h" + PLAIN + b"\x1b[?2026h\x1b[1;1H\x1b[J" + SPIN[0]
           + b"\x1b[11;3H" + SPIN[1] + b"\x1b[?25h" + PLAIN + b"\x1b[?2026l")


def main():
    check("codex opts in, claude doesn't",
          server.CodexEngine.ready_on_title_spin and not server.ClaudeEngine.ready_on_title_spin)

    s = fake(); feed(s, PLAIN + b"hello")
    check("a plain title before any spin is not ready", not s._started_evt.is_set())

    s = fake(); feed(s, PLAIN + SPIN[0])
    check("still spinning is not ready", not s._started_evt.is_set())

    s = fake(); feed(s, STARTUP)
    check("spin then plain = ready", s._started_evt.is_set())

    s = fake(); feed(s, STARTUP, chunk=1)
    check("ready even when every byte is its own read", s._started_evt.is_set())

    s = fake(); feed(s, STARTUP.replace(b"\x07", b"\x1b\\"), chunk=3)
    check("ST-terminated titles work too", s._started_evt.is_set())

    s = fake(); feed(s, b"x" * 10000 + SPIN[0] + b"y" * 10000 + PLAIN)
    check("buffer stays bounded", s._started_evt.is_set() and len(s._title_raw) <= 4096)

    s = fake(); feed(s, "the glyph ⠙ in screen text".encode() + PLAIN)
    check("a spinner glyph outside a title is ignored", not s._started_evt.is_set())

    print("FAIL" if FAILED else "all ok")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
