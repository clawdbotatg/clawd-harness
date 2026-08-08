#!/usr/bin/env python3
"""Regression test: a PM turn that ends with no user-visible text never shows
"(no result)" in the chat.

The contract (agent.py chat_stream): an empty turn gets ONE automatic re-prompt
(EMPTY_TURN_NUDGE) on the same conversation; if that also comes back empty (or
errors), the reply is the plain EMPTY_TURN_FALLBACK line. Normal turns are
untouched — exactly one engine call, reply passed through verbatim.

Run:  python3 -m controller.test_empty_turn
"""
import os
import tempfile

# Point every path the brain touches at a throwaway dir BEFORE importing agent,
# so the test never writes into the real ~/.clawd-controller or repo root.
_TMP = tempfile.mkdtemp(prefix="pm-empty-turn-")
from . import config  # noqa: E402
config.AGENT_HOME_DIR = os.path.join(_TMP, "home")
config.MEMORY_DIR = os.path.join(_TMP, "memory")
config.PROMPT_PATH = os.path.join(_TMP, "prompt.txt")
config.MODEL_PATH = os.path.join(_TMP, "model.txt")

from . import agent as agent_mod  # noqa: E402
from .agent import AgentBrain, EMPTY_TURN_FALLBACK, EMPTY_TURN_NUDGE  # noqa: E402

agent_mod.MCP_CONFIG = os.path.join(_TMP, "mcp-config.json")


class StubGuard:
    autonomy = "readonly"


class FakeEngine:
    """Stands in for claude-p-agent's run_turn: returns scripted texts in order."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []                     # prompts received, in order

    def __call__(self, user_text, **kw):
        self.calls.append(user_text)
        return {"text": self.replies.pop(0), "num_turns": 1}


def _brain(replies):
    agent_mod._run_turn = FakeEngine(replies)
    b = AgentBrain(StubGuard(), trust="private", claude_bin="claude")
    return b, agent_mod._run_turn


def main():
    failures = []

    def check(name, fn):
        try:
            fn()
            print(f"  ✓ {name}")
        except Exception as e:
            print(f"  ✗ {name}: {e}")
            failures.append(name)

    def normal_turn_untouched():
        b, eng = _brain(["all good"])
        out = b.chat("status?")
        assert out["reply"] == "all good", out
        assert len(eng.calls) == 1, eng.calls

    def empty_turn_gets_one_nudge():
        b, eng = _brain(["", "spawned c3 on heart, building now"])
        out = b.chat("go build it")
        assert out["reply"] == "spawned c3 on heart, building now", out
        assert len(eng.calls) == 2, eng.calls
        assert eng.calls[1] == EMPTY_TURN_NUDGE, eng.calls
        assert "(no result)" not in out["reply"]

    def double_empty_falls_back():
        b, eng = _brain(["", ""])
        out = b.chat("go build it")
        assert out["reply"] == EMPTY_TURN_FALLBACK, out
        assert len(eng.calls) == 2, eng.calls          # exactly one retry, no loop
        assert "(no result)" not in out["reply"]

    def nudge_error_falls_back():
        class DyingEngine(FakeEngine):
            def __call__(self, user_text, **kw):
                self.calls.append(user_text)
                if len(self.calls) == 2:
                    raise RuntimeError("nudge turn died")
                return {"text": "", "num_turns": 1}
        agent_mod._run_turn = DyingEngine([])
        b = AgentBrain(StubGuard(), trust="private", claude_bin="claude")
        out = b.chat("go build it")
        assert out["reply"] == EMPTY_TURN_FALLBACK, out
        assert len(agent_mod._run_turn.calls) == 2, agent_mod._run_turn.calls

    check("normal turn: 1 engine call, reply verbatim", normal_turn_untouched)
    check("empty turn: one nudge, nudged reply shown", empty_turn_gets_one_nudge)
    check("empty twice: fallback line, no '(no result)'", double_empty_falls_back)
    check("nudge errors: fallback line, no crash", nudge_error_falls_back)

    if failures:
        raise SystemExit(f"FAILED: {failures}")
    print("controller empty-turn guardrail: all checks passed")


if __name__ == "__main__":
    main()
