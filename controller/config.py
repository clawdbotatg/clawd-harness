"""Controller configuration (stdlib only).

Reuses the harness's `.clawd-harness.env` (the same file server.py loads) so
there's one source of truth — the controller never hardcodes a secret.

The PM brain is `claude -p` on your subscription (see agent.py), so there's no LLM
gateway key to configure here anymore: the old Bankr-gateway brain + its model
knobs were removed when the PM became a minimal claude-p-agent. The only model
knob left is the optional CONTROLLER_MODEL, which picks the `claude --model` the
PM runs as (empty → Claude Code's default).
"""
import os

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


# Creds/config come from .clawd-harness.env (laptop) or .env.controller (the box
# deploy, which has no harness env), then the real environment wins over both.
_ENV = {**_load_env_file(os.path.join(ROOT, ".clawd-harness.env")),
        **_load_env_file(os.path.join(ROOT, ".env.controller")),
        **os.environ}


def cfg(key, default=""):
    return _ENV.get(key, default)


# -- controller knobs ----------------------------------------------------------
# The `claude --model` the PM runs as. Empty → Claude Code's own default (the
# right call: the PM is real Claude on your subscription, not a model menu). Set
# CONTROLLER_MODEL to pin a specific one (e.g. claude-sonnet-4.6).
AGENT_MODEL = cfg("CONTROLLER_MODEL", "")
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
# Persisted model override (picked on the debug page's Config tab). Absent →
# CONTROLLER_MODEL, which absent → Claude Code's own default.
MODEL_PATH = cfg("CONTROLLER_MODEL_PATH", os.path.join(ROOT, ".clawd-controller.model.txt"))
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


def _ws_to_http(base):
    if base.startswith("wss://"):
        return "https://" + base[len("wss://"):].rstrip("/")
    if base.startswith("ws://"):
        return "http://" + base[len("ws://"):].rstrip("/")
    return base.rstrip("/")


def fleet_mode():
    """True in box mode: the controller drives the whole fleet through the relay,
    so harness UI deep links must be machine-prefixed (`#/m/<machine>/…`) and
    served from the relay's PUBLIC origin — not the box-internal WS endpoint."""
    return bool(RELAY_URL)


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
    return _ws_to_http(RELAY_URL or HARNESS_WS or "ws://127.0.0.1:8787")


def public_ui_base():
    """The PUBLIC origin the harness UI is actually reached at — for the absolute
    url baked into deep links (Telegram/non-browser clients can't rebuild against a
    browser origin). In fleet/box mode the controller dials the relay over a
    box-internal `ws://127.0.0.1:8788`, but users reach the UI at the relay's public
    host, so prefer an explicit CONTROLLER_HARNESS_HTTP, then the fleet relay host
    (FLEET_RELAY, wss→https), before falling back to the WS-derived base."""
    explicit = cfg("CONTROLLER_HARNESS_HTTP")
    if explicit:
        return explicit.rstrip("/")
    if RELAY_URL:
        fr = cfg("FLEET_RELAY")
        if fr:
            return _ws_to_http(fr)
    return harness_http_base()
