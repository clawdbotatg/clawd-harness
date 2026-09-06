#!/usr/bin/env python3
"""A changed server.py restarts the box only once it is COMMITTED and compiles.

Why this exists: the watcher used to arm a full-box self-restart on any mtime
change to server.py — every save by a session working on the harness, half
edits included. Every restart kills and `--resume`s every session on the box.
head's log held 152 boots in three weeks; 08-27 13:01 booted five times in one
minute on a broken edit; 09-06 had four restarts by lunch. The user reads that
churn as "it bricked itself again" (docs/HISTORY.md, 2026-09-06).

Pins (`_RestartGate`):
  * unchanged / reverted-to-running content never arms
  * an uncommitted edit defers (logged once) and is re-checked without an
    mtime move, so the commit itself arms the restart
  * a file that doesn't compile defers, whatever git says
  * once armed for a digest, the same digest never re-arms (cancel is honored)
  * a second commit arms again

Runs against a sandbox copy in a throwaway git repo — never the live tree.
SERVER_PY=/path/to/server.py tests a candidate copy.

    python3 test_restart_gate.py
"""
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent
SRC = Path(os.environ.get("SERVER_PY") or REPO / "server.py")
fails = 0


def check(name, ok, detail=""):
    global fails
    print(("  ok   " if ok else "  FAIL ") + name + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        fails += 1


def git(tmp, *args):
    return subprocess.run(["git", "-C", str(tmp), *args], capture_output=True, text=True)


def main():
    tmp = Path(tempfile.mkdtemp(prefix="restart-gate-"))
    try:
        shutil.copy(SRC, tmp / "server.py")
        git(tmp, "init", "-q")
        git(tmp, "config", "user.email", "t@t"); git(tmp, "config", "user.name", "t")
        git(tmp, "add", "server.py"); git(tmp, "commit", "--no-verify", "-qm", "boot")
        spec = importlib.util.spec_from_file_location("server_gate_sandbox", tmp / "server.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["server_gate_sandbox"] = mod
        spec.loader.exec_module(mod)
        path = tmp / "server.py"
        boot = path.read_bytes()
        gate = mod._RestartGate(path)

        check("no change → no restart", gate.check(False) is None and gate.check(True) is None)

        path.write_bytes(boot + b"\n# a live edit\n")
        check("uncommitted edit → deferred, not armed", gate.check(True) is None and gate.deferred)
        quiet = [gate.check(False) for _ in range(6)]
        check("stays deferred while dirty (re-checked on the tick)", all(r is None for r in quiet) and gate.deferred)

        git(tmp, "commit", "--no-verify", "-qam", "the edit")
        got = [gate.check(False) for _ in range(6)]
        check("the commit arms it — with no mtime move", "server.py changed" in got, str(got))
        check("armed once, then quiet", got.count("server.py changed") == 1 and not gate.deferred)
        check("same content never re-arms (cancel honored)", gate.check(True) is None)

        committed = path.read_bytes()
        path.write_bytes(committed + b"\ndef (broken\n")
        check("syntax error → deferred, never armed", gate.check(True) is None and gate.deferred)
        git(tmp, "add", "server.py"); git(tmp, "commit", "--no-verify", "-qm", "broken commit")
        got = [gate.check(False) for _ in range(6)]
        check("a committed file that doesn't compile still never arms", all(r is None for r in got))

        path.write_bytes(boot)
        check("reverted to the running content → nothing to restart into", gate.check(True) is None and not gate.deferred)

        path.write_bytes(committed + b"\n# second change\n")
        git(tmp, "commit", "--no-verify", "-qam", "second")
        check("a later commit arms again", gate.check(True) == "server.py changed")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("[restart-gate] " + ("ALL GREEN" if not fails else f"{fails} FAILED"))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
