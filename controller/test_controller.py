#!/usr/bin/env python3
"""End-to-end controller smoke: mock harness ← HarnessClient → World/Verbs.

Asserts the full read+write loop with no real claude:
  - the client materializes projects/sessions from broadcasts
  - the world snapshot nests sessions under projects with task links
  - the attention queue surfaces a blocked session with the right verb
  - write verbs honor the autonomy gate (readonly → confirm → auto)
  - assign spawns a session, kicks it off, and links it to a task in the ledger
  - answer_prompt clears a blocked session

Run:  python3 -m controller.test_controller
"""
import os
import tempfile
import time

from .harness_client import HarnessClient
from .ledger import TaskLedger
from .mock_harness import MockHarness, TOKEN
from .verbs import Guard, Verbs
from .world import World

PORT = 8893


def _wait(pred, timeout=6.0, what=""):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    raise AssertionError(f"timeout waiting for {what}")


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
    mock.state.add_session(title="existing work")        # c1, idle
    ledger = TaskLedger(tempfile.mktemp(suffix=".jsonl"))
    client = HarnessClient("self", mock.url, TOKEN).start()
    clients = {"self": client}
    guard = Guard(autonomy="readonly")
    world = World(clients, ledger)
    verbs = Verbs(world, ledger, clients, guard)

    _wait(lambda: client.connected and client.sessions, what="client to sync state")

    def t_snapshot():
        snap = verbs.get_world()
        m = snap["machines"][0]
        assert m["id"] == "self" and m["connected"], m
        proj = m["projects"][0]
        assert proj["pid"] == "p1", proj
        assert any(s["cid"] == "c1" for s in proj["sessions"]), proj["sessions"]
    check("world snapshot: machines→projects→sessions", t_snapshot)

    def t_readonly_blocks_write():
        r = verbs.ask("self", "c1", "hello")
        assert r["ok"] is False and r.get("blocked"), r
    check("autonomy=readonly refuses writes (proposes instead)", t_readonly_blocks_write)

    def t_confirm_gate():
        guard.autonomy = "confirm"
        r = verbs.ask("self", "c1", "hello")
        assert r["ok"] is False and r.get("needs_confirm"), r
        r2 = verbs.ask("self", "c1", "hello", confirm=True)
        assert r2["ok"] is True, r2
    check("autonomy=confirm: dry-run unless confirm=true", t_confirm_gate)

    def t_create_and_assign():
        guard.autonomy = "auto"
        task = verbs.create_task("add a README", project="p1", machine="self")["task"]
        r = verbs.assign(task["id"], "self", spawn_in="p1")
        assert r["ok"] and r["spawned"], r
        cid = r["cid"]
        # the ledger now links the spawned session to the task
        assert ledger.task_for_cid(cid) == task["id"]
        # and the world snapshot reflects the link
        _wait(lambda: any(
            s["cid"] == cid and s.get("task") == task["id"]
            for m in verbs.get_world()["machines"]
            for p in m["projects"] for s in p["sessions"]),
            what="assigned session linked in world")
    check("create_task + assign spawns a session and links it", t_create_and_assign)

    def t_attention_and_answer():
        guard.autonomy = "auto"
        # drive c1 into a blocked state via the mock's ASK? convention
        verbs.ask("self", "c1", "do the thing ASK?", confirm=True)
        _wait(lambda: any(i["cid"] == "c1" and i["kind"] == "blocked"
                          for i in verbs.get_attention()["items"]),
              what="c1 to appear blocked in attention")
        item = next(i for i in verbs.get_attention()["items"] if i["cid"] == "c1")
        assert item["sev"] == "high" and item["suggested_action"] == "answer_prompt", item
        # clear it
        verbs.answer_prompt("self", "c1", "1\r")
        _wait(lambda: all(i["cid"] != "c1" for i in verbs.get_attention()["items"]),
              what="c1 to clear from attention")
    check("attention queue surfaces a block; answer_prompt clears it", t_attention_and_answer)

    def t_rate_limit():
        g = Guard(autonomy="auto", rate_per_min=3)
        v = Verbs(world, ledger, clients, g)
        oks = [v.ask("self", "c1", f"m{i}")["ok"] for i in range(5)]
        assert oks.count(True) == 3 and oks.count(False) == 2, oks
    check("rate limiter caps writes per target", t_rate_limit)

    client.stop()
    mock.stop()
    print()
    if failures:
        print(f"FAILED: {failures}")
        return 1
    print("PASSED: controller read+write loop end-to-end")
    return 0


if __name__ == "__main__":
    import os
    import sys
    rc = main()
    sys.stdout.flush()
    os._exit(rc)        # skip interpreter finalize — daemon WS threads race the buffered writer
