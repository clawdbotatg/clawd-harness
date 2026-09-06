#!/usr/bin/env python3
"""A harness restart no longer launches every claude on the box at once.

Why this exists: 2026-09-06 12:27 on head — 15 sessions `--resume`d in the
same second (load 4-5), and the tab the user opened next painted nothing for
15s+ while its claude cold-started behind them. That black screen was the
"bricked" screenshot.

Pins:
  * load() parks every registry session (starting=True, alive=False) and
    starts them from a background thread most-recently-active first,
    BOOT_STAGGER_S apart
  * a parked session reports alive to viewers (no dead veil, no dead dot)
  * ensure_started() spawns exactly once; a subscribe spawns a parked
    session immediately (a viewer jumps the queue)
  * the stagger thread skips sessions a viewer already started

Sandbox import (HERE = tmp); ClaudeSession.start is stubbed — nothing spawns.
SERVER_PY=/path/to/server.py tests a candidate copy.

    python3 test_boot_stagger.py
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent
SRC = Path(os.environ.get("SERVER_PY") or REPO / "server.py")
fails = 0


def check(name, ok, detail=""):
    global fails
    print(("  ok   " if ok else "  FAIL ") + name + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        fails += 1


class FakeClient:
    def __init__(self): self.frames = []
    def send_json(self, o): self.frames.append(o)
    def send_bytes(self, b): pass


def main():
    tmp = Path(tempfile.mkdtemp(prefix="boot-stagger-"))
    try:
        shutil.copy(SRC, tmp / "server.py")
        spec = importlib.util.spec_from_file_location("server_boot_sandbox", tmp / "server.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["server_boot_sandbox"] = mod
        spec.loader.exec_module(mod)
        pdir = mod.PROJECTS_DIR
        (pdir / "p").mkdir(parents=True)
        now = time.time()
        mod.REGISTRY_FILE.write_text(json.dumps({
            "projects": [{"pid": "p1", "name": "p", "path": str(pdir / "p"),
                          "repo_url": "https://github.com/x/p", "kind": "gh"}],
            "sessions": [
                {"cid": "old-1", "pid": "p1", "engine": "codex", "last_active": now - 3000},
                {"cid": "new-1", "pid": "p1", "engine": "codex", "last_active": now - 10},
                {"cid": "mid-1", "pid": "p1", "engine": "codex", "last_active": now - 1000},
            ],
        }))
        started = []
        mod.ClaudeSession.start = lambda self: (started.append(self.cid), setattr(self, "alive", True))
        mod.ClaudeSession._history_seed_bytes = lambda self: b""
        mod.ClaudeSession._replay_history = lambda self, client, limit=150: None
        mod.BOOT_STAGGER_S = 0.6

        mgr = mod.SessionManager()
        mgr.load()
        time.sleep(0.15)
        check("load() parked all three, started only the most recent so far",
              started == ["new-1"], str(started))
        old = mgr.sessions["old-1"]
        check("a parked session is starting, not alive, but shows alive to viewers",
              old.starting and not old.alive and old.meta()["alive"] is True)

        c = FakeClient()
        old.subscribe(c)
        check("a subscribe starts a parked session immediately (jumps the queue)",
              started == ["new-1", "old-1"] and not old.starting and old.alive, str(started))
        check("hello still goes first", c.frames and c.frames[0].get("type") == "hello")
        check("ensure_started is once-only", old.ensure_started() is False and started.count("old-1") == 1)

        time.sleep(1.6)
        check("the stagger finishes the rest, skipping what a viewer already started",
              started == ["new-1", "old-1", "mid-1"], str(started))
        check("nothing is left parked", not any(s.starting for s in mgr.sessions.values()))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("[boot-stagger] " + ("ALL GREEN" if not fails else f"{fails} FAILED"))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
