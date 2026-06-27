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
if AGENT_HOME not in sys.path:
    sys.path.insert(0, AGENT_HOME)
from agent import run_turn  # noqa: E402

ALLOWED_TOOLS = ",".join(f"mcp__fleet__{n}" for n, _d, _s in TOOLS)
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

    def _finish(self, reply, trace):
        self.history.append({"role": "assistant", "content": reply})
        return {"reply": reply, "trace": trace}
