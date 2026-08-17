#!/usr/bin/env python3
"""The share/ kit sync: repo-shipped skills + CLIs land on every machine.

Why this exists: the todo skill (Austin's shared list) must be known to every
session on every machine, and push-to-main is the only distribution channel
the fleet has. `_sync_shared_kit` runs at boot and installs
share/skills/* → ~/.claude/skills/ (fanned into accounts by the SHARE_PATHS
symlink) and share/bin/* → ~/bin. These tests pin the parts that would fail
silently:

  * install lands in ~/.claude/skills, ~/bin (executable), AND any account
    whose skills/ is a real dir rather than the shared symlink,
  * a symlinked account is left as a symlink (no copy shoved through it),
  * idempotent — a second run changes nothing,
  * the repo copy is canonical — a locally edited file is overwritten,
  * nothing under share/ carries a credential (the kit is committed to a
    public repo; the todo token must only ever live in ~/.clawd-todo.env).

Runs entirely against temp dirs — no SessionManager, nothing spawned.

    python3 test_shared_kit.py
"""
import os
import re
import sys
import tempfile
from pathlib import Path

import server

FAILS = []


def check(name, ok, detail=""):
    print(f"  {'ok' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def main():
    share = server.SHARE_DIR
    check("share/skills/todo/SKILL.md shipped", (share / "skills/todo/SKILL.md").is_file())
    check("share/bin/todo shipped", (share / "bin/todo").is_file())

    # no credential may ride the kit (this repo is public)
    leak = []
    for f in share.rglob("*"):
        if f.is_file():
            text = f.read_text(errors="ignore")
            if re.search(r"TODO_TOKEN\s*=\s*[A-Za-z0-9]{8,}", text) \
               or re.search(r"Bearer\s+[A-Za-z0-9_\-]{16,}", text):
                leak.append(str(f))
    check("no token committed under share/", not leak, ", ".join(leak))

    with tempfile.TemporaryDirectory() as td:
        home = Path(td) / "home"
        accounts = Path(td) / "accounts"
        # one account on the shared symlink, one with a real skills dir
        (home / ".claude" / "skills").mkdir(parents=True)
        (accounts / "linked").mkdir(parents=True)
        (accounts / "linked" / "skills").symlink_to(home / ".claude" / "skills")
        (accounts / "real" / "skills").mkdir(parents=True)

        changed = server._sync_shared_kit(home=home, accounts_dir=accounts)

        skill = home / ".claude" / "skills" / "todo" / "SKILL.md"
        check("skill installed to ~/.claude/skills", skill.is_file())
        check("skill content matches repo copy",
              skill.read_bytes() == (share / "skills/todo/SKILL.md").read_bytes())
        cli = home / "bin" / "todo"
        check("CLI installed to ~/bin", cli.is_file())
        check("CLI is executable", os.access(cli, os.X_OK))
        check("real-dir account got its own copy",
              (accounts / "real" / "skills" / "todo" / "SKILL.md").is_file())
        check("symlinked account still a symlink",
              (accounts / "linked" / "skills").is_symlink())
        check("symlinked account sees the skill through the link",
              (accounts / "linked" / "skills" / "todo" / "SKILL.md").is_file())
        check("first run reports changes", bool(changed))

        check("second run is a no-op",
              server._sync_shared_kit(home=home, accounts_dir=accounts) == [])

        skill.write_text("locally edited\n")
        server._sync_shared_kit(home=home, accounts_dir=accounts)
        check("repo copy is canonical (local edit overwritten)",
              skill.read_bytes() == (share / "skills/todo/SKILL.md").read_bytes())

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)}: {', '.join(FAILS)}")
        sys.exit(1)
    print("all shared-kit checks passed")


if __name__ == "__main__":
    main()
