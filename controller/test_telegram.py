#!/usr/bin/env python3
"""Telegram bridge test — against a mock Bot API (never touches a real bot).

Spins a tiny HTTP server that mimics api.telegram.org's getMe/sendMessage, then
asserts: getMe round-trips, allowlisted messages route to the brain and a reply
is sent, non-allowlisted senders are refused, and notify() pushes to the allow
set. Routing (_handle) is tested directly; the HTTP path (_api) against the mock.

Run: python3 -m controller.test_telegram
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler
from socketserver import TCPServer, ThreadingMixIn

from .telegram import TelegramBridge

PORT = 8901


class _Mock(ThreadingMixIn, TCPServer):
    daemon_threads = True
    allow_reuse_address = True


sent = []        # captured sendMessage calls


def handler():
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            method = self.path.rsplit("/", 1)[-1]
            n = int(self.headers.get("Content-Length", "0") or 0)
            body = self.rfile.read(n).decode() if n else ""
            params = dict(p.split("=", 1) for p in body.split("&") if "=" in p)
            if method == "getMe":
                out = {"ok": True, "result": {"username": "mock_pm_bot"}}
            elif method == "sendMessage":
                sent.append(params)
                out = {"ok": True, "result": {}}
            else:
                out = {"ok": True, "result": []}
            data = json.dumps(out).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    return H


class FakeRouter:
    def __init__(self):
        self.seen = []

    def chat(self, text):
        self.seen.append(text)
        return {"reply": f"ack: {text}", "trace": [{"tool": "get_world"}]}

    def reset(self):
        self.seen.clear()


def main():
    failures = []

    def check(name, fn):
        try:
            fn()
            print(f"  ✓ {name}")
        except Exception as e:
            print(f"  ✗ {name}: {e}")
            failures.append(name)

    srv = _Mock(("127.0.0.1", PORT), handler())
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{PORT}"
    router = FakeRouter()
    bridge = TelegramBridge("faketoken", ["672968601"], router, base_url=base)

    def t_getme():
        me = bridge.get_me()
        assert me["result"]["username"] == "mock_pm_bot", me
    check("getMe round-trips through _api", t_getme)

    def t_allowed_routes_to_brain():
        sent.clear()
        bridge._handle({"chat": {"id": 672968601}, "from": {"id": 672968601},
                        "text": "anything need me?"})
        assert router.seen and router.seen[-1] == "anything need me?", router.seen
        assert sent and "ack%3A" in sent[-1].get("text", "") or "ack" in sent[-1].get("text", ""), sent
    check("allowlisted message → brain → reply sent", t_allowed_routes_to_brain)

    def t_denied():
        sent.clear()
        bridge._handle({"chat": {"id": 999}, "from": {"id": 999}, "text": "hi"})
        assert sent and "authorized" in sent[-1].get("text", ""), sent
    check("non-allowlisted sender is refused", t_denied)

    def t_commands():
        sent.clear()
        before = len(router.seen)
        bridge._handle({"chat": {"id": 672968601}, "from": {"id": 672968601}, "text": "/start"})
        assert sent and "Fleet" in sent[-1].get("text", ""), sent     # url-encoded body
        assert len(router.seen) == before, "/start must not hit the brain"
    check("/start is handled without hitting the brain", t_commands)

    def t_notify():
        sent.clear()
        bridge.notify("⏳ needs you — self/abcd: blocked")
        assert sent and "needs+you" in sent[-1].get("text", "") or "needs" in sent[-1].get("text", ""), sent
    check("notify() pushes to the allow set", t_notify)

    srv.shutdown()
    def t_turn_lock():
        # Telegram used to call the router bare: a phone message could run
        # while an HTTP or autopilot turn held the one turn lock.
        lock = threading.Lock()
        r2 = FakeRouter()
        b2 = TelegramBridge("faketoken", ["672968601"], r2, base_url=base, turn_lock=lock)
        msg = lambda text: {"chat": {"id": 672968601}, "from": {"id": 672968601}, "text": text}
        lock.acquire()                           # another turn is running
        th = threading.Thread(target=b2._handle, args=(msg("while busy"),), daemon=True)
        th.start()
        time.sleep(0.4)
        held_back = not r2.seen
        lock.release()
        th.join(5)
        assert held_back, "the brain ran while another turn held the lock"
        assert r2.seen == ["while busy"], r2.seen
        r2.seen.append("stale")
        lock.acquire()
        th = threading.Thread(target=b2._handle, args=(msg("/reset"),), daemon=True)
        th.start()
        time.sleep(0.4)
        still = list(r2.seen)
        lock.release()
        th.join(5)
        assert still and not r2.seen, (still, r2.seen)
    check("a Telegram turn (and /reset) waits for the shared turn lock", t_turn_lock)

    print()
    if failures:
        print(f"FAILED: {failures}")
        return 1
    print("PASSED: Telegram bridge (mock Bot API)")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
