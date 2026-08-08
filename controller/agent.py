"""agent.py — the fleet PM brain (adapter over claude-p-agent).

AgentBrain is what the chat server talks to. Each turn calls `run_turn()` from
claude-p-agent (imported via CLAUDE_P_AGENT_HOME) with this adapter's MCP tools
and prompts in controller/prompts/.

Framework contract (claude-p-agent ≥ de70b99, the modules era):
- Subscription routing is the engine's `modules/router` env hook — the 100-line
  duplicate this adapter used to carry is gone. The controller must therefore
  NEVER set CLAUDE_CONFIG_DIR (an explicit pin makes the router module no-op);
  after `git pull` in the engine repo, `tools/module sync` is required or no
  routing happens at all.
- The engine is pinned `engine="claude"`: run_turn hard-errors when extra_args
  is combined with a non-claude engine, so a stray ENGINE= in a shared .env
  must never be able to select one under the PM.
- The PM runs in its own home (config.AGENT_HOME_DIR, outside the repo tree)
  with its own CLAUDE.md and its own memory dir (CLAUDE_P_AGENT_MEMORY), and
  auto-memory off by default — see config.py.
"""
import json
import os
import shutil
import sys
import threading
import time

from . import config
from .mcp import TOOLS

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PROMPTS_DIR = os.path.join(HERE, "prompts")
MCP_CONFIG = os.path.join(HERE, ".mcp-config.json")
HOME_CLAUDE_MD = os.path.join(PROMPTS_DIR, "CLAUDE.pm.md")

AGENT_HOME = os.path.abspath(os.environ.get(
    "CLAUDE_P_AGENT_HOME",
    os.path.expanduser("~/clawd/clawd-harness/projects/claude-p-agent"),
))

_run_turn = None


def _engine_agent_py():
    return os.path.join(AGENT_HOME, "agent.py")


def _missing_engine_msg():
    return (
        f"⚠️ claude-p-agent engine not found at `{AGENT_HOME}` "
        f"(need agent.py). Clone github.com/clawdbotatg/claude-p-agent there, "
        f"or set CLAUDE_P_AGENT_HOME."
    )


def _autonomy_note(mode):
    """The persona lists all autonomy modes but the model is otherwise NOT told which
    one is live — so it defaults to the persona's 'confirm' behavior (propose-and-wait)
    even when the operator set 'auto'. Append this each turn so the model acts on the
    REAL mode. Read fresh per turn, so changing the mode takes effect immediately."""
    mode = (mode or "confirm").lower()
    if mode == "auto":
        return ("\n\n# ACTIVE AUTONOMY MODE: auto\n"
                "You are in AUTO right now. Write tools execute immediately. When the operator "
                "asks for something, DO it — call the write tool with confirm=true straight away. "
                "Do NOT propose-and-wait and do NOT ask for permission you already have. "
                "After acting, report what you did (cids / task ids).")
    if mode == "readonly":
        return ("\n\n# ACTIVE AUTONOMY MODE: readonly\n"
                "Writes are refused right now. Read and propose only; never claim work was done.")
    return ("\n\n# ACTIVE AUTONOMY MODE: confirm\n"
            "You are in CONFIRM right now. Make each write call WITHOUT confirm, relay the "
            "proposal, and STOP until the operator says yes.")


def _get_run_turn():
    """Lazy import — missing engine must not crash the controller at import time."""
    global _run_turn
    if _run_turn is not None:
        return _run_turn
    if not os.path.isfile(_engine_agent_py()):
        return None
    if AGENT_HOME not in sys.path:
        sys.path.insert(0, AGENT_HOME)
    from agent import run_turn as _rt  # noqa: E402
    _run_turn = _rt
    return _run_turn


def _get_forget():
    """Lazy import of the engine's forget() (clears a conversation's memory)."""
    if _get_run_turn() is None:
        return None
    from agent import forget  # noqa: E402
    return forget


# Built-ins are a liability on the fleet box: the PM there has no repos on
# disk, no gh, and every fleet question is answerable through the MCP verbs —
# yet given Bash it will happily burn a whole turn grepping paths that don't
# exist (the 240s-timeout Gmail hunt was 20 Bash calls + 4 doomed GitHub
# curls). So fleet/box mode defaults to Read only, which deletes that failure
# mode outright; direct/laptop mode (a real filesystem worth inspecting) keeps
# the investigation set. Override with CONTROLLER_PM_BUILTINS (comma list, or
# "none"). Write/Edit stay withheld everywhere: the PM delegates code changes
# to the sessions it spawns. Headless `claude -p` DENIES any tool not in
# --allowedTools, which is why these are enumerated.
_DEFAULT_BUILTINS = "Read" if config.fleet_mode() else \
    "Read,Grep,Glob,LS,Bash,WebFetch,WebSearch"
_pm_builtins = config.cfg("CONTROLLER_PM_BUILTINS", "") or _DEFAULT_BUILTINS
_BUILTIN_TOOLS = [] if _pm_builtins.lower() == "none" else \
    [t.strip() for t in _pm_builtins.split(",") if t.strip()]
ALLOWED_TOOLS = ",".join([*(f"mcp__fleet__{n}" for n, _d, _s in TOOLS), *_BUILTIN_TOOLS])
VALID_TRUST = ("private", "public")

# A turn can end with no user-visible text (tool-only turn, or the CLI swallowed
# the answer). Rendering "(no result)" taught the operator nothing — instead the
# PM gets ONE nudge for a real status line, and only if that also comes back
# empty does the chat show the fallback line.
EMPTY_TURN_NUDGE = (
    "Your last turn produced no reply visible to the operator. "
    "Give the operator a one-line status of what you just did or found."
)
EMPTY_TURN_FALLBACK = "PM turn produced no output — see transcript."


def write_mcp_config():
    """Write the MCP config that tells `claude -p` how to launch the controller's
    stdio MCP server, pointed at the same harness + ledger this process drives."""
    cfg = {"mcpServers": {"fleet": {
        "command": os.environ.get("PYTHON", "python3"),
        "args": ["-m", "controller", "mcp"],
        "env": {
            # cwd is now the PM's own home (not this repo), so the subprocess
            # needs the repo root on PYTHONPATH to find `-m controller` at all
            "PYTHONPATH": ROOT,
            # the subprocess proxies its tools through the serve process on this
            # port (see __main__ mcp mode) — explicit opt-in so a hand-run or
            # test `-m controller mcp` never gets hijacked by an unrelated
            # local serve; the rest is the standalone fallback
            "CONTROLLER_MCP_PROXY": "1",
            "CONTROLLER_CHAT_PORT": str(config.CHAT_PORT),
            "CONTROLLER_HARNESS_WS": config.HARNESS_WS,
            "CONTROLLER_HARNESS_TOKEN": config.harness_token(),
            "CONTROLLER_MACHINE": config.MACHINE_ID,
            "CONTROLLER_AUTONOMY": config.AUTONOMY,
            "CONTROLLER_LEDGER": config.LEDGER_PATH,
            "CONTROLLER_RELAY": config.RELAY_URL,
            "CONTROLLER_RELAY_TOKEN": config.RELAY_TOKEN,
        }}}}
    with open(MCP_CONFIG, "w") as f:
        json.dump(cfg, f, indent=2)
    return MCP_CONFIG


def _read_prompt(trust):
    try:
        with open(os.path.join(PROMPTS_DIR, f"{trust}.md"), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _log_turn(evt, **fields):
    """One structured line per turn boundary → stderr → journald. This is the
    observability the 240s-timeout era never had: every timeout/stall/error is
    now a grep away in `journalctl -u clawd-controller`."""
    print("[turn] " + json.dumps({"evt": evt, **fields}),
          file=sys.stderr, flush=True)


class AgentBrain:
    """PM brain — same interface the chat-server Router expects."""

    label = "claude"

    def __init__(self, guard, trust="private", model=None, claude_bin=None,
                 notes=None):
        self.guard = guard
        self.notes = notes              # NotesStore — rendered into every turn
        self.trust = trust if trust in VALID_TRUST else "private"
        self.model = model or (config.AGENT_MODEL or None)
        # Runtime model override (debug page Config tab) wins over the env pin,
        # mirroring the prompt override: a small persisted file, absent = default.
        try:
            with open(config.MODEL_PATH, encoding="utf-8") as f:
                override = f.read().strip()
            if override:
                self.model = override
        except OSError:
            pass
        self.bin = claude_bin or "claude"
        if self.bin != "claude":
            os.environ.setdefault("CLAUDE_BIN", self.bin)
        # Memory uses claude-p-agent's one system: a conversation key → an engine-owned
        # session, auto-resumed. The Router sets this to the current thread's key before
        # each turn; the engine loads/resumes/saves. No session juggling lives here.
        self.conversation_key = None
        # Live turn status for GET /api/turn — lets the chat UI show progress
        # instead of silence during a long turn.
        self.turn_status = {"active": False}
        self.prompt_override = None
        try:
            with open(config.PROMPT_PATH, encoding="utf-8") as f:
                self.prompt_override = f.read() or None
        except OSError:
            pass
        if self.prompt_override is not None:
            # An override silently shadows prompts/private.md — on the box one
            # sat forgotten for weeks while the tracked persona was edited.
            print(f"[pm] WARNING: prompt override active ({config.PROMPT_PATH}) "
                  f"— it SHADOWS controller/prompts/{self.trust}.md; POST "
                  f"/api/prompt {{\"reset\":true}} to drop it", flush=True)
        self._install_home()
        write_mcp_config()

    def _install_home(self):
        """The PM's own cwd/CLAUDE.md + memory dir (context diet — see config).
        Refreshed each boot so persona updates in the repo reach the home."""
        try:
            os.makedirs(config.AGENT_HOME_DIR, exist_ok=True)
            os.makedirs(config.MEMORY_DIR, exist_ok=True)
            if os.path.isfile(HOME_CLAUDE_MD):
                shutil.copyfile(HOME_CLAUDE_MD,
                                os.path.join(config.AGENT_HOME_DIR, "CLAUDE.md"))
            os.environ["CLAUDE_P_AGENT_MEMORY"] = config.MEMORY_DIR
        except OSError as e:
            print(f"[pm] WARNING: could not prepare agent home: {e}", flush=True)

    def forget_conversation(self, key):
        """Clear one conversation's engine memory (used on thread clear/reset)."""
        f = _get_forget()
        if f and key:
            f(key)

    def reset(self):
        self.forget_conversation(self.conversation_key)

    def set_model(self, model):
        """Set the `claude --model` the PM runs as, from the next turn on. Empty →
        back to the CONTROLLER_MODEL env pin (or Claude Code's default). Persists
        across restarts via MODEL_PATH."""
        model = (model or "").strip()
        self.model = model or (config.AGENT_MODEL or None)
        try:
            if model:
                with open(config.MODEL_PATH, "w", encoding="utf-8") as f:
                    f.write(model)
            elif os.path.exists(config.MODEL_PATH):
                os.remove(config.MODEL_PATH)
        except OSError:
            pass

    def default_prompt(self):
        return _read_prompt(self.trust)

    def current_prompt(self):
        return self.prompt_override or self.default_prompt()

    def set_prompt(self, text):
        text = (text or "").strip()
        self.prompt_override = text or None
        try:
            if text:
                with open(config.PROMPT_PATH, "w", encoding="utf-8") as f:
                    f.write(text)
            elif os.path.exists(config.PROMPT_PATH):
                os.remove(config.PROMPT_PATH)
        except OSError:
            pass

    def chat(self, user_text):
        """One user turn → {reply, trace}. Same code path as chat_stream —
        one turn policy for every front-end."""
        return self.chat_stream(user_text, lambda kind, text: None)

    def chat_stream(self, user_text, emit):
        """One user turn, streaming: fires emit(kind, text) per claude event AS
        the turn runs — kind 'tool' (a tool call), 'text' (interim narration),
        or 'final' (the answer if it wasn't already streamed) — and returns
        {reply, trace}. A watchdog enforces the turn policy: TURN_STALL kills a
        turn with no stream events (wedged CLI), TURN_TIMEOUT is the ceiling."""
        run_turn = _get_run_turn()
        if run_turn is None:
            return self._finish(_missing_engine_msg(), [])
        sys_prompt = (self.current_prompt() or "") + _autonomy_note(self.guard.autonomy)
        if self.notes:
            block = self.notes.render()
            if block:                    # durable memory — every thread starts knowing it
                sys_prompt += "\n\n" + block
        os.environ["CONTROLLER_AUTONOMY"] = self.guard.autonomy
        started = time.time()
        stats = {"tools": 0, "last_event": started}
        seen = {"text": ""}
        self.turn_status = {"active": True, "started": started,
                            "thread": self.conversation_key, "tools": 0,
                            "last": ""}

        def _ev(event):
            stats["last_event"] = time.time()
            # Only act on complete assistant messages; ignore partial token deltas
            # (stream_event), system init, and tool-result (user) events.
            if event.get("type") != "assistant":
                return
            for b in (event.get("message") or {}).get("content") or []:
                bt = b.get("type")
                if bt == "text":
                    txt = (b.get("text") or "").strip()
                    if txt:
                        seen["text"] = txt
                        self.turn_status["last"] = txt[:160]
                        emit("text", txt)
                elif bt == "tool_use":
                    stats["tools"] += 1
                    self.turn_status["tools"] = stats["tools"]
                    name = (b.get("name") or "tool").replace("mcp__fleet__", "")
                    inp = b.get("input") or {}
                    arg = ""
                    if inp:
                        try:
                            arg = json.dumps(inp, separators=(",", ":"))
                        except Exception:
                            arg = str(inp)
                        if len(arg) > 200:
                            arg = arg[:197] + "…"
                    self.turn_status["last"] = (name + " " + arg).strip()[:160]
                    emit("tool", (name + " " + arg).strip())

        # Watchdog: stall (no events) or ceiling → terminate the CLI process.
        ph = {}
        verdict = {"kind": "ok"}
        stop_evt = threading.Event()

        def _watchdog():
            while not stop_evt.wait(10):
                now = time.time()
                if config.TURN_STALL and now - stats["last_event"] > config.TURN_STALL:
                    verdict["kind"] = "stalled"
                elif config.TURN_TIMEOUT and now - started > config.TURN_TIMEOUT:
                    verdict["kind"] = "timeout"
                else:
                    continue
                p = ph.get("proc")
                if p is not None:
                    try:
                        p.terminate()
                    except Exception:
                        pass
                return

        threading.Thread(target=_watchdog, daemon=True,
                         name="pm-turn-watchdog").start()
        xargs = ["--mcp-config", MCP_CONFIG, "--allowedTools", ALLOWED_TOOLS]
        if self.model:
            xargs += ["--model", self.model]
        _log_turn("start", thread=self.conversation_key or "",
                  model=self.model or "", autonomy=self.guard.autonomy)

        def _end(outcome, err=None, meta=None):
            stop_evt.set()
            self.turn_status = {"active": False}
            fields = {"thread": self.conversation_key or "",
                      "dur_s": round(time.time() - started, 1),
                      "tools": stats["tools"], "outcome": outcome}
            if isinstance(meta, dict) and meta.get("num_turns") is not None:
                fields["num_turns"] = meta.get("num_turns")
            if err:
                fields["err"] = str(err)[:200]
            _log_turn("end", **fields)

        try:
            meta = run_turn(
                user_text, append_system_prompt=sys_prompt or None,
                remember=self.conversation_key, cwd=config.AGENT_HOME_DIR,
                extra_args=xargs, on_event=_ev, return_meta=True,
                proc_holder=ph, auto_memory=config.AUTO_MEMORY,
                engine="claude",
            )
        except FileNotFoundError as e:
            _end("error", err=e)
            return self._finish(f"⚠️ `{self.bin}` not found — is the Claude CLI installed?", [])
        except RuntimeError as e:
            kind = verdict["kind"] if verdict["kind"] != "ok" else "error"
            _end(kind, err=e)
            msg = {
                "stalled": f"⚠️ turn killed: no activity for {int(config.TURN_STALL)}s "
                           f"(after {stats['tools']} tool calls) — likely a wedged CLI; try again",
                "timeout": f"⚠️ turn killed at the {int(config.TURN_TIMEOUT)}s ceiling "
                           f"(after {stats['tools']} tool calls)",
            }.get(kind, f"⚠️ {e}")
            return self._finish(msg, [])
        def _text_of(m):
            return ((m.get("text") if isinstance(m, dict) else m) or "").strip()

        reply = _text_of(meta)
        outcome = "ok"
        if not reply:
            _log_turn("empty_renudge", thread=self.conversation_key or "")
            try:
                meta2 = run_turn(
                    EMPTY_TURN_NUDGE, append_system_prompt=sys_prompt or None,
                    remember=self.conversation_key, cwd=config.AGENT_HOME_DIR,
                    extra_args=xargs, on_event=_ev, return_meta=True,
                    proc_holder=ph, auto_memory=config.AUTO_MEMORY,
                    engine="claude",
                )
            except (FileNotFoundError, RuntimeError):
                meta2 = None                 # nudge died → fall to the fallback line
            if _text_of(meta2):
                reply, meta = _text_of(meta2), meta2
            else:
                reply, outcome = EMPTY_TURN_FALLBACK, "empty"
        _end(outcome, meta=meta if isinstance(meta, dict) else None)
        if reply != seen["text"]:          # answer wasn't already streamed as the last text block
            emit("final", reply)
        trace = []
        if isinstance(meta, dict):
            tmeta = {k: meta.get(k) for k in ("num_turns", "duration_ms")
                     if meta.get(k) is not None}
            if tmeta:
                trace = [{"tool": "claude", "args": tmeta, "result": {"ok": True}}]
        return self._finish(reply, trace)

    def _finish(self, reply, trace):
        return {"reply": reply, "trace": trace}
