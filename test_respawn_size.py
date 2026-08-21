#!/usr/bin/env python3
"""Regression guard: an in-place respawn keeps the viewer's PTY geometry.

The 2026-08-20 phone screenshot: a session was handed off to another account
(clone_for_respawn → start → carry viewers) and came back with every line's
tail wrapped onto the next row. The respawned session opened its PTY at the
120×34 boot defaults with tty_owner=None; the phone that was carried across
got a hello at the wrong dims, armed staleGeomReplay, and waited for a
ttySize frame that nothing would ever send — it only emits maintenance
resizes, and no one re-applied its claim. Claude's --resume repainted at 120
cols and a ~44-col xterm rendered that shredded.

Asserts, against a sandboxed copy of server.py (never the live one):
  1. clone_for_respawn() carries tty_cols/tty_rows (start() reads them).
  2. adopt_viewers() moves the viewers AND the size owner; the hello they get
     carries the owner's dims; a viewer that switched away is not carried.
  3. With the owner gone, the most recently sized survivor takes over.
  4. With no sized viewer at all, the respawn keeps the old geometry (no
     regression to the 120×34 defaults).
Exit 0 = invariants hold.
"""
import importlib.util
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent


def load_server_sandboxed():
    tmp = Path(tempfile.mkdtemp(prefix="respawn-size-test-"))
    shutil.copy(REPO / "server.py", tmp / "server.py")
    spec = importlib.util.spec_from_file_location("server_sandbox", tmp / "server.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["server_sandbox"] = mod
    spec.loader.exec_module(mod)     # HERE = tmp dir → empty registry, inert
    return mod


class FakeClient:
    def __init__(self, cid, size=None, ts=0.0):
        self.cid, self.tty_size, self.tty_ts = cid, size, ts
        self.dead = False
        self.frames, self.bytes = [], []

    def send_json(self, obj):
        self.frames.append(obj)

    def send_bytes(self, data):
        self.bytes.append(data)


def mk(srv, cid="cid-1"):
    mgr = types.SimpleNamespace(projects={})
    return srv.ClaudeSession(mgr, cid=cid, pid="p", session_id="", resuming=False, first_prompt="x")


def main():
    srv = load_server_sandboxed()
    PHONE, LAPTOP = (44, 60), (160, 48)

    # --- 1+2: owner rides across, hello carries its dims -------------------
    old = mk(srv)
    phone = FakeClient(old.cid, PHONE, ts=10.0)
    laptop = FakeClient(old.cid, LAPTOP, ts=5.0)
    gone = FakeClient("other-cid", LAPTOP, ts=99.0)    # switched away mid-respawn
    for c in (phone, laptop, gone):
        old.clients.add(c)
    old.tty_owner = phone
    old.tty_cols, old.tty_rows = PHONE
    fresh = old.clone_for_respawn(account="elsewhere", resuming=True)
    assert (fresh.tty_cols, fresh.tty_rows) == PHONE, \
        f"clone lost the geometry: {(fresh.tty_cols, fresh.tty_rows)} != {PHONE}"
    kept = fresh.adopt_viewers(old)
    assert set(kept) == {phone, laptop}, "adopt_viewers carried the wrong viewers"
    assert fresh.clients == {phone, laptop} and not old.clients
    assert fresh.tty_owner is phone, "size owner did not ride across the respawn"
    assert (fresh.tty_cols, fresh.tty_rows) == PHONE
    hello = [f for f in phone.frames if f.get("type") == "hello"]
    assert hello and (hello[-1]["cols"], hello[-1]["rows"]) == PHONE, \
        f"hello carried the wrong dims: {hello}"
    assert not gone.frames, "a viewer that switched away got re-subscribed"

    # --- 3: owner gone → most recently sized survivor ------------------------
    old = mk(srv, "cid-2")
    a = FakeClient(old.cid, PHONE, ts=1.0)
    b = FakeClient(old.cid, LAPTOP, ts=2.0)
    owner = FakeClient("elsewhere", (80, 24), ts=3.0)   # the owner left for another session
    for c in (a, b, owner):
        old.clients.add(c)
    old.tty_owner = owner
    old.tty_cols, old.tty_rows = (80, 24)
    fresh = old.clone_for_respawn()
    fresh.adopt_viewers(old)
    assert fresh.tty_owner is b, "fallback owner should be the most recently sized survivor"
    assert (fresh.tty_cols, fresh.tty_rows) == LAPTOP

    # --- 4: nobody sized → keep the old geometry, not the boot defaults ------
    old = mk(srv, "cid-3")
    unsized = FakeClient(old.cid, None)
    old.clients.add(unsized)
    old.tty_cols, old.tty_rows = PHONE
    fresh = old.clone_for_respawn()
    fresh.adopt_viewers(old)
    assert fresh.tty_owner is None
    assert (fresh.tty_cols, fresh.tty_rows) == PHONE, "respawn fell back to COLS×ROWS"
    assert fresh.clients == {unsized}

    # --- start() honours the carried geometry (read the source, no PTY) ------
    import inspect
    src = inspect.getsource(srv.ClaudeSession.start)
    assert "self.tty_rows" in src and "self.tty_cols" in src, \
        "start() must open the PTY at self.tty_rows/self.tty_cols, not the boot constants"

    print("OK — respawn keeps the viewer's PTY geometry + size owner "
          "(handoff / onboarding heal)", flush=True)


if __name__ == "__main__":
    main()
    os._exit(0)      # the sandboxed import may have started daemon threads
