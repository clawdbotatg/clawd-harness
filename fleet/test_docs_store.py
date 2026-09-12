#!/usr/bin/env python3
"""Integration test: the fleet DOC STORE on the relay.

A shared shelf of named files on the relay box (h.atg.link) so an agent on one
machine can leave a plan for an agent on another — from anywhere on the web.
Its own token (never the worker/mobile token: it rides inside the `fleet-docs`
library skill). Boots a REAL relay on a tmp store and asserts:

  1. auth: every /docs/* endpoint refuses without the docs token; the worker
     token does NOT open it
  2. hostile names are fenced (traversal, dotfiles, spaces); size cap 413s
  3. put → list → get round-trips bytes, content-type by extension; put is
     an upsert and the old bytes land in .trash
  4. rm trashes (not gone), 404 on a missing doc, .trash never lists

Run: python3 fleet/test_docs_store.py
"""
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
from urllib.parse import quote

HERE = Path(__file__).resolve().parent
PORT = "8809"
BASE = f"http://127.0.0.1:{PORT}"
TOKEN = "docs-test-token"
WORKER = "docs-test-worker"
TMP = Path(tempfile.mkdtemp(prefix="clawd-docs-test."))
STORE = TMP / "docs"

ENV = {
    **os.environ,
    "FLEET_PORT": PORT,
    "FLEET_BIND": "127.0.0.1",
    "FLEET_MOBILE_TOKEN": "docs-test-mobile",
    "FLEET_WORKER_TOKEN": WORKER,
    "FLEET_DOCS_TOKEN": TOKEN,
    "FLEET_DOCS_DIR": str(STORE),
    "FLEET_MAX_DOC_BYTES": "4096",
    "FLEET_REQUIRE_PASSKEY": "0",
    "FLEET_SKILLS_DIR": str(TMP / "skills"),
    "FLEET_PREFS_FILE": str(TMP / "prefs.json"),
    "FLEET_SESSIONS_FILE": str(TMP / "sessions.json"),
    "FLEET_PUSH_SUBS_FILE": str(TMP / "push.json"),
}


def call(path, body=None, token=TOKEN, method=None):
    """→ (status, bytes, content-type)."""
    url = f"{BASE}{path}{'&' if '?' in path else '?'}t={quote(token)}"
    req = urllib.request.Request(url, method=method or ("POST" if body is not None else "GET"),
                                 data=body, headers={"Content-Type": "application/octet-stream"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read(), r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers.get("Content-Type", "")


def j(data):
    try:
        return json.loads(data.decode())
    except Exception:
        return {}


def main():
    proc = subprocess.Popen([sys.executable, "relay.py"], env=ENV, cwd=str(HERE),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.2)
    checks = []

    def check(name, cond):
        checks.append((name, bool(cond)))
        print(f"  {'✓' if cond else '✗ FAIL'} {name}")

    try:
        plan = b"# Plan\n\n1. ship the doc store\n"
        # 1. auth
        check("list refused without token", call("/docs/list", token="wrong")[0] == 403)
        check("list refused with the WORKER token", call("/docs/list", token=WORKER)[0] == 403)
        check("put refused without token", call("/docs/put?name=plan.md", plan, token="wrong")[0] == 403)
        check("get refused without token", call("/docs/get?name=plan.md", token="")[0] == 403)
        check("rm refused without token", call("/docs/rm?name=plan.md", b"", token="wrong")[0] == 403)

        # 2. fences
        code, data, _ = call("/docs/put?name=../escape.md", plan)
        check("traversal name rejected", code == 400 and "name" in j(data).get("error", ""))
        check("dotfile name rejected", call("/docs/put?name=.hidden", plan)[0] == 400)
        check("name with space rejected", call("/docs/put?name=" + quote("my plan.md"), plan)[0] == 400)
        check("empty name rejected", call("/docs/put?name=", plan)[0] == 400)
        check("oversize doc 413s", call("/docs/put?name=big.md", b"x" * 5000)[0] == 413)
        check("store dir has nothing after rejected puts",
              not STORE.exists() or not [f for f in STORE.iterdir() if f.is_file() and not f.name.startswith(".")])

        # 3. round trip
        code, data, _ = call("/docs/put?name=plan.md", plan)
        r = j(data)
        check("put accepted", code == 200 and r.get("ok") and r.get("size") == len(plan))
        code, data, _ = call("/docs/list")
        docs = j(data).get("docs") or []
        check("list shows the doc with size", code == 200 and len(docs) == 1
              and docs[0]["name"] == "plan.md" and docs[0]["size"] == len(plan))
        code, data, ctype = call("/docs/get?name=plan.md")
        check("get returns the exact bytes as text/markdown",
              code == 200 and data == plan and ctype.startswith("text/markdown"))
        blob = bytes(range(256))
        call("/docs/put?name=bits.bin", blob)
        code, data, ctype = call("/docs/get?name=bits.bin")
        check("binary round-trips as octet-stream",
              code == 200 and data == blob and ctype == "application/octet-stream")
        plan2 = plan + b"2. tell the other machine\n"
        code, data, _ = call("/docs/put?name=plan.md", plan2)
        trashed = list((STORE / ".trash").glob("plan.md-*"))
        check("put is an upsert; old bytes land in .trash",
              code == 200 and call("/docs/get?name=plan.md")[1] == plan2
              and len(trashed) == 1 and trashed[0].read_bytes() == plan)
        check("get of a missing doc 404s", call("/docs/get?name=nope.md")[0] == 404)
        check("get with a bad name 400s", call("/docs/get?name=..")[0] == 400)

        # 4. rm
        code, data, _ = call("/docs/rm?name=plan.md", b"")
        check("rm ok", code == 200 and j(data).get("removed") == "plan.md")
        check("rm trashes (recoverable, not gone)",
              len(list((STORE / ".trash").glob("plan.md-*"))) == 2
              and not (STORE / "plan.md").exists())
        check("rm of a missing doc 404s", call("/docs/rm?name=plan.md", b"")[0] == 404)
        names = [d["name"] for d in j(call("/docs/list")[1]).get("docs") or []]
        check(".trash never lists; remaining doc does", names == ["bits.bin"])
    finally:
        proc.terminate()
        proc.wait(timeout=5)
        shutil.rmtree(TMP, ignore_errors=True)

    failed = [n for n, ok in checks if not ok]
    print("PASS — doc store: auth, fences, round-trip, upsert→trash, rm→trash"
          if not failed else f"FAIL — {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
