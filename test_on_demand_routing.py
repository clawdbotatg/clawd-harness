#!/usr/bin/env python3
"""On-demand routing (stages 1–2 of ON-DEMAND-SUB-ROUTING-PLAN.md): the pool
is chosen when a prompt needs a model, and only the prompted session moves.

Pins the two lists the plan calls out:

  Pool selection — headroom-first ranking (capable > cool > most headroom,
  reset only a tie-break — the deliberate inverse of _route_key), org-UUID
  collapsing (a second config dir is an alias, not capacity), stale-usage
  stays, hysteresis kills prompt-by-prompt ping-pong.

  Prompt behavior — exactly-once delivery through the per-session routing
  lock: one prompt moves one session, a readiness timeout never delivers into
  both old and new, two racing sends mint at most one handoff, same-org never
  moves, live background work vetoes an optional move, and the carve-outs
  (control sends, ceremonies, non-routing engines) fall through untouched.

Like test_handoff_batch.py this binds the REAL manager methods to a stub and
constructs no SessionManager (a real one would --resume this machine's live
sessions — the scratch-registry trap). Nothing spawns, no registry is touched.

    python3 test_on_demand_routing.py
"""
import threading
import time

import server

NOW = time.time()
FAILED = []


def check(label, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + label
          + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILED.append(label)


class FakeEng:
    routes_accounts = True
    def bg_probe(self, s):
        return False


class FakeSession:
    def __init__(self, cid, account, ready=True):
        self.cid = cid
        self.account = account
        self.alive = True
        self.busy = False
        self.bg = False
        self.ceremony = False
        self.eng = FakeEng()
        self.sent = []                        # what send_message delivered HERE
        self.ready = ready                    # wait_ready's answer
        self.resumed = False                  # set by FakeMgr._handoff
        self.last_handoff = 0.0
        self.transcript_path = "/fake/transcript.jsonl"  # zero-turn tests blank this

    def pilot_note_human_send(self, via, control):
        pass                                  # 🤖 autopilot bookkeeping — not under test here

    def _find_transcript(self):
        return None                           # transcript_path is the whole truth here

    def _has_conversation(self):
        return bool(self.transcript_path)     # real content check tested separately (8c)

    def send_message(self, text, control=False):
        self.sent.append(text)

    def wait_ready(self, timeout=20.0):
        return self.ready


class FakeAccount:
    def __init__(self, name, pct, org="", resets=None, checked_at=None,
                 ready=True, broken=False, fable=True):
        self.name = name
        self.org = org
        self.ready = ready
        self.broken = broken
        self._fable = fable
        windows = ([{"label": "7d", "resets": resets}] if resets else [])
        self.usage = {"pct": pct, "windows": windows,
                      "checkedAt": NOW if checked_at is None else checked_at}

    def routable(self):
        return self._fable


class FakeMgr:
    """Just enough surface for the real preflight to run."""

    # the real thing under test, bound straight off the class
    _route_lock = server.SessionManager._route_lock
    _pool_key = server.SessionManager._pool_key
    _prompt_pool = server.SessionManager._prompt_pool
    _candidates = server.SessionManager._candidates
    _pick_pool = server.SessionManager._pick_pool
    _route_decision = server.SessionManager._route_decision
    _routable_first = server.SessionManager._routable_first
    _log_route = server.SessionManager._log_route
    _route_handoff = server.SessionManager._route_handoff
    send_prompt = server.SessionManager.send_prompt

    def __init__(self, accounts, sessions):
        self.lock = threading.RLock()
        self.accounts = {a.name: a for a in accounts}
        self.sessions = {s.cid: s for s in sessions}
        self._route_locks = {}
        self._stranded_warned = False
        self._stale_route_noted = ""
        self.moves = []                       # (cid, src, dst)
        self.handoff_delay = 0.0              # widen the race window in tests
        self.decline_handoff = False

    def _handoff(self, s, target, why=""):
        """What the real one does to the registry, minus the process: swap in
        a replacement under the same cid, kill the old object."""
        if self.decline_handoff or s.busy or not s.alive or s.ceremony:
            return
        time.sleep(self.handoff_delay)
        fresh = FakeSession(s.cid, target.name, ready=self.fresh_ready)
        fresh.resumed = True
        self.sessions[s.cid] = fresh
        s.alive = False
        self.moves.append((s.cid, s.account, target.name))

    fresh_ready = True


def build(accounts, sessions):
    return FakeMgr(accounts, sessions)


ISO_SOON = "2026-08-20T00:00:00+00:00"
ISO_LATE = "2026-08-25T00:00:00+00:00"

print(__doc__.strip().splitlines()[0])

# capture the import-time default before the tests flip it
DEFAULT_FLAG = server.SUB_ROUTE_ON_PROMPT

# make applied moves real and instant for the whole file
server.SUB_ROUTE_ON_PROMPT = True
server.SUB_ROUTE_SETTLE = 0.0
server.SUB_ROUTE_WAIT = 0.5

# ── pool selection ──────────────────────────────────────────────────────
print("\npool selection (headroom-first, org-collapsed)")

m = build([FakeAccount("a", 70.0, org="A"), FakeAccount("b", 20.0, org="B")], [])
check("1. the capable pool with the most headroom wins",
      m._prompt_pool().name == "b")

m = build([FakeAccount("a", 50.0, org="A", resets=ISO_LATE),
           FakeAccount("b", 50.0, org="B", resets=ISO_SOON)], [])
check("2a. reset time breaks a headroom tie (sooner wins)",
      m._prompt_pool().name == "b")
m = build([FakeAccount("a", 30.0, org="A", resets=ISO_LATE),
           FakeAccount("b", 50.0, org="B", resets=ISO_SOON)], [])
check("2b. materially more headroom beats the sooner reset",
      m._prompt_pool().name == "a")

m = build([FakeAccount("a1", 40.0, org="SHARED"),
           FakeAccount("a2", 40.0, org="SHARED"),
           FakeAccount("b", 90.0, org="B")], [])
pools = {m._prompt_pool().name}
check("3. two dirs with one org UUID collapse to one pool",
      pools == {"a1"})                        # one representative, deterministic

m = build([FakeAccount("nofable", 5.0, org="A", fable=False),
           FakeAccount("fable", 80.0, org="B")], [])
check("4. an incapable pool never wins while a capable one exists",
      m._prompt_pool().name == "fable")

m = build([FakeAccount("cur", 50.0, org="A",
                       checked_at=NOW - 4 * server.USAGE_TTL),
           FakeAccount("other", 10.0, org="B")],
          [FakeSession("c1", "cur")])
d, tgt, why = m._route_decision(m.sessions["c1"])
check("5. stale current usage keeps a usable session in place",
      d == "stay", f"{d}: {why}")

m = build([FakeAccount("cur", 50.0, org="A"),
           FakeAccount("other", 50.0 - server.SUB_HYSTERESIS + 1, org="B")],
          [FakeSession("c1", "cur")])
d, tgt, why = m._route_decision(m.sessions["c1"])
check("6a. a gap under SUB_HYSTERESIS stays put",
      d == "stay", f"{d}: {why}")
m = build([FakeAccount("cur", 50.0, org="A"),
           FakeAccount("other", 50.0 - server.SUB_HYSTERESIS - 1, org="B")],
          [FakeSession("c1", "cur")])
d, tgt, why = m._route_decision(m.sessions["c1"])
check("6b. a gap over SUB_HYSTERESIS moves",
      d == "move" and tgt.name == "other", f"{d}: {why}")

# ── prompt behavior ─────────────────────────────────────────────────────
print("\nprompt behavior (exactly-once through the routing lock)")

def drained_pair(n_sessions=1):
    return build([FakeAccount("dead", 100.0, org="A"),
                  FakeAccount("fresh", 5.0, org="B")],
                 [FakeSession(f"c{i}", "dead") for i in range(n_sessions)])

m = drained_pair(2)
old0 = m.sessions["c0"]
ok = m.send_prompt("c0", "hello")
check("1. prompting one parked session moves only that session",
      ok and len(m.moves) == 1 and m.moves[0][0] == "c0"
      and m.sessions["c1"].account == "dead",
      f"moves={m.moves}")
check("2. the moved session gets the prompt exactly once, post-readiness",
      m.sessions["c0"].sent == ["hello"] and old0.sent == [],
      f"fresh={m.sessions['c0'].sent} old={old0.sent}")

m = drained_pair()
m.fresh_ready = False                        # readiness barrier times out
old0 = m.sessions["c0"]
ok = m.send_prompt("c0", "hello")
check("3. a readiness timeout never delivers into both old and new",
      ok and old0.sent == [] and m.sessions["c0"].sent == ["hello"],
      f"fresh={m.sessions['c0'].sent} old={old0.sent}")

m = drained_pair(2)
m.handoff_delay = 0.05
ts = [threading.Thread(target=m.send_prompt, args=(f"c{i}", f"p{i}"))
      for i in range(2)]
[t.start() for t in ts]; [t.join() for t in ts]
check("4. simultaneous prompts to two sessions route independently",
      len(m.moves) == 2 and m.sessions["c0"].sent == ["p0"]
      and m.sessions["c1"].sent == ["p1"], f"moves={m.moves}")

m = drained_pair()
m.handoff_delay = 0.05                       # widen the race window
ts = [threading.Thread(target=m.send_prompt, args=("c0", f"p{i}"))
      for i in range(2)]
[t.start() for t in ts]; [t.join() for t in ts]
delivered = m.sessions["c0"].sent
check("5. two simultaneous prompts to ONE session mint at most one handoff",
      len(m.moves) == 1, f"moves={m.moves}")
check("5b. …and both prompts land exactly once, on the replacement",
      sorted(delivered) == ["p0", "p1"], f"delivered={delivered}")

m = build([FakeAccount("alias1", 100.0, org="SHARED"),
           FakeAccount("alias2", 3.0, org="SHARED")],
          [FakeSession("c0", "alias1")])
m.send_prompt("c0", "hello")
check("6. a same-organization alias never causes a handoff",
      m.moves == [] and m.sessions["c0"].sent == ["hello"], f"moves={m.moves}")

m = build([FakeAccount("hot", 98.0, org="A"), FakeAccount("cool", 5.0, org="B")],
          [FakeSession("c0", "hot")])
m.sessions["c0"].bg = "shell"
m.send_prompt("c0", "hello")
check("7. live background work vetoes the optional handoff",
      m.moves == [] and m.sessions["c0"].sent == ["hello"], f"moves={m.moves}")

# 2026-08-27: a handoff is a --resume respawn, and a session that has never
# completed a turn has no transcript — the CLI dies with "No conversation
# found with session ID" and the prompt lands in a corpse. Every fresh
# session's FIRST composer send hit this (spawn-time _route_key and
# prompt-time _pool_key rank pools differently by design, so a move was
# near-guaranteed).
m = drained_pair()
m.sessions["c0"].transcript_path = ""        # zero-turn: nothing on disk yet
ok = m.send_prompt("c0", "hello")
check("8. a zero-turn session (no transcript) never moves — nothing to resume",
      ok and m.moves == [] and m.sessions["c0"].sent == ["hello"],
      f"moves={m.moves}")

# …and the hard guard inside the REAL _handoff, for every other handoff path
m = drained_pair()
s0 = m.sessions["c0"]
s0.transcript_path = ""
server.SessionManager._handoff(m, s0, m.accounts["fresh"], why="test")
check("8b. real _handoff declines a transcript-less session outright",
      m.sessions["c0"] is s0 and s0.alive and s0.last_handoff == 0.0)

# 8c: the REAL predicate, on real files. The transcript FILE exists from the
# first hook (SessionStart writes mode/snapshot lines immediately), so
# file-exists is NOT the test — v1 of this guard used it and a fresh
# session's first send was still moved and killed (2026-08-27, second time).
import os, tempfile, types
with tempfile.TemporaryDirectory() as td:
    fresh = os.path.join(td, "fresh.jsonl")     # hook lines only, zero turns
    with open(fresh, "w") as f:
        f.write('{"type":"mode","mode":"normal","sessionId":"x"}\n'
                '{"type":"file-history-snapshot","messageId":"m"}\n')
    talked = os.path.join(td, "talked.jsonl")   # has a real conversation
    with open(talked, "w") as f:
        f.write('{"type":"mode","mode":"normal","sessionId":"x"}\n'
                '{"type":"user","message":{"role":"user","content":"hi"}}\n')
    fake = types.SimpleNamespace(transcript_path=fresh,
                                 _find_transcript=lambda: None)
    a = server.ClaudeSession._has_conversation(fake)
    fake.transcript_path = talked
    b = server.ClaudeSession._has_conversation(fake)
    fake.transcript_path = os.path.join(td, "gone.jsonl")
    c = server.ClaudeSession._has_conversation(fake)
    check("8c. _has_conversation: hook-only file False, real convo True, missing False",
          a is False and b is True and c is False, f"a={a} b={b} c={c}")

# ── carve-outs and the flag ─────────────────────────────────────────────
print("\ncarve-outs (the addenda's fences) and the rollout flag")

m = drained_pair()
m.sessions["c0"].ceremony = True
m.send_prompt("c0", "hello")
check("ceremony sessions fall straight through, never routed",
      m.moves == [] and m.sessions["c0"].sent == ["hello"])

m = drained_pair()
m.sessions["c0"].eng.routes_accounts = False
m.send_prompt("c0", "hello")
check("a non-routing engine (codex) falls straight through",
      m.moves == [] and m.sessions["c0"].sent == ["hello"])

m = drained_pair()
m.send_prompt("c0", "/compact ", control=True)
check("control sends never route (and deliver as control)",
      m.moves == [] and m.sessions["c0"].sent == ["/compact "])

m = drained_pair()
m.sessions["c0"].busy = True
m.send_prompt("c0", "hello")
check("a mid-turn session is never respawned by its own prompt",
      m.moves == [] and m.sessions["c0"].sent == ["hello"])

server.SUB_ROUTE_ON_PROMPT = False
m = drained_pair()
m.send_prompt("c0", "hello")
check("flag OFF: the decision is logged but nothing moves (stage 1)",
      m.moves == [] and m.sessions["c0"].sent == ["hello"])
import os
check("flag defaults OFF (rollout stage 2 is opt-in per box)",
      DEFAULT_FLAG is False or os.environ.get("SUB_ROUTE_ON_PROMPT") == "1")
server.SUB_ROUTE_ON_PROMPT = True

# ── the readiness barrier itself (real session events) ─────────────────
print("\nwait_ready (started AND gate-resolved)")
import types
s = types.SimpleNamespace(
    _started_evt=threading.Event(), _gate_resolved_evt=threading.Event())
wait_ready = server.ClaudeSession.wait_ready
check("neither event → not ready", wait_ready(s, timeout=0.05) is False)
s._started_evt.set()
check("started alone is NOT ready (the addenda race)",
      wait_ready(s, timeout=0.05) is False)
s._gate_resolved_evt.set()
check("started + gate resolved → ready", wait_ready(s, timeout=0.05) is True)

if FAILED:
    print(f"\n{len(FAILED)} FAILED: {FAILED}")
    raise SystemExit(1)
print("\nall on-demand-routing checks passed")
