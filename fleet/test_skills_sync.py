#!/usr/bin/env python3
"""Integration test: the fleet skills library, end to end.

The relay holds the private skills store (.clawd-fleet.skills/) and serves it
over worker-token-gated HTTP; every machine's worker pulls it into
~/.claude/skills/ (worker.sync_skills_once), where the harness's account
symlinks fan it into every session. This boots a REAL relay (tmp store) and
runs the REAL worker sync against a tmp home, asserting the properties the
feature is:

  1. publish → manifest → sync installs the skill (in ~/.claude/skills AND a
     real-dir opt-out account, matching server.py's roots rule)
  2. auth: no/wrong token is refused on every endpoint
  3. hostile input is fenced: traversal paths, bad names, missing SKILL.md
  4. a re-publish (new rev) updates in place and removes files the new rev
     no longer ships
  5. deleting from the library removes the skill everywhere — but ONLY files
     the sync installed (a hand-placed skill sharing the dir is untouched)

Run: python3 fleet/test_skills_sync.py
"""
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import worker  # noqa: E402  (sync_skills_once — the code under test)

PORT = "8807"
BASE = f"http://127.0.0.1:{PORT}"
TOKEN = "skills-test-worker"
TMP = Path(tempfile.mkdtemp(prefix="clawd-skills-test."))
STORE = TMP / "store"
HOME = TMP / "home"

ENV = {
    **os.environ,
    "FLEET_PORT": PORT,
    "FLEET_BIND": "127.0.0.1",
    "FLEET_MOBILE_TOKEN": "skills-test-mobile",
    "FLEET_WORKER_TOKEN": TOKEN,
    "FLEET_REQUIRE_PASSKEY": "0",
    "FLEET_SKILLS_DIR": str(STORE),
    "FLEET_PREFS_FILE": str(TMP / "prefs.json"),
    "FLEET_SESSIONS_FILE": str(TMP / "sessions.json"),
    "FLEET_PUSH_SUBS_FILE": str(TMP / "push.json"),
}


def call(path, payload=None, token=TOKEN):
    url = f"{BASE}{path}{'&' if '?' in path else '?'}t={token}"
    req = urllib.request.Request(url, method="POST" if payload is not None else "GET",
                                 data=json.dumps(payload).encode() if payload is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}


def b64(s):
    return base64.b64encode(s.encode()).decode()


def sync():
    return worker.sync_skills_once(BASE, TOKEN, HOME)


def main():
    (HOME / ".claude").mkdir(parents=True)
    # one symlink-style account (covered via ~/.claude/skills) is implicit; make
    # one REAL-dir opt-out account to assert the roots rule
    optout = HOME / ".clawd-accounts" / "optout" / "skills"
    optout.mkdir(parents=True)
    proc = subprocess.Popen([sys.executable, "relay.py"], env=ENV, cwd=str(HERE),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.2)
    checks = []

    def check(name, cond):
        checks.append((name, bool(cond)))
        print(f"  {'✓' if cond else '✗ FAIL'} {name}")

    try:
        skill = {"name": "vesta", "files": {
            "SKILL.md": b64("---\nname: vesta\ndescription: put words on the Vestaboard\n---\nbody v1\n"),
            "helper/notes.md": b64("helper v1"),
        }}

        # 2. auth first — nothing readable or writable without the worker token
        check("manifest refused without token", call("/skills/manifest", token="wrong")[0] == 403)
        check("publish refused without token", call("/skills/put", skill, token="wrong")[0] == 403)

        # 3. hostile input fenced at the relay
        code, r = call("/skills/put", {"name": "Bad Name!", "files": skill["files"]})
        check("bad skill name rejected", code == 400)
        code, r = call("/skills/put", {"name": "evil", "files": {
            "SKILL.md": b64("x"), "../../escape.txt": b64("x")}})
        check("traversal path rejected", code == 400 and "path" in r.get("error", ""))
        code, r = call("/skills/put", {"name": "nomd", "files": {"other.md": b64("x")}})
        check("missing SKILL.md rejected", code == 400)

        # 1. publish → manifest → sync installs everywhere
        code, r = call("/skills/put", skill)
        check("publish accepted", code == 200 and r.get("ok"))
        code, man = call("/skills/manifest")
        names = [s["name"] for s in man.get("skills", [])]
        check("manifest lists it with its description",
              names == ["vesta"] and man["skills"][0]["description"] == "put words on the Vestaboard")
        changed = sync()
        installed = HOME / ".claude" / "skills" / "vesta"
        check("sync installs into ~/.claude/skills",
              changed == ["installed vesta"] and (installed / "SKILL.md").read_text().endswith("body v1\n")
              and (installed / "helper" / "notes.md").is_file())
        check("sync installs into a real-dir opt-out account",
              (optout / "vesta" / "SKILL.md").is_file())
        check("re-sync is a no-op (rev matched)", sync() == [])

        # 4. new rev updates in place, drops the file the new rev lost
        skill2 = {"name": "vesta", "files": {
            "SKILL.md": b64("---\nname: vesta\ndescription: put words on the Vestaboard\n---\nbody v2\n")}}
        call("/skills/put", skill2)
        changed = sync()
        check("update lands and stale helper file is removed",
              changed == ["updated vesta"]
              and (installed / "SKILL.md").read_text().endswith("body v2\n")
              and not (installed / "helper" / "notes.md").exists())

        # 5. delete removes it everywhere — but only what the sync installed
        handmade = HOME / ".claude" / "skills" / "handmade"
        handmade.mkdir(parents=True)
        (handmade / "SKILL.md").write_text("mine — not the fleet's")
        (installed / "my-local-note.txt").write_text("user file inside a managed skill")
        call("/skills/put", {"name": "vesta", "delete": True})
        changed = sync()
        check("delete propagates", changed == ["removed vesta"]
              and not (installed / "SKILL.md").exists()
              and not (optout / "vesta").exists())
        check("user file inside the managed dir survives (dir kept)",
              (installed / "my-local-note.txt").is_file())
        check("hand-placed skill untouched", (handmade / "SKILL.md").is_file())
        check("empty manifest after delete", call("/skills/manifest")[1] == {"skills": []})
    finally:
        proc.terminate()
        proc.wait(timeout=5)
        shutil.rmtree(TMP, ignore_errors=True)

    failed = [n for n, ok in checks if not ok]
    print("PASS — fleet skills library: publish/sync/update/delete + fences hold"
          if not failed else f"FAIL — {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
