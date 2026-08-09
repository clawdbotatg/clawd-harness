#!/usr/bin/env python3
"""Pipelines: one task, N steps, a session (and engine) each.

The shape this exists for is the one the operator asked for and the PM could
not express: **research it with claude, have codex double-check that, then let
claude write the final report.** A PM turn ends when it replies, so there was no
"and then" — the only unattended follow-up was the autopilot's verify turn,
which is single-shot and explicitly forbidden from spawning.

Asserted here, with no real claude/codex anywhere:
  - create_pipeline validates the plan at creation time (a bad plan must fail
    where a human can read the message, not three steps in, unattended)
  - start_pipeline is the ONE approval; advance_pipeline refuses to run step 1,
    so the confirm gate can't be side-stepped by calling advance first
  - the full 3-step chain runs itself off Stop hooks: claude → codex → claude,
    ending in `review` with a retrievable report
  - each step's prompt carries the EARLIER steps' answers (that's the handoff)
  - `reuse` sends a step to an earlier step's own session — and the baseline
    fingerprint stops that session's PREVIOUS answer being mistaken for the new
    one (which would silently skip the step's actual work)
  - the settle sweep advances a step whose turn-end hook never fires, because
    codex's hooks are unproven and a missed frame must not wedge a chain forever
  - a vanished session force-closes rather than hanging

Run:  python3 -m controller.test_pipeline
"""
import tempfile
import time

from .autopilot import Autopilot
from .events import Reactor
from .harness_client import HarnessClient
from .ledger import TaskLedger
from .mock_harness import MockHarness, TOKEN
from .verbs import Guard, Verbs, out_hash
from .world import World

PORT = 8897

STEPS = [
    {"role": "research", "engine": "claude", "pid": "p1",
     "prompt": "Research how the fleet routes subscriptions; cite evidence."},
    {"role": "review", "engine": "codex", "pid": "p1",
     "prompt": "Independently double-check the research above. What is wrong, "
               "unsupported, or missing?"},
    {"role": "report", "engine": "claude", "pid": "p1", "reuse": 1,
     "prompt": "Take the critique above and write the final report. Say what "
               "you changed and what you rejected."},
]


def _wait(pred, timeout=15.0, what=""):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    raise AssertionError(f"timeout waiting for {what}")


def main():
    failures = []

    from . import config
    config.RELAY_URL = ""

    def check(name, fn):
        try:
            fn()
            print(f"  ✓ {name}")
        except Exception as e:
            print(f"  ✗ {name}: {e}")
            failures.append(name)

    mock = MockHarness(PORT).start()
    ledger = TaskLedger(tempfile.mktemp(suffix=".jsonl"))
    reactor = Reactor(ledger)
    client = HarnessClient("self", mock.url, TOKEN, on_hook=reactor.feed).start()
    clients = {"self": client}
    guard = Guard(autonomy="confirm", rate_per_min=30)
    world = World(clients, ledger)
    verbs = Verbs(world, ledger, clients, guard)
    pings = []
    # The production wiring: reactor → autopilot.feed (cheap, queues) → worker
    # thread. Never call _handle from the reactor thread — an advance blocks on
    # a spawn's focus reply, which only that same reader thread can deliver.
    ap = Autopilot(lambda kind, prompt: "", verbs, ledger, guard,
                   notify=pings.append, enabled=True,
                   toggle_path=tempfile.mktemp(suffix=".txt"),
                   pipeline_idle_s=0.0, pipeline_sweep_s=0.2).start()
    verbs.escalate_sink = ap.escalate
    reactor.on_event(ap.feed)

    _wait(lambda: client.connected and client.projects, what="client to sync")

    def t_validation():
        assert verbs.create_pipeline("g", [])["ok"] is False
        assert verbs.create_pipeline("g", [{"role": "x"}])["ok"] is False
        many = verbs.create_pipeline("g", [{"prompt": "p"}] * 9)
        assert many["ok"] is False and "too many" in many["error"], many
        # a forward reference would be unrunnable — catch it at creation
        fwd = verbs.create_pipeline("g", [{"prompt": "a", "reuse": 2},
                                          {"prompt": "b"}])
        assert fwd["ok"] is False and "EARLIER" in fwd["error"], fwd
        # a bare string is just a prompt; unknown engines normalize
        ok = verbs.create_pipeline("g", ["do a thing",
                                         {"prompt": "b", "engine": "gpt-9"}])
        assert ok["ok"], ok
        st = ok["task"]["steps"]
        assert st[0]["role"] == "step1" and st[0]["engine"] == "claude", st
        assert st[1]["engine"] == "claude", st
    check("create_pipeline validates the plan at creation time", t_validation)

    def t_gate():
        p = verbs.create_pipeline("gated", list(STEPS), machine="self")["task"]
        # advance must NOT be a way around the approval for step 1
        adv = verbs.advance_pipeline(p["id"])
        assert adv["ok"] is False and "start_pipeline" in adv["error"], adv
        r = verbs.start_pipeline(p["id"])
        assert r["ok"] is False and r.get("needs_confirm"), r
        assert r["proposed"]["verb"] == "start_pipeline", r
        assert ledger.get(p["id"])["status"] == "open", ledger.get(p["id"])
    check("start_pipeline is the one approval; advance can't skip it", t_gate)

    task = verbs.create_pipeline(
        "explain how subscription routing works",
        list(STEPS), machine="self",
        acceptance="a report that names the routing key and the hop threshold",
    )["task"]
    tid = task["id"]

    def t_start():
        r = verbs.start_pipeline(tid, confirm=True)
        assert r["ok"] and r["step"] == 1, r
        assert r["engine"] == "claude" and r["spawned"], r
        assert r["url"], r
        t = ledger.get(tid)
        assert t["status"] == "in_progress" and t["step"] == 1, t
        s1 = t["steps"][0]
        assert s1["status"] in ("running", "done") and s1["cid"], s1
        # the kickoff tells the session its final message IS the handoff
        assert "final message" in (s1.get("sent") or "").lower(), s1.get("sent")
        assert f"step 1/3" in s1["sent"] and "role: research" in s1["sent"], s1["sent"]
    check("start_pipeline runs step 1 and records the kickoff", t_start)

    def t_chain_runs_itself():
        _wait(lambda: ledger.get(tid)["status"] == "review", timeout=25,
              what="the pipeline to finish all three steps")
        t = ledger.get(tid)
        assert [s["status"] for s in t["steps"]] == ["done"] * 3, t["steps"]
        assert [s["engine"] for s in t["steps"]] == ["claude", "codex", "claude"], t
        # every step produced a recorded answer
        assert all(s["output"] for s in t["steps"]), t["steps"]
    check("the chain advances itself: claude → codex → claude → review",
          t_chain_runs_itself)

    def t_engines_really_used():
        t = ledger.get(tid)
        cids = [s["cid"] for s in t["steps"]]
        live = client.sessions
        assert live[cids[1]]["engine"] == "codex", live[cids[1]]
        assert live[cids[0]]["engine"] == "claude", live[cids[0]]
        # …and the ledger can answer "which CLI did which part"
        assert t["engines"][cids[1]] == "codex", t["engines"]
    check("the review step really ran on codex, and the ledger says so",
          t_engines_really_used)

    def t_handoff():
        t = ledger.get(tid)
        sent2, sent3 = t["steps"][1]["sent"], t["steps"][2]["sent"]
        # step 2 receives step 1's answer; step 3 receives 1 and 2
        assert "step 1 (research, claude) answered" in sent2, sent2
        assert "step 1 (research, claude) answered" in sent3, sent3
        assert "step 2 (review, codex) answered" in sent3, sent3
        # prior context is in chronological order, and the acceptance rides on
        # the LAST step only
        assert sent3.index("step 1 (") < sent3.index("step 2 ("), sent3
        assert "Done when: a report that names" in sent3, sent3
        assert "Done when:" not in sent2, sent2
    check("each step's prompt carries the earlier steps' answers", t_handoff)

    def t_reuse():
        t = ledger.get(tid)
        s1, s3 = t["steps"][0], t["steps"][2]
        assert s3["cid"] == s1["cid"], (s1["cid"], s3["cid"])   # same session
        assert s3["reuse"] == 1, s3
        # the trap this guards: a reused session already HAS an answer sitting in
        # it. Without the baseline fingerprint, step 3 would close instantly
        # against step 1's text and its actual work would never be read.
        assert s3["base"] == out_hash(s1["output"]), (s3["base"], s1["output"])
        assert s3["output"] != s1["output"], s3["output"]
    check("reuse targets the earlier step's own session", t_reuse)

    def t_report_retrievable():
        # get_task truncates step outputs; the full report has its own verb
        full = verbs.get_step_output(tid, 3)
        assert full["ok"] and full["output"] and full["engine"] == "claude", full
        assert full["output"] == ledger.get(tid)["steps"][2]["output"]
        assert verbs.get_step_output(tid, 9)["ok"] is False
        # and the operator was told, unprompted
        assert any("pipeline" in p and tid in p for p in pings), pings
    check("the finished report is retrievable and pushed to the operator",
          t_report_retrievable)

    def t_list_is_compact():
        row = next(t for t in verbs.list_tasks()["tasks"] if t["id"] == tid)
        assert all(isinstance(s, str) for s in row["steps"]), row["steps"]
        assert "3 report/claude done" in row["steps"], row["steps"]
    check("list_tasks summarizes steps instead of dumping their outputs",
          t_list_is_compact)

    # -- the no-hook path: codex's Stop may never fire ------------------------
    def t_settle_sweep():
        ap.enabled = False                     # no auto-advance; drive it by hand
        p = verbs.create_pipeline("hookless", [
            {"role": "one", "engine": "codex", "pid": "p1", "prompt": "first"},
            {"role": "two", "engine": "claude", "pid": "p1", "prompt": "second"},
        ], machine="self")["task"]
        r = verbs.start_pipeline(p["id"], confirm=True)
        assert r["ok"], r
        cid = ledger.get(p["id"])["steps"][0]["cid"]
        # simulate an engine that answered but emitted no turn-end hook: wipe the
        # hook capture, leave the answer only in the session meta + transcript
        client.last_answer.pop(cid, None)
        ap.enabled = True
        ap._pipeline_sweep()                   # first look: records the answer
        ap._pipeline_sweep()                   # second: unchanged → advance
        _wait(lambda: ledger.get(p["id"])["steps"][0]["status"] == "done",
              what="the settle sweep to close step 1")
        t = ledger.get(p["id"])
        assert t["steps"][0]["output"], t["steps"][0]
        assert t["steps"][1]["status"] in ("running", "done"), t["steps"]
    check("a step whose turn-end hook never fires still advances (settle sweep)",
          t_settle_sweep)

    def t_vanished_session():
        ap.enabled = False
        p = verbs.create_pipeline("vanishing", [
            {"role": "one", "engine": "claude", "pid": "p1", "prompt": "first"},
            {"role": "two", "engine": "claude", "pid": "p1", "prompt": "second"},
        ], machine="self")["task"]
        verbs.start_pipeline(p["id"], confirm=True)
        cid = ledger.get(p["id"])["steps"][0]["cid"]
        verbs.close("self", cid, confirm=True)
        _wait(lambda: cid not in client.sessions, what="the session to go away")
        ap.enabled = True
        ap._pipeline_sweep()
        _wait(lambda: ledger.get(p["id"])["steps"][0]["status"] != "running",
              what="the dead step to be force-closed")
        t = ledger.get(p["id"])
        assert t["steps"][1]["status"] in ("running", "done"), t["steps"]
    check("a step whose session vanished is force-closed, not left hanging",
          t_vanished_session)

    def t_readonly_stops_advance():
        guard.autonomy = "readonly"
        r = verbs.advance_pipeline(tid)
        assert r["ok"] is False and r.get("blocked"), r
        guard.autonomy = "confirm"
    check("autonomy=readonly refuses to advance a pipeline", t_readonly_stops_advance)

    ap.stop()
    client.stop()
    mock.stop()
    print()
    if failures:
        print(f"FAILED: {failures}")
        return 1
    print("PASSED: pipelines — multi-engine chains run themselves")
    return 0


if __name__ == "__main__":
    import os
    import sys
    rc = main()
    sys.stdout.flush()
    os._exit(rc)        # skip finalize — daemon WS threads race the buffered writer
