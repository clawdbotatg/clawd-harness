#!/usr/bin/env python3
"""🤖 Autopilot (2026-08-28): the checkbox beside the state square that hands a
session to an LLM supervisor — on every Stop it reads goal + transcript and
either types the next nudge or parks with a reason in the 🤖 row.

What must hold (each guards a real failure mode):
  - continue → exactly one send via the manager preflight, round counter bumps,
    the 🤖 row narrates; needs_human/done → NO send (an unattended loop that
    keeps typing into a parked session is the nightmare scenario).
  - the round cap parks the pilot without an LLM call, and a human send refills
    the budget (steering is expected, not an off switch) — but the pilot's OWN
    via:"pilot" sends must never refill it, or the cap is no cap at all.
  - a slash-command "prompt" from the supervisor is dropped: typed into the TUI
    it would drive claude's command menu, not the conversation.
  - busy / disengaged / hook-moved at the end of the grace sleep drops the step.
  - the fields are ctor params + registry rows, so clone_for_respawn and a
    restart carry an engaged pilot across (the pins-reverted lesson, 765c759).

Constructs NO SessionManager and spawns nothing (the scratch-registry trap: a
real manager here would --resume the live sessions). ClaudeSession alone is
inert until .start().

    python3 test_autopilot.py
"""
import inspect
import server

FAILS = []


def check(name, cond):
    print(("  ok  " if cond else "  FAIL") + " " + name)
    if not cond:
        FAILS.append(name)


class FakeMgr:
    def __init__(self):
        self.sent = []          # (cid, text, via)
        self.saves = 0
        self.broadcasts = 0

    def send_prompt(self, cid, text, via="", control=False):
        self.sent.append((cid, text, via))
        return True

    def save_registry(self):
        self.saves += 1

    def broadcast_sessions(self):
        self.broadcasts += 1


def make_session(**kw):
    mgr = FakeMgr()
    s = server.ClaudeSession(mgr, cid="cid12345", session_id="", resuming=False,
                             pid="p1", **kw)
    s.alive = True
    s.first_prompt = "build the widget"
    return mgr, s


# The step's grace sleep is real time — zero it for the tests, and stub the
# pieces that would do I/O (transcript read, prompt log, the LLM gateway).
server.PILOT_DELAY = 0
server.log_prompt = lambda *a, **k: None

def stub_llm(reply):
    """All _llm_json calls answer `reply` (goal derivation included)."""
    server._llm_json = lambda sys_prompt, text, max_tokens=120, model="": reply


print("knobs:")
check("feature defaults ON", server.AUTO_PILOT)
check("round cap is positive and sane", 0 < server.PILOT_MAX_ROUNDS <= 100)
check("supervisor demands compact JSON with the three actions",
      all(w in server.PILOT_SYS_PROMPT for w in
          ("continue", "needs_human", "done", "JSON")))

print("continue path:")
mgr, s = make_session(autopilot=1.0, pilot_goal="ship the widget")
s._pilot_goal_stale = False
s._transcript_text_for_naming = lambda cap=3500: "User: go\nClaude: done part 1"
stub_llm({"action": "continue", "prompt": "run the tests and fix failures",
          "status": "part 1 done, testing next"})
s._pilot_step()
check("exactly one send", len(mgr.sent) == 1)
check("send is via:'pilot' through the manager preflight",
      mgr.sent and mgr.sent[0][2] == "pilot")
check("round counter bumped", s.pilot_rounds == 1)
check("🤖 row narrates with the round count",
      "part 1 done" in s.pilot_status and "1/" in s.pilot_status)
check("status persisted (registry saved)", mgr.saves >= 1)

print("needs_human / done park without sending:")
mgr, s = make_session(autopilot=1.0, pilot_goal="g")
s._pilot_goal_stale = False
s._transcript_text_for_naming = lambda cap=3500: "User: go\nClaude: which db?"
stub_llm({"action": "needs_human", "prompt": "", "status": "needs a db choice"})
s._pilot_step()
check("needs_human sends nothing", not mgr.sent)
check("needs_human row flags you", s.pilot_status.startswith("🙋"))
stub_llm({"action": "done", "prompt": "", "status": "shipped and verified"})
s._pilot_step()
check("done sends nothing", not mgr.sent)
check("done row shows ✅", s.pilot_status.startswith("✅"))

print("slash-command prompt is refused:")
mgr, s = make_session(autopilot=1.0, pilot_goal="g")
s._pilot_goal_stale = False
s._transcript_text_for_naming = lambda cap=3500: "User: go\nClaude: ok"
stub_llm({"action": "continue", "prompt": "/compact", "status": "tidy up"})
s._pilot_step()
check("no send for a slash command", not mgr.sent)
check("no round burned on it", s.pilot_rounds == 0)

print("round cap:")
mgr, s = make_session(autopilot=1.0, pilot_goal="g",
                      pilot_rounds=server.PILOT_MAX_ROUNDS)
s._pilot_goal_stale = False
called = []
server._llm_json = lambda *a, **k: called.append(1) or {"action": "continue",
                                                        "prompt": "x", "status": "y"}
s._pilot_step()
check("cap parks without an LLM call", not called and not mgr.sent)
check("cap row says paused", s.pilot_status.startswith("⏸"))
s.pilot_note_human_send("typed", control=False)
check("human send refills the budget", s.pilot_rounds == 0)
check("…and marks the goal stale", s._pilot_goal_stale)
s.pilot_rounds = 5
s.pilot_note_human_send("pilot", control=False)
s.pilot_note_human_send("auto", control=False)
s.pilot_note_human_send("typed", control=True)
check("pilot/auto/control sends never refill", s.pilot_rounds == 5)

print("boundary guards drop the step:")
mgr, s = make_session(autopilot=1.0, pilot_goal="g")
s._pilot_goal_stale = False
stub_llm({"action": "continue", "prompt": "x", "status": "y"})
s.busy = True
s._pilot_step()
check("busy session — no send", not mgr.sent)
s.busy = False
s.autopilot = 0.0
s._pilot_step()
check("disengaged — no send", not mgr.sent)

print("durability:")
sig = inspect.signature(server.ClaudeSession.__init__).parameters
check("pilot fields are ctor params (clone_for_respawn carries them)",
      all(k in sig for k in ("autopilot", "pilot_goal", "pilot_status",
                             "pilot_rounds")))
mgr, s = make_session(autopilot=42.0, pilot_goal="g", pilot_status="▶ x",
                      pilot_rounds=3)
reg = s.to_registry()
check("registry row round-trips the pilot state",
      reg["autopilot"] == 42.0 and reg["pilot_goal"] == "g"
      and reg["pilot_status"] == "▶ x" and reg["pilot_rounds"] == 3)
meta = s.meta()
check("meta broadcasts autopilot/pilotStatus/pilotRounds",
      meta["autopilot"] is True and meta["pilotStatus"] == "▶ x"
      and meta["pilotRounds"] == 3)

print("auto-tldr yields to the pilot:")
import re as _re
src = open(server.__file__).read()
gate = _re.search(r"if \(AUTO_TLDR and armed[\s\S]{0,200}?_auto_tldr", src)
check("the Stop gate excludes autopilot sessions",
      gate and "not self.autopilot" in gate.group(0))

if FAILS:
    print("\n%d FAILED" % len(FAILS))
    raise SystemExit(1)
print("\nall passed")
