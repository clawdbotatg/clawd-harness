#!/usr/bin/env python3
"""End-to-end smoke test for the fleet prototype: relay + 2 workers + 1 mobile.

Spawns everything as subprocesses on localhost, then as a scripted mobile:
roster discovery, ping both, exec on one, exec on all (@*). Asserts the
replies routed back correctly. Exits non-zero on failure.
"""
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import fleet_ws

HERE = Path(__file__).resolve().parent
PORT = "8799"            # dedicated test port
MOBILE_TOKEN = "mobile-smoketoken"   # split tokens — exercise the auth model
WORKER_TOKEN = "worker-smoketoken"
RELAY = f"ws://127.0.0.1:{PORT}"
ENV = {**os.environ, "FLEET_PORT": PORT,
       "FLEET_MOBILE_TOKEN": MOBILE_TOKEN, "FLEET_WORKER_TOKEN": WORKER_TOKEN,
       "FLEET_WORKER_ALLOW": "alpha,beta",   # gamma below is intentionally not listed
       "FLEET_REQUIRE_PASSKEY": "0",  # routing smokes: 2nd factor off
       "FLEET_RELAY": RELAY, "FLEET_BIND": "127.0.0.1",
       "FLEET_ALLOW_EXEC": "1"}   # this suite exercises the exec diagnostic

procs = []


def spawn(args):
    p = subprocess.Popen([sys.executable, *args], env=ENV, cwd=str(HERE),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    procs.append(p)
    return p


def cleanup():
    for p in procs:
        try:
            p.send_signal(signal.SIGTERM)
        except Exception:
            pass


def main():
    spawn(["relay.py"])
    time.sleep(1.0)
    spawn(["worker.py", "--machine", "alpha", "--host", "alpha-host"])
    spawn(["worker.py", "--machine", "beta", "--host", "beta-host"])
    spawn(["worker.py", "--machine", "gamma", "--host", "gamma-host"])  # NOT allowlisted → must be rejected
    time.sleep(1.5)

    from urllib.parse import quote
    url = f"{RELAY}/ws?role=mobile&t={quote(MOBILE_TOKEN)}"
    sock, rfile, wfile = fleet_ws.client_connect(url)
    lock = threading.Lock()
    inbox = []
    done = threading.Event()

    def reader():
        while True:
            msg = fleet_ws.ws_read_message(rfile)
            if msg is None:
                done.set()
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

    def wait_for(pred, timeout=6.0, what=""):
        end = time.time() + timeout
        while time.time() < end:
            for f in list(inbox):
                if pred(f):
                    return f
            time.sleep(0.05)
        raise AssertionError(f"timeout waiting for {what}; inbox={inbox}")

    failures = []

    def check(name, fn):
        try:
            fn()
            print(f"  ✓ {name}")
        except Exception as e:
            print(f"  ✗ {name}: {e}")
            failures.append(name)

    # 1. roster shows both workers
    def t_roster():
        send({"type": "list"})
        f = wait_for(lambda f: f.get("type") == "machines"
                     and {m["id"] for m in f["machines"]} >= {"alpha", "beta"},
                     what="roster with alpha+beta")
        assert all(m["online"] for m in f["machines"])
    check("roster discovery (both workers online)", t_roster)

    # 1b. the non-allowlisted worker (gamma) must never register
    def t_allowlist():
        f = wait_for(lambda f: f.get("type") == "machines", what="roster")
        ids = {m["id"] for m in f["machines"]}
        assert "gamma" not in ids, f"non-allowlisted worker registered: {ids}"
    check("worker allowlist rejects gamma", t_allowlist)

    # 1c. a mobile presenting the WORKER token is rejected at the handshake
    def t_wrongtoken():
        try:
            s, _, _ = fleet_ws.client_connect(f"{RELAY}/ws?role=mobile&t={quote(WORKER_TOKEN)}")
            s.close()
            raise AssertionError("mobile accepted the worker token")
        except ConnectionError:
            pass   # 403 → handshake fails, as it should
    check("wrong-role token rejected at handshake", t_wrongtoken)

    # 2. ping alpha → pong from alpha
    def t_ping():
        inbox.clear()
        send({"type": "toMachine", "machine": "alpha", "msg": {"kind": "ping"}})
        f = wait_for(lambda f: f.get("type") == "machineMsg"
                     and f["machine"] == "alpha"
                     and f["msg"].get("kind") == "pong", what="pong from alpha")
        assert f["msg"]["host"] == "alpha-host"
    check("ping routes to one machine and back", t_ping)

    # 3. exec on beta → streamed output + exit 0
    def t_exec():
        inbox.clear()
        send({"type": "toMachine", "machine": "beta",
              "msg": {"kind": "exec", "cmd": "echo hello-from-beta"}})
        out = wait_for(lambda f: f.get("type") == "machineMsg"
                       and f["machine"] == "beta"
                       and f["msg"].get("kind") == "output"
                       and "hello-from-beta" in f["msg"].get("data", ""),
                       what="exec output from beta")
        wait_for(lambda f: f.get("type") == "machineMsg"
                 and f["machine"] == "beta"
                 and f["msg"].get("kind") == "exit"
                 and f["msg"].get("code") == 0, what="exec exit 0 from beta")
    check("exec streams output + exit from one machine", t_exec)

    # 4. fan-out @* → both machines reply
    def t_fanout():
        inbox.clear()
        send({"type": "toMachine", "machine": "*",
              "msg": {"kind": "exec", "cmd": "hostname"}})
        wait_for(lambda f: f.get("type") == "machineMsg" and f["machine"] == "alpha"
                 and f["msg"].get("kind") == "exit", what="alpha exit")
        wait_for(lambda f: f.get("type") == "machineMsg" and f["machine"] == "beta"
                 and f["msg"].get("kind") == "exit", what="beta exit")
    check("fan-out @* reaches all machines", t_fanout)

    # 5. unknown machine → relay error
    def t_unknown():
        inbox.clear()
        send({"type": "toMachine", "machine": "ghost", "msg": {"kind": "ping"}})
        wait_for(lambda f: f.get("type") == "error", what="error for unknown machine")
    check("unknown machine returns a relay error", t_unknown)

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s): {failures}")
        return 1
    print("PASSED: full mobile → relay → machine → back loop works")
    return 0


if __name__ == "__main__":
    rc = 1
    try:
        rc = main()
    finally:
        cleanup()
    sys.exit(rc)
