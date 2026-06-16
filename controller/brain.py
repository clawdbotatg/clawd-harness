"""The PM brain — a chat-driven agent over the verb surface.

You talk to it ("anything need me?", "add a README to demo and get it going");
it reads the world, decides, and acts through the same tools the MCP server
exposes. The loop is model-agnostic: the model replies with ONE JSON object per
step — either a tool call or a final reply — so it works on any Bankr gateway
model without native function-calling. Default model: kimi-k2.6.

Guardrails live in the verbs (autonomy gate, rate limit, audit), not here — the
brain just drives. Under autonomy=confirm a write returns needs_confirm; the
brain relays the proposal and waits for you to confirm in chat.
"""
import json
import os
import re

from . import config

MAX_STEPS = 8        # tool calls per user turn before we force a reply


def _tool_catalog(tools):
    lines = []
    for name, desc, schema in tools:
        props = schema.get("properties", {})
        req = set(schema.get("required", []))
        args = ", ".join(
            (f"{k}*" if k in req else k) for k in props) or "—"
        lines.append(f"- {name}({args}) — {desc}")
    return "\n".join(lines)


def _system_prompt(tools, machine_ids, autonomy):
    return f"""You are the project manager for a fleet of autonomous coding sessions \
(each an interactive Claude Code running in a project repo). You see every session \
and can start work, send messages, and unblock them — but you act THROUGH TOOLS, \
never by pretending.

Machines you can drive: {', '.join(machine_ids) or '(none connected)'}.
Write autonomy is currently: {autonomy} \
({'writes execute immediately' if autonomy == 'auto' else 'writes need confirm=true — propose first, act after the user agrees' if autonomy == 'confirm' else 'writes are disabled — you can only read and propose'}).

HOW TO RESPOND — every message you send is exactly ONE JSON object, nothing else:
  to use a tool:   {{"thought": "<brief>", "tool": "<name>", "args": {{...}}}}
  to reply to user:{{"thought": "<brief>", "reply": "<your message>"}}

Tools:
{_tool_catalog(tools)}

Operating rules:
- Triage with get_attention / get_world before acting. Don't guess fleet state.
- Address sessions by (machine, cid); tasks by id. Read blocked_on before answer_prompt.
- ONE TASK = ONE FRESH SESSION. For any actionable piece of work: create_task \
(goal + acceptance), then assign with spawn_in=<the target project's pid> to spawn \
a NEW session. This is the default and the strong preference — every task gets its \
own session so it's tracked cleanly and in isolation.
- NEVER route task work into an existing or currently-active session: do not pass \
`existing` to assign, and do not `ask` an unrelated session to do new work — unless \
the user EXPLICITLY names the session to reuse. The session you're being chatted \
from is off-limits as a work target.
- If no project fits the task, say so and ask which project to use (or offer to \
create_project) rather than reusing a session.
- Under confirm autonomy, a write returns needs_confirm with a proposal — relay it \
plainly and stop; only re-call with confirm=true after the user says yes.
- Keep replies short and concrete. Cite cids/task ids. No filler."""


class Brain:
    label = "bankr"

    def __init__(self, call_tool, tools, machine_ids, guard, model=None):
        self.call_tool = call_tool
        self.tools = tools
        self.machine_ids = machine_ids
        self.guard = guard
        self.model = model or config.BRAIN_MODEL
        self.history = []        # [{role, content}] user/assistant turns (no system)
        # a tweakable system-prompt override (set from the debug page), persisted
        # to disk so edits survive daemon restarts. None → use the built-in prompt.
        self.prompt_override = None
        try:
            with open(config.PROMPT_PATH) as f:
                self.prompt_override = f.read() or None
        except OSError:
            pass

    def reset(self):
        self.history = []

    def default_prompt(self):
        return _system_prompt(self.tools, self.machine_ids, self.guard.autonomy)

    def current_prompt(self):
        return self.prompt_override or self.default_prompt()

    def set_prompt(self, text):
        """Override the system prompt (or clear it with empty text). Persisted."""
        text = (text or "").strip()
        self.prompt_override = text or None
        try:
            if text:
                with open(config.PROMPT_PATH, "w") as f:
                    f.write(text)
            elif os.path.exists(config.PROMPT_PATH):
                os.remove(config.PROMPT_PATH)
        except OSError:
            pass

    def _messages(self):
        return [{"role": "system", "content": self.current_prompt()}] + self.history

    @staticmethod
    def _parse(text):
        """Pull the first JSON object out of the model's reply (tolerant of stray
        prose / code fences)."""
        text = text.strip()
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except Exception:
            return None

    def chat(self, user_text):
        """One user turn → {reply, trace}. trace lists the tool calls made (for the
        UI to show what the bot did). Mutates self.history."""
        self.history.append({"role": "user", "content": user_text})
        trace = []
        seen = set()                 # tool-call signatures already run this turn
        for step in range(MAX_STEPS):
            # On the last step, forbid tools so we always end with an answer.
            force_reply = step == MAX_STEPS - 1
            msgs = self._messages()
            if force_reply:
                msgs = msgs + [{"role": "user", "content":
                    "Enough tool calls — reply NOW using what you have: "
                    "{\"reply\":\"...\"}. Do not call any tool."}]
            try:
                raw = config.llm_chat(msgs, model=self.model, max_tokens=1200, temperature=0.3)
            except Exception as e:
                reply = f"⚠️ LLM error: {e}"
                self.history.append({"role": "assistant", "content": reply})
                return {"reply": reply, "trace": trace}

            action = self._parse(raw)
            if action is None:
                self.history.append({"role": "assistant", "content": raw})
                self.history.append({"role": "user", "content":
                    "Reply with ONE JSON object only: {\"thought\":..., \"tool\":..., "
                    "\"args\":{...}} or {\"thought\":..., \"reply\":...}."})
                continue

            if "reply" in action:
                reply = str(action["reply"])
                self.history.append({"role": "assistant", "content": json.dumps(action)})
                return {"reply": reply, "trace": trace}

            tool = action.get("tool")
            args = action.get("args") or {}
            self.history.append({"role": "assistant", "content": json.dumps(action)})
            # Don't re-run an identical call — that's the loop that never converges.
            sig = tool + json.dumps(args, sort_keys=True)
            if sig in seen:
                self.history.append({"role": "user", "content":
                    f"You already called {tool} with those args — its result is above. "
                    "Do NOT call it again; answer the user now with {\"reply\":\"...\"}."})
                continue
            seen.add(sig)
            try:
                result = self.call_tool(tool, args)
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            trace.append({"tool": tool, "args": args, "result": result})
            self.history.append({"role": "user", "content":
                f"OBSERVATION ({tool}):\n{json.dumps(result, indent=2)[:16000]}"})

        # Exhausted steps without a reply — make one last reply-only call.
        try:
            raw = config.llm_chat(self._messages() + [{"role": "user", "content":
                "Final answer now as {\"reply\":\"...\"} — no tools."}],
                model=self.model, max_tokens=800, temperature=0.3)
            action = self._parse(raw)
            reply = str(action["reply"]) if action and "reply" in action else (
                raw.strip() or "I gathered the data but couldn't summarize — ask me again.")
        except Exception:
            reply = "I gathered the data but couldn't summarize — ask me again."
        self.history.append({"role": "assistant", "content": reply})
        return {"reply": reply, "trace": trace}
