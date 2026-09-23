#!/usr/bin/env python3
"""Integration test: ☑ per-iron to-do lists on the relay (fleet mode).

The relay owns the lists (they span machines, like irons). Asserts what the
page and `harness-todo` depend on:
  1. A `todos` snapshot follows every `prefs` on connect (the page's first
     paint has the lists next to the irons they belong to).
  2. A `todo` op from one mobile is applied item-level, echoed to EVERY authed
     mobile as a fresh snapshot, and survives to disk (0600, own file).
  3. A refused op (unknown iron / empty text) answers the sender with an
     `error` frame and writes nothing.
  4. Deleting an iron (a prefs write without it) prunes its list.
  5. `/todo/agent` (worker-token gated) resolves a session's project to its
     iron through the key fold: exact projectKey, the `name:` form, and the
     URL↔name basename fold; a project in no iron is told so; list/add/done
     reply the CLI lines; `order` is refused (403); a bad token is denied.

Run: python3 fleet/test_relay_todos.py
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

import fleet_ws

HERE = Path(__file__).resolve().parent
PORT = "8813"
RELAY = f"ws://127.0.0.1:{PORT}"
HTTP = f"http://127.0.0.1:{PORT}"
TOKEN = "todos-test-token"
WTOKEN = "todos-test-worker"
TMP = Path(tempfile.gettempdir())
PREFS = TMP / "clawd-fleet-test-todos.prefs.json"
TODOS = TMP / "clawd-fleet-test-todos.todos.json"

ENV = {
    **os.environ,
    "FLEET_PORT": PORT,
    "FLEET_BIND": "127.0.0.1",
    "FLEET_MOBILE_TOKEN": TOKEN,
    "FLEET_WORKER_TOKEN": WTOKEN,
    "FLEET_REQUIRE_PASSKEY": "0",
    "FLEET_PREFS_FILE": str(PREFS),
    "FLEET_TODOS_FILE": str(TODOS),
    "FLEET_SESSIONS_FILE": str(TMP / "clawd-fleet-test-todos.sessions.json"),
    "FLEET_PUSH_SUBS_FILE": str(TMP / "clawd-fleet-test-todos.push.json"),
    "FLEET_DOCS_DIR": str(TMP / "clawd-fleet-test-todos.docs"),
}
FAILS = []


def check(name, ok, detail=""):
    print(f"  {'✓' if ok else '✗ FAIL'} {name}" + ("" if ok or not detail else f" — {detail}"))
    if not ok:
        FAILS.append(name)


def dial():
    sock, rfile, wfile = fleet_ws.client_connect(f"{RELAY}/ws?role=mobile&t={quote(TOKEN)}")
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


def agent(body, token=WTOKEN):
    req = urllib.request.Request(f"{HTTP}/todo/agent?t={quote(token)}", data=json.dumps(body).encode(),
                                 method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}


def main():
    for f in (PREFS, TODOS):
        f.unlink(missing_ok=True)
    proc = subprocess.Popen([sys.executable, "relay.py"], env=ENV, cwd=str(HERE),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.2)
    try:
        a_sock, a_send, a_in = dial()
        b_sock, b_send, b_in = dial()
        # 1. snapshot follows prefs on connect
        f = wait_for(a_in, lambda m: m.get("type") == "todos")
        types = [m.get("type") for m in a_in]
        check("todos snapshot arrives on connect, right after prefs",
              f is not None and f.get("todos") == {} and types.index("prefs") < types.index("todos") < types.index("machines"),
              str(types))
        # an iron to hang lists on (the same gh repo keyed by URL on one, by name on another)
        irons = [{"id": "i1", "title": "voice", "desc": "", "tags": [],
                  "keys": ["github.com/clawdbotatg/gpt-voice", "name:hud"], "created": 1},
                 {"id": "i2", "title": "other", "desc": "", "tags": [], "keys": ["local:box-a:/Users/x/priv"], "created": 2}]
        a_send({"type": "prefs", "irons": irons})
        wait_for(b_in, lambda m: m.get("type") == "prefs" and len(m.get("irons") or []) == 2)
        # 2. an op from A lands on B as a snapshot + on disk
        a_in.clear(); b_in.clear()
        a_send({"type": "todo", "op": "add", "iron": "i1", "text": "  test the mic on the phone  ", "key": "github.com/clawdbotatg/gpt-voice"})
        fb = wait_for(b_in, lambda m: m.get("type") == "todos" and m["todos"].get("i1"))
        fa = wait_for(a_in, lambda m: m.get("type") == "todos" and m["todos"].get("i1"))
        item = fb and fb["todos"]["i1"][0]
        check("add echoes a snapshot to EVERY authed mobile (sender included)",
              fa is not None and fb is not None and item["text"] == "test the mic on the phone" and item["via"] == "page"
              and item["key"] == "github.com/clawdbotatg/gpt-voice" and not item["done"], str(fb))
        disk = json.loads(TODOS.read_text())
        check("persisted to its own file, mode 0600",
              disk.get("i1", [{}])[0].get("text") == "test the mic on the phone" and (TODOS.stat().st_mode & 0o777) == 0o600)
        # done from B, seen by A
        a_in.clear()
        b_send({"type": "todo", "op": "done", "iron": "i1", "id": item["id"]})
        fa = wait_for(a_in, lambda m: m.get("type") == "todos" and m["todos"].get("i1", [{}])[0].get("done"))
        check("done from the other device lands here", fa is not None and fa["todos"]["i1"][0]["doneAt"] > 0)
        # 3. refused ops → error to the sender, no snapshot
        a_in.clear(); b_in.clear()
        a_send({"type": "todo", "op": "add", "iron": "ghost", "text": "x"})
        err = wait_for(a_in, lambda m: m.get("type") == "error" and "todo:" in m.get("error", ""))
        check("unknown iron → error frame to the sender", err is not None and "no such iron" in err["error"], str(err))
        a_send({"type": "todo", "op": "add", "iron": "i1", "text": "   "})
        err = wait_for(a_in, lambda m: m.get("type") == "error" and "empty" in m.get("error", ""))
        time.sleep(0.3)
        check("empty text → error, and nobody got a snapshot", err is not None and not any(m.get("type") == "todos" for m in b_in))
        # 5. the agent route
        st, rep = agent({"op": "list", "machine": "box-a", "project": {"name": "nothing", "repoUrl": "https://github.com/x/nothing", "kind": "gh"}})
        check("agent: a project in no iron is told so", st == 200 and rep["ok"] is False and rep["iron"] is None and "isn't in any iron" in rep["msg"], str(rep))
        st, rep = agent({"op": "list", "all": True, "machine": "box-b",
                         "project": {"name": "gpt-voice", "repoUrl": "git@github.com:clawdbotatg/gpt-voice.git", "kind": "gh"}})
        check("agent: exact key via the normRepo fold (ssh form → stored https form), list --all",
              st == 200 and rep["ok"] and rep["iron"]["id"] == "i1" and "[x]" in rep["msg"], str(rep))
        st, rep = agent({"op": "add", "text": "wire the hud", "machine": "box-b",
                         "project": {"name": "hud", "repoUrl": "", "kind": "gh"}})
        check("agent: the name: form resolves; add keyed by the project's key, via agent",
              st == 200 and rep["ok"] and rep["iron"]["id"] == "i1" and rep["item"]["key"] == "name:hud" and rep["item"]["via"] == "agent", str(rep))
        st, rep = agent({"op": "add", "text": "from the url side", "machine": "box-c",
                         "project": {"name": "hud", "repoUrl": "https://github.com/someone/hud", "kind": "gh"}})
        check("agent: URL-keyed project ↔ name:-keyed member folds by basename",
              st == 200 and rep["ok"] and rep["iron"]["id"] == "i1", str(rep))
        st, rep = agent({"op": "list", "machine": "box-a", "project": {"name": "priv", "path": "/Users/x/priv", "kind": "local"}})
        check("agent: a local project resolves by its machine-qualified key", st == 200 and rep["ok"] and rep["iron"]["id"] == "i2", str(rep))
        st, rep = agent({"op": "list", "machine": "box-b", "project": {"name": "priv", "path": "/Users/x/priv", "kind": "local"}})
        check("agent: the same folder on ANOTHER machine is a different project", st == 200 and rep["ok"] is False, str(rep))
        st, rep = agent({"op": "done", "ref": "the", "machine": "box-b", "project": {"name": "hud", "kind": "gh"}})
        check("agent: ambiguous words refused with the count", st == 200 and rep["ok"] is False and "3 items match" in rep["msg"], str(rep))
        st, rep = agent({"op": "done", "ref": "wire the hud", "machine": "box-b", "project": {"name": "hud", "kind": "gh"}})
        check("agent: done by words", st == 200 and rep["ok"] and rep["msg"].startswith("done ["), str(rep))
        st, rep = agent({"op": "order", "ids": [], "machine": "box-b", "project": {"name": "hud", "kind": "gh"}})
        check("agent: order refused", st == 403, str(rep))
        st, _ = agent({"op": "list", "machine": "box-b", "project": {"name": "hud"}}, token="wrong")
        check("agent: bad worker token denied", st == 403)
        a_in.clear()
        st, rep = agent({"op": "add", "text": "seen on the phone?", "machine": "box-b", "project": {"name": "hud", "kind": "gh"}})
        fa = wait_for(a_in, lambda m: m.get("type") == "todos" and any(i["text"] == "seen on the phone?" for i in m["todos"].get("i1", [])))
        check("an agent add is broadcast to the mobiles live", fa is not None)
        # 4. deleting the iron prunes its list
        a_in.clear()
        a_send({"type": "prefs", "irons": [irons[1]]})
        fa = wait_for(a_in, lambda m: m.get("type") == "todos" and "i1" not in m["todos"])
        check("a prefs write without the iron prunes its list (+ snapshot)", fa is not None and "i1" not in json.loads(TODOS.read_text()))
        a_sock.close(); b_sock.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except Exception:
            proc.kill()
    print("\nOK" if not FAILS else f"\nFAILED: {FAILS}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
