#!/usr/bin/env python3
"""Integration test: drive `python3 -m controller mcp` as a real stdio subprocess.

This is the exact transport `claude -p` uses — spawn the MCP server, point it at a
mock harness via env, and speak newline-delimited JSON-RPC over its stdin/stdout:
initialize → tools/list → tools/call get_world. Proves the stdio server boots,
connects to a harness, and answers. Run: python3 -m controller.test_mcp_stdio
"""
import json
import os
import subprocess
import sys
import time

from .mock_harness import MockHarness, TOKEN

PORT = 8897


def main():
    mock = MockHarness(PORT).start()
    mock.state.add_session(title="work")
    env = dict(os.environ)
    env.update({"CONTROLLER_HARNESS_WS": f"ws://127.0.0.1:{PORT}",
                "CONTROLLER_HARNESS_TOKEN": TOKEN,
                "CONTROLLER_LEDGER": "/tmp/ctrl-stdio-test.jsonl"})
    if os.path.exists("/tmp/ctrl-stdio-test.jsonl"):
        os.remove("/tmp/ctrl-stdio-test.jsonl")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    proc = subprocess.Popen([sys.executable, "-m", "controller", "mcp"],
                            cwd=root, env=env, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)

    def rpc(obj, expect_id):
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()
        end = time.time() + 12
        while time.time() < end:
            line = proc.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line)
            except Exception:
                continue
            if msg.get("id") == expect_id:
                return msg
        raise AssertionError(f"no response for id={expect_id}")

    failures = []

    def check(name, fn):
        try:
            fn()
            print(f"  ✓ {name}")
        except Exception as e:
            print(f"  ✗ {name}: {e}")
            failures.append(name)

    time.sleep(2.5)   # let it connect to the mock harness

    def t_init():
        r = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": "2025-06-18"}}, 1)
        assert r["result"]["serverInfo"]["name"] == "clawd-controller", r
    check("stdio initialize", t_init)

    def t_tools():
        r = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, 2)
        assert len(r["result"]["tools"]) >= 13, r
    check("stdio tools/list", t_tools)

    def t_world():
        r = rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                 "params": {"name": "get_world", "arguments": {}}}, 3)
        data = json.loads(r["result"]["content"][0]["text"])
        assert data["machines"] and data["machines"][0]["id"] == "self", data
        assert data["machines"][0]["projects"][0]["pid"] == "p1", data
    check("stdio tools/call get_world reflects mock harness", t_world)

    proc.terminate()
    mock.stop()
    print()
    if failures:
        print(f"FAILED: {failures}")
        return 1
    print("PASSED: MCP stdio subprocess (the claude -p transport)")
    return 0


if __name__ == "__main__":
    rc = main()
    sys.stdout.flush()
    os._exit(rc)
