"""Controller configuration + the Bankr LLM client (stdlib HTTP).

Reuses the harness's gateway creds from `.clawd-harness.env` (the same file
server.py loads for AI naming) so there's one source of truth for the key — the
controller never hardcodes a secret. The brain model is separate from the naming
model: naming is a cheap labeler (qwen3-coder); the PM brain reasons + uses tools,
so it defaults to a stronger model (kimi-k2.6). Override via CONTROLLER_MODEL.
"""
import json
import os
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _load_env_file(path):
    """Parse a KEY=VALUE env file into a dict (no shell, stdlib only). Mirrors
    server.py's loader so the controller reads the same .clawd-harness.env."""
    out = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


# Creds/config come from .clawd-harness.env (laptop) or controller.env (the box
# deploy, which has no harness env), then the real environment wins over both.
_ENV = {**_load_env_file(os.path.join(ROOT, ".clawd-harness.env")),
        **_load_env_file(os.path.join(ROOT, "controller.env")),
        **os.environ}


def cfg(key, default=""):
    return _ENV.get(key, default)


# -- gateway creds (shared with the harness's naming) --------------------------
BANKR_API_KEY = cfg("BANKR_API_KEY")
BANKR_BASE_URL = cfg("BANKR_BASE_URL").rstrip("/")
BANKR_API = (cfg("BANKR_API", "openai")).lower()

# -- controller knobs ----------------------------------------------------------
# The PM brain's model. Default claude-haiku-4.5: in the bench_controller.py
# survey it was the best speed-for-reliability — 0 leaks, completed every run,
# ~6× faster than the old kimi-k2.6 default (3.9s vs ~25s p50 per turn). Naming
# stays qwen3-coder; this is a different job. Override via CONTROLLER_MODEL.
BRAIN_MODEL = cfg("CONTROLLER_MODEL", "claude-haiku-4.5")
# The selectable model menu surfaced in the chat UI — the leak-aware top of the
# bench_controller.py ranking (the leaky kimi-k2.7-code is deliberately left out;
# add it here if you want its speed). Re-derive by running `python3 bench_controller.py`.
BRAIN_MODELS = [m.strip() for m in cfg("CONTROLLER_MODELS",
    "claude-haiku-4.5,kimi-k2.6,deepseek-v3.2,qwen3.7-plus,claude-sonnet-4.6").split(",")
    if m.strip()]
# Persisted model choice (picked from the UI) so it survives a daemon restart —
# same pattern as PROMPT_PATH. Absent → BRAIN_MODEL.
MODEL_PATH = cfg("CONTROLLER_MODEL_FILE", os.path.join(ROOT, ".clawd-controller.model.txt"))
# Autonomy gate for write verbs: readonly (refuse) | confirm (dry-run unless
# confirm=true) | auto (execute). Default confirm — safe but useful out of the box.
AUTONOMY = cfg("CONTROLLER_AUTONOMY", "confirm").lower()
# How many write actions a single target (cid/machine) may trigger per minute.
RATE_PER_MIN = int(cfg("CONTROLLER_RATE_PER_MIN", "8") or 8)

# Which harness this controller drives. Single-machine by default (direct to the
# local harness); the relay/multi-machine adapter layers on top of the same World.
HARNESS_WS = cfg("CONTROLLER_HARNESS_WS", "ws://127.0.0.1:8787")
MACHINE_ID = cfg("CONTROLLER_MACHINE", "self")
# Box mode: instead of a local harness, drive the WHOLE fleet through the relay's
# trusted-control path. Set CONTROLLER_RELAY (ws://127.0.0.1:8788) + the shared
# CONTROLLER_RELAY_TOKEN (== the relay's FLEET_CONTROLLER_TOKEN). Takes precedence
# over HARNESS_WS when set. See controller/relay_client.py.
RELAY_URL = cfg("CONTROLLER_RELAY", "")
RELAY_TOKEN = cfg("CONTROLLER_RELAY_TOKEN", "")
CHAT_PORT = int(cfg("CONTROLLER_CHAT_PORT", "8799") or 8799)
LEDGER_PATH = cfg("CONTROLLER_LEDGER", os.path.join(ROOT, ".clawd-controller.tasks.jsonl"))
# Persisted system-prompt override (edited from the debug page). Absent → built-in.
PROMPT_PATH = cfg("CONTROLLER_PROMPT", os.path.join(ROOT, ".clawd-controller.prompt.txt"))
# Persisted PM chat threads (multiple concurrent conversations + their history),
# so they survive a daemon restart. Mirrors .clawd-harness.sessions.json.
THREADS_PATH = cfg("CONTROLLER_THREADS", os.path.join(ROOT, ".clawd-controller.threads.json"))

# Telegram front-end (optional). Set CONTROLLER_TELEGRAM_TOKEN to a bot token that
# is NOT already being polled elsewhere (Telegram allows one getUpdates consumer
# per token — pointing it at a live bot would 409 and disrupt that bot). Allowlist
# is a comma-separated list of Telegram user ids permitted to drive the PM (for a
# private chat the user id IS the chat id, so it's also the push target).
TELEGRAM_TOKEN = cfg("CONTROLLER_TELEGRAM_TOKEN", "")
TELEGRAM_ALLOW = [x.strip() for x in cfg("CONTROLLER_TELEGRAM_ALLOW", "672968601").split(",") if x.strip()]


def harness_token():
    """The harness WS token — env override, else the .clawd-harness.token file."""
    t = cfg("CONTROLLER_HARNESS_TOKEN") or cfg("CONSOLE_TOKEN")
    if t:
        return t
    try:
        with open(os.path.join(ROOT, ".clawd-harness.token")) as f:
            return f.read().strip()
    except OSError:
        return ""


def harness_http_base():
    """The harness UI's HTTP origin, derived from the WS URL the controller dials
    (ws→http, wss→https). Direct mode → the local harness; relay/box mode → the
    relay (which serves the same unified index.html). This is the base the PM uses
    to build deep links that 'send' the user from the chat into a specific
    session/project. Override with CONTROLLER_HARNESS_HTTP if the UI is reachable
    at a different origin than the WS endpoint."""
    explicit = cfg("CONTROLLER_HARNESS_HTTP")
    if explicit:
        return explicit.rstrip("/")
    base = RELAY_URL or HARNESS_WS or "ws://127.0.0.1:8787"
    if base.startswith("wss://"):
        return "https://" + base[len("wss://"):].rstrip("/")
    if base.startswith("ws://"):
        return "http://" + base[len("ws://"):].rstrip("/")
    return base.rstrip("/")


def llm_chat(messages, model=None, max_tokens=1024, temperature=0.4, timeout=60):
    """One chat-completion round against the Bankr gateway. `messages` is a list
    of {role, content}. Returns the assistant's text (str), or raises on failure
    — the brain loop catches and surfaces errors rather than silently degrading.
    Model-agnostic: we drive tools via a JSON action protocol (see brain.py), so
    no native function-calling support is required from the gateway."""
    if not (BANKR_API_KEY and BANKR_BASE_URL):
        raise RuntimeError("LLM gateway not configured (BANKR_API_KEY / BANKR_BASE_URL)")
    model = model or BRAIN_MODEL
    if BANKR_API == "anthropic":
        url = f"{BANKR_BASE_URL}/v1/messages"
        sys_msgs = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        body = {"model": model, "max_tokens": max_tokens,
                "messages": [m for m in messages if m["role"] != "system"]}
        if sys_msgs:
            body["system"] = sys_msgs
        headers = {"x-api-key": BANKR_API_KEY, "anthropic-version": "2023-06-01",
                   "content-type": "application/json"}
    else:
        url = f"{BANKR_BASE_URL}/chat/completions"
        body = {"model": model, "max_tokens": max_tokens,
                "temperature": temperature, "messages": messages}
        if BANKR_API == "bankr":
            headers = {"X-API-Key": BANKR_API_KEY, "content-type": "application/json"}
        else:
            headers = {"Authorization": f"Bearer {BANKR_API_KEY}",
                       "content-type": "application/json"}
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode())
    if BANKR_API == "anthropic":
        return "".join(b.get("text", "") for b in (payload.get("content") or [])
                       if isinstance(b, dict))
    return (((payload.get("choices") or [{}])[0]).get("message") or {}).get("content", "")
