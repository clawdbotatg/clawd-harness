"""A second PM brain backend: Claude Code in print mode (`claude -p`).

Same `.chat()` interface as the Bankr brain, but the agent loop is Claude Code's
own: we shell out to `claude -p` with the controller's **MCP server** attached, so
Claude reads/writes the fleet through the exact same tools. State is shared — the
MCP subprocess talks to the same harness and appends to the same ledger file as
the in-process Kimi brain, so you can switch backends mid-stream.

Multi-turn continuity uses `--resume <session_id>` captured from the JSON output.
Env is scrubbed (CLAUDECODE/CLAUDE_CODE_*/ANTHROPIC_API_KEY) so it runs as a
pristine top-level subscription session, mirroring server.py's SCRUB_ENV.
"""
import json
import os
import shutil
import subprocess

from . import config
from .mcp import TOOLS

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MCP_CONFIG = os.path.join(HERE, ".mcp-config.json")
SCRUB = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_EXECPATH",
         "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION", "ANTHROPIC_API_KEY",
         "AI_AGENT")
ALLOWED_TOOLS = ",".join(f"mcp__fleet__{n}" for n, _d, _s in TOOLS)


def write_mcp_config():
    """Write the MCP config that tells `claude -p` how to launch the controller's
    stdio MCP server, pointed at the same harness + ledger this process uses."""
    cfg = {"mcpServers": {"fleet": {
        "command": os.environ.get("PYTHON", "python3"),
        "args": ["-m", "controller", "mcp"],
        "env": {
            "CONTROLLER_HARNESS_WS": config.HARNESS_WS,
            "CONTROLLER_HARNESS_TOKEN": config.harness_token(),
            "CONTROLLER_MACHINE": config.MACHINE_ID,
            "CONTROLLER_AUTONOMY": config.AUTONOMY,
            "CONTROLLER_LEDGER": config.LEDGER_PATH,
        }}}}
    with open(MCP_CONFIG, "w") as f:
        json.dump(cfg, f, indent=2)
    return MCP_CONFIG


class ClaudeCodeBrain:
    label = "claude-code"

    def __init__(self, guard, model=None, claude_bin=None):
        self.guard = guard               # shared gate (the MCP subprocess reads its mode via env)
        self.model = model               # None → Claude Code's default
        self.bin = claude_bin or shutil.which("claude") or "claude"
        self.session_id = None
        self.history = []                # kept for chat-server reset() parity
        write_mcp_config()

    def reset(self):
        self.session_id = None
        self.history = []

    # -- thread state (each PM thread keeps its own --resume continuity) --------
    def export_state(self):
        return {"history": list(self.history), "session_id": self.session_id}

    def import_state(self, state):
        state = state or {}
        self.history = list(state.get("history") or [])
        self.session_id = state.get("session_id")

    def _env(self):
        env = dict(os.environ)
        for k in SCRUB:
            env.pop(k, None)
        # the MCP subprocess must see the *current* autonomy mode
        env["CONTROLLER_AUTONOMY"] = self.guard.autonomy
        return env

    def chat(self, user_text):
        cmd = [self.bin, "-p", user_text,
               "--mcp-config", MCP_CONFIG,
               "--allowedTools", ALLOWED_TOOLS,
               "--output-format", "json"]
        if self.model:
            cmd += ["--model", self.model]
        if self.session_id:
            cmd += ["--resume", self.session_id]
        try:
            proc = subprocess.run(cmd, cwd=ROOT, env=self._env(),
                                  capture_output=True, text=True, timeout=240)
        except subprocess.TimeoutExpired:
            return {"reply": "⚠️ Claude Code timed out (240s).", "trace": []}
        except FileNotFoundError:
            return {"reply": f"⚠️ `{self.bin}` not found — is the Claude CLI installed?", "trace": []}
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()[-600:]
            return {"reply": f"⚠️ Claude Code exited {proc.returncode}: {err}", "trace": []}
        out = proc.stdout.strip()
        try:
            data = json.loads(out)
        except Exception:
            return {"reply": out or "(no output)", "trace": []}
        if data.get("session_id"):
            self.session_id = data["session_id"]
        reply = data.get("result") or data.get("text") or "(no result)"
        meta = {k: data.get(k) for k in ("num_turns", "total_cost_usd", "duration_ms") if k in data}
        trace = [{"tool": "claude-code", "args": meta, "result": {"ok": True}}] if meta else []
        return {"reply": reply, "trace": trace}
