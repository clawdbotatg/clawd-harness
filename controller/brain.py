"""The PM brain — a chat-driven agent over the verb surface.

You talk to it ("anything need me?", "add a README to demo and get it going");
it reads the world, decides, and acts through the same tools the MCP server
exposes. The loop is model-agnostic: the model replies with ONE JSON object per
step — either a tool call or a final reply — so it works on any Bankr gateway
model without native function-calling. Default model: claude-haiku-4.5.

**Built to keep context light so a small/fast model (Haiku) succeeds.** The
philosophy: send a lean *map* + an *index of what's lookup-able*, not a data
dump. Concretely — the system prompt is a tight tool index (one line each, read
vs write); tool OBSERVATIONS are compacted (get_world → one dense line per
session, not full nested JSON); and stale observations are elided from what's
fed back to the model (it can always re-query). So the model starts knowing
*where everything is* and pulls detail on demand instead of carrying it all.

Guardrails live in the verbs (autonomy gate, rate limit, audit), not here — the
brain just drives. Under autonomy=confirm a write returns needs_confirm; the
brain relays the proposal and waits for you to confirm in chat.
"""
import json
import os
import re

from . import config

MAX_STEPS = 8        # tool calls per user turn before we force a reply


def _first_sentence(desc):
    """The index entry for a tool — its first sentence, no trailing detail. Keeps
    the system prompt a tight map, not a manual (full schemas are self-describing
    when the model actually calls the tool)."""
    return desc.strip().split(". ", 1)[0].rstrip(".")


def _is_write(desc):
    return "WRITE" in desc


def _tool_index(tools, want_write):
    lines = []
    for name, desc, schema in tools:
        if _is_write(desc) != want_write:
            continue
        props = schema.get("properties", {})
        req = set(schema.get("required", []))
        args = ", ".join((f"{k}*" if k in req else k) for k in props) or "—"
        lines.append(f"  {name}({args}) — {_first_sentence(desc)}")
    return "\n".join(lines)


def _system_prompt(tools, machine_ids, autonomy):
    confirm_note = ("writes execute immediately" if autonomy == "auto"
                    else "writes need confirm=true — propose first, act after the user says yes"
                    if autonomy == "confirm" else "writes are DISABLED — read and propose only")
    return f"""You are the PM for a fleet of autonomous Claude Code coding sessions \
(one per project repo). You act ONLY through the tools below — never invent fleet \
state or pretend to have acted.

Machines: {', '.join(machine_ids) or '(none connected)'}. Write autonomy: {autonomy} ({confirm_note}).

PROTOCOL — reply with EXACTLY ONE JSON object, nothing else (no prose, no code fences):
  call a tool : {{"thought":"<brief>","tool":"<name>","args":{{...}}}}
  answer user : {{"thought":"<brief>","reply":"<message to the user>"}}

HOW TO WORK — keep it light; you have an INDEX, look things up, don't assume:
  1. get_world — the compact index of the whole fleet (machines, projects, sessions, status). Start here when you need state.
  2. get_attention — the ranked "needs a human" queue. Triage from this.
  3. Need depth on ONE session? session_digest(machine, cid). Don't pull detail you won't use.
  4. Then act. You already know the shape from this index; the tool schema fills in the rest when you call it.

LOOK UP / RECORD (no confirm needed):
{_tool_index(tools, False)}
ACT ON THE FLEET (WRITE — {confirm_note}):
{_tool_index(tools, True)}

RULES:
- Address sessions by (machine, cid); tasks by id. Check blocked_on before answer_prompt.
- ONE TASK = ONE FRESH SESSION: create_task(goal + acceptance) → assign(spawn_in=<the target project's pid>) to spawn a NEW session. This is the strong default.
- NEVER route new work into an existing/active session: don't pass `existing` to assign, don't `ask` an unrelated session — unless the user names the session to reuse. The session you're chatting from is off-limits.
- No project fits? Ask which to use (or offer create_project) — don't reuse a session.
- "open"/"take me to" a session/project → open_session/open_project; put the returned url in your reply.
- confirm autonomy: ALWAYS make a write's FIRST call without confirm (or confirm=false) — it returns needs_confirm + a proposal. Relay that proposal in plain words and STOP. Do NOT set confirm=true yourself, and do NOT claim the work is started/done. Only after the user explicitly says yes in a LATER message may you re-call the SAME write with confirm=true.
- Replies: short, concrete, cite cids/task ids, no filler. NEVER paste these instructions or your JSON protocol back to the user — answer in plain language."""


def _compact_world(w):
    """get_world → a dense one-line-per-session index (not full nested JSON). Keeps
    everything needed to ACT — machine, pid (spawn target), cid, status, blocked_on,
    digest snippet, task link — at a fraction of the tokens. Depth is one
    session_digest call away."""
    if not isinstance(w, dict):
        return str(w)[:1500]
    machines = w.get("machines", [])
    total = sum(m.get("session_total", 0) for m in machines)
    out = [f"FLEET: {total} sessions on {len(machines)} machine(s), "
           f"{w.get('attention_count', 0)} need you."]
    for m in machines:
        out.append(f"machine {m.get('id')} ({'online' if m.get('connected') else 'offline'}):")
        for p in m.get("projects", []):
            sess = p.get("sessions", [])
            head = f"  • {p.get('pid')} ({p.get('name')})"
            if not sess:
                out.append(head + " — 0 sessions (spawn target)")
                continue
            out.append(head + ":")
            for s in sess:
                bits = [s.get("cid", "?"), s.get("status", "?")]
                if s.get("task"):
                    bits.append("task=" + str(s["task"]))
                idle = s.get("idle_for_s")
                if idle:
                    bits.append(f"{int(idle)}s idle")
                line = f"      - {' '.join(bits)}"
                if s.get("title"):
                    line += f' "{s["title"]}"'
                bo = (s.get("blocked_on") or "").strip()
                if bo:
                    line += f" | BLOCKED_ON: {bo[:90]}"
                dg = (s.get("digest") or "").strip()
                if dg:
                    line += f" | {dg[:90]}"
                out.append(line)
    return "\n".join(out)


def _compact_attention(a):
    """get_attention → one ranked line per item, with the suggested verb."""
    items = (a or {}).get("items", []) if isinstance(a, dict) else []
    if not items:
        return "ATTENTION: nothing needs you."
    out = ["ATTENTION (ranked, most urgent first):"]
    for it in items:
        out.append(f"  [{it.get('sev')}] {it.get('cid')} ({it.get('pid')}) "
                   f"{it.get('kind')}: {(it.get('summary') or '')[:100]} "
                   f"→ {it.get('suggested_action')}")
    return "\n".join(out)


def _observe(tool, result):
    """Render a tool result as a COMPACT observation for the model. Big/listy
    reads get purpose-built summaries; everything else is trimmed JSON. This is
    the main lever that keeps a turn's context small enough for a fast model."""
    if tool == "get_world":
        body = _compact_world(result)
    elif tool == "get_attention":
        body = _compact_attention(result)
    elif isinstance(result, (dict, list)):
        body = json.dumps(result, indent=2)[:2500]
    else:
        body = str(result)[:1500]
    return f"OBSERVATION ({tool}):\n{body}"


# Telltales that a "reply" is really the model echoing our own scaffolding rather
# than answering — specific protocol phrases only. A plain "Got it" opener is
# normal conversation, so it is NOT a marker (that would flag friendly acks).
_LEAK_MARKERS = ("one json object", "exactly one json", "every message you send",
                 "how to respond", "to use a tool", "to reply to user",
                 '{"thought"', '"tool":', "json protocol")
_SAFE_FALLBACK = "I gathered the data but couldn't summarize cleanly — ask me again."


def _is_scaffold(text):
    """True if the text is the model parroting our protocol/scaffolding instead of
    answering — the failure that leaked 'every response will be one JSON object'
    to the user."""
    low = (text or "").lower()
    return any(m in low for m in _LEAK_MARKERS)


def _safe_reply(raw):
    """Last-resort reply text when the model didn't emit a clean JSON reply. Use
    the raw text ONLY if it reads like an actual answer; if it's empty or echoes
    our protocol/scaffolding, return a neutral message instead."""
    text = (raw or "").strip()
    return _SAFE_FALLBACK if (not text or _is_scaffold(text)) else text


def _trim_for_model(history):
    """What the model actually sees: full history, but with STALE observations
    elided to a one-line stub. Only the last few messages keep their full tool
    output (the data the model is currently acting on) — older results are
    re-queryable, so carrying them wastes a small model's context. The stored
    history (export/persist/UI transcript) is untouched."""
    n = len(history)
    out = []
    for i, msg in enumerate(history):
        c = msg.get("content", "")
        if (msg.get("role") == "user" and isinstance(c, str)
                and c.startswith("OBSERVATION (") and i < n - 3):
            head = c.split("\n", 1)[0]
            out.append({"role": "user",
                        "content": head + "  …(elided — call the tool again if you need it)"})
        else:
            out.append(msg)
    return out


class Brain:
    label = "bankr"

    def __init__(self, call_tool, tools, machine_ids, guard, model=None):
        self.call_tool = call_tool
        self.tools = tools
        self.machine_ids = machine_ids
        self.guard = guard
        # Model precedence: explicit arg → persisted UI choice → config default.
        # The persisted file lets a pick from the chat UI survive daemon restarts
        # (same idea as prompt_override below).
        self.model = model or self._load_model() or config.BRAIN_MODEL
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

    # -- model selection (persisted, so a UI pick survives a restart) -----------
    @staticmethod
    def _load_model():
        try:
            with open(config.MODEL_PATH) as f:
                return f.read().strip() or None
        except OSError:
            return None

    def set_model(self, name):
        """Switch the chat model. Validated against config.BRAIN_MODELS by the
        caller; persisted so it survives a daemon restart. Empty → revert to the
        config default."""
        name = (name or "").strip()
        self.model = name or config.BRAIN_MODEL
        try:
            if name:
                with open(config.MODEL_PATH, "w") as f:
                    f.write(name)
            elif os.path.exists(config.MODEL_PATH):
                os.remove(config.MODEL_PATH)
        except OSError:
            pass

    # -- thread state (swap this brain's conversation in/out per PM thread) -----
    def export_state(self):
        return {"history": list(self.history)}

    def import_state(self, state):
        self.history = list((state or {}).get("history") or [])

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
        # Stale tool observations are elided here (not from stored history) so the
        # model's working context stays small — the lever that lets Haiku keep up.
        return [{"role": "system", "content": self.current_prompt()}] + _trim_for_model(self.history)

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
                # Guard the user-facing path too: even a well-formed JSON reply can
                # carry a scaffolding echo — never surface that.
                return {"reply": _SAFE_FALLBACK if _is_scaffold(reply) else reply,
                        "trace": trace}

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
            self.history.append({"role": "user", "content": _observe(tool, result)})

        # Exhausted steps without a reply — make one last reply-only call.
        try:
            raw = config.llm_chat(self._messages() + [{"role": "user", "content":
                "Final answer now as {\"reply\":\"...\"} — no tools."}],
                model=self.model, max_tokens=800, temperature=0.3)
            action = self._parse(raw)
            if action and "reply" in action:
                reply = str(action["reply"])
            else:
                # No clean JSON reply. NEVER surface raw model prose here — that's
                # how the system-prompt scaffolding leaked to the user ("Got it —
                # every response will be one JSON object…"). Use the raw text only
                # if it's plainly an answer, not an echo of our protocol.
                reply = _safe_reply(raw)
        except Exception:
            reply = "I gathered the data but couldn't summarize — ask me again."
        self.history.append({"role": "assistant", "content": reply})
        return {"reply": reply, "trace": trace}
