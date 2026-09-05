#!/usr/bin/env python3
"""test_closed_sessions.py — 🗃️ closed-session history + reopen (server side).

The tab ✕ has no confirm; on 2026-09-05 three sessions were lost to it with no
way back. Every close() now files a row (persisted with the registry) and
`reopen` spawns the engine's resume from it. Guards:
  1. a close files a row newest-first with the fields the modal needs;
  2. re-closing the same cid dedups; the list is capped at CLOSED_MAX;
  3. a sign-in ceremony session is never filed;
  4. reopen → create_session(pid, engine, resume=<session_id>, title) and the
     row is dropped; a gone project → None and the row STAYS;
  5. closed_forget drops one row or all; closed_meta is the wire frame.
Exits non-zero on any failure.
"""
import sys
import threading
import types

import server

FAILS = []


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + (f"  — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


class FakeMgr:
    _closed_row = server.SessionManager._closed_row
    _record_closed = server.SessionManager._record_closed
    closed_meta = server.SessionManager.closed_meta
    broadcast_closed = server.SessionManager.broadcast_closed
    reopen = server.SessionManager.reopen
    closed_forget = server.SessionManager.closed_forget

    def __init__(self):
        self.lock = threading.RLock()
        self.closed = []
        self.projects = {"p1": types.SimpleNamespace(name="dvrable")}
        self.saved = 0
        self.broadcasts = []
        self.spawns = []

    def save_registry(self):
        self.saved += 1

    def broadcast_all(self, obj):
        self.broadcasts.append(obj)

    def create_session(self, pid, account=None, ceremony=False, engine="claude", resume="", title=""):
        self.spawns.append({"pid": pid, "engine": engine, "resume": resume, "title": title})
        if pid not in self.projects:
            return None
        return types.SimpleNamespace(cid="new-" + resume[:4], resuming=bool(resume))


def sess(cid, **kw):
    d = dict(cid=cid, engine="claude", pid="p1", session_id="sid-" + cid, title="T " + cid,
             desc="d", account="ef", prompt_count=3, first_prompt="hello", created=1.0,
             last_active=2.0, ceremony=False)
    d.update(kw)
    s = types.SimpleNamespace(**d)
    s._fallback_title = lambda: "fallback"
    return s


m = FakeMgr()
m._record_closed(sess("a"))
m._record_closed(sess("b", title="", engine="codex", pid="gone"))
rows = m.closed
check("newest first", [r["cid"] for r in rows] == ["b", "a"])
a = rows[1]
check("row carries what the modal + reopen need",
      a["session_id"] == "sid-a" and a["project"] == "dvrable" and a["title"] == "T a"
      and a["engine"] == "claude" and a["prompt_count"] == 3 and a["resumable"] is True
      and a["reason"] == "closed" and a["closed_at"] > 0, str(a))
check("blank title falls back; unknown project → '' ",
      rows[0]["title"] == "fallback" and rows[0]["project"] == "" and rows[0]["engine"] == "codex")

m._record_closed(sess("a", title="T a again"))
check("re-closing a cid dedups (one row, refreshed, newest)",
      [r["cid"] for r in m.closed] == ["a", "b"] and m.closed[0]["title"] == "T a again")

old = server.CLOSED_MAX
server.CLOSED_MAX = 5
for i in range(10):
    m._record_closed(sess(f"x{i}"))
check("capped at CLOSED_MAX, newest kept", len(m.closed) == 5 and m.closed[0]["cid"] == "x9")
server.CLOSED_MAX = old

m2 = FakeMgr()
m2._record_closed(sess("cer", ceremony=True))
check("sign-in ceremony never filed", m2.closed == [])
m2._record_closed(sess("np", prompt_count=0))
check("no-prompt session filed but marked not resumable", m2.closed and m2.closed[0]["resumable"] is False)

m3 = FakeMgr()
m3._record_closed(sess("r1"))
m3._record_closed(sess("r2", pid="gone", engine="codex"))
s = m3.reopen("r1")
check("reopen spawns the engine resume in the same project",
      s and s.cid == "new-sid-" and m3.spawns[-1] == {"pid": "p1", "engine": "claude", "resume": "sid-r1", "title": "T r1"}, str(m3.spawns))
check("reopened row dropped + saved + broadcast",
      [r["cid"] for r in m3.closed] == ["r2"] and m3.saved >= 1
      and m3.broadcasts and m3.broadcasts[-1]["type"] == "closed" and len(m3.broadcasts[-1]["closed"]) == 1)
check("unknown cid → None, nothing spawned", m3.reopen("nope") is None and len(m3.spawns) == 1)
r = m3.reopen("r2")
check("gone project → None and the row STAYS (the id isn't lost)",
      r is None and [x["cid"] for x in m3.closed] == ["r2"] and m3.spawns[-1]["engine"] == "codex")

m4 = FakeMgr()
for c in ("f1", "f2", "f3"):
    m4._record_closed(sess(c))
check("forget one", m4.closed_forget("f2") is True and [r["cid"] for r in m4.closed] == ["f3", "f1"])
check("forget unknown → no change, no save", m4.closed_forget("zz") is False)
check("forget all", m4.closed_forget(None) is True and m4.closed == [])
meta = m4.closed_meta()
check("closed_meta wire shape", meta == {"type": "closed", "closed": []})

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {FAILS}"); sys.exit(1)
print("all passed")
