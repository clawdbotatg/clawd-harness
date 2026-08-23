#!/usr/bin/env python3
"""Local projects: the create-if-missing confirm flow (2026-08-23).

`addLocalProject` on a path that doesn't exist round-trips a confirmation
(`confirm_create:<abspath>` sentinel → `localProjectMissing` frame → UI
are-you-sure naming the absolute path + machine) and only the retry with
create=True ever touches the disk. Pins the parts that would fail dangerously:

  * a missing path WITHOUT create creates nothing and returns the sentinel
    carrying the resolved absolute path (what the dialog shows must be what
    mkdir would make),
  * WITH create it mkdir -p's (parents included) and registers kind="local"
    with repo_url forced "" — the privacy guarantee holds for created folders,
  * every path guard (/, ~, projects/, the harness dir) beats create=True and
    leaves the disk untouched — create can never be talked into them,
  * a path that exists as a FILE errors either way and is never clobbered,
  * re-adding a registered path reuses the project (no dup),
  * the existing-folder path still registers with no create flag (regression).

No SessionManager loop, no claude, nothing leaves the machine.

    python3 test_local_create.py
"""
import os
import sys
import tempfile
import threading
from pathlib import Path

import server

FAILS = []


def check(name, ok, detail=""):
    print(f"  {'ok' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


class FakeMgr:
    """Just enough SessionManager for add_local_project."""
    def __init__(self):
        self.projects = {}
        self.lock = threading.Lock()
        self.saved = 0
        self.broadcasts = 0

    def save_registry(self):
        self.saved += 1

    def broadcast_projects(self):
        self.broadcasts += 1

    _unique_project_name = server.SessionManager._unique_project_name
    add_local_project = server.SessionManager.add_local_project


def run(tmp):
    mgr = FakeMgr()
    add = mgr.add_local_project

    print("missing path, no create")
    want = os.path.join(tmp, "newproj")
    p, err = add(want)
    check("returns the confirm sentinel", p is None and err == f"confirm_create:{want}", err)
    check("nothing created on disk", not os.path.exists(want))
    p, err = add("~/nope-" + os.path.basename(tmp))
    check("~ paths resolve absolute in the sentinel",
          p is None and err.startswith("confirm_create:/"), err)
    check("…and that wasn't created either",
          not os.path.exists(os.path.expanduser("~/nope-" + os.path.basename(tmp))))

    print("missing path, create=True")
    deep = os.path.join(tmp, "a", "b", "proj")
    p, err = add(deep, create=True)
    check("registers", p is not None and err == "", err)
    check("mkdir -p'd the parents", os.path.isdir(deep))
    check("kind=local, no repo url", p and p.kind == "local" and p.repo_url == "")
    check("status ready", p and p.status == "ready")
    check("registry saved + broadcast", mgr.saved >= 1 and mgr.broadcasts >= 1)

    print("re-add reuses")
    p2, err = add(deep)
    check("same project back, no error", p2 is p and err == "", err)

    print("guards beat create")
    home = os.path.realpath(os.path.expanduser("~"))
    for label, target in [("/", "/"), ("home", home),
                          ("projects/", str(server.PROJECTS_DIR / "victim")),
                          ("harness dir", str(server.HERE / "victim"))]:
        p3, err = add(target, create=True)
        check(f"{label} refused", p3 is None and err
              and not err.startswith("confirm_create:"), err)
    check("projects/ victim not created",
          not os.path.exists(server.PROJECTS_DIR / "victim"))
    check("harness victim not created",
          not os.path.exists(server.HERE / "victim"))

    print("file in the way")
    f = os.path.join(tmp, "afile")
    Path(f).write_text("x")
    p4, err = add(f, create=True)
    check("file path refused", p4 is None and "not a directory" in err, err)
    check("file untouched", Path(f).read_text() == "x")

    print("existing folder (regression)")
    plain = os.path.join(tmp, "already-there")
    os.mkdir(plain)
    p5, err = add(plain)
    check("registers without create", p5 is not None and err == "", err)


def main():
    # realpath'd so sentinel comparisons match the server's resolved paths
    # (macOS mkdtemp hands back /var/… which realpath turns into /private/var/…)
    tmp = os.path.realpath(tempfile.mkdtemp(prefix="clawd-localcreate-"))
    orig = server.PROJECTS_DIR
    server.PROJECTS_DIR = Path(tmp) / "projects"
    server.PROJECTS_DIR.mkdir()
    try:
        run(tmp)
    finally:
        server.PROJECTS_DIR = orig
    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED: {FAILS}")
        sys.exit(1)
    print("all ok")


if __name__ == "__main__":
    main()
