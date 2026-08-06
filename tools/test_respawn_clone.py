#!/usr/bin/env python3
"""Regression guard: every field ClaudeSession PERSISTS must SURVIVE a respawn.

The bug class this pins down (the recurring "my tab/session state rolled back"
reports, most recently the 📌 pin wipe of 2026-08-06): the account-handoff and
onboarding-rescue paths rebuild the session object in place, and any durable
field not carried across is silently reset — then save_registry() makes the
loss permanent. clone_for_respawn() now derives its copy from the constructor
signature, and this test asserts the two invariants that keep that airtight:

  1. clone_for_respawn() reproduces to_registry() exactly (no field lost).
  2. Every to_registry() key is a constructor parameter — a persisted field
     that isn't a ctor param couldn't be restored on boot OR carried across a
     respawn, so adding one fails here loudly instead of rolling back later.

Runs against a sandboxed COPY of server.py in a temp dir (importing it from
the repo would construct the real SessionManager and resume live sessions).
No server needed; safe anywhere. Exit 0 = invariants hold.
"""
import importlib.util
import shutil
import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def load_server_sandboxed():
    tmp = Path(tempfile.mkdtemp(prefix="respawn-clone-test-"))
    shutil.copy(REPO / "server.py", tmp / "server.py")
    spec = importlib.util.spec_from_file_location("server_sandbox",
                                                  tmp / "server.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["server_sandbox"] = mod
    spec.loader.exec_module(mod)     # HERE = tmp dir → empty registry, inert
    return mod


def main():
    srv = load_server_sandboxed()
    import inspect
    params = [n for n in inspect.signature(srv.ClaudeSession.__init__).parameters
              if n not in ("self", "manager")]

    # A distinct, truthy sentinel per field (truthy so `x or default` fallbacks
    # in the ctor can't mask a dropped value).
    sentinels = {}
    for i, name in enumerate(params):
        if name in ("resuming", "ceremony"):
            sentinels[name] = True
        elif name in ("created", "last_active", "prompted_at", "pinned"):
            sentinels[name] = 1000.0 + i
        elif name == "prompt_count":
            sentinels[name] = 7 + i
        else:
            sentinels[name] = f"sentinel-{i}-{name}"

    old = srv.ClaudeSession(types.SimpleNamespace(), **sentinels)

    # Invariant 2 first: persisted keys ⊆ ctor params.
    orphan = set(old.to_registry()) - set(params)
    assert not orphan, (
        f"to_registry() persists {sorted(orphan)} but they are not "
        "ClaudeSession ctor params — they can't survive a boot resume or a "
        "respawn. Add them to __init__ (stored under the same name).")

    # Invariant 1: a plain clone loses nothing.
    clone = old.clone_for_respawn()
    a, b = old.to_registry(), clone.to_registry()
    diff = {k: (a[k], b.get(k)) for k in a if a[k] != b.get(k)}
    assert not diff, f"respawn clone dropped/changed persisted fields: {diff}"

    # Overrides apply — and don't bleed into anything else.
    c2 = old.clone_for_respawn(account="elsewhere", resuming=True)
    assert c2.account == "elsewhere" and c2.resuming is True
    assert c2.pinned == old.pinned and c2.title == old.title

    print(f"OK — all {len(a)} persisted fields survive a respawn clone "
          f"({len(params)} ctor params checked)", flush=True)


if __name__ == "__main__":
    main()
    import os
    os._exit(0)      # the sandboxed import may have started daemon threads
