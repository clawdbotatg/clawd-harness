#!/usr/bin/env python3
"""test_wrap.py — 📑 wrap: a session documents its handoff, then closes ITSELF.

The one rule: a session can only self-close while a human armed it (the 📑
chip / the PM's `wrap` verb), for WRAP_TTL_S seconds and WRAP_TURNS turns.
Nothing the session does can arm it, and the harness never force-closes.
Guards (server side, on fakes — never the live daemon):
  1. unarmed `harness-close` → 403 and the session is untouched;
  2. armed → 200, deferred: the NEXT Stop closes it with reason "wrapped",
     and the grace timer closes it if no Stop ever comes;
  3. gates: sign-in ceremony, autopilot → 409; a dirty git worktree (untracked
     counts) still closes, its porcelain lines named in the reply (6b5aec1);
  4. the arm lapses: WRAP_TURNS Stops without a call, or the TTL → disarmed,
     session alive; wrapCancel disarms and defuses an accepted close;
  5. manager.wrap arms + delivers the prompt (default WRAP_PROMPT, or the
     chip's text) and refuses ceremony/autopilot; meta carries wrapArmed;
  6. the real HTTP endpoint + bin/harness-close: exit 2 without the env, 1 on
     a refusal, 0 on accept — the message reaches stdout either way;
  7. a wrapped session's history row carries its TLDR (last_answer).
Exits non-zero on any failure.
"""
import http.server
import json
import re
import os
import subprocess
import sys
import tempfile
import threading
import time
import types
import urllib.request
import urllib.error

import server

FAILS = []
HERE = os.path.dirname(os.path.abspath(__file__))


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + (f"  — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


TMP = tempfile.mkdtemp(prefix="wraptest-")
server.PROMPTS_LOG = type(server.PROMPTS_LOG)(os.path.join(TMP, "prompts.jsonl"))  # never the real log
server.SUB_ROUTE_ON_PROMPT = False
server.WRAP_GRACE_S = 0.3


class FakeMgr:
    wrap = server.SessionManager.wrap
    wrap_cancel = server.SessionManager.wrap_cancel
    _closed_row = server.SessionManager._closed_row

    def __init__(self):
        self.sessions = {}
        self.projects = {"p1": types.SimpleNamespace(name="proj")}
        self.closed_calls = []
        self.sent = []
        self.broadcasts = 0

    def close(self, cid, reason="closed", _broadcast=True):
        s = self.sessions.pop(cid, None)
        self.closed_calls.append((cid, reason, s.last_answer if s else None))

    def send_prompt(self, cid, text, via="", control=False):
        self.sent.append((cid, text, via))
        return True

    def broadcast_sessions(self):
        self.broadcasts += 1

    def get(self, cid):
        return self.sessions.get(cid)


class FakeSession:
    wrap_armed = server.ClaudeSession.wrap_armed
    wrap_arm = server.ClaudeSession.wrap_arm
    wrap_cancel = server.ClaudeSession.wrap_cancel
    self_close_request = server.ClaudeSession.self_close_request
    _wrap_close = server.ClaudeSession._wrap_close
    _wrap_on_stop = server.ClaudeSession._wrap_on_stop

    def __init__(self, mgr, cid, workdir="", ceremony=False, autopilot=0.0):
        self.manager, self.cid = mgr, cid
        self.engine, self.pid, self.session_id = "claude", "p1", "sid-" + cid
        self.title, self.desc, self.account = "T " + cid, "", "ef"
        self.prompt_count, self.first_prompt = 3, "hi"
        self.created = self.last_active = 1.0
        self.ceremony, self.autopilot = ceremony, autopilot
        self.last_answer, self.auto_tldr_armed = "", True
        self.wrap_armed_at, self.wrap_turns_left, self.wrap_closing = 0.0, 0, False
        self._wrap_timer = None
        self._wd = workdir
        mgr.sessions[cid] = self

    def workdir(self):
        return self._wd

    def _fallback_title(self):
        return "fb"


def wait_for(pred, secs=2.0):
    end = time.time() + secs
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


# --- 1. unarmed → 403, untouched ---------------------------------------------
m = FakeMgr()
s = FakeSession(m, "a")
code, msg = s.self_close_request("done")
check("unarmed harness-close is refused (403)", code == 403 and "not armed" in msg, f"{code} {msg}")
check("…and the session is untouched", "a" in m.sessions and not s.wrap_closing and not m.closed_calls)

# --- 2. armed → accepted, closes on the next Stop with reason wrapped ---------
s.wrap_arm()
check("arm → wrapArmed", s.wrap_armed() and s.wrap_turns_left == server.WRAP_TURNS)
code, msg = s.self_close_request("handoff written")
check("armed harness-close accepted (200), deferred", code == 200 and s.wrap_closing and "a" in m.sessions, f"{code} {msg}")
code2, msg2 = s.self_close_request()
check("second call while closing → 200 'already'", code2 == 200 and "already" in msg2)
s.last_answer = "TLDR: shipped the thing"
s._wrap_on_stop()
check("the Stop closes it: reason wrapped, TLDR captured",
      wait_for(lambda: m.closed_calls == [("a", "wrapped", "TLDR: shipped the thing")]), str(m.closed_calls))
check("grace timer defused after the Stop close", s._wrap_timer is None)

# grace: accepted, no Stop ever → closes anyway
m2 = FakeMgr()
g = FakeSession(m2, "g")
g.wrap_arm()
g.self_close_request()
check("no Stop → the grace timer closes it",
      wait_for(lambda: m2.closed_calls and m2.closed_calls[0][:2] == ("g", "wrapped"), 2.0), str(m2.closed_calls))

# --- 3. gates ------------------------------------------------------------------
m3 = FakeMgr()
c = FakeSession(m3, "c", ceremony=True); c.wrap_arm()
check("ceremony can't self-close (409)", c.self_close_request()[0] == 409)
ap = FakeSession(m3, "ap", autopilot=1.0); ap.wrap_arm()
check("autopilot can't self-close (409)", ap.self_close_request()[0] == 409)
repo = os.path.join(TMP, "repo"); os.makedirs(repo)
subprocess.run(["git", "init", "-q", repo], check=True)
subprocess.run(["git", "-C", repo, "config", "user.email", "t@t"], check=True)
subprocess.run(["git", "-C", repo, "config", "user.name", "t"], check=True)
open(os.path.join(repo, "notes.md"), "w").write("x")
check("_worktree_dirty: untracked file counts", "?? notes.md" in server._worktree_dirty(repo))
check("_worktree_dirty: not a repo → ''", server._worktree_dirty(TMP) == "")
# 📑 the handoff itself is LOCAL: an untracked HANDOFF.md never counts as dirty,
# and the arm writes it into .git/info/exclude (never .gitignore) so `git status`
# doesn't even see it — the old prompt said "commit and push it", and the gate's
# 409 on `?? HANDOFF.md` would have pushed a session that did not into doing so.
open(os.path.join(repo, server.HANDOFF_FILE), "w").write("# handoff")
check("_worktree_dirty: untracked HANDOFF.md is tolerated, other untracked still counts",
      "notes.md" in server._worktree_dirty(repo) and "HANDOFF" not in server._worktree_dirty(repo),
      server._worktree_dirty(repo))
check("_exclude_handoff: writes the LOCAL exclude entry", server._exclude_handoff(repo) is True)
excl = open(os.path.join(repo, ".git", "info", "exclude")).read()
check("…as an anchored /HANDOFF.md line", "/HANDOFF.md\n" in excl)
check("…idempotent (second call adds nothing)",
      server._exclude_handoff(repo) is True and open(os.path.join(repo, ".git", "info", "exclude")).read() == excl)
check("…and never touches .gitignore", not os.path.exists(os.path.join(repo, ".gitignore")))
porc = subprocess.run(["git", "-C", repo, "status", "--porcelain"], capture_output=True, text=True).stdout
check("git status no longer lists HANDOFF.md", "HANDOFF" not in porc and "notes.md" in porc, porc)
check("_exclude_handoff: not a repo → False", server._exclude_handoff(TMP) is False)
d = FakeSession(m3, "d", workdir=repo); d.wrap_arm()
code, msg = d.self_close_request()
# 6b5aec1: a dirty tree no longer blocks the close (Austin, 09-11 — local notes
# live in the tree on purpose); the porcelain lines ride in the reply instead.
check("dirty tree → accepted, the porcelain lines in the reply",
      code == 200 and d.wrap_closing and "uncommitted" in msg and "notes.md" in msg, f"{code} {msg}")
d.wrap_cancel()                       # defuse before the 0.3 s grace fires; re-arm for the clean case
check("…defused, still alive", not d.wrap_closing and "d" in m3.sessions)
subprocess.run(["git", "-C", repo, "add", "-A"], check=True)
subprocess.run(["git", "-C", repo, "commit", "-qm", "handoff"], check=True)
check("_worktree_dirty: clean after commit", server._worktree_dirty(repo) == "")
d.wrap_arm()
code, msg = d.self_close_request()
check("clean tree → accepted, no uncommitted note", code == 200 and d.wrap_closing and "uncommitted" not in msg, f"{code} {msg}")
d.wrap_cancel()
check("wrapCancel defuses an accepted close", not d.wrap_closing and d._wrap_timer is None)
time.sleep(0.5)
check("…and the defused close never fires", "d" in m3.sessions and not m3.closed_calls)

# --- 4. the arm lapses ---------------------------------------------------------
m4 = FakeMgr()
l = FakeSession(m4, "l"); l.wrap_arm()
for _ in range(server.WRAP_TURNS):
    l._wrap_on_stop()
check("WRAP_TURNS Stops without a call → disarmed, alive", not l.wrap_armed() and "l" in m4.sessions)
check("…then harness-close is refused", l.self_close_request()[0] == 403)
l.wrap_arm(); l.wrap_armed_at = time.time() - server.WRAP_TTL_S - 1
check("TTL passed → not armed (403)", not l.wrap_armed() and l.self_close_request()[0] == 403)

# --- 5. manager.wrap -----------------------------------------------------------
m5 = FakeMgr()
w_repo = os.path.join(TMP, "wrepo"); os.makedirs(w_repo)
subprocess.run(["git", "init", "-q", w_repo], check=True)
w = FakeSession(m5, "w", workdir=w_repo)
err = m5.wrap("w")
check("manager.wrap arms + delivers WRAP_PROMPT via 'wrap'",
      err == "" and w.wrap_armed() and m5.sent == [("w", server.WRAP_PROMPT, "wrap")] and m5.broadcasts == 1, f"{err} {m5.sent}")
check("…and the AUTO_TLDR arm is dropped (the reply carries its own TLDR)", w.auto_tldr_armed is False)
m5.wrap("w", text="custom words")
check("chip text wins over the default", m5.sent[-1][1] == "custom words")
check("wrap refuses an unknown cid", m5.wrap("zz") == "no such session")
cc = FakeSession(m5, "cc", ceremony=True)
check("wrap refuses a ceremony", "sign-in" in m5.wrap("cc") and not cc.wrap_armed())
aa = FakeSession(m5, "aa", autopilot=1.0)
check("wrap refuses autopilot", "autopilot" in m5.wrap("aa") and not aa.wrap_armed())
m5.wrap_cancel("w")
check("manager.wrap_cancel disarms + broadcasts", not w.wrap_armed() and m5.broadcasts == 3)
check("prompt log got the wrap sends", open(server.PROMPTS_LOG).read().count('"via": "wrap"') == 2)
check("WRAP_PROMPT names the command and the no-close rule",
      "harness-close" in server.WRAP_PROMPT and "do NOT close" in server.WRAP_PROMPT)
check("WRAP_PROMPT: the handoff is HANDOFF.md, local, never committed/pushed/gitignored",
      "HANDOFF.md" in server.WRAP_PROMPT and "do NOT commit it" in server.WRAP_PROMPT
      and "do NOT push it" in server.WRAP_PROMPT and ".gitignore" in server.WRAP_PROMPT
      and "commit and push it" not in server.WRAP_PROMPT)
# index.html's 📑 chip sends its OWN copy of the prompt (chip text wins over the
# server default), so the two must be byte-identical or the UI ships stale words.
_html = open(os.path.join(HERE, "index.html"), encoding="utf-8").read()
_m = re.search(r"label: 'doc', wrap: true,\n\s+tip: [^\n]*\n\s+text: (\"(?:[^\"\\\\]|\\\\.)*\")", _html)
check("index.html 📑 chip text == server.WRAP_PROMPT (single source of truth)",
      bool(_m) and json.loads(_m.group(1)) == server.WRAP_PROMPT,
      (json.loads(_m.group(1))[:80] if _m else "chip not found"))
check("index.html 📑 chip tip says local / never committed, not 'commit it'",
      "never committed" in _html.split("label: 'doc'")[1].split("\n")[1] and "commit it," not in _html.split("label: 'doc'")[1].split("\n")[1])
check("manager.wrap wrote the handoff exclude into the session's checkout",
      "/HANDOFF.md\n" in open(os.path.join(w_repo, ".git", "info", "exclude")).read())

# --- 6. the HTTP endpoint + bin/harness-close --------------------------------
m6 = FakeMgr()
h = FakeSession(m6, "h")
server.MGR = m6
server.AUTH_REQUIRED = False            # loopback bind ⇒ no token check (as in production on a loopback bind)
httpd = http.server.HTTPServer(("127.0.0.1", 0), server.Handler)
httpd.timeout = 1
threading.Thread(target=httpd.serve_forever, daemon=True).start()
port = httpd.server_address[1]
base = f"http://127.0.0.1:{port}/self/close?t=x&cid="


def post(cid, reason=""):
    try:
        r = urllib.request.urlopen(urllib.request.Request(
            base + cid, data=("reason=" + reason).encode(), method="POST"), timeout=3)
        return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


code, body = post("nope")
check("HTTP: unknown cid → 404", code == 404 and "no such session" in body, f"{code} {body}")
code, body = post("h")
check("HTTP: unarmed → 403 with the sentence", code == 403 and "not armed" in body, f"{code} {body}")
script = os.path.join(HERE, "bin", "harness-close")
r = subprocess.run([script], capture_output=True, text=True, env={"PATH": os.environ["PATH"]})
check("script without HARNESS_CLOSE_URL → exit 2", r.returncode == 2 and "not running under the harness" in r.stderr, r.stderr)
env = {"PATH": os.environ["PATH"], "HARNESS_CLOSE_URL": base + "h"}
r = subprocess.run([script, "all", "done"], capture_output=True, text=True, env=env)
check("script unarmed → exit 1, refusal on stdout", r.returncode == 1 and "not armed" in r.stdout, f"{r.returncode} {r.stdout} {r.stderr}")
h.wrap_arm()
r = subprocess.run([script, "handoff", "pushed"], capture_output=True, text=True, env=env)
check("script armed → exit 0, 'closing when this turn ends'", r.returncode == 0 and "closing when this turn ends" in r.stdout and h.wrap_closing, f"{r.returncode} {r.stdout} {r.stderr}")
httpd.shutdown()
h.wrap_cancel()

# --- 7. history row carries the TLDR -------------------------------------------
h.last_answer = "TLDR: all pushed\nnext: nothing"
row = m6._closed_row(h, "wrapped")
check("closed row: reason wrapped + last_answer", row["reason"] == "wrapped" and row["last_answer"].startswith("TLDR: all pushed"))

print()
if FAILS:
    print(f"FAILED ({len(FAILS)}): " + ", ".join(FAILS))
    sys.exit(1)
print("all wrap guards pass")
