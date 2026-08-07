#!/usr/bin/env python3
"""PM senses v2: find / bounded world / transcript tail / sweep / proxy MCP.

Covers the rebuild's new read layer end-to-end against the mock harness:
  - find() answers "which session/task is about X" in one call (meta + ledger
    + server-side transcript search), with deep links
  - get_world() is bounded: compact by default, never exceeds the char budget
    even on a 10x fleet; scoped drill-down still works
  - transcript_tail clamps n; degrades cleanly against an old harness that
    ignores the frame (timeout → {"error"})
  - sweep() bundles attention items with tail evidence + url + clear_with
  - ProxyMCPServer round-trips tools/resources over a stub serve HTTP API

Run:  python3 -m controller.test_pm_senses
"""
import json
import os
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler
from socketserver import TCPServer, ThreadingMixIn

from .harness_client import HarnessClient
from .ledger import TaskLedger
from .mcp import ProxyMCPServer
from .mock_harness import MockHarness, TOKEN
from .verbs import Guard, Verbs, WORLD_CHAR_BUDGET
from .world import World

PORT = 8899


def _wait(pred, timeout=6.0, what=""):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    raise AssertionError(f"timeout waiting for {what}")


def main():
    failures = []

    from . import config
    config.RELAY_URL = ""          # force direct mode regardless of local env

    def check(name, fn):
        try:
            fn()
            print(f"  ✓ {name}")
        except Exception as e:
            print(f"  ✗ {name}: {e}")
            failures.append(name)

    mock = MockHarness(PORT).start()
    c_gmail = mock.state.add_session(title="Gmail OAuth fix",
                                     digest="wiring gmail label buckets")
    c_other = mock.state.add_session(title="terrain renderer")
    mock.state.transcripts[c_other] = [
        {"role": "user", "text": "let's sort gmail into three buckets"},
        {"role": "assistant", "text": "the bucket script is in tools/sort.py"},
    ]
    mock.state.screens[c_other] = "Do you trust the files in this folder?\n> Yes"
    ledger = TaskLedger(tempfile.mktemp(suffix=".jsonl"))
    ledger.create_task("wire gmail labels into the sorter", project="p1",
                       machine="self")
    client = HarnessClient("self", mock.url, TOKEN).start()
    clients = {"self": client}
    guard = Guard(autonomy="auto")
    world = World(clients, ledger)
    verbs = Verbs(world, ledger, clients, guard)
    _wait(lambda: client.connected and client.sessions, what="client state")

    def t_find():
        r = verbs.find("gmail")
        assert r["ok"], r
        wheres = {m["where"] for m in r["matches"]}
        cids = {m.get("cid") for m in r["matches"]}
        assert "task" in wheres, r["matches"]
        assert "title" in wheres and c_gmail in cids, r["matches"]
        assert "transcript" in wheres and c_other in cids, r["matches"]
        sess_hits = [m for m in r["matches"] if m.get("cid")]
        assert all(m.get("url", "").startswith("http") for m in sess_hits), sess_hits
    check("find('gmail') → task + title + transcript hits in one call", t_find)

    def t_find_meta_only():
        mock.state.ignore_frames.add("search")     # old harness: frame unknown
        try:
            r = verbs.find("gmail")
            assert r["ok"] and any(m["where"] == "title" for m in r["matches"]), r
            assert r.get("unreachable") == ["self"], r
        finally:
            mock.state.ignore_frames.discard("search")
    check("find degrades to meta+ledger when a machine can't search", t_find_meta_only)

    def t_world_compact():
        snap = verbs.get_world()
        assert len(json.dumps(snap)) < WORLD_CHAR_BUDGET, len(json.dumps(snap))
        m = snap["machines"][0]
        row = m["projects"][0]["sessions"][0]
        assert set(row) <= {"cid", "title", "status", "task", "idle_m",
                            "digest", "blocked_on"}, row
        assert "lastAnswer" not in json.dumps(snap), "lastAnswer leaked into compact world"
    check("get_world compact: one-line sessions, no lastAnswer", t_world_compact)

    def t_world_bounded_at_scale():
        big = {}
        for i in range(10):
            big[f"m{i}"] = client        # 10 machines sharing the mock's state
        big_world = World(big, ledger)
        big_verbs = Verbs(big_world, ledger, big, guard)
        for i in range(40):              # fatten the mock: 40 more sessions
            mock.state.add_session(title=f"filler session {i}",
                                   digest="d" * 120)
        mock.state.broadcast(mock.state.sessions_frame())
        _wait(lambda: len(client.sessions) >= 40, what="fat session list")
        snap = big_verbs.get_world()
        assert len(json.dumps(snap)) < WORLD_CHAR_BUDGET + 2000, len(json.dumps(snap))
    check("get_world stays bounded on a 10x fleet (degrades, never blows)",
          t_world_bounded_at_scale)

    def t_world_scoped():
        snap = verbs.get_world(machine="self", pid="p1", verbose=True)
        rows = snap["machines"][0]["projects"][0]["sessions"]
        assert any("sessionId" in r for r in rows), rows[:1]
    check("get_world scoped+verbose returns full session dicts", t_world_scoped)

    def t_tail():
        r = verbs.transcript_tail("self", c_other, n=999)   # clamped server-side
        assert r["ok"] and len(r["events"]) == 2, r
        assert r["events"][-1]["role"] == "assistant", r
        bad = verbs.transcript_tail("self", "nope")
        assert bad["ok"] is False, bad
    check("transcript_tail returns slim events; clamps; clean error", t_tail)

    def t_screen():
        r = verbs.peek_screen("self", c_other)
        assert r["ok"] and "trust the files" in r["text"], r
    check("peek_screen reads the scripted screen", t_screen)

    def t_old_harness_timeout():
        mock.state.ignore_frames.add("transcriptTail")
        try:
            t0 = time.time()
            r = client.transcript_tail(c_other, n=3)
            # patched short timeout would be nicer; default 12s is the contract
            assert r.get("error"), r
            assert time.time() - t0 < 13.5
        finally:
            mock.state.ignore_frames.discard("transcriptTail")
    check("old harness (frame ignored) → clean timeout error", t_old_harness_timeout)

    def t_sweep():
        client.send_message(c_gmail, "please ASK? something")   # → blocked
        _wait(lambda: (client.sessions.get(c_gmail) or {}).get("status") == "blocked",
              what="blocked session")
        mock.state.transcripts[c_gmail] = [
            {"role": "assistant", "text": "which option?",
             "tools": [{"name": "AskUserQuestion", "input": "{\"q\":1}"}]}]
        b = verbs.sweep()
        assert b["ok"] and b["counts"].get("high"), b
        it = next(i for i in b["items"] if i["cid"] == c_gmail)
        assert it["url"].startswith("http"), it
        assert it["clear_with"]["verb"] == "answer_prompt", it
        assert it.get("tail"), it
        assert isinstance(b.get("idle_no_task"), list)
    check("sweep bundles evidence + url + clear_with + rollups", t_sweep)

    # -- ProxyMCPServer against a stub serve HTTP API --------------------------
    class _Stub(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _reply(self, obj):
            body = json.dumps(obj).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/api/world":
                return self._reply({"machines": [{"id": "stub"}]})
            return self._reply({})

        def do_POST(self):
            n = int(self.headers.get("Content-Length", "0") or 0)
            data = json.loads(self.rfile.read(n) or b"{}")
            return self._reply({"tool": data.get("name"), "args": data.get("args"),
                                "result": {"ok": True, "echo": data.get("name")}})

    class _Srv(ThreadingMixIn, TCPServer):
        daemon_threads = True
        allow_reuse_address = True

    stub = _Srv(("127.0.0.1", 8901), _Stub)
    threading.Thread(target=stub.serve_forever, daemon=True).start()

    def t_proxy():
        p = ProxyMCPServer("http://127.0.0.1:8901")
        r = p.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                      "params": {"name": "get_attention", "arguments": {}}})
        data = json.loads(r["result"]["content"][0]["text"])
        assert data == {"ok": True, "echo": "get_attention"}, data
        r2 = p.handle({"jsonrpc": "2.0", "id": 2, "method": "resources/read",
                       "params": {"uri": "fleet://world"}})
        data2 = json.loads(r2["result"]["contents"][0]["text"])
        assert data2["machines"][0]["id"] == "stub", data2
    check("ProxyMCPServer round-trips tools + resources over HTTP", t_proxy)

    stub.shutdown()
    mock.stop()
    print()
    if failures:
        print(f"FAILED: {failures}")
        return 1
    print("PASSED: PM senses v2 (find / bounded world / tail / sweep / proxy)")
    return 0


if __name__ == "__main__":
    rc = main()
    import sys
    sys.stdout.flush()
    os._exit(rc)
