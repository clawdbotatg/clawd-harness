"""Voice front-end for the PM — OpenAI gpt-realtime over WebRTC.

The browser talks to the PM by voice: native speech-to-speech (no STT→LLM→TTS
pipeline), semantic VAD turn-taking, barge-in. The recipe is the one verified in
github.com/clawdbotatg/gpt-voice (INTEGRATION.md there); this module is the
server half:

- `mint(verbs)` — POST /api/voice/token calls OpenAI's client_secrets endpoint
  with the REAL key (which never reaches the browser) and the full session
  config: persona, semantic VAD, transcription, and the tool definitions below.
  The ephemeral secret in the response is what the browser uses for WebRTC.
- The persona is built fresh per mint: voice-tuned instructions + a compact live
  fleet snapshot (so it can answer "what's going on?" with zero tool calls) +
  the clawd-md knowledge-base index.
- Tools are EXECUTED CLIENT-SIDE against the same /pm endpoints the typed PM tab
  uses (`/api/tool`, `/api/chat`, `/api/voice/lore`), so the voice layer can see
  and do exactly what the chat can — including handing hard/write work to the
  real PM brain via `ask_pm`. The `exec` map in the mint response tells the
  browser which endpoint each tool name maps to, so this file stays the single
  source of truth for the tool surface.

Config (all optional except the key): OPENAI_API_KEY enables the feature;
CONTROLLER_VOICE_MODEL (gpt-realtime | gpt-realtime-mini | pinned snapshot),
CONTROLLER_VOICE (marin/cedar/alloy/echo/sage/shimmer/verse — realtime voices
only), CONTROLLER_VOICE_EAGERNESS, CONTROLLER_VOICE_SPEED, CONTROLLER_LORE_DIR
(a checkout of clawdbotatg/clawd-md; missing dir degrades to "no lore").
"""
import json
import os
import urllib.request

from . import config

API_KEY = config.cfg("OPENAI_API_KEY", "")
MODEL = config.cfg("CONTROLLER_VOICE_MODEL", "gpt-realtime")
VOICE = config.cfg("CONTROLLER_VOICE", "marin")
EAGERNESS = config.cfg("CONTROLLER_VOICE_EAGERNESS", "auto")
SPEED = float(config.cfg("CONTROLLER_VOICE_SPEED", "1.0") or 1.0)
LORE_DIR = os.path.expanduser(config.cfg("CONTROLLER_LORE_DIR", "~/clawd-md"))

# How much of a lore file / embedded context we ship. The realtime model's
# context is small and billed in audio+text tokens — bound everything.
LORE_MAX = 24_000
SNAPSHOT_MAX = 4_000
IDENTITY_MAX = 1_600

# -- the voice tool surface ----------------------------------------------------
# (name, description, JSON-schema properties, required, exec-spec). The exec
# spec is NOT sent to OpenAI — it rides beside the token so the browser knows
# how to run each call: kind "verb" → POST /pm/api/tool {name, args};
# "chat" → POST /pm/api/chat {message: args.question}; "lore" →
# GET /pm/api/voice/lore?name=….
_TOOLS = [
    ("whats_waiting",
     "What needs the operator right now, fleet-wide: the ranked attention "
     "queue (blocked sessions with the actual open question), idle sessions, "
     "stuck tasks. Call this for 'what's up / anything need me?'.",
     {}, [], {"kind": "verb", "name": "sweep"}),
    ("fleet_overview",
     "Compact live map of the whole fleet: machines, projects, sessions with "
     "status and one-line digests. Optionally scope to one machine.",
     {"machine": {"type": "string", "description": "machine id to scope to"}},
     [], {"kind": "verb", "name": "get_world"}),
    ("find_it",
     "Fleet-wide search in one call: find the session/task/project about X "
     "(titles, digests, task ledger, transcripts).",
     {"query": {"type": "string"}}, ["query"],
     {"kind": "verb", "name": "find"}),
    ("check_pins",
     "The 📌 pin board: work that is coded but waiting on a human to verify, "
     "each with a one-line test instruction.",
     {}, [], {"kind": "verb", "name": "get_pins"}),
    ("account_usage",
     "Claude subscription health per machine: usage windows, which plan new "
     "work lands on.",
     {}, [], {"kind": "verb", "name": "get_accounts"}),
    ("read_lore",
     "Read one page of the clawd-md knowledge base (identity, lore, projects, "
     "infrastructure, history). name='index' lists the pages.",
     {"name": {"type": "string", "description": "page name, e.g. 'soul' or 'lore'"}},
     ["name"], {"kind": "lore"}),
    ("ask_pm",
     "Hand a question or an ORDER to the real PM brain (Claude with full fleet "
     "tools). It can act: spawn sessions, assign work, answer blocked prompts, "
     "run pipelines. SLOW — one to three minutes. Tell the operator you're "
     "handing it off and keep chatting; speak the result when it returns. Use "
     "this for anything beyond the read tools above, and for all writes.",
     {"question": {"type": "string", "description": "the full request, self-contained"}},
     ["question"], {"kind": "chat"}),
]


def tool_defs():
    return [{"type": "function", "name": n, "description": d,
             "parameters": {"type": "object", "properties": p, "required": r}}
            for n, d, p, r, _spec in _TOOLS]


def exec_map():
    return {n: spec for n, _d, _p, _r, spec in _TOOLS}


# -- lore (the clawd-md knowledge base) ---------------------------------------
def lore_index():
    try:
        return sorted(f[:-3] for f in os.listdir(LORE_DIR)
                      if f.endswith(".md") and not f.startswith("."))
    except OSError:
        return []


def read_lore(name):
    """One knowledge-base page, bounded. Basename-only (no traversal), .md only."""
    name = os.path.basename((name or "").strip())
    if name.endswith(".md"):
        name = name[:-3]
    if not name or name == "index":
        return {"pages": lore_index()}
    if name not in lore_index():
        return {"error": f"no such page: {name}", "pages": lore_index()}
    try:
        with open(os.path.join(LORE_DIR, name + ".md"), encoding="utf-8",
                  errors="replace") as f:
            text = f.read(LORE_MAX + 1)
    except OSError as e:
        return {"error": f"unreadable: {e}"}
    if len(text) > LORE_MAX:
        text = text[:LORE_MAX] + "\n…(truncated)"
    return {"page": name, "text": text}


def _identity_brief():
    """A short who-am-I pulled from the knowledge base (soul.md → clawd.md →
    README.md, first present wins), so the voice knows whose fleet this is even
    before any tool call. Missing checkout → generic line."""
    for cand in ("soul", "clawd", "README"):
        got = read_lore(cand)
        if got.get("text"):
            return got["text"][:IDENTITY_MAX]
    return ("Clawd is an autonomous Claude agent running Austin's machines; the "
            "clawd-md knowledge base is not checked out on this box, so use the "
            "fleet tools for context.")


def _fleet_snapshot(verbs):
    try:
        snap = json.dumps(verbs.get_world())
    except Exception as e:
        return f"(fleet snapshot unavailable: {e})"
    if len(snap) > SNAPSHOT_MAX:
        snap = snap[:SNAPSHOT_MAX] + "…(truncated — call fleet_overview for the full map)"
    return snap


def instructions(verbs):
    pages = lore_index()
    return f"""You are the VOICE of Clawd's fleet PM — the project manager for a fleet of
Claude Code sessions running across Austin's machines (the clawd-harness).
You are talking to Austin out loud.

Voice style: SHORT, conversational, warm, a little wry. One or two sentences
unless asked to go deep. Never read raw JSON, ids, or URLs aloud — summarize in
plain words ("three sessions need you, the hot one is the twitter bot asking
about auth"). It's fine to trail off naturally and to be interrupted.

What you are on top of:
- The live fleet (snapshot below; tools give you fresh detail on demand).
- The pin board (coded-but-unverified work) and subscription/account health.
- The clawd-md knowledge base ({', '.join(pages) if pages else 'not on this box'})
  via read_lore — identity, history, projects, infrastructure.

Doing real work: you personally only READ. For anything that changes the world —
start work, answer a blocked session, spawn/assign, or any hard multi-step
question — use ask_pm to hand it to the PM brain (real Claude with fleet tools).
It takes a minute or two: say you're handing it off, keep the conversation
going, and speak the result when it lands. Don't invent fleet facts; if a tool
fails, say so plainly.

Identity backdrop (from the knowledge base):
{_identity_brief()}

Live fleet snapshot (as of this session's start — may age; tools are fresh):
{_fleet_snapshot(verbs)}"""


# -- the mint ------------------------------------------------------------------
def enabled():
    return bool(API_KEY)


def session_config(verbs):
    return {"session": {
        "type": "realtime",
        "model": MODEL,
        "instructions": instructions(verbs),
        "audio": {
            "input": {
                "turn_detection": {"type": "semantic_vad", "eagerness": EAGERNESS},
                "transcription": {"model": "gpt-4o-mini-transcribe"},
            },
            "output": {"voice": VOICE, "speed": SPEED},
        },
        "tools": tool_defs(),
    }}


def mint(verbs):
    """Mint an ephemeral realtime secret. Returns OpenAI's response (secret in
    top-level `value` — the GA shape) plus our client-side `exec` map."""
    if not enabled():
        return {"error": "voice not configured (OPENAI_API_KEY unset on the controller)"}
    req = urllib.request.Request(
        "https://api.openai.com/v1/realtime/client_secrets",
        data=json.dumps(session_config(verbs)).encode(),
        headers={"Authorization": f"Bearer {API_KEY}",
                 "Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        out = json.loads(r.read().decode())
    out["exec"] = exec_map()
    out["model"] = MODEL
    out["voice"] = VOICE
    return out
