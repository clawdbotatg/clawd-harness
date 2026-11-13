#!/usr/bin/env python3
"""Three small server fixes from the 2026-11 audit (S8):

  1. the login-expired tripwire ignores transcript lines from before this
     launch (a restart re-reads the file from the top);
  2. a linked git worktree / submodule (.git is a FILE) counts as a repo;
  3. two handoffs racing on one session start exactly one new claude.

No SessionManager is constructed (it would resume live sessions).
"""
import datetime
import json
import os
import subprocess
import tempfile
import threading
import time
import types

import server

FAILED = []


def check(label, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + label
          + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(label)


NOW = time.time()
TMP = tempfile.mkdtemp(prefix="small-server-")


def iso(seconds):
    return datetime.datetime.fromtimestamp(
        NOW + seconds, datetime.timezone.utc).isoformat()


# ------------------------------------------------------------------ 1
print("login-expired tripwire:")


def synth(ts):
    return json.dumps({"type": "assistant", "timestamp": ts, "message": {
        "model": "<synthetic>", "role": "assistant",
        "content": [{"type": "text", "text": "Login expired · Please run /login"}]}})


check("the test line matches the tripwire", server._LOGIN_EXPIRED_RE.search(synth(iso(0))))
path = os.path.join(TMP, "t.jsonl")
with open(path, "w") as f:
    f.write(synth(iso(-7200)) + "\n")
s = server.ClaudeSession.__new__(server.ClaudeSession)
s.cid, s.engine, s.alive = "small-server-test", "claude", True
s.transcript_path, s._live_transcript, s._launched_at = None, path, NOW
s.model, s.ctx_tokens, s.ctx_window = "", 0, 0
fired = []
s._to_subscribers_json = lambda m: None
s._on_login_expired = lambda: fired.append("login")
s._on_limit_transcript = lambda raw: None
th = threading.Thread(target=s._tail_transcript, daemon=True)
th.start()
time.sleep(0.7)
check("a line from before this launch does not fire", fired == [], f"{fired}")
with open(path, "a") as f:
    f.write(synth(iso(1)) + "\n")
deadline = time.time() + 5
while not fired and time.time() < deadline:
    time.sleep(0.05)
check("a line from this launch does", fired == ["login"], f"{fired}")
s.alive = False
th.join(2)

# ------------------------------------------------------------------ 2
print("\ngit worktrees are repos:")


def git(*a, cwd):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a],
                   cwd=cwd, check=True, capture_output=True)


main = os.path.join(TMP, "main")
wt = os.path.join(TMP, "wt")
os.mkdir(main)
git("init", "-q", cwd=main)
open(os.path.join(main, "a"), "w").write("1")
git("add", "a", cwd=main)
git("commit", "-qm", "one", cwd=main)
git("worktree", "add", "-q", wt, cwd=main)
check(".git is a file there", os.path.isfile(os.path.join(wt, ".git")))
check("_git_head reads it", len(server._git_head(wt)) == 40, repr(server._git_head(wt)))
open(os.path.join(wt, "new.txt"), "w").write("x")
check("_worktree_dirty sees an untracked file", "new.txt" in server._worktree_dirty(wt))
check("_exclude_handoff works", server._exclude_handoff(wt) is True)
open(os.path.join(wt, "HANDOFF-x.md"), "w").write("x")
out = subprocess.run(["git", "status", "--porcelain"], cwd=wt, capture_output=True, text=True).stdout
check("…and the handoff is hidden from git status", "HANDOFF" not in out, out)
check("a plain folder is still not a repo",
      server._git_head(TMP) == "" and server._worktree_dirty(TMP) == "")

# ------------------------------------------------------------------ 3
print("\nracing handoffs:")
started, killed = [], []


class Fake:
    def __init__(self, n):
        self.cid, self.n = "c", n
        self.busy, self.alive, self.ceremony = False, True, False
        self.transcript_path, self.session_id = None, "sid"
        self.config_dir, self.account, self.last_handoff = "/tmp/a", "a", 0.0
    def _find_transcript(self): return None
    def _has_conversation(self): return False
    def clone_for_respawn(self, **kw):
        time.sleep(0.2)                  # both racers are in here together
        return Fake(self.n + 1)
    def kill(self): killed.append(self.n)
    def start(self): started.append(self.n)
    def adopt_viewers(self, old): pass


old = Fake(0)
m = types.SimpleNamespace(lock=threading.RLock(), sessions={"c": old},
                          save_registry=lambda: None, broadcast_sessions=lambda: None)
target = types.SimpleNamespace(name="b", config_dir="/tmp/b")
hand = server.SessionManager._handoff.__get__(m)
ts = [threading.Thread(target=hand, args=(old, target), kwargs={"fresh_if_empty": True})
      for _ in range(2)]
for t in ts:
    t.start()
for t in ts:
    t.join()
check("exactly one new claude is started", len(started) == 1, f"started={started}")
check("the old one is killed once", killed == [0], f"killed={killed}")
check("the tracked session is the one that started",
      m.sessions["c"].n == 1 and m.sessions["c"] is not old)

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {FAILED}")
    raise SystemExit(1)
print("all green")
