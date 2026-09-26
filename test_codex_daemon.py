#!/usr/bin/env python3
"""codex sessions run with --no-daemon when (and only when) codex knows it.

Why this exists: codex 0.157 runs its TUI against a SHARED background
app-server by default. Startup goes async, and a prompt submitted before the
thread is up is queued and held until some later turn ends — the 🔍 reviewer's
brief, typed at SessionStart, sat unsent until Austin typed "test" (clawd-head,
2026-09-26). 0.154 has no such flag and refuses to start if handed one, so the
flag rides only when `codex --help` lists it — re-read when the binary changes,
because codex gets upgraded under a running harness.

Stand-in codex binaries (shell scripts) + a stand-in session; spawns nothing
real.

    python3 test_codex_daemon.py
"""
import os
import sys
import tempfile
import time
import types

import server

FAILED = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def fake_codex(path, help_text):
    with open(path, "w") as f:
        f.write("#!/bin/sh\ncat <<'EOF'\n" + help_text + "\nEOF\n")
    os.chmod(path, 0o755)


def argv(resuming=False):
    s = types.SimpleNamespace(resuming=resuming, session_id="sid-1")
    return server.CodexEngine().argv(s)


def main():
    d = tempfile.mkdtemp()
    bin_ = os.path.join(d, "codex")
    old = server.CODEX_BIN
    server.CODEX_BIN = bin_
    try:
        fake_codex(bin_, "Options:\n      --no-alt-screen\n")
        check("0.154-style codex: no --no-daemon", "--no-daemon" not in argv())

        # Upgrade under a running harness: same path, new bytes, new mtime.
        time.sleep(0.01)
        fake_codex(bin_, "Options:\n      --no-alt-screen\n      --no-daemon\n"
                         "          Run without the shared background server\n")
        os.utime(bin_, (time.time() + 5, time.time() + 5))
        a = argv()
        check("0.157-style codex: --no-daemon after an in-place upgrade", "--no-daemon" in a, a)
        r = argv(resuming=True)
        check("resume carries it too, after the id", r[1:3] == ["resume", "sid-1"]
              and "--no-daemon" in r, r)

        os.remove(bin_)
        check("missing binary reads as unsupported", "--no-daemon" not in argv())
    finally:
        server.CODEX_BIN = old
    if FAILED:
        print(f"{len(FAILED)} FAILED")
        return 1
    print("all ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
