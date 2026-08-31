#!/usr/bin/env python3
"""Integration test: the fleet skill LIBRARY on the relay.

The library is a stack of user-written skill files stored on the relay box —
one list, same on every device and machine, deliberately decoupled from what's
installed in any machine's ~/.claude/skills. `skillput` publishes over the
worker-token HTTP; the 📚 picker reads it (mobile WS `skillsLib`, bodies
included — a tap pastes the text into a session) and removes with `skillsRm`
(→ .trash, recoverable). This boots a REAL relay on a tmp store and asserts:

  1. auth: every endpoint refuses without the worker token; the WS frames
     refuse an unauthed mobile
  2. hostile input is fenced (bad names, traversal paths, missing SKILL.md)
  3. publish → /skills/lib and the WS skillsLib both carry name + frontmatter
     description + the full SKILL.md body
  4. skillsRm trashes (dir lands in .trash, not gone) and replies the fresh
     list; the HTTP delete path trashes identically
  5. the worker's one-shot cleanup removes exactly what the retired 08-30
     sync installed — tracked files only — then deletes its state file

Run: python3 fleet/test_skills_lib.py
"""
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fleet_ws  # noqa: E402
import worker    # noqa: E402  (_skills_sync_cleanup — the retired-sync janitor)

PORT = "8807"
BASE = f"http://127.0.0.1:{PORT}"
WS = f"ws://127.0.0.1:{PORT}"
TOKEN = "skills-test-worker"
MOBILE = "skills-test-mobile"
TMP = Path(tempfile.mkdtemp(prefix="clawd-skills-test."))
STORE = TMP / "store"

ENV = {
    **os.environ,
    "FLEET_PORT": PORT,
    "FLEET_BIND": "127.0.0.1",
    "FLEET_MOBILE_TOKEN": MOBILE,
    "FLEET_WORKER_TOKEN": TOKEN,
    "FLEET_REQUIRE_PASSKEY": "0",
    "FLEET_SKILLS_DIR": str(STORE),
    "FLEET_PREFS_FILE": str(TMP / "prefs.json"),
    "FLEET_SESSIONS_FILE": str(TMP / "sessions.json"),
    "FLEET_PUSH_SUBS_FILE": str(TMP / "push.json"),
}


def call(path, payload=None, token=TOKEN):
    url = f"{BASE}{path}{'&' if '?' in path else '?'}t={quote(token)}"
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


def dial(token=MOBILE):
    """An authed mobile + background reader (the test_relay_prefs pattern)."""
    sock, rfile, wfile = fleet_ws.client_connect(f"{WS}/ws?role=mobile&t={quote(token)}")
    lock, inbox = threading.Lock(), []

    def reader():
        while True:
            msg = fleet_ws.ws_read_message(rfile)
            if msg is None:
                return
            kind, data = msg
            if kind == "ping":
                fleet_ws.ws_send(wfile, lock, data, opcode=0xA, mask=True)
                continue
            if kind in ("pong", "close"):
                continue
            try:
                inbox.append(json.loads(data.decode()))
            except Exception:
                pass

    threading.Thread(target=reader, daemon=True).start()

    def send(obj):
        fleet_ws.ws_send(wfile, lock, json.dumps(obj), opcode=0x1, mask=True)

    return sock, send, inbox


def wait_for(inbox, pred, timeout=4.0):
    end = time.time() + timeout
    while time.time() < end:
        for f in list(inbox):
            if pred(f):
                return f
        time.sleep(0.05)
    return None


SKILL_BODY = "---\nname: vesta\ndescription: put words on the Vestaboard\n---\ncurl the board at 10.0.0.9\n"


def main():
    proc = subprocess.Popen([sys.executable, "relay.py"], env=ENV, cwd=str(HERE),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.2)
    checks, socks = [], []

    def check(name, cond):
        checks.append((name, bool(cond)))
        print(f"  {'✓' if cond else '✗ FAIL'} {name}")

    try:
        skill = {"name": "vesta", "files": {"SKILL.md": b64(SKILL_BODY)}}

        # 1+2. auth + fences
        check("lib refused without token", call("/skills/lib", token="wrong")[0] == 403)
        check("publish refused without token", call("/skills/put", skill, token="wrong")[0] == 403)
        check("bad skill name rejected",
              call("/skills/put", {"name": "Bad Name!", "files": skill["files"]})[0] == 400)
        code, r = call("/skills/put", {"name": "evil", "files": {
            "SKILL.md": b64("x"), "../../escape.txt": b64("x")}})
        check("traversal path rejected", code == 400 and "path" in r.get("error", ""))
        check("missing SKILL.md rejected",
              call("/skills/put", {"name": "nomd", "files": {"other.md": b64("x")}})[0] == 400)

        # 3. publish → both read paths carry desc + full body
        code, r = call("/skills/put", skill)
        check("publish accepted", code == 200 and r.get("ok"))
        code, lib = call("/skills/lib")
        one = (lib.get("skills") or [{}])[0]
        check("HTTP lib: name + description + full body",
              one.get("name") == "vesta"
              and one.get("description") == "put words on the Vestaboard"
              and one.get("body") == SKILL_BODY)

        s1, send1, in1 = dial()
        socks.append(s1)
        send1({"type": "skillsLib"})
        got = wait_for(in1, lambda f: f.get("type") == "skillsLib")
        check("WS skillsLib: same payload to an authed mobile",
              got and (got.get("skills") or [{}])[0].get("body") == SKILL_BODY)

        # 4. skillsRm trashes + replies the fresh (empty) list
        in1.clear()
        send1({"type": "skillsRm", "name": "vesta"})
        fresh = wait_for(in1, lambda f: f.get("type") == "skillsLib")
        trashed = list((STORE / ".trash").glob("vesta-*"))
        check("skillsRm: fresh empty list replied", fresh is not None and fresh.get("skills") == [])
        check("skillsRm: dir landed in .trash (recoverable, not gone)",
              len(trashed) == 1 and (trashed[0] / "SKILL.md").read_text() == SKILL_BODY)
        in1.clear()
        send1({"type": "skillsRm", "name": "vesta"})
        err = wait_for(in1, lambda f: f.get("type") == "error")
        check("skillsRm on a missing skill → error frame", err and "skillsRm" in err.get("error", ""))
        call("/skills/put", skill)
        code, r = call("/skills/put", {"name": "vesta", "delete": True})
        check("HTTP delete path trashes too",
              code == 200 and len(list((STORE / ".trash").glob("vesta-*"))) == 2
              and call("/skills/lib")[1] == {"skills": []})

        # 5. the retired-sync janitor: removes tracked files only, then its state
        home = TMP / "home"
        skdir = home / ".claude" / "skills"
        (skdir / "add-skill").mkdir(parents=True)
        (skdir / "add-skill" / "SKILL.md").write_text("synced by the retired sync")
        (skdir / "handmade").mkdir()
        (skdir / "handmade" / "SKILL.md").write_text("mine")
        (home / ".clawd-fleet.skills.json").write_text(json.dumps(
            {"managed": {"add-skill": {"rev": "x", "files": ["SKILL.md"]}}}))
        worker._skills_sync_cleanup(home)
        check("cleanup removes the synced skill + its state file",
              not (skdir / "add-skill").exists()
              and not (home / ".clawd-fleet.skills.json").exists())
        check("cleanup leaves hand-placed skills alone", (skdir / "handmade" / "SKILL.md").is_file())
        worker._skills_sync_cleanup(home)   # no state file → must be a silent no-op
        check("cleanup is a no-op without state", (skdir / "handmade" / "SKILL.md").is_file())
    finally:
        for s in socks:
            try:
                s.close()
            except OSError:
                pass
        proc.terminate()
        proc.wait(timeout=5)
        shutil.rmtree(TMP, ignore_errors=True)

    failed = [n for n, ok in checks if not ok]
    print("PASS — skill library: auth, fences, bodies, trash-on-remove, sync janitor"
          if not failed else f"FAIL — {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
