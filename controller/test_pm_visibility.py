#!/usr/bin/env python3
"""Three things the web UI shows that the PM couldn't see: irons, the closed
history, and "what's warm".

  - get_irons: irons in priority order, member projects, live sessions warmest
    first with deep links — direct mode (harness `irons` frame, pids) AND fleet
    mode (relay `prefs` frame, projectKeys, incl. the name:<x> → URL fold)
  - `iron` rides get_world / find / sweep rows only when the project is in one
  - get_closed: newest first, query filter, truncated TLDR; find includes
    closed rows marked `closed: true`
  - reopen: gated like close; brings the row back as a new cid; a refused
    reopen (project gone) is an error, not a hang
  - get_warm: promptedAt || lastActive, warmest first, pinned left out

Run:  python3 -m controller.test_pm_visibility
"""
import json
import tempfile
import time

from .harness_client import HarnessClient
from .ledger import TaskLedger
from .mcp import MCPServer, TOOLS
from .mock_harness import MockHarness, TOKEN
from .relay_client import RelayFleet
from .verbs import Guard, Verbs, WRITE_VERBS
from .world import World

PORT = 8903


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
    config.RELAY_URL = ""          # direct mode first: stable deep-link shapes

    def check(name, fn):
        try:
            fn()
            print(f"  ✓ {name}")
        except Exception as e:
            print(f"  ✗ {name}: {type(e).__name__}: {e}")
            failures.append(name)

    now = time.time()
    mock = MockHarness(PORT).start()
    st = mock.state
    st.projects["p2"] = dict(st.projects["p1"], pid="p2", name="other",
                             repoUrl="https://github.com/clawdbotatg/other.git")
    c_hot = st.add_session(title="voice hud", promptedAt=now - 60, lastActive=now - 50)
    c_cold = st.add_session(title="old idea", lastActive=now - 7200)
    c_pin = st.add_session(title="parked", pinned=now - 10, lastActive=now - 5)
    c_mid = st.add_session(pid="p2", title="other work", lastActive=now - 600,
                           digest="refactoring the parser\nsecond line")
    st.irons = [{"id": "i2", "title": "Side quest", "desc": "", "tags": [],
                 "pids": ["p2"], "created": 0, "rank": 2},
                {"id": "i1", "title": "Voice", "desc": "talk to the fleet",
                 "tags": ["audio"], "pids": ["p1", "gone"], "created": 0, "rank": 1}]
    closed_wrap = st.add_closed(title="gmail filter", reason="wrapped",
                                closed_at=now - 3 * 3600,
                                last_answer="TLDR: filter shipped " + "x" * 400)
    closed_new = st.add_closed(title="calendar sync", closed_at=now - 120,
                               first_prompt="sync the dentist calendar")
    closed_orphan = st.add_closed(pid="nope", title="orphan", closed_at=now - 5 * 3600)

    ledger = TaskLedger(tempfile.mktemp(suffix=".jsonl"))
    client = HarnessClient("self", mock.url, TOKEN).start()
    clients = {"self": client}
    guard = Guard(autonomy="confirm")
    world = World(clients, ledger)
    verbs = Verbs(world, ledger, clients, guard)

    _wait(lambda: client.connected and len(client.sessions) >= 4
          and client.irons and len(client.closed) == 3,
          what="client to sync sessions + irons + closed")

    def t_irons_direct():
        r = verbs.get_irons()
        assert r["ok"] and [i["title"] for i in r["irons"]] == ["Voice", "Side quest"], r
        v = r["irons"][0]
        assert v["rank"] == 1 and v["desc"] == "talk to the fleet" and v["tags"] == ["audio"], v
        assert [p["pid"] for p in v["projects"]] == ["p1"], v
        assert v["unresolved_members"] == 1, v           # "gone" isn't a project
        cids = [s["cid"] for s in v["sessions"]]
        # warmest first by promptedAt || lastActive; pinned members still listed
        assert cids[0] == c_pin and cids[1] == c_hot and cids[-1] == c_cold, cids
        assert v["sessions"][1]["url"].endswith(f"#/p/p1/s/{c_hot}"), v["sessions"][1]
        assert v["sessions"][0].get("pinned") is True, v["sessions"][0]
        assert r["irons"][1]["sessions"][0]["cid"] == c_mid, r["irons"][1]
    check("get_irons (direct): rank order, members, warmest-first sessions", t_irons_direct)

    def t_iron_tags():
        projs = {p["pid"]: p for p in verbs.get_world()["machines"][0]["projects"]}
        assert projs["p1"]["iron"] == "Voice" and projs["p2"]["iron"] == "Side quest", projs
        hit = next(h for h in verbs.find("voice hud", scope="meta")["matches"]
                   if h.get("cid") == c_hot)
        assert hit["iron"] == "Voice", hit
        st.irons = [i for i in st.irons if i["id"] == "i1"]
        st.broadcast(st.irons_frame())
        _wait(lambda: len(client.irons) == 1, what="iron removal to land")
        projs = {p["pid"]: p for p in verbs.get_world()["machines"][0]["projects"]}
        assert "iron" not in projs["p2"], projs["p2"]       # absent ⇒ no iron
        st.set_session(c_hot, status="blocked", waiting=True, blocked_on="which mic?")
        _wait(lambda: any(i["cid"] == c_hot for i in verbs.sweep()["items"]),
              what="blocked session in the sweep")
        item = next(i for i in verbs.sweep()["items"] if i["cid"] == c_hot)
        assert item["iron"] == "Voice", item
        st.set_session(c_hot, status="idle", waiting=False, blocked_on="")
    check("iron title rides get_world / find / sweep only when in one", t_iron_tags)

    def t_closed():
        r = verbs.get_closed()
        assert [x["cid"] for x in r["closed"]] == [closed_new, closed_wrap, closed_orphan], r
        w = r["closed"][1]
        assert w["reason"] == "wrapped" and w["closed_ago"] == "3h" and w["resumable"], w
        assert len(w["last_answer"]) == 200 and w["iron"] == "Voice", w
        q = verbs.get_closed(query="DENTIST")
        assert [x["cid"] for x in q["closed"]] == [closed_new], q
        assert verbs.get_closed(limit=1)["truncated"] is True
    check("get_closed: newest first, filter, truncated TLDR, iron tag", t_closed)

    def t_find_closed():
        r = verbs.find("gmail filter", scope="meta")
        hit = next(h for h in r["matches"] if h.get("cid") == closed_wrap)
        assert hit["closed"] is True and hit["where"] == "closed", hit
        assert hit["reopen_with"] == {"verb": "reopen",
                                      "args": {"machine": "self", "cid": closed_wrap}}, hit
    check("find includes closed rows, clearly marked", t_find_closed)

    def t_reopen():
        assert "reopen" in WRITE_VERBS
        r = verbs.reopen("self", closed_new)
        assert r.get("needs_confirm") and r["proposed"]["verb"] == "reopen", r
        assert any(x["cid"] == closed_new for x in client.closed)    # nothing sent
        bad = verbs.reopen("self", "c999", confirm=True)
        assert bad["ok"] is False and "get_closed" in bad["error"], bad
        ok = verbs.reopen("self", closed_new, confirm=True)
        assert ok["ok"] and ok["cid"] and ok["resumed"] and ok["reopened"] == closed_new, ok
        _wait(lambda: ok["cid"] in client.sessions
              and all(x["cid"] != closed_new for x in client.closed),
              what="reopened session live + row gone")
        refused = verbs.reopen("self", closed_orphan, confirm=True)
        assert refused["ok"] is False and "project is gone" in refused["error"], refused
    check("reopen: gated, unknown row refused, round-trips, refusal surfaces", t_reopen)

    def t_warm():
        r = verbs.get_warm()
        cids = [s["cid"] for s in r["sessions"]]
        assert c_pin not in cids, cids                       # parked work is not warm
        assert cids.index(c_hot) < cids.index(c_mid) < cids.index(c_cold), cids
        mid = next(s for s in r["sessions"] if s["cid"] == c_mid)
        assert mid["digest"] == "refactoring the parser" and mid["age"] == "10m", mid
        hot = next(s for s in r["sessions"] if s["cid"] == c_hot)
        assert hot["iron"] == "Voice" and hot["url"].endswith(f"/s/{c_hot}"), hot
        assert len(verbs.get_warm(limit=1)["sessions"]) == 1
    check("get_warm: promptedAt||lastActive order, pinned excluded", t_warm)

    def t_mcp_surface():
        names = {n for n, _d, _s in TOOLS}
        assert {"get_irons", "get_warm", "get_closed", "reopen"} <= names, names
        srv = MCPServer(verbs)
        for name, args in (("get_irons", {}), ("get_warm", {"limit": 3}),
                           ("get_closed", {"query": "gmail"})):
            out = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": name, "arguments": args}})
            data = json.loads(out["result"]["content"][0]["text"])
            assert data["ok"], (name, data)
        from pathlib import Path
        persona = (Path(__file__).parent / "prompts" / "private.md").read_text()
        for verb in ("get_irons", "get_warm", "get_closed", "reopen"):
            assert f"`{verb}" in persona, f"persona never mentions {verb}"
    check("MCP + persona expose all four (the three-places rule)", t_mcp_surface)

    client.stop()
    mock.stop()

    # ── fleet mode: irons from the relay `prefs` frame, keyed by projectKey ──
    config.RELAY_URL = "ws://relay.invalid"
    fleet = RelayFleet("ws://relay.invalid", "x")          # never started: frames fed by hand
    fleet._on_frame({"type": "machines", "machines": [
        {"id": "boxa", "online": True}, {"id": "boxb", "online": True}]})

    def feed(mid, msg):
        fleet._on_frame({"type": "machineMsg", "machine": mid, "msg": msg})
    feed("boxa", {"type": "projects", "projects": [
        {"pid": "a1", "name": "harness", "kind": "gh",
         "repoUrl": "git@github.com:clawdbotatg/clawd-harness.git"},
        {"pid": "a2", "name": "notes", "kind": "local", "path": "/x/notes"}]})
    feed("boxb", {"type": "projects", "projects": [
        # no repo URL here → keys as name:<x>, folds into the URL group
        {"pid": "b7", "name": "clawd-harness", "kind": "gh", "repoUrl": ""},
        {"pid": "b8", "name": "loner", "kind": "gh",
         "repoUrl": "https://github.com/x/loner"}]})
    feed("boxa", {"type": "sessions", "sessions": [
        {"cid": "sa", "pid": "a1", "title": "relay work", "lastActive": now - 30,
         "status": "working"},
        {"cid": "sn", "pid": "a2", "title": "private", "lastActive": now - 20}]})
    feed("boxb", {"type": "sessions", "sessions": [
        {"cid": "sb", "pid": "b7", "title": "ui work", "promptedAt": now - 5,
         "lastActive": now - 900},
        {"cid": "sl", "pid": "b8", "title": "lonely", "lastActive": now - 10}]})
    feed("boxb", {"type": "closed", "closed": [
        {"cid": "zz", "pid": "b7", "title": "finished", "closed_at": now - 60,
         "reason": "wrapped", "last_answer": "TLDR done", "resumable": True}]})
    fverbs = Verbs(World(fleet.machines, ledger), ledger, fleet.machines,
                   Guard(autonomy="confirm"), irons_source=fleet.irons_state)

    def t_fleet_not_seen():
        r = fverbs.get_irons()
        assert r["count"] == 0 and "hint" in r, r
    check("fleet: no prefs yet → says so instead of 'no irons'", t_fleet_not_seen)

    fleet._on_frame({"type": "prefs", "inactive": [], "irons": [
        {"id": "f1", "title": "Harness", "desc": "", "tags": [], "created": 0,
         "keys": ["github.com/clawdbotatg/clawd-harness", "local:boxa:/x/notes",
                  "github.com/clawdbotatg/retired"]},
        {"id": "f2", "title": "Empty", "desc": "", "tags": [], "created": 0, "keys": []}]})

    def t_fleet_irons():
        r = fverbs.get_irons()
        assert [i["title"] for i in r["irons"]] == ["Harness", "Empty"], r
        h = r["irons"][0]
        assert {(p["machine"], p["pid"]) for p in h["projects"]} == {
            ("boxa", "a1"), ("boxa", "a2"), ("boxb", "b7")}, h["projects"]
        assert [s["cid"] for s in h["sessions"]] == ["sb", "sn", "sa"], h["sessions"]
        assert h["unresolved_members"] == 1, h
        assert "/#/m/boxb/p/" in h["sessions"][0]["url"], h["sessions"][0]
        # a prefs frame without an irons field must not wipe them
        fleet._on_frame({"type": "prefs", "inactive": ["boxb"]})
        assert fverbs.get_irons()["count"] == 2
    check("fleet: projectKey membership across machines, name:<x> fold, local key",
          t_fleet_irons)

    def t_fleet_tags():
        warm = {s["cid"]: s for s in fverbs.get_warm()["sessions"]}
        assert warm["sb"]["iron"] == "Harness" and "iron" not in warm["sl"], warm
        assert next(iter(warm)) == "sb", list(warm)
        cl = fverbs.get_closed()["closed"][0]
        assert cl["iron"] == "Harness" and cl["machine"] == "boxb", cl
    check("fleet: iron tags on warm + closed rows", t_fleet_tags)

    config.RELAY_URL = ""
    print()
    if failures:
        print(f"FAILED: {failures}")
        return 1
    print("PASSED: PM sees irons, closed history and warm sessions")
    return 0


if __name__ == "__main__":
    import os
    import sys
    rc = main()
    sys.stdout.flush()
    os._exit(rc)
