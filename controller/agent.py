"""agent.py — the PM brain, as a minimal claude-p-agent.

This is the whole engine, ported from `clawdbotatg/claude-p-agent` and pointed at
the fleet. The thesis of that repo: **an agent is `claude -p` run in a directory,
fed a message tagged by trust level, with tools.** There is no orchestration
framework, no JSON-action protocol to babysit a weak model — **Claude Code is the
loop.** We just hand it the fleet tools (the controller's MCP server) and a
persona, and read back what it said.

Why this replaced the old two-brain setup (Bankr gateway + a separate claude_brain):
  • The Bankr brain ran a cheap metered model (haiku-4.5) over a hand-rolled
    one-JSON-object-per-step protocol — the "crappy LLM" the PM felt like.
  • Real Claude with native tool-calling is strictly better at the PM job, and a
    clean top-level `claude -p` (env scrubbed) runs on the **subscription**, not
    metered API credits — the same trick server.py uses for sessions.

A turn:
  1. pick a system prompt by trust level     (prompts/private.md | prompts/public.md)
  2. scrub the environment                    (so the child runs on YOUR subscription)
  3. spawn `claude -p` in the repo root with the fleet MCP server attached
  4. hand back {reply, trace}, and remember the session id for --resume continuity

Trust: the chat UI / Telegram are YOU, so they run `private` (full, write-capable
tools, gated by the autonomy guard). `public` is the locked-down persona for any
future untrusted adapter — read-only, never acts. The real boundary lives in the
verbs' autonomy gate + the per-trust prompt, not just here.
"""
import json
import os
import shutil
import subprocess

from . import config
from .mcp import TOOLS

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PROMPTS_DIR = os.path.join(HERE, "prompts")
MCP_CONFIG = os.path.join(HERE, ".mcp-config.json")

# The fleet tools, namespaced as Claude Code exposes MCP tools. We allowlist
# exactly these so the PM can drive the fleet but nothing else by default.
ALLOWED_TOOLS = ",".join(f"mcp__fleet__{n}" for n, _d, _s in TOOLS)

# ── the one non-obvious thing (same as server.py SCRUB_ENV / claude-p-agent) ──
# A `claude` shelled from inside an environment that already has these set
# detects it's "embedded", switches to metered API billing, and writes no
# transcript. Scrubbing them makes the child a clean, top-level run on your
# Claude subscription. Do not remove this.
SCRUB_PREFIXES = ("CLAUDECODE", "CLAUDE_CODE_")
SCRUB_EXACT = {"ANTHROPIC_API_KEY", "AI_AGENT"}

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
            # box/fleet mode passes through so the MCP child drives the relay too
            "CONTROLLER_RELAY": config.RELAY_URL,
            "CONTROLLER_RELAY_TOKEN": config.RELAY_TOKEN,
        }}}}
    with open(MCP_CONFIG, "w") as f:
        json.dump(cfg, f, indent=2)
    return MCP_CONFIG


def _prompt_path(trust):
    return os.path.join(PROMPTS_DIR, f"{trust}.md")


def _read_prompt(trust):
    try:
        with open(_prompt_path(trust), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def scrubbed_env(autonomy):
    env = dict(os.environ)
    for k in list(env):
        if k in SCRUB_EXACT or any(k.startswith(p) for p in SCRUB_PREFIXES):
            env.pop(k, None)
    # the MCP subprocess must see the *current* autonomy mode (it gates writes)
    env["CONTROLLER_AUTONOMY"] = autonomy
    return env


class AgentBrain:
    """The PM brain. Same interface the chat-server Router expects from a brain
    (chat / reset / history / import_state / export_state) plus the prompt-editor
    hooks (current_prompt / default_prompt / set_prompt) the debug page uses."""

    label = "claude"

    def __init__(self, guard, trust="private", model=None, claude_bin=None):
        self.guard = guard                  # shared autonomy gate (MCP child reads its mode via env)
        self.trust = trust if trust in VALID_TRUST else "private"
        self.model = model or (config.AGENT_MODEL or None)  # None → Claude Code's default
        self.bin = claude_bin or shutil.which("claude") or "claude"
        self.session_id = None              # claude's id, captured for --resume
        self.history = []                   # display history (chat-server reset() parity)
        # editable persona override (debug page): when set it replaces the
        # private.md persona for this turn, persisted so it survives a restart.
        self.prompt_override = None
        try:
            with open(config.PROMPT_PATH, encoding="utf-8") as f:
                self.prompt_override = f.read() or None
        except OSError:
            pass
        write_mcp_config()

    # -- conversation lifecycle ------------------------------------------------
    def reset(self):
        self.session_id = None
        self.history = []

    def export_state(self):
        return {"history": list(self.history), "session_id": self.session_id}

    def import_state(self, state):
        state = state or {}
        self.history = list(state.get("history") or [])
        self.session_id = state.get("session_id")

    # -- the editable persona (debug page /api/prompt) -------------------------
    def default_prompt(self):
        return _read_prompt(self.trust)

    def current_prompt(self):
        return self.prompt_override or self.default_prompt()

    def set_prompt(self, text):
        """Override the persona for the active trust (or clear with empty text).
        Persisted to config.PROMPT_PATH so an edit survives a daemon restart."""
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

    # -- a turn ----------------------------------------------------------------
    def chat(self, user_text):
        """One user turn → {reply, trace}. Drives `claude -p` with the fleet MCP
        tools; multi-turn continuity rides on --resume. Mutates self.history."""
        self.history.append({"role": "user", "content": user_text})
        cmd = [self.bin, "-p", user_text,
               "--mcp-config", MCP_CONFIG,
               "--allowedTools", ALLOWED_TOOLS,
               "--output-format", "json"]
        sys_prompt = self.current_prompt()
        if sys_prompt:
            cmd += ["--append-system-prompt", sys_prompt]
        if self.model:
            cmd += ["--model", self.model]
        if self.session_id:
            cmd += ["--resume", self.session_id]
        try:
            proc = subprocess.run(cmd, cwd=ROOT, env=scrubbed_env(self.guard.autonomy),
                                  capture_output=True, text=True, timeout=240)
        except subprocess.TimeoutExpired:
            return self._finish("⚠️ Claude timed out (240s).", [])
        except FileNotFoundError:
            return self._finish(f"⚠️ `{self.bin}` not found — is the Claude CLI installed?", [])
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()[-600:]
            return self._finish(f"⚠️ Claude exited {proc.returncode}: {err}", [])
        out = proc.stdout.strip()
        try:
            data = json.loads(out)
        except Exception:
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
