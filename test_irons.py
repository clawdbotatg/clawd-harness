#!/usr/bin/env python3
"""Direct-mode irons: named groups of projects, stored in the registry.

An iron ("irons in the fire") bundles projects into one trackable effort; the
UI's iron page shows every session from every member project. This guards the
server half in DIRECT mode (fleet stores irons relay-side — that half is
fleet/test_relay_prefs.py):

  1. CRUD: create trims/clips + mints an id; update edits in place; delete.
  2. Assignment is ONE iron per project — assigning moves, "" removes, and
     unknown pids/irons are refused.
  3. A vanished project is forgotten by every iron (_iron_forget_pid — the
     reconcile/remove cleanup path).
  4. Registry round-trip against a SANDBOXED copy of server.py (never the live
     one — the scratch-registry trap): saved irons come back, and membership is
     validated at load, so a pid whose project died while we were down never
     lingers in an iron.

Run: python3 test_irons.py
"""
import importlib.util
import json
import shutil
import sys
import tempfile
import threading
import types
from pathlib import Path

import server

REPO = Path(__file__).resolve().parent
FAILS = []


def check(name, ok, detail=""):
    print(f"  {'✓' if ok else '✗ FAIL'} {name}" + ("" if ok or not detail else f" — {detail}"))
    if not ok:
        FAILS.append(name)


class FakeMgr:
    """Just enough manager for the iron ops — real methods, stubbed I/O."""
    irons_meta = server.SessionManager.irons_meta
    iron_create = server.SessionManager.iron_create
    iron_update = server.SessionManager.iron_update
    iron_delete = server.SessionManager.iron_delete
    iron_assign = server.SessionManager.iron_assign
    _iron_forget_pid = server.SessionManager._iron_forget_pid

    def __init__(self):
        self.lock = threading.RLock()
        self.irons = {}
        self.projects = {"p1": object(), "p2": object()}
        self.saved = 0
        self.broadcasts = []

    def save_registry(self):
        self.saved += 1

    def broadcast_irons(self):
        self.broadcasts.append(self.irons_meta())


def test_crud():
    m = FakeMgr()
    check("blank title refused", m.iron_create("   ") is None and not m.saved)
    it = m.iron_create("  voice  ", desc=" all the voice work ", tags=["speech", "  ", 7, "x" * 99])
    check("create trims + clips + mints an id",
          it and it["title"] == "voice" and it["desc"] == "all the voice work"
          and it["tags"] == ["speech", "x" * 24] and it["id"] and it["pids"] == [])
    check("create saves + broadcasts", m.saved == 1 and len(m.broadcasts) == 1)
    check("meta frame shape", m.irons_meta()["type"] == "irons"
          and m.irons_meta()["irons"][0]["id"] == it["id"])
    m.iron_update(it["id"], title=" voice v2 ", tags=["a", "b"])
    check("update edits in place", m.irons[it["id"]]["title"] == "voice v2"
          and m.irons[it["id"]]["tags"] == ["a", "b"])
    check("update on a ghost iron refused", m.iron_update("nope", title="x") is False)
    check("delete", m.iron_delete(it["id"]) and not m.irons)
    check("double delete refused", m.iron_delete(it["id"]) is False)


def test_assign():
    m = FakeMgr()
    a = m.iron_create("alpha")
    b = m.iron_create("beta")
    check("unknown pid refused", m.iron_assign("ghost", a["id"]) is False)
    check("unknown iron refused", m.iron_assign("p1", "ghost") is False)
    m.iron_assign("p1", a["id"])
    m.iron_assign("p2", a["id"])
    check("assign lands", m.irons[a["id"]]["pids"] == ["p1", "p2"])
    m.iron_assign("p1", b["id"])
    check("ONE iron per project — assigning moves it",
          m.irons[a["id"]]["pids"] == ["p2"] and m.irons[b["id"]]["pids"] == ["p1"])
    m.iron_assign("p1", b["id"])
    check("re-assign is idempotent", m.irons[b["id"]]["pids"] == ["p1"])
    m.iron_assign("p1", "")
    check('"" takes it out of any iron',
          m.irons[b["id"]]["pids"] == [] and m.irons[a["id"]]["pids"] == ["p2"])


def test_forget():
    m = FakeMgr()
    a = m.iron_create("alpha")
    m.iron_assign("p1", a["id"])
    check("_iron_forget_pid drops the pid and reports it",
          m._iron_forget_pid("p1") is True and m.irons[a["id"]]["pids"] == [])
    check("forgetting an absent pid is a quiet no-op", m._iron_forget_pid("p1") is False)


def test_registry_roundtrip():
    tmp = Path(tempfile.mkdtemp(prefix="irons-test-"))
    try:
        shutil.copy(REPO / "server.py", tmp / "server.py")
        spec = importlib.util.spec_from_file_location("server_irons_sandbox", tmp / "server.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["server_irons_sandbox"] = mod
        spec.loader.exec_module(mod)         # HERE = tmp → registry + projects live there
        pdir = mod.PROJECTS_DIR
        (pdir / "alive").mkdir(parents=True)
        mod.REGISTRY_FILE.write_text(json.dumps({
            "projects": [{"pid": "p-alive", "name": "alive", "path": str(pdir / "alive"),
                          "repo_url": "https://github.com/x/alive", "kind": "gh"},
                         {"pid": "p-ghost", "name": "ghost", "path": str(pdir / "ghost"),
                          "repo_url": "https://github.com/x/ghost", "kind": "gh"}],
            "sessions": [],
            "irons": [{"id": "i1", "title": "  fire  ", "desc": "d" * 999,
                       "tags": ["ok", "", 3], "pids": ["p-alive", "p-ghost"], "created": 7.0},
                      {"id": "", "title": "no id — dropped"},
                      {"id": "i2", "title": ""}],
        }))
        mgr = mod.SessionManager()
        mgr.load()
        check("iron survives a reboot", "i1" in mgr.irons and mgr.irons["i1"]["title"] == "fire")
        check("hydration clips + types the fields",
              len(mgr.irons["i1"]["desc"]) == 400 and mgr.irons["i1"]["tags"] == ["ok", "3"])
        check("a pid whose project died offline is dropped from the iron",
              mgr.irons["i1"]["pids"] == ["p-alive"],
              str(mgr.irons["i1"]["pids"]))
        check("junk iron entries never hydrate", set(mgr.irons) == {"i1"})
        saved = json.loads(mod.REGISTRY_FILE.read_text())
        check("save_registry writes the irons back",
              saved.get("irons") and saved["irons"][0]["id"] == "i1")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    print("[irons] CRUD")
    test_crud()
    print("[irons] assignment")
    test_assign()
    print("[irons] cleanup")
    test_forget()
    print("[irons] registry round-trip (sandboxed server.py)")
    test_registry_roundtrip()
    if FAILS:
        print(f"FAILED: {len(FAILS)} check(s): {FAILS}")
        sys.exit(1)
    print("PASSED: irons CRUD + one-iron-per-project + cleanup + registry round-trip")


if __name__ == "__main__":
    main()
