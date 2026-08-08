#!/usr/bin/env python3
"""Regression test: the PM tab stays usable while a turn is thinking.

The bug this pins down: `/api/thread/new|select|archive` used to run `with
chat_lock:` — the SAME lock a brain turn holds for its entire duration. A PM turn
runs for minutes, so for those minutes ＋ new, every thread tab, and ✕ archive
were dead. Clicks didn't error, they *hung*, which read as "the whole PM tab is
frozen" — and each hung click also consumed one of the browser's ~6 connections
to the origin, so after a handful of clicks the rest of the page stalled too.

The contract now: only actual turns serialize. Thread bookkeeping answers
immediately mid-turn, and `/api/thread/clear` — which drops brain memory a
running turn is writing — refuses fast with 409 rather than blocking.

Run:  python3 -m controller.test_pm_responsive
"""
import json
import threading
import time
import urllib.error
import urllib.request

from .chat_server import ThreadingHTTPServer, make_handler

TURN_SECS = 3.0          # stands in for a multi-minute brain turn
SLACK = 1.0              # a freed endpoint must answer well inside the turn


class SlowRouter:
    """A router whose chat() blocks like a real turn; everything else is instant."""

    def __init__(self):
        self.turn_started = threading.Event()

    def chat(self, message):
        self.turn_started.set()
        time.sleep(TURN_SECS)
        return {"reply": "done", "trace": []}

    def list_threads(self):            return {"threads": [], "current": "t1"}
    def thread_messages(self, tid=None): return {"messages": []}
    def new_thread(self, title=None):  return {"threads": [], "current": "t2"}
    def select_thread(self, tid):      return {"ok": True, "current": tid}
    def archive_thread(self, tid=None): return {"threads": [], "current": "t1"}
    def clear_thread(self, tid=None):  return {"threads": [], "current": "t1"}


class StubVerbs:
    def get_world(self):
        return {"machines": [], "attention_count": 0}


class StubGuard:
    autonomy = "auto"


def main():
    failures = []

    def check(name, fn):
        try:
            fn()
            print(f"  ✓ {name}")
        except Exception as e:
            print(f"  ✗ {name}: {e}")
            failures.append(name)

    router = SlowRouter()
    srv = ThreadingHTTPServer(("127.0.0.1", 0),
                              make_handler(router, StubVerbs(), StubGuard(),
                                           lambda: "bankr"))
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def call(path, body=None, timeout=TURN_SECS + 5):
        """→ (status, parsed body, seconds elapsed)."""
        req = urllib.request.Request(
            base + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json"},
            method="POST" if body is not None else "GET")
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.loads(r.read() or b"{}"), time.time() - t0
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}"), time.time() - t0

    # Start a turn and wait until the brain is genuinely inside it.
    turn = {}
    threading.Thread(
        target=lambda: turn.update(zip(("code", "body", "secs"),
                                       call("/api/chat", {"message": "hi"}))),
        daemon=True).start()
    assert router.turn_started.wait(5), "turn never started"
    time.sleep(0.1)                       # let chat_lock actually be held

    def t_thread_ops_are_instant():
        for path, body in (("/api/thread/new", {}),
                           ("/api/thread/select", {"id": "t1"}),
                           ("/api/thread/archive", {"id": "t1"})):
            code, _, secs = call(path, body)
            assert code == 200, f"{path} → {code}"
            assert secs < SLACK, f"{path} blocked {secs:.2f}s on the in-flight turn"
    check("＋ new / select / archive answer instantly mid-turn", t_thread_ops_are_instant)

    def t_reads_are_instant():
        for path in ("/api/threads", "/api/thread/messages", "/api/state"):
            code, _, secs = call(path)
            assert code == 200, f"{path} → {code}"
            assert secs < SLACK, f"{path} blocked {secs:.2f}s"
    check("thread list / messages / state stay readable mid-turn", t_reads_are_instant)

    def t_clear_refuses_fast():
        code, body, secs = call("/api/thread/clear", {})
        assert code == 409, f"expected 409 while a turn runs, got {code}"
        assert body.get("error"), "409 must explain itself to the UI"
        assert secs < SLACK, f"clear blocked {secs:.2f}s instead of refusing"
    check("clear refuses (409) mid-turn instead of hanging", t_clear_refuses_fast)

    # Let the turn finish, then confirm nothing above broke it.
    deadline = time.time() + TURN_SECS + 5
    while "code" not in turn and time.time() < deadline:
        time.sleep(0.05)

    def t_turn_survived():
        assert turn.get("code") == 200, f"turn returned {turn.get('code')}"
        assert turn["body"]["reply"] == "done", turn["body"]
    check("the in-flight turn completes normally", t_turn_survived)

    def t_clear_allowed_once_idle():
        code, _, _ = call("/api/thread/clear", {})
        assert code == 200, f"clear should work when idle, got {code}"
    check("clear works once the turn is done", t_clear_allowed_once_idle)

    srv.shutdown()
    print()
    if failures:
        print(f"FAILED: {len(failures)} — " + ", ".join(failures))
        raise SystemExit(1)
    print("PASSED: PM controls stay live during a turn")


if __name__ == "__main__":
    main()
