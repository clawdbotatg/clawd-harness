#!/usr/bin/env python3
"""The folder-trust seed: claude's per-folder trust dialog never paints.

Why this exists: the CLI blocks a first spawn on "Is this a project you
created or one you trust?" — remembered per (login, abs cwd) as
`projects[path].hasTrustDialogAccepted: true` in that login's .claude.json.
In this harness the human already chose every folder, and the modal re-fires
on every fresh clone AND every account handoff (new config dir = the path is
new to that login), parking the session dead until someone opens the tty and
presses Enter. `_ensure_trusted` answers it in the config file before spawn.

These tests pin the three things that make the seed safe rather than merely
convenient:

  * a dir with NO .claude.json is left strictly alone (creating the file
    would flip _opens_normal_tui's never-signed-in detection and garble the
    sign-in ceremony),
  * a foreign/corrupt file is never rewritten,
  * existing config (other keys, other projects, the entry's own siblings
    like projectOnboardingSeenCount) survives the write byte-for-value.

Pure helper — constructs no SessionManager, touches no registry, spawns
nothing (the scratch-registry trap).

    python3 test_trust_seed.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

import server

PASS = 0


def ok(cond, msg):
    global PASS
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)
    PASS += 1
    print(f"  ok: {msg}")


def cfg_path(d):
    return Path(d) / ".claude.json"


def main():
    with tempfile.TemporaryDirectory() as tmp:
        work = os.path.join(tmp, "proj")
        os.makedirs(work)

        # 1) never-signed-in dir: no .claude.json → untouched, not created
        acct = os.path.join(tmp, "acct-fresh")
        os.makedirs(acct)
        ok(server._ensure_trusted(acct, work) is False,
           "no .claude.json → declines")
        ok(not cfg_path(acct).exists(),
           "no .claude.json → file NOT created (ceremony dir stays fresh)")

        # 2) signed-in dir → seeds trust, preserves everything else
        acct2 = os.path.join(tmp, "acct-live")
        os.makedirs(acct2)
        before = {"hasCompletedOnboarding": True,
                  "oauthAccount": {"emailAddress": "x@y.z"},
                  "projects": {"/elsewhere": {"hasTrustDialogAccepted": True,
                                              "projectOnboardingSeenCount": 7}}}
        cfg_path(acct2).write_text(json.dumps(before))
        ok(server._ensure_trusted(acct2, work) is True, "signed-in dir → seeds")
        after = json.loads(cfg_path(acct2).read_text())
        keyed = after["projects"].get(os.path.abspath(work), {})
        ok(keyed.get("hasTrustDialogAccepted") is True,
           "cwd keyed by abspath with hasTrustDialogAccepted true")
        ok(after["oauthAccount"] == before["oauthAccount"]
           and after["hasCompletedOnboarding"] is True,
           "unrelated top-level keys survive")
        ok(after["projects"]["/elsewhere"]["projectOnboardingSeenCount"] == 7,
           "other projects' entries survive")

        # 3) idempotent: already trusted → no write
        mtime = cfg_path(acct2).stat().st_mtime_ns
        ok(server._ensure_trusted(acct2, work) is False,
           "already trusted → declines")
        ok(cfg_path(acct2).stat().st_mtime_ns == mtime,
           "already trusted → file untouched")

        # 4) existing entry gains the flag without losing siblings
        acct3 = os.path.join(tmp, "acct-partial")
        os.makedirs(acct3)
        cfg_path(acct3).write_text(json.dumps({"projects": {
            os.path.abspath(work): {"projectOnboardingSeenCount": 3}}}))
        ok(server._ensure_trusted(acct3, work) is True,
           "entry without the flag → seeds")
        e = json.loads(cfg_path(acct3).read_text())["projects"][os.path.abspath(work)]
        ok(e.get("hasTrustDialogAccepted") is True
           and e.get("projectOnboardingSeenCount") == 3,
           "entry's sibling keys survive the seed")

        # 5) corrupt file → untouched
        acct4 = os.path.join(tmp, "acct-corrupt")
        os.makedirs(acct4)
        cfg_path(acct4).write_text("{not json")
        ok(server._ensure_trusted(acct4, work) is False, "corrupt json → declines")
        ok(cfg_path(acct4).read_text() == "{not json",
           "corrupt json → file untouched")

        # 6) symlinked cwd: both the symlink path and its target get keyed
        #    (node resolves cwd through symlinks; our registry path may not)
        link = os.path.join(tmp, "proj-link")
        os.symlink(work, link)
        acct5 = os.path.join(tmp, "acct-sym")
        os.makedirs(acct5)
        cfg_path(acct5).write_text("{}")
        ok(server._ensure_trusted(acct5, link) is True, "symlink cwd → seeds")
        pr = json.loads(cfg_path(acct5).read_text())["projects"]
        ok(pr.get(os.path.abspath(link), {}).get("hasTrustDialogAccepted") is True
           and pr.get(os.path.realpath(link), {}).get("hasTrustDialogAccepted") is True,
           "both abspath and realpath keyed")

    print(f"\nall {PASS} checks passed")


if __name__ == "__main__":
    main()
