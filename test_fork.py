#!/usr/bin/env python3
"""test_fork.py — ⑂ fork: a LIVE session continued in a NEW tab, the source untouched.

The mechanism is claude's own `--resume <id> --fork-session` (verified
2026-09-06 in an isolated run: a new session id + a new transcript file, the
source file untouched, SessionStart fires with source="fork" and the new id,
and the fork answers from the source's context). Guards, on a sandboxed copy
of server.py and fakes — never the live daemon:
  1. argv carries --fork-session only for a resuming fork (never a fresh
     spawn, never a plain resume);
  2. `fork` is a ctor param + registry field, so a restart in the window
     before claude's first hook re-forks instead of resuming the SOURCE;
     clone_for_respawn keeps it;
  3. the first id rotation (the fork's own id) clears the flag — from then on
     the fork resumes plainly;
  4. manager.fork refuses: unknown cid, codex, a sign-in ceremony, a session
     with no conversation yet — with a reason, and spawns nothing;
  5. manager.fork spawns via create_session(resume=<source id>, fork=True) in
     the source's project, seeds the title with ⑂, describes the fork — and
     says so when create_session had to fall back to a fresh session.
Exits non-zero on any failure.
"""
import sys, types, shutil, tempfile   # (one line: the gitleaks bip39 rule reads a column of imports as a mnemonic)
import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parent
FAILS = []


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + (f"  — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def load_server_sandboxed():
    tmp = Path(tempfile.mkdtemp(prefix="fork-test-"))
    shutil.copy(REPO / "server.py", tmp / "server.py")
    spec = importlib.util.spec_from_file_location("server_sandbox_fork", tmp / "server.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["server_sandbox_fork"] = mod
    spec.loader.exec_module(mod)     # HERE = tmp dir → empty registry, inert
    return mod


srv = load_server_sandboxed()
eng = srv.ENGINES["claude"]


def fake_s(**kw):
    d = dict(resuming=False, fork=False, session_id="sid-1", settings_path="/dev/null",
             standing_rule=lambda: "", cid="c1")
    d.update(kw)
    return types.SimpleNamespace(**d)


# 1. argv
check("fresh spawn: no --fork-session", "--fork-session" not in eng.argv(fake_s()))
check("plain resume: no --fork-session", "--fork-session" not in eng.argv(fake_s(resuming=True)))
a = eng.argv(fake_s(resuming=True, fork=True))
check("resuming fork: --resume <id> --fork-session",
      "--fork-session" in a and a[1:3] == ["--resume", "sid-1"], str(a))
check("fork flag without resuming is inert", "--fork-session" not in eng.argv(fake_s(fork=True)))
check("engine capability: claude forks, codex doesn't",
      srv.ENGINES["claude"].forks and not srv.ENGINES["codex"].forks)

# 2. persistence
mgr = types.SimpleNamespace(save_registry=lambda: None)
s = srv.ClaudeSession(mgr, cid="c1", session_id="src-id", resuming=True, pid="p1", fork=True)
check("ctor stores fork", s.fork is True)
check("registry row carries fork", s.to_registry().get("fork") is True)
check("default is not a fork", srv.ClaudeSession(mgr, cid="c2", session_id="x", resuming=False).fork is False)
check("clone_for_respawn keeps fork", s.clone_for_respawn().fork is True)

# 3. rotation clears it
s._follow_session({"session_id": "fork-own-id", "transcript_path": "/tmp/x.jsonl"})
check("first rotation records the fork's own id", s.session_id == "fork-own-id")
check("…and clears the fork flag", s.fork is False)
check("…so the registry now resumes plainly", s.to_registry().get("fork") is False)


# 4/5. manager.fork
class FakeMgr:
    fork = srv.SessionManager.fork

    def __init__(self):
        self.sessions = {}
        self.calls = []
        self.spawn = None          # what create_session returns
        self.broadcasts = 0

    def get(self, cid):
        return self.sessions.get(cid)

    def create_session(self, pid, account=None, ceremony=False, engine="claude",
                       resume="", title="", fork=False):
        self.calls.append(dict(pid=pid, engine=engine, resume=resume, title=title, fork=fork))
        return self.spawn

    def broadcast_sessions(self):
        self.broadcasts += 1


def src(cid, engine="claude", ceremony=False, convo=True, title="Rename the widget"):
    return types.SimpleNamespace(cid=cid, pid="p1", engine=engine, ceremony=ceremony,
                                 session_id="src-" + cid, title=title,
                                 _fallback_title=lambda: "new session",
                                 _has_conversation=lambda: convo)


m = FakeMgr()
m.sessions["a"] = src("a")
m.sessions["cx"] = src("cx", engine="codex")
m.sessions["cer"] = src("cer", ceremony=True)
m.sessions["empty"] = src("empty", convo=False)
for cid, why in [("nope", "no such"), ("cx", "codex"), ("cer", "sign-in"), ("empty", "nothing to fork")]:
    got, reason = m.fork(cid)
    check(f"refuses {cid!r}: {why}", got is None and why in reason and not m.calls, reason)

m.spawn = types.SimpleNamespace(cid="new1", resuming=True, desc="")
got, reason = m.fork("a")
c = m.calls[-1]
check("spawns via create_session(resume=<source id>, fork=True)",
      got is m.spawn and reason == "" and c["resume"] == "src-a" and c["fork"] is True
      and c["pid"] == "p1" and c["engine"] == "claude", str(c))
check("title seeded with ⑂ + the source title", c["title"] == "⑂ Rename the widget", c["title"])
check("desc names the source", got.desc == "forked from Rename the widget", got.desc)
check("broadcasts sessions", m.broadcasts == 1)

m.spawn = types.SimpleNamespace(cid="new2", resuming=False, desc="")
got, reason = m.fork("a")
check("fresh fallback is labelled, not passed off as a fork",
      got is m.spawn and "fork failed" in got.desc, got.desc)

m.spawn = None
got, reason = m.fork("a")
check("no session from create_session → refused with a reason", got is None and reason)

if FAILS:
    print(f"\n{len(FAILS)} FAILED: {FAILS}")
    sys.exit(1)
print("\nall ok")
