#!/usr/bin/env python3
"""
clawd-harness — a web terminal mirror for INTERACTIVE (subscription-billed)
Claude Code sessions.

Why interactive (no -p): on 2026-06-15 `claude -p`/headless usage moves to a
separate metered Agent SDK credit pool. The interactive TUI keeps drawing on the
Claude subscription. So we run real `claude` (no -p) inside a pseudo-terminal and
mirror it to the browser.

We never parse the "weird text" the TUI emits. Two decoupled channels per session:
  • WRITE  -> keystrokes injected into the PTY (raw passthrough + a "send" helper)
  • READ   -> (a) raw PTY bytes streamed to xterm.js, which *renders* the ANSI
              faithfully (the live, token-level visual mirror), and
              (b) the session transcript JSONL (clean, structured, zero ANSI),
              tailed and forwarded so a controller can act on real events.

Multi-session: a SessionManager owns N ClaudeSessions, each its own PTY +
transcript + ring buffer. One websocket per browser, multiplexed — a client
subscribes to one session at a time (its bytes + transcript stream), while
menu-level metadata (titles, busy badges) fan out to every client. Sessions are
persisted to a registry and `--resume`d across a daemon restart.

Pure Python stdlib. Reuses the PTY recipe from clawd-tg-claude/pty_probe.py and
the hand-rolled RFC 6455 WebSocket framing from clawd-web-claude/server.py.

Run:
  python3 server.py
  PORT=8787 WORKDIR=/some/dir CLAUDE_BIN=claude python3 server.py
Then open http://127.0.0.1:8787
"""

import base64
import datetime
import fcntl
import glob
import hashlib
import json
import os
import pty
import hmac
import inspect
import re
import secrets
import select
import signal
import struct
import subprocess
import termios
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingMixIn, TCPServer


def _load_env_file():
    """Load KEY=VALUE lines from .clawd-harness.env (gitignored) into the env
    *before* the config block reads it. The launchd daemon doesn't inherit your
    shell env, so this is how secrets like BANKR_API_KEY reach both a manual run
    and the daemon. Real environment vars always win."""
    path = Path(__file__).resolve().parent / ".clawd-harness.env"
    try:
        text = path.read_text()
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, val)

_load_env_file()

# ── config ──────────────────────────────────────────────────────────────────
PORT       = int(os.environ.get("PORT", "8787"))
BIND       = os.environ.get("BIND", "127.0.0.1")  # localhost-only by default.
# Remote access is the fleet's job (worker dials the harness over localhost, then
# the relay/passkey/E2E stack gates it). Binding 0.0.0.0 exposes :PORT to anyone
# on the LAN, *below* that whole stack — only the token guards it. Opt in with
# BIND=0.0.0.0 (and accept that the token alone gates bypass-permissions claude).
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
WORKDIR    = os.path.abspath(os.environ.get("WORKDIR", os.getcwd()))
COLS       = int(os.environ.get("COLS", "120"))
ROWS       = int(os.environ.get("ROWS", "34"))
RING_MAX   = int(os.environ.get("RING_MAX", str(256 * 1024)))  # replay buffer cap
# A subscribe whose ring replay is this shallow gets the transcript rendered in
# as seed scrollback first (see _history_seed_bytes) — the ring goes shallow
# exactly when it can't carry history: a width-change fence (_apply_size) or a
# fresh boot. Deep rings skip the seed (real painted bytes beat a re-rendering).
# SEED_RING_MAX=0 disables seeding.
SEED_RING_MAX = int(os.environ.get("SEED_RING_MAX", "8192"))
SEED_CHARS    = int(os.environ.get("SEED_CHARS", "12000"))   # cap on rendered seed text
# Scrub transcript text before it's replayed as terminal bytes: any escape
# sequence or control char (except \t \n) in a prompt/answer would otherwise
# execute in the subscriber's terminal instead of displaying.
SEED_SCRUB_RE = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"    # OSC … BEL/ST
    r"|\x1b\[[0-9;?]*[ -/]*[@-~]"           # CSI
    r"|\x1b."                               # any other escape
    r"|[\x00-\x08\x0b-\x1f\x7f]"            # C0 controls except \t \n
)
# Scrub OSC 52 (clipboard-write) sequences from ring REPLAYS only. The ring
# stores every byte a session emits, so a copy made inside claude days ago is
# replayed verbatim on every subscribe — and the client's OSC 52 handler would
# re-execute it, silently overwriting the viewer's system clipboard with stale
# text just for opening the tty view. Live bytes stay untouched (selecting in
# claude's TUI still copies); only the catch-up snapshot is cleaned.
OSC52_SCRUB_RE = re.compile(rb"\x1b\]52;[^\x07\x1b]*(?:\x07|\x1b\\)")
# Settle gap between typing a message and pressing Enter. Claude's TUI treats a
# fast text+CR burst as a multi-line *paste* (CR becomes a newline, not submit);
# a pause lets the paste finalize so the CR registers as Enter. <0.6s fails here.
# Big/multi-line pastes need the full settle; short one-liners only need to clear
# the 0.6s cliff, so they submit ~2x faster (SEND_SETTLE_MIN).
SEND_SETTLE     = float(os.environ.get("SEND_SETTLE", "1.5"))
SEND_SETTLE_MIN = float(os.environ.get("SEND_SETTLE_MIN", "0.7"))

# AI session naming (title + one-line description). Optional — without a key we
# fall back to deriving a title from the first prompt. Defaults assume an
# OpenAI-compatible chat-completions gateway; set BANKR_API=anthropic for the
# /v1/messages shape instead.
BANKR_API_KEY  = os.environ.get("BANKR_API_KEY", "")
BANKR_BASE_URL = os.environ.get("BANKR_BASE_URL", "").rstrip("/")
BANKR_MODEL    = os.environ.get("BANKR_MODEL", "claude-haiku-4-5-20251001")
BANKR_API      = os.environ.get("BANKR_API", "openai").lower()   # openai | anthropic | bankr
# (bankr = OpenAI-compatible body at /v1/chat/completions but authed with an
#  X-API-Key header instead of Authorization: Bearer — see llm.bankr.bot)
# ElevenLabs text-to-speech. Optional — without a key the browser falls back to
# the native Web Speech voice. The key MUST stay server-side, so the browser
# POSTs prose to /tts and we proxy to ElevenLabs (Flash v2.5, ~200ms TTFB),
# piping the MP3 straight back. Voice ID defaults to "Brian" if unset.
ELEVENLABS_API_KEY  = os.environ.get("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "") or "nPczCjzI2devNBz1zQrb"

# The AI controller (PM brain) runs as a *separate* process (see controller/), but
# we reverse-proxy /pm/* to it so the whole UI lives on one origin — the browser
# never sees its port. Optional: if the controller isn't running, /pm/* 502s and
# the harness UI's PM panel shows it offline. This proxies HTTP only (no import).
CONTROLLER_PORT = int(os.environ.get("CONTROLLER_CHAT_PORT", "8799") or 8799)

# A fresh id per server process. Sent to every client on connect; when a client
# reconnects (e.g. after a daemon restart) and sees a *different* boot id, it
# hard-reloads — fresh state clears any stale "thinking" spinner left mid-turn.
BOOT_ID = uuid.uuid4().hex

# Re-name the session at prompt 1, then every 3 prompts (3, 6, 9, 12, …) so a
# long-running session's title/desc keep sharpening. Naming is cheap + async, so
# the steady cadence is worth it. The instant first-prompt naming lives in
# _on_prompt; this gate fires on Stop once the turn's transcript exists.
def name_at_prompt(count):
    return count <= 1 or count % 3 == 0
# The naming instruction — a module constant so bench_naming.py tests the exact
# same prompt the app uses (single source of truth; no drift).
NAME_SYS_PROMPT = ("You name software-engineering sessions. Given a transcript, "
                   "reply with ONLY compact JSON and nothing else: "
                   '{"title": "<max 5 words>", "desc": "<max 12 words>", '
                   '"tab": "<1-2 words>"}. '
                   "Name the session by its MAIN objective — the overarching task "
                   "it was set up to accomplish, usually established in the opening "
                   "messages. Treat later one-off questions or tangents (a passing "
                   "pricing/how-to/model question) as side-quests: do NOT let them "
                   "redefine the name unless the session's whole focus has clearly "
                   "and durably shifted to a new task. "
                   "The title is a terse label; the desc is a one-line summary; "
                   "the tab is the tightest possible handle for a narrow browser "
                   "tab — one or two words that still identify the task (e.g. "
                   '"local projects", "tab ages", "router port").')
# The *digest* is the volatile companion to the (stable) title/desc: a one-line
# "what is this session doing right now", refreshed on every Stop so a controller
# (or the GUI) can read live session state without re-parsing a transcript. See
# docs/CONTROLLER.md (the reading phase). blocked_on catches a turn that ended by
# asking the human something in plain text — a soft block the `waiting` flag (TUI
# prompts only) misses.
DIGEST_SYS_PROMPT = ("You summarize the live state of a software-engineering "
                     "session for a dashboard. Given a transcript, reply with "
                     "ONLY compact JSON and nothing else: "
                     '{"digest": "<max 12 words: what it is doing right now>", '
                     '"blocked_on": "<if it is waiting on a human decision, the '
                     'question in <=12 words; else empty string>"}.')
# Project *emoji codes*: every project gets a short 1–3 emoji badge (AI-picked
# from its README / files / commits) shown on its card, the sessions rung, and
# every session tab — so a glance at the tab strip tells you which project each
# session belongs to. Generated lazily by emoji_sweep(): a brand-new repo is
# left to "mature" (enough files/commits to say something about itself) before
# its first badge, then the badge refreshes on a slow cadence so it tracks what
# the project has become. Same optional BANKR gateway as naming — unconfigured
# means no badges, nothing breaks.
EMOJI_SYS_PROMPT = ("You assign a short emoji code to a software project — a visual "
                    "badge that identifies it at a glance in a tab strip. Given the "
                    "project's name, file listing, README and recent commits, reply "
                    'with ONLY compact JSON and nothing else: {"emoji": "<1 to 3 '
                    'emoji>"}. Pick emoji that evoke the project\'s PURPOSE/domain '
                    "(not generic coding symbols like 💻 or a bare 📁). Use 2–3 emoji "
                    "when needed to be distinctive. You are given a list of emoji "
                    "codes already taken by sibling projects — your answer MUST NOT "
                    "duplicate any of them. No letters, digits or words — emoji only.")
EMOJI_REFRESH_S    = float(os.environ.get("EMOJI_REFRESH_S", str(7 * 86400)))  # re-badge cadence (projects evolve)
EMOJI_SCAN_EVERY   = float(os.environ.get("EMOJI_SCAN_EVERY", "60"))     # sweep throttle on the ~1s watch loop
EMOJI_RETRY_S      = float(os.environ.get("EMOJI_RETRY_S", "1800"))      # immature/failed → back off before retrying
EMOJI_MIN_FILES    = int(os.environ.get("EMOJI_MIN_FILES", "3"))         # maturity: this many tracked files…
EMOJI_MIN_README   = int(os.environ.get("EMOJI_MIN_README", "120"))      # …or a README with this many bytes

# Env vars that, when inherited, put a spawned `claude` into a nested/embedded
# mode (e.g. it stops writing a normal session transcript). We scrub them so the
# child is a pristine, top-level interactive session — and drop the API key so
# it authenticates with the subscription (OAuth), not metered API credits.
SCRUB_ENV = [
    "ANTHROPIC_API_KEY",
    "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_EXECPATH",
    "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_EFFORT",
    "AI_AGENT",
]

WS_GUID    = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"  # RFC 6455 magic
HERE       = Path(__file__).resolve().parent
UPLOAD_DIR = HERE / ".clawd-harness-uploads"            # pasted images land here (absolute paths → cwd-agnostic)
MAX_UPLOAD = 25 * 1024 * 1024
EXT_BY_CTYPE = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
                "image/webp": ".webp",
                "text/markdown": ".md", "text/plain": ".txt", "text/csv": ".csv",
                "application/json": ".json", "text/yaml": ".yaml",
                "text/xml": ".xml", "text/html": ".html"}
REGISTRY_FILE = HERE / ".clawd-harness.sessions.json"   # persists projects+sessions across restarts
# Every browser-initiated send is appended here (gitignored) — first-party data
# for ranking the UI's quick-prompt chips (QUICK_PROMPTS in index.html; mined by
# tools/mine_quick_prompts.py). `via:"quick"` marks a chip tap vs typed text.
PROMPTS_LOG = HERE / ".clawd-harness.prompts.jsonl"

def log_prompt(s, text, via=""):
    """Best-effort append of a user send to PROMPTS_LOG — never break a send."""
    try:
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
               "cid": s.cid, "pid": s.pid, "via": via or "typed", "text": text}
        with open(PROMPTS_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass
# Projects = git repos we drive. Each is a subdir here; a session's `claude`
# runs with cwd = its project's path. Gitignored, so the cloned repos never
# enter the harness repo. The GitHub owner new repos are created under.
PROJECTS_DIR = Path(os.environ.get("PROJECTS_DIR", str(HERE / "projects"))).resolve()
GH_OWNER     = os.environ.get("GH_OWNER", "clawdbotatg")
# How long a failed clone/create's error entry stays in the list (so the user
# can read the error) before reconcile_projects() drops it. Without a folder on
# disk the reconcile loop used to leave it forever, permanently blocking a
# retry of the same repo name.
ERROR_LINGER = float(os.environ.get("ERROR_LINGER", "45"))
# Local (kind="local") projects: how long a registered folder must be
# continuously missing before its card flips to error. Locals are never
# auto-dropped (a network volume blip must not forget an explicitly-added
# project) — the error heals when the path returns; only removeProject forgets.
LOCAL_GONE = float(os.environ.get("LOCAL_GONE", "30"))
# ...and how often the liveness stat runs: os.path.isdir on a dead network
# mount can hang, and reconcile shares the ~1s watch loop with UI reload /
# restart watching — so locals are only statted every few seconds.
LOCAL_SCAN_EVERY = float(os.environ.get("LOCAL_SCAN_EVERY", "5"))
# The harness always offers *itself* as a pinned project (path = HERE, outside
# PROJECTS_DIR) so you can open a session and live-edit the app you're running.
# Stable sentinel pid so its sessions resume across restarts; never persisted to
# the registry (always re-injected) and never removable.
SELF_PID = "self"

# ── multi-account subscription routing (docs/fleet/SUB-ROUTING.md) ───────────
# Claude Code keys its credential store off CLAUDE_CONFIG_DIR: each distinct
# dir gets its own isolated login (macOS Keychain item / .credentials.json on
# Linux). One `/login` per account dir, once — Claude Code refreshes its own
# tokens after that. So N subscriptions = N config dirs under ACCOUNTS_DIR,
# and a session spawns under whichever account is ACTIVE. The `default`
# account is the machine's plain ~/.claude login (empty config_dir — sessions
# spawn exactly as they always have). Mechanism studied from
# github.com/dennisonbertram/claw-router; implemented in-house (stdlib only).
# NOTE: an account dir's absolute path keys its Keychain item — never move it.
ACCOUNTS_DIR = Path(os.environ.get("CLAWD_ACCOUNTS_DIR",
                                   str(Path.home() / ".clawd-accounts")))
# Claude Code's production OAuth client id (public; used only for token refresh)
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
OAUTH_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
OAUTH_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"     # UNDOCUMENTED — degrade gracefully
OAUTH_PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"  # UNDOCUMENTED — degrade gracefully
OAUTH_BETA      = "oauth-2025-04-20"
USAGE_TTL       = float(os.environ.get("USAGE_TTL", "180"))     # s between usage polls per account
# How long a rate-limited (429ing) account's LAST GOOD reading stays routable:
# while the newest successful poll is younger than this, a 429 keeps the
# snapshot alive (checkedAt bumped) instead of aging it out of _best_account's
# freshness filter. Past it, the pool goes blind and drops from routing until
# a poll succeeds again — old numbers on a pool under active burn are a guess.
USAGE_RL_TRUST  = float(os.environ.get("USAGE_RL_TRUST", "1800"))
# Local routing rule (direct mode; the fleet relay will own this fleet-wide):
# among COOL pools (< SUB_HOT below — was < EXHAUSTED until the 07-11 wall
# incident), spend the one whose WEEKLY window resets soonest — weekly headroom
# is use-it-or-lose-it, so draining the earliest-resetting pool first forfeits
# the least capacity; once it resets its clock jumps +7d and it goes to the
# back of the queue. Headroom (pct) is only the fallback when a reset time is
# unknown, and the tie-break. Reset order is stable between polls, so a
# reset-driven switch needs only the DEBOUNCE; a pct-driven one also needs
# HYSTERESIS points — and a HOT active account bypasses both when a cool
# target exists (no loyalty to a pool about to wall).
SUB_AUTOSWITCH = os.environ.get("SUB_AUTOSWITCH", "1") != "0"
SUB_HYSTERESIS = float(os.environ.get("SUB_HYSTERESIS", "20"))  # headroom pts
SUB_DEBOUNCE   = float(os.environ.get("SUB_DEBOUNCE", "7200"))  # seconds
SUB_EXHAUSTED  = float(os.environ.get("SUB_EXHAUSTED", "99"))   # % used
# Mid-session handoff: an IDLE session whose plan has run dry is respawned
# under the best plan with --resume (transcript symlinked across). Checked on
# every Stop; per-session cooldown so a flapping window can't churn respawns.
HANDOFF_COOLDOWN = float(os.environ.get("HANDOFF_COOLDOWN", "600"))
# A session on a HARD-dead plan (100% used / login refused) whose hooks have
# been silent this long is stuck on the limit screen (the eaten turn never
# emits Stop, so `busy` never clears) — the sweep reclaims and moves it.
BUSY_STUCK = float(os.environ.get("BUSY_STUCK", "600"))
# Bounced-prompt rescue: a UserPromptSubmit landing on a HARD-dead plan gets
# answered by the CLI's limit line and the turn never runs — no Stop, so the
# on-Stop handoff never fires and the sweep is minutes away (worse, the prompt
# hook resets last_active, re-arming BUSY_STUCK). So the prompt itself is the
# trigger: wait SETTLE for a genuinely-running turn to show hooks, confirm the
# plan is dead against the live endpoint, hand off, and REDELIVER the bounced
# prompt on the fresh pool. COOLDOWN stops a retype storm from churning respawns.
BOUNCE_SETTLE   = float(os.environ.get("BOUNCE_SETTLE", "3"))
BOUNCE_COOLDOWN = float(os.environ.get("BOUNCE_COOLDOWN", "60"))
# Send watchdog: every delivered message must produce a UserPromptSubmit hook
# within seconds. A hard-walled CLI answers with its limit line and fires NO
# hook at all (proven live 2026-07-12 03:2x: send → hook-silent 881s → only
# the BUSY_STUCK sweep rescued it; both the hook-triggered bounce rescue and
# the PTY banner scan missed). Hook-silence after a send the harness ITSELF
# delivered is the bounce signal that needs no hook and no terminal parsing.
SEND_WATCHDOG = float(os.environ.get("SEND_WATCHDOG", "10"))
# Rebalance = the spend-the-soonest-reset policy applied to sessions ALREADY
# RUNNING: an idle session sitting on a healthy pool still moves to the
# router's best pool when that pool's weekly window resets ≥ MARGIN sooner —
# otherwise a long-lived session pins yesterday's routing choice for days
# while the soonest-resetting pool forfeits capacity. Same handoff mechanics
# and per-session cooldown as the drain rescue; the margin keeps near-ties
# (incl. same-day resets) from churning respawns.
SUB_REBALANCE = os.environ.get("SUB_REBALANCE", "1") != "0"
SUB_REBALANCE_MARGIN = float(os.environ.get("SUB_REBALANCE_MARGIN", "21600"))  # s
# NEVER SEE A RATE LIMIT: routing avoids pools at HOT (default 97% of the most-
# constrained window — usually the fast-burning 5h session window), not just at
# EXHAUSTED (99). The number is Austin's: spend the soonest-resetting pool down
# to ~3% left (retuned from 5–10%, 2026-07-23 — less forfeited headroom, leaning
# harder on the rescue backstops below), THEN hop to the next-soonest with
# headroom — earlier forfeits
# use-it-or-lose-it capacity; later is wall-flirting. While any cooler pool
# exists, a hot pool gets no new spawns or rebalances, idle sessions EVACUATE
# it (sweep), and the on-Stop check moves a session off it preemptively —
# reset-soonest still picks among the cool pools, so the spend-it-before-it's-
# forfeited policy is unchanged; it just stops slamming one pool into its
# session wall. EXHAUSTED remains the last-resort bar: a truly drained session
# may still flee TO a merely-hot pool (98 beats 100). Final backstop: the CLI's
# own limit banner, spotted in the PTY stream, triggers an immediate endpoint-
# confirmed handoff (rescue_limit_wall) instead of waiting out BUSY_STUCK — an
# eaten prompt is redelivered, and a turn cut mid-flight is resumed with an
# automatic 'continue' (LIMIT_CONTINUE=0 opts out).
SUB_HOT = float(os.environ.get("SUB_HOT", "97"))                 # % used
LIMIT_CONTINUE = os.environ.get("LIMIT_CONTINUE", "1") != "0"
# The CLI's limit banner, as painted in the PTY ("You've hit your session
# limit · resets …", or the blocking "Stop and wait for limit to reset" menu).
# Needles are deliberately narrow, and the rescue re-confirms against the live
# usage endpoint, so a session merely *displaying* this text (e.g. reading this
# file) never causes a spurious handoff.
_PTY_ANSI_RE = re.compile(
    rb"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][A-Z0-9]|\x1b[=>]")
_LIMIT_BANNER_RE = re.compile(
    r"you.?ve hit your [a-z0-9 -]{0,24}limit"    # .? = ' or ’ (the CLI uses either)
    r"|stop and wait for limit to reset"
    r"|ask your admin for more usage", re.I)
# Claude's fresh-onboarding screen (theme picker) — painted only when the
# config dir's .claude.json lacks hasCompletedOnboarding. On a login-holding
# dir that screen is ALWAYS wrong (the 07-16 ambushes); _scan_for_onboarding
# seeds the flag and respawns past it. Scanned only inside the first
# ONBOARD_SCAN_WINDOW seconds of a FRESH launch — a resume repaints recent
# conversation, so quoted picker text would false-match there (and resumes
# skip onboarding regardless, so a resume has nothing real to catch).
_ONBOARDING_RE = re.compile(
    r"choose the text style that looks best with your terminal", re.I)
ONBOARD_SCAN_WINDOW = float(os.environ.get("ONBOARD_SCAN_WINDOW", "180"))
# Paths symlinked from ~/.claude into each account dir so every account runs
# with the user's full extension environment (same list claw-router shares).
SHARE_PATHS = ["settings.json", "CLAUDE.md", "commands", "rules", "skills",
               "agents", "hooks", "workflows", "plugins"]

# Shared secret. Required on /ws and /hook because we bind to the LAN and the
# session runs with bypass-permissions — without it anyone on the wifi could run
# commands as you. Persisted so the URL/QR stays stable across restarts.
def _load_or_make_token():
    env = os.environ.get("CONSOLE_TOKEN")
    if env:
        return env
    tok_file = HERE / ".clawd-harness.token"
    try:
        return tok_file.read_text().strip()
    except OSError:
        tok = secrets.token_urlsafe(32)  # 256-bit, URL-safe (was uuid4 hex[:16] = 64-bit)
        tok_file.write_text(tok)
        return tok

TOKEN = _load_or_make_token()

# Auth posture. The token only ever existed to gate *non-loopback* (LAN) access,
# because the session runs bypass-permissions. On the default loopback bind the
# harness is reachable solely by local processes — you in a browser on this box
# and the fleet worker — so we skip the token entirely: 127.0.0.1 just works, no
# token anywhere. Remote access goes exclusively through the fleet relay, which
# enforces a passkey + end-to-end encryption that the *worker verifies locally*
# (so a pwned relay can neither drive this box nor read its sessions). Opt into a
# non-loopback bind (BIND=0.0.0.0) and the token is enforced again as the LAN guard.
AUTH_REQUIRED = BIND not in ("127.0.0.1", "localhost", "::1")


def lan_ip():
    """Best-effort primary LAN IP (no traffic actually sent)."""
    import socket as _s
    sk = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
    try:
        sk.connect(("8.8.8.8", 80))
        return sk.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sk.close()


def _transcript_exists(session_id, config_dir=""):
    base = config_dir or os.path.expanduser("~/.claude")
    return bool(glob.glob(f"{base}/projects/*/{session_id}.jsonl"))


# ── account helpers: credentials, usage, settings sharing ────────────────────
def _keychain_service(config_dir):
    """Mirror Claude Code's own Keychain item derivation: the default login is
    'Claude Code-credentials'; a CLAUDE_CONFIG_DIR login appends
    -<sha256(NFC(dir))[0:8]>."""
    if not config_dir:
        return "Claude Code-credentials"
    import unicodedata
    nfc = unicodedata.normalize("NFC", config_dir)
    return "Claude Code-credentials-" + hashlib.sha256(nfc.encode()).hexdigest()[:8]


def _read_oauth_creds_ex(config_dir):
    """(blob, definitive) — the credential JSON blob for an account dir:
    macOS Keychain first, then the Linux-style <dir>/.credentials.json.
    blob=None + definitive=True means the store POSITIVELY holds no
    credentials (the keychain answered "no such item" and no file exists);
    blob=None + definitive=False means we couldn't tell — subprocess spawn
    failure, fd exhaustion, keychain locked, timeout. Callers must treat
    the indefinite case as transient, NEVER as a sign-out: the 2026-07-11
    Errno 24 outage mass-flagged every healthy login "credentials refused"
    through exactly this ambiguity (root cause v3 in EXPECTATIONS.md)."""
    definitive = False
    try:
        r = subprocess.run(["security", "find-generic-password",
                            "-s", _keychain_service(config_dir),
                            "-a", os.environ.get("USER", ""), "-w"],
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            return json.loads(r.stdout.strip()), True
        definitive = r.returncode == 44          # errSecItemNotFound: the
                                                 # keychain ANSWERED "absent"
    except Exception:
        pass
    path = Path(config_dir or os.path.expanduser("~/.claude")) / ".credentials.json"
    try:
        return json.loads(path.read_text()), True
    except FileNotFoundError:
        return None, definitive
    except (OSError, ValueError):
        return None, False


def _read_oauth_creds(config_dir):
    """The blob alone — for callers whose failure mode is already safe on
    'unknown' (skip a persist, fail the pre-spawn gate, blank a sig)."""
    return _read_oauth_creds_ex(config_dir)[0]


def _write_oauth_creds(config_dir, blob):
    """Write the credential blob back to wherever it currently lives —
    the macOS Keychain when the Keychain holds this account, else the
    Linux-style .credentials.json (atomic replace, 0600). Best-effort bool;
    never creates a store that didn't exist (no shadowing claude's own)."""
    payload = json.dumps(blob)
    try:
        r = subprocess.run(["security", "find-generic-password",
                            "-s", _keychain_service(config_dir),
                            "-a", os.environ.get("USER", "")],
                           capture_output=True, timeout=10)
        in_keychain = r.returncode == 0
    except Exception:
        in_keychain = False
    if in_keychain:
        try:
            r = subprocess.run(["security", "add-generic-password", "-U",
                                "-a", os.environ.get("USER", ""),
                                "-s", _keychain_service(config_dir),
                                "-w", payload],
                               capture_output=True, text=True, timeout=10)
            return r.returncode == 0
        except Exception:
            return False
    path = Path(config_dir or os.path.expanduser("~/.claude")) / ".credentials.json"
    if not path.exists():
        return False
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(payload)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def _persist_refreshed(config_dir, consumed_refresh, resp):
    """After a refresh-grant call, write the new token(s) back to the
    credential store. If Anthropic ROTATES refresh tokens on use, the grant
    we just consumed is dead and discarding its replacement kills the login
    the next time anyone tries to refresh — the prime suspect for idle
    accounts dying over and over while the busy one survived (claude itself
    persists its own refreshes; the poller used to throw them away — see
    EXPECTATIONS.md). Skips the write when the store changed under us
    (claude rotated concurrently — its blob is newer than what we consumed)."""
    fresh_access = resp.get("access_token")
    if not fresh_access:
        return
    blob = _read_oauth_creds(config_dir) or {}
    oa = blob.get("claudeAiOauth")
    if not isinstance(oa, dict) or (oa.get("refreshToken") or "") != consumed_refresh:
        # store moved on — leave it be, but say so: this is the signature of
        # a concurrent rotation race (claude and the poller consuming the
        # same grant), which silent-skipping would hide from a post-mortem
        _clog(config_dir, "SKIPPED persisting a refresh — the store rotated "
                          "concurrently (another consumer of this grant beat us)")
        return
    oa["accessToken"] = fresh_access
    if isinstance(resp.get("expires_in"), (int, float)):
        oa["expiresAt"] = int((time.time() + resp["expires_in"]) * 1000)
    rotated = bool(resp.get("refresh_token")) \
        and resp["refresh_token"] != consumed_refresh
    if rotated:
        oa["refreshToken"] = resp["refresh_token"]
    ok = _write_oauth_creds(config_dir, blob)
    _clog(config_dir, "refreshed access token persisted"
          + (" — refresh token ROTATED and persisted" if rotated else "")
          + ("" if ok else " — WRITE FAILED (login will die at next refresh!)"))


def _has_creds(config_dir):
    """True iff a usable credential blob exists for this account dir RIGHT
    NOW. The pre-spawn gate: a session must never open onto a login screen."""
    oauth = (_read_oauth_creds(config_dir) or {}).get("claudeAiOauth") or {}
    return bool(oauth.get("accessToken"))


def _creds_state(config_dir):
    """'present' | 'absent' | 'unknown' — the tri-state the ambush gates need.
    'absent' ONLY when the store POSITIVELY answered empty (keychain rc 44
    and no credentials file). A store we couldn't read — locked keychain,
    subprocess spawn failure, fd exhaustion, timeout — is 'unknown', and per
    root cause v3 (EXPECTATIONS.md) that must NEVER be treated as signed-out:
    the failure can be local to THIS process (e.g. Errno 24) while the
    spawned claude child reads the keychain just fine. Suspected (pending
    log confirmation) as the path behind the 2026-07-15 leftclaw
    OAuth-screen ambush: gates that collapsed 'unknown' into 'absent'."""
    blob, definitive = _read_oauth_creds_ex(config_dir)
    oauth = (blob or {}).get("claudeAiOauth") or {}
    if oauth.get("accessToken"):
        return "present"
    return "absent" if definitive else "unknown"


def _claude_config_file(config_dir):
    """The .claude.json that gates claude's onboarding for this login:
    <dir>/.claude.json under CLAUDE_CONFIG_DIR, plain ~/.claude.json for the
    default account (claude keeps it at HOME level, NOT inside ~/.claude)."""
    return (Path(config_dir) / ".claude.json") if config_dir \
        else (Path.home() / ".claude.json")


def _ensure_onboarded(config_dir):
    """Finish a half-completed onboarding before claude can ever paint it.

    A sign-in ceremony closed after the OAuth step but before the theme
    question leaves a config dir with a VALID login (oauthAccount + keychain
    grant) and no `hasCompletedOnboarding`. Resumed sessions skip onboarding,
    so the dir works for days — then the first FRESH spawn routed onto it
    opens the full theme-picker/onboarding flow (the 2026-07-16 'AI DJ
    Prototype' / 'Audit Toolchain Scope' ambushes; heart's austinmax dir had
    carried the latent state since at least 07-13). No credential gate can
    catch this — the login is healthy; it's the ONBOARDING state that's
    missing, and only claude reads it.

    So the harness completes the onboarding itself: if the dir holds a login
    but the flag is missing, seed `hasCompletedOnboarding: true` (the theme
    is cosmetic — unset renders as the default, exactly like our known-good
    dirs whose `theme` is null). A dir with NO login (a pending sign-in
    ceremony) is left strictly alone: its onboarding screen is the point.
    Returns True iff it seeded."""
    cfg = _claude_config_file(config_dir)
    try:
        data = json.loads(cfg.read_text()) if cfg.exists() else {}
    except (OSError, ValueError):
        return False                    # unreadable/foreign file — not ours to rewrite
    if data.get("hasCompletedOnboarding"):
        return False
    if not (data.get("oauthAccount") or _creds_state(config_dir) == "present"):
        return False                    # no login yet — a real ceremony dir
    data["hasCompletedOnboarding"] = True
    tmp = cfg.with_name(cfg.name + ".onboard-seed.tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, cfg)
    except OSError as e:
        print(f"[creds {config_dir or '~/.claude'}] onboarding seed WRITE "
              f"FAILED ({e}) — the next fresh spawn here may show the theme "
              "picker", flush=True)
        return False
    print(f"[creds {config_dir or '~/.claude'}] half-finished onboarding "
          "completed (hasCompletedOnboarding seeded — login already present, "
          "theme picker suppressed)", flush=True)
    return True


def _cred_sig(config_dir):
    """Fingerprint of the stored credential blob, '' when there is none.
    Lets the poller tell 'the same refused login is still sitting there'
    apart from 'someone actually re-signed in' — a broken account is only
    re-admitted to routing when this changes."""
    oauth = (_read_oauth_creds(config_dir) or {}).get("claudeAiOauth") or {}
    tok = oauth.get("accessToken") or ""
    if not tok:
        return ""
    return hashlib.sha256(
        f"{tok}:{oauth.get('refreshToken') or ''}".encode()).hexdigest()


def _link_transcript(session_id, src_cfg, dst_cfg):
    """Make `--resume` under dst_cfg find a transcript recorded under src_cfg:
    symlink the .jsonl (+ its subagents dir) into dst's projects tree.
    Real-file-wins, never clobber; best-effort (a miss just means the session
    starts fresh instead of resuming)."""
    src_base = Path(src_cfg or os.path.expanduser("~/.claude"))
    dst_base = Path(dst_cfg or os.path.expanduser("~/.claude"))
    if src_base == dst_base:
        return
    for hit in glob.glob(f"{src_base}/projects/*/{session_id}.jsonl"):
        src = Path(hit)
        for extra in [src, src.with_suffix("")]:
            if not extra.exists():
                continue
            dst = dst_base / extra.relative_to(src_base)
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                if not (dst.exists() or dst.is_symlink()):
                    dst.symlink_to(extra)
            except OSError as e:
                print(f"[transcript link] {dst} failed: {e}", flush=True)


AUTH_FAIL = "auth"   # _fetch_usage sentinel: credentials are present but refused
RATE_LIMITED = "rate_limited"  # _fetch_usage sentinel: the usage endpoint 429'd.
# AMBIGUOUS by itself: a hard-limited plan 429s its usage endpoint, but so does
# the endpoint's own per-account limiter when the fleet polls too hard (Retry-
# After: 0, plan nowhere near its wall — the 2026-07-19 'limited 0%' incident,
# where the fake-100% placeholder this used to return evacuated healthy pools
# and mis-routed new sessions for hours). So the POLLER treats it as "keep the
# last good reading + back off", while the rescue paths — which only run with
# independent evidence of a wall (a bounced prompt, the CLI's limit banner in
# the PTY) — treat it as corroboration.


def _clog(config_dir, msg):
    """Timestamped credential-event log line. The 2026-07-09 post-mortem was
    nearly impossible because account/creds events had no timestamps — every
    credential-lifecycle event goes through here now."""
    print(f"[creds {config_dir or '~/.claude'} "
          f"{time.strftime('%m-%d %H:%M:%S')}] {msg}", flush=True)


def _claude_ua():
    """User-Agent for the OAuth token endpoint, mimicking the claude CLI's
    own (`claude-cli/<version> (external, cli)`). REQUIRED: the endpoint
    rate-limits by client identity — curl's default UA gets a blanket 429
    `rate_limit_error` on EVERY request (no Retry-After; 2026-07-09→11 every
    refresh on this box failed that way, starving the router of usage data),
    while the claude-cli UA succeeds immediately. Version read from the real
    binary once so the UA tracks upgrades; any failure falls back to a
    known-good pin."""
    ua = getattr(_claude_ua, "_cached", None)
    if ua:
        return ua
    ver = "2.1.207"
    try:
        out = subprocess.run([CLAUDE_BIN, "--version"], capture_output=True,
                             text=True, timeout=10).stdout.strip().split()
        if out and out[0][0:1].isdigit():
            ver = out[0]
    except Exception:
        pass
    ua = f"claude-cli/{ver} (external, cli)"
    _claude_ua._cached = ua
    return ua


def _refresh_grant(refresh):
    """POST a refresh grant via CURL and return (http_status, body_dict).

    Curl, not urllib, ON PURPOSE: the token endpoint (platform.claude.com)
    sits behind Cloudflare bot protection that 403s Python's TLS signature
    with 'error code: 1010' — every urllib refresh in this file's history
    FAILED AT THE EDGE without ever reaching Anthropic, and the harness
    misread that as revoked credentials (the 'idle logins keep dying'
    epidemic — see EXPECTATIONS.md 2026-07-09). Curl's signature passes
    the edge, but the app then rate-limits curl's DEFAULT User-Agent —
    hence _claude_ua(). The token travels via stdin so it never appears
    in `ps` output."""
    try:
        r = subprocess.run(
            ["curl", "-sS", "-m", "15", "-w", "\n%{http_code}",
             "-X", "POST", OAUTH_TOKEN_URL,
             "-H", "Content-Type: application/json",
             "-H", f"User-Agent: {_claude_ua()}",
             "--data-binary", "@-"],
            input=json.dumps({"grant_type": "refresh_token",
                              "refresh_token": refresh,
                              "client_id": OAUTH_CLIENT_ID}),
            capture_output=True, text=True, timeout=25)
        body_txt, _, status_txt = (r.stdout or "").rpartition("\n")
        status = int(status_txt) if status_txt.strip().isdigit() else None
        try:
            body = json.loads(body_txt)
        except ValueError:
            body = {}
        return status, body
    except Exception:
        return None, {}


def _fetch_profile(tok):
    """Token-bound identity {email, org, org_name, tier} via the OAuth profile
    endpoint (UNDOCUMENTED — degrade gracefully). This is the truth about
    WHOSE usage pool `tok` draws from: .claude.json can lie (a re-login can
    update the keychain and leave the json stale, so an account wears the
    wrong email), but the token cannot. None on any failure — callers keep
    what they have."""
    req = urllib.request.Request(OAUTH_PROFILE_URL, headers={
        "Authorization": f"Bearer {tok}", "anthropic-beta": OAUTH_BETA})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            p = json.loads(r.read().decode())
        acct, org = p.get("account") or {}, p.get("organization") or {}
        return {"email": acct.get("email") or "",
                "org": org.get("uuid") or "",
                "org_name": org.get("name") or "",
                "tier": org.get("rate_limit_tier") or ""}
    except Exception:
        return None


def _fetch_usage(config_dir, tok_cache=None, want_ident=False,
                 allow_refresh=True):
    """(pct_used, windows) for one account via Claude's OAuth usage endpoint —
    (pct_used, windows, ident|None) when want_ident, ident fetched from the
    profile endpoint with the same working token; AUTH_FAIL ONLY when
    Anthropic's OAuth service itself rejects the refresh grant (the account
    truly needs a re-sign-in) or the store POSITIVELY holds no credentials;
    None for everything else (network blip, Cloudflare block, endpoint
    change, unreadable credential store) — callers keep the last snapshot
    and the router stays put rather than flapping to a blind guess. NEVER
    map an infra failure to AUTH_FAIL: that exact misdiagnosis caused every
    'idle login died' incident — Cloudflare 1010 read as revocation before
    2026-07-09, an fd-starved credential read on 2026-07-11 (root cause v3).
    allow_refresh=False = poll with the stored access token only and return
    None when it's expired — for accounts whose grant a live claude process
    may also hold (two consumers of one rotating grant can kill the family).
    `tok_cache` (a mutable dict) keeps a refreshed access token in memory
    across polls (plus the 429 back-off horizon); refreshed tokens are
    written back to the credential store via _persist_refreshed."""
    blob, definitive = _read_oauth_creds_ex(config_dir)
    oauth = (blob or {}).get("claudeAiOauth") or {}
    access, refresh = oauth.get("accessToken"), oauth.get("refreshToken")
    cached = (tok_cache or {}).get("access")
    if not (access or cached or refresh):
        # Nothing usable. Only a POSITIVE "no credentials stored" verdict is
        # a sign-out; an unreadable store is an infra failure (Errno 24 made
        # the `security` spawn itself fail on 2026-07-11 and this exact spot
        # flagged all seven healthy logins "refused") — keep the last
        # snapshot and let the next poll retry.
        if not definitive:
            _clog(config_dir, "credential store unreadable — transient; "
                              "keeping the last snapshot")
            return None
        return AUTH_FAIL
    # A stored access token we KNOW is expired is junk traffic: it can't
    # answer, and the failed calls feed the endpoint's rate limiter until
    # even honest polls 429 — which painted a freshly-reset pool as
    # 'limited · 0%' on 2026-07-09. Skip straight to refresh instead.
    exp = oauth.get("expiresAt")
    if access and isinstance(exp, (int, float)) \
            and exp / 1000 <= time.time() + 60:
        access = None

    def call(tok):
        req = urllib.request.Request(OAUTH_USAGE_URL, headers={
            "Authorization": f"Bearer {tok}", "anthropic-beta": OAUTH_BETA})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read().decode()), None
        except urllib.error.HTTPError as e:
            return e.code, None, e.headers.get("Retry-After")
        except Exception:
            return None, None, None

    code = usage = retry_after = None
    good = None                                  # the token the 200 came from
    tries = [t for t in (cached, access) if t]
    for tok in tries:
        code, usage, retry_after = call(tok)
        if code == 200:
            good = tok
        if code != 401:
            break
        if tok is cached and tok_cache:          # cache went stale — drop it
            tok_cache.pop("access", None)
    needs_auth = (code == 401) or not tries      # refused, or nothing usable
    if needs_auth and not allow_refresh:
        return None                              # stale, not dead: a live claude
                                                 # owns this grant and will renew it
    if needs_auth and refresh:
        rstatus, tokresp = _refresh_grant(refresh)
        fresh = tokresp.get("access_token")
        if fresh:
            if tok_cache is not None:
                tok_cache["access"] = fresh
            # write the new token(s) back — a consumed grant's rotated
            # replacement must never be discarded
            _persist_refreshed(config_dir, refresh, tokresp)
            code, usage, retry_after = call(fresh)
            if code == 200:
                good = fresh
        elif rstatus in (400, 401):
            # the OAuth service itself rejected the grant — the one and only
            # signal that a re-sign-in is genuinely needed
            _clog(config_dir, f"refresh REJECTED by the OAuth service "
                              f"(HTTP {rstatus} {json.dumps(tokresp)[:120]}) "
                              "— this login needs a re-sign-in")
            return AUTH_FAIL
        else:
            # edge block / rate limit / outage — NOT a dead login. Back off
            # here too: retrying a 429ing token endpoint every 15s poller
            # cycle keeps its limiter hot and the refresh never succeeds.
            if tok_cache is not None:
                tok_cache["no_poll_until"] = time.time() + 600
            _clog(config_dir, f"refresh blocked in transit (HTTP {rstatus}) "
                              "— transient; keeping the last snapshot, "
                              "next attempt in 10 min")
            return None
    if code == 401 or (not tries and code is None):
        return AUTH_FAIL                         # refused even after refresh,
                                                 # or no usable token at all
    if code == 429:
        # Rate-limited — see the RATE_LIMITED sentinel comment for why this is
        # NOT reported as fully used. Back off exponentially per consecutive
        # 429 (observed storms send Retry-After: 0, so honoring the header
        # alone kept re-poking a hot limiter every 60s forever).
        streak = (tok_cache.get("rl_streak", 0) + 1) if tok_cache is not None else 1
        if tok_cache is not None:
            tok_cache["rl_streak"] = streak
        try:
            base = max(60.0, float(retry_after))
        except (TypeError, ValueError):
            base = 60.0
        until = time.time() + min(1800.0, base * (2 ** min(streak - 1, 5)))
        if tok_cache is not None:
            tok_cache["no_poll_until"] = until
        return RATE_LIMITED
    if code != 200 or not isinstance(usage, dict):
        return None
    if tok_cache is not None:
        tok_cache.pop("rl_streak", None)         # endpoint recovered
    windows, worst = [], 0.0
    for key, label in [("five_hour", "5h"), ("seven_day", "7d"),
                       ("seven_day_opus", "7d opus"),
                       ("seven_day_sonnet", "7d sonnet")]:
        w = usage.get(key)
        used = w.get("utilization") if isinstance(w, dict) else w
        if not isinstance(used, (int, float)):
            continue
        worst = max(worst, used)
        windows.append({"key": key, "label": label, "used": round(used, 1),
                        "resets": w.get("resets_at") if isinstance(w, dict) else None})
    # Model-scoped caps (e.g. the Fable weekly limit) only appear in the newer
    # `limits` array — the legacy per-model keys above are null for them. The
    # unscoped kinds (session / weekly_all) duplicate five_hour / seven_day,
    # so only scoped entries with a model name are added.
    group_label = {"session": "5h", "weekly": "7d"}
    for lim in usage.get("limits") or []:
        if not isinstance(lim, dict):
            continue
        pct = lim.get("percent")
        model = (((lim.get("scope") or {}).get("model") or {})
                 .get("display_name") or "")
        if not isinstance(pct, (int, float)) or not model:
            continue
        worst = max(worst, pct)
        grp = group_label.get(lim.get("group"), lim.get("group") or "")
        windows.append({"key": f"{lim.get('kind') or 'scoped'}_{model.lower()}",
                        "label": f"{grp} {model.lower()}".strip(),
                        "used": round(float(pct), 1),
                        "resets": lim.get("resets_at")})
    if not windows:
        return None
    if want_ident:
        return worst, windows, (_fetch_profile(good) if good else None)
    return worst, windows


def _parse_reset(ts):
    """A window's resets_at ISO timestamp → epoch seconds; None when absent
    or unparseable (never let a malformed API field break routing)."""
    if not isinstance(ts, str) or not ts:
        return None
    try:
        return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _weekly_reset(usage):
    """Soonest WEEKLY reset (epoch s) across an account's cached usage
    windows — every weekly window's label starts '7d' (incl. model-scoped
    ones like '7d fable'). The 5h window is deliberately ignored: it cycles
    all day on every pool and says nothing about whose weekly capacity is
    about to be forfeited."""
    soonest = None
    for w in (usage or {}).get("windows") or []:
        if not str(w.get("label", "")).startswith("7d"):
            continue
        t = _parse_reset(w.get("resets"))
        if t and (soonest is None or t < soonest):
            soonest = t
    return soonest


def _account_identity(config_dir):
    """Best-effort (email, org_uuid, org_name) from the account's .claude.json
    (the default account's lives at ~/.claude.json, not inside ~/.claude). The
    ORG uuid — not the email — names the usage pool: one email can hold
    seats in several orgs (personal + team), each with its own limits, so
    grouping plans by email merges pools that are actually separate. The org
    NAME matters most for BROKEN logins: the token-bound profile fetch can't
    run without a working token, and a dead card titled by its email's local
    part hides which plan needs the re-sign-in."""
    path = (Path(config_dir) / ".claude.json") if config_dir \
        else (Path.home() / ".claude.json")
    try:
        oa = json.loads(path.read_text()).get("oauthAccount") or {}
        return (oa.get("emailAddress") or "", oa.get("organizationUuid") or "",
                oa.get("organizationName") or "")
    except (OSError, ValueError):
        return ("", "", "")


def _link_shared_paths(config_dir):
    """Symlink the user's ~/.claude extension environment (SHARE_PATHS) into an
    account dir so every account behaves identically. Idempotent; never
    clobbers a real file, only replaces stale symlinks."""
    src_home = Path.home() / ".claude"
    dst_home = Path(config_dir)
    dst_home.mkdir(parents=True, exist_ok=True)
    for name in SHARE_PATHS:
        src, dst = src_home / name, dst_home / name
        if not src.exists():
            continue
        if dst.is_symlink():
            if os.readlink(str(dst)) == str(src):
                continue
            dst.unlink()
        elif dst.exists():
            continue                             # real file — leave it alone
        try:
            dst.symlink_to(src)
        except OSError:
            pass


def _share_projects(config_dir):
    """Point <account>/projects at the shared ~/.claude/projects store so
    EVERY account sees EVERY session transcript: --resume works under any
    plan, account handoffs need no per-file links, and the PM can jump plans
    between turns without losing its threads. Migrates a real per-account
    projects dir by moving its transcripts into the shared store first
    (never clobbering); aborts harmlessly if anything is in the way."""
    src = Path.home() / ".claude" / "projects"
    dst = Path(config_dir) / "projects"
    try:
        src.mkdir(parents=True, exist_ok=True)
        if dst.is_symlink():
            return
        if dst.is_dir():
            for proj in list(dst.iterdir()):
                tgt = src / proj.name
                if proj.is_dir():
                    tgt.mkdir(exist_ok=True)
                    for f in list(proj.iterdir()):
                        if not (tgt / f.name).exists():
                            f.rename(tgt / f.name)
                    proj.rmdir()                 # raises if we skipped anything
                elif not tgt.exists():
                    proj.rename(tgt)
            dst.rmdir()
        elif dst.exists():
            return
        Path(config_dir).mkdir(parents=True, exist_ok=True)
        dst.symlink_to(src)
        print(f"[accounts] {config_dir}/projects → shared ~/.claude/projects",
              flush=True)
    except OSError as e:
        print(f"[accounts] projects share skipped for {config_dir}: {e}",
              flush=True)


def _merge_mcp(config_dir):
    """One-shot after login: merge ~/.claude.json's mcpServers into the
    account's .claude.json (shared source wins) so accounts share the MCP
    environment. Identity keys untouched; atomic replace."""
    dst = Path(config_dir) / ".claude.json"
    try:
        shared = json.loads((Path.home() / ".claude.json").read_text()
                            ).get("mcpServers") or {}
        if not shared or not dst.exists():
            return
        obj = json.loads(dst.read_text())
        merged = dict(obj.get("mcpServers") or {})
        merged.update(shared)
        obj["mcpServers"] = merged
        tmp = dst.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(obj, indent=2))
        os.replace(tmp, dst)
    except (OSError, ValueError) as e:
        print(f"[account] mcp merge skipped for {config_dir}: {e}", flush=True)


class Account:
    """One Claude subscription login = one config dir. `default` is the
    machine's plain ~/.claude login (empty config_dir — sessions spawn exactly
    as they always have). `ready` flips when credentials are first observed
    (i.e. the sign-in ceremony completed)."""

    def __init__(self, name, config_dir="", email="", org="", org_name="",
                 tier="", ready=False, created=0.0, usage=None):
        self.name = name
        self.config_dir = config_dir
        self.email = email
        self.org = org                           # organizationUuid = the usage pool
        self.org_name = org_name                 # human name of that org (profile)
        self.tier = tier                         # rate_limit_tier, e.g. …max_20x
        self.ready = ready
        self.created = created or time.time()
        self.usage = usage or None               # {"pct","windows","checkedAt"}
        self.error = ""                          # last poll error (in-memory)
        self.broken = False                      # ready but credentials now refused →
                                                 # excluded from routing until re-sign-in
        self.refused_sig = ""                    # _cred_sig of the blob that was refused —
                                                 # re-admit only when it changes
        self.tok = {}                            # in-memory refreshed-access-token cache
        self.last_pending_check = 0.0            # backoff anchor while awaiting sign-in

    def to_registry(self):
        return {"name": self.name, "config_dir": self.config_dir,
                "email": self.email, "org": self.org,
                "org_name": self.org_name, "tier": self.tier,
                "ready": self.ready,
                "created": self.created, "usage": self.usage}

    def meta(self, active=False):
        pct = (self.usage or {}).get("pct")
        status = ("pending" if not self.ready
                  else "needs-login" if self.broken else "ready")
        return {"name": self.name, "email": self.email,
                "orgUuid": self.org, "orgName": self.org_name,
                "tier": self.tier,
                "status": status,
                "active": active, "usagePct": pct,
                "headroom": None if pct is None else round(100 - pct, 1),
                "windows": (self.usage or {}).get("windows") or [],
                "checkedAt": (self.usage or {}).get("checkedAt"),
                "error": self.error,
                "configDir": self.config_dir}


def _safe_name(name):
    """A filesystem/repo-safe slug from a free-text project name."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", (name or "").strip()).strip("-._")
    return slug or "project"


def _scrub_url_creds(url):
    """Strip any embedded userinfo (`user:token@`) from an http(s) URL so a
    credential baked into a remote (e.g. `gh repo create --clone` writing a
    tokenized URL) never gets stored in the registry or broadcast to the UI."""
    return re.sub(r"(https?://)[^/@]*@", r"\1", url or "")


def _git_remote_url(path):
    """Best-effort origin URL for a repo (empty string if none). Credentials
    embedded in the URL are scrubbed — we display/persist this value."""
    try:
        r = subprocess.run(["git", "remote", "get-url", "origin"],
                           cwd=path, capture_output=True, text=True, timeout=5)
        return _scrub_url_creds(r.stdout.strip()) if r.returncode == 0 else ""
    except Exception:
        return ""


def _remote_repo_exists(slug):
    """True iff `owner/repo` already exists on GitHub (per `gh repo view`).
    Used to turn a `create` of an already-existing repo into a `clone` instead
    of letting `gh repo create` 422. Best-effort: if `gh` is missing or errors
    for any other reason, returns False so we fall through to the normal create
    path (which surfaces its own error)."""
    try:
        r = subprocess.run(["gh", "repo", "view", slug],
                           capture_output=True, text=True, timeout=15)
        return r.returncode == 0
    except Exception:
        return False


# ── project: a git repo under PROJECTS_DIR that sessions run inside ───────────
class Project:
    """One git repo we drive. Owns no processes itself — it's the workdir N
    ClaudeSessions launch in. `status` tracks an async clone/create."""

    def __init__(self, pid, name, path, repo_url="", status="ready",
                 error="", created=0.0, pinned=False, kind="gh",
                 emoji="", emoji_at=0.0):
        self.pid = pid
        self.name = name
        self.path = path                         # abs path to the repo
        self.kind = kind if kind in ("gh", "local") else "gh"
        # local = a private folder registered in place: it must never carry a
        # remote URL, no matter which code path constructs it
        self.repo_url = "" if self.kind == "local" else _scrub_url_creds(repo_url)
        self.status = status                     # ready | cloning | error
        self.error = error
        self.error_at = 0.0                      # when status flipped to error (in-memory only)
        self.miss_since = 0.0                    # local liveness: path first seen missing (in-memory only)
        self.created = created or time.time()
        self.pinned = pinned                     # the harness-itself project: top of list, not removable
        self.emoji = emoji                       # AI-picked 1–3 emoji identity badge
        self.emoji_at = emoji_at                 # when it was last generated (drives refresh)
        self.emoji_retry_at = 0.0                # backoff anchor after immature/failed generation (in-memory only)

    def to_registry(self):
        return {"pid": self.pid, "name": self.name, "path": self.path,
                "repo_url": self.repo_url, "status": self.status,
                "created": self.created, "kind": self.kind,
                "emoji": self.emoji, "emoji_at": self.emoji_at}

    def meta(self, session_count=0, busy_count=0, waiting_count=0, last_touched=0.0):
        return {"pid": self.pid, "name": self.name, "path": self.path,
                "repoUrl": self.repo_url, "status": self.status,
                "error": self.error, "sessionCount": session_count,
                "busyCount": busy_count, "waitingCount": waiting_count,
                "created": self.created, "pinned": self.pinned,
                "kind": self.kind, "lastTouched": last_touched,
                "emoji": self.emoji}


# ── PTY-backed Claude session ─────────────────────────────────────────────────
class ClaudeSession:
    """One interactive `claude` process in a PTY, streamed to the websocket
    clients currently *subscribed* to it. Owned by a SessionManager."""

    def __init__(self, manager, cid, session_id, resuming, pid="",
                 title="", desc="", tab="", prompt_count=0, first_prompt="",
                 created=0.0, last_active=0.0, prompted_at=0.0,
                 account="default", config_dir="", ceremony=False,
                 pinned=0.0, model="", ctx_tokens=0):
        self.manager = manager
        self.pid = pid                           # owning project id
        self.cid = cid                           # stable console id (ours; survives claude rotation)
        self.session_id = session_id             # claude's id (rotates on compaction/resume)
        self.resuming = resuming
        self.created = created or time.time()
        # Which subscription this claude runs under — RECORDED AT SPAWN and
        # persisted: a --resume after a harness restart must reuse the same
        # config dir or the transcript/login won't be found. config_dir is the
        # resolved path (not re-derived from the account name) so the session
        # keeps resuming even if the account entry is later removed.
        self.account = account or "default"
        self.config_dir = config_dir
        self.last_handoff = 0.0                  # cooldown anchor for account handoffs
        # Sign-in ceremony session (the 🧠 panel's add / re-sign-in flow):
        # its ENTIRE PURPOSE is to sit on a broken/credential-less account
        # while the human completes OAuth — exactly the state every reactive
        # rescue (handoff sweep, limit-wall/bounce rescues, onboarding heal)
        # reads as "dead plan, move it". Those paths must leave it strictly
        # alone: a handoff would --resume a transcript-less session under
        # another dir ("no conversation found") and yank the user out
        # mid-login. The flag is permanent for the session's lifetime and
        # persisted, so a harness restart mid-ceremony can't strip it.
        self.ceremony = ceremony
        # 📌 parked on the pin board ("test this later"): timestamp when pinned,
        # 0.0 = not pinned. A pinned session stays fully alive (its claude keeps
        # running and can still be prompted) — the flag only tells the UI to
        # move it off the active tab strip onto the pin board. Persisted so a
        # restart doesn't dump every parked to-do back into the tabs.
        self.pinned = pinned

        self.title = title
        self.desc = desc
        self.tab = tab                           # 1-2 word label for the tab strip (AI, like title/desc)
        # Splash-card hints: which model is answering (transcript assistant
        # lines are the only place the CLI states it) and the latest turn's
        # input-side token total — "how full is the context window".
        self.model = model
        self.ctx_tokens = ctx_tokens
        self.prompt_count = prompt_count
        self.first_prompt = first_prompt
        self.last_active = last_active or self.created   # warmth: drives project sort
        # When the HUMAN last prompted this session — the tab-strip age. Distinct
        # from last_active, which every hook (incl. resume's SessionStart) bumps:
        # a harness restart must NOT reset every tab's age to 0s.
        self.prompted_at = prompted_at

        self.master_fd = None
        self.os_pid = None                       # claude's process pid (not the project pid)
        self.proc = None
        self.alive = False

        self.ring = bytearray()                  # recent PTY output for late joiners
        self.ring_lock = threading.Lock()

        self.clients = set()                     # _Clients currently viewing this session
        self.clients_lock = threading.Lock()

        # One PTY, many differently-sized viewers (phone + desktop on the same
        # session): claude's TUI paints for exactly one geometry, so the PTY
        # follows a single OWNER at a time instead of last-resize-wins. See
        # claim_resize() for the policy.
        self.tty_owner = None                    # the _Client whose size the PTY follows
        self.tty_cols, self.tty_rows = COLS, ROWS

        self.transcript_path = None
        self._live_transcript = None             # live path from hooks; may rotate on compaction
        self.busy = False                        # working (turn in flight) vs idle
        self.waiting = False                      # blocked on an interactive prompt (permission / question)
        self.bg = ""                              # background work while idle: "shell" | "agent" | "" (poll_bg)
        self._bg_pending = ""                     # debounce: bg candidate seen on the previous sweep
        self.hook_count = 0                       # bumps on every hook — "did the turn progress?" probe
        self.last_bounce_rescue = 0.0             # cooldown anchor for the bounced-prompt rescue
        self.last_prompt = ""                     # most recent user prompt — redelivered if a limit wall eats it
        self.hooks_at_prompt = 0                  # hook_count when it landed — "did that turn ever progress?"
        self._limit_tail = ""                     # rolling de-ANSI'd PTY text, for the limit-banner scan
        self._limit_seen_at = 0.0                 # cooldown anchor for banner-triggered rescues
        self._onboard_tail = ""                   # rolling de-ANSI'd PTY text, for the onboarding-screen scan
        self._onboard_deadline = 0.0              # scan window end; start() arms it, a match disarms it
        self._onboard_rescues = 0                 # respawns burned on this cid (carried across; caps the loop)
        self._started_evt = threading.Event()     # set on SessionStart — "the TUI is up"
        self.last_tool = None
        self.digest = ""                          # volatile "what it's doing now" (LLM, refreshed each Stop)
        self.blocked_on = None                    # the open question if it ended asking the human (LLM)
        self.settings_path = None

    # -- registry shape --------------------------------------------------------
    def to_registry(self):
        return {"cid": self.cid, "pid": self.pid, "session_id": self.session_id,
                "title": self.title, "desc": self.desc, "tab": self.tab,
                "prompt_count": self.prompt_count, "first_prompt": self.first_prompt,
                "created": self.created, "last_active": self.last_active,
                "prompted_at": self.prompted_at,
                "account": self.account, "config_dir": self.config_dir,
                "ceremony": self.ceremony, "pinned": self.pinned,
                "model": self.model, "ctx_tokens": self.ctx_tokens}

    def clone_for_respawn(self, **overrides):
        """A fresh session object for an in-place respawn under the SAME cid
        (account handoff, onboarding heal). Kwargs are derived from the
        constructor signature, so EVERY durable field rides across
        automatically — a hand-copied field list here is exactly how 📌 pins
        (and before them, other session state) kept getting silently reset:
        each new persisted field had to be remembered in every respawn site,
        and one miss meant the next rebalance sweep wiped it and
        save_registry() made the loss permanent. Pass overrides only for what
        the respawn actually changes (account, config_dir, resuming, …).
        tools/test_respawn_clone.py holds the invariant."""
        kw = {n: getattr(self, n)
              for n in inspect.signature(ClaudeSession.__init__).parameters
              if n not in ("self", "manager")}
        kw.update(overrides)
        fresh = ClaudeSession(self.manager, **kw)
        fresh.last_handoff = self.last_handoff
        fresh._onboard_rescues = self._onboard_rescues
        return fresh

    def workdir(self):
        """Where this session's claude runs — its project's repo path."""
        proj = self.manager.projects.get(self.pid)
        return proj.path if proj else WORKDIR

    def _fallback_title(self):
        if self.first_prompt:
            words = self.first_prompt.split()
            t = " ".join(words[:7])
            return (t[:46] + "…") if len(t) > 47 else t
        return "new session"

    def meta(self):
        """Menu-level snapshot broadcast to every client."""
        # Deterministic, LLM-free status for the controller's attention queue:
        # blocked (needs a human now) > working (turn in flight) > background
        # (turn ended but claude still has background shells/agents running —
        # see poll_bg) > idle.
        status = "blocked" if self.waiting else \
                 ("working" if self.busy else
                  ("background" if self.bg else "idle"))
        return {"cid": self.cid, "pid": self.pid,
                "title": self.title or self._fallback_title(),
                "desc": self.desc or "",
                "tab": self.tab or "",
                "named": bool(self.title),
                "busy": self.busy, "waiting": self.waiting, "tool": self.last_tool,
                "status": status, "bg": self.bg,
                "digest": self.digest or "",
                "blocked_on": self.blocked_on or "",
                "sessionId": self.session_id,
                "promptCount": self.prompt_count,
                "lastActive": self.last_active,
                "promptedAt": self.prompted_at,
                "created": self.created,
                "alive": self.alive,
                "account": self.account,
                "pinned": self.pinned,
                "model": self.model,
                "ctxTokens": self.ctx_tokens}

    def _bg_probe(self):
        """One read-only look at claude's status file → "shell" | "agent" | "".
        No state change — safe to call from any thread (the handoff guards use
        it fresh, skipping poll_bg's UI debounce)."""
        if not (self.alive and self.os_pid and not self.busy):
            return ""
        base = self.config_dir or os.path.expanduser("~/.claude")
        try:
            with open(os.path.join(base, "sessions",
                                   f"{self.os_pid}.json")) as f:
                st = (json.load(f) or {}).get("status")
        except Exception:
            return ""
        return {"shell": "shell", "busy": "agent"}.get(st, "")

    def poll_bg(self):
        """Detect background work our Stop-driven `busy` can't see. Claude
        publishes its own live status to <config_dir>/sessions/<pid>.json
        (undocumented — always degrade): "shell" = a run_in_background shell
        is still running after the turn ended; "busy" while OUR busy is False
        = background agents (delegatedActive keeps claude "busy" between
        turns). Truly-disowned jobs (nohup … & disown) are invisible even to
        claude itself — out of scope. Turning ON needs two consecutive sweeps
        agreeing (claude flips the file to idle at ~the same moment the Stop
        hook fires, so a single racy read would flash a phantom 'background'
        at every turn end); turning OFF is immediate. Returns True when
        self.bg changed (caller broadcasts)."""
        bg = self._bg_probe()
        if bg and bg != self.bg and self._bg_pending != bg:
            self._bg_pending = bg                # first sighting — arm only
            return False
        self._bg_pending = ""
        if bg != self.bg:
            self.bg = bg
            return True
        return False

    # -- lifecycle -------------------------------------------------------------
    def start(self):
        master, slave = pty.openpty()
        self._set_winsize(master, ROWS, COLS)

        env = dict(os.environ)
        env["TERM"] = "xterm-256color"
        env["COLORTERM"] = "truecolor"          # xterm.js renders 24-bit; let claude emit it
        env["COLUMNS"] = str(COLS)
        env["LINES"] = str(ROWS)
        for k in SCRUB_ENV:                      # pristine top-level + subscription auth
            env.pop(k, None)
        # Claude Code can render its whole TUI in the ALTERNATE screen buffer —
        # a server-side rollout, so it flips on per-account with no CLI update
        # or harness change (sub2 flipped mid-day 2026-07-16). xterm.js has no
        # scrollback in the alt buffer, so every scroll path dies silently: the
        # ring replay and _history_seed_bytes paint into the hidden normal
        # buffer and a phone's touch pan finds nothing to scroll. Pin inline
        # rendering — the seed/ring/scrollback contract depends on it.
        env["CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN"] = "1"
        if self.config_dir:                      # non-default subscription account
            env["CLAUDE_CONFIG_DIR"] = self.config_dir
        else:
            # default = plain ~/.claude, always: an operator-exported
            # CLAUDE_CONFIG_DIR would strand transcripts where our globs
            # (config_dir or ~/.claude) never look.
            env.pop("CLAUDE_CONFIG_DIR", None)

        # Every spawn path (fresh, --resume, handoff, restart) funnels through
        # here — the one place to guarantee claude never opens onto the
        # onboarding/theme screen when the dir already holds a login.
        _ensure_onboarded(self.config_dir)
        # PTY tripwire: FRESH spawns only. A --resume REPAINTS recent
        # conversation, so a session that ever quoted the picker text (this
        # repo's sources; the session that wrote this fix) re-trips the scan
        # on every resume and gets respawn-cycled (sub2/1951a6f5 2026-07-16).
        # Resumes can't hit the real ambush anyway — they skip onboarding even
        # on an unflagged dir (austinmax ran resumed handoffs for days
        # half-onboarded) — and the _ensure_onboarded seed above covers them.
        self._onboard_deadline = 0.0 if self.resuming \
            else time.time() + ONBOARD_SCAN_WINDOW

        self.settings_path = self._write_hook_settings()
        cmd = [CLAUDE_BIN,
               ("--resume" if self.resuming else "--session-id"), self.session_id,
               "--settings", self.settings_path]

        def _preexec():
            os.setsid()
            fcntl.ioctl(slave, termios.TIOCSCTTY, 0)  # slave becomes controlling tty

        self.proc = subprocess.Popen(
            cmd, cwd=self.workdir(), env=env,
            stdin=slave, stdout=slave, stderr=slave,
            preexec_fn=_preexec, close_fds=True,
        )
        os.close(slave)                          # parent only needs the master
        self.master_fd = master
        self.os_pid = self.proc.pid
        self.alive = True
        print(f"[session {self.cid[:8]}] claude pid={self.os_pid} "
              f"session_id={self.session_id} account={self.account} "
              f"({'resumed' if self.resuming else 'new'})", flush=True)

        threading.Thread(target=self._pump_pty, daemon=True).start()
        threading.Thread(target=self._tail_transcript, daemon=True).start()
        # Backfill: a resumed session that has a transcript but no title (e.g. it
        # only ever reached prompt 1, so the old start-of-turn naming missed it)
        # gets named now from its existing content.
        if self.resuming and not self.title:
            threading.Thread(target=self._regenerate_name, daemon=True).start()

    def _write_hook_settings(self):
        """Generate a settings file that POSTs every hook event's stdin JSON to
        our /hook endpoint, tagged with this session's cid so the manager can
        route it. Self-contained — passed via `claude --settings`, so it never
        touches the user's ~/.claude or project settings."""
        post = (f"curl -sS -m 2 -X POST "
                f"'http://127.0.0.1:{PORT}/hook?t={TOKEN}&cid={self.cid}' "
                f"--data-binary @- >/dev/null 2>&1 || true")
        one = [{"hooks": [{"type": "command", "command": post}]}]
        star = [{"matcher": "*", "hooks": [{"type": "command", "command": post}]}]
        settings = {"hooks": {
            "SessionStart": one, "SessionEnd": one,
            "UserPromptSubmit": one, "Stop": one, "Notification": one,
            "PreToolUse": star, "PostToolUse": star,
        }}
        path = str(HERE / f".clawd-harness.hooks.{self.cid}.json")
        Path(path).write_text(json.dumps(settings))
        return path

    def _set_winsize(self, fd, rows, cols):
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def on_hook(self, obj):
        """Handle one hook callback (from claude via /hook) → update state, fan a
        slim event out to every client (menu badges), and trigger AI naming."""
        ev = obj.get("hook_event_name", "?")
        self.last_active = time.time()
        if ev == "UserPromptSubmit":
            self.prompted_at = time.time()   # the human prompted — the tab age anchor
        self.hook_count += 1
        # Claude rotates its transcript file on compaction/resume. Main-session
        # lifecycle hooks report the live transcript_path + session_id, so follow
        # them — otherwise the tail strands on the pre-rotation file and the
        # transcript view silently freezes. (Subagents use SubagentStop, not these.)
        if ev in ("UserPromptSubmit", "Stop", "SessionStart"):
            self._follow_session(obj)
        # Any hook other than Notification means the turn is making progress
        # again (the prompt, if any, got answered) → clear the blocked flag.
        if ev != "Notification":
            self.waiting = False
        data = {}
        if ev == "UserPromptSubmit":
            self.busy = True
            prompt = obj.get("prompt", "")
            data = {"prompt": prompt}
            self.last_prompt = prompt
            self.hooks_at_prompt = self.hook_count
            self._on_prompt(prompt)
            # If this plan already looks drained, the CLI may answer with its
            # limit line and never run the turn (no Stop ever comes). Hand the
            # verdict to the rescue path — it confirms against the live
            # endpoint and, on a true bounce, hands off + redelivers.
            acct = self.manager.accounts.get(self.account)
            if not self.ceremony and acct and (acct.broken
                         or (acct.usage or {}).get("pct", 0) >= SUB_EXHAUSTED):
                threading.Thread(target=self.manager.rescue_bounced_prompt,
                                 args=(self, prompt, self.hook_count),
                                 daemon=True).start()
        elif ev == "PreToolUse":
            self.busy = True
            self.last_tool = obj.get("tool_name")
            # These two tools render a blocking interactive prompt in the TUI and
            # don't emit a Notification — so flag waiting here (the matching
            # PostToolUse, like any non-Notification hook above, clears it).
            if obj.get("tool_name") in ("AskUserQuestion", "ExitPlanMode"):
                self.waiting = True
            data = {"tool": obj.get("tool_name")}
        elif ev == "PostToolUse":
            self.busy = True
            data = {"tool": obj.get("tool_name"),
                    "duration_ms": obj.get("duration_ms")}
        elif ev == "Stop":
            self.busy = False
            self.last_tool = None
            data = {"last": obj.get("last_assistant_message", "")}
            # Turn complete → the transcript now has a real exchange. Name it if
            # it's still unnamed (so even a 1-prompt session gets a title), and
            # re-name at the 1/3/6/9/… milestones to sharpen as it grows.
            if (not self.title) or name_at_prompt(self.prompt_count):
                threading.Thread(target=self._regenerate_name, daemon=True).start()
            # The digest is volatile — refresh it every turn (not just at the
            # naming milestones) so live session state stays current for the
            # controller / dashboard. Cheap, async, in-memory only.
            threading.Thread(target=self._regenerate_digest, daemon=True).start()
            # Turn over + idle = the safe moment to move this session off a
            # drained plan (no-ops fast in the common case).
            threading.Thread(target=self.manager.maybe_handoff, args=(self,),
                             daemon=True).start()
        elif ev == "Notification":
            # Fires both for "needs your permission / input" (mid-turn, busy) and
            # for a 60s-idle nudge (turn already Stopped, not busy). Only the
            # former is a real block — gate on busy so an idle session doesn't
            # masquerade as waiting-for-you.
            if self.busy:
                self.waiting = True
            data = {"message": obj.get("message", "")}
        elif ev == "SessionStart":
            self.busy = False
            self._started_evt.set()
            if obj.get("model"):        # earliest model signal, before any reply
                self.model = obj["model"]
            data = {"source": obj.get("source"), "model": obj.get("model")}
        elif ev == "SessionEnd":
            data = {"reason": obj.get("reason")}
        self.manager.broadcast_all({"type": "hook", "cid": self.cid, "event": ev,
                                    "busy": self.busy, "waiting": self.waiting,
                                    "tool": self.last_tool, "data": data})
        self.manager.broadcast_sessions()

    def _on_prompt(self, prompt):
        """Count the prompt + remember a fallback first prompt. On the *first*
        prompt we name immediately from the prompt text itself (don't wait for
        the turn to finish): UserPromptSubmit fires before claude has written
        the transcript, so we can't read it yet — but the prompt is right here,
        and it's enough to label the session the instant it's created. The Stop
        milestones (1, then every 3) re-name from the full transcript to sharpen."""
        self.prompt_count += 1
        if not self.first_prompt and prompt:
            self.first_prompt = prompt.strip().splitlines()[0][:200]
        self.manager.save_registry()
        if self.prompt_count == 1 and prompt.strip():
            seed = ("User: " + prompt.strip())[:3500]
            threading.Thread(target=self._regenerate_name,
                             kwargs={"seed_text": seed}, daemon=True).start()

    def _regenerate_name(self, seed_text=""):
        text = seed_text or self._transcript_text_for_naming()
        if not text:
            return
        title, desc, tab = generate_name(text)
        if title:
            self.title = title[:60]
            self.desc = (desc or "")[:120]
            self.tab = (tab or "")[:24]
            print(f"[name {self.cid[:8]}] {self.title!r} — {self.desc!r}", flush=True)
            self.manager.save_registry()
            self.manager.broadcast_sessions()

    def _regenerate_digest(self):
        """Refresh the volatile 'what's happening now' digest from the transcript.
        Companion to _regenerate_name, but fired on *every* Stop (naming fires only
        at milestones) since that's when the turn's outcome is freshest. Held in
        memory only — derived/ephemeral state, regenerated next turn (no registry).
        See docs/CONTROLLER.md."""
        text = self._transcript_text_for_naming()
        if not text:
            return
        digest, blocked_on = generate_digest(text)
        if digest is None:                          # naming off, or call failed
            return
        self.digest = (digest or "")[:140]
        self.blocked_on = ((blocked_on or "").strip() or None)
        if self.blocked_on:
            self.blocked_on = self.blocked_on[:140]
        self.manager.broadcast_sessions()

    def _transcript_text_for_naming(self, cap=3500):
        path = self.transcript_path or self._find_transcript()
        if not path:
            return ""
        try:
            lines = open(path).read().splitlines()
        except OSError:
            return ""
        chunks = []
        for ln in lines:
            ev = self._slim_event(ln)
            if not ev:
                continue
            if ev.get("role") == "user" and ev.get("text"):
                chunks.append("User: " + ev["text"])
            elif ev.get("role") == "assistant" and ev.get("text"):
                chunks.append("Claude: " + ev["text"])
        text = "\n".join(chunks)
        if len(text) <= cap:
            return text
        # Keep BOTH the session's founding context (the head — what it was set up
        # to do) and the most recent activity (the tail), so a late tangent (a
        # one-off question) can't evict the original objective from the namer's
        # window. A pure tail-truncation (text[-cap:]) used to drop the opening
        # task once a session got long, making the name chase whatever was latest.
        head = int(cap * 0.45)
        tail = cap - head - 3                        # 3 for the "\n…\n" elision marker
        return text[:head] + "\n…\n" + text[-tail:]

    def resize(self, cols, rows):
        if self.master_fd is not None and cols and rows:
            try:
                self._set_winsize(self.master_fd, int(rows), int(cols))
            except OSError:
                pass

    # -- viewer size policy -----------------------------------------------------
    # A resize frame is a size CLAIM, not a command. `claim:true` (a deliberate
    # act on that device: opening the tty view, resizing the window) takes
    # ownership; a maintenance resize (reconnect re-sync, footer refit) only
    # applies if the sender already owns the PTY — so a background desktop's
    # watchdog can't yank the size out from under the phone you're driving.
    # Typing/sending from a sized viewer also claims (bump_owner): the device
    # being driven is the one whose geometry the TUI should fit.
    def claim_resize(self, client, cols, rows, claim=False):
        try:
            cols, rows = int(cols or 0), int(rows or 0)
        except (TypeError, ValueError):
            return
        if not cols or not rows:                 # 0×0 = release (left the view / hidden)
            client.tty_size = None
            if self.tty_owner is client:
                self._owner_fallback()
            return
        client.tty_size = (cols, rows)
        client.tty_ts = time.time()
        owner = self.tty_owner
        with self.clients_lock:
            owner_live = owner is not None and not owner.dead and owner in self.clients
        if claim or owner is client or not owner_live:
            self._set_owner(client)

    def bump_owner(self, client):
        if client.tty_size and client.cid == self.cid and self.tty_owner is not client:
            client.tty_ts = time.time()
            self._set_owner(client)

    def _set_owner(self, client):
        self.tty_owner = client
        self._apply_size(*client.tty_size)

    def _owner_fallback(self):
        """Owner left: hand the PTY to the most recently sized remaining viewer."""
        with self.clients_lock:
            cands = [c for c in self.clients if c.tty_size and not c.dead]
        self.tty_owner = max(cands, key=lambda c: c.tty_ts, default=None)
        if self.tty_owner:
            self._apply_size(*self.tty_owner.tty_size)

    def _apply_size(self, cols, rows):
        if (cols, rows) == (self.tty_cols, self.tty_rows):
            return                               # same size → no SIGWINCH, no repaint
        if cols != self.tty_cols and self.alive:
            # Bytes painted for another WIDTH can never render right in a
            # replay — they rewrap into shredded fragments in the scrollback
            # of whoever attaches next (the mobile scroll-up-garbage bug).
            # Drop them; claude's SIGWINCH repaint refills a clean frame at
            # the new width. Rows-only changes (phone keyboard, footer refit)
            # keep the ring — wrapping is unaffected. Skip if claude exited:
            # no repaint would follow, and a mangled last screen beats a
            # blank one.
            with self.ring_lock:
                del self.ring[:]
        self.tty_cols, self.tty_rows = cols, rows
        self.resize(cols, rows)
        self._to_subscribers_json({"type": "ttySize", "cid": self.cid,
                                   "cols": cols, "rows": rows})

    # -- write channel ---------------------------------------------------------
    def write(self, data: bytes):
        """Raw keystrokes -> PTY."""
        if self.master_fd is None:
            return
        try:
            os.write(self.master_fd, data)
        except OSError:
            pass

    def send_message(self, text: str):
        """High-level: type a message, let the paste settle, then submit (CR)."""
        self.prompted_at = time.time()   # belt-and-braces: a bounced prompt fires no hook
        pre_hooks = self.hook_count
        self.write(text.encode("utf-8"))
        # Short one-liners only need to clear the 0.6s burst cliff; big or
        # multi-line pastes take longer to finalize, so keep the full settle.
        big = len(text) > 280 or text.count("\n") >= 1
        time.sleep(SEND_SETTLE if big else SEND_SETTLE_MIN)
        self.write(b"\r")
        threading.Thread(target=self._send_watchdog, args=(text, pre_hooks),
                         daemon=True).start()

    def _send_watchdog(self, text, pre_hooks):
        """We just delivered a message; a healthy CLI answers with a
        UserPromptSubmit hook in ~1-2s. Total hook silence means the prompt
        bounced (walled plan — the CLI's limit reply emits NO hook, so
        neither the hook-triggered rescue nor busy-based sweeps see it) or
        the CLI is wedged. Log the stripped PTY tail as evidence, then let
        rescue_bounced_prompt confirm against the live endpoint — on a
        healthy pool it's a no-op, so a wedged-but-unwalled CLI is left for
        the wedge runbook, not respawned blind."""
        time.sleep(SEND_WATCHDOG)
        if not self.alive or self.hook_count != pre_hooks or self.ceremony:
            return                               # (ceremony sends — e.g. the auto-typed
                                                 #  /login — fire no hooks by design)
        with self.ring_lock:
            raw = bytes(self.ring[-4000:])
        tail = _PTY_ANSI_RE.sub(b"", raw).decode("utf-8", "ignore")
        tail = re.sub(r"\s+", " ", tail)[-300:]
        print(f"[session {self.cid[:8]}] send got NO hook in "
              f"{SEND_WATCHDOG:.0f}s on {self.account} — suspecting a walled "
              f"plan; pty tail: {tail!r}", flush=True)
        self.manager.rescue_bounced_prompt(self, text, pre_hooks)

    # -- read channel: raw PTY bytes -> subscribed clients ---------------------
    def _pump_pty(self):
        while True:
            try:
                r, _, _ = select.select([self.master_fd], [], [], 0.5)
            except (OSError, ValueError):
                break
            if not r:
                continue
            try:
                chunk = os.read(self.master_fd, 65536)
            except OSError:
                chunk = b""
            if not chunk:
                break
            with self.ring_lock:
                self.ring.extend(chunk)
                if len(self.ring) > RING_MAX:
                    del self.ring[:-RING_MAX]
            self._to_subscribers_bytes(chunk)
            self._scan_for_limit(chunk)
            if self._onboard_deadline:
                self._scan_for_onboarding(chunk)
        self.alive = False
        print(f"[session {self.cid[:8]}] PTY closed / claude exited", flush=True)
        # An account handoff replaces this object under the same cid — the
        # dying child's exit must not paint "session ended" over its successor.
        if self.manager.sessions.get(self.cid) is self:
            self.manager.broadcast_all({"type": "exit", "cid": self.cid})
            self.manager.broadcast_sessions()

    def _scan_for_limit(self, chunk):
        """Watch the raw PTY stream for the CLI's own limit banner — the
        zero-lag 'this pool just walled' signal (the eaten turn's hooks go
        silent and the next usage poll is minutes out). On a match, hand the
        verdict to rescue_limit_wall, which CONFIRMS against the live usage
        endpoint before moving anything — so echoed text (a session merely
        reading a file that quotes the banner) can never cause a handoff on a
        healthy pool. This is the one sanctioned exception to 'never parse
        the terminal's weird text': a needle match, not a parse."""
        if self.ceremony:
            return                               # sign-in ceremony: never rescued
        text = _PTY_ANSI_RE.sub(b"", chunk).decode("utf-8", "ignore")
        text = re.sub(r"\s+", " ", text)
        tail = (self._limit_tail + " " + text)[-800:]
        self._limit_tail = tail[-120:]           # keep enough to bridge a chunk split
        if not _LIMIT_BANNER_RE.search(tail):
            return
        self._limit_tail = ""                    # don't re-match this banner from the tail
        now = time.time()
        if now - self._limit_seen_at < BOUNCE_COOLDOWN:
            return
        self._limit_seen_at = now
        print(f"[session {self.cid[:8]}] limit banner in the PTY on "
              f"{self.account} — confirming against the endpoint", flush=True)
        threading.Thread(target=self.manager.rescue_limit_wall, args=(self,),
                         daemon=True).start()

    def _scan_for_onboarding(self, chunk):
        """First-launch tripwire: claude paints the theme picker only when
        its config dir says onboarding never finished — on a login-holding
        dir that is ALWAYS the half-finished-ceremony ambush (see
        _ensure_onboarded, which prevents every known case at spawn; this
        catches one that paints anyway). On a match, rescue_onboarding seeds
        the flag and respawns the session past the screen. Armed only for
        the first ONBOARD_SCAN_WINDOW seconds of a FRESH launch: a resume
        repaints recent conversation, so a session that QUOTED the picker
        text (this repo's own sources do) would false-match on every
        resume — and resumes skip onboarding anyway, so there is nothing
        real for the scan to catch there."""
        if time.time() >= self._onboard_deadline:
            self._onboard_deadline = 0.0         # window over — stop scanning entirely
            self._onboard_tail = ""
            return
        text = _PTY_ANSI_RE.sub(b"", chunk).decode("utf-8", "ignore")
        text = re.sub(r"\s+", " ", text)
        # Concatenate WITHOUT a joiner (unlike the limit scan): a chunk split
        # mid-word must still match, and a spurious word-join can't fabricate
        # this needle's full sentence.
        tail = (self._onboard_tail + text)[-400:]
        self._onboard_tail = tail[-120:]         # keep enough to bridge a chunk split
        if not _ONBOARDING_RE.search(tail):
            return
        self._onboard_deadline = 0.0             # one shot per session object
        self._onboard_tail = ""
        print(f"[session {self.cid[:8]}] onboarding/theme screen in the PTY "
              f"on {self.account} — rescuing", flush=True)
        threading.Thread(target=self.manager.rescue_onboarding, args=(self,),
                         daemon=True).start()

    # -- read channel: transcript JSONL -> structured events -------------------
    def _find_transcript(self):
        # Locate by session-id across all project dirs (robust to path
        # encoding), under THIS session's account config dir — a session
        # spawned under a non-default account writes its transcript there.
        base = self.config_dir or os.path.expanduser("~/.claude")
        hits = glob.glob(f"{base}/projects/*/{self.session_id}.jsonl")
        return hits[0] if hits else None

    def _follow_session(self, obj):
        """Track the live transcript file + session id from a hook payload. A
        compaction (or resume) rotates claude's session file mid-run; following
        it keeps the tail on the live file and makes a daemon restart resume the
        current session instead of a stale pre-rotation one."""
        tpath = obj.get("transcript_path")
        if tpath:
            self._live_transcript = os.path.expanduser(tpath)
        sid = obj.get("session_id")
        if sid and sid != self.session_id:
            print(f"[session {self.cid[:8]}] rotated {self.session_id} -> {sid}",
                  flush=True)
            self.session_id = sid
            self.manager.save_registry()         # so the next restart resumes this one

    def _tail_transcript(self):
        # Wait (indefinitely, while the session lives) for a file to tail; claude
        # creates it on the first turn, which may be long after launch.
        target = None
        while self.alive and not target:
            target = self._live_transcript or self._find_transcript()
            if not target:
                time.sleep(0.25)
        # Outer loop reopens whichever file is current: when a compaction/resume
        # rotates the session, _follow_session repoints _live_transcript and we
        # switch, streaming the new file from the top so the client catches up
        # across the rotation boundary.
        announced = None
        while self.alive and target:
            self.transcript_path = target
            try:
                f = open(target, "r")
            except OSError:
                # claude reports transcript_path on SessionStart *before* it
                # creates the file (it's written lazily on the first turn), so
                # retry quietly — printing here busy-loops the log until the
                # file appears. Only announce a successful attach (below).
                time.sleep(0.25)
                target = self._live_transcript or target
                continue
            if target != announced:                   # one line per real (re)attach
                print(f"[transcript {self.cid[:8]}] tailing {target}", flush=True)
                announced = target
            with f:
                buf = ""
                while self.alive:
                    if self._live_transcript and self._live_transcript != target:
                        target = self._live_transcript       # rotated → reopen new file
                        break
                    line = f.readline()
                    if not line:
                        time.sleep(0.2)
                        continue
                    buf += line
                    if not buf.endswith("\n"):
                        continue                     # partial line; wait for the rest
                    raw, buf = buf, ""
                    ev = self._slim_event(raw.strip())
                    if ev:
                        self._to_subscribers_json(
                            {"type": "transcript", "cid": self.cid, "event": ev})

    def _slim_event(self, line: str):
        """Reduce a raw transcript line to the bits a controller cares about.
        Event shapes mirror clawd-tg-claude/bot.py's stream-json handling."""
        if not line:
            return None
        try:
            obj = json.loads(line)
        except Exception:
            return None
        t = obj.get("type")
        if t == "user":
            content = (obj.get("message") or {}).get("content")
            text = content if isinstance(content, str) else _collect_text(content)
            if text:
                # local slash-command artifacts → clean events, not raw XML tags
                m = re.search(r"<command-name>([^<]*)</command-name>", text)
                if m:
                    name = m.group(1).strip()
                    am = re.search(r"<command-args>([^<]*)</command-args>", text)
                    args = am.group(1).strip() if am else ""
                    return {"role": "command", "text": (name + " " + args).strip()}
                m = re.search(r"<local-command-stdout>([\s\S]*?)</local-command-stdout>", text)
                if m:
                    out = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", m.group(1))  # strip ANSI
                    out = re.sub(r"<[^>]+>", "", out).strip()
                    return {"role": "system", "text": out} if out else None
                clean = _strip_noise(text).strip()
                if not clean:
                    return None
                return {"role": "user", "text": clean}
            # tool_result blocks arrive as user messages too
            tr = _collect_tool_results(content)
            if tr:
                return {"role": "tool_result", "results": tr}
            return None
        if t == "assistant":
            msg = obj.get("message") or {}
            # Side effect: keep the splash-card hints fresh. Assistant lines
            # carry the answering model and a usage block whose input-side sum
            # is the turn's whole context — the "how full is the window" number.
            # ("<synthetic>" tags CLI-generated error text, not a real model.)
            if msg.get("model") and "<" not in msg["model"]:
                self.model = msg["model"]
            u = msg.get("usage") or {}
            tok = sum(u.get(k) or 0 for k in
                      ("input_tokens", "cache_read_input_tokens",
                       "cache_creation_input_tokens"))
            if tok:
                self.ctx_tokens = tok
            content = msg.get("content") or []
            text = _collect_text(content)
            tools = _collect_tool_uses(content)
            out = {"role": "assistant"}
            if text:
                out["text"] = text
            if tools:
                out["tools"] = tools
            return out if (text or tools) else None
        if t == "attachment":
            # A message sent while claude is busy never gets a `type:"user"`
            # line — the TUI queues it (`queue-operation` enqueue/remove) and
            # the only record carrying its text is this queued_command
            # attachment, written when the turn actually consumes it. Emit the
            # user event here or the client's "⏳ queued" box never lands.
            att = obj.get("attachment") or {}
            if att.get("type") == "queued_command":
                clean = _strip_noise(att.get("prompt") or "").strip()
                if clean:
                    return {"role": "user", "text": clean}
            return None
        if t == "result":
            return {"role": "result",
                    "subtype": obj.get("subtype"),
                    "is_error": obj.get("is_error"),
                    "duration_ms": obj.get("duration_ms"),
                    "usage": obj.get("usage")}
        return None

    # -- subscriber registry / streaming --------------------------------------
    def subscribe(self, client):
        """Attach a client to this session's live stream and catch it up:
        a hello, recent screen bytes, and the structured history. The hello
        goes FIRST so the client knows which cid the bytes that follow belong
        to — it gates painting on that, which is what keeps a stale/mis-routed
        subscription from leaking another session's output into its terminal."""
        with self.clients_lock:
            self.clients.add(client)
        client.send_json({"type": "hello",
                          "cid": self.cid, "pid": self.pid,
                          "account": self.account,
                          "sessionId": self.session_id,
                          "title": self.title or self._fallback_title(),
                          "workdir": self.workdir(),
                          "busy": self.busy, "waiting": self.waiting, "tool": self.last_tool,
                          "cols": self.tty_cols, "rows": self.tty_rows})
        with self.ring_lock:
            snapshot = bytes(self.ring)
        snapshot = OSC52_SCRUB_RE.sub(b"", snapshot)
        if len(snapshot) < SEED_RING_MAX:
            try:
                seed = self._history_seed_bytes()
            except Exception:
                seed = b""            # seeding is best-effort — never block the replay
            if seed:
                client.send_bytes(seed)
        if snapshot:
            client.send_bytes(snapshot)
        self._replay_history(client)

    def _replay_history(self, client, limit=150):
        """Send recent transcript events so a fresh subscriber's structured view
        isn't empty — important now that mobile defaults to the transcript."""
        path = self.transcript_path or self._find_transcript()
        if not path:
            return
        try:
            lines = open(path).read().splitlines()
        except OSError:
            return
        events = [e for e in (self._slim_event(l) for l in lines) if e]
        for ev in events[-limit:]:
            client.send_json({"type": "transcript", "cid": self.cid,
                              "event": ev, "history": True})

    def _history_seed_bytes(self, limit=60):
        """Rendered-transcript scrollback, sent before a shallow ring replay.

        Raw PTY bytes can't cross a width change — they're painted for one
        geometry, and _apply_size fences the ring exactly so we never replay
        them into another (the mobile shredded-scrollback bug). The cost was
        that a phone attaching to a session got a near-empty replay: nothing
        above the live screen, so scroll-up found nothing to scroll to.
        Instead of replaying old-geometry bytes, render the conversation from
        the transcript as plain SOFT-WRAPPED text — long lines carry no
        newlines, so xterm wraps them to the client's real width and re-wraps
        them on any future resize. A screenful of newlines then pushes the
        whole block above the visible rows: the live repaint that follows uses
        relative cursor moves, which can never reach scrollback, so it can't
        chew into the seeded history."""
        path = self.transcript_path or self._find_transcript()
        if not path:
            return b""
        try:
            lines = open(path).read().splitlines()
        except OSError:
            return b""
        DIM, RST, MARK = "\x1b[2m", "\x1b[0m", "\x1b[38;5;179m"
        scrub = lambda s: SEED_SCRUB_RE.sub("", s)
        parts = []                    # (kind, text); runs of tool-only chips coalesce below
        for ev in (self._slim_event(l) for l in lines):
            if not ev:
                continue
            role, text = ev.get("role"), scrub(ev.get("text") or "").strip()
            if role == "user" and text:
                parts.append(("text", MARK + "❯ " + RST + text))
            elif role == "command" and text:
                parts.append(("text", DIM + "❯ " + text + RST))
            elif role == "assistant":
                block = [DIM + "· " + scrub(t["name"]) + RST
                         for t in (ev.get("tools") or []) if t.get("name")]
                if text:
                    parts.append(("text", "\n".join(block + [text])))
                elif block:
                    # a tool-only event: glue onto a preceding tool-only run so
                    # long tool sequences read as one compact chip column
                    if parts and parts[-1][0] == "tools":
                        parts[-1] = ("tools", parts[-1][1] + "\n" + "\n".join(block))
                    else:
                        parts.append(("tools", "\n".join(block)))
        parts = [t for _, t in parts[-limit:]]
        while parts and sum(map(len, parts)) > SEED_CHARS:
            parts.pop(0)
        if not parts:
            return b""
        out = (DIM + "── conversation so far (rendered from the transcript; "
               "live screen below) ──" + RST + "\n\n"
               + "\n\n".join(parts) + "\n\n" + DIM + "── live ──" + RST)
        pad = "\r\n" * max(8, min(60, self.tty_rows or ROWS))
        return (out.replace("\n", "\r\n") + pad).encode("utf-8", "ignore")

    def unsubscribe(self, client):
        with self.clients_lock:
            self.clients.discard(client)
        if self.tty_owner is client:             # size owner left → next viewer takes over
            self._owner_fallback()

    def _to_subscribers_bytes(self, data: bytes):
        with self.clients_lock:
            targets = list(self.clients)
        for c in targets:
            c.send_bytes(data)

    def _to_subscribers_json(self, obj):
        with self.clients_lock:
            targets = list(self.clients)
        for c in targets:
            c.send_json(obj)

    def shutdown(self):
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.send_signal(signal.SIGTERM)
        except Exception:
            pass

    def kill(self):
        """Terminate for good (menu close): SIGTERM and drop subscribers."""
        self.alive = False
        self.shutdown()
        if self.settings_path:                   # its per-session hooks file is now dead weight
            try:
                os.unlink(self.settings_path)
            except OSError:
                pass


# ── session manager: registry of Projects + ClaudeSessions ────────────────────
class SessionManager:
    def __init__(self):
        self.projects = {}                       # pid -> Project
        self.sessions = {}                       # cid -> ClaudeSession
        self.accounts = {}                       # name -> Account (subscriptions)
        self.active_account = "default"          # new sessions spawn under this
        self.last_switch_at = 0.0                # debounce anchor for auto-switch
        self._poll_now = threading.Event()       # kick the usage poller early
        self.lock = threading.RLock()
        self.all_clients = set()                 # every connected browser
        self.clients_lock = threading.Lock()
        # Graceful self-restart: when a boot-time file (server.py / .env) changes,
        # we flag a pending restart, surface it in every browser, and wait until
        # *all* sessions are idle before tearing down — so no in-flight turn dies.
        self.restart_pending = False
        self.restart_reason = ""
        self._restarting = False
        self._restart_lock = threading.Lock()

    # -- graceful self-restart -------------------------------------------------
    def busy_count(self):
        with self.lock:
            return sum(1 for s in self.sessions.values() if s.busy and s.alive)

    def request_restart(self, reason):
        """Flag that a restart is needed; it fires once all sessions are idle.
        Idempotent — repeated calls just keep the pending state."""
        with self._restart_lock:
            if self._restarting:
                return
            first = not self.restart_pending
            self.restart_pending = True
            self.restart_reason = reason
        if first:
            print(f"[restart] pending — {reason} (waiting for all sessions idle)",
                  flush=True)
        self.broadcast_restart()
        self._maybe_restart()

    def cancel_restart(self):
        with self._restart_lock:
            if self._restarting or not self.restart_pending:
                return
            self.restart_pending = False
            self.restart_reason = ""
        print("[restart] cancelled by user", flush=True)
        self.broadcast_restart()

    def _maybe_restart(self):
        """Fire the restart iff one is pending and nothing is mid-turn."""
        with self._restart_lock:
            if self._restarting or not self.restart_pending or self.busy_count():
                return
            self._restarting = True
        threading.Thread(target=self._execute_restart, daemon=True).start()

    def _execute_restart(self):
        print("[restart] all idle → tearing down + exiting (launchd relaunches)",
              flush=True)
        self.broadcast_all({"type": "restart", "state": "go"})
        time.sleep(0.5)                          # let the 'go' frame flush to clients
        self.shutdown()                          # SIGTERM the claude children cleanly
        time.sleep(0.5)
        os._exit(0)                              # KeepAlive=true → launchd respawns us

    def restart_state(self):
        return {"type": "restart", "pending": self.restart_pending,
                "reason": self.restart_reason, "busy": self.busy_count()}

    def broadcast_restart(self):
        self.broadcast_all(self.restart_state())

    # -- startup / persistence -------------------------------------------------
    def load(self):
        PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        reg = self._read_registry()
        for e in reg.get("projects", []):
            kind = e.get("kind", "gh")
            missing = not e.get("path") or not os.path.isdir(e["path"])
            if missing and kind != "local":
                continue                         # repo dir gone — drop the entry
            if missing and not e.get("path"):
                continue                         # local with no path at all — unusable
            # A local whose folder is missing at boot is KEPT as an error entry
            # (a network volume may simply not be mounted yet) — the reconcile
            # liveness pass heals it when the path returns; only the explicit
            # removeProject verb forgets a local.
            # Backfill the origin URL when the registry stored an empty one (legacy
            # entries adopted before this backfill existed). Every machine reporting
            # its canonical repo URL is what lets the fleet view merge the same repo
            # across boxes into one card instead of splitting name-only vs. URL keys.
            # Locals are exempt: private folders never report a remote.
            repo_url = ("" if kind == "local"
                        else e.get("repo_url", "") or _git_remote_url(e["path"]))
            p = Project(pid=e.get("pid") or str(uuid.uuid4()),
                        name=e.get("name") or os.path.basename(e["path"]),
                        path=e["path"], repo_url=repo_url,
                        status=e.get("status", "ready") if e.get("status") != "cloning" else "ready",
                        created=e.get("created", 0.0), kind=kind,
                        emoji=e.get("emoji", ""), emoji_at=e.get("emoji_at", 0.0))
            if missing:
                p.status, p.error, p.error_at = "error", "folder missing", time.time()
            self.projects[p.pid] = p
        self._discover_projects()                # adopt repos dropped into projects/ by hand
        self._ensure_self_project()              # always offer the harness itself, pinned
        # the pinned self-project is re-injected (not stored in "projects"), so
        # its badge rides a dedicated registry key — else it'd re-roll every boot
        se = reg.get("self_emoji") or {}
        self.projects[SELF_PID].emoji = se.get("emoji", "")
        self.projects[SELF_PID].emoji_at = se.get("emoji_at", 0.0)

        for e in reg.get("accounts", []):
            if not e.get("name"):
                continue
            a = Account(name=e["name"], config_dir=e.get("config_dir", ""),
                        email=e.get("email", ""), org=e.get("org", ""),
                        org_name=e.get("org_name", ""), tier=e.get("tier", ""),
                        ready=e.get("ready", False),
                        created=e.get("created", 0.0), usage=e.get("usage"))
            if not a.org_name:
                # registries predating org_name (and BROKEN logins, whose
                # token-bound profile fetch can never run) still deserve a
                # real title — .claude.json is a fine guess until the profile
                # endpoint overrides it
                em, org, oname = _account_identity(a.config_dir)
                a.email, a.org = a.email or em, a.org or org
                a.org_name = oname
            self.accounts[a.name] = a
        self._ensure_default_account()
        for a in self.accounts.values():         # boot migration: shared transcripts
            if a.config_dir:
                _share_projects(a.config_dir)
        self.active_account = reg.get("active_account") or "default"
        act = self.accounts.get(self.active_account)
        if not act or not act.ready:             # unknown / never-signed-in / removed
            self.active_account = next(
                (a.name for a in self._ordered_accounts() if a.ready), "default")
        self.last_switch_at = reg.get("last_switch_at", 0.0)

        known = set(self.projects)
        for e in reg.get("sessions", []):
            pid = e.get("pid")
            if pid not in known:
                continue                         # orphaned session — its project is gone
            proj = self.projects.get(pid)
            if proj is not None and proj.kind == "local" and proj.status == "error":
                # local folder missing at boot: the project entry survives (it
                # heals when the path returns) but a session can't — Popen on a
                # dead cwd would crash the boot
                print(f"[session {(e.get('cid') or '')[:8]}] local folder "
                      f"missing ({proj.path}) — session not resumed", flush=True)
                continue
            cfg = e.get("config_dir", "")
            name = e.get("account", "default")
            if cfg and not os.path.isdir(cfg):
                print(f"[session {(e.get('cid') or '')[:8]}] account dir gone "
                      f"({cfg}) — session will start fresh and logged out; "
                      "restore the dir to resume", flush=True)
            sid = e.get("session_id")
            # The RESUME gate, mirror of the spawn gate in create_session():
            # never reopen a session onto a login screen. A session recorded
            # under an account that is signed out (or gone from the roster,
            # e.g. a removed `default`) is landed on the signed-in account
            # with the most headroom, its transcript linked across so
            # --resume still finds it. A pending sign-in ceremony (roster
            # entry exists but not ready) is exempt — its login screen is
            # the whole point.
            acct_entry = self.accounts.get(name)
            rstate = _creds_state(cfg)
            if rstate == "unknown":
                # unreadable store ≠ signed out (root cause v3) — resume
                # under the recorded account; claude reads the keychain itself
                print(f"[session {(e.get('cid') or '')[:8]}] credential "
                      f"store for {name!r} unreadable — transient; resuming "
                      "under it anyway", flush=True)
            elif rstate == "absent" \
                    and not e.get("ceremony") \
                    and not (acct_entry and not acct_entry.ready):
                alts = sorted([a for a in self.accounts.values()
                               if a.ready and not a.broken],
                              key=lambda a: (a.usage or {}).get("pct", 100.0))
                alt = next((a for a in alts
                            if _creds_state(a.config_dir) != "absent"), None)
                if acct_entry:
                    acct_entry.broken = True
                if alt is not None:
                    print(f"[session {(e.get('cid') or '')[:8]}] account "
                          f"{name!r} is signed out — resuming under "
                          f"{alt.name}", flush=True)
                    if sid:
                        _link_transcript(sid, cfg, alt.config_dir)
                    name, cfg = alt.name, alt.config_dir
                else:
                    print(f"[session {(e.get('cid') or '')[:8]}] account "
                          f"{name!r} is signed out and NO plan is signed in — "
                          "this session opens Claude's login screen",
                          flush=True)
            resuming = bool(sid and _transcript_exists(sid, cfg))
            if sid and not resuming:
                # transcript gone (e.g. cleared history) — start it fresh instead
                # of resuming into nothing.
                sid = str(uuid.uuid4())
            s = ClaudeSession(
                self, cid=e.get("cid") or str(uuid.uuid4()), pid=pid,
                session_id=sid or str(uuid.uuid4()), resuming=resuming,
                title=e.get("title", ""), desc=e.get("desc", ""),
                tab=e.get("tab", ""),
                prompt_count=e.get("prompt_count", 0),
                first_prompt=e.get("first_prompt", ""),
                created=e.get("created", 0.0),
                last_active=e.get("last_active", 0.0),
                prompted_at=e.get("prompted_at", 0.0),
                account=name, config_dir=cfg,
                ceremony=e.get("ceremony", False),
                pinned=e.get("pinned", 0.0),
                model=e.get("model", ""), ctx_tokens=e.get("ctx_tokens", 0))
            self.sessions[s.cid] = s
            s.start()
        # No auto-created session: with zero projects there are legitimately zero
        # sessions, and the client lands on the projects page.
        self.save_registry()

    def _discover_projects(self):
        """Adopt any git repo under PROJECTS_DIR not already registered, so the
        project list mirrors what's on disk — a clone/create, or a repo dropped
        into projects/ by hand. Returns the number of newly adopted projects."""
        with self.lock:
            known_paths = {p.path for p in self.projects.values()}
        try:
            entries = sorted(os.listdir(PROJECTS_DIR))
        except OSError:
            return 0
        added = 0
        for name in entries:
            path = str(PROJECTS_DIR / name)
            # .git is a DIR for a clone but a FILE for a linked worktree — both
            # are adoptable (the code orchestrator parks selfdev worktrees here).
            if path in known_paths or not os.path.exists(os.path.join(path, ".git")):
                continue
            p = Project(pid=str(uuid.uuid4()), name=name, path=path,
                        repo_url=_git_remote_url(path), status="ready")
            with self.lock:
                self.projects[p.pid] = p
            added += 1
        return added

    def reconcile_projects(self):
        """Disk is the source of truth for the project list (there is no
        in-app "remove" — you delete a repo's folder yourself). Drop any ready
        project under PROJECTS_DIR whose folder has vanished (killing its now
        cwd-less sessions), then adopt any new repo dir. The pinned self-project
        and in-flight clones are left alone; a folder-less `error` entry (failed
        clone/create) is dropped after ERROR_LINGER, or healed to ready if a git
        repo appears at its path. Returns True if the set of projects changed.
        Cheap; runs on the watch loop so the list follows disk within ~1s for
        every open browser."""
        base = str(PROJECTS_DIR) + os.sep
        try:
            on_disk = {str(PROJECTS_DIR / n) for n in os.listdir(PROJECTS_DIR)}
            scan_ok = True
        except OSError as e:
            on_disk = set()
            scan_ok = False
            print(f"[reconcile] PROJECTS_DIR scan failed ({e}); "
                  "skipping drop pass this cycle", flush=True)
        with self.lock:
            ready = [(pid, p) for pid, p in self.projects.items()
                     if not p.pinned and p.status == "ready"
                     and p.path.startswith(base)]
            # A failed scan — or an *empty* listing while we still track ready
            # projects — is almost always a transient FS blip (a single bad
            # os.listdir once nuked every project here and killed all their
            # sessions). You never delete a dozen repos atomically between two
            # ~1s ticks, so refuse to drop EVERYTHING on one read: skip the drop
            # pass and let the next good scan reconcile. Discovery still runs.
            safe_to_drop = scan_ok and (on_disk or not ready)
            if ready and not safe_to_drop:
                print(f"[reconcile] on-disk scan empty but {len(ready)} ready "
                      "project(s) tracked → skipping drop pass (transient?)",
                      flush=True)
            gone = ([pid for pid, p in ready if p.path not in on_disk]
                    if safe_to_drop else [])
            # A failed clone/create leaves an `error` entry with no folder on
            # disk. The ready-only pass above never touches it, so it lingered
            # forever — and its registered path blocked re-cloning the same
            # repo. Drop it once the user has had a moment to read the error
            # (ERROR_LINGER); and if a git repo *appears* at its path (manual
            # clone, later retry), heal the entry to ready instead.
            now = time.time()
            errored = [(pid, p) for pid, p in self.projects.items()
                       if not p.pinned and p.status == "error"
                       and p.path.startswith(base)]
            gone += [pid for pid, p in errored
                     if scan_ok and p.path not in on_disk
                     and now - (p.error_at or p.created) > ERROR_LINGER]
            healed = [p for _, p in errored
                      if os.path.isdir(os.path.join(p.path, ".git"))]
        changed = False
        for p in healed:
            p.status, p.error = "ready", ""
            if not p.repo_url:
                p.repo_url = _git_remote_url(p.path)
            print(f"[project {p.name}] git repo appeared at its path → "
                  "error entry healed to ready", flush=True)
            changed = True
        for pid in gone:
            with self.lock:
                p = self.projects.pop(pid, None)
                cids = [c for c, s in self.sessions.items() if s.pid == pid]
            if not p:
                continue
            what = ("failed clone/create entry expired" if p.status == "error"
                    else "folder gone from disk")
            print(f"[project {p.name}] {what} → dropped", flush=True)
            for cid in cids:
                self.close(cid, _broadcast=False)
            changed = True
        if self._reconcile_locals():
            changed = True
        if self._discover_projects():
            changed = True
        if changed:
            self.save_registry()
        return changed

    def _reconcile_locals(self):
        """Liveness for kind="local" projects (invisible to the disk passes
        above — their paths live outside PROJECTS_DIR). Never auto-drops: a
        path continuously missing for LOCAL_GONE flips the card to error
        (blocks new sessions; existing ones keep running), and heals back to
        ready when it returns. Throttled to every LOCAL_SCAN_EVERY seconds —
        a stat on a dead network mount can hang, and this shares the ~1s
        watch loop. Returns True if any status changed."""
        now = time.time()
        if now - getattr(self, "_local_scan_at", 0.0) < LOCAL_SCAN_EVERY:
            return False
        self._local_scan_at = now
        with self.lock:
            locals_ = [p for p in self.projects.values() if p.kind == "local"]
        changed = False
        for p in locals_:
            try:
                present = os.path.isdir(p.path)
            except OSError:
                present = False
            if present:
                p.miss_since = 0.0
                if p.status == "error":
                    p.status, p.error = "ready", ""
                    print(f"[project {p.name}] local folder returned → healed",
                          flush=True)
                    changed = True
            else:
                if not p.miss_since:
                    p.miss_since = now
                elif p.status == "ready" and now - p.miss_since > LOCAL_GONE:
                    p.status, p.error, p.error_at = "error", "folder missing", now
                    print(f"[project {p.name}] local folder missing "
                          f"({p.path}) → error (kept; heals if it returns)",
                          flush=True)
                    changed = True
        return changed

    def emoji_sweep(self):
        """Assign / refresh each project's emoji identity badge (see
        EMOJI_SYS_PROMPT). Rides the ~1s watch loop but self-throttles to
        EMOJI_SCAN_EVERY and badges at most ONE project per pass in a
        background thread — cheap and steady, never a thundering herd. A
        too-young repo is skipped (with backoff) until it has matured enough
        to say something about itself; a badged project re-rolls every
        EMOJI_REFRESH_S so the badge tracks what the project becomes. No-op
        when the naming gateway is unconfigured."""
        if not (BANKR_API_KEY and BANKR_BASE_URL):
            return
        now = time.time()
        if (now - getattr(self, "_emoji_scan_at", 0.0) < EMOJI_SCAN_EVERY
                or getattr(self, "_emoji_busy", False)):
            return
        self._emoji_scan_at = now
        with self.lock:
            cand = next((p for p in self._ordered_projects()
                         if p.status == "ready" and now >= p.emoji_retry_at
                         and (not p.emoji or now - p.emoji_at > EMOJI_REFRESH_S)),
                        None)
            if not cand:
                return
            taken = [p.emoji for p in self.projects.values()
                     if p.emoji and p.pid != cand.pid]
            titles = [s.title for s in self._ordered()
                      if s.pid == cand.pid and s.title]
        self._emoji_busy = True
        threading.Thread(target=self._emoji_generate,
                         args=(cand, taken, titles), daemon=True).start()

    def _emoji_generate(self, project, taken, titles):
        try:
            ctx = _emoji_context(project, titles)
            code = generate_project_emoji(ctx, taken) if ctx else ""
            if not code:                         # immature repo or failed call — back off
                project.emoji_retry_at = time.time() + EMOJI_RETRY_S
                return
            if code != project.emoji:
                print(f"[emoji] {project.name} → {code}", flush=True)
            project.emoji, project.emoji_at = code, time.time()
            project.emoji_retry_at = 0.0
            self.save_registry()
            self.broadcast_projects()
        finally:
            self._emoji_busy = False

    def _ensure_self_project(self):
        """Always present the harness's own repo as a pinned project so you can
        open a session and live-edit the running app. Path = HERE (outside
        PROJECTS_DIR); re-injected every boot rather than persisted."""
        name = os.path.basename(str(HERE)) or "clawd-harness"
        self.projects[SELF_PID] = Project(
            pid=SELF_PID, name=name, path=str(HERE),
            repo_url=_git_remote_url(str(HERE)), status="ready", pinned=True)

    def _read_registry(self):
        try:
            raw = REGISTRY_FILE.read_text()
        except OSError:
            return {}
        try:
            data = json.loads(raw)
        except ValueError:
            # A corrupt registry is evidence AND possibly hand-recoverable —
            # move it aside so the next save_registry() can't silently pave
            # over it with an empty state.
            try:
                REGISTRY_FILE.rename(
                    REGISTRY_FILE.with_name(REGISTRY_FILE.name + ".corrupt"))
                print("[registry] sessions.json is corrupt — moved aside to "
                      ".corrupt, starting empty", flush=True)
            except OSError:
                pass
            return {}
        if isinstance(data, dict):
            return data
        return {}                                # legacy flat-list → ignored (fresh start)

    def save_registry(self):
        with self.lock:
            selfp = self.projects.get(SELF_PID)
            data = {"projects": [p.to_registry() for p in self._ordered_projects()
                                 if not p.pinned],   # self project is re-injected, not stored
                    "self_emoji": {"emoji": selfp.emoji,      # …except its badge
                                   "emoji_at": selfp.emoji_at} if selfp else {},
                    "sessions": [s.to_registry() for s in self._ordered()],
                    "accounts": [a.to_registry() for a in self._ordered_accounts()],
                    "active_account": self.active_account,
                    "last_switch_at": self.last_switch_at}
        # Atomic write: a crash/power-cut mid-write must never leave a
        # truncated registry — _read_registry would fall back to {} and the
        # very next save would pave the wreckage over with an empty state
        # (the "ALL my tabs rolled back" catastrophe).
        try:
            tmp = REGISTRY_FILE.with_name(REGISTRY_FILE.name + ".tmp")
            tmp.write_text(json.dumps(data, indent=2))
            os.replace(tmp, REGISTRY_FILE)
        except OSError:
            pass

    # -- accounts: subscription logins + usage-aware routing --------------------
    def _ensure_default_account(self):
        """Bootstrap only: on an EMPTY roster, the machine's plain ~/.claude
        login is injected as `default` (empty config_dir → sessions spawn
        exactly as before accounts existed). Once named accounts exist the
        user may remove `default` and it stays removed — typing `default`
        into the add box re-adopts it (no sign-in needed)."""
        if not self.accounts:
            em, org, oname = _account_identity("")
            self.accounts["default"] = Account(
                "default", "", email=em, org=org, org_name=oname, ready=True)

    def _ordered_accounts(self):
        return sorted(self.accounts.values(),
                      key=lambda a: (a.name != "default", a.created))

    def accounts_meta(self):
        with self.lock:
            active = self.active_account
            return {"type": "accounts", "active": active,
                    "auto": SUB_AUTOSWITCH,
                    "best": self._best_account(),   # what a new session would pick now
                    "lastSwitch": self.last_switch_at,
                    "accounts": [a.meta(active=(a.name == active))
                                 for a in self._ordered_accounts()]}

    def broadcast_accounts(self):
        self.broadcast_all(self.accounts_meta())

    def add_account(self, name):
        """Register a new subscription account and spawn its sign-in session —
        a normal claude in the self project with CLAUDE_CONFIG_DIR pointed at
        the fresh dir, so it walks the user through OAuth right in the harness
        UI. Re-invoking on a still-pending account just opens another sign-in
        session. Returns the login ClaudeSession (None if nothing to do)."""
        if not (name or "").strip():
            # The add flow is just a button now — no nickname field. Auto-pick
            # the first free folder label: it's invisible in the display
            # (identity headlines every card), it only names the config dir.
            # Skip existing dirs too: adopting a stale one would silently
            # inherit whatever login it last held.
            n = 2
            while (f"sub{n}" in self.accounts
                   or (ACCOUNTS_DIR / f"sub{n}").exists()):
                n += 1
            slug = f"sub{n}"
        else:
            slug = _safe_name(name).lower()
        if slug == "default":
            # Re-adopt the machine's plain ~/.claude login (e.g. after a
            # remove). Already signed in — no ceremony, no session. An
            # EXISTING default falls through instead: healthy → the generic
            # no-op below; BROKEN (the ~/.claude login itself got revoked) →
            # its re-sign-in ceremony spawns like any other account's.
            with self.lock:
                adopted = "default" not in self.accounts
                if adopted:
                    em, org, oname = _account_identity("")
                    self.accounts["default"] = Account(
                        "default", "", email=em, org=org, org_name=oname,
                        ready=True)
            if adopted:
                self.save_registry()
                self.broadcast_accounts()
                print("[account default] re-adopted the ~/.claude login",
                      flush=True)
                return None
        with self.lock:
            a = self.accounts.get(slug)
            if a and a.ready and not a.broken:
                return None                      # already signed in
            if not a:
                a = Account(slug, str(ACCOUNTS_DIR / slug))
                self.accounts[slug] = a
        try:
            if a.config_dir:                     # default's "" IS ~/.claude —
                _link_shared_paths(a.config_dir)  # nothing to link into itself
                _share_projects(a.config_dir)
        except Exception as e:
            print(f"[account {slug}] share links failed: {e}", flush=True)
        self.save_registry()
        self.broadcast_accounts()
        s = self.create_session(SELF_PID, account=slug, ceremony=True)
        if s:
            s.title = f"sign in · {slug}"
            s.desc = "complete the Claude OAuth login in this terminal"
            # Re-sign-in (the account's dir still holds stale/revoked
            # credentials): the CLI opens its NORMAL TUI, not the login
            # screen — the user shouldn't have to remember to type /login,
            # so type it for them once the TUI is up. A credential-less dir
            # is left alone: there the CLI boots straight into its own
            # login/onboarding flow and injected keystrokes would garble it.
            if _creds_state(a.config_dir) == "present":
                s.desc = ("/login is being typed for you — complete the "
                          "Claude OAuth in this terminal")
                def _autologin(sess=s):
                    if not sess._started_evt.wait(30):
                        return
                    time.sleep(2.0)
                    if sess.alive and self.sessions.get(sess.cid) is sess:
                        print(f"[account {slug}] stale login on file — "
                              "typing /login", flush=True)
                        sess.send_message("/login")
                threading.Thread(target=_autologin, daemon=True).start()
            self.broadcast_sessions()
        print(f"[account {slug}] created — sign-in session "
              f"{s.cid[:8] if s else 'FAILED'}", flush=True)
        return s

    def _route_key(self, a):
        """Sort key for 'which pool should we spend right now' (lower wins):
        COOL pools (< SUB_HOT on the most-constrained window — see the
        never-see-a-rate-limit comment block) before hot ones; among the cool,
        the soonest WEEKLY reset first (use-it-or-lose-it — see the SUB_*
        comment block); pct is the fallback when no reset is known, and the
        tie-break."""
        pct = (a.usage or {}).get("pct")
        pct = 100.0 if pct is None else pct
        reset = _weekly_reset(a.usage)
        return (pct >= SUB_HOT, reset is None, reset or 0.0, pct)

    def _best_account(self):
        """The ready account the router would spend RIGHT NOW, from cached
        usage that's fresh enough to trust (< 3×USAGE_TTL old): the
        non-exhausted pool whose weekly window resets soonest — NOT the most
        headroom (that's only the tie-break; see _route_key). None when no
        account qualifies — callers fall back to active_account. This is what
        routes each NEW session when auto-routing is on: per-spawn choice,
        not a sticky default."""
        now = time.time()
        with self.lock:
            fresh = [a for a in self.accounts.values()
                     if a.ready and not a.broken
                     and (a.usage or {}).get("pct") is not None
                     and now - (a.usage or {}).get("checkedAt", 0) < 3 * USAGE_TTL]
        if not fresh:
            return None
        return min(fresh, key=self._route_key).name

    def remove_account(self, name):
        """Drop an account from the routing roster. This logs NOTHING out —
        the config dir and Keychain credential stay (delete those yourself if
        you really mean it), and sessions already running under it keep their
        recorded config_dir, so they resume fine. Refused when it would leave
        no ready account. Removing the ACTIVE account re-routes new spawns to
        the ready account the router ranks best (see _route_key)."""
        with self.lock:
            a = self.accounts.get(name)
            others = [x for x in self.accounts.values()
                      if x.name != name and x.ready and not x.broken]
            if not a or not others:
                return False                     # never drop the last usable login
            del self.accounts[name]
            if self.active_account == name:
                best = min(others, key=self._route_key)
                self.active_account = best.name
                self.last_switch_at = time.time()
        print(f"[account {name}] removed from roster (credentials untouched)",
              flush=True)
        self.save_registry()
        self.broadcast_accounts()
        return True

    def use_account(self, name, why="manual"):
        """Flip which account NEW sessions spawn under. Existing sessions
        finish on their old account — zero interruption. Refuses a pending
        (credential-less) account — spawning there would drop the user's next
        session into a login TUI instead of running their prompt."""
        with self.lock:
            a = self.accounts.get(name)
            if not a or not a.ready or a.broken or name == self.active_account:
                return False
            self.active_account = name
            self.last_switch_at = time.time()
        print(f"[accounts] active → {name} ({why})", flush=True)
        self.save_registry()
        self.broadcast_accounts()
        return True

    def refresh_accounts(self):
        """Kick the usage poller now instead of waiting out the TTL."""
        self._poll_now.set()

    def poll_accounts_loop(self):
        """Background: watch pending accounts for their first credentials
        (sign-in completed) and keep every ready account's usage fresh
        (USAGE_TTL cadence; the endpoint is undocumented, so failures keep the
        last snapshot). Account fields are mutated under self.lock (readers —
        accounts_meta/save_registry — snapshot under the same lock); the
        blocking credential/network reads happen outside it. Any change
        re-evaluates the auto-switch rule."""
        while True:
            forced = self._poll_now.wait(timeout=15.0)
            self._poll_now.clear()
            changed = False
            now = time.time()
            with self.lock:
                accts = list(self.accounts.values())
            # Pending accounts: watch for first credentials. Full 15s cadence
            # while the sign-in is plausibly in progress (first hour), then
            # back off to USAGE_TTL — an abandoned ceremony must not fork a
            # Keychain subprocess every 15s forever.
            for a in accts:
                if a.ready and not a.broken:
                    continue
                # pending accounts await their FIRST credentials; broken ones
                # await a RE-sign-in — both watched the same way
                fresh_add = now - a.created < 3600
                if not (forced or fresh_add or a.broken
                        or now - a.last_pending_check > USAGE_TTL):
                    continue
                a.last_pending_check = now
                if not _has_creds(a.config_dir):
                    continue
                if a.broken and a.refused_sig \
                        and _cred_sig(a.config_dir) == a.refused_sig:
                    continue                     # same refused login still there —
                                                 # wait for an actual re-sign-in
                email, org, oname = _account_identity(a.config_dir)
                if not a.ready and a.config_dir:
                    _merge_mcp(a.config_dir)
                with self.lock:
                    was_broken = a.broken
                    a.ready, a.broken = True, False
                    a.refused_sig = ""
                    a.email = email or a.email
                    a.org = org or a.org
                    a.org_name = oname or a.org_name
                    a.tok.clear()                # stale cached token from the old login
                print(f"[account {a.name}] "
                      f"{'re-signed in' if was_broken else 'signed in'}"
                      f" ({email or 'email unknown'})", flush=True)
                changed = True
            # Ready accounts due a usage refresh — fetched in PARALLEL so one
            # slow/hung endpoint can't stall the other accounts (or the
            # sign-in watch above) behind a serial chain of 10s timeouts.
            # A pool nearing the hot bar can cross 90→100 inside one normal
            # poll under a hard parallel burn — watch the endgame 3× closer
            # so evacuation fires from headroom data, not the banner tripwire.
            def _ttl(a):
                pct = (a.usage or {}).get("pct")
                near_hot = pct is not None and pct >= SUB_HOT - 15
                return USAGE_TTL / 3 if near_hot else USAGE_TTL
            due = [a for a in accts if a.ready and not a.broken and
                   (forced or (now - (a.usage or {}).get("checkedAt", 0) > _ttl(a)
                               and now >= a.tok.get("no_poll_until", 0)))]
            if due:
                # An account with live claude sessions: those processes hold
                # (and renew) the very same refresh grant — the poller must
                # not consume it too, or two consumers of one rotating grant
                # race and the loser kills the token family. Poll such
                # accounts with the stored access token only; claude keeps
                # the store fresh whenever it actually works.
                with self.lock:
                    live = {s.account or "default"
                            for s in self.sessions.values() if s.alive}
                # Same-org logins share one pool AND one endpoint rate
                # limiter — polling each config dir separately multi-taps the
                # limiter for identical numbers (three EF seats = 3 hits per
                # burst, the pattern behind the 07-19 429 storm). Poll ONE
                # representative per known org and share its reading with the
                # siblings; never-identified (org-less) accounts poll solo.
                groups = {}
                for a in due:
                    groups.setdefault(a.org or f"~{a.name}", []).append(a)
                reps = [next((m for m in ms if m.name not in live), ms[0])
                        for ms in groups.values()]
                with ThreadPoolExecutor(max_workers=min(4, len(reps))) as ex:
                    got = list(ex.map(
                        lambda a: _fetch_usage(a.config_dir, a.tok,
                                               want_ident=True,
                                               allow_refresh=a.name not in live),
                        reps))
                for a, res in zip(reps, got):
                    sibs = [m for m in groups[a.org or f"~{a.name}"]
                            if m is not a]
                    if res == AUTH_FAIL:
                        # the rep's OWN login is refused → OUT of routing until
                        # re-sign-in; siblings hold their own credentials and
                        # are untouched — one of them fronts the next poll
                        sig = _cred_sig(a.config_dir)
                        with self.lock:
                            a.broken = True
                            a.refused_sig = sig
                        print(f"[account {a.name}] credentials refused — "
                              "excluded from routing until re-sign-in", flush=True)
                        changed = True
                    elif res == RATE_LIMITED:
                        # endpoint 429 ≠ plan exhausted (see the sentinel
                        # comment): keep the last GOOD reading routable while
                        # it's young enough to trust, banner the card, and
                        # spread the back-off horizon to the whole org so a
                        # sibling doesn't immediately re-poke the hot limiter.
                        until = a.tok.get("no_poll_until", 0)
                        with self.lock:
                            for m in [a] + sibs:
                                u = m.usage
                                if u and now - u.get("goodAt",
                                                     u.get("checkedAt", 0)) \
                                        < USAGE_RL_TRUST:
                                    u["checkedAt"] = now
                                m.error = ("usage endpoint rate-limited — "
                                           "backing off; real numbers resume "
                                           "automatically")
                                m.tok["no_poll_until"] = max(
                                    m.tok.get("no_poll_until", 0), until)
                        changed = True
                    elif res:
                        pct, windows, ident = res
                        with self.lock:
                            a.usage = {"pct": round(pct, 1), "windows": windows,
                                       "checkedAt": now, "goodAt": now}
                            a.error = ""
                            for m in sibs:
                                # same pool, same numbers — a copy, not a poll
                                m.usage = dict(a.usage)
                                m.error = ""
                            if ident:
                                # token-bound identity is THE authority: it
                                # names the pool the numbers above came from,
                                # even when a stale .claude.json disagrees
                                a.email = ident["email"] or a.email
                                a.org = ident["org"] or a.org
                                a.org_name = ident["org_name"] or a.org_name
                                a.tier = ident["tier"] or a.tier
                        changed = True
                    elif not a.error:
                        with self.lock:
                            a.error = ("access token stale — a live claude "
                                       "session renews it on its next turn"
                                       if a.name in live else "usage unavailable")
                        changed = True
                    # backfill from .claude.json only while the profile
                    # endpoint hasn't spoken — a label guess, never an override
                    if not a.email or not a.org or not a.org_name:
                        email, org, oname = _account_identity(a.config_dir)
                        if (email and not a.email) or (org and not a.org) \
                                or (oname and not a.org_name):
                            with self.lock:
                                a.email = a.email or email
                                a.org = a.org or org
                                a.org_name = a.org_name or oname
                            changed = True
            if changed:
                self.save_registry()
                self.broadcast_accounts()
                self._maybe_autoswitch()
                self._handoff_sweep()

    def _handoff_sweep(self):
        """Poller-driven safety net behind maybe_handoff: sessions idling on a
        drained/broken plan get moved even if they never emit another Stop
        (e.g. the limit screen already ate their last turn). With
        SUB_REBALANCE it ALSO moves idle sessions off healthy pools when the
        router's best pool wins on the weekly-reset clock (_rebalance_win) —
        promise 2's spend-the-soonest-reset policy applied to running
        sessions, not just new spawns."""
        if not SUB_AUTOSWITCH:
            return
        now = time.time()
        with self.lock:
            drained, dead, hot, pcts = set(), set(), set(), {}
            for a in self.accounts.values():
                pct = (a.usage or {}).get("pct", 0)
                pcts[a.name] = pct
                if a.broken or pct >= SUB_EXHAUSTED:
                    drained.add(a.name)
                if a.broken or pct >= 100:
                    dead.add(a.name)             # an in-flight turn CANNOT finish here
                if a.broken or pct >= SUB_HOT:
                    hot.add(a.name)              # heating toward the wall — stop feeding it
            sessions = list(self.sessions.values())
        best = self.accounts.get(self._best_account() or "")
        if not best or best.name in drained:
            return
        for s in sessions:
            if s.ceremony:
                continue                         # deliberate sign-in — hands off
            if not (s.alive and s.account != best.name
                    and now - s.last_handoff >= HANDOFF_COOLDOWN):
                continue
            if s.account in drained:
                if s.busy:
                    # `busy` with silent hooks on a dead plan = the limit screen ate
                    # the turn (no Stop ever comes) — stuck, not working. Reclaim it.
                    if not (s.account in dead and now - s.last_active > BUSY_STUCK):
                        continue
                    print(f"[handoff {s.cid[:8]}] busy but hook-silent "
                          f"{int(now - s.last_active)}s on dead plan {s.account} — "
                          "treating as stuck", flush=True)
                    s.busy = False
                self._handoff(s, best)
                continue
            if s.busy:
                continue
            if s.bg or s._bg_probe():
                # Idle-looking but background shells/agents are still running —
                # a respawn would kill them. Preemptive moves (evacuation,
                # rebalance) aren't worth that; only the drained rescue above
                # may still take the session.
                continue
            # Preemptive evacuation: an idle session on a heating pool moves to
            # a COOL best before the wall, not after (never-see-a-rate-limit).
            if s.account in hot and best.name not in hot:
                self._handoff(s, best, f"pool {pcts.get(s.account, 0):.0f}% hot "
                                       "— evacuating before the limit wall")
                continue
            why = self._rebalance_win(s.account, best)
            if why:
                self._handoff(s, best, why)

    def _rebalance_win(self, name, best):
        """Reason string when an idle session on healthy pool `name` should
        move to `best` anyway: best's weekly window resets ≥
        SUB_REBALANCE_MARGIN sooner (use-it-or-lose-it). None = stay put.
        Same-pool logins (one org, several config dirs) never rebalance —
        they share the limit, so moving buys nothing. Both reset clocks must
        be KNOWN: a blind/stale pool is a polling problem, not a routing
        signal, and pct headroom alone never justifies a respawn (the drain
        rescue covers that endgame)."""
        if not SUB_REBALANCE:
            return None
        acct = self.accounts.get(name)
        if not acct or not acct.ready or acct.broken:
            return None
        if acct.org and best.org and acct.org == best.org:
            return None
        cur_r, best_r = _weekly_reset(acct.usage), _weekly_reset(best.usage)
        if cur_r is None or best_r is None \
                or cur_r - best_r < SUB_REBALANCE_MARGIN:
            return None
        return (f"rebalance: weekly resets {int((cur_r - best_r) // 3600)}h "
                "sooner — spend it before it's forfeited")

    def rescue_bounced_prompt(self, s, prompt, hooks_at_prompt):
        """A prompt just landed on a plan that looks drained. If the plan is
        HARD dead (>=100% used, or the login is refused) the CLI answers with
        its limit line and the turn never runs — no Stop ever comes, so the
        on-Stop handoff can't fire and the stuck sweep is BUSY_STUCK away
        (worse: the prompt hook just reset last_active, re-arming that clock).
        The user's own message is the strongest "I am waiting" signal there
        is, so it triggers the rescue: confirm the plan is dead against the
        live endpoint, hand off to the best pool, and REDELIVER the bounced
        prompt there. A 95–99% plan can still finish a turn — only >=100 (or
        AUTH_FAIL) qualifies, and a genuinely-running turn is detected and
        left alone."""
        if not SUB_AUTOSWITCH or s.ceremony:     # sign-in ceremony: never rescued
            return
        time.sleep(BOUNCE_SETTLE)
        # ANY hook since the send (tool hooks, or a Stop that took the normal
        # maybe_handoff path) = the turn progressed = not a bounce. hook_count
        # is the whole test — `busy` is NOT required: a bounced send sets no
        # hooks at all, so nothing ever sets busy (the 2026-07-12 03:2x miss).
        if not s.alive or s.hook_count != hooks_at_prompt:
            return
        now = time.time()
        if now - s.last_bounce_rescue < BOUNCE_COOLDOWN:
            return
        acct = self.accounts.get(s.account)
        cfg = acct.config_dir if acct else (s.config_dir or "")
        got = _fetch_usage(cfg, acct.tok if acct else None, allow_refresh=False)
        # A 429 here corroborates the bounce: a hard-limited plan 429s its own
        # usage endpoint, and the prompt's silent death is independent evidence
        # (unlike the background poller, where a bare 429 proves nothing).
        dead = got in (AUTH_FAIL, RATE_LIMITED)
        if got and got not in (AUTH_FAIL, RATE_LIMITED):
            pct, windows = got
            dead = pct >= 100
            if acct:
                with self.lock:
                    now2 = time.time()
                    acct.usage = {"pct": round(pct, 1), "windows": windows,
                                  "checkedAt": now2, "goodAt": now2}
                    acct.broken = False
        elif got is None and acct:
            # Endpoint unreachable with the stored token — fall back to the
            # poll snapshot that flagged this plan (only if it's fresh).
            u = acct.usage or {}
            fresh_poll = now - (u.get("checkedAt") or 0) < 600
            dead = acct.broken or (fresh_poll and u.get("pct", 0) >= 100)
        if not dead:
            return
        best = self.accounts.get(self._best_account() or "")
        if (not best or best.name == s.account
                or (acct and acct.org and best.org and acct.org == best.org)
                or (best.usage or {}).get("pct", 100.0) >= SUB_EXHAUSTED):
            self.broadcast_accounts()
            return                               # nowhere better to go — stay put
        s.last_bounce_rescue = time.time()
        self.broadcast_accounts()
        # The eaten turn never emits Stop, so `busy` is a lie here — the same
        # reclaim the stuck sweep does, without the BUSY_STUCK wait.
        s.busy = False
        print(f"[handoff {s.cid[:8]}] prompt bounced off dead plan {s.account} "
              "— rescuing now and redelivering", flush=True)
        self._handoff(s, best,
                      why="prompt bounced off the dead plan; resuming under the fresh one")
        fresh = self.sessions.get(s.cid)
        if fresh is s or not fresh or not fresh.alive or not prompt.strip():
            return                               # handoff declined/failed — nothing to redeliver
        # Wait for the resumed claude to come up (SessionStart), give the TUI a
        # beat to finish painting, then retype the bounced message — the user's
        # send is what heals the session, not what gets lost.
        fresh._started_evt.wait(20)
        time.sleep(2)
        if fresh.alive:
            print(f"[handoff {s.cid[:8]}] redelivering the bounced prompt "
                  f"({len(prompt)} chars) under {fresh.account}", flush=True)
            fresh.send_message(prompt)

    def rescue_limit_wall(self, s):
        """The CLI just painted its limit banner in this session's terminal
        (_scan_for_limit). That's the zero-lag wall signal — hooks are silent
        from here and the sweep is BUSY_STUCK away — but PTY text alone is
        never trusted: confirm against the live endpoint first, so a session
        merely QUOTING the banner (this repo's own docs contain it) on a
        healthy pool is a no-op. Confirmed, the session moves immediately;
        a prompt the wall ate is redelivered, and a turn it cut mid-flight
        gets an automatic 'continue' (LIMIT_CONTINUE) — the user never
        babysits a rate limit."""
        if not SUB_AUTOSWITCH or not s.alive or s.ceremony:
            return
        now = time.time()
        if now - s.last_bounce_rescue < BOUNCE_COOLDOWN:
            return
        # Claim the cooldown BEFORE the slow confirm — it's the mutual-exclusion
        # anchor against rescue_bounced_prompt racing this on the same session.
        s.last_bounce_rescue = now
        acct = self.accounts.get(s.account)
        cfg = acct.config_dir if acct else (s.config_dir or "")
        # allow_refresh=False: this session's own claude holds this grant
        got = _fetch_usage(cfg, acct.tok if acct else None, allow_refresh=False)
        # endpoint mute OR 429 → believe the banner (the PTY text is the
        # independent evidence a bare poller 429 lacks)
        walled = got is None or got in (AUTH_FAIL, RATE_LIMITED)
        if got and got not in (AUTH_FAIL, RATE_LIMITED):
            pct, windows = got
            walled = pct >= SUB_HOT              # the wall the banner announced
            if acct:
                with self.lock:
                    now2 = time.time()
                    acct.usage = {"pct": round(pct, 1), "windows": windows,
                                  "checkedAt": now2, "goodAt": now2}
                    acct.broken = False
        if not walled:
            return                               # echoed/stale banner on a cool pool
        best = self.accounts.get(self._best_account() or "")
        if (not best or best.name == s.account
                or (acct and acct.org and best.org and acct.org == best.org)
                or (best.usage or {}).get("pct", 100.0) >= SUB_EXHAUSTED):
            self.broadcast_accounts()
            return                               # nowhere better to go — stay put
        bounced = s.busy and s.hook_count == s.hooks_at_prompt and s.last_prompt.strip()
        cut_midturn = s.busy and not bounced
        self.broadcast_accounts()
        # The walled turn will never emit Stop — reclaim busy so _handoff runs.
        s.busy = False
        self._handoff(s, best, why="limit banner on screen; resuming under the fresh pool")
        fresh = self.sessions.get(s.cid)
        if fresh is s or not fresh or not fresh.alive:
            return                               # handoff declined/failed
        fresh._started_evt.wait(20)
        time.sleep(2)
        if not fresh.alive:
            return
        if bounced:
            print(f"[handoff {s.cid[:8]}] redelivering the walled prompt "
                  f"({len(s.last_prompt)} chars) under {fresh.account}", flush=True)
            fresh.send_message(s.last_prompt)
        elif cut_midturn and LIMIT_CONTINUE:
            print(f"[handoff {s.cid[:8]}] turn was cut by the wall — "
                  f"auto-continuing under {fresh.account}", flush=True)
            fresh.send_message("continue")

    def rescue_onboarding(self, s):
        """The CLI painted its first-run onboarding (theme picker) in this
        session's terminal (_scan_for_onboarding). On a dir that holds a
        login that screen is always wrong — the half-finished-ceremony
        ambush (_ensure_onboarded's docstring; EXPECTATIONS.md 2026-07-16).
        Heal: seed hasCompletedOnboarding, then respawn the SAME session
        under the SAME account — the fresh claude reads the completed flag
        and opens straight into the session; a prompt the picker ate is
        redelivered. A dir with NO login is a real sign-in ceremony whose
        onboarding is the point — left strictly alone."""
        if not s.alive or s.ceremony:            # a ceremony's screens are the point
            return
        _ensure_onboarded(s.config_dir)          # no-op if already flagged
        cfg = _claude_config_file(s.config_dir)
        try:
            data = json.loads(cfg.read_text()) if cfg.exists() else {}
        except (OSError, ValueError):
            data = {}
        if not data.get("hasCompletedOnboarding"):
            print(f"[session {s.cid[:8]}] onboarding on screen but "
                  f"{s.account} holds no login (or the seed write failed) — "
                  "a real ceremony, leaving it alone", flush=True)
            return
        if s._onboard_rescues >= 2:
            print(f"[session {s.cid[:8]}] onboarding screen came BACK after "
                  f"{s._onboard_rescues} respawns — something keeps "
                  "un-flagging the dir; giving up (pick a theme by hand and "
                  "bring this log line)", flush=True)
            return
        print(f"[session {s.cid[:8]}] onboarding ambush on {s.account} — "
              "flag seeded; respawning past the screen", flush=True)
        # A prompt delivered into the picker fires no hooks at all (same
        # signature as the limit-wall bounce) — remember it for redelivery.
        bounced = bool(s.last_prompt.strip()) and s.hook_count == s.hooks_at_prompt
        fresh = s.clone_for_respawn(last_active=time.time())
        fresh._onboard_rescues = s._onboard_rescues + 1
        with self.lock:
            if self.sessions.get(s.cid) is not s:
                return                           # a handoff replaced it mid-rescue
            self.sessions[s.cid] = fresh
        s.kill()
        fresh.start()
        with s.clients_lock:                     # carry the viewers across
            viewers = list(s.clients)
            s.clients.clear()
        for c in viewers:
            if c.cid == fresh.cid:
                fresh.subscribe(c)
        self.save_registry()
        self.broadcast_sessions()
        if bounced:
            fresh._started_evt.wait(20)
            time.sleep(2)
            if fresh.alive:
                print(f"[session {s.cid[:8]}] redelivering the prompt the "
                      f"onboarding screen ate ({len(s.last_prompt)} chars)",
                      flush=True)
                fresh.send_message(s.last_prompt)

    def maybe_handoff(self, s):
        """Mid-session account handoff (SUB-ROUTING.md Phase 5): called after
        every Stop. If THIS session's plan is drained (>= SUB_EXHAUSTED used,
        or its login broke) — or merely HOT (>= SUB_HOT: the session window is
        heating toward the wall) — and a better plan is ready, respawn the
        session under that plan with --resume — transcript symlinked across,
        so the conversation continues seamlessly and the user is never asked
        to do anything. A hot-but-alive pool only gives the session up to a
        COOL target (a lateral hop buys nothing); a drained one flees to
        anything under SUB_EXHAUSTED. The usage check hits the endpoint
        directly (the poll is too slow to catch a window dying
        mid-conversation)."""
        if not SUB_AUTOSWITCH or s.busy or not s.alive or s.ceremony:
            return
        if time.time() - s.last_handoff < HANDOFF_COOLDOWN:
            return
        acct = self.accounts.get(s.account)      # may be None (e.g. removed default)
        cfg = acct.config_dir if acct else (s.config_dir or "")
        # Respect an active 429 back-off horizon: this check fires after EVERY
        # Stop, and poking a hot limiter once per turn is a big part of how
        # the endpoint got rate-limited in the first place. Backed off (or
        # answered 429), a Stop in hand means the turn RAN — that's evidence
        # AGAINST a wall — so fall back to the cached reading instead.
        backed_off = acct and time.time() < acct.tok.get("no_poll_until", 0)
        # allow_refresh=False: this session's own claude holds this grant
        got = None if backed_off else _fetch_usage(
            cfg, acct.tok if acct else None, allow_refresh=False)
        drained = hot = got == AUTH_FAIL
        pct = 100.0
        if got and got not in (AUTH_FAIL, RATE_LIMITED):
            pct, windows = got
            drained = pct >= SUB_EXHAUSTED
            hot = pct >= SUB_HOT
            if acct:
                with self.lock:
                    now2 = time.time()
                    acct.usage = {"pct": round(pct, 1), "windows": windows,
                                  "checkedAt": now2, "goodAt": now2}
                    acct.broken = False
        elif (backed_off or got == RATE_LIMITED) and acct:
            u = acct.usage or {}
            if time.time() - (u.get("checkedAt") or 0) < 3 * USAGE_TTL:
                pct = u.get("pct", 100.0)
                drained, hot = pct >= SUB_EXHAUSTED, pct >= SUB_HOT
        elif acct and drained:
            sig = _cred_sig(acct.config_dir)
            with self.lock:
                acct.broken = True
                acct.refused_sig = sig
        if not hot:
            return
        best = self.accounts.get(self._best_account() or "")
        bar = SUB_EXHAUSTED if drained else SUB_HOT
        if (not best or best.name == s.account
                or (acct and acct.org and best.org and acct.org == best.org)
                or (best.usage or {}).get("pct", 100.0) >= bar):
            self.broadcast_accounts()
            return                               # nowhere better to go — stay put
        self.broadcast_accounts()
        if not drained and (s.bg or s._bg_probe()):
            return                               # hot-move is optional; don't kill live background work
        why = ("plan drained; resuming under the fresh one" if drained else
               f"pool {pct:.0f}% hot — moving before the limit wall")
        self._handoff(s, best, why)

    def _handoff(self, s, target, why="plan drained; resuming under the fresh one"):
        """Move one idle session to `target`'s account: link its transcript
        into the target config dir (real-file-wins, never clobber), replace
        the session object under the SAME cid with a --resume respawn, and
        move the viewers over. The old claude gets SIGTERM once we're sure."""
        if s.busy or not s.alive or s.ceremony:  # re-check after the network call
            return
        s.last_handoff = time.time()
        src = s.transcript_path or s._find_transcript()
        base_src = Path(s.config_dir or os.path.expanduser("~/.claude"))
        base_dst = Path(target.config_dir or os.path.expanduser("~/.claude"))
        if src and base_src != base_dst:
            try:
                src = Path(src)
                rel = src.relative_to(base_src)  # projects/<munged-cwd>/<sid>.jsonl
                for extra in [src, src.with_suffix("")]:   # + subagents dir if present
                    if not extra.exists():
                        continue
                    dst = base_dst / extra.relative_to(base_src)
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    if not (dst.exists() or dst.is_symlink()):
                        dst.symlink_to(extra)
            except (ValueError, OSError) as e:
                print(f"[handoff {s.cid[:8]}] transcript link failed ({e}) — "
                      "staying put", flush=True)
                return
        print(f"[handoff {s.cid[:8]}] {s.account} → {target.name} "
              f"({why})", flush=True)
        fresh = s.clone_for_respawn(
            resuming=True, last_active=time.time(),
            account=target.name, config_dir=target.config_dir)
        with self.lock:
            self.sessions[s.cid] = fresh
        s.kill()                                 # SIGTERM the drained claude (exit broadcast suppressed)
        fresh.start()
        with s.clients_lock:                     # carry the viewers across
            viewers = list(s.clients)
            s.clients.clear()
        for c in viewers:
            if c.cid == fresh.cid:               # skip a viewer that switched away mid-handoff
                fresh.subscribe(c)
        self.save_registry()
        self.broadcast_sessions()

    def _maybe_autoswitch(self):
        """Local switch rule (direct mode; the fleet relay will own this
        fleet-wide): move to the pool _route_key ranks best — the
        non-exhausted one whose weekly window resets soonest. Reset order is
        stable between polls, so a reset-driven win needs only the
        SUB_DEBOUNCE; a pct-driven win (reset times unknown) also needs
        SUB_HYSTERESIS points, as before. An active account at/over
        SUB_EXHAUSTED bypasses the debounce when the target has room (no
        loyalty to a dead account). Only ever affects NEW spawns."""
        if not SUB_AUTOSWITCH:
            return
        with self.lock:
            cur = self.accounts.get(self.active_account)
            ready = [a for a in self.accounts.values()
                     if a.ready and not a.broken
                     and (a.usage or {}).get("pct") is not None]
        if not cur or len(ready) < 2:
            return
        cur_pct = (cur.usage or {}).get("pct")
        if cur_pct is None:
            return
        best = min(ready, key=self._route_key)
        if best.name == cur.name:
            return
        cur_k, best_k = self._route_key(cur), self._route_key(best)
        gain = cur_pct - best.usage["pct"]
        # The hot bypass only fires when the TARGET is actually cool —
        # two accounts both over the threshold would otherwise ping-pong every
        # poll (each switch making the other one "best"), debounce ignored.
        # All-hot falls back to the debounced rules below.
        exhausted = cur_k[0] and not best_k[0]
        # Did best win on the weekly-reset clock (sooner reset, or a known
        # reset vs an unknown one)? That ordering only changes when a window
        # actually resets, so debounce alone is enough to prevent flap.
        by_reset = best_k[:3] < cur_k[:3] and best_k[1:3] != cur_k[1:3]
        if exhausted or ((by_reset or gain >= SUB_HYSTERESIS)
                         and time.time() - self.last_switch_at >= SUB_DEBOUNCE):
            if exhausted:
                why = ("active exhausted" if cur_pct >= SUB_EXHAUSTED
                       else f"active pool {cur_pct:.0f}% hot — routing around the wall")
            elif by_reset and not (cur_k[1] or best_k[1]):
                why = (f"weekly resets {max(1, int((cur_k[2] - best_k[2]) // 3600))}h "
                       "sooner — spend it before it's forfeited")
            elif by_reset:
                why = "weekly reset known vs unknown"
            else:
                why = f"+{gain:.0f} pts headroom"
            self.use_account(best.name, why=why)

    # -- project crud ----------------------------------------------------------
    def _readopt(self, base):
        """If `base` names a dir already on disk in projects/ (left behind by a
        non-destructive remove, or a partial clone), re-register it in place and
        SKIP cloning — the files are already there, so a clone would only fail
        (e.g. the remote was renamed/deleted). Returns the (re)adopted Project, or
        None when there's nothing on disk to adopt (→ clone fresh)."""
        path = str(PROJECTS_DIR / base)
        with self.lock:
            for p in list(self.projects.values()):
                if p.path != path:
                    continue
                if p.status == "error" and not os.path.isdir(path):
                    # a failed clone left a folder-less error corpse at this
                    # path — returning it would block the retry the user just
                    # asked for; drop it and clone fresh under the same name
                    self.projects.pop(p.pid, None)
                    print(f"[project {base}] dropped failed entry → retrying",
                          flush=True)
                    break
                return p                         # already registered → reuse it
        try:
            present = os.path.isdir(path) and bool(os.listdir(path))
        except OSError:
            present = False
        if not present:
            return None                          # nothing on disk → clone fresh
        is_git = os.path.exists(os.path.join(path, ".git"))  # dir=clone, file=worktree
        p = Project(pid=str(uuid.uuid4()), name=base, path=path,
                    repo_url=_git_remote_url(path) if is_git else "",
                    status="ready", created=time.time())
        with self.lock:
            self.projects[p.pid] = p
        self.save_registry()
        self.broadcast_projects()
        if is_git:                               # best-effort refresh; never blocks adoption
            threading.Thread(target=self._refresh_repo, args=(path, base),
                             daemon=True).start()
        print(f"[project {base}] re-adopted existing dir (skipped clone)", flush=True)
        return p

    def _refresh_repo(self, path, base):
        """Best-effort `git pull --ff-only` on an adopted repo. Non-fatal: a repo
        with local changes, no upstream, or a gone remote just stays as-is."""
        try:
            r = subprocess.run(["git", "pull", "--ff-only"], cwd=path,
                               capture_output=True, text=True, timeout=60)
            if r.returncode == 0:
                print(f"[project {base}] pulled", flush=True)
            else:
                print(f"[project {base}] pull skipped: "
                      f"{(r.stderr or r.stdout or '').strip()[-120:]}", flush=True)
        except Exception as e:
            print(f"[project {base}] pull error: {e}", flush=True)

    def create_project(self, name):
        """Create a new public repo under GH_OWNER and clone it into projects/.
        If a dir of the same name already exists on disk (e.g. removed earlier),
        re-adopt it in place rather than spinning up a `name-2`. If the repo
        already exists *remotely* on GH_OWNER (e.g. created on another machine),
        clone it instead of trying to `gh repo create` (which would 422)."""
        base = _safe_name(name)
        existing = self._readopt(base)
        if existing:
            return existing
        if _remote_repo_exists(f"{GH_OWNER}/{base}"):
            print(f"[project {base}] exists on {GH_OWNER} — cloning instead of creating",
                  flush=True)
            return self.add_project(f"{GH_OWNER}/{base}")
        safe = self._unique_project_name(base)
        path = str(PROJECTS_DIR / safe)
        url = f"https://github.com/{GH_OWNER}/{safe}"
        p = Project(pid=str(uuid.uuid4()), name=safe, path=path,
                    repo_url=url, status="cloning", created=time.time())
        with self.lock:
            self.projects[p.pid] = p
        self.broadcast_projects()
        cmd = ["gh", "repo", "create", f"{GH_OWNER}/{safe}",
               "--public", "--add-readme", "--clone"]
        threading.Thread(target=self._provision, args=(p, cmd, "create"),
                         daemon=True).start()
        return p

    def add_project(self, repo_url):
        """Clone an existing repo into projects/. Accepts a full git URL/path, an
        `owner/repo` shorthand, or a bare `repo` name — the latter two are
        resolved against github.com (bare names assume GH_OWNER), so typing
        `slop-computer-live` clones github.com/clawdbotatg/slop-computer-live."""
        repo_url = (repo_url or "").strip()
        if not re.match(r"^(https?://|git@|ssh://|file://|/|~)", repo_url):
            repo_url = (f"https://github.com/{repo_url}" if "/" in repo_url
                        else f"https://github.com/{GH_OWNER}/{repo_url}")
        base = _safe_name(re.sub(r"\.git$", "", repo_url.rstrip("/").split("/")[-1]))
        existing = self._readopt(base)
        if existing:
            return existing
        safe = self._unique_project_name(base)
        path = str(PROJECTS_DIR / safe)
        p = Project(pid=str(uuid.uuid4()), name=safe, path=path,
                    repo_url=repo_url, status="cloning", created=time.time())
        with self.lock:
            self.projects[p.pid] = p
        self.broadcast_projects()
        cmd = ["git", "clone", repo_url, safe]
        threading.Thread(target=self._provision, args=(p, cmd, "clone"),
                         daemon=True).start()
        return p

    def add_local_project(self, raw):
        """Register an existing folder anywhere on this machine's disk as a
        PRIVATE local project (kind="local"): sessions run inside it like any
        project, but the harness never runs gh/git-remote operations on it and
        never stores/broadcasts a repo URL for it. Lives only in the registry
        (it can't be disk-discovered); removed via removeProject, never by the
        disk reconcile. Returns (project, "") or (None, error)."""
        raw = (raw or "").strip()
        if not raw:
            return None, "empty path"
        path = os.path.realpath(os.path.expanduser(raw))
        if not os.path.isdir(path):
            return None, f"not a directory: {path}"
        home = os.path.realpath(os.path.expanduser("~"))
        if path in ("/", home):
            return None, "refusing to register / or your home folder itself"
        pdir = os.path.realpath(str(PROJECTS_DIR))
        if path == pdir or path.startswith(pdir + os.sep):
            return None, "that's inside projects/ — it's already auto-managed"
        here = os.path.realpath(str(HERE))
        if path == here or path.startswith(here + os.sep):
            return None, "that's the harness itself (the pinned project)"
        with self.lock:
            for p in self.projects.values():
                if os.path.realpath(p.path) == path:
                    return p, ""                 # already registered → reuse
        name = self._unique_project_name(os.path.basename(path))
        p = Project(pid=str(uuid.uuid4()), name=name, path=path,
                    status="ready", created=time.time(), kind="local")
        with self.lock:
            self.projects[p.pid] = p
        self.save_registry()
        self.broadcast_projects()
        print(f"[project {name}] local folder registered ({path})", flush=True)
        return p, ""

    def remove_project(self, pid):
        """Detach a LOCAL project: drop the registry entry and close its
        sessions. Never touches the folder on disk, and only applies to
        kind="local" — gh projects keep the delete-the-folder contract."""
        with self.lock:
            p = self.projects.get(pid)
            if not p or p.kind != "local" or p.pinned:
                return False
            self.projects.pop(pid, None)
            cids = [c for c, s in self.sessions.items() if s.pid == pid]
        for cid in cids:
            self.close(cid, _broadcast=False)
        print(f"[project {p.name}] local project detached "
              f"(folder untouched: {p.path})", flush=True)
        self.save_registry()
        self.broadcast_projects()
        self.broadcast_sessions()
        return True

    def _provision(self, project, cmd, kind):
        """Run a clone/create in PROJECTS_DIR, then flip the project's status."""
        try:
            r = subprocess.run(cmd, cwd=str(PROJECTS_DIR),
                               capture_output=True, text=True, timeout=180)
            ok = r.returncode == 0 and os.path.isdir(
                os.path.join(project.path, ".git"))
            if ok:
                project.status = "ready"
                project.error = ""
                if not project.repo_url:
                    project.repo_url = _git_remote_url(project.path)
                print(f"[project {project.name}] {kind} ok", flush=True)
            else:
                project.status = "error"
                project.error_at = time.time()
                err = (r.stderr or r.stdout or "failed").strip()
                if kind == "create" and ("auth" in err.lower() or "gh auth" in err.lower()):
                    err += " (is `gh` authenticated in the server's environment?)"
                project.error = err[-300:]
                print(f"[project {project.name}] {kind} FAILED: {project.error}",
                      flush=True)
        except Exception as e:
            project.status = "error"
            project.error_at = time.time()
            project.error = str(e)[-300:]
            print(f"[project {project.name}] {kind} error: {e}", flush=True)
        self.save_registry()
        self.broadcast_projects()

    def _unique_project_name(self, base):
        existing = {p.name for p in self.projects.values()}
        if base not in existing and not os.path.exists(PROJECTS_DIR / base):
            return base
        i = 2
        while f"{base}-{i}" in existing or os.path.exists(PROJECTS_DIR / f"{base}-{i}"):
            i += 1
        return f"{base}-{i}"

    def get_project(self, pid):
        with self.lock:
            return self.projects.get(pid)

    def _project_last_active(self):
        """pid -> most recent session activity (max last_active over its
        sessions), the raw input to the warmth sort. No lock: reads sessions
        the same lock-free way as `_ordered()`."""
        latest = {}
        for s in self.sessions.values():
            if s.last_active > latest.get(s.pid, 0.0):
                latest[s.pid] = s.last_active
        return latest

    def _warmth(self, p, latest):
        """How 'warm' a project is: its most-recently-active session, falling
        back to its own creation time. Spinning up a session or sending a prompt
        bumps a session's last_active → floats the project to the top."""
        return max(latest.get(p.pid, 0.0), p.created)

    def _ordered_projects(self):
        # pinned (the harness itself) first, then warmest first (most recently
        # touched session at top), creation time as the fallback/tiebreak.
        latest = self._project_last_active()
        return sorted(self.projects.values(),
                      key=lambda p: (not p.pinned, -self._warmth(p, latest)))

    def session_count(self, pid):
        with self.lock:
            return sum(1 for s in self.sessions.values() if s.pid == pid)

    def session_counts(self, pid):
        """(total, busy, waiting) sessions for a project — busy = a turn in
        flight; waiting = blocked on an interactive prompt (needs you)."""
        with self.lock:
            total = busy = waiting = 0
            for s in self.sessions.values():
                if s.pid == pid:
                    total += 1
                    if s.waiting:
                        waiting += 1
                    elif s.busy:
                        busy += 1
            return total, busy, waiting

    def projects_meta(self):
        latest = self._project_last_active()
        return [p.meta(*self.session_counts(p.pid),
                       last_touched=self._warmth(p, latest))
                for p in self._ordered_projects()]

    # -- session crud ----------------------------------------------------------
    def create_session(self, pid, account=None, ceremony=False):
        if pid not in self.projects:
            return None
        proj = self.projects[pid]
        if proj.kind == "local" and proj.status == "error":
            return None                          # folder missing — Popen on a dead cwd would fail
        # Routing: an explicit override (e.g. the sign-in ceremony) always
        # wins; otherwise, with auto-routing on, each new session picks the
        # account with the MOST HEADROOM at this moment (per-spawn — not a
        # sticky default), falling back to active_account when no fresh usage
        # data qualifies. The resolved config dir is recorded on the session
        # so --resume always finds it.
        name = account
        if not name:
            name = (self._best_account() if SUB_AUTOSWITCH else None) \
                   or self.active_account
        acct = self.accounts.get(name)
        if acct is None:
            if account:                          # explicit ask for a missing account
                print(f"[accounts] unknown account {account!r} requested — "
                      "spawning under default", flush=True)
            name, acct = "default", self.accounts.get("default")
        # NEVER ambush the user with a login screen: verify the chosen
        # account's credentials exist at THIS moment (explicit overrides are
        # exempt — the sign-in ceremony spawns into a credential-less dir on
        # purpose). This includes `default`: the machine's plain ~/.claude is
        # a login like any other, and on a box whose sign-ins all live in
        # named account dirs it may hold nothing. A signed-out account gets
        # flagged + skipped for the next-best ready one.
        no_creds_anywhere = False
        state = "present" if account else \
            _creds_state(acct.config_dir if acct else "")
        if state == "unknown":
            # Credential store unreadable (locked keychain / fd exhaustion /
            # timeout) — a transient READ failure, not a sign-out. Never mark
            # anything broken and never reroute off it: the spawned claude
            # reads the keychain itself and usually succeeds where we could
            # not (root cause v3's rule, applied to the spawn gate).
            print(f"[accounts] {name}: credential store unreadable — "
                  "transient; spawning under it anyway", flush=True)
        elif state == "absent":
            if acct:
                acct.broken = True
            print(f"[accounts] {name} is signed out — rerouting this spawn",
                  flush=True)
            with self.lock:
                alts = sorted(
                    [x for x in self.accounts.values()
                     if x.ready and not x.broken and x.name != name],
                    key=lambda x: (x.usage or {}).get("pct", 100.0))
            name, acct = "default", None         # last resort: plain ~/.claude
            for alt in alts:
                st = _creds_state(alt.config_dir)
                if st != "absent":               # present, or unreadable-but-
                    name, acct = alt.name, alt   # probably-there — never mark
                    break                        # broken on a failed READ
                alt.broken = True
            if acct is None and _creds_state("") == "absent":
                no_creds_anywhere = True
                print("[accounts] NO plan is signed in on this machine — the "
                      "new session opens Claude's login screen; complete it "
                      "once (or sign in via the \U0001f9e0 page)", flush=True)
            self.broadcast_accounts()
        cid = str(uuid.uuid4())
        s = ClaudeSession(self, cid=cid, pid=pid, session_id=str(uuid.uuid4()),
                          resuming=False, created=time.time(),
                          account=name,
                          config_dir=acct.config_dir if acct else "",
                          ceremony=ceremony or no_creds_anywhere)
        if no_creds_anywhere:
            s.desc = ("no plan is signed in on this machine yet — complete "
                      "the login in this terminal (once per machine)")
        with self.lock:
            self.sessions[cid] = s
        s.start()
        self.save_registry()
        self.broadcast_sessions()
        return s

    def get(self, cid):
        with self.lock:
            return self.sessions.get(cid)

    def pin(self, cid, on):
        """📌 park a session on the pin board (or restore it). Pure metadata —
        the claude process is untouched, so a pinned to-do can still be
        prompted from the board and picks up exactly where it left off."""
        s = self.get(cid)
        if not s:
            return
        s.pinned = time.time() if on else 0.0
        print(f"[session {cid[:8]}] {'pinned 📌' if on else 'unpinned'}",
              flush=True)
        self.save_registry()
        self.broadcast_sessions()

    def close(self, cid, _broadcast=True):
        with self.lock:
            s = self.sessions.pop(cid, None)
        if not s:
            return
        s.kill()
        # detach any viewers so they reattach elsewhere
        with s.clients_lock:
            viewers = list(s.clients)
            s.clients.clear()
        for c in viewers:
            c.cid = None
        self.save_registry()
        if _broadcast:
            self.broadcast_sessions()

    def _ordered(self):
        """Most-recently-active first — the menu order."""
        return sorted(self.sessions.values(),
                      key=lambda s: s.last_active, reverse=True)

    def default_cid(self):
        ses = self._ordered()
        return ses[0].cid if ses else None

    def sessions_meta(self):
        return [s.meta() for s in self._ordered()]

    # -- global client registry (menu-level fan-out) ---------------------------
    def add_client(self, client):
        with self.clients_lock:
            self.all_clients.add(client)
        # Send projects then sessions; the client decides the initial view (no
        # forced focus — there may be zero sessions).
        client.send_json({"type": "projects", "projects": self.projects_meta(),
                          "boot": BOOT_ID})
        client.send_json({"type": "sessions",
                          "sessions": self.sessions_meta(),
                          "current": self.default_cid()})
        client.send_json(self.accounts_meta())
        if self.restart_pending:                 # a late joiner still sees the banner
            client.send_json(self.restart_state())

    def remove_client(self, client):
        with self.clients_lock:
            self.all_clients.discard(client)
        if client.cid:
            s = self.get(client.cid)
            if s:
                s.unsubscribe(client)

    def subscribe_client(self, client, cid):
        s = self.get(cid)
        if client.cid and client.cid != cid:
            old = self.get(client.cid)
            if old:
                old.unsubscribe(client)
        if not s:
            # Unknown cid (closed here, or a fleet subscribe routed to the wrong
            # box). Going silent while the previous subscription kept streaming is
            # how "another session's output paints into this terminal" happened —
            # detach (above) and answer loudly instead.
            client.cid = None
            client.send_json({"type": "error", "cid": cid,
                              "error": f"no such session: {cid}"})
            return
        client.cid = cid
        s.subscribe(client)

    def broadcast_all(self, obj):
        with self.clients_lock:
            targets = list(self.all_clients)
        for c in targets:
            c.send_json(obj)

    def broadcast_projects(self):
        self.broadcast_all({"type": "projects", "projects": self.projects_meta()})

    def broadcast_sessions(self):
        self.broadcast_all({"type": "sessions",
                            "sessions": self.sessions_meta(),
                            "current": self.default_cid()})
        self.broadcast_projects()                # session counts changed
        if self.restart_pending:                 # refresh the pending banner's busy count…
            self.broadcast_restart()
            self._maybe_restart()                # …and fire if the last turn just ended

    def shutdown(self):
        with self.lock:
            for s in self.sessions.values():
                s.shutdown()


# ── AI naming (title + one-line description via Bankr LLM gateway) ─────────────
def _llm_json(sys_prompt, user_text, max_tokens=120):
    """POST one (system, user) turn to the configured gateway and return the
    parsed JSON object the model emitted, or None if naming is unconfigured, the
    call fails, or no JSON is found. Stdlib-only HTTP — the single transport both
    generate_name and generate_digest share (one place handles the
    openai/anthropic/bankr body+auth differences; no drift)."""
    if not (BANKR_API_KEY and BANKR_BASE_URL):
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
            body = {"model": BANKR_MODEL, "max_tokens": max_tokens, "temperature": 0.3,
                    "messages": [{"role": "system", "content": sys_prompt},
                                 {"role": "user", "content": user_text}]}
            if BANKR_API == "bankr":
                headers = {"X-API-Key": BANKR_API_KEY, "content-type": "application/json"}
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
        m = re.search(r"\{[\s\S]*\}", raw)
        return json.loads(m.group(0)) if m else None
    except Exception as e:
        print(f"[llm] generation failed: {e}", flush=True)
        return None


def generate_name(transcript_text):
    """Return (title, desc, tab) for a coding session, or (None, None, None)
    if naming is unconfigured or the call fails."""
    parsed = _llm_json(NAME_SYS_PROMPT, transcript_text)
    if not parsed:
        return (None, None, None)
    return (parsed.get("title"), parsed.get("desc"), parsed.get("tab"))


def generate_digest(transcript_text):
    """Return (digest, blocked_on) — the volatile live-state summary — or
    (None, None) if naming is unconfigured or the call fails. See
    DIGEST_SYS_PROMPT and docs/CONTROLLER.md."""
    parsed = _llm_json(DIGEST_SYS_PROMPT, transcript_text)
    if not parsed:
        return (None, None)
    return (parsed.get("digest"), parsed.get("blocked_on"))


# ── project emoji codes (1–3 emoji identity badge via the same gateway) ───────
def _clean_emoji(raw):
    """Sanitize a model-emitted emoji code: strip anything that isn't emoji
    machinery (letters, digits, punctuation, whitespace — models love to add
    prose), cap it at a tab-friendly width. Returns "" when nothing survives."""
    if not isinstance(raw, str):
        return ""
    JOINERS = {"\u200d", "\ufe0f", "\u20e3"}  # ZWJ / VS16 / keycap — glue, not glyphs
    kept = [c for c in raw.strip()
            if c in JOINERS or ord(c) >= 0x2190]  # arrows block onward = symbols/emoji; ASCII+latin drop out
    # cap: at most 4 visible glyph units (a flag is two regional indicators but
    # reads as one — close enough) and 12 codepoints total (ZWJ families are long)
    out, visible = [], 0
    for c in kept:
        if len(out) >= 12:
            break
        if c not in JOINERS:
            visible += 1
            if visible > 4:
                break
        out.append(c)
    while out and out[-1] in JOINERS:            # never end on dangling glue…
        out.pop()
    while out and out[0] in JOINERS:             # …or start on it (a keycap whose digit we stripped)
        out.pop(0)
    return "".join(out)


def _emoji_context(project, session_titles=()):
    """What the model sees when badging a project: name, remote, README head,
    file listing, recent commit subjects, plus this project's session titles
    (often the sharpest signal of what actually happens in it). Returns None
    while the repo is too thin to say anything about itself — the sweep retries
    once it has matured. Local (private) projects still get badged: the context
    is built here and only ever leaves as an LLM prompt, same as session naming."""
    path = project.path
    def run(*cmd):
        try:
            r = subprocess.run(cmd, cwd=path, capture_output=True, text=True,
                               timeout=5)
            return r.stdout if r.returncode == 0 else ""
        except Exception:
            return ""
    files = [l for l in run("git", "ls-files").splitlines() if l.strip()]
    if not files:                                # non-git local folder → directory listing
        try:
            files = sorted(os.listdir(path))[:200]
        except OSError:
            files = []
    readme = ""
    for cand in ("README.md", "readme.md", "README.rst", "README.txt", "README"):
        try:
            readme = (Path(path) / cand).read_text(errors="replace")[:1500]
            break
        except OSError:
            continue
    # Maturity gate: a just-created repo (bare README stub, no real files) says
    # nothing yet — let it grow before spending a badge on it.
    if len(files) < EMOJI_MIN_FILES and len(readme) < EMOJI_MIN_README:
        return None
    log = run("git", "log", "--oneline", "-12", "--no-color")
    parts = [f"Project name: {project.name}"]
    if project.repo_url:
        parts.append(f"Repo: {project.repo_url}")
    if readme:
        parts.append("README (head):\n" + readme)
    if files:
        parts.append("Files:\n" + "\n".join(files[:80]))
    if log:
        parts.append("Recent commits:\n" + log[:800])
    titles = [t for t in session_titles if t][:8]
    if titles:
        parts.append("Recent session titles in this project:\n" + "\n".join(titles))
    return "\n\n".join(parts)[:6000]


def generate_project_emoji(context_text, taken=()):
    """Return a sanitized 1–3 emoji code for a project, or "" if the gateway is
    unconfigured / the call fails / nothing usable comes back. `taken` = codes
    already worn by sibling projects; the model is told to avoid them and a
    collision gets one stricter retry."""
    avoid = [t for t in taken if t]
    for attempt in (0, 1):
        text = context_text
        if avoid:
            text += ("\n\nAlready taken (do NOT reuse any of these): "
                     + " , ".join(avoid))
        if attempt:
            text += "\nYour previous answer collided with a taken code — pick something clearly different."
        parsed = _llm_json(EMOJI_SYS_PROMPT, text)
        code = _clean_emoji((parsed or {}).get("emoji", ""))
        if code and code not in avoid:
            return code
        if not code:
            return ""                            # gateway off / junk reply — don't burn a retry
    return code                                  # collided twice — accept; refresh will re-roll later


def _strip_noise(text):
    """Drop harness boilerplate that shouldn't show as a user message."""
    text = re.sub(r"<local-command-caveat>[\s\S]*?</local-command-caveat>", "", text)
    text = re.sub(r"<system-reminder>[\s\S]*?</system-reminder>", "", text)
    text = re.sub(r"</?command-(message|name|args)>", "", text)
    return text


def _collect_text(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = [b.get("text", "") for b in content
             if isinstance(b, dict) and b.get("type") == "text"]
    return "".join(parts).strip()


def _collect_tool_uses(content):
    if not isinstance(content, list):
        return []
    out = []
    for b in content:
        if isinstance(b, dict) and b.get("type") == "tool_use":
            out.append({"name": b.get("name"), "input": b.get("input")})
    return out


def _collect_tool_results(content):
    if not isinstance(content, list):
        return []
    out = []
    for b in content:
        if isinstance(b, dict) and b.get("type") == "tool_result":
            c = b.get("content")
            out.append(c if isinstance(c, str) else _collect_text(c))
    return out


MGR = SessionManager()


# ── WebSocket framing (RFC 6455) — from clawd-web-claude/server.py ─────────────
def ws_send(wfile, lock, data, opcode=0x1):
    payload = data.encode("utf-8") if isinstance(data, str) else data
    header = bytearray([0x80 | opcode])
    n = len(payload)
    if n < 126:
        header.append(n)
    elif n < 65536:
        header.append(126)
        header += struct.pack(">H", n)
    else:
        header.append(127)
        header += struct.pack(">Q", n)
    with lock:
        wfile.write(bytes(header) + payload)
        wfile.flush()


def ws_read_message(rfile):
    payload = b""
    msg_opcode = None
    while True:
        hdr = rfile.read(2)
        if len(hdr) < 2:
            return None
        b0, b1 = hdr[0], hdr[1]
        fin = b0 & 0x80
        opcode = b0 & 0x0F
        masked = b1 & 0x80
        length = b1 & 0x7F
        if length == 126:
            ext = rfile.read(2)
            if len(ext) < 2:
                return None
            length = struct.unpack(">H", ext)[0]
        elif length == 127:
            ext = rfile.read(8)
            if len(ext) < 8:
                return None
            length = struct.unpack(">Q", ext)[0]
        mask = rfile.read(4) if masked else b""
        chunk = rfile.read(length) if length else b""
        if masked and chunk:
            chunk = bytes(chunk[i] ^ mask[i % 4] for i in range(len(chunk)))
        if opcode == 0x8:
            return ("close", chunk)
        if opcode == 0x9:
            return ("ping", chunk)
        if opcode == 0xA:
            return ("pong", chunk)
        if opcode != 0x0:
            msg_opcode = opcode
        payload += chunk
        if fin:
            return (msg_opcode or 0x1, payload)


class _Client:
    """A connected browser. Owns its send lock so broadcasts are thread-safe.
    `cid` is the session it's currently subscribed to (None until it focuses)."""
    def __init__(self, wfile):
        self.wfile = wfile
        self.lock = threading.Lock()
        self.dead = False
        self.cid = None
        self.tty_size = None    # (cols, rows) this viewer last fit to — its size claim
        self.tty_ts = 0.0       # when; recency picks the fallback owner

    def send_bytes(self, data: bytes):
        if self.dead:
            return
        try:
            ws_send(self.wfile, self.lock, data, opcode=0x2)  # binary = PTY bytes
        except Exception:
            self.dead = True

    def send_json(self, obj):
        if self.dead:
            return
        try:
            ws_send(self.wfile, self.lock, json.dumps(obj), opcode=0x1)
        except Exception:
            self.dead = True


# ── HTTP + WS handler ──────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # quiet; the session logs what matters

    def _is_ws_upgrade(self):
        return (self.headers.get("Upgrade", "").lower() == "websocket"
                and "upgrade" in self.headers.get("Connection", "").lower())

    def _query(self):
        from urllib.parse import urlparse, parse_qs
        return parse_qs(urlparse(self.path).query)

    def _token_ok(self):
        # Loopback bind ⇒ no auth (see AUTH_REQUIRED): only local processes can
        # reach us, so the token is moot — every request passes.
        if not AUTH_REQUIRED:
            return True
        # Constant-time: avoid a byte-by-byte timing oracle on the token. (== on
        # str short-circuits at the first mismatch.) compare_digest raises on
        # non-ASCII, so guard the types.
        try:
            return hmac.compare_digest(self._query().get("t", [""])[0], TOKEN)
        except (TypeError, ValueError):
            return False

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/ws" and self._is_ws_upgrade():
            if not self._token_ok():
                return self.send_error(403, "bad token")
            return self.handle_ws()
        if path in ("/", "/index.html"):
            # page loads without a token; it just can't open the WS without one
            return self._serve_file(HERE / "index.html", "text/html; charset=utf-8")
        if path in ("/favicon.png", "/favicon.ico"):
            return self._serve_file(HERE / "favicon.png", "image/png")
        if path == "/logo.png":
            return self._serve_file(HERE / "logo.png", "image/png")
        if path == "/logo-ui.png":
            return self._serve_file(HERE / "logo-ui.png", "image/png")
        if path == "/manifest.webmanifest":
            return self._serve_manifest()
        if path == "/sw.js":
            return self._serve_file(HERE / "sw.js", "text/javascript; charset=utf-8")
        if path in ("/icon-180.png", "/icon-192.png", "/icon-512.png"):
            return self._serve_file(HERE / path.lstrip("/"), "image/png")
        if path == "/pm" or path.startswith("/pm/"):
            return self._proxy_pm("GET")
        if path == "/config":
            # Token-gated: it leaks workdir / lanIp / sessionId, which a malicious
            # site could grab via DNS-rebinding if this were open. The page sends
            # the token it already holds (?t=). No token is returned in the body.
            if not self._token_ok():
                return self.send_error(403, "bad token")
            cur = MGR.get(MGR.default_cid())
            return self._serve_json({
                "sessionId": cur.session_id if cur else None,
                "workdir": WORKDIR,
                "sessions": len(MGR.sessions),
                "lanIp": lan_ip(),
                "port": PORT,
                "tts": bool(ELEVENLABS_API_KEY),   # browser uses ElevenLabs when true
            })
        self.send_error(404, "not found")

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/hook":
            return self._handle_hook()
        if path == "/upload":
            return self._handle_upload()
        if path == "/tts":
            return self._handle_tts()
        if path.startswith("/pm/"):
            return self._proxy_pm("POST")
        self.send_error(404, "not found")

    def _proxy_pm(self, method):
        """Reverse-proxy /pm/* → the controller (sibling process on CONTROLLER_PORT)
        so the PM chat + debug live on this one origin. The controller stays a
        separate process; the browser never sees its port. 502 if it's down."""
        import urllib.error
        sub = self.path[len("/pm"):] or "/"
        url = f"http://127.0.0.1:{CONTROLLER_PORT}{sub}"
        body = None
        headers = {}
        if method == "POST":
            n = int(self.headers.get("Content-Length", "0") or 0)
            body = self.rfile.read(n) if n else b""
            ct = self.headers.get("Content-Type")
            if ct:
                headers["Content-Type"] = ct
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                data, ctype, code = r.read(), r.headers.get("Content-Type", "application/octet-stream"), r.status
        except urllib.error.HTTPError as e:
            data, ctype, code = e.read(), e.headers.get("Content-Type", "application/json"), e.code
        except Exception as e:
            data = json.dumps({"error": f"controller unreachable: {e}"}).encode()
            ctype, code = "application/json", 502
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _handle_tts(self):
        """Proxy a chunk of prose to ElevenLabs and stream the MP3 back. Keeps the
        API key server-side; the browser plays the audio it gets in return."""
        if not self._token_ok():
            return self.send_error(403, "bad token")
        if not ELEVENLABS_API_KEY:
            return self.send_error(503, "tts not configured")
        try:
            n = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(n)) if n else {}
            text = (body.get("text") or "").strip()[:4000]
        except Exception:
            text = ""
        if not text:
            return self.send_error(400, "empty text")
        url = (f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}/stream"
               "?optimize_streaming_latency=3&output_format=mp3_44100_64")
        req_body = json.dumps({
            "text": text,
            "model_id": "eleven_flash_v2_5",
            "voice_settings": {"stability": 0.65, "similarity_boost": 0.5,
                               "use_speaker_boost": True, "speed": 1.2},
        }).encode()
        req = urllib.request.Request(url, data=req_body, method="POST", headers={
            "xi-api-key": ELEVENLABS_API_KEY,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        })
        # Stream upstream chunks straight through (read-until-EOF: no Content-Length,
        # Connection: close) so first audio bytes reach the client ASAP.
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                self.send_response(200)
                self.send_header("Content-Type", "audio/mpeg")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                while True:
                    chunk = resp.read(2048)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:200]
            self.send_error(502, f"elevenlabs {e.code}: {detail}")
        except Exception as e:
            try:
                self.send_error(502, f"tts upstream error: {e}")
            except Exception:
                pass

    def _handle_hook(self):
        if not self._token_ok():
            return self.send_error(403, "bad token")
        cid = self._query().get("cid", [""])[0]
        try:
            n = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(n) if n else b""
            obj = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            obj = {}
        if obj:
            s = MGR.get(cid)
            if s:
                s.on_hook(obj)
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _handle_upload(self):
        """Save a pasted/dropped file (image or text-ish doc) to the workdir and
        return its path so the browser can fold it into the next message (claude
        reads it via Read). The original filename rides as a `name=` param on the
        Content-Type header — the one header the fleet bridge (relay → worker)
        already forwards verbatim, so it needs no fleet protocol change."""
        if not self._token_ok():
            return self.send_error(403, "bad token")
        n = int(self.headers.get("Content-Length", "0"))
        if n <= 0 or n > MAX_UPLOAD:
            return self.send_error(413, "bad size")
        parts = [p.strip() for p in self.headers.get("Content-Type", "image/png").split(";")]
        ctype = parts[0]
        fname = ""
        for p in parts[1:]:
            if p.lower().startswith("name="):
                fname = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(p[5:].strip('"')).name).lstrip(".")[:80]
        ext = EXT_BY_CTYPE.get(ctype, ".png")
        data = self.rfile.read(n)
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        name = f"paste-{uuid.uuid4().hex[:8]}-{fname}" if fname else f"paste-{uuid.uuid4().hex[:8]}{ext}"
        dest = UPLOAD_DIR / name
        dest.write_bytes(data)
        print(f"[upload] {n} bytes -> {dest}", flush=True)
        self._serve_json({"path": str(dest), "name": name})

    def _serve_file(self, path, ctype):
        try:
            body = Path(path).read_bytes()
        except OSError:
            self.send_error(404, "not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")   # always serve fresh UI
        self.end_headers()
        self.wfile.write(body)

    def _serve_json(self, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_manifest(self):
        # PWA manifest, served dynamically so direct mode can bake the token into
        # start_url — an installed home-screen icon then authenticates the WS on a
        # LAN bind (loopback ignores ?t=, so it's harmless there). The relay serves
        # its OWN bare-start_url manifest (the passkey is the sole credential in
        # fleet mode); see fleet/relay.py. start_url stays same-origin/same-scope so
        # the launched window is treated as the installed app, not a browser tab.
        start = f"/?t={TOKEN}" if TOKEN else "/"
        man = {
            "name": "clawd-harness", "short_name": "clawd",
            "description": "Drive interactive Claude Code sessions from your phone.",
            "start_url": start, "scope": "/", "display": "standalone",
            "orientation": "portrait-primary",
            "background_color": "#000000", "theme_color": "#000000",
            "icons": [
                {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
                {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"},
                {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png",
                 "purpose": "maskable"},
            ],
        }
        body = json.dumps(man).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/manifest+json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def handle_ws(self):
        key = self.headers.get("Sec-WebSocket-Key", "")
        accept = base64.b64encode(
            hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.close_connection = True

        client = _Client(self.wfile)
        MGR.add_client(client)
        print("[ws] client connected", flush=True)
        try:
            while True:
                try:
                    msg = ws_read_message(self.rfile)
                except Exception:
                    break
                if msg is None:
                    break
                kind, data = msg
                if kind == "close":
                    break
                if kind == "ping":
                    try:
                        ws_send(self.wfile, client.lock, data, opcode=0xA)
                    except Exception:
                        break
                    continue
                if kind == "pong":
                    continue
                # data frame: control JSON from the browser
                try:
                    frame = json.loads(data.decode("utf-8"))
                except Exception:
                    continue
                self._dispatch(client, frame)
        finally:
            MGR.remove_client(client)
            print("[ws] client disconnected", flush=True)

    def _dispatch(self, client, frame):
        t = frame.get("type")
        if t == "ping":
            # App-level liveness probe. A browser can't read native WS pong from JS,
            # so the client pings over this channel to prove the FULL path is live
            # (in fleet: browser→relay→worker→harness and back, exercising the e2e
            # channel). A returned pong lets the client repaint in place instead of
            # tearing the socket down + resetting the terminal on every tab-switch.
            client.send_json({"type": "pong", "id": frame.get("id")})
            return
        if t == "subscribe":
            MGR.subscribe_client(client, frame.get("cid"))
        elif t == "list":
            client.send_json({"type": "projects", "projects": MGR.projects_meta()})
            client.send_json({"type": "sessions",
                              "sessions": MGR.sessions_meta(),
                              "current": MGR.default_cid()})
        elif t == "new":
            s = MGR.create_session(frame.get("pid"),
                                   account=frame.get("account"))
            if s:
                client.send_json({"type": "focus", "cid": s.cid})
        elif t == "accountAdd":
            s = MGR.add_account(frame.get("name", ""))
            if s:
                client.send_json({"type": "focus", "cid": s.cid})
        elif t == "accountUse":
            MGR.use_account(frame.get("name", ""))
        elif t == "accountRemove":
            MGR.remove_account(frame.get("name", ""))
        elif t == "accountsRefresh":
            MGR.refresh_accounts()
        elif t == "close":
            MGR.close(frame.get("cid"))
        elif t == "pin":
            MGR.pin(frame.get("cid"), bool(frame.get("on", True)))
        elif t == "createProject":
            MGR.create_project(frame.get("name", ""))
        elif t == "addProject":
            MGR.add_project(frame.get("repoUrl", ""))
        elif t == "addLocalProject":
            _, lerr = MGR.add_local_project(frame.get("path", ""))
            if lerr:
                client.send_json({"type": "error",
                                  "error": f"addLocalProject: {lerr}"})
        elif t == "removeProject":
            MGR.remove_project(frame.get("pid"))
        elif t == "restart":
            MGR.request_restart(frame.get("reason") or "manual")
        elif t == "restartCancel":
            MGR.cancel_restart()
        elif t in ("input", "send", "resize"):
            s = MGR.get(frame.get("cid") or client.cid)
            if not s:
                return
            if t == "input":
                s.bump_owner(client)             # driving a session claims its size
                s.write(frame.get("data", "").encode("utf-8"))
            elif t == "send":
                txt = frame.get("text", "")
                print(f"[ws {s.cid[:8]}] send: {txt[:60]!r}", flush=True)
                s.bump_owner(client)
                log_prompt(s, txt, frame.get("via", ""))
                s.send_message(txt)
            elif t == "resize":
                s.claim_resize(client, frame.get("cols"), frame.get("rows"),
                               bool(frame.get("claim")))


class ThreadingHTTPServer(ThreadingMixIn, TCPServer):
    daemon_threads = True
    allow_reuse_address = True


# --- live-reload: watch the UI file and tell open browsers to refresh ---------
# The harness's whole point is live-editing itself, but an open page has no way
# to know index.html changed on disk (it's served fresh, yet nothing pings the
# tab). Poll its mtime and broadcast a `reload` so self-edits show up instantly.
WATCH_FILES = [HERE / "index.html"]               # served fresh → just reload browsers
# Boot-time files: their changes only take effect on a fresh process, so a disk
# change flags a *graceful restart* (waits for all sessions idle) rather than a
# browser reload. This is what makes "live-edit the harness" safe.
RESTART_FILES = [Path(__file__).resolve(), HERE / ".clawd-harness.env"]

def auto_update_loop():
    """Fleet-wide deploy = `git push`. Every harness polls origin/main and
    fast-forwards itself when it's cleanly behind — the RESTART_FILES watcher
    then gracefully restarts into the new server.py (waiting for idle turns),
    and a changed index.html hot-reloads every open browser. Guards: only on
    the main branch, only with a clean worktree (a box being live-edited is
    skipped until its work is committed or dropped), ff-only (a diverged box
    never gets rewritten). Disable per box with AUTO_PULL=0."""
    if os.environ.get("AUTO_PULL", "1") == "0":
        return
    import random
    interval = float(os.environ.get("AUTO_PULL_INTERVAL", "300"))

    def git(*args, timeout=30):
        return subprocess.run(["git", "-C", str(HERE)] + list(args),
                              capture_output=True, text=True, timeout=timeout)

    while True:
        time.sleep(interval + random.uniform(0, min(interval, 60)))  # jitter: don't stampede origin
        try:
            if git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip() != "main":
                continue
            # Tracked modifications = a live-edit in progress → hands off.
            # Untracked files (logs, scratch) don't block: an ff merge never
            # touches them, and a genuine path collision just fails the merge
            # cleanly below.
            if git("status", "--porcelain", "--untracked-files=no").stdout.strip():
                continue
            if git("fetch", "--quiet", "origin", "main", timeout=60).returncode != 0:
                continue                                 # offline — try again next tick
            behind = git("rev-list", "--count", "HEAD..origin/main").stdout.strip()
            if behind in ("", "0"):
                continue
            r = git("merge", "--ff-only", "origin/main", timeout=60)
            if r.returncode == 0:
                print(f"[autoupdate] pulled {behind} commit(s) from origin/main "
                      "— watcher will reload/restart as needed", flush=True)
            else:
                print(f"[autoupdate] ff-only merge refused: "
                      f"{(r.stderr or r.stdout).strip()[-120:]}", flush=True)
        except Exception as e:
            print(f"[autoupdate] {e}", flush=True)


def watch_ui():
    last = {}
    for f in WATCH_FILES + RESTART_FILES:
        try: last[f] = f.stat().st_mtime
        except OSError: last[f] = 0
    while True:
        time.sleep(1.0)
        if MGR.reconcile_projects():             # project list follows disk
            MGR.broadcast_projects()
        MGR.emoji_sweep()                        # self-throttled; badges one project at a time
        # Background-work sweep: does claude have shells/agents still running
        # behind an idle prompt? (poll_bg reads claude's own status file.)
        if any([s.poll_bg() for s in list(MGR.sessions.values())]):
            MGR.broadcast_sessions()
        for f in WATCH_FILES:
            try: m = f.stat().st_mtime
            except OSError: continue
            if m != last[f]:
                last[f] = m
                print(f"[watch] {f.name} changed → reloading browsers", flush=True)
                MGR.broadcast_all({"type": "reload"})
        for f in RESTART_FILES:
            try: m = f.stat().st_mtime
            except OSError: continue
            if m != last[f]:
                last[f] = m
                MGR.request_restart(f"{f.name} changed")


def raise_fd_limit(target=10240):
    """launchd defaults the soft NOFILE limit to 256 — with an fd per ws client,
    transcript tail, PTY, and project scan, a slow client leak (e.g. a buggy fleet
    worker holding zombie links) starves us into Errno 24 and the machine looks
    dead. Raise the soft limit up front; best-effort (never fatal)."""
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        want = target if hard == resource.RLIM_INFINITY else min(target, hard)
        if soft < want:
            resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
    except Exception:
        pass


def main():
    raise_fd_limit()
    MGR.load()
    threading.Thread(target=watch_ui, daemon=True).start()
    threading.Thread(target=MGR.poll_accounts_loop, daemon=True).start()
    threading.Thread(target=auto_update_loop, daemon=True).start()
    srv = ThreadingHTTPServer((BIND, PORT), Handler)
    ip = lan_ip()
    print(f"[http] clawd-harness ({'token required' if AUTH_REQUIRED else 'no auth — loopback only'})", flush=True)
    print(f"[http]   workdir : {WORKDIR}", flush=True)
    print(f"[http]   sessions: {len(MGR.sessions)}", flush=True)
    if AUTH_REQUIRED:
        print(f"[http]   local : http://127.0.0.1:{PORT}/?t={TOKEN}", flush=True)
        print(f"[http]   phone : http://{ip}:{PORT}/?t={TOKEN}", flush=True)
    else:
        print(f"[http]   local : http://127.0.0.1:{PORT}/   (no token)", flush=True)
    if not (BANKR_API_KEY and BANKR_BASE_URL):
        print("[http]   note  : AI naming off (set BANKR_API_KEY + BANKR_BASE_URL "
              "to enable); using first-prompt titles.", flush=True)
    if BIND == "127.0.0.1":
        print("[http]   bind  : 127.0.0.1 (localhost only) — remote access via the "
              "fleet (relay + passkey + E2E). Set BIND=0.0.0.0 for direct LAN access.", flush=True)
    else:
        print(f"[http]   ⚠ bind {BIND}: reachable beyond localhost with "
              "bypass-permissions — the token is the only thing gating command "
              "execution. Prefer BIND=127.0.0.1 + the fleet for remote access.", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[http] shutting down", flush=True)
    finally:
        MGR.shutdown()
        srv.server_close()


if __name__ == "__main__":
    main()
