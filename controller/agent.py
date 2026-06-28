"""agent.py — the fleet PM brain (adapter over claude-p-agent).

AgentBrain is what the chat server talks to. Each turn calls `run_turn()` from
claude-p-agent (imported via CLAUDE_P_AGENT_HOME) with this adapter's MCP tools
and prompts in controller/prompts/.
"""
import json
import os
import sys

from . import config
from .mcp import TOOLS

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PROMPTS_DIR = os.path.join(HERE, "prompts")
MCP_CONFIG = os.path.join(HERE, ".mcp-config.json")

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


# The PM gets the fleet MCP verbs PLUS read/investigation built-ins, so it can
# inspect repos directly (gh/git via Bash, fetch docs, read files) instead of driving
# sessions blind. Headless `claude -p` DENIES any tool not in --allowedTools, which is
# why these must be enumerated. Write/Edit are deliberately withheld: the PM delegates
# actual code changes to the coding sessions it spawns/assigns.
_BUILTIN_TOOLS = ["Read", "Grep", "Glob", "LS", "Bash", "WebFetch", "WebSearch"]
ALLOWED_TOOLS = ",".join([*(f"mcp__fleet__{n}" for n, _d, _s in TOOLS), *_BUILTIN_TOOLS])
VALID_TRUST = ("private", "public")


def write_mcp_config():
    """Write the MCP config that tells `claude -p` how to launch the controller's
    stdio MCP server, pointed at the same harness + ledger this process drives."""
    cfg = {"mcpServers": {"fleet": {
        "command": os.environ.get("PYTHON", "python3"),
        "args": ["-m", "controller", "mcp"],
        "env": {
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


def _extra_args(model):
    args = ["--mcp-config", MCP_CONFIG, "--allowedTools", ALLOWED_TOOLS,
            "--output-format", "json"]
    if model:
        args += ["--model", model]
    return args


class AgentBrain:
    """PM brain — same interface the chat-server Router expects."""

    label = "claude"

    def __init__(self, guard, trust="private", model=None, claude_bin=None):
        self.guard = guard
        self.trust = trust if trust in VALID_TRUST else "private"
        self.model = model or (config.AGENT_MODEL or None)
        self.bin = claude_bin or "claude"
        if self.bin != "claude":
            os.environ.setdefault("CLAUDE_BIN", self.bin)
        self.session_id = None
        self.history = []
        self.prompt_override = None
        try:
            with open(config.PROMPT_PATH, encoding="utf-8") as f:
                self.prompt_override = f.read() or None
        except OSError:
            pass
        write_mcp_config()

    def reset(self):
        self.session_id = None
        self.history = []

    def export_state(self):
        return {"history": list(self.history), "session_id": self.session_id}

    def import_state(self, state):
        state = state or {}
        self.history = list(state.get("history") or [])
        self.session_id = state.get("session_id")

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
        """One user turn → {reply, trace}."""
        run_turn = _get_run_turn()
        if run_turn is None:
            return self._finish(_missing_engine_msg(), [])
        self.history.append({"role": "user", "content": user_text})
        sys_prompt = self.current_prompt()
        os.environ["CONTROLLER_AUTONOMY"] = self.guard.autonomy
        try:
            out = run_turn(
                user_text,
                append_system_prompt=sys_prompt or None,
                session_id=self.session_id,
                cwd=ROOT,
                extra_args=_extra_args(self.model),
                timeout=240,
            )
        except FileNotFoundError:
            return self._finish(f"⚠️ `{self.bin}` not found — is the Claude CLI installed?", [])
        except RuntimeError as e:
            return self._finish(f"⚠️ {e}", [])
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            return self._finish(out or "(no output)", [])
        if data.get("session_id"):
            self.session_id = data["session_id"]
        reply = data.get("result") or data.get("text") or "(no result)"
        meta = {k: data.get(k) for k in ("num_turns", "duration_ms") if k in data}
        trace = [{"tool": "claude", "args": meta, "result": {"ok": True}}] if meta else []
        return self._finish(reply, trace)

    def chat_stream(self, user_text, emit):
        """Like chat(), but fires emit(kind, text) per claude event AS the turn runs
        — kind 'tool' (a tool call), 'text' (interim narration), or 'final' (the answer
        if it wasn't already streamed) — then returns the same {reply, trace} as chat().
        Used by the Telegram front-end so it shows work in progress, not one final dump."""
        run_turn = _get_run_turn()
        if run_turn is None:
            return self._finish(_missing_engine_msg(), [])
        self.history.append({"role": "user", "content": user_text})
        sys_prompt = self.current_prompt()
        os.environ["CONTROLLER_AUTONOMY"] = self.guard.autonomy
        seen = {"text": ""}

        def _ev(event):
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
                        emit("text", txt)
                elif bt == "tool_use":
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
                    emit("tool", (name + " " + arg).strip())

        # Streaming drops the blocking `--output-format json`; run_turn adds stream-json
        # because on_event is set.
        xargs = ["--mcp-config", MCP_CONFIG, "--allowedTools", ALLOWED_TOOLS]
        if self.model:
            xargs += ["--model", self.model]
        try:
            meta = run_turn(
                user_text, append_system_prompt=sys_prompt or None,
                session_id=self.session_id, cwd=ROOT, extra_args=xargs,
                on_event=_ev, return_meta=True,
            )
        except FileNotFoundError:
            return self._finish(f"⚠️ `{self.bin}` not found — is the Claude CLI installed?", [])
        except RuntimeError as e:
            return self._finish(f"⚠️ {e}", [])
        sid = meta.get("session_id") if isinstance(meta, dict) else None
        if sid:
            self.session_id = sid
        reply = ((meta.get("text") if isinstance(meta, dict) else meta) or "").strip() or "(no result)"
        if reply != seen["text"]:          # answer wasn't already streamed as the last text block
            emit("final", reply)
        return self._finish(reply, [])

    def _finish(self, reply, trace):
        self.history.append({"role": "assistant", "content": reply})
        return {"reply": reply, "trace": trace}
