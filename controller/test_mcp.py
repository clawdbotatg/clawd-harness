#!/usr/bin/env python3
"""MCP protocol test — drives the JSON-RPC dispatch directly (no subprocess).

Asserts initialize/tools/resources handshakes and that tools/call routes into
the verbs and frames results per the MCP content shape. Uses the mock harness so
get_world/assign actually exercise the real stack.

Run:  python3 -m controller.test_mcp
"""
import json
import tempfile
import time

from .harness_client import HarnessClient
from .ledger import TaskLedger
from .mcp import MCPServer, TOOLS
from .mock_harness import MockHarness, TOKEN
from .verbs import Guard, Verbs
from .world import World

PORT = 8894


def main():
    failures = []

    def check(name, fn):
        try:
            fn()
            print(f"  ✓ {name}")
        except Exception as e:
            print(f"  ✗ {name}: {e}")
            failures.append(name)

    mock = MockHarness(PORT).start()
    mock.state.add_session(title="work")
    ledger = TaskLedger(tempfile.mktemp(suffix=".jsonl"))
    client = HarnessClient("self", mock.url, TOKEN).start()
    clients = {"self": client}
    world = World(clients, ledger)
    verbs = Verbs(world, ledger, clients, Guard(autonomy="auto"))
    srv = MCPServer(verbs)

    end = time.time() + 6
    while time.time() < end and not (client.connected and client.sessions):
        time.sleep(0.05)

    def rpc(method, params=None, mid=1):
        return srv.handle({"jsonrpc": "2.0", "id": mid, "method": method,
                           "params": params or {}})

    def t_initialize():
        r = rpc("initialize", {"protocolVersion": "2025-06-18"})
        assert r["result"]["serverInfo"]["name"] == "clawd-controller", r
        assert "tools" in r["result"]["capabilities"], r
    check("initialize handshake", t_initialize)

    def t_notif():
        assert srv.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    check("notifications get no response", t_notif)

    def t_tools_list():
        r = rpc("tools/list")
        names = {t["name"] for t in r["result"]["tools"]}
        assert {"get_world", "get_attention", "assign", "ask", "answer_prompt"} <= names, names
        assert len(r["result"]["tools"]) == len(TOOLS)
        for t in r["result"]["tools"]:           # every tool self-describes
            assert t["description"] and "inputSchema" in t, t
    check("tools/list exposes all verbs with schemas", t_tools_list)

    def t_resources():
        r = rpc("resources/list")
        uris = {x["uri"] for x in r["result"]["resources"]}
        assert uris == {"fleet://world", "fleet://attention", "fleet://tasks"}, uris
        rd = rpc("resources/read", {"uri": "fleet://world"})
        data = json.loads(rd["result"]["contents"][0]["text"])
        assert "machines" in data, data
    check("resources/list + resources/read world", t_resources)

    def t_tools_call_world():
        r = rpc("tools/call", {"name": "get_world", "arguments": {}})
        assert r["result"]["isError"] is False, r
        data = json.loads(r["result"]["content"][0]["text"])
        assert data["machines"][0]["id"] == "self", data
    check("tools/call get_world routes to verbs", t_tools_call_world)

    def t_tools_call_write():
        # create a task then assign via MCP — full write path through JSON-RPC
        c = rpc("tools/call", {"name": "create_task",
                               "arguments": {"goal": "do x", "machine": "self"}})
        task = json.loads(c["result"]["content"][0]["text"])["task"]
        a = rpc("tools/call", {"name": "assign",
                               "arguments": {"task_id": task["id"], "machine": "self",
                                             "spawn_in": "p1"}})
        res = json.loads(a["result"]["content"][0]["text"])
        assert res["ok"] and res["spawned"], res
    check("tools/call assign spawns + links via MCP", t_tools_call_write)

    def t_unknown_method():
        r = rpc("nope/nope")
        assert r["error"]["code"] == -32601, r
    check("unknown method → JSON-RPC error", t_unknown_method)

    client.stop()
    mock.stop()
    print()
    if failures:
        print(f"FAILED: {failures}")
        return 1
    print("PASSED: MCP read+write surface")
    return 0


if __name__ == "__main__":
    import os
    import sys
    rc = main()
    sys.stdout.flush()
    os._exit(rc)
