#!/usr/bin/env python3
"""End-to-end smoke test for the HARNESS-PROXY loop (roadmap #1+#2).

  mobile → relay → worker → (mock) harness → worker → relay → mobile

Spins up a relay, a tiny **mock harness** that speaks the WS-PROTOCOL.md
contract (so no real `claude` is needed), and a worker bridging the two. Then,
as a scripted mobile, it runs the doc's "send a prompt, get the answer" flow and
asserts every hop — including a **binary PTY frame** tunneled all the way back
(proving opcode 0x2 routing). Exits non-zero on failure.

Run:  python3 fleet_proxy_smoke.py
"""
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler
from socketserver import TCPServer, ThreadingMixIn
from urllib.parse import quote, urlparse, parse_qs

import fleet_ws

HERE = os.path.dirname(os.path.abspath(__file__))
RELAY_PORT = "8798"
HARNESS_PORT = 8786
TOKEN = "smoketoken"
HARNESS_TOKEN = "harnesstoken"
RELAY = f"ws://127.0.0.1:{RELAY_PORT}"
MACHINE = "mockbox"

ENV = {**os.environ, "FLEET_PORT": RELAY_PORT, "FLEET_TOKEN": TOKEN,
       "FLEET_REQUIRE_PASSKEY": "0",  # routing smoke: 2nd factor off (covered by test_relay_passkey)
       "FLEET_E2E_REQUIRE": "0",      # transport smoke: E2E off (covered by test_e2e* + interop)
       "FLEET_RELAY": RELAY, "FLEET_BIND": "127.0.0.1"}

procs = []
PTY_SNAPSHOT = b"\x1b[2J\x1b[Hmock-pty-snapshot\r\n"


# ── mock harness (a minimal WS server speaking WS-PROTOCOL.md) ────────────────
class HarnessHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        # mock the harness's /upload: read the body, return a {path,name} like the
        # real harness does (it saves the bytes and hands back a local path).
        q = parse_qs(urlparse(self.path).query)
        if urlparse(self.path).path != "/upload" or q.get("t", [""])[0] != HARNESS_TOKEN:
            return self.send_error(403, "bad token")
        n = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(n) if n else b""
        payload = json.dumps({"path": f"/tmp/harness-uploads/img-{len(body)}.png",
                              "name": "img.png"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        q = parse_qs(urlparse(self.path).query)
        if urlparse(self.path).path != "/ws" or q.get("t", [""])[0] != HARNESS_TOKEN:
            return self.send_error(403, "bad token")
        key = self.headers.get("Sec-WebSocket-Key", "")
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", fleet_ws.server_accept_headers(key))
        self.end_headers()
        self.close_connection = True
        lock = threading.Lock()

        def j(obj):
            fleet_ws.ws_send(self.wfile, lock, json.dumps(obj), opcode=0x1)

        def b(data):
            fleet_ws.ws_send(self.wfile, lock, data, opcode=0x2)

        # on connect: projects + sessions (every client gets these immediately)
        j({"type": "projects", "boot": "mockboot",
           "projects": [{"pid": "p1", "name": "demo", "status": "ready"}]})
        j({"type": "sessions", "current": None,
           "sessions": [{"cid": "c1", "pid": "p1", "title": "demo", "busy": False}]})
        try:
            while True:
                msg = fleet_ws.ws_read_message(self.rfile)
                if msg is None:
                    break
                kind, data = msg
                if kind == "close":
                    break
                if kind == "ping":
                    fleet_ws.ws_send(self.wfile, lock, data, opcode=0xA)
                    continue
                if kind == "pong" or kind == 0x2:
                    continue
                try:
                    f = json.loads(data.decode())
                except Exception:
                    continue
                t = f.get("type")
                cid = f.get("cid", "c1")
                if t == "list":
                    j({"type": "projects", "boot": "mockboot",
                       "projects": [{"pid": "p1", "name": "demo", "status": "ready"}]})
                    j({"type": "sessions", "current": None,
                       "sessions": [{"cid": "c1", "pid": "p1", "title": "demo",
                                     "busy": False}]})
                elif t == "subscribe":
                    b(PTY_SNAPSHOT)                       # ring-buffer snapshot
                    j({"type": "hello", "cid": cid, "pid": "p1", "sessionId": "s1",
                       "title": "demo", "workdir": "/x", "busy": False, "tool": None,
                       "cols": 80, "rows": 24})
                    j({"type": "transcript", "cid": cid, "history": True,
                       "event": {"role": "user", "text": "earlier message"}})
                elif t == "send":
                    text = f.get("text", "")
                    j({"type": "hook", "cid": cid, "event": "UserPromptSubmit",
                       "busy": True, "tool": None, "data": {"prompt": text}})
                    b(b"\x1b[32m" + text.encode() + b"\x1b[0m\r\n")  # live PTY echo
                    j({"type": "hook", "cid": cid, "event": "Stop", "busy": False,
                       "tool": None, "data": {"last": "echo: " + text}})
        except Exception:
            pass


class ThreadingHTTPServer(ThreadingMixIn, TCPServer):
    daemon_threads = True
    allow_reuse_address = True


def start_mock_harness():
    srv = ThreadingHTTPServer(("127.0.0.1", HARNESS_PORT), HarnessHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ── process helpers ───────────────────────────────────────────────────────────
def spawn(args):
    p = subprocess.Popen([sys.executable, *args], env=ENV, cwd=HERE,
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
    start_mock_harness()
    spawn(["relay.py"])
    time.sleep(1.0)
    spawn(["worker.py", "--machine", MACHINE, "--host", "mock-host",
           "--harness", f"ws://127.0.0.1:{HARNESS_PORT}",
           "--harness-token", HARNESS_TOKEN])
    time.sleep(1.5)

    url = f"{RELAY}/ws?role=mobile&t={quote(TOKEN)}"
    sock, rfile, wfile = fleet_ws.client_connect(url)
    lock = threading.Lock()
    inbox = []          # decoded JSON machineMsg frames (and roster/error)
    pty_frames = []     # (machine_id, payload) from tunneled binary frames

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
                if kind == "close":
                    return
                continue
            if kind == 0x2:  # tunneled PTY: [len][machine][payload]
                n = data[0]
                pty_frames.append((data[1:1 + n].decode(), data[1 + n:]))
                continue
            try:
                inbox.append(json.loads(data.decode()))
            except Exception:
                pass

    threading.Thread(target=reader, daemon=True).start()

    def send(obj):
        fleet_ws.ws_send(wfile, lock, json.dumps(obj), opcode=0x1, mask=True)

    def to_machine(msg):
        send({"type": "toMachine", "machine": MACHINE, "msg": msg})

    def wait_for(pred, timeout=6.0, what=""):
        end = time.time() + timeout
        while time.time() < end:
            for f in list(inbox):
                if pred(f):
                    return f
            time.sleep(0.05)
        raise AssertionError(f"timeout waiting for {what}; inbox={inbox}")

    def wait_pty(pred, timeout=6.0, what=""):
        end = time.time() + timeout
        while time.time() < end:
            for f in list(pty_frames):
                if pred(f):
                    return f
            time.sleep(0.05)
        raise AssertionError(f"timeout waiting for {what}; pty={pty_frames}")

    failures = []

    def check(name, fn):
        try:
            fn()
            print(f"  ✓ {name}")
        except Exception as e:
            print(f"  ✗ {name}: {e}")
            failures.append(name)

    def is_msg(f, **kv):
        if f.get("type") != "machineMsg" or f.get("machine") != MACHINE:
            return False
        m = f.get("msg") or {}
        return all(m.get(k) == v for k, v in kv.items())

    # 1. roster shows the mock machine
    def t_roster():
        send({"type": "list"})
        wait_for(lambda f: f.get("type") == "machines"
                 and any(m["id"] == MACHINE and m["online"] for m in f["machines"]),
                 what="roster with mockbox")
    check("roster discovery (mock machine online)", t_roster)

    # 1b. the worker reports plaintext aggregate counts → roster carries stats
    #     (mock harness = 1 project, 1 idle session). Wait for the roster the
    #     relay re-broadcasts once the worker's stats frame lands.
    def t_stats():
        def has_stats(f):
            if f.get("type") != "machines":
                return False
            m = next((x for x in f["machines"] if x["id"] == MACHINE), None)
            return bool(m and m.get("stats"))
        f = wait_for(has_stats, what="roster entry with stats")
        st = next(x for x in f["machines"] if x["id"] == MACHINE)["stats"]
        # Counts are exact; `sys` (CPU/RAM/disk/GPU) rides along on real machines but
        # is best-effort/volatile, so assert the counts subset rather than equality.
        assert {k: st.get(k) for k in ("projects", "sessions", "active")} == \
            {"projects": 1, "sessions": 1, "active": 0}, st
        if "sys" in st:   # if present, it must be a dict of metric blocks
            assert isinstance(st["sys"], dict), st
    check("worker reports aggregate counts → roster stats", t_stats)

    # 2. harness `list` → projects + sessions tunnel back as machineMsgs
    def t_list():
        inbox.clear()
        to_machine({"type": "list"})
        wait_for(lambda f: is_msg(f, type="projects"), what="projects from harness")
        wait_for(lambda f: is_msg(f, type="sessions"), what="sessions from harness")
    check("harness list → projects+sessions proxied", t_list)

    # 3. subscribe → hello + a tunneled BINARY PTY snapshot tagged with the machine
    def t_subscribe():
        inbox.clear()
        pty_frames.clear()
        to_machine({"type": "subscribe", "cid": "c1"})
        wait_for(lambda f: is_msg(f, type="hello", cid="c1"), what="hello for c1")
        wait_for(lambda f: is_msg(f, type="transcript"), what="transcript history")
        snap = wait_pty(lambda p: p[0] == MACHINE and p[1] == PTY_SNAPSHOT,
                        what="binary PTY snapshot tagged with machine")
        assert snap[1] == PTY_SNAPSHOT
    check("subscribe → hello + binary PTY snapshot tunneled (opcode 0x2)", t_subscribe)

    # 4. send a prompt → hook Stop carries the answer, plus a live PTY echo
    def t_send():
        inbox.clear()
        pty_frames.clear()
        to_machine({"type": "send", "cid": "c1", "text": "ping-prompt"})
        wait_for(lambda f: is_msg(f, type="hook", event="UserPromptSubmit"),
                 what="UserPromptSubmit hook (busy)")
        stop = wait_for(lambda f: is_msg(f, type="hook", event="Stop"),
                        what="Stop hook (the answer)")
        assert stop["msg"]["data"]["last"] == "echo: ping-prompt", stop
        wait_pty(lambda p: b"ping-prompt" in p[1], what="live PTY echo of the prompt")
    check("send → live PTY + hook Stop with the answer", t_send)

    # 5. image upload bridges HTTP POST → worker → harness /upload → {path,name}
    def t_upload():
        url = f"http://127.0.0.1:{RELAY_PORT}/upload?t={quote(TOKEN)}&machine={MACHINE}"
        req = urllib.request.Request(url, data=b"\x89PNG\r\n\x1a\nfakeimagebytes",
                                     method="POST", headers={"Content-Type": "image/png"})
        with urllib.request.urlopen(req, timeout=10) as r:
            j = json.loads(r.read().decode())
        assert j.get("path", "").startswith("/tmp/harness-uploads/"), j
        assert j.get("name") == "img.png", j
    check("image upload bridges HTTP→worker→harness→{path,name}", t_upload)

    # 6. unknown machine still errors (relay routing intact)
    def t_unknown():
        inbox.clear()
        send({"type": "toMachine", "machine": "ghost", "msg": {"type": "list"}})
        wait_for(lambda f: f.get("type") == "error", what="error for unknown machine")
    check("unknown machine returns a relay error", t_unknown)

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s): {failures}")
        return 1
    print("PASSED: mobile → relay → worker → harness → back, incl. binary PTY")
    return 0


if __name__ == "__main__":
    rc = 1
    try:
        rc = main()
    finally:
        cleanup()
    sys.exit(rc)
