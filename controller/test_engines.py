#!/usr/bin/env python3
"""What the PM can actually SEE — engines, pins, plan headroom, project kinds.

The controller shipped the codex engine as one line in the `spawn` schema and
nothing else: the PM could start a codex session but could not tell one from a
claude session in any read, `assign` could only ever spawn claude, and the pin
board / subscription pools / private local projects had no verb at all. A tool
the model can't see the effect of is a tool it never uses, so each of these is
asserted as a visible field, not just a working call:

  - a codex session is tagged `engine` in get_world / find / sweep / get_pins,
    and a claude session is NOT (absent ⇒ claude — the wire's own convention)
  - assign(engine=…) spawns that CLI and records it on the task
  - an unknown engine degrades to claude rather than failing
  - get_pins surfaces the done-but-unverified queue with its test hints
  - get_accounts reports per-machine plan usage + the codex card, and there is
    deliberately no verb to switch accounts
  - a private local project is adoptable, distinguishable, and detachable —
    and remove_project refuses a gh project (those are folder deletions)

Run:  python3 -m controller.test_engines
"""
import tempfile
import time

from .harness_client import HarnessClient
from .ledger import TaskLedger
from .mock_harness import MockHarness, TOKEN
from .verbs import Guard, Verbs
from .world import World

PORT = 8896


def _wait(pred, timeout=6.0, what=""):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    raise AssertionError(f"timeout waiting for {what}")


def _sessions(verbs):
    return [s for m in verbs.get_world()["machines"]
            for p in m["projects"] for s in p["sessions"]]


def main():
    failures = []

    from . import config
    config.RELAY_URL = ""          # direct mode: stable deep-link shapes

    def check(name, fn):
        try:
            fn()
            print(f"  ✓ {name}")
        except Exception as e:
            print(f"  ✗ {name}: {e}")
            failures.append(name)

    mock = MockHarness(PORT).start()
    c_claude = mock.state.add_session(title="claude work")
    c_codex = mock.state.add_session(title="codex review", engine="codex")
    mock.state.transcripts[c_codex] = [
        {"role": "assistant", "text": "the migration plan has a hole in step 3"}]
    ledger = TaskLedger(tempfile.mktemp(suffix=".jsonl"))
    client = HarnessClient("self", mock.url, TOKEN).start()
    clients = {"self": client}
    guard = Guard(autonomy="auto")
    world = World(clients, ledger)
    verbs = Verbs(world, ledger, clients, guard)

    _wait(lambda: client.connected and len(client.sessions) >= 2,
          what="client to sync state")

    def t_world_tags_engine():
        rows = {s["cid"]: s for s in _sessions(verbs)}
        assert rows[c_codex].get("engine") == "codex", rows[c_codex]
        # absent ⇒ claude. Spending a field on the default on every row would
        # cost real budget, and it's the same rule the wire protocol uses.
        assert "engine" not in rows[c_claude], rows[c_claude]
    check("get_world tags a codex session; claude stays implicit", t_world_tags_engine)

    def t_find_tags_engine():
        r = verbs.find("codex review")
        hit = next(h for h in r["matches"] if h.get("cid") == c_codex)
        assert hit.get("engine") == "codex", hit
        # a transcript hit resolves the engine from our own cache (the harness's
        # search reply has no engine field)
        r2 = verbs.find("hole in step 3")
        t_hit = next((h for h in r2["matches"]
                      if h.get("where") == "transcript" and h.get("cid") == c_codex), None)
        assert t_hit and t_hit.get("engine") == "codex", r2["matches"]
        r3 = verbs.find("claude work")
        assert "engine" not in next(h for h in r3["matches"]
                                    if h.get("cid") == c_claude)
    check("find tags codex hits (meta AND transcript)", t_find_tags_engine)

    def t_sweep_tags_engine():
        mock.state.set_session(c_codex, status="blocked", waiting=True,
                              blocked_on="approve this?")
        _wait(lambda: any(i["cid"] == c_codex for i in verbs.sweep()["items"]),
              what="codex session in the sweep")
        item = next(i for i in verbs.sweep()["items"] if i["cid"] == c_codex)
        assert item.get("engine") == "codex", item
        mock.state.set_session(c_codex, status="idle", waiting=False, blocked_on="")
    check("sweep items name the engine that parked them", t_sweep_tags_engine)

    def t_assign_engine():
        task = verbs.create_task("double-check the migration", machine="self")["task"]
        r = verbs.assign(task["id"], "self", spawn_in="p1", engine="codex")
        assert r["ok"] and r["spawned"] and r["engine"] == "codex", r
        _wait(lambda: r["cid"] in client.sessions, what="the new session")
        assert client.sessions[r["cid"]]["engine"] == "codex", client.sessions[r["cid"]]
        # the ledger records WHICH cli did the work, not just that a session did
        t = ledger.get(task["id"])
        assert t["engines"][r["cid"]] == "codex", t
        assert any("codex" in h["event"] for h in t["history"]), t["history"]
    check("assign(engine='codex') spawns codex and records it on the task",
          t_assign_engine)

    def t_unknown_engine():
        r = verbs.spawn("self", "p1", engine="gpt-9")
        assert r["ok"] and r["engine"] == "claude", r
        _wait(lambda: r["cid"] in client.sessions, what="the fallback session")
        assert client.sessions[r["cid"]]["engine"] == "claude"
    check("an unknown engine degrades to claude instead of failing",
          t_unknown_engine)

    def t_pins():
        assert verbs.get_pins()["count"] == 0, verbs.get_pins()
        r = verbs.pin("self", c_claude)
        assert r["ok"] and r["pinned"], r
        _wait(lambda: verbs.get_pins()["count"] == 1, what="the pin board")
        p = verbs.get_pins()["pins"][0]
        assert p["cid"] == c_claude and p["test_hint"], p
        assert p["url"].endswith(f"#/p/p1/s/{c_claude}"), p
        # and a pin is visible as parked in the world, not as neglected idle work
        row = next(s for s in _sessions(verbs) if s["cid"] == c_claude)
        assert row.get("pinned") is True, row
        assert verbs.pin("self", c_claude, on=False)["ok"]
        _wait(lambda: verbs.get_pins()["count"] == 0, what="the unpin")
    check("get_pins is the done-but-unverified queue; pin/unpin round-trips",
          t_pins)

    def t_accounts():
        r = verbs.get_accounts()
        m = r["machines"][0]
        assert m["known"] and m["active"] == "alpha", m
        hot = next(a for a in m["accounts"] if a["name"] == "alpha")
        assert hot["usage_pct"] == 91.0 and hot["active"] and hot["windows"], hot
        assert m["would_spawn_on"] == "beta", m           # the router's own pick
        assert m["codex"]["plan"] == "pro" and m["codex"]["routed"] is False, m
        # there is deliberately no verb to move accounts around
        assert not any(n.startswith("account") for n in dir(verbs))
    check("get_accounts reports plan headroom + the codex card (read-only)",
          t_accounts)

    def t_local_projects():
        r = verbs.add_local_project("self", "/tmp/private-thing")
        assert r["ok"], r
        _wait(lambda: any(p.get("kind") == "local"
                          for p in client.state()["projects"]),
              what="the local project to appear")
        pid = next(p["pid"] for p in client.state()["projects"]
                   if p.get("kind") == "local")
        # a private folder must be *distinguishable* — the PM has to know not to
        # suggest pushing it or naming its path anywhere
        m = verbs.get_world()["machines"][0]
        assert "private-thing" in (m.get("local_projects") or []), m
        spawn = verbs.spawn("self", pid)
        assert spawn["ok"], spawn
        _wait(lambda: any(p.get("kind") == "local" and p.get("sessions")
                          for p in verbs.get_world()["machines"][0]["projects"]),
              what="the local project's session row")
        proj = next(p for p in verbs.get_world()["machines"][0]["projects"]
                    if p["pid"] == pid)
        assert proj["kind"] == "local", proj
        # gh projects are removed by deleting the folder — not by this verb
        bad = verbs.remove_project("self", "p1")
        assert bad["ok"] is False and "gh project" in bad["error"], bad
        good = verbs.remove_project("self", pid)
        assert good["ok"] and good["folder_kept"], good
        _wait(lambda: all(p.get("kind") != "local"
                          for p in client.state()["projects"]),
              what="the local project to detach")
    check("private local projects: adopt, distinguish, detach (never delete)",
          t_local_projects)

    client.stop()
    mock.stop()
    print()
    if failures:
        print(f"FAILED: {failures}")
        return 1
    print("PASSED: PM sees engines, pins, plan headroom and project kinds")
    return 0


if __name__ == "__main__":
    import os
    import sys
    rc = main()
    sys.stdout.flush()
    os._exit(rc)        # skip finalize — daemon WS threads race the buffered writer
