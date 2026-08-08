"""AI thread naming — the PM-chat analog of the harness's session naming.

Sessions get an LLM title + one-line tldr that keeps refreshing as work
progresses (server.py: NAME_SYS_PROMPT / name_at_prompt, the Bankr gateway).
PM threads get the same treatment here: at user-prompt 1, then every 3
(3, 6, 9, …), one cheap gateway call turns the thread transcript into
{"title", "desc"} — the title lands on the thread tab, the desc is the
running "what are we doing" tldr line above the composer.

Reads the SAME BANKR_* creds server.py uses, via config.cfg (so the laptop's
.clawd-harness.env just works; a box deploy adds them to .env.controller).
Unconfigured → configured() is False and callers skip naming entirely —
threads keep their first-prompt titles, exactly as before.
"""
import json
import re
import urllib.request

from . import config

BANKR_API_KEY = config.cfg("BANKR_API_KEY", "")
BANKR_BASE_URL = config.cfg("BANKR_BASE_URL", "").rstrip("/")
BANKR_MODEL = config.cfg("BANKR_MODEL", "claude-haiku-4-5-20251001")
BANKR_API = config.cfg("BANKR_API", "openai").lower()   # openai | anthropic | bankr

# Mirrors server.py's cadence: prompt 1 (name it immediately), then every 3 so a
# long thread's title/tldr keep sharpening. Cheap + async, so steady is fine.
def name_at_prompt(count):
    return count <= 1 or count % 3 == 0


THREAD_NAME_SYS_PROMPT = (
    "You name conversations between an operator and their fleet PM (a project "
    "manager over coding sessions). Given the transcript, reply with ONLY "
    'compact JSON and nothing else: {"title": "<max 5 words>", '
    '"desc": "<max 12 words: what we are doing right now>"}. '
    "The title names the thread by its MAIN topic — the overarching goal, "
    "usually set in the opening messages; do not let a passing side-question "
    "redefine it. The desc is a live tldr of where the work stands NOW."
)

_TAIL_MSGS = 30          # transcript tail: recent enough to be current,
_TAIL_CHARS = 4000       # small enough to stay a ~1k-token labeler call


def configured():
    return bool(BANKR_API_KEY and BANKR_BASE_URL)


def transcript_tail(messages):
    """Flatten a thread's display messages into the compact 'you:/pm:' tail the
    namer reads — last _TAIL_MSGS turns, clipped to _TAIL_CHARS from the end
    (the end is where the current state lives)."""
    lines = []
    for m in (messages or [])[-_TAIL_MSGS:]:
        text = " ".join((m.get("text") or "").split())
        if not text:
            continue
        who = "you" if m.get("who") == "me" else "pm"
        lines.append(f"{who}: {text[:400]}")
    return "\n".join(lines)[-_TAIL_CHARS:]


def _llm_json(sys_prompt, user_text, max_tokens=120):
    """One (system, user) turn against the configured gateway → the parsed JSON
    object the model emitted, or None (unconfigured / call failed / no JSON).
    Same transport shape as server.py's _llm_json — keep the two in step."""
    if not configured():
        return None
    try:
        if BANKR_API == "anthropic":
            url = f"{BANKR_BASE_URL}/v1/messages"
            body = {"model": BANKR_MODEL, "max_tokens": max_tokens,
                    "system": sys_prompt,
                    "messages": [{"role": "user", "content": user_text}]}
            headers = {"x-api-key": BANKR_API_KEY,
                       "anthropic-version": "2023-06-01",
                       "content-type": "application/json"}
        else:  # openai-compatible (incl. bankr — same body, different auth header)
            url = f"{BANKR_BASE_URL}/chat/completions"
            body = {"model": BANKR_MODEL, "max_tokens": max_tokens,
                    "temperature": 0.3,
                    "messages": [{"role": "system", "content": sys_prompt},
                                 {"role": "user", "content": user_text}]}
            if BANKR_API == "bankr":
                headers = {"X-API-Key": BANKR_API_KEY,
                           "content-type": "application/json"}
            else:
                headers = {"Authorization": f"Bearer {BANKR_API_KEY}",
                           "content-type": "application/json"}
        req = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"),
            headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=12) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        if BANKR_API == "anthropic":
            content = payload.get("content") or []
            raw = "".join(b.get("text", "") for b in content
                          if isinstance(b, dict))
        else:
            raw = (((payload.get("choices") or [{}])[0]).get("message") or {}
                   ).get("content", "")
        m = re.search(r"\{[\s\S]*\}", raw or "")
        return json.loads(m.group(0)) if m else None
    except Exception as e:
        print(f"[pm-name] generation failed: {e}", flush=True)
        return None


def generate_thread_name(transcript_text):
    """→ (title, desc) for a PM thread, or (None, None) when naming is
    unconfigured or the call fails."""
    parsed = _llm_json(THREAD_NAME_SYS_PROMPT, transcript_text)
    if not isinstance(parsed, dict):
        return (None, None)
    title = (parsed.get("title") or "").strip() or None
    desc = (parsed.get("desc") or "").strip() or None
    return (title, desc)
