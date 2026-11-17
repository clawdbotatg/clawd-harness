#!/usr/bin/env python3
"""A PM turn that hits the usage-limit wall is re-run on the fallback model.

The contract (agent.py chat_stream + limits.py): a walled turn on the primary
model is retried once on config.FALLBACK_MODEL with the SAME operator message;
the reply carries one line naming the model; the raw wall never reaches the
operator; the primary comes back after the reset time, or on a thread that has
not run on the fallback yet. Normal turns: one engine call, reply verbatim.

Run:  python3 -m controller.test_model_fallback
"""
import datetime
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="pm-fallback-")
from . import config  # noqa: E402
config.AGENT_HOME_DIR = os.path.join(_TMP, "home")
config.MEMORY_DIR = os.path.join(_TMP, "memory")
config.PROMPT_PATH = os.path.join(_TMP, "prompt.txt")
config.MODEL_PATH = os.path.join(_TMP, "model.txt")
config.FALLBACK_PATH = os.path.join(_TMP, "fallback.json")
config.FALLBACK_MODEL = "claude-opus-5-5"
config.FALLBACK_RETRY_S = 1800

from . import agent as agent_mod, limits  # noqa: E402
from .agent import AgentBrain  # noqa: E402

agent_mod.MCP_CONFIG = os.path.join(_TMP, "mcp-config.json")

PRIMARY = "claude-fable-5-2"
WALL = ("You've hit your Fable limit · resets 9pm (UTC). Switch to another "
        "model, or manage usage credits at https://claude.ai/settings/usage, "
        "to continue.")


class StubGuard:
    autonomy = "readonly"


class Engine:
    """Fake run_turn: models in `walled` die the way the real CLI does."""

    def __init__(self, walled=(), as_text=False, wall=WALL):
        self.walled, self.as_text, self.wall = set(walled), as_text, wall
        self.calls = []                 # (model, prompt)

    def __call__(self, user_text, extra_args=None, **kw):
        x = list(extra_args or [])
        model = x[x.index("--model") + 1] if "--model" in x else ""
        self.calls.append((model, user_text))
        if model in self.walled:
            if self.as_text:
                return {"text": self.wall, "num_turns": 1}
            raise RuntimeError(f"claude exited 1: {self.wall}")
        return {"text": f"answer from {model}", "num_turns": 1}


def _brain(engine, key="pm-t1"):
    if os.path.exists(config.FALLBACK_PATH):
        os.remove(config.FALLBACK_PATH)
    agent_mod._run_turn = engine
    b = AgentBrain(StubGuard(), trust="private", model=PRIMARY)
    b.conversation_key = key
    return b


def main():
    failures = []

    def check(name, fn):
        try:
            fn()
            print(f"  ✓ {name}")
        except Exception as e:
            print(f"  ✗ {name}: {type(e).__name__}: {e}")
            failures.append(name)

    def normal_turn_untouched():
        eng = Engine()
        out = _brain(eng).chat("status?")
        assert out["reply"] == f"answer from {PRIMARY}", out
        assert eng.calls == [(PRIMARY, "status?")], eng.calls

    def wall_retries_same_message_on_fallback():
        for as_text in (False, True):            # exit-1 wall / wall as the turn text
            eng = Engine(walled={PRIMARY}, as_text=as_text)
            b = _brain(eng)
            seen = []
            out = b.chat_stream("what is running?", lambda k, t: seen.append((k, t)))
            assert eng.calls == [(PRIMARY, "what is running?"),
                                 ("claude-opus-5-5", "what is running?")], eng.calls
            assert out["reply"].startswith("answer from claude-opus-5-5"), out
            assert "hit your" not in out["reply"], out
            note = out["reply"].splitlines()[-1]
            assert "Opus 5.5" in note and "Fable 5.2" in note and "21:00 UTC" in note, note
            assert ("final", "answer from claude-opus-5-5") in seen, seen
            assert ("text", note) in seen, seen
            assert not any("hit your" in t for _k, t in seen), seen

    def stays_on_fallback_until_reset():
        eng = Engine(walled={PRIMARY})
        b = _brain(eng)
        b.chat("one")
        out = b.chat("two")                       # same thread: no wasted primary try
        assert eng.calls[2:] == [("claude-opus-5-5", "two")], eng.calls
        assert "Opus 5.5" in out["reply"], out
        # reset time passes, Fable has headroom again
        b.fallback["until"] = 1
        eng.walled.clear()
        out = b.chat("three")
        assert eng.calls[-1] == (PRIMARY, "three"), eng.calls
        assert out["reply"] == f"answer from {PRIMARY}", out
        assert b.fallback is None and not os.path.exists(config.FALLBACK_PATH)

    def fresh_thread_tries_primary_first():
        eng = Engine(walled={PRIMARY})
        b = _brain(eng)
        b.chat("one")
        eng.walled.clear()                        # headroom is back early
        b.conversation_key = "pm-t2"
        out = b.chat("hello")
        assert eng.calls[-1] == (PRIMARY, "hello"), eng.calls
        assert out["reply"] == f"answer from {PRIMARY}", out

    def state_survives_restart():
        eng = Engine(walled={PRIMARY})
        b = _brain(eng)
        b.chat("one")
        b2 = AgentBrain(StubGuard(), trust="private", model=PRIMARY)
        assert b2.fallback and b2.fallback["label"] == "21:00 UTC", b2.fallback
        b2.set_model("claude-sonnet-5-5")         # a deliberate pick clears it
        assert b2.fallback is None
        b2.set_model("")                          # drop the persisted override

    def both_walled_is_a_clean_line():
        eng = Engine(walled={PRIMARY, "claude-opus-5-5"})
        seen = []
        out = _brain(eng).chat_stream("hi", lambda k, t: seen.append((k, t)))
        assert len(eng.calls) == 2, eng.calls
        assert "hit your" not in out["reply"] and "usage limit" in out["reply"], out
        assert seen == [("final", out["reply"])], seen

    def other_errors_do_not_fall_back():
        class Dead(Engine):
            def __call__(self, user_text, extra_args=None, **kw):
                self.calls.append(("", user_text))
                raise RuntimeError("claude exited 1: MCP server crashed")
        eng = Dead()
        seen = []
        out = _brain(eng).chat_stream("hi", lambda k, t: seen.append((k, t)))
        assert len(eng.calls) == 1 and "MCP server crashed" in out["reply"], out
        assert seen == [("final", out["reply"])], seen   # Telegram hears the error too

    def credits_wall_and_no_reset_time():
        eng = Engine(walled={PRIMARY}, wall="Fable requires usage credits. Run /usage-credits.")
        out = _brain(eng).chat("hi")
        note = out["reply"].splitlines()[-1]
        assert out["reply"].startswith("answer from claude-opus-5-5"), out
        assert "Opus 5.5" in note and "30 min" in note, note

    def a_quoted_wall_is_not_a_wall():
        long = "Session c3 on heart shows: " + WALL
        assert not limits.is_wall_reply(long)
        assert limits.is_wall_reply(WALL)
        assert limits.parse_wall("all sessions are fine") is None

    def reset_time_parsing():
        utc = datetime.timezone.utc
        now = datetime.datetime(2026, 11, 17, 1, 48, tzinfo=utc).timestamp()
        w = limits.parse_wall(WALL, now)
        want = datetime.datetime(2026, 11, 17, 21, 0, tzinfo=utc).timestamp()
        assert w["model"] == "Fable" and w["until"] == want + limits.RESET_GRACE_S, w
        late = datetime.datetime(2026, 11, 17, 22, 0, tzinfo=utc).timestamp()
        w = limits.parse_wall(WALL, late)          # 9pm already gone → tomorrow
        assert w["until"] == want + 86400 + limits.RESET_GRACE_S, w
        w = limits.parse_wall("You've hit your session limit · resets 9:30am (UTC)", now)
        assert w["model"] == "" and w["label"] == "09:30 UTC", w
        w = limits.parse_wall("You’ve reached your Fable limit · resets Nov 20, 3pm (UTC)", now)
        assert w["label"] == "Nov 20 15:00 UTC", w
        assert limits.model_label("claude-opus-5-5") == "Opus 5.5"
        assert limits.model_label("claude-haiku-4-5-20251001") == "Haiku 4.5"

    check("normal turn: 1 engine call, reply verbatim", normal_turn_untouched)
    check("wall: same message re-run on the fallback, note added, no raw wall",
          wall_retries_same_message_on_fallback)
    check("stays on the fallback, returns to the primary after the reset",
          stays_on_fallback_until_reset)
    check("a fresh thread tries the primary first", fresh_thread_tries_primary_first)
    check("fallback state survives a restart; set_model clears it", state_survives_restart)
    check("both models walled: clean line, no raw wall", both_walled_is_a_clean_line)
    check("non-limit errors do not fall back", other_errors_do_not_fall_back)
    check("credits wall with no reset time", credits_wall_and_no_reset_time)
    check("an answer quoting a wall is not a wall", a_quoted_wall_is_not_a_wall)
    check("reset time parsing + model labels", reset_time_parsing)

    if failures:
        raise SystemExit(f"FAILED: {failures}")
    print("controller model fallback: all checks passed")


if __name__ == "__main__":
    main()
