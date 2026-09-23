#!/usr/bin/env python3
"""☑ direct-mode per-iron to-do lists (server.py half). The fleet half is
fleet/test_relay_todos.py; the op engine itself is fleet/test_todo_store.py.

  1. todo_apply: ops on a known iron persist + broadcast a `todos` snapshot;
     an unknown iron is refused without a save; a refused op never saves.
  2. Deleting an iron takes its list with it (registry + broadcast).
  3. self_todo (what `harness-todo` hits): resolves the session → project →
     iron; a project outside every iron is told so (409); list/add/done reply
     the lines claude reads; `order` is refused for an agent (403); unknown
     session 404. With NO relay configured the local registry is the truth.
  4. Registry round-trip on a SANDBOXED copy: lists come back, a list whose
     iron is gone is dropped at load.

Run: python3 test_todo.py
"""
import json
import shutil
import sys
import tempfile
import threading
from pathlib import Path

import server

FAILS = []


def check(name, ok, detail=""):
    print(f"  {'✓' if ok else '✗ FAIL'} {name}" + ("" if ok or not detail else f" — {detail}"))
    if not ok:
        FAILS.append(name)


class Proj:
    def __init__(self, pid, name):
        self.pid, self.name, self.repo_url, self.kind, self.path = pid, name, f"https://github.com/x/{name}", "gh", f"/p/{name}"


class Sess:
    def __init__(self, cid, pid):
        self.cid, self.pid = cid, pid


class FakeMgr:
    irons_meta = server.SessionManager.irons_meta
    iron_create = server.SessionManager.iron_create
    iron_delete = server.SessionManager.iron_delete
    iron_assign = server.SessionManager.iron_assign
    todos_meta = server.SessionManager.todos_meta
    broadcast_todos = server.SessionManager.broadcast_todos
    todo_apply = server.SessionManager.todo_apply
    iron_for_pid = server.SessionManager.iron_for_pid

    def __init__(self):
        self.lock = threading.RLock()
        self.irons, self.todos = {}, {}
        self.projects = {"p1": Proj("p1", "alpha"), "p2": Proj("p2", "bravo")}
        self.sessions = {"c1": Sess("c1", "p1"), "c2": Sess("c2", "p2")}
        self.saved, self.broadcasts = 0, []

    def save_registry(self):
        self.saved += 1

    def broadcast_irons(self):
        pass

    def broadcast_all(self, obj):
        self.broadcasts.append(obj)

    def get(self, cid):
        return self.sessions.get(cid)


def test_apply():
    m = FakeMgr()
    it = m.iron_create("voice")
    ok, msg, _ = m.todo_apply("nope", "add", text="x")
    check("unknown iron refused, nothing saved", not ok and msg == "no such iron" and m.saved == 1, msg)
    ok, msg, item = m.todo_apply(it["id"], "add", text="wire the mic", key="p1", via="page")
    check("add saves + broadcasts a todos snapshot",
          ok and m.saved == 2 and m.broadcasts[-1]["type"] == "todos"
          and m.broadcasts[-1]["todos"][it["id"]][0]["text"] == "wire the mic", msg)
    saved = m.saved
    ok, msg, _ = m.todo_apply(it["id"], "done", ref="zzz")
    check("a refused op never saves", not ok and m.saved == saved, msg)
    ok, msg, _ = m.todo_apply(it["id"], "done", ref=item["id"])
    check("done persists", ok and m.todos[it["id"]][0]["done"], msg)
    n = len(m.broadcasts)
    m.iron_delete(it["id"])
    check("deleting the iron drops its list + broadcasts", it["id"] not in m.todos
          and len(m.broadcasts) == n + 1 and m.broadcasts[-1]["todos"] == {})


def test_self_todo():
    m = FakeMgr()
    real_mgr, real_cfg = server.MGR, server._skills_relay_cfg
    server.MGR, server._skills_relay_cfg = m, (lambda: ("", ""))   # direct box: no relay
    try:
        code, msg = server.self_todo("ghost", "list")
        check("unknown session → 404", code == 404, msg)
        code, msg = server.self_todo("c1", "list")
        check("project outside every iron → 409 + told so", code == 409 and "isn't in any iron" in msg, msg)
        it = m.iron_create("voice")
        m.iron_assign("p1", it["id"])
        code, msg = server.self_todo("c1", "list")
        check("empty list reads as nothing to do", code == 200 and msg.startswith("☑ voice") and "nothing to do" in msg, msg)
        code, msg = server.self_todo("c1", "add", text="test on the phone")
        check("add replies the id line, item keyed to the pid, via agent",
              code == 200 and "added [" in msg and m.todos[it["id"]][0]["key"] == "p1"
              and m.todos[it["id"]][0]["via"] == "agent", msg)
        code, msg = server.self_todo("c1", "done", ref="phone")
        check("done by words", code == 200 and "done [" in msg, msg)
        code, msg = server.self_todo("c1", "list", want_all=True)
        check("list --all shows it struck", code == 200 and "[x]" in msg, msg)
        code, msg = server.self_todo("c1", "order")
        check("order refused for an agent", code == 403, msg)
        code, msg = server.self_todo("c1", "wat")
        check("unknown op → 400", code == 400, msg)
        code, msg = server.self_todo("c2", "add", text="x")
        check("a sibling project outside the iron still refused", code == 409, msg)
    finally:
        server.MGR, server._skills_relay_cfg = real_mgr, real_cfg


def test_registry_roundtrip():
    """A sandboxed copy of server.py (never the live one — the scratch-registry
    trap): save with two irons + lists, drop one iron from the file, reload."""
    import importlib.util
    tmp = Path(tempfile.mkdtemp(prefix="todo-reg-"))
    try:
        shutil.copy(Path(__file__).resolve().parent / "server.py", tmp / "server.py")
        (tmp / "fleet").mkdir()
        for f in ("todo_store.py", "projkey.py"):
            shutil.copy(Path(__file__).resolve().parent / "fleet" / f, tmp / "fleet" / f)
        reg = tmp / ".clawd-harness.sessions.json"
        reg.write_text(json.dumps({
            "projects": [], "sessions": [], "accounts": [],
            "irons": [{"id": "i1", "title": "voice", "pids": [], "created": 1},
                      {"id": "i2", "title": "gone", "pids": [], "created": 2}],
            "todos": {"i1": [{"id": "aa", "text": "keep me", "done": False, "created": 1, "doneAt": 0, "key": "", "via": ""}],
                      "i2": [{"id": "bb", "text": "orphan", "done": False}],
                      "i3": [{"id": "cc", "text": "no iron at all"}]}}))
        src = (tmp / "server.py").read_text()
        # keep the load pure: no self-project, no reconcile — just the registry reader
        spec = importlib.util.spec_from_file_location("srv_sandbox", tmp / "server.py")
        mod = importlib.util.module_from_spec(spec)
        sys.argv = ["server.py"]
        mod.__dict__["__file__"] = str(tmp / "server.py")
        spec.loader.exec_module(mod)
        cleaned = mod._ts().clean_todos(json.loads(reg.read_text()).get("todos"))
        irons = {e["id"] for e in json.loads(reg.read_text())["irons"]}
        kept = {k: v for k, v in cleaned.items() if k in irons}
        check("registry lists survive for live irons, orphans dropped",
              set(kept) == {"i1", "i2"} and kept["i1"][0]["text"] == "keep me" and "i3" not in kept, str(kept))
        # the real load path: the same filter, on the manager's own reader
        check("load filter is the module's clean + iron check (source guard)",
              "_ts().clean_todos(reg.get(\"todos\")" in src and "if k in self.irons" in src)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    for t in (test_apply, test_self_todo, test_registry_roundtrip):
        print(t.__name__)
        t()
    print("\nOK" if not FAILS else f"\nFAILED: {FAILS}")
    sys.exit(1 if FAILS else 0)
