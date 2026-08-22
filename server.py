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
# Second engine: a session is either `claude` or `codex` (see the Engine layer
# below and docs/CODEX-ENGINE.md). CODEX_STORE is codex's CODEX_HOME — its
# config, credentials AND rollout transcripts all live there, so it doubles as
# the transcript root. One login per machine in v1 (no multi-account routing:
# codex exposes no pollable usage endpoint — see EXPECTATIONS.md's scope note).
CODEX_BIN   = os.environ.get("CODEX_BIN", "codex")
CODEX_STORE = os.path.abspath(os.path.expanduser(
    os.environ.get("CODEX_HOME", "~/.codex")))
# Approval posture for codex sessions. Claude sessions inherit the user's
# bypass-permissions settings; codex needs it stated explicitly or every tool
# call blocks the turn on an approval prompt no browser client can answer.
CODEX_SANDBOX  = os.environ.get("CODEX_SANDBOX", "danger-full-access")
CODEX_APPROVAL = os.environ.get("CODEX_APPROVAL", "never")
# Run our own generated hooks without codex's per-handler trust ceremony —
# see CodexEngine.argv. CODEX_BYPASS_HOOK_TRUST=0 opts out (and accepts that
# a codex session then has no turn signal until someone answers the prompt).
CODEX_BYPASS_HOOK_TRUST = os.environ.get("CODEX_BYPASS_HOOK_TRUST", "1") != "0"
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
# Controller read-query bounds (the `search` / `transcriptTail` / `screen` WS
# frames — docs/WS-PROTOCOL.md). Every reply must fit an LLM tool-output budget,
# so the caps are hard: clamps, per-session hit limits, and a wall-clock stop.
SEARCH_LIMIT_MAX   = 40                # matches per search reply
SEARCH_TAIL_BYTES  = 2 * 1024 * 1024   # per-transcript read window (tail of file)
SEARCH_PER_SESSION = 3                 # transcript hits kept per session (newest first)
SEARCH_SNIPPET     = 160               # chars of context per hit
SEARCH_BUDGET_S    = 5.0               # wall-clock stop → truncated:true
TAIL_EVENTS_MAX    = 50                # transcriptTail: max events
TAIL_CHARS_MAX     = 2000              # transcriptTail: per-text-field cap
TAIL_REPLY_BYTES   = 16 * 1024         # transcriptTail: whole-reply cap
SCREEN_CHARS_MAX   = 4000              # screen: cap on returned text
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
# The *test hint* is what the 📌 pin board is for. Pinning means "this work looks
# done but nobody has verified it" — so the moment a session lands on the board
# we ask the LLM one question: what does the HUMAN have to go do to prove it?
# One imperative line ("run a stream with three guests and watch for choppy
# video"), rendered blue on the pin card, so a board of parked to-dos reads as a
# testing checklist instead of a pile of titles you have to re-open to decode.
# Durable (persisted, unlike the digest): a restart must not blank the board.
TEST_SYS_PROMPT = ("A developer just parked this software-engineering session on "
                   "a \"test it later\" board: the coding looks done, but a HUMAN "
                   "still has to verify it by hand. Given the transcript, reply "
                   "with ONLY compact JSON and nothing else: "
                   '{"test": "<max 16 words: the manual check the human must '
                   'perform>"}. '
                   "Write ONE imperative instruction naming the concrete thing to "
                   "open, run or look at AND what would prove it worked — e.g. "
                   '"run a stream with three guests and watch for choppy '
                   'video/audio". Describe only what a PERSON does outside this '
                   "session (open the app, click the thing, watch the output); "
                   "never say \"ask claude\" and never describe more coding. If "
                   "the session has produced nothing verifiable yet, reply with an "
                   "empty string.")
# The hint is a slightly harder ask than naming (it has to reason about what
# would falsify the work), so it gets its own model knob — default: whatever
# names sessions, so an unconfigured harness changes nothing.
TEST_MODEL = os.environ.get("TEST_HINT_MODEL", "") or ""
# Pinning also COMPACTS. A pin means "I'm done driving this for now, come back
# after testing" — and the thing that decides whether coming back is pleasant is
# how much context window is left. Compacting at the moment of parking is free
# (nobody is waiting on the session) and buys the return trip a full window
# instead of an immediate auto-compact mid-thought. Sent as a real TUI slash
# command once the session goes idle: PIN_COMPACT_WAIT bounds how long we hold
# the door for a turn that's still finishing. PIN_COMPACT=0 opts out.
PIN_COMPACT      = os.environ.get("PIN_COMPACT", "1") != "0"
PIN_COMPACT_WAIT = float(os.environ.get("PIN_COMPACT_WAIT", "900"))   # s to wait for idle
# Auto-TLDR (2026-08-16): you prompt a session from a browser, walk away, and
# come back to a wall of text — wishing someone had tapped the "tldr" chip
# while you were gone. So the harness does: when a turn ends with a long reply
# and NOBODY is subscribed to the session, it sends the chip's prompt itself.
# Three fences keep it from running away: it's armed only by a BROWSER send
# (the frame's `via` tag — controller/pipeline prompts never carry one, so
# PM-orchestrated sessions are untouched and pipeline chaining can't be
# corrupted by an injected turn); the arm is CONSUMED at the next Stop, so
# one human prompt buys at most one auto-tldr and the tldr turn itself can
# never re-trigger; and anyone actually watching (a live subscriber, checked
# again after a short grace) suppresses it. AUTO_TLDR=0 opts the box out.
AUTO_TLDR       = os.environ.get("AUTO_TLDR", "1") != "0"
AUTO_TLDR_TEXT  = os.environ.get("AUTO_TLDR_TEXT",
                                 "TLDR, use simple plain english and as few "
                                 "words as possible. NO SLOP.")
AUTO_TLDR_MIN   = int(os.environ.get("AUTO_TLDR_MIN", "350"))   # shorter never fires
AUTO_TLDR_LONG  = int(os.environ.get("AUTO_TLDR_LONG", "900"))  # one-paragraph wall
AUTO_TLDR_DELAY = float(os.environ.get("AUTO_TLDR_DELAY", "3")) # grace before sending


def wants_auto_tldr(text):
    """Is this reply a 'wall of text'? More than one real paragraph (and long
    enough that a summary buys anything), or a single monster paragraph. Pure
    so test_auto_tldr.py can pin the thresholds."""
    t = (text or "").strip()
    if len(t) < AUTO_TLDR_MIN:
        return False
    paras = [p for p in re.split(r"\n\s*\n", t) if p.strip()]
    return len(paras) >= 2 or len(t) >= AUTO_TLDR_LONG
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
# Project kinds: gh = a clawdbotatg repo (create/clone), local = a private
# folder registered in place, external = SOMEONE ELSE'S GitHub repo — forked
# when we lack push access, cloned when we have it; sessions in it are born
# with a standing branch-and-PR rule (Project.standing_rule) and its default
# branch is fast-forwarded from upstream at every spawn. An external project
# lives under projects/ like a gh one, so it keeps the delete-the-folder
# removal contract.
PROJECT_KINDS = ("gh", "local", "external")
# How long a spawn into an external project waits for `git fetch upstream` +
# the ff merge before giving up and starting the session anyway (stale beats
# no session). The fetch runs synchronously in the spawn path on purpose: a
# session that starts working while the fetch is still in flight is the exact
# "starts stale" the sync exists to prevent.
EXTERNAL_SYNC_TIMEOUT = float(os.environ.get("EXTERNAL_SYNC_TIMEOUT", "25"))
# 0 opts out of the spawn-time sync (the standing rule still tells the agent to
# fetch upstream before branching, so it degrades to "the agent syncs").
EXTERNAL_SYNC = os.environ.get("EXTERNAL_SYNC", "1") != "0"
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
# NOTE: the harness deliberately has NO token-endpoint client anymore — all
# rotation goes through the real claude CLI (_ping_rotate). Families rotated
# only by direct token-endpoint calls were server-side expired ~4 weeks after
# /login (2026-08-07 postmortem: sub3, sub4).
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
# How old a COOL reading may be and still be routed to when every FRESH pool
# is hot. Freshness is a ranking tier, not a filter (2026-08-22, heart): the
# 3×TTL filter alone collapsed the candidate set to the fresh-but-dead pools
# and spawned onto a 100% plan while the one pool with headroom sat at 83%
# on a 2h-old reading — stale only because its idle sessions hold the grant
# (single-consumer rule) and never renewed the access token. Root cause v3's
# symptom, a different cause. A session landing on the stale pool renews
# the token, so the reading heals itself.
USAGE_STALE_TRUST = float(os.environ.get("USAGE_STALE_TRUST", "43200"))
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
# ── on-demand routing (2026-08-19) ───────────────────────────────────────────
# Rollout stages 1–2 of docs/fleet/ON-DEMAND-SUB-ROUTING-PLAN.md: pick the
# account at the moment a prompt needs a model, instead of eagerly re-parking
# idle sessions between pools. Stage 1 (the `prompt_route` decision log) is
# always on — every browser/auto prompt logs what preflight decided, so a box
# can be read for a while before anyone flips the switch. ROUTE_ON_PROMPT=1
# makes the decisions real: a prompt whose session sits on a worse pool
# triggers a single-session handoff and delivers exactly once after the
# replacement is READY (SessionStart fired AND the resume gate resolved — the
# addenda race: delivering on "started" alone can type the prompt into the
# CLI's numbered resume modal and disarm the very scan that answers it).
# WAIT bounds that readiness barrier; on timeout the delivery proceeds and
# logs — the bound must never strand a prompt. SETTLE is the same paint pause
# every rescue redelivery takes between SessionStart and typing.
# The eager sweep and post-Stop movement still run — removing them is rollout
# stage 4+, after this path has soaked (see the plan's Rollout section).
SUB_ROUTE_ON_PROMPT = os.environ.get("SUB_ROUTE_ON_PROMPT", "0") == "1"
SUB_ROUTE_WAIT   = float(os.environ.get("SUB_ROUTE_WAIT", "25"))    # s
SUB_ROUTE_SETTLE = float(os.environ.get("SUB_ROUTE_SETTLE", "2"))   # s
# After a session's claude exits, its final token rotation may still be
# settling (or, if it was killed mid-refresh, stranded server-side) — the
# poller must not consume that account's refresh grant until the dust
# settles. Replaying a superseded refresh token REVOKES THE WHOLE FAMILY
# (how sub3 died 2026-08-06; ef/sub2 died the same way via VM custody).
SUB_REFRESH_EXIT_GRACE = float(os.environ.get("SUB_REFRESH_EXIT_GRACE", "900"))  # s
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
# ── model-capability gate (2026-08-09) ───────────────────────────────────────
# Headroom is not the only thing that makes a pool usable: a plan can be
# BILLED for a narrower set of models than the fleet actually works on. The
# slop@buidlguidl.com org changed plans on 2026-08-09 and stopped carrying
# Fable — sessions routed there kept running (Opus only) but silently lost the
# model the work is done on. So capacity routing gets a prerequisite: a pool
# the fleet can't do its job on is not a cool pool, it's an unusable one.
#
# The signal is the usage payload's model-scoped weekly windows. A plan that
# CARRIES fable advertises a '7d fable' window from 0% used (verified: sub3 at
# 0.0%), so absence is entitlement, not merely non-use. It stays a heuristic on
# an undocumented endpoint, so it degrades in the safe direction — see
# _fable_state: only a GOOD reading that positively lacks the window blocks a
# pool, unknown never does, and the router never strands itself (if the gate
# would empty the roster it is ignored for that decision and logged).
SUB_REQUIRE_FABLE = os.environ.get("SUB_REQUIRE_FABLE", "1") != "0"
# Manual overrides for when the endpoint lies in either direction — comma-
# separated ACCOUNT names (not orgs). SUB_NO_FABLE blocks a pool the payload
# still flatters; SUB_FABLE_OK re-admits one the heuristic wrongly convicts
# (the escape hatch that keeps a bad guess from costing you the whole fleet).
SUB_NO_FABLE = {n.strip() for n in
                os.environ.get("SUB_NO_FABLE", "").split(",") if n.strip()}
SUB_FABLE_OK = {n.strip() for n in
                os.environ.get("SUB_FABLE_OK", "").split(",") if n.strip()}
# Once fable HAS been seen for an account, believe it this long. Plan
# entitlement doesn't flicker between polls; a payload that stops mentioning
# fable for one reading is a degraded payload, not a downgraded plan. Long
# enough to ride out a hot 5h window, short enough that a REAL plan change
# (the slop org, 2026-08-09) is caught within a day.
FABLE_STICKY = float(os.environ.get("FABLE_STICKY", "21600"))     # 6h
# How many sessions the capability evacuation may move per sweep. A plan
# change convicts a whole pool at once, so this is the one handoff trigger
# that can fire on EVERY session simultaneously — 16 of them were parked on
# the slop org the day this shipped, and a respawn is a `claude --resume`
# reading a full transcript back in. Staged over a few sweeps instead, so the
# migration can't be the thing that wedges the box. 0 = no cap.
SUB_CAP_EVAC_BATCH = int(os.environ.get("SUB_CAP_EVAC_BATCH", "4"))
# Per-sweep ceiling on account handoffs, across EVERY reason (drained rescue,
# capability evacuation, hot evacuation, rebalance). A handoff respawns the
# session with --resume, so each one re-ingests that session's whole context —
# cheap for one session, a bill for ten at once. Before this, only the
# capability path was batched, so a plan hitting the wall evacuated every
# session in a single sweep: 10 simultaneous context re-ingests landed on the
# fresh pool, spent enough of it to drain that pool too, and the next sweep
# marched everyone back. The logs showed the ping-pong plainly — sub4->clawd
# 89 times, clawd->sub4 67 times in one day. The sweep re-runs every ~15s, so
# a small cap still clears a 10-session evacuation inside a minute; it just
# arrives as a queue instead of a herd. 0 = unlimited (old behaviour).
SUB_HANDOFF_BATCH = int(os.environ.get("SUB_HANDOFF_BATCH", "2"))
# How long a pending graceful restart waits for mid-turn work before taking the
# hit anyway. 0 = wait forever (the old behavior). This exists because "wait for
# quiet" on a machine somebody actually uses can mean "never": on 2026-08-09 a
# routing fix sat unapplied on clawd-head for 30+ minutes — the box kept spawning
# onto the exact plan the fix was written to avoid — while the running process
# waited on a session that was itself parked on a human. Code that can't land is
# not a safe default; 20 min is long enough that a normal turn finishes first.
RESTART_MAX_WAIT = float(os.environ.get("RESTART_MAX_WAIT", "1200"))
# The CLI's limit banner, as painted in the PTY ("You've hit your session
# limit · resets …", or the blocking "Stop and wait for limit to reset" menu).
# Needles are deliberately narrow, and the rescue re-confirms against the live
# usage endpoint, so a session merely *displaying* this text (e.g. reading this
# file) never causes a spurious handoff.
_PTY_ANSI_RE = re.compile(
    rb"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][A-Z0-9]|\x1b[=>]")
_LIMIT_BANNER_RE = re.compile(
    r"you.?ve hit your [a-z0-9 -]{0,24}limit"    # .? = ' or ’ (the CLI uses either)
    r"|you.?ve reached your [a-z0-9 .-]{0,32}limit"
    r"|stop and wait for limit to reset"
    r"|ask your admin for more usage", re.I)
# The newer EXTRA-USAGE-CREDITS wall (2026-08): a model-scoped weekly window
# (e.g. the Fable weekly) runs dry and the CLI paints a blocking ink dialog —
# "You've reached your <model> limit … uses usage credits" — with numbered
# options (continue on credits / switch model) and an Enter-confirms footer.
# Same trap class as the resume gate: nobody is there to answer it, and a
# harness-delivered prompt's CR could CONFIRM an option (spend real credits,
# or silently switch off the model SUB_REQUIRE_FABLE exists to keep). ink pads
# dialogs with cursor motion, not spaces, so after de-ANSI the words arrive
# RUN TOGETHER and _LIMIT_BANNER_RE's spaced needles match nothing — this one
# is matched against _flat_pty (whitespace-stripped) text instead. It demands
# the credits/weekly context, not just the headline; and like the banner it is
# confirm-gated by rescue_limit_wall, so quoted text on a cool pool is a no-op.
_LIMIT_MODAL_RE = re.compile(
    r"you.?vereachedyour[a-z0-9.-]{0,32}limit"
    r".{0,400}?(?:usesusagecredits|usageforthisweek|extrausageforpaid)",
    re.I | re.S)
# Raw-byte window the limit scan re-strips each read (the resume-gate lesson:
# per-chunk flattening leaks half-stripped escapes into the needle). The
# credits dialog is ~1.5KB painted; 8KB holds it whole through a repaint.
LIMIT_RAW_MAX = 8192
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
# Claude's RESUME GATE (CLI 2.1.226): resuming a session that is older than
# CLAUDE_CODE_RESUME_THRESHOLD_MINUTES (70) AND carries more than
# CLAUDE_CODE_RESUME_TOKEN_THRESHOLD (100k) estimated tokens opens a modal —
# titled "This session is 1d 17h old and 438k tokens.", offering three numbered
# options (summarize / resume as-is / stop asking) with the first preselected
# — and the session sits there, resumed but frozen, until somebody answers. In
# a browser harness "somebody" is a human who may be hours away, and EVERY
# harness resume path hits it (daemon restart, graceful self-restart, account
# handoff, every rescue respawn) on exactly the long-lived sessions that matter.
# Option 1 runs plain `/compact` (verified in the CLI bundle: the "compact"
# branch calls the same slash command _compact_for_pin types), so answering it
# is the cheap branch as well as the unblocking one.
#
# TWO RENDERING FACTS THE NEEDLE DEPENDS ON, both measured, not assumed:
#  * ink lays this dialog out with CURSOR-FORWARD padding (ESC[nC), not literal
#    spaces, so stripping ANSI leaves the words RUN TOGETHER —
#    "Resumefromsummary(recommended)". A spaced needle (as _LIMIT_BANNER_RE can
#    afford, its banner being one contiguous styled line) matches NOTHING here.
#    Hence _flat_pty below strips whitespace entirely, on both sides.
#  * claude's own status file still reads "idle" while the modal is up, so —
#    unlike the limit banner, which rescue_limit_wall re-confirms against the
#    usage endpoint — there is NO out-of-band oracle to confirm this one.
# So the needle carries the whole burden: it demands the full option list AND
# the live footer, which prose quoting the dialog (this comment; the CLAUDE.md
# section) does not reproduce in one screen. The blast radius if it ever does
# false-match is deliberately tiny — one bare CR into an empty composer, which
# claude ignores — rather than the respawn the onboarding scan risks.
def _flat_pty(chunk: bytes) -> str:
    """De-ANSI a PTY chunk and strip whitespace ENTIRELY. The limit/onboarding
    scans collapse runs to single spaces, which is right for text the CLI emits
    as one styled line; anything ink lays out in a box arrives space-free
    instead (the padding is cursor motion, which de-ANSI'ing deletes). Dropping
    whitespace on both sides is the only form that matches both."""
    return re.sub(r"\s+", "", _PTY_ANSI_RE.sub(b"", chunk).decode("utf-8", "ignore"))


_RESUME_GATE_RE = re.compile(
    r"resumefromsummary\(recommended\)"
    r".{0,120}?resumefullsessionas-is"
    r".{0,120}?entertoconfirm", re.I | re.S)
RESUME_GATE = os.environ.get("RESUME_GATE", "1") != "0"
# The modal paints ~0.8s into a resume, before the session can do anything
# else; the window only has to cover a slow box replaying a huge transcript.
RESUME_GATE_WINDOW = float(os.environ.get("RESUME_GATE_WINDOW", "120"))
# Better than answering the modal: don't let it paint. The CLI only shows it
# when the session's age and size clear env-tunable floors (verified in the
# 2.1.235 bundle: CLAUDE_CODE_RESUME_THRESHOLD_MINUTES ?? 70, and a token
# floor ?? 1e5), so a huge minutes floor in the child env means no modal —
# and no auto-/compact turn, which bills a full-context model turn to the
# pool a handoff just moved onto. Every resume here is harness-initiated
# (daemon restart, graceful restart, handoff, rescue); nobody is at the
# keyboard wanting a summarize offer. Undocumented knob on a server-side-
# flagged feature, so it degrades one way: if the CLI ever ignores it, the
# gate scan above still answers the modal exactly as before.
RESUME_MODAL_SUPPRESS = os.environ.get("RESUME_MODAL_SUPPRESS", "1") != "0"
RESUME_MODAL_FLOOR_MIN = "525600"                    # one year, in minutes
# Raw-byte window the gate scan re-strips each read. The modal is ~1.5KB of
# painted bytes; 8KB holds it whole even when a repaint interleaves.
GATE_RAW_MAX = 8192
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


def _ensure_trusted(config_dir, workdir):
    """Pre-accept claude's per-folder trust dialog for this session's cwd.

    The CLI paints a blocking "Is this a project you created or one you
    trust?" modal the first time a LOGIN meets a WORKSPACE, remembered as
    `projects["<abs cwd>"].hasTrustDialogAccepted: true` in that login's
    .claude.json (verified against the live file and the 2.1.234 bundle,
    which itself tells headless users to set exactly that key). In this
    harness the human already chose the folder — every cwd is a repo they
    created/cloned via the projects layer — so the question is answered
    before it can be asked. Without this, every freshly cloned project AND
    every account handoff (new config dir = never-trusted path) parks the
    session on the modal.

    Same discipline as _ensure_onboarded: a dir with NO .claude.json is a
    pending sign-in ceremony and is left strictly alone — creating the file
    would flip _opens_normal_tui's fresh-dir detection. Seeds both abspath
    and realpath when they differ (macOS /tmp → /private/tmp: node's cwd is
    symlink-resolved, ours may not be). Returns True iff it wrote."""
    cfg = _claude_config_file(config_dir)
    if not cfg.exists():
        return False                    # never-signed-in dir — not ours to create
    try:
        data = json.loads(cfg.read_text())
    except (OSError, ValueError):
        return False                    # unreadable/foreign file — not ours to rewrite
    if not isinstance(data, dict):
        return False
    paths = {os.path.abspath(workdir), os.path.realpath(workdir)}
    projects = data.setdefault("projects", {})
    dirty = False
    for p in paths:
        entry = projects.setdefault(p, {})
        if not (isinstance(entry, dict) and entry.get("hasTrustDialogAccepted")):
            if not isinstance(entry, dict):
                entry = projects[p] = {}
            entry["hasTrustDialogAccepted"] = True
            dirty = True
    if not dirty:
        return False
    tmp = cfg.with_name(cfg.name + ".trust-seed.tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, cfg)
    except OSError as e:
        print(f"[creds {config_dir or '~/.claude'}] trust seed WRITE FAILED "
              f"({e}) — this spawn may open on the folder-trust dialog",
              flush=True)
        return False
    print(f"[creds {config_dir or '~/.claude'}] folder trust seeded for "
          f"{os.path.abspath(workdir)} (trust dialog suppressed)", flush=True)
    return True


def _opens_normal_tui(config_dir):
    """True iff a spawn into this dir will paint claude's NORMAL TUI (so a
    `/login` has to be typed into it) rather than the CLI's own login /
    onboarding flow (which we must never inject keystrokes into).

    The signal is ONBOARDING state, not credentials. A dir that has ever been
    signed in keeps `hasCompletedOnboarding` + `oauthAccount` in its
    .claude.json forever — and a revoked login is *deleted* from the
    credential store, so `_creds_state` reads 'absent' for exactly the
    re-sign-in case the auto-/login exists to serve (2026-08-07: every
    'sign in again' ceremony on this box — clawd, slop, austinmax, sub2 —
    landed on a normal TUI with no /login typed, because the old gate asked
    'are the creds present?'). A genuinely fresh dir has no .claude.json at
    all, so it answers False and is left strictly alone."""
    cfg = _claude_config_file(config_dir)
    try:
        data = json.loads(cfg.read_text()) if cfg.exists() else {}
    except (OSError, ValueError):
        data = {}
    if data.get("hasCompletedOnboarding") or data.get("oauthAccount"):
        return True
    # No config record, but a live credential blob: onboarding is skipped and
    # the TUI opens anyway (a signed-in dir whose .claude.json got wiped).
    return _creds_state(config_dir) == "present"


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


def _live_claude_dirs():
    """Config dirs of every claude process running on this host — OURS OR
    NOT (a hand-launched terminal claude counts). Any such process holds,
    and lazily renews, that account's rotating refresh grant; a second
    consumer replaying the same grant trips OAuth reuse detection and
    revokes the whole token family. A claude with no CLAUDE_CONFIG_DIR in
    its env is on the default account (~/.claude). Empty set on any scan
    failure — the flock and exit-grace guards still stand behind this."""
    dirs = set()
    try:
        out = subprocess.run(["ps", "axeww", "-o", "pid=,command="],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return dirs
    for line in out.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) < 2:
            continue
        exe = parts[1].split(None, 1)[0]
        if os.path.basename(exe) != "claude":
            continue
        m = re.search(r"CLAUDE_CONFIG_DIR=(\S+)", parts[1])
        d = m.group(1) if m else "~/.claude"
        dirs.add(os.path.normpath(os.path.expanduser(d.rstrip("/"))))
    return dirs


def _norm_config_dir(config_dir):
    """One spelling for an account's config dir ('' = default ~/.claude)."""
    return os.path.normpath(os.path.expanduser(
        (config_dir or "~/.claude").rstrip("/")))


# cont's credential-custody ledger (clawd-containers): one file per VM whose
# guest currently rides a login, containing that login's config dir. A login
# in VM custody has a SECOND live consumer of its rotating refresh grant —
# the guest's claude — that no host-side ps scan can see (2026-08-07: the
# wrangler's bounce path put sub5 in exactly this blind spot).
VM_CUSTODY_DIR = os.environ.get(
    "VM_CUSTODY_DIR", os.path.expanduser("~/.config/cont/vm-accounts"))


def _vm_custody_dirs():
    """Normalized config dirs of every login a VM custody record names.
    Empty set when the ledger doesn't exist (no cont on this box) or can't
    be read — the flock and live-process guards still stand behind this."""
    dirs = set()
    try:
        for f in os.listdir(VM_CUSTODY_DIR):
            try:
                with open(os.path.join(VM_CUSTODY_DIR, f)) as fh:
                    rec = fh.read().strip()
                dirs.add(_norm_config_dir(rec))
            except OSError:
                continue
    except OSError:
        pass
    return dirs


def _ping_rotate(config_dir):
    """Rotate an idle account's OAuth tokens by letting the REAL client do
    it: a minimal `claude -p` under the config dir refreshes and persists
    its own store entry exactly like interactive use, and holds nothing in
    memory afterwards (the process exits). This RETIRED the hand-rolled
    curl refresh grant (2026-08-07): families rotated only by the bare
    token endpoint were server-side expired ~4 weeks after /login even
    though every rotation 'succeeded' — see the call site in _fetch_usage.
    (Historical lore from the curl era, kept for future archaeologists:
    platform.claude.com sits behind Cloudflare bot protection that 403s
    Python's urllib TLS signature with 'error code: 1010', and the app
    layer blanket-429s curl's default User-Agent — any future direct call
    to the token endpoint must be curl with claude-cli's own UA string.)
    True iff the ping exited 0. Caller must hold the per-account refresh
    flock; the same env scrub as session spawn keeps the child on the
    subscription and out of embedded mode. For the default account
    CLAUDE_CONFIG_DIR must be UNSET (the 2026-07-09 trap: shells on this
    host export it pointing at a harness account)."""
    env = {k: v for k, v in os.environ.items() if k not in SCRUB_ENV}
    if config_dir:
        env["CLAUDE_CONFIG_DIR"] = config_dir
    else:
        env.pop("CLAUDE_CONFIG_DIR", None)
    try:
        r = subprocess.run(
            [CLAUDE_BIN, "-p", "--output-format", "text",
             "reply with the single word OK"],
            env=env, stdin=subprocess.DEVNULL, capture_output=True,
            text=True, timeout=90, cwd=os.path.expanduser("~"))
        return r.returncode == 0
    except Exception:
        return False


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
    across polls (plus the 429 back-off horizon); rotation happens via
    _ping_rotate — the real client persists its own tokens to the store."""
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
        # Cross-process refresh lock (same path cont's keepalive takes):
        # exactly one deliberate refresher per grant at a time. A busy lock
        # means someone else is rotating this grant RIGHT NOW — defer; their
        # rotation lands in the store for our next poll. Racing them would
        # replay a superseded refresh token and revoke the family.
        lock_path = os.path.join(_norm_config_dir(config_dir), ".refresh.lock")
        try:
            lockf = open(lock_path, "w")
            fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            _clog(config_dir, "refresh deferred — another process holds the "
                              "refresh lock; keeping the last snapshot")
            return None
        try:
            # Double-check under the lock: a sibling may have rotated while
            # we decided. Re-read the store and use ITS tokens — a now-valid
            # access token means no refresh is needed at all, and if we do
            # refresh, we must consume the lineage tip, not our snapshot.
            blob2, _ = _read_oauth_creds_ex(config_dir)
            oa2 = (blob2 or {}).get("claudeAiOauth") or {}
            a2, e2 = oa2.get("accessToken"), oa2.get("expiresAt")
            refresh = oa2.get("refreshToken") or refresh
            if a2 and a2 not in tries and isinstance(e2, (int, float)) \
                    and e2 / 1000 > time.time() + 60:
                code, usage, retry_after = call(a2)
                if code == 200:
                    good = a2
                    if tok_cache is not None:
                        tok_cache["access"] = a2
            if code != 200:
                # Rotate via the REAL client, never a hand-rolled refresh
                # grant (2026-08-07): idle logins whose families only ever
                # saw the bare token endpoint died with invalid_grant
                # "Refresh token expired" ~4 weeks after /login, one per
                # day in login order (sub3 08-06, sub4 08-07, on rotations
                # this code had "persisted" successfully hours earlier),
                # while logins with real claude traffic (default,
                # austinmax) sailed past 30 days. The ping persists its
                # own rotation to the store; re-read it for the token.
                if _ping_rotate(config_dir):
                    blob3, _ = _read_oauth_creds_ex(config_dir)
                    oa3 = (blob3 or {}).get("claudeAiOauth") or {}
                    fresh = oa3.get("accessToken")
                    if fresh:
                        # Timestamped success line — the sub4 postmortem
                        # lived and died by this log; silence is not an
                        # option on the rotation path.
                        _clog(config_dir, "rotated via client ping — fresh "
                                          "access token adopted from the store")
                        if tok_cache is not None:
                            tok_cache["access"] = fresh
                        code, usage, retry_after = call(fresh)
                        if code == 200:
                            good = fresh
                if code != 200:
                    # Ping failed or its token was still refused. A blob the
                    # store positively holds WITHOUT tokens is claude's own
                    # wipe — its refresh was rejected, the one true
                    # re-sign-in signal. Anything else (network, endpoint
                    # outage, locked keychain) is transient: back off so a
                    # hot limiter isn't re-poked every poller cycle.
                    blob3, definitive3 = _read_oauth_creds_ex(config_dir)
                    oa3 = (blob3 or {}).get("claudeAiOauth") or {}
                    if definitive3 and not (oa3.get("accessToken")
                                            or oa3.get("refreshToken")):
                        _clog(config_dir, "refresh ping failed and the client "
                                          "wiped the blob — this login needs "
                                          "a re-sign-in")
                        return AUTH_FAIL
                    if tok_cache is not None:
                        tok_cache["no_poll_until"] = time.time() + 600
                    _clog(config_dir, "refresh ping failed but the credential "
                                      "is intact — transient; keeping the last "
                                      "snapshot, next attempt in 10 min")
                    return None
        finally:
            lockf.close()                        # closing releases the flock
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


def _has_fable_window(usage):
    """True iff this snapshot advertises a fable-scoped window (any group).
    Reads _fetch_usage's NORMALIZED windows (key `weekly_scoped_fable`, label
    `7d fable`), so it works whether the number arrived via the legacy
    per-model keys or the newer `limits` array."""
    for w in (usage or {}).get("windows") or []:
        if "fable" in f"{w.get('key','')} {w.get('label','')}".lower():
            return True
    return False


# A NOTE ON A THEORY THAT WAS WRONG (2026-08-09, kept so it isn't re-derived):
# it briefly looked like the scoped windows drop out of the payload when a plan
# is at its limit — `sub4` showed no fable window at 91% used on one box while
# "the same" `sub4` showed `7d fable 2.0%` on another. They were not the same
# subscription. `sub4` is a CONFIG-DIR LABEL, and the two boxes' sub4 dirs hold
# logins into different orgs (18f36efd vs 94f7f5f0) — the label lied, which is
# the trap ACCOUNTS-PANEL.md documents. The 91%/100% correlation was
# coincidence: the fable-less org simply happened to be hot on those boxes.
# So there is NO evidence that limits suppress the window, and gating the
# verdict on a "healthy" reading only re-opened the original bug (a fable-less
# pool at 91% is under SUB_HOT, so nothing else would have skipped it).
# The stickiness below is the guard that survived, and it is the principled
# one: a plan that really carries fable advertises it on any healthy poll, so
# having seen it recently is what protects against a one-off odd payload.
# ALWAYS compare pools by org uuid, never by label.


def _fable_state(usage, seen_at=0.0, now=None):
    """True / False / None — does this pool's plan carry Fable?

    True  = this reading advertises a fable window, or one was seen recently
            (within FABLE_STICKY). Entitlement doesn't flicker minute to
            minute; a payload that momentarily stops mentioning it is far
            more likely to be degraded than the plan to have been downgraded.
    False = a good reading advertises windows, none of them are fable's, and we
            haven't seen fable for this pool recently.
    None  = no good reading at all. Callers must treat None as 'yes' — an
            endpoint change that stopped emitting scoped limits altogether
            would otherwise convict every pool at once and leave the router
            with nothing to spend, which is far worse than one wrong turn.

    `seen_at` is the epoch seconds of the last observed fable window for this
    account (0 = never), which is what makes the stickiness possible at all —
    it must be remembered ACROSS readings, not derived from one."""
    now = time.time() if now is None else now
    if not (usage or {}).get("windows"):
        return None
    if _has_fable_window(usage):
        return True
    if seen_at and now - seen_at < FABLE_STICKY:
        return True                              # believed recently; a blip, not a downgrade
    return False


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


# ── shared kit (share/) ──────────────────────────────────────────────────────
# Machine-level agent kit shipped IN the repo: skills + CLIs every session on
# every machine should have (today: the `todo` skill + CLI for Austin's shared
# list at todo.atg.link). Push-to-main is the fleet's only automatic
# distribution channel, so the harness installs these at boot:
# share/skills/* → ~/.claude/skills/ (which the SHARE_PATHS symlink already
# fans into every account dir) and share/bin/* → ~/bin (0755). The repo copy
# is canonical — same contract as the rest of the deploy — so local edits are
# overwritten; change the repo copy instead. Secrets never ride this path: the
# todo token lives in ~/.clawd-todo.env, placed once per machine by hand (see
# docs/fleet/ADD-MACHINE.md); a box that has the kit but not the token gets a
# warning line in the log instead of a broken-silent skill.
SHARE_DIR = HERE / "share"


def _sync_shared_kit(home=None, accounts_dir=None):
    """Install share/ onto this machine. Idempotent; returns changed paths.
    `home`/`accounts_dir` exist for tests only."""
    home = Path(home) if home is not None else Path.home()
    accounts_dir = Path(accounts_dir) if accounts_dir is not None else ACCOUNTS_DIR
    changed = []

    def _put(src, dst, mode=None):
        try:
            data = src.read_bytes()
            if dst.exists() and not dst.is_symlink() and dst.read_bytes() == data:
                if mode is not None:
                    dst.chmod(mode)
                return
            dst.parent.mkdir(parents=True, exist_ok=True)
            tmp = dst.parent / (dst.name + ".tmp")
            tmp.write_bytes(data)
            tmp.chmod(mode if mode is not None else 0o644)
            tmp.replace(dst)
            changed.append(str(dst))
        except OSError as e:
            print(f"[kit] {dst}: {e}", flush=True)

    skills_src = SHARE_DIR / "skills"
    if skills_src.is_dir():
        # ~/.claude/skills covers every symlinked account; an account whose
        # skills/ is a REAL dir opted out of the symlink, so copy in directly.
        roots = [home / ".claude" / "skills"]
        try:
            for acc in sorted(accounts_dir.iterdir()):
                sk = acc / "skills"
                if sk.is_dir() and not sk.is_symlink():
                    roots.append(sk)
        except OSError:
            pass
        for f in sorted(skills_src.rglob("*")):
            if f.is_file():
                for root in roots:
                    _put(f, root / f.relative_to(skills_src))

    bin_src = SHARE_DIR / "bin"
    if bin_src.is_dir():
        for f in sorted(bin_src.iterdir()):
            if f.is_file():
                _put(f, home / "bin" / f.name, mode=0o755)

    if changed:
        print(f"[kit] installed/updated: {', '.join(changed)}", flush=True)
    if (skills_src / "todo").is_dir() and not (home / ".clawd-todo.env").exists():
        print("[kit] ⚠ todo skill is installed but ~/.clawd-todo.env is missing "
              "— sessions can't reach todo.atg.link until the token file is "
              "placed (TODO_URL + TODO_TOKEN; see share/skills/todo/SKILL.md)",
              flush=True)
    return changed


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
                 tier="", ready=False, created=0.0, usage=None, fable_seen=0.0):
        self.name = name
        self.config_dir = config_dir
        self.email = email
        self.org = org                           # organizationUuid = the usage pool
        self.org_name = org_name                 # human name of that org (profile)
        self.tier = tier                         # rate_limit_tier, e.g. …max_20x
        self.ready = ready
        self.created = created or time.time()
        self.usage = usage or None               # {"pct","windows","checkedAt"}
        self.fable_seen = fable_seen or 0.0      # epoch of the last OBSERVED fable window.
                                                 # Persisted: the stickiness in _fable_state is
                                                 # only meaningful across readings, and a restart
                                                 # that forgot it would re-convict every pool
                                                 # whose payload is momentarily degraded.
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
                "ready": self.ready, "fable_seen": self.fable_seen,
                "created": self.created, "usage": self.usage}

    def record_usage(self, pct, windows, now=None):
        """Store a GOOD usage reading, and stamp the fable sighting with it.

        One method rather than four assignments: `fable_seen` is what makes
        _fable_state's stickiness work, and a caller that set `usage` while
        forgetting the stamp would let a degraded payload convict a pool that
        this very reading proves is fine. Callers must not assign `.usage`
        directly on a good reading."""
        now = time.time() if now is None else now
        self.usage = {"pct": round(pct, 1), "windows": windows,
                      "checkedAt": now, "goodAt": now}
        if _has_fable_window(self.usage):
            self.fable_seen = now
        return self.usage

    def fable(self):
        """Tri-state Fable entitlement for THIS account — the usage payload's
        verdict, with the manual override lists winning over it."""
        if self.name in SUB_FABLE_OK:
            return True
        if self.name in SUB_NO_FABLE:
            return False
        return _fable_state(self.usage, self.fable_seen)

    def routable(self):
        """False iff the capability gate says this pool can't do the fleet's
        work right now. Deliberately NOT folded into `broken`: the account
        stays listed, signed in, and manually selectable — it is skipped by
        the router, not evicted from the roster."""
        return not (SUB_REQUIRE_FABLE and self.fable() is False)

    def meta(self, active=False):
        pct = (self.usage or {}).get("pct")
        status = ("pending" if not self.ready
                  else "needs-login" if self.broken else "ready")
        return {"name": self.name, "email": self.email,
                # Capability, shown alongside headroom: a pool can be wide
                # open and still be the wrong place to spend a turn.
                "fable": self.fable(), "routable": self.routable(),
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


def _normalize_repo_url(raw):
    """Accept a full git URL/path, an `owner/repo` shorthand, or a bare `repo`
    name — the latter two resolve against github.com (bare → GH_OWNER), so
    typing `slop-computer-live` means github.com/clawdbotatg/slop-computer-live."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    if not re.match(r"^(https?://|git@|ssh://|file://|/|~)", raw):
        raw = (f"https://github.com/{raw}" if "/" in raw
               else f"https://github.com/{GH_OWNER}/{raw}")
    return raw


# viewerPermission values that mean "we can push" (the rest: TRIAGE, READ, none)
_GH_PUSH_PERMS = ("ADMIN", "MAINTAIN", "WRITE")


def _gh_repo_info(url):
    """One `gh repo view --json …` for an EXTERNAL add: push access, default
    branch, canonical slug. Returns (info_dict, "") or (None, error). Kept to
    a single call because it sits on the user's click — everything the
    fork-or-clone decision needs rides in this one payload."""
    fields = "viewerPermission,defaultBranchRef,nameWithOwner,url,isFork,parent"
    try:
        r = subprocess.run(["gh", "repo", "view", url, "--json", fields],
                           capture_output=True, text=True, timeout=25)
    except Exception as e:
        return None, f"gh repo view failed: {e}"
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "gh repo view failed").strip()
        if "auth" in err.lower():
            err += " (is `gh` authenticated in the server's environment?)"
        return None, err[-300:]
    try:
        j = json.loads(r.stdout or "{}")
    except Exception as e:
        return None, f"gh repo view: unreadable JSON ({e})"
    return {"slug": j.get("nameWithOwner") or "",
            "url": j.get("url") or url,
            "push": (j.get("viewerPermission") or "").upper() in _GH_PUSH_PERMS,
            "perm": j.get("viewerPermission") or "",
            "default_branch": ((j.get("defaultBranchRef") or {}).get("name") or ""),
            "is_fork": bool(j.get("isFork"))}, ""


def _git(path, *args, timeout=30):
    """Run one git command in `path`; returns (rc, combined output)."""
    try:
        r = subprocess.run(["git", *args], cwd=path, capture_output=True,
                           text=True, timeout=timeout)
        return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()
    except Exception as e:
        return 1, str(e)


def _external_sync(path, default_branch, name="", budget=None):
    """Bring an external project's default branch up to upstream before a
    session starts in it. Always fetches `upstream` (so `upstream/<default>`
    is fresh to branch from); fast-forwards the default branch ONLY when it is
    checked out and clean (a feature branch in progress, or local edits, are
    left exactly as they are — the rule is "never stale", not "never dirty");
    and, when we're on a fork, nudges the fork's default branch along too so
    PR diffs stay minimal. Best-effort throughout: any failure is logged and
    the session still spawns. Returns a one-line summary for the log."""
    budget = EXTERNAL_SYNC_TIMEOUT if budget is None else budget
    tag = f"[project {name or os.path.basename(path)}]"
    br = default_branch or "main"
    rc, out = _git(path, "fetch", "upstream", "--prune", timeout=budget)
    if rc != 0:
        msg = f"upstream fetch failed: {out[-160:]}"
        print(f"{tag} sync: {msg}", flush=True)
        return msg
    rc, cur = _git(path, "rev-parse", "--abbrev-ref", "HEAD", timeout=5)
    if rc != 0 or cur != br:
        msg = f"fetched upstream; on `{cur or '?'}`, left {br} alone"
        print(f"{tag} sync: {msg}", flush=True)
        return msg
    rc, dirty = _git(path, "status", "--porcelain", timeout=10)
    if rc != 0 or dirty:
        msg = f"fetched upstream; {br} has local changes, not fast-forwarded"
        print(f"{tag} sync: {msg}", flush=True)
        return msg
    rc, out = _git(path, "merge", "--ff-only", f"upstream/{br}", timeout=30)
    if rc != 0:
        msg = f"fetched upstream; {br} diverged from upstream/{br}, not fast-forwarded"
        print(f"{tag} sync: {msg}", flush=True)
        return msg
    moved = "Already up to date" not in out
    # fork: keep origin/<default> trailing upstream too (best-effort, quiet —
    # a push rejection here never matters to the session, it's cosmetic)
    rc, o_url = _git(path, "remote", "get-url", "origin", timeout=5)
    rc2, u_url = _git(path, "remote", "get-url", "upstream", timeout=5)
    if moved and rc == 0 and rc2 == 0 and o_url != u_url:
        _git(path, "push", "origin", br, timeout=30)
    msg = f"{br} fast-forwarded to upstream/{br}" if moved else f"{br} already at upstream/{br}"
    print(f"{tag} sync: {msg}", flush=True)
    return msg


# ── project: a git repo under PROJECTS_DIR that sessions run inside ───────────
class Project:
    """One git repo we drive. Owns no processes itself — it's the workdir N
    ClaudeSessions launch in. `status` tracks an async clone/create."""

    def __init__(self, pid, name, path, repo_url="", status="ready",
                 error="", created=0.0, pinned=False, kind="gh",
                 emoji="", emoji_at=0.0, upstream="", default_branch=""):
        self.pid = pid
        self.name = name
        self.path = path                         # abs path to the repo
        self.kind = kind if kind in PROJECT_KINDS else "gh"
        # local = a private folder registered in place: it must never carry a
        # remote URL, no matter which code path constructs it
        self.repo_url = "" if self.kind == "local" else _scrub_url_creds(repo_url)
        # external = someone else's GitHub repo (see add_external_project):
        # `upstream` is the SOURCE repo URL (where PRs go); repo_url is what we
        # push to (our fork, or the source itself when we hold push access).
        self.upstream = _scrub_url_creds(upstream) if self.kind == "external" else ""
        self.default_branch = default_branch if self.kind == "external" else ""
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
                "emoji": self.emoji, "emoji_at": self.emoji_at,
                "upstream": self.upstream, "default_branch": self.default_branch}

    def meta(self, session_count=0, busy_count=0, waiting_count=0, last_touched=0.0):
        return {"pid": self.pid, "name": self.name, "path": self.path,
                "repoUrl": self.repo_url, "status": self.status,
                "error": self.error, "sessionCount": session_count,
                "busyCount": busy_count, "waitingCount": waiting_count,
                "created": self.created, "pinned": self.pinned,
                "kind": self.kind, "lastTouched": last_touched,
                "emoji": self.emoji,
                "upstream": self.upstream, "defaultBranch": self.default_branch}

    def is_external(self):
        return self.kind == "external"

    @staticmethod
    def _slug(url):
        """`owner/repo` from a github URL ("" when it isn't one)."""
        m = re.search(r"github\.com[/:]([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", url or "")
        return f"{m.group(1)}/{m.group(2)}" if m else ""

    def upstream_slug(self):
        return self._slug(self.upstream)

    def fork_slug(self):
        return self._slug(self.repo_url)

    def standing_rule(self):
        """The rule every session in an EXTERNAL project is born with. Written
        for the agent, not the human: concrete remotes, concrete commands, and
        the one prohibition that matters — the default branch is read-only."""
        if not self.is_external():
            return ""
        up, mine = self.upstream_slug() or self.upstream, self.fork_slug() or self.repo_url
        br = self.default_branch or "main"
        forked = bool(up) and up != mine
        head = f"{mine.split('/')[0]}:<branch>" if forked and "/" in mine else "<branch>"
        where = (f"a FORK (`origin` = {mine}) of the upstream repo `upstream` = {up}"
                 if forked else f"the upstream repo {up} (`origin`; you have push access)")
        return (
            "# External repo — standing rule (injected by the clawd harness)\n"
            f"This working directory is {where}. The upstream default branch is `{br}`.\n"
            "\n"
            f"- NEVER commit to or push `{br}` (or any upstream branch directly). "
            f"`{br}` is read-only here: it tracks `upstream/{br}` and the harness "
            "fast-forwards it from upstream at every session start.\n"
            f"- ALWAYS work on a feature branch cut from `upstream/{br}`: "
            f"`git fetch upstream && git switch -c <branch> upstream/{br}`. "
            f"If you find yourself on `{br}` with changes, move them to a branch "
            f"(`git switch -c <branch>`) before committing.\n"
            "- Push the branch to `origin` (`git push -u origin <branch>`) and open a "
            f"pull request against upstream: `gh pr create --repo {up} --base {br} "
            f"--head {head} --title ... --body ...`. One PR per task; push further "
            "commits to the same branch to update it.\n"
            "- When the work is done, REPORT THE PR LINK (the `https://github.com/.../pull/N` "
            "URL `gh pr create` prints) in your final message. Work that isn't in a PR "
            "isn't done.\n"
            "- Do not merge your own PR, do not force-push shared branches, and do not "
            "rewrite history that has already been pushed.\n"
            "- Follow the upstream repo's own CONTRIBUTING / AGENTS / CLAUDE guidance "
            "where it exists; this rule only adds the branch-and-PR discipline.\n")


# ── Engines: what differs between one agent CLI and another ───────────────────
# The harness's contract with a CLI is deliberately narrow (see CLAUDE.md,
# "Channels"): keystrokes in, raw PTY bytes out, plus a transcript JSONL on disk
# and lifecycle hooks POSTed to /hook. We never parse the TUI. That's what makes
# a second engine a plug-in rather than a fork — everything above this layer
# (busy pill, naming, digests, pins, tabs, deep links, fleet) is engine-blind.
#
# Everything a CLI does DIFFERENTLY lives behind this interface. Adding a third
# engine should mean writing one subclass, not grepping for "claude".
# Design notes + what's verified vs assumed: docs/CODEX-ENGINE.md.
class Engine:
    name = "claude"
    bin = CLAUDE_BIN
    routes_accounts = True      # participates in the subscription router
    scrub_extra = ()            # env names to strip beyond SCRUB_ENV

    def argv(self, s):          raise NotImplementedError
    def env(self, s, env):      pass          # mutated in place
    def hook_setup(self, s):    return None   # → settings path, or None
    def transcript_globs(self, s):  return []
    def slim_event(self, s, line):  return None
    def send_settle(self, big): return SEND_SETTLE if big else SEND_SETTLE_MIN
    def bg_probe(self, s):      return ""
    # The keystroke that answers this CLI's resume gate (see _RESUME_GATE_RE) —
    # a bare CR, because option 1 ("Resume from summary") is the one already
    # highlighted and the modal's own footer reads "Enter to confirm". b"" opts
    # an engine out: codex raises no such modal, and a CR fired at a codex TUI
    # on a needle we have never seen it paint would be a guess, not a fix.
    resume_gate_key = b""
    # The CLI's "summarize this conversation to reclaim context" slash command,
    # typed into the TUI when a session is pinned. Both shipped CLIs carry
    # /compact in their command tables; "" would opt an engine out.
    #
    # THE TRAILING SPACE IS LOAD-BEARING. Typing "/compact" leaves the TUI's
    # slash-command autocomplete menu open, and the submitting CR is eaten by
    # the menu instead of running anything — the harness's first cut of this
    # left "/compact" sitting in the composer with the picker up (verified in a
    # scratch harness: menu open, session id unrotated, nothing ran). The space
    # completes the token, the menu closes, and the CR submits. Verified on
    # claude; assumed for codex, whose TUI uses the same picker pattern.
    compact_cmd = "/compact "


class ClaudeEngine(Engine):
    name, bin, routes_accounts = "claude", CLAUDE_BIN, True
    resume_gate_key = b"\r"                  # verified: CR → "❯ /compact" → Compacting…

    def argv(self, s):
        argv = [self.bin,
                ("--resume" if s.resuming else "--session-id"), s.session_id,
                "--settings", s.settings_path]
        rule = s.standing_rule()
        if rule:
            # External project: the branch-and-PR rule rides as an appended
            # SYSTEM prompt — above CLAUDE.md, survives compaction, touches no
            # file in the (someone else's) repo. Recomputed on every start, so
            # a handoff/restart respawn carries it too.
            argv += ["--append-system-prompt", rule]
        return argv

    def env(self, s, env):
        # Claude Code can render its whole TUI in the ALTERNATE screen buffer —
        # a server-side rollout, so it flips on per-account with no CLI update
        # or harness change (sub2 flipped mid-day 2026-07-16). xterm.js has no
        # scrollback in the alt buffer, so every scroll path dies silently: the
        # ring replay and _history_seed_bytes paint into the hidden normal
        # buffer and a phone's touch pan finds nothing to scroll. Pin inline
        # rendering — the seed/ring/scrollback contract depends on it.
        env["CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN"] = "1"
        if RESUME_MODAL_SUPPRESS:
            # Suppress the resume modal at the source (see the knob's comment
            # block). setdefault so an operator export still wins.
            env.setdefault("CLAUDE_CODE_RESUME_THRESHOLD_MINUTES",
                           RESUME_MODAL_FLOOR_MIN)
        if s.config_dir:                     # non-default subscription account
            env["CLAUDE_CONFIG_DIR"] = s.config_dir
        else:
            # default = plain ~/.claude, always: an operator-exported
            # CLAUDE_CONFIG_DIR would strand transcripts where our globs
            # (config_dir or ~/.claude) never look.
            env.pop("CLAUDE_CONFIG_DIR", None)
        # Guarantee claude never opens onto the onboarding/theme screen when
        # the dir already holds a login…
        _ensure_onboarded(s.config_dir)
        # …nor onto the per-folder trust dialog: every cwd here was chosen by
        # the human via the projects layer, and a handoff to a fresh account
        # re-asks it for every path that login has never seen.
        _ensure_trusted(s.config_dir, s.workdir())

    def hook_setup(self, s):
        return s._write_hook_settings()

    def transcript_globs(self, s):
        base = s.config_dir or os.path.expanduser("~/.claude")
        return [f"{base}/projects/*/{s.session_id}.jsonl"]

    def slim_event(self, s, line):
        return s._slim_event_claude(line)

    def bg_probe(self, s):
        return s._bg_probe_claude()


class CodexEngine(Engine):
    name, bin, routes_accounts = "codex", CODEX_BIN, False
    # An inherited OPENAI_API_KEY makes codex authenticate as METERED API
    # instead of the ChatGPT subscription — the exact shape of SCRUB_ENV's
    # nested-claude trap (gotcha #1), different name. Strip it.
    scrub_extra = ("OPENAI_API_KEY", "OPENAI_BASE_URL", "CODEX_API_KEY")

    def argv(self, s):
        # No --session-id analogue: codex assigns its own id, so the cid↔sid
        # binding is INVERTED vs claude — we learn the id from the first
        # SessionStart hook (_follow_session already handles id changes,
        # because claude rotates ids on compaction).
        argv = [self.bin]
        if s.resuming and s.session_id:
            argv += ["resume", s.session_id]
        argv += ["--no-alt-screen",              # inline mode; see slim note below
                 "-a", CODEX_APPROVAL,
                 "-s", CODEX_SANDBOX]
        if CODEX_BYPASS_HOOK_TRUST:
            # Without this, codex opens on a blocking "Hooks need review — N
            # hooks are new or changed" screen and, until a human answers it,
            # runs none of them: no Stop, no busy pill, no naming, no digest.
            # (Verified on clawd-head 2026-08-07: trusting them by hand from
            # the TUI recorded 7 trusted_hash entries and STILL didn't fire.)
            # The alternative — persisting the per-handler trust state
            # ourselves — means reproducing codex's private hashing scheme,
            # which would rot silently the moment it changes. Our hooks are
            # generated by this server and POST to this server, so there is
            # nothing here for the trust gate to protect us from. Companion
            # to _ensure_codex_trusted, which answers the directory gate.
            argv += ["--dangerously-bypass-hook-trust"]
        return argv

    def env(self, s, env):
        env["CODEX_HOME"] = CODEX_STORE
        # How the hook command knows WHICH session it belongs to. Claude gets a
        # per-session settings file via --settings; codex discovers hooks from
        # config layers only, so the file is shared machine-wide and the cid
        # rides in the env instead (hook processes are codex's children, so
        # they inherit it). A hand-run `codex` in a terminal therefore POSTs
        # with an empty cid — /hook drops unknown cids, which is what we want.
        env["HARNESS_CID"] = s.cid

    def hook_setup(self, s):
        _ensure_codex_hooks()
        _ensure_codex_trusted(s.workdir())
        rule = s.standing_rule()
        if rule:
            # codex has no --append-system-prompt; its project instructions
            # come from AGENTS.md in the cwd, and an AGENTS.override.md beside
            # it takes precedence. We write the rule there (prefixed to the
            # repo's own AGENTS.md text, so nothing upstream wrote is lost) and
            # hide it from git via .git/info/exclude — never .gitignore, which
            # would be a change to someone else's repo. See docs/CODEX-ENGINE.md
            # ("what isn't verified") — the override filename is from codex's
            # docs, not observed here.
            _ensure_codex_external_doc(s.workdir(), rule)
        return None

    def transcript_globs(self, s):
        # $CODEX_HOME/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl. Old
        # rollouts get zstd-compressed in place (.jsonl.zst) by codex's
        # maintenance pass — we only ever tail the LIVE one, but the glob
        # tolerates the suffix so a resume can still find a just-archived file.
        return [f"{CODEX_STORE}/sessions/*/*/*/rollout-*{s.session_id}.jsonl",
                f"{CODEX_STORE}/sessions/*/*/*/*{s.session_id}*.jsonl"]

    def slim_event(self, s, line):
        return s._slim_event_codex(line)


ENGINES = {"claude": ClaudeEngine(), "codex": CodexEngine()}


_EXT_DOC_MARK = "<!-- clawd-harness external-project rule; regenerated each spawn -->"


def _ensure_codex_external_doc(path, rule):
    """Write `<repo>/AGENTS.override.md` = our standing rule + the repo's own
    AGENTS.md (if any), and list it in .git/info/exclude so no `git add -A`
    in the session can sweep it into a PR. Refuses to touch a file the repo
    itself tracks under that name (someone else's override is not ours to
    rewrite) — logged, the session then runs without the codex-side rule."""
    try:
        doc = os.path.join(path, "AGENTS.override.md")
        rc, tracked = _git(path, "ls-files", "--error-unmatch", "AGENTS.override.md",
                           timeout=5)
        if rc == 0:
            print(f"[external] {path}: AGENTS.override.md is tracked upstream — "
                  "not overwriting; codex session runs WITHOUT the PR rule", flush=True)
            return False
        own = ""
        agents = os.path.join(path, "AGENTS.md")
        if os.path.isfile(agents):
            with open(agents, encoding="utf-8", errors="replace") as f:
                own = f.read()
        body = _EXT_DOC_MARK + "\n" + rule + ("\n\n---\n\n" + own if own.strip() else "")
        tmp = doc + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(body)
        os.replace(tmp, doc)
        # .git may be a FILE for a linked worktree — resolve the real git dir
        rc, gitdir = _git(path, "rev-parse", "--git-common-dir", timeout=5)
        if rc == 0 and gitdir:
            gitdir = gitdir if os.path.isabs(gitdir) else os.path.join(path, gitdir)
            info = os.path.join(gitdir, "info")
            os.makedirs(info, exist_ok=True)
            excl = os.path.join(info, "exclude")
            have = ""
            if os.path.isfile(excl):
                with open(excl, encoding="utf-8", errors="replace") as f:
                    have = f.read()
            if "AGENTS.override.md" not in have:
                with open(excl, "a", encoding="utf-8") as f:
                    f.write(("" if have.endswith("\n") or not have else "\n")
                            + "AGENTS.override.md\n")
        return True
    except Exception as e:
        print(f"[external] {path}: could not write AGENTS.override.md: {e}", flush=True)
        return False


def _codex_hook_command():
    """The one-liner every codex hook runs: POST the event's stdin JSON to
    /hook, tagged with the cid we planted in the child env."""
    return (f"curl -sS -m 2 -X POST "
            f"'http://127.0.0.1:{PORT}/hook?t={TOKEN}&cid='\"$HARNESS_CID\" "
            f"--data-binary @- >/dev/null 2>&1 || true")


# Wrapper tags codex injects as `role:"user"` messages that are context, not
# conversation. Verified against a real rollout on heart (2026-08-07); harmless
# to over-list, so add freely as new ones show up.
CODEX_INJECTED_TAGS = ("<environment_context>", "<skills_instructions>",
                       "<multi_agent_mode>", "<user_instructions>",
                       "<project_doc>", "<agents_md>")


def _collect_codex_text(content):
    """Text out of a codex message's content parts.

    claude's _collect_text only knows blocks of `type:"text"`; codex uses the
    Responses-API spelling — `input_text` on the way in, `output_text` on the
    way out. Reusing claude's helper silently returned "" for every message,
    which read as "the parser is broken" when in fact only this one lookup
    was (clawd-head, 2026-08-07: tool calls parsed, messages didn't)."""
    if isinstance(content, str):
        return content
    out = []
    for b in content or []:
        if isinstance(b, str):
            out.append(b)
        elif isinstance(b, dict):
            if b.get("type") in ("output_text", "input_text", "text", None) \
                    and isinstance(b.get("text"), str):
                out.append(b["text"])
    return "\n".join(t for t in out if t).strip()


def _codex_signed_in():
    """Cheap "is codex usable" probe: its credential file exists. Mirrors
    _creds_state's job for claude but far simpler — there's one login per
    machine and no rotation to race with. Never blocks a spawn; a False just
    labels the session so the user knows why the TUI is asking them to log in
    (same never-ambush-the-user principle as the claude spawn gate)."""
    try:
        return (Path(CODEX_STORE) / "auth.json").exists()
    except Exception:
        return True                              # unreadable ≠ signed out


# Codex usage, cached. The probe spawns a process, so it is never on a request
# path: accounts_meta serves whatever is cached and kicks a refresh when stale.
CODEX_USAGE_TTL = float(os.environ.get("CODEX_USAGE_TTL", "300"))
_codex_usage = {"data": None, "at": 0.0, "err": "", "busy": False}
_codex_usage_lock = threading.Lock()


def _codex_app_server_call(methods, timeout=15.0):
    """Run a short `codex app-server` session and return {method: result}.

    Newline-delimited JSON-RPC over stdio (no "jsonrpc" field), handshake is
    initialize → initialized → calls. This is codex's own app-server protocol —
    the same one its VS Code extension speaks — and it is marked EXPERIMENTAL,
    so treat every part of it as liable to change and degrade to {} rather than
    raising. Costs one process per call, hence the cache above."""
    try:
        proc = subprocess.Popen(
            [CODEX_BIN, "app-server"], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1, env={**os.environ, "CODEX_HOME": CODEX_STORE})
    except Exception as e:
        return {}, f"spawn failed: {e}"
    out, deadline = {}, time.time() + timeout

    def _reader():
        try:
            for ln in proc.stdout:
                try:
                    o = json.loads(ln)
                except Exception:
                    continue
                if isinstance(o, dict) and o.get("id") is not None:
                    out[o["id"]] = o
        except Exception:
            pass

    threading.Thread(target=_reader, daemon=True).start()

    def _send(o):
        proc.stdin.write(json.dumps(o) + "\n")
        proc.stdin.flush()

    err = ""
    try:
        _send({"id": 0, "method": "initialize",
               "params": {"clientInfo": {"name": "clawd-harness",
                                         "title": "clawd-harness",
                                         "version": "1.0.0"},
                          "capabilities": {"experimentalApi": True}}})
        while 0 not in out and time.time() < deadline:
            time.sleep(0.1)
        if 0 not in out:
            raise RuntimeError("no initialize reply")
        _send({"method": "initialized", "params": {}})
        for i, m in enumerate(methods, start=1):
            _send({"id": i, "method": m, "params": {}})
        while len(out) < len(methods) + 1 and time.time() < deadline:
            time.sleep(0.1)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    res = {m: (out.get(i) or {}).get("result")
           for i, m in enumerate(methods, start=1)}
    if not err and not any(v for v in res.values()):
        err = "no result"
    return res, err


def _codex_usage_refresh():
    """Refresh the cached codex usage snapshot (blocking; call in a thread)."""
    with _codex_usage_lock:
        if _codex_usage["busy"]:
            return
        _codex_usage["busy"] = True
    try:
        if not _codex_signed_in():
            _codex_usage.update(data=None, err="signed out", at=time.time())
            return
        res, err = _codex_app_server_call(
            ["account/rateLimits/read", "account/read"])
        rl = res.get("account/rateLimits/read") or {}
        acct = (res.get("account/read") or {}).get("account") or {}
        snap = rl.get("rateLimits") or {}
        prim, sec = snap.get("primary") or {}, snap.get("secondary") or {}

        def _win(w):
            if not w:
                return None
            return {"pct": w.get("usedPercent"),
                    "windowMins": w.get("windowDurationMins"),
                    "resetsAt": w.get("resetsAt")}

        windows = [w for w in (_win(prim), _win(sec)) if w]
        data = None
        if windows or acct:
            credits = snap.get("credits") or {}
            data = {
                "email": acct.get("email") or "",
                "plan": acct.get("planType") or snap.get("planType") or "",
                # The most-constrained window is the headline number, matching
                # how a claude card reads.
                "pct": max([w["pct"] for w in windows
                            if isinstance(w.get("pct"), (int, float))],
                           default=None),
                "windows": windows,
                "credits": {"has": bool(credits.get("hasCredits")),
                            "unlimited": bool(credits.get("unlimited")),
                            "balance": credits.get("balance")},
                "limitReached": snap.get("rateLimitReachedType") or "",
            }
        _codex_usage.update(data=data, err="" if data else (err or "no data"),
                            at=time.time())
        if data:
            print(f"[codex usage] {data.get('plan') or '?'} "
                  f"{data.get('pct')}% used", flush=True)
        else:
            print(f"[codex usage] unavailable ({err})", flush=True)
    except Exception as e:                       # never let this kill a sweep
        _codex_usage.update(data=None, err=f"{type(e).__name__}: {e}",
                            at=time.time())
    finally:
        _codex_usage["busy"] = False


def codex_usage_meta():
    """Cached codex usage for the accounts panel, refreshing in the background
    when stale. Returns None when codex isn't installed/signed in, so the UI
    simply shows no codex card rather than an error."""
    if not _codex_signed_in():
        return None
    if (time.time() - _codex_usage["at"] > CODEX_USAGE_TTL
            and not _codex_usage["busy"]):
        threading.Thread(target=_codex_usage_refresh, daemon=True).start()
    d = _codex_usage["data"]
    return {"engine": "codex", "status": "ready" if d else "unknown",
            "checkedAt": _codex_usage["at"], "error": _codex_usage["err"],
            **(d or {})}


_codex_hooks_written = False


def _ensure_codex_hooks():
    """Install our hook handlers into $CODEX_HOME/hooks.json, MERGING with
    whatever the user already has there (theirs wins on conflict; we only add
    our own entries, marked so a re-run replaces them instead of stacking up).

    Unlike claude's `--settings <file>`, codex has no per-invocation hook flag —
    hooks come from config layers only. So this file is machine-wide and shared
    with the user's own hand-run codex sessions; see CodexEngine.env for why
    that's safe (cid rides in the env; /hook drops unknown cids)."""
    global _codex_hooks_written
    if _codex_hooks_written:
        return
    path = Path(CODEX_STORE) / "hooks.json"
    mark = "clawd-harness"
    entry = {"hooks": [{"type": "command", "command": _codex_hook_command(),
                        "timeout": 5, "statusMessage": mark}]}
    # PermissionRequest is codex's analogue of claude's Notification — the
    # "blocked, needs a human" signal. Verified present in 0.147.0.
    events = ["SessionStart", "SessionEnd", "UserPromptSubmit", "Stop",
              "PreToolUse", "PostToolUse", "PermissionRequest"]
    try:
        cur = json.loads(path.read_text()) if path.exists() else {}
    except Exception:
        cur = {}                              # unparseable → don't clobber blind
        print(f"[codex] {path} is unreadable — leaving it alone; "
              "codex sessions will have no turn signal", flush=True)
        return
    hooks = cur.setdefault("hooks", {})
    for ev in events:
        lst = [e for e in hooks.get(ev, [])   # drop our previous entries
               if mark not in json.dumps(e)]
        e = dict(entry)
        if ev in ("PreToolUse", "PostToolUse"):
            e = {"matcher": "*", **entry}
        hooks[ev] = lst + [e]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cur, indent=2))
        os.chmod(path, 0o600)                 # it embeds our token
    except Exception as e:
        print(f"[codex] could not write {path}: {e}", flush=True)
        return
    _codex_hooks_written = True
    print(f"[codex] hooks installed → {path}", flush=True)


def _ensure_codex_trusted(workdir):
    """Pre-trust a project directory in $CODEX_HOME/config.toml.

    A fresh codex session in a directory it hasn't seen opens on a BLOCKING
    prompt — "Do you trust the contents of this directory?" — and sits there
    until a human answers. That's the same never-ambush-the-user problem
    _ensure_onboarded solves for claude, and the same fix: state the answer
    up front so the screen never appears. (The companion gate, "Hooks need
    review", is handled by --dangerously-bypass-hook-trust in the argv:
    persisting a per-handler trust hash would mean reproducing codex's
    hashing scheme, which is its private business and would silently rot.)

    Append-only and idempotent — we never rewrite the user's TOML, only add a
    block for a path that has none. Verified against 0.147.0 on clawd-head."""
    p = os.path.abspath(workdir or "")
    if not p:
        return
    cfg = Path(CODEX_STORE) / "config.toml"
    header = f'[projects."{p}"]'
    try:
        cur = cfg.read_text() if cfg.exists() else ""
    except Exception:
        return                                   # unreadable → leave it alone
    if header in cur:
        return                                   # already known (trusted or not)
    try:
        cfg.parent.mkdir(parents=True, exist_ok=True)
        with open(cfg, "a") as f:
            if cur and not cur.endswith("\n"):
                f.write("\n")
            f.write(f'\n{header}\ntrust_level = "trusted"\n')
    except Exception as e:
        print(f"[codex] could not pre-trust {p}: {e}", flush=True)
        return
    print(f"[codex] pre-trusted project dir {p}", flush=True)


# ── PTY-backed agent session (claude | codex) ─────────────────────────────────
class ClaudeSession:
    """One interactive agent CLI in a PTY, streamed to the websocket clients
    currently *subscribed* to it. Owned by a SessionManager. Which CLI is
    `self.engine` (claude | codex) — everything engine-specific is behind the
    Engine layer above. (Name kept for now: it appears in the registry, the
    respawn-clone introspection, and every log line.)"""

    def __init__(self, manager, cid, session_id, resuming, pid="",
                 title="", desc="", tab="", prompt_count=0, first_prompt="",
                 created=0.0, last_active=0.0, prompted_at=0.0,
                 account="default", config_dir="", ceremony=False,
                 pinned=0.0, test_hint="", model="", ctx_tokens=0,
                 engine="claude"):
        self.manager = manager
        # Which agent CLI drives this session ("claude" | "codex"). Chosen at
        # spawn and durable: a --resume must reach for the same binary, and an
        # unknown/legacy value falls back to claude so an old registry (written
        # before engines existed) resumes exactly as it did.
        self.engine = engine if engine in ENGINES else "claude"
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
        # The pin's blue line: "what the human has to go do to close this out"
        # (TEST_SYS_PROMPT). Written when the session is pinned and refreshed on
        # every Stop while it stays pinned; cleared on unpin so a re-pin asks
        # again. Durable — a restart shouldn't blank the board's instructions.
        self.test_hint = test_hint
        self._hint_at_prompt = 0                  # prompt_count the hint was derived at

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
        self._limit_raw = b""                     # rolling RAW PTY bytes, for the limit banner/modal scan
        self._limit_seen_at = 0.0                 # cooldown anchor for banner-triggered rescues
        self._onboard_tail = ""                   # rolling de-ANSI'd PTY text, for the onboarding-screen scan
        self._onboard_deadline = 0.0              # scan window end; start() arms it, a match disarms it
        self._onboard_rescues = 0                 # respawns burned on this cid (carried across; caps the loop)
        self._gate_raw = b""                      # rolling RAW PTY bytes, for the resume-gate scan
        self._gate_deadline = 0.0                 # scan window end; a resume start() arms it, a match/send disarms it
        self._started_evt = threading.Event()     # set on SessionStart — "the TUI is up"
        self._gate_resolved_evt = threading.Event()  # resume gate answered/expired/never-armed —
                                                     # "safe to type a prompt" (see wait_ready)
        self.last_tool = None
        self.digest = ""                          # volatile "what it's doing now" (LLM, refreshed each Stop)
        self.auto_tldr_armed = False              # volatile: browser send seen, no Stop yet (AUTO_TLDR)
        self.blocked_on = None                    # the open question if it ended asking the human (LLM)
        self.last_answer = ""                     # last Stop's assistant message — durable (backfilled on resume)
        self.settings_path = None

    @property
    def eng(self):
        """The Engine strategy object for this session."""
        return ENGINES.get(self.engine) or ENGINES["claude"]

    # -- registry shape --------------------------------------------------------
    def to_registry(self):
        return {"engine": self.engine,
                "cid": self.cid, "pid": self.pid, "session_id": self.session_id,
                "title": self.title, "desc": self.desc, "tab": self.tab,
                "prompt_count": self.prompt_count, "first_prompt": self.first_prompt,
                "created": self.created, "last_active": self.last_active,
                "prompted_at": self.prompted_at,
                "account": self.account, "config_dir": self.config_dir,
                "ceremony": self.ceremony, "pinned": self.pinned,
                "test_hint": self.test_hint,
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
        # Live (non-persisted) state the respawn must also keep: the PTY
        # geometry. Not a ctor param — it belongs to whoever is viewing, not
        # to the session — but the replacement must open at the same dims
        # (start() reads them) or the viewer's screen comes back mangled.
        # The OWNER rides across in adopt_viewers().
        fresh.tty_cols, fresh.tty_rows = self.tty_cols, self.tty_rows
        return fresh

    def workdir(self):
        """Where this session's claude runs — its project's repo path."""
        proj = self.manager.projects.get(self.pid)
        return proj.path if proj else WORKDIR

    def project(self):
        return self.manager.projects.get(self.pid)

    def standing_rule(self):
        """The project-level rule this session is born with ("" for most
        projects; the branch-and-PR rule for kind="external"). Ceremony
        sessions are exempt — a sign-in screen needs no git discipline."""
        if self.ceremony:
            return ""
        proj = self.project()
        return proj.standing_rule() if proj else ""

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
                "engine": self.engine,
                "title": self.title or self._fallback_title(),
                "desc": self.desc or "",
                "tab": self.tab or "",
                "named": bool(self.title),
                "busy": self.busy, "waiting": self.waiting, "tool": self.last_tool,
                "status": status, "bg": self.bg,
                "digest": self.digest or "",
                "blocked_on": self.blocked_on or "",
                # truncated hard: this rides every `sessions` broadcast. The
                # fuller text is in the Stop hook / transcriptTail. Durable —
                # _backfill_last_answer restores it across restarts, so a
                # controller can always retrieve "what did it last say".
                "lastAnswer": (self.last_answer or "")[:280],
                "sessionId": self.session_id,
                "promptCount": self.prompt_count,
                "lastActive": self.last_active,
                "promptedAt": self.prompted_at,
                "created": self.created,
                "alive": self.alive,
                "account": self.account,
                "pinned": self.pinned,
                "testHint": self.test_hint or "",   # 📌 board: the human's verification step
                "model": self.model,
                "ctxTokens": self.ctx_tokens}

    def _bg_probe_claude(self):
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
        bg = self.eng.bg_probe(self)
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
        # Open at the geometry this session already has — an in-place respawn
        # (handoff / onboarding heal) carries the viewer's dims across in
        # clone_for_respawn, so claude boots straight into the phone's width
        # instead of painting a 120-col frame that the phone then renders
        # shredded. Fresh sessions still get the COLS×ROWS defaults.
        self._set_winsize(master, self.tty_rows or ROWS, self.tty_cols or COLS)

        env = dict(os.environ)
        env["TERM"] = "xterm-256color"
        env["COLORTERM"] = "truecolor"          # xterm.js renders 24-bit; let claude emit it
        env["COLUMNS"] = str(COLS)
        env["LINES"] = str(ROWS)
        for k in SCRUB_ENV:                      # pristine top-level + subscription auth
            env.pop(k, None)
        for k in self.eng.scrub_extra:           # engine's own metered-API trap
            env.pop(k, None)
        # Everything engine-specific about the child's environment — the
        # alt-screen pin, the config-dir variable, onboarding — lives in the
        # Engine. Every spawn path (fresh, resume, handoff, restart) funnels
        # through here, so it's the one place that has to be right.
        self.eng.env(self, env)

        # PTY tripwire: FRESH spawns only. A --resume REPAINTS recent
        # conversation, so a session that ever quoted the picker text (this
        # repo's sources; the session that wrote this fix) re-trips the scan
        # on every resume and gets respawn-cycled (sub2/1951a6f5 2026-07-16).
        # Resumes can't hit the real ambush anyway — they skip onboarding even
        # on an unflagged dir (austinmax ran resumed handoffs for days
        # half-onboarded) — and the _ensure_onboarded seed above covers them.
        self._onboard_deadline = 0.0 if self.resuming \
            else time.time() + ONBOARD_SCAN_WINDOW

        # The resume gate arms on exactly the OPPOSITE spawns: it is a modal
        # only --resume can raise, so a fresh session must never scan for it.
        self._gate_raw = b""
        self._gate_deadline = (time.time() + RESUME_GATE_WINDOW) if (
            self.resuming and RESUME_GATE and self.eng.resume_gate_key
            and not self.ceremony) else 0.0
        if not self._gate_deadline or RESUME_MODAL_SUPPRESS:
            # Nothing to wait for before typing: the gate never armed (fresh
            # spawn, other engine, ceremony), or suppression keeps the modal
            # from painting at all — the scan stays armed as a backstop, but a
            # delivery need not wait out its window. Armed WITHOUT suppression,
            # the event is set by whichever comes first: the scan answering,
            # the window expiring, or any write to the PTY (which disarms the
            # scan for good — resolved either way).
            self._gate_resolved_evt.set()

        self.settings_path = self.eng.hook_setup(self)
        cmd = self.eng.argv(self)

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
        print(f"[session {self.cid[:8]}] {self.engine} pid={self.os_pid} "
              f"session_id={self.session_id} account={self.account} "
              f"({'resumed' if self.resuming else 'new'})", flush=True)

        threading.Thread(target=self._pump_pty, daemon=True).start()
        threading.Thread(target=self._tail_transcript, daemon=True).start()
        # Backfill: a resumed session that has a transcript but no title (e.g. it
        # only ever reached prompt 1, so the old start-of-turn naming missed it)
        # gets named now from its existing content.
        if self.resuming and not self.title:
            threading.Thread(target=self._regenerate_name, daemon=True).start()
        # lastAnswer lives only in memory; a restart would blank it for every
        # session until its next turn — recover it from the transcript instead.
        if self.resuming and not self.last_answer:
            threading.Thread(target=self._backfill_last_answer, daemon=True).start()

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
        """Handle one hook callback (from the engine via /hook) → update state,
        fan a slim event out to every client (menu badges), and trigger AI
        naming. Engine-blind on purpose: codex emits the same event names and
        the same payload fields (`hook_event_name`, `session_id`,
        `transcript_path`, `prompt`, `tool_name`, `last_assistant_message`),
        which is the single fact that makes a second engine cheap."""
        ev = obj.get("hook_event_name", "?")
        # codex's name for "blocked, needs a human" — claude calls it
        # Notification. Normalise at the door so one state machine serves both.
        if ev == "PermissionRequest":
            ev = "Notification"
            obj = {**obj, "hook_event_name": ev}
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
            if self.eng.routes_accounts \
                    and not self.ceremony and acct and (acct.broken
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
            if data["last"]:
                self.last_answer = data["last"][:500]
            # Turn complete → the transcript now has a real exchange. Name it if
            # it's still unnamed (so even a 1-prompt session gets a title), and
            # re-name at the 1/3/6/9/… milestones to sharpen as it grows.
            if (not self.title) or name_at_prompt(self.prompt_count):
                threading.Thread(target=self._regenerate_name, daemon=True).start()
            # The digest is volatile — refresh it every turn (not just at the
            # naming milestones) so live session state stays current for the
            # controller / dashboard. Cheap, async, in-memory only.
            threading.Thread(target=self._regenerate_digest, daemon=True).start()
            # Parked on the 📌 board? Re-derive "what the human must test" too —
            # a board session can be prompted in place, and the answer to "what
            # am I waiting on" moves with it.
            # …but only when a real exchange has happened since the last one:
            # /compact ends a turn too, and its post-compaction transcript is
            # exactly the wrong thing to re-derive a test step from.
            if self.pinned and self.prompt_count > self._hint_at_prompt:
                threading.Thread(target=self._regenerate_test_hint,
                                 daemon=True).start()
            # Turn over + idle = the safe moment to move this session off a
            # drained plan (no-ops fast in the common case).
            if self.eng.routes_accounts:
                threading.Thread(target=self.manager.maybe_handoff, args=(self,),
                                 daemon=True).start()
            # The absent reader's chip tap: a browser-armed prompt just ended
            # in a wall of text and nobody is subscribed — tap "tldr" for them.
            # The arm is consumed HERE, hit or miss, so a stale arm can never
            # fire on some later (possibly controller-driven) turn.
            armed, self.auto_tldr_armed = self.auto_tldr_armed, False
            if (AUTO_TLDR and armed and not self.ceremony and not self.pinned
                    and not self.clients and wants_auto_tldr(data["last"])):
                threading.Thread(target=self._auto_tldr, daemon=True).start()
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

    def _regenerate_test_hint(self):
        """Ask the LLM what the HUMAN has to go verify, for the 📌 pin board's
        blue line. Fired when a session is pinned and on every Stop while it
        stays pinned (prompting a parked session from the board can change what
        needs testing). Persisted, so the board survives a restart with its
        instructions intact. Silent no-op when naming is unconfigured."""
        if not self.pinned:                          # unpinned mid-flight — drop it
            return
        text = self._transcript_text_for_naming()
        if not text:
            return
        hint = generate_test_hint(text)
        if hint is None:                             # naming off, or call failed
            return
        hint = hint[:160]
        # Derived at this prompt count. The Stop-side refresh compares against
        # it so only a REAL new exchange re-asks — the turn that /compact itself
        # ends must not overwrite a good hint with one read off a summarized
        # transcript.
        self._hint_at_prompt = self.prompt_count
        if hint == self.test_hint:
            return
        self.test_hint = hint
        print(f"[test {self.cid[:8]}] {hint!r}", flush=True)
        self.manager.save_registry()
        self.manager.broadcast_sessions()

    def _on_pinned(self):
        """Everything that happens when a session lands on the 📌 board, in the
        order that matters. The test hint first, off the FULL transcript — then
        compact, which is the thing that would have thinned it."""
        self._regenerate_test_hint()
        self._compact_for_pin()

    def _compact_for_pin(self):
        """Type /compact into a freshly pinned session. Parking is exactly when
        compaction is free: nobody is waiting on the answer, and the return trip
        (you come back after testing, often days later) gets a full context
        window instead of an auto-compact firing mid-thought.

        Never mid-turn: `busy` would race the composer, `waiting` would answer
        a TUI prompt with the literal text "/compact", and background work means
        the CLI is still occupied. So we hold the door open until it's genuinely
        idle, bounded by PIN_COMPACT_WAIT — a session that never settles simply
        doesn't get compacted."""
        cmd = self.eng.compact_cmd
        if not (PIN_COMPACT and cmd) or self.ceremony or not self.prompt_count:
            return
        deadline = time.time() + PIN_COMPACT_WAIT
        while time.time() < deadline:
            if not (self.pinned and self.alive):
                return                    # unpinned or closed while we waited
            if not (self.busy or self.waiting or self.bg):
                break
            time.sleep(2.0)
        else:
            print(f"[session {self.cid[:8]}] pinned but never went idle in "
                  f"{PIN_COMPACT_WAIT:.0f}s — skipping the automatic {cmd}",
                  flush=True)
            return
        if not (self.pinned and self.alive):
            return
        print(f"[session {self.cid[:8]}] pinned → typing {cmd.strip()}", flush=True)
        self.send_message(cmd, control=True)      # ours, not the human's

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
        # Anything reaching the PTY disarms the resume-gate scan: a human at
        # the keyboard answers the modal themselves, and a harness send must
        # never have our CR fire between its text and its own submitting CR
        # (that would post half a prompt). The scan zeroes the deadline BEFORE
        # calling us, so its own keystroke isn't caught by this. A disarmed
        # scan can never fire again, so the gate is RESOLVED from here — this
        # is also how the scan's own answering CR flips the event.
        self._gate_deadline = 0.0
        self._gate_resolved_evt.set()
        try:
            os.write(self.master_fd, data)
        except OSError:
            pass

    def wait_ready(self, timeout=20.0):
        """Safe-to-type barrier for a freshly (re)spawned session: SessionStart
        has fired AND the resume gate is RESOLVED (answered, expired, or never
        armed / suppressed). Waiting on _started_evt alone is the addenda race:
        the resume modal can paint after SessionStart, and the first write
        disarms the scan — so a prompt delivered on "started" can land in a
        NUMBERED modal with the scan dead. True = ready; False = timed out
        (callers log and proceed — a bound that strands a prompt is worse than
        the race it guards, and the wait itself never writes to the PTY)."""
        deadline = time.time() + timeout
        started = self._started_evt.wait(timeout)
        resolved = self._gate_resolved_evt.wait(max(0.0, deadline - time.time()))
        return started and resolved

    def send_message(self, text: str, control: bool = False):
        """High-level: type a message, let the paste settle, then submit (CR).

        control=True marks a send the HARNESS made, not the human: a TUI slash
        command like /compact. Those are handled by the TUI itself and fire no
        UserPromptSubmit, so the no-hook bounce detector would read a perfectly
        healthy delivery as a walled plan and go hunting for a rescue — and
        they aren't prompts, so they must not touch `prompted_at` (the "when
        did a human last say something" clock behind tab ages)."""
        if not control:
            self.prompted_at = time.time()   # belt-and-braces: a bounced prompt fires no hook
        pre_hooks = self.hook_count
        self.write(text.encode("utf-8"))
        # Short one-liners only need to clear the 0.6s burst cliff; big or
        # multi-line pastes take longer to finalize, so keep the full settle.
        big = len(text) > 280 or text.count("\n") >= 1
        # The settle is per-ENGINE: it's tuned to one TUI's paste heuristic
        # (gotcha #2), and another CLI's is its own empirical question. A
        # control send takes the long settle regardless of length: it's a slash
        # command, and what has to finish before the CR is the autocomplete
        # menu closing, not a paste burst.
        time.sleep(self.eng.send_settle(big or control))
        self.write(b"\r")
        if not control:
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

    def _auto_tldr(self):
        """Deliver the AUTO_TLDR chip tap (armed + gated in on_hook's Stop).
        A short grace re-checks everything that can change at the Stop
        boundary: a viewer arriving to read the wall themselves, a new prompt
        (any hook moves hook_count), or the session going busy/away — all of
        those win and the tap is dropped. Sent as a normal message on purpose:
        it's a real prompt, so the bounce watchdog / limit rescues cover it
        like anything a human sends."""
        pre_hooks = self.hook_count
        time.sleep(AUTO_TLDR_DELAY)
        if (not self.alive or self.busy or self.waiting or self.clients
                or self.hook_count != pre_hooks):
            return
        print(f"[session {self.cid[:8]}] auto-tldr: long reply, nobody "
              f"watching — sending {AUTO_TLDR_TEXT!r}", flush=True)
        log_prompt(self, AUTO_TLDR_TEXT, "auto")
        # Through the same preflight as a human send: it's a real model prompt
        # and must not bounce off an exhausted pool (plan's Auto-TLDR section).
        # One turn either way — a move re-delivers THIS text once, never twice.
        self.manager.send_prompt(self.cid, AUTO_TLDR_TEXT, via="auto")

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
            # Both PTY tripwires read CLAUDE's screens (its limit banner, its
            # onboarding picker) and both act by moving the session between
            # subscription accounts — meaningless on an engine outside the
            # router, and a false positive there could respawn a healthy
            # session for no reason.
            if self.eng.routes_accounts:
                self._scan_for_limit(chunk)
                if self._onboard_deadline:
                    self._scan_for_onboarding(chunk)
            # NOT fenced behind routes_accounts: answering a modal is a TUI
            # act, not a subscription-router one. Its fence is the Engine's
            # own resume_gate_key, which is empty everywhere but claude.
            if self._gate_deadline:
                self._scan_for_resume_gate(chunk)
        self.alive = False
        # Stamp the account: the poller must not consume this grant for
        # SUB_REFRESH_EXIT_GRACE — the dying claude's last token rotation
        # may still be settling (or stranded, if it was killed mid-refresh).
        self.manager.acct_last_exit[self.account or "default"] = time.time()
        print(f"[session {self.cid[:8]}] PTY closed / {self.engine} exited", flush=True)
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
        # Buffer RAW bytes and re-strip the whole window each read (the
        # resume-gate lesson: a chunk boundary inside an escape sequence leaks
        # junk like "38;5;246m" into per-chunk flattened text). Two needles
        # because the CLI paints walls two ways: the classic banner is one
        # styled line (real spaces → _LIMIT_BANNER_RE on space-collapsed
        # text), the extra-usage-credits dialog is an ink box padded with
        # cursor motion (space-free after de-ANSI → _LIMIT_MODAL_RE on
        # whitespace-stripped text).
        self._limit_raw = (self._limit_raw + chunk)[-LIMIT_RAW_MAX:]
        stripped = _PTY_ANSI_RE.sub(b"", self._limit_raw).decode("utf-8", "ignore")
        if not (_LIMIT_BANNER_RE.search(re.sub(r"\s+", " ", stripped))
                or _LIMIT_MODAL_RE.search(re.sub(r"\s+", "", stripped))):
            return
        self._limit_raw = b""                    # don't re-match this paint
        now = time.time()
        if now - self._limit_seen_at < BOUNCE_COOLDOWN:
            return
        self._limit_seen_at = now
        print(f"[session {self.cid[:8]}] limit banner/modal in the PTY on "
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

    def _scan_for_resume_gate(self, chunk):
        """Answer claude's resume gate the moment it paints, so a session that
        was resumed in the background is already compacted by the time a human
        opens it — instead of sitting frozen on a modal nobody was there to
        click. Option 1 is pre-highlighted and runs /compact, so the answer is
        one bare CR (Engine.resume_gate_key).

        Armed only inside RESUME_GATE_WINDOW seconds of a --resume launch, and
        one-shot. There is no confirming oracle for this modal (the status file
        reads "idle" while it is up), so the guard is the needle's narrowness
        plus a blast radius of one CR into an empty composer."""
        if time.time() >= self._gate_deadline:
            self._gate_deadline = 0.0            # window over — stop scanning entirely
            self._gate_raw = b""
            self._gate_resolved_evt.set()        # no modal inside the window = resolved
            return
        # Buffer RAW bytes and de-ANSI the whole window each time, rather than
        # flattening per chunk and concatenating the text (what the limit and
        # onboarding scans do). A chunk boundary can fall inside an escape
        # sequence, and a half-stripped escape leaks literal junk like "38;5;
        # 246m" into the middle of the needle — measured: per-chunk flattening
        # misses the modal entirely at small chunk sizes. Re-stripping a few KB
        # per read costs nothing and is only armed for one window per resume.
        self._gate_raw = (self._gate_raw + chunk)[-GATE_RAW_MAX:]
        if not _RESUME_GATE_RE.search(_flat_pty(self._gate_raw)):
            return
        self._gate_deadline = 0.0                # one shot per session object
        self._gate_raw = b""
        print(f"[session {self.cid[:8]}] resume gate on {self.account} — "
              f"accepting 'resume from summary' (auto-/compact)", flush=True)
        self.write(self.eng.resume_gate_key)

    # -- read channel: transcript JSONL -> structured events -------------------
    def _find_transcript(self):
        # Fallback locator, by session-id, for when no hook has told us the
        # path yet. Claude: across all project dirs (robust to path encoding),
        # under THIS session's account config dir — a session spawned under a
        # non-default account writes its transcript there. Codex: under the
        # date-sharded rollout tree. Both are the Engine's business; newest
        # wins when a glob matches more than one.
        if not self.session_id:
            return None                          # codex: no id until SessionStart
        for pat in self.eng.transcript_globs(self):
            hits = sorted(glob.glob(pat), key=os.path.getmtime, reverse=True)
            if hits:
                return hits[0]
        return None

    def _follow_session(self, obj):
        """Track the live transcript file + session id from a hook payload. A
        compaction (or resume) rotates claude's session file mid-run; following
        it keeps the tail on the live file and makes a daemon restart resume the
        current session instead of a stale pre-rotation one.

        It is ALSO how a codex session learns its id at all: codex has no
        --session-id to preset, so a fresh one spawns with an empty id and the
        first SessionStart fills it in — the same code path, because claude
        already had to tolerate its id changing underneath us."""
        tpath = obj.get("transcript_path")
        if tpath:
            self._live_transcript = os.path.expanduser(tpath)
        sid = obj.get("session_id")
        if sid and sid != self.session_id:
            print(f"[session {self.cid[:8]}] "
                  f"{'learned id' if not self.session_id else 'rotated'} "
                  f"{self.session_id or '(none)'} -> {sid}", flush=True)
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
        The OUTPUT shape is the harness's own (role/text/tools/…) and is
        engine-independent — every consumer above this line (transcript view,
        naming seed, digests, search, history seed) stays engine-blind. Only
        the INPUT format differs, so the parse is the Engine's."""
        return self.eng.slim_event(self, line)

    def _slim_event_claude(self, line: str):
        """claude's transcript JSONL → slim events. Shapes mirror
        clawd-tg-claude/bot.py's stream-json handling."""
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

    def _slim_event_codex(self, line: str):
        """codex's rollout JSONL → the same slim events.

        Format (0.147.0): one JSON object per line, `{"timestamp":…,
        "type":<kind>, "payload":{…}}`. The kinds we care about:
          session_meta   — header; carries the session id + cwd
          response_item  — the conversation itself; payload.type is
                           `message` (role user/assistant, content parts),
                           `function_call` / `local_shell_call` (tool use),
                           `function_call_output` (tool result)
          event_msg      — CLI-side events; payload.type `token_count` carries
                           usage + rate_limits, `turn_started`/`turn_complete`
                           bound the turn.
        Written defensively: unknown kinds return None rather than raising, and
        both the payload-wrapped and flat spellings are accepted, because this
        format is versioned by a CLI we don't control. Anything unrecognised
        simply doesn't render — it never breaks the tail."""
        if not line:
            return None
        try:
            obj = json.loads(line)
        except Exception:
            return None
        kind = obj.get("type")
        p = obj.get("payload")
        p = p if isinstance(p, dict) else obj    # tolerate an unwrapped line
        ptype = p.get("type") or kind

        if ptype in ("session_meta", "turn_context"):
            # Header lines: harvest the model for the splash card, emit nothing.
            m = p.get("model") or (p.get("turn_context") or {}).get("model")
            if isinstance(m, str) and m:
                self.model = m
            return None

        if ptype == "token_count":
            # The context-window number for the splash card: "how full is the
            # window right now", matching what the claude branch computes.
            # LAST_token_usage, not total_ — total_ is cumulative across the
            # whole session and blows past the context window (346k against a
            # 258k window on the rollout this was written from). The input
            # side alone is the occupancy; output is what left. The
            # denominator, if we ever show a fraction, is
            # info.model_context_window.
            info = p.get("info") or {}
            usage = info.get("last_token_usage") or info
            tok = usage.get("input_tokens") or 0
            if tok:
                self.ctx_tokens = tok
            return None

        if ptype in ("message", "user_message", "agent_message"):
            role = p.get("role") or ("user" if ptype == "user_message"
                                     else "assistant")
            # Codex opens every session by injecting its own preamble as
            # `role:"developer"` messages (skills instructions, the /root
            # team prompt, multi-agent mode) plus a `role:"user"`
            # <environment_context> block. None of that is conversation: left
            # in, it heads the transcript view, poisons the naming/digest seed
            # and eats the phone's history seed with boilerplate. Only the two
            # real roles survive, and injected context blocks are dropped by
            # their wrapper tag.
            if role not in ("user", "assistant"):
                return None
            text = p.get("text")
            if not isinstance(text, str):
                text = _collect_codex_text(p.get("content") or [])
            clean = _strip_noise(text or "").strip()
            if not clean or clean.startswith(CODEX_INJECTED_TAGS):
                return None
            if role == "assistant":
                return {"role": "assistant", "text": clean}
            return {"role": "user", "text": clean}

        if ptype in ("function_call", "local_shell_call", "custom_tool_call",
                     "mcp_tool_call", "command_execution", "web_search_call"):
            name = (p.get("name") or p.get("tool_name")
                    or ptype.replace("_call", ""))
            args = p.get("arguments") or p.get("command") or p.get("input")
            if isinstance(args, (dict, list)):
                args = json.dumps(args)[:400]
            elif isinstance(args, str):
                args = args[:400]
            return {"role": "assistant",
                    "tools": [{"name": name, "input": args or ""}]}

        if ptype in ("function_call_output", "custom_tool_call_output",
                     "tool_result"):
            # `output` is a LIST of Responses-API content parts, not a string —
            # str()ing it renders a python repr into the transcript view.
            out = p.get("output")
            if out is None:
                out = p.get("result") or ""
            if isinstance(out, list):
                out = _collect_codex_text(out)
            elif isinstance(out, dict):
                out = out.get("output") or json.dumps(out)
            out = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", str(out))[:2000]
            return {"role": "tool_result",
                    "results": [{"text": out}]} if out.strip() else None

        if ptype == "reasoning":
            return None                          # thinking is not the transcript
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

    # -- bounded controller reads (transcriptTail / screen frames) ------------
    def tail_events(self, n=30, chars=400):
        """Last n slim transcript events with every text field truncated — the
        controller's 'what did this session actually say/do' read. Bounded by
        construction: n/chars clamped, whole reply capped at TAIL_REPLY_BYTES."""
        n = max(1, min(int(n or 30), TAIL_EVENTS_MAX))
        chars = max(40, min(int(chars or 400), TAIL_CHARS_MAX))
        path = self.transcript_path or self._find_transcript()
        if not path:
            return []
        try:
            lines = open(path).read().splitlines()
        except OSError:
            return []
        events = [e for e in (self._slim_event(l) for l in lines) if e]
        out = []
        for ev in events[-n:]:
            ev = dict(ev)
            if isinstance(ev.get("text"), str):
                ev["text"] = ev["text"][:chars]
            if ev.get("tools"):
                tools = []
                for tu in ev["tools"][:8]:
                    try:
                        arg = json.dumps(tu.get("input"), separators=(",", ":"))
                    except Exception:
                        arg = str(tu.get("input"))
                    tools.append({"name": tu.get("name"), "input": arg[:chars]})
                ev["tools"] = tools
            if ev.get("results"):
                ev["results"] = [str(r)[:chars] for r in ev["results"][:8]]
            out.append(ev)
        while len(out) > 1 and len(json.dumps(out)) > TAIL_REPLY_BYTES:
            out.pop(0)                      # drop oldest until the reply fits
        return out

    def screen_text(self, chars=1500):
        """De-ANSI'd tail of the live terminal — what a human would see right
        now. For TUI dialogs (trust prompts, menus, /login) that never reach
        the transcript; the same technique as the limit-banner scan."""
        chars = max(80, min(int(chars or 1500), SCREEN_CHARS_MAX))
        with self.ring_lock:
            raw = bytes(self.ring)
        text = _PTY_ANSI_RE.sub(b"", raw).decode("utf-8", "ignore")
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text[-chars:]

    def _backfill_last_answer(self):
        """Recover last_answer from the transcript after a resume, so the
        controller's cheapest retrieval channel survives restarts. The
        transcript may not exist yet (claude writes it lazily) — retry briefly,
        then give up quietly; the next Stop hook fills it anyway."""
        deadline = time.time() + 60
        while self.alive and not self.last_answer and time.time() < deadline:
            evs = self.tail_events(10, 500)
            if evs:
                for ev in reversed(evs):
                    if ev.get("role") == "assistant" and ev.get("text"):
                        self.last_answer = ev["text"][:500]
                        self.manager.broadcast_sessions()
                        break
                return
            time.sleep(5)

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

    def adopt_viewers(self, old):
        """In-place respawn under the SAME cid (account handoff, onboarding
        heal): move `old`'s viewers onto this session — and the PTY size
        ownership that goes with them. The 2026-08-20 phone screenshot: a
        handoff respawned the session at the 120×34 defaults with
        tty_owner=None, the carried-over phone got a hello at the wrong dims,
        armed staleGeomReplay and waited for a ttySize frame that never came
        (it only sends maintenance resizes, and nothing re-applied its
        claim) — so every line's tail wrapped. Size is applied BEFORE the
        re-subscribe so the hello already carries the right dims and the
        replay is clean; _apply_size is a no-op when clone_for_respawn
        already started us at the owner's geometry (no SIGWINCH, no ring
        drop). A viewer that switched away mid-respawn is not carried."""
        with old.clients_lock:
            viewers = list(old.clients)
            old.clients.clear()
        kept = [c for c in viewers if c.cid == self.cid and not c.dead]
        owner = old.tty_owner if (old.tty_owner in kept and old.tty_owner.tty_size) else None
        if owner is None:                        # owner gone → most recently sized survivor
            owner = max((c for c in kept if c.tty_size), key=lambda c: c.tty_ts, default=None)
        if owner is not None:
            self._set_owner(owner)
        for c in kept:
            self.subscribe(c)
        return kept

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
        self.acct_last_exit = {}                 # account -> ts of last claude exit
        self._stranded_warned = False            # one-shot: fable gate emptied the roster
        self._stale_route_noted = ""             # last pool routed to on a stale reading (log once)
        self._route_locks = {}                   # cid -> Lock: serializes prompt preflight,
                                                 # so two sends can't race one handoff
        self.last_switch_at = 0.0                # debounce anchor for auto-switch
        self._poll_now = threading.Event()       # kick the usage poller early
        self.lock = threading.RLock()
        self.all_clients = set()                 # every connected browser
        self.clients_lock = threading.Lock()
        self._projects_sig = None                # last broadcast projects payload (see broadcast_projects)
        # Graceful self-restart: when a boot-time file (server.py / .env) changes,
        # we flag a pending restart, surface it in every browser, and wait until
        # nothing is MID-TURN before tearing down — so no in-flight turn dies.
        # Not "all idle": a session parked on an interactive prompt is waiting
        # on a human, not computing (see restart_blockers), and treating it as
        # busy is how a pending restart becomes a permanent one.
        self.restart_pending = False
        self.restart_reason = ""
        self.restart_since = 0.0                 # when the wait started (for the ceiling)
        self._restarting = False
        self._restart_lock = threading.Lock()

    # -- graceful self-restart -------------------------------------------------
    def busy_count(self):
        with self.lock:
            return sum(1 for s in self.sessions.values() if s.busy and s.alive)

    def restart_blockers(self):
        """Sessions that would actually LOSE something to a restart — the bar
        for holding one back, which is narrower than `busy`.

        What a restart costs a session is specific: SIGTERM cuts an in-flight
        turn, so a partial reply is dropped and a tool call in progress is
        cancelled (possibly having already half-applied — the file is written,
        the model never saw the result). That is worth waiting for.

        `waiting` is NOT that. A session blocked on an interactive prompt has
        already stopped computing and is parked on a HUMAN; it can sit there
        for hours. Counting it as mid-turn is what let one stalled permission
        prompt on clawd-head hold a routing fix unapplied for 30+ minutes on
        2026-08-09, while that harness kept spawning onto a plan the fix
        exists to avoid. A restart that never fires is not the safe option.

        Background shells/agents are counted: they are killed outright and,
        unlike a turn, nothing resumes them."""
        with self.lock:
            return [s for s in self.sessions.values()
                    if s.alive and ((s.busy and not s.waiting) or s.bg)]

    def request_restart(self, reason, force=False):
        """Flag that a restart is needed; it fires once nothing is mid-turn
        (see restart_blockers). Idempotent — repeated calls just keep the
        pending state. `force` fires immediately, cutting whatever is in
        flight: the button behind it says what it will interrupt, and a
        restart you can't take is its own outage."""
        with self._restart_lock:
            if self._restarting:
                return
            first = not self.restart_pending
            self.restart_pending = True
            self.restart_reason = reason
            if first:
                self.restart_since = time.time()
        if first:
            print(f"[restart] pending — {reason} (waiting for mid-turn sessions)",
                  flush=True)
        self.broadcast_restart()
        self._maybe_restart(force=force)

    def cancel_restart(self):
        with self._restart_lock:
            if self._restarting or not self.restart_pending:
                return
            self.restart_pending = False
            self.restart_reason = ""
            self.restart_since = 0.0
        print("[restart] cancelled by user", flush=True)
        self.broadcast_restart()

    def _maybe_restart(self, force=False):
        """Fire the restart iff one is pending and nothing is mid-turn — or the
        wait has run out of patience.

        The ceiling is the lesson of the 08-09 incident: on a machine where
        somebody is always working, "wait for quiet" can mean "never", and the
        code sitting unapplied is exactly the code someone shipped because the
        running behavior was wrong. After RESTART_MAX_WAIT we take the hit."""
        blockers = self.restart_blockers()
        with self._restart_lock:
            if self._restarting or not self.restart_pending:
                return
            waited = time.time() - (self.restart_since or time.time())
            timed_out = RESTART_MAX_WAIT > 0 and waited >= RESTART_MAX_WAIT
            if blockers and not (force or timed_out):
                return
            why = ("forced" if force else
                   f"waited {waited / 60:.0f}m" if timed_out else "all idle")
            self._restarting = True
        if blockers:
            names = ", ".join(f"{s.cid[:8]}{' (bg)' if s.bg else ''}"
                              for s in blockers[:6])
            print(f"[restart] {why} — cutting {len(blockers)} mid-turn "
                  f"session(s): {names}", flush=True)
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
        # `busy` is what HOLDS the restart (mid-turn or background work), not
        # the raw busy count — the banner's number and its "restart now" button
        # must agree about what is being waited on, and about what a force cuts.
        blockers = self.restart_blockers() if self.restart_pending else []
        return {"type": "restart", "pending": self.restart_pending,
                "reason": self.restart_reason, "busy": len(blockers),
                "waitedFor": (time.time() - self.restart_since)
                             if self.restart_pending and self.restart_since else 0,
                "maxWait": RESTART_MAX_WAIT,
                "blockers": [{"cid": s.cid, "title": s.title or "",
                              "bg": s.bg or ""} for s in blockers[:8]]}

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
                        emoji=e.get("emoji", ""), emoji_at=e.get("emoji_at", 0.0),
                        upstream=e.get("upstream", ""),
                        default_branch=e.get("default_branch", ""))
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
                        created=e.get("created", 0.0), usage=e.get("usage"),
                        fable_seen=e.get("fable_seen", 0.0))
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
            engine = e.get("engine", "claude")    # legacy rows predate engines
            if engine != "claude":
                # Non-claude engines don't participate in the subscription
                # router, so the whole account/credential resume gate below is
                # not just unnecessary but WRONG for them (it would reroute a
                # codex session onto a claude config dir). Resume plainly: the
                # transcript either exists under the engine's own store or it
                # starts fresh.
                sid = e.get("session_id") or ""
                probe = ClaudeSession(self, cid=e.get("cid") or str(uuid.uuid4()),
                                      pid=pid, session_id=sid, resuming=False,
                                      engine=engine)
                resuming = bool(sid and probe._find_transcript())
                s = ClaudeSession(
                    self, cid=probe.cid, pid=pid,
                    session_id=sid if resuming else "", resuming=resuming,
                    engine=engine,
                    title=e.get("title", ""), desc=e.get("desc", ""),
                    tab=e.get("tab", ""),
                    prompt_count=e.get("prompt_count", 0),
                    first_prompt=e.get("first_prompt", ""),
                    created=e.get("created", 0.0),
                    last_active=e.get("last_active", 0.0),
                    prompted_at=e.get("prompted_at", 0.0),
                    pinned=e.get("pinned", 0.0),
                    test_hint=e.get("test_hint", ""),
                    model=e.get("model", ""), ctx_tokens=e.get("ctx_tokens", 0))
                self.sessions[s.cid] = s
                s.start()
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
                # Capability first, then headroom — but this list is a
                # SURVIVAL path (the recorded account is signed out and the
                # session must resume somewhere), so it only ever reorders.
                alts = sorted([a for a in self.accounts.values()
                               if a.ready and not a.broken],
                              key=lambda a: (not a.routable(),
                                             (a.usage or {}).get("pct", 100.0)))
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
                test_hint=e.get("test_hint", ""),
                model=e.get("model", ""), ctx_tokens=e.get("ctx_tokens", 0))
            self.sessions[s.cid] = s
            s.start()
        # Backfill the 📌 board's blue line for pins that predate the field (or
        # whose generation lost a race with a restart). One-shot at boot: a
        # session already parked and already verified-looking still needs to
        # tell you what to go test. A "" answer isn't retried until its next
        # Stop or re-pin, so this can't loop.
        for s in self.sessions.values():
            if s.pinned and not s.test_hint:
                threading.Thread(target=s._regenerate_test_hint,
                                 daemon=True).start()
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
                                 for a in self._ordered_accounts()],
                    # The second engine's plan, informational only: codex is
                    # single-login and outside the router, so this is a
                    # read-out, never an input to routing. None when codex
                    # isn't signed in — the UI then shows no codex card.
                    "codex": codex_usage_meta()}

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
            # Re-sign-in (this dir has been signed in before — stale creds,
            # or creds the provider already revoked): the CLI opens its
            # NORMAL TUI, not the login screen — the user shouldn't have to
            # remember to type /login, so type it for them once the TUI is
            # up. A never-signed-in dir is left alone: there the CLI boots
            # straight into its own login/onboarding flow and injected
            # keystrokes would garble it. See _opens_normal_tui for why the
            # test is onboarding state and NOT "are the creds present".
            if _opens_normal_tui(a.config_dir):
                s.desc = ("/login is being typed for you — complete the "
                          "Claude OAuth in this terminal")
                def _autologin(sess=s):
                    # SessionStart is the clean "TUI is up" signal, but it is
                    # a hook — and a hook that never fires must not silently
                    # cost the user the whole feature. Fall through to a
                    # fixed delay instead; by then the TUI has long painted.
                    if not sess._started_evt.wait(30):
                        print(f"[account {slug}] no SessionStart in 30s — "
                              "typing /login on the timer anyway", flush=True)
                    time.sleep(2.0)
                    if sess.alive and self.sessions.get(sess.cid) is sess:
                        print(f"[account {slug}] previously-signed-in dir "
                              "(normal TUI) — typing /login", flush=True)
                        # Trailing space is load-bearing, same as compact_cmd:
                        # a bare "/login" leaves the slash-command picker open
                        # and it eats the CR, so the command sits unrun in the
                        # composer — the exact silent no-op this feature exists
                        # to prevent. The space completes the token and closes
                        # the menu.
                        sess.send_message("/login ", control=True)
                threading.Thread(target=_autologin, daemon=True).start()
            self.broadcast_sessions()
        print(f"[account {slug}] created — sign-in session "
              f"{s.cid[:8] if s else 'FAILED'}", flush=True)
        return s

    def _route_key(self, a):
        """Sort key for 'which pool should we spend right now' (lower wins):
        CAPABLE pools before ones the model gate rejects (a plan that can't do
        fable is worth less than any amount of headroom — see
        SUB_REQUIRE_FABLE); then COOL pools (< SUB_HOT on the most-constrained
        window — see the never-see-a-rate-limit comment block) before hot ones;
        among the cool, the soonest WEEKLY reset first (use-it-or-lose-it — see
        the SUB_* comment block); pct is the fallback when no reset is known,
        and the tie-break.

        The capability term SORTS, it doesn't filter — callers that must never
        strand themselves can rank an unfiltered roster and still get the best
        available pool. Callers reading positional terms (_maybe_autoswitch)
        index via the KEY_* names below; keep those in step with the tuple."""
        pct = (a.usage or {}).get("pct")
        pct = 100.0 if pct is None else pct
        reset = _weekly_reset(a.usage)
        return (not a.routable(), pct >= SUB_HOT, reset is None,
                reset or 0.0, pct)

    # Positional names for _route_key's tuple — see the docstring above.
    KEY_CAP, KEY_HOT, KEY_NORESET, KEY_RESET, KEY_PCT = range(5)

    def _routable_first(self, accounts):
        """`accounts` narrowed to pools the capability gate allows — falling
        back to the WHOLE list when that would leave nothing. Routing to a
        fable-less plan is bad; routing nowhere is worse, so the gate is a
        preference of last resort, never a way to strand the fleet."""
        if not SUB_REQUIRE_FABLE:
            return list(accounts)
        ok = [a for a in accounts if a.routable()]
        if ok:
            self._stranded_warned = False        # re-arm: say it again if it recurs
            return ok
        if accounts and not self._stranded_warned:
            self._stranded_warned = True
            print("[accounts] every ready pool fails the fable gate — routing "
                  "on capacity alone (set SUB_REQUIRE_FABLE=0 to silence, or "
                  "SUB_FABLE_OK=<name> to trust one)", flush=True)
        return list(accounts)

    def _candidates(self):
        """(fresh, stale_cool): the ready, non-broken accounts that hold a
        usage reading, split by age. `fresh` (< 3×USAGE_TTL) is what every
        router path ranks first. `stale_cool` is the fallback tier — readings
        older than that but younger than USAGE_STALE_TRUST and still under
        SUB_HOT. A fresh reading that says 100% is *certain* failure; a stale
        one that said 83% is a good bet and, if wrong, costs one bounce that
        the tripwires already handle. Filtering the stale tier out entirely
        is how heart spawned onto a walled plan on 2026-08-22."""
        now = time.time()
        with self.lock:
            have = [a for a in self.accounts.values()
                    if a.ready and not a.broken
                    and (a.usage or {}).get("pct") is not None]
        fresh, stale = [], []
        for a in have:
            age = now - (a.usage.get("checkedAt") or 0)
            if age < 3 * USAGE_TTL:
                fresh.append(a)
            elif age < USAGE_STALE_TRUST and a.usage["pct"] < SUB_HOT:
                stale.append(a)
        return fresh, stale

    def _pick_pool(self, fresh, stale, key):
        """min(fresh, key) — unless that pick is hot or incapable and the
        stale-cool tier holds something better. Logs the stale route once
        per target (re-armed when a fresh pick resumes), because a stale
        route is a poller problem announcing itself, not a quiet success."""
        def ok(a):
            return (a is not None and a.routable()
                    and (a.usage or {}).get("pct", 100.0) < SUB_HOT)
        best = min(self._routable_first(fresh), key=key) if fresh else None
        if ok(best) or not stale:
            if best is not None:
                self._stale_route_noted = ""
            return best
        alt = min(self._routable_first(stale), key=key)
        if not ok(alt) and best is not None:
            return best
        if getattr(self, "_stale_route_noted", "") != alt.name:
            self._stale_route_noted = alt.name
            age = (time.time() - (alt.usage.get("checkedAt") or 0)) / 60
            bp = (best.usage or {}).get("pct") if best else None
            print(f"[accounts] every fresh pool is hot"
                  f"{f' (best fresh: {best.name} {bp:.0f}%)' if best else ''}"
                  f" — routing to {alt.name} on a {age:.0f}-min-old reading "
                  f"({alt.usage['pct']:.0f}%); a session there renews its "
                  "token and the reading heals", flush=True)
        return alt

    def _best_account(self):
        """The ready account the router would spend RIGHT NOW: the
        non-exhausted pool whose weekly window resets soonest — NOT the most
        headroom (that's only the tie-break; see _route_key), and only from
        pools the model gate allows (SUB_REQUIRE_FABLE). Fresh readings
        (< 3×USAGE_TTL) rank first; a stale-but-cool reading is the fallback
        when every fresh pool is hot (_candidates / _pick_pool). None when no
        account holds a reading at all — callers fall back to active_account.
        This is what routes each NEW session when auto-routing is on:
        per-spawn choice, not a sticky default.

        Every handoff path (both rescues, the sweep, the rebalance) picks its
        target through here, so gating this one function is what keeps a
        fable-less pool from being the answer to 'where should this go'."""
        fresh, stale = self._candidates()
        best = self._pick_pool(fresh, stale, self._route_key)
        return best.name if best else None

    # -- prompt-time preflight (ON-DEMAND-SUB-ROUTING-PLAN.md, stages 1–2) -----
    # The plan's shape: a prompt is the only proof a session needs a model, so
    # the routing decision belongs to the moment of delivery — not to a poller
    # re-parking idle sessions (each move re-ingests context for nobody). The
    # decision LOG is always on; SUB_ROUTE_ON_PROMPT makes the moves real.

    def _route_lock(self, cid):
        """The per-session routing lock. Serializes prompt preflight per cid so
        two simultaneous sends can't mint two replacement processes or deliver
        either prompt twice. Rescue paths joining this lock is stage 4+ — until
        then their cooldowns (last_bounce_rescue / last_handoff) keep the old
        mutual exclusion."""
        with self.lock:
            return self._route_locks.setdefault(cid, threading.Lock())

    def _pool_key(self, a):
        """Headroom-first sort key for PROMPT-TIME routing (lower wins):
        capable, then cool (< SUB_HOT), then MOST remaining headroom, with the
        weekly-reset clock only breaking ties. Deliberately the inverse
        emphasis of _route_key (reset-soonest first), which keeps governing
        spawn-time choice and the eager sweep while they exist — the plan
        records the reversal, don't 'fix' either to match the other."""
        pct = (a.usage or {}).get("pct")
        pct = 100.0 if pct is None else pct
        reset = _weekly_reset(a.usage)
        return (not a.routable(), pct >= SUB_HOT, pct, reset is None, reset or 0.0)

    def _prompt_pool(self):
        """Best usable ORGANIZATION pool for a prompt right now, or None when
        no reading exists at all (routing on a blind guess is the one thing
        the plan forbids — a stale-but-cool reading is not blind, see
        _candidates). Accounts sharing an organizationUuid share one limit,
        so they collapse to a single representative before ranking — a
        second config dir is a login alias, never extra capacity."""
        def collapse(accts):
            pools = {}
            for a in accts:
                k = a.org or ("\x00solo:" + a.name)  # orgless: each dir is its own pool
                cur = pools.get(k)
                if cur is None or a.name < cur.name:  # deterministic representative
                    pools[k] = a
            return list(pools.values())
        fresh, stale = self._candidates()
        # an org with a fresh reading never falls back to a sibling's stale one
        fresh_orgs = {a.org for a in fresh if a.org}
        stale = [a for a in stale if a.org not in fresh_orgs]
        return self._pick_pool(collapse(fresh), collapse(stale), self._pool_key)

    def _route_decision(self, s):
        """(decision, target Account|None, reason) for delivering session
        `s`'s next prompt — a pure read, moves nothing. The plan's selection
        order: dead/incapable current moves to any better usable pool, hot
        moves to a cool one, healthy-to-healthy only past SUB_HYSTERESIS,
        stale usage stays put, same-org never moves."""
        now = time.time()
        cur = self.accounts.get(s.account)
        best = self._prompt_pool()
        if best is None:
            return ("stay", None, "no fresh usable pool to compare — not moving blind")
        cur_u = (cur.usage or {}) if cur else {}
        cur_fresh = (cur_u.get("pct") is not None
                     and now - (cur_u.get("checkedAt") or 0) < 3 * USAGE_TTL)
        cur_pct = 100.0 if cur_u.get("pct") is None else cur_u["pct"]
        cur_dead = (cur is None or cur.broken
                    or (cur_fresh and cur_pct >= SUB_EXHAUSTED))
        if cur and cur.org and best.org and cur.org == best.org:
            return ("stay", None, "best pool shares this org — one limit, a move buys nothing")
        if best.name == s.account:
            return ("stay", None, "already on the best pool")
        best_pct = (best.usage or {}).get("pct", 100.0)
        if cur_dead:
            if best_pct < SUB_EXHAUSTED:
                return ("move", best, "current plan drained or refused")
            return ("stay", None, "current plan drained but nowhere usable to go")
        if cur and not cur.routable() and best.routable():
            return ("move", best, "current plan can't run the fleet's model")
        if not cur_fresh:
            return ("stay", None, "current usage stale — keeping the usable pool we have")
        if cur_pct >= SUB_HOT:
            if best_pct < SUB_HOT:
                return ("move", best, f"pool {cur_pct:.0f}% hot and a cool pool exists")
            return ("stay", None, "every pool is hot — a lateral hop buys nothing")
        if cur_pct - best_pct >= SUB_HYSTERESIS:
            return ("move", best, f"target has {cur_pct - best_pct:.0f} points more headroom")
        return ("stay", None, "healthy pool; headroom gap under hysteresis")

    def _log_route(self, s, decision, best, reason, via="", prompt_id="",
                   applied=False):
        """Stage 1: one structured line per routing decision — greppable as
        `[route]`, machine-readable after it. `applied` False = the flag is
        off (or the move was vetoed) and this is what preflight WOULD do; the
        rollout reads these before anyone flips SUB_ROUTE_ON_PROMPT."""
        cur = self.accounts.get(s.account)
        print("[route] " + json.dumps({
            "event": "prompt_route", "cid": s.cid, "prompt_id": prompt_id,
            "via": via,
            "source": s.account, "source_org": (cur.org if cur else "") or "",
            "source_pct": (cur.usage or {}).get("pct") if cur else None,
            "target": best.name if best else None,
            "target_org": (best.org or "") if best else None,
            "target_pct": (best.usage or {}).get("pct") if best else None,
            "requested_model": "fable" if SUB_REQUIRE_FABLE else "any",
            "decision": decision, "reason": reason, "applied": bool(applied),
        }), flush=True)

    def _route_handoff(self, s, target, why):
        """The plan's explicit-result handoff primitive: ("stayed"|"moved"|
        "failed", session). Wraps _handoff — which re-checks busy/alive/
        ceremony and may decline — and answers from the registry, the one
        authority on which object owns the cid now."""
        self._handoff(s, target, why)
        fresh = self.sessions.get(s.cid)
        if fresh is s:
            return ("stayed", s)
        if fresh and fresh.alive:
            return ("moved", fresh)
        return ("failed", fresh or s)

    def send_prompt(self, cid, text, via="", control=False):
        """Manager-owned prompt delivery — routing and delivery as ONE
        operation under the per-session lock (the plan's preflight). Returns
        True iff the prompt was typed into a live session exactly once.

        The carve-outs fall through to plain delivery untouched: control
        sends are TUI slash commands, not prompts; ceremony sessions sit on
        broken accounts on purpose; a non-routing engine must never be moved
        between Anthropic plans; router off = nothing to decide."""
        s = self.sessions.get(cid)
        if not s:
            return False
        if control or s.ceremony or not s.eng.routes_accounts \
                or not SUB_AUTOSWITCH:
            s.send_message(text, control=control)
            return True
        with self._route_lock(cid):
            s = self.sessions.get(cid) or s      # re-resolve: a rescue may have swapped the object
            decision, best, reason = self._route_decision(s)
            if decision == "move" and (s.busy or not s.alive):
                # A mid-turn session queues the text in its composer exactly
                # as today; routing belongs to the NEXT idle prompt.
                decision, best, reason = "stay", None, "session mid-turn — deliver in place"
            if decision == "move" and (s.bg or s.eng.bg_probe(s)):
                # A respawn kills live background shells/agents. Deliver in
                # place; if the pool is truly dead the send watchdog + bounce
                # rescue still act on real evidence (the plan's emergency rule).
                decision, best, reason = "stay", None, "live background work — a respawn would kill it"
            prompt_id = uuid.uuid4().hex[:8]
            applied = bool(SUB_ROUTE_ON_PROMPT and decision == "move" and best)
            self._log_route(s, decision, best, reason, via=via,
                            prompt_id=prompt_id, applied=applied)
            if applied:
                state, fresh = self._route_handoff(
                    s, best, why=f"prompt-time: {reason}")
                if state == "moved":
                    if not fresh.wait_ready(SUB_ROUTE_WAIT):
                        print(f"[route {cid[:8]}] moved session not ready in "
                              f"{SUB_ROUTE_WAIT:.0f}s — delivering anyway",
                              flush=True)
                    time.sleep(SUB_ROUTE_SETTLE)  # let the TUI finish painting
                    s = self.sessions.get(cid) or fresh
                elif state == "failed":
                    print(f"[route {cid[:8]}] handoff failed — prompt not "
                          "delivered", flush=True)
                    return False
                # "stayed": _handoff declined (busy/dead re-check) — in place.
            if not s.alive:
                return False
            s.send_message(text)
            return True

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
                    last_exit = dict(self.acct_last_exit)
                # ...and claude processes we did NOT spawn hold grants too
                # (a hand-launched terminal claude, cont's keepalive ping, a
                # ceremony left open). Scan the whole host, and also honor a
                # grace window after any session on the account exits — its
                # final rotation may still be settling. sub3 died 2026-08-06
                # because this gate only knew about our own live sessions.
                proc_dirs = _live_claude_dirs()
                # ...and a login in cont's VM custody has a consumer no ps
                # scan can see: the guest's claude inside a tart VM. The
                # wrangler's bounce path (2026-08-07) ran guests on the
                # fleet login with no host-visible process at all.
                custody_dirs = _vm_custody_dirs()
                def _grant_free(a):
                    return (a.name not in live
                            and _norm_config_dir(a.config_dir) not in proc_dirs
                            and _norm_config_dir(a.config_dir)
                                not in custody_dirs
                            and now - last_exit.get(a.name, 0)
                                > SUB_REFRESH_EXIT_GRACE)
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
                                               allow_refresh=_grant_free(a)),
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
                            a.record_usage(pct, windows, now)
                            a.error = ""
                            for m in sibs:
                                # same pool, same numbers — a copy, not a poll.
                                # The fable sighting copies too: siblings are
                                # the SAME subscription under another folder
                                # label, so one seeing fable is all of them
                                # seeing it (and a sibling left unstamped
                                # would convict on the next degraded payload).
                                m.usage = dict(a.usage)
                                m.fable_seen = max(m.fable_seen, a.fable_seen)
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
            drained, dead, hot, incapable, pcts = set(), set(), set(), set(), {}
            for a in self.accounts.values():
                pct = (a.usage or {}).get("pct", 0)
                pcts[a.name] = pct
                if a.broken or pct >= SUB_EXHAUSTED:
                    drained.add(a.name)
                if a.broken or pct >= 100:
                    dead.add(a.name)             # an in-flight turn CANNOT finish here
                if a.broken or pct >= SUB_HOT:
                    hot.add(a.name)              # heating toward the wall — stop feeding it
                if not a.routable():
                    incapable.add(a.name)        # plan can't do fable — wrong pool at 0%
            sessions = list(self.sessions.values())
        best = self.accounts.get(self._best_account() or "")
        if not best or best.name in drained:
            return
        cap_moved = cap_left = 0                 # capability evacuation, this sweep
        moved = deferred = 0                     # shared per-sweep handoff budget

        def take_slot():
            """Consume one of this sweep's handoff slots. False = budget spent;
            the caller leaves the session where it is and the next sweep (~15s)
            picks it up."""
            nonlocal moved
            if SUB_HANDOFF_BATCH and moved >= SUB_HANDOFF_BATCH:
                return False
            moved += 1
            return True

        # Rescues before optional moves: a session on a drained plan cannot run
        # a turn at all, so it should win the budget over a rebalance that is
        # only an optimisation.
        sessions.sort(key=lambda s: 0 if s.account in drained else 1)
        for s in sessions:
            if s.ceremony:
                continue                         # deliberate sign-in — hands off
            if not s.eng.routes_accounts:
                continue                         # engine outside the router
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
                if not take_slot():
                    deferred += 1
                    continue
                self._handoff(s, best)
                continue
            if s.busy:
                continue
            if s.bg or s.eng.bg_probe(s):
                # Idle-looking but background shells/agents are still running —
                # a respawn would kill them. Preemptive moves (evacuation,
                # rebalance) aren't worth that; only the drained rescue above
                # may still take the session.
                continue
            # Capability evacuation: a session parked on a plan that can't do
            # fable is on the wrong pool no matter how much headroom it has —
            # sessions that were routed there before the plan changed under
            # them don't come back on their own, because nothing else in the
            # sweep looks at anything but percentages. Idle-only and behind the
            # same bg veto above: a wrong-plan session is worth a respawn, an
            # in-flight turn or a live background shell is not.
            if s.account in incapable and best.name not in incapable:
                if SUB_CAP_EVAC_BATCH and cap_moved >= SUB_CAP_EVAC_BATCH:
                    cap_left += 1
                    continue                     # next sweep takes the rest
                if not take_slot():
                    deferred += 1
                    continue
                cap_moved += 1
                self._handoff(s, best, f"{s.account} can't do fable on its "
                                       "current plan — moving to one that can")
                continue
            # Preemptive evacuation: an idle session on a heating pool moves to
            # a COOL best before the wall, not after (never-see-a-rate-limit).
            if s.account in hot and best.name not in hot:
                if not take_slot():
                    deferred += 1
                    continue
                self._handoff(s, best, f"pool {pcts.get(s.account, 0):.0f}% hot "
                                       "— evacuating before the limit wall")
                continue
            why = self._rebalance_win(s.account, best)
            if why:
                if not take_slot():
                    deferred += 1
                    continue
                self._handoff(s, best, why)
        if deferred:
            print(f"[accounts] handoff budget: moved {moved} to {best.name}, "
                  f"{deferred} deferred to a later sweep (batch cap "
                  f"{SUB_HANDOFF_BATCH}) — spreading the context re-ingests",
                  flush=True)
        if cap_left:
            print(f"[accounts] fable evacuation: moved {cap_moved} to "
                  f"{best.name}, {cap_left} still queued (batch cap "
                  f"{SUB_CAP_EVAC_BATCH}) — the rest follow next sweep",
                  flush=True)

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
        if not SUB_AUTOSWITCH or s.ceremony or not s.eng.routes_accounts:
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
                    acct.record_usage(pct, windows, now2)
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
            self._stay_put_log(s, best, "prompt bounced off a dead plan")
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

    def _stay_put_log(self, s, best, what):
        """A rescue that found nowhere to go must say so — on 2026-08-22 the
        limit tripwire fired twice on heart and went silent both times while
        a cool pool sat one stale reading away. One line, with what the
        router saw, so the next reader starts at checkedAt ages (the
        EXPECTATIONS.md debugging rule) instead of at the tripwire."""
        fresh, stale = self._candidates()
        def fmt(a):
            age = (time.time() - (a.usage.get("checkedAt") or 0)) / 60
            return f"{a.name} {a.usage['pct']:.0f}%/{age:.0f}m"
        print(f"[session {s.cid[:8]}] {what} on {s.account} — nowhere better "
              f"to go (router's best: "
              f"{fmt(best) if best else 'none'}; fresh: "
              f"{', '.join(map(fmt, fresh)) or '-'}; stale-cool: "
              f"{', '.join(map(fmt, stale)) or '-'})", flush=True)

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
        if not SUB_AUTOSWITCH or not s.alive or s.ceremony \
                or not s.eng.routes_accounts:
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
                    acct.record_usage(pct, windows, now2)
                    acct.broken = False
        if not walled:
            return                               # echoed/stale banner on a cool pool
        best = self.accounts.get(self._best_account() or "")
        if (not best or best.name == s.account
                or (acct and acct.org and best.org and acct.org == best.org)
                or (best.usage or {}).get("pct", 100.0) >= SUB_EXHAUSTED):
            self._stay_put_log(s, best, "limit banner confirmed")
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
        fresh.adopt_viewers(s)                   # carry the viewers + PTY size owner across
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
        if not s.eng.routes_accounts:
            return                               # engine outside the router
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
                    acct.record_usage(pct, windows, now2)
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
        if not drained and (s.bg or s.eng.bg_probe(s)):
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
        fresh.adopt_viewers(s)                   # carry the viewers + PTY size owner across
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
        best = min(self._routable_first(ready), key=self._route_key)
        if best.name == cur.name:
            return
        H, N, R = self.KEY_HOT, self.KEY_NORESET, self.KEY_RESET
        cur_k, best_k = self._route_key(cur), self._route_key(best)
        gain = cur_pct - best.usage["pct"]
        # A capability win bypasses the debounce exactly like an exhausted
        # pool does: leaving the active account parked on a plan that can't run
        # the fleet's model is not a marginal headroom call, and there is no
        # flap risk — the target passes the gate and the source doesn't, so the
        # ordering can't invert on the next poll.
        by_cap = cur_k[self.KEY_CAP] and not best_k[self.KEY_CAP]
        # The hot bypass only fires when the TARGET is actually cool —
        # two accounts both over the threshold would otherwise ping-pong every
        # poll (each switch making the other one "best"), debounce ignored.
        # All-hot falls back to the debounced rules below.
        exhausted = cur_k[H] and not best_k[H]
        # Did best win on the weekly-reset clock (sooner reset, or a known
        # reset vs an unknown one)? That ordering only changes when a window
        # actually resets, so debounce alone is enough to prevent flap.
        by_reset = best_k[H:R + 1] < cur_k[H:R + 1] and best_k[N:R + 1] != cur_k[N:R + 1]
        if by_cap or exhausted or ((by_reset or gain >= SUB_HYSTERESIS)
                                   and time.time() - self.last_switch_at >= SUB_DEBOUNCE):
            if by_cap:
                why = (f"{cur.name} can't do fable on its current plan — "
                       "routing new sessions to a pool that can")
            elif exhausted:
                why = ("active exhausted" if cur_pct >= SUB_EXHAUSTED
                       else f"active pool {cur_pct:.0f}% hot — routing around the wall")
            elif by_reset and not (cur_k[N] or best_k[N]):
                why = (f"weekly resets {max(1, int((cur_k[R] - best_k[R]) // 3600))}h "
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
        repo_url = _normalize_repo_url(repo_url)
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

    def add_external_project(self, raw):
        """Adopt SOMEONE ELSE'S GitHub repo as a kind="external" project. The
        fork-or-clone decision is `gh repo view`'s viewerPermission: no push
        access → `gh repo fork --clone` (origin = our fork, upstream = the
        source); push access → a plain clone with an `upstream` remote added
        so every external project has the same remote shape. Either way the
        project records the upstream URL + default branch, every session in
        it is born with Project.standing_rule, and its default branch is
        synced from upstream at each spawn. The gh call and the clone both
        run in the provisioning thread (status cloning → ready|error), so the
        click returns immediately. Returns (project, "") or (None, error).

        Re-adding a URL whose folder is already a project: an external one is
        simply returned; a plain `gh` clone at that path is CONVERTED in
        place (kind flipped, upstream remote ensured) — the user asked for the
        PR discipline on this repo, and the folder is the same repo."""
        url = _normalize_repo_url(raw)
        if not url:
            return None, "empty URL"
        if not re.search(r"github\.com[/:]", url):
            return None, ("external needs a GitHub URL (or owner/repo) — the "
                          "fork/PR flow is `gh`-driven")
        base = _safe_name(re.sub(r"\.git$", "", url.rstrip("/").split("/")[-1]))
        path = str(PROJECTS_DIR / base)
        want = Project._slug(url).lower()
        collide = False                          # name taken by a DIFFERENT repo
        with self.lock:
            for p in list(self.projects.values()):
                if p.path != path:
                    continue
                if p.kind == "external" and p.status != "error":
                    # same repo (by upstream OR by our fork's URL) → reuse it;
                    # a different owner's repo that merely shares the name
                    # gets its own folder below (`name-2`)
                    if want in (p.upstream_slug().lower(), p.fork_slug().lower()):
                        return p, ""
                    collide = True
                    break
                if p.status == "error" and not os.path.isdir(path):
                    self.projects.pop(p.pid, None)   # failed corpse → retry fresh
                    break
                if p.kind == "gh" and p.status == "ready":
                    p.kind, p.upstream = "external", url
                    print(f"[project {p.name}] gh clone converted to external "
                          f"(upstream {url})", flush=True)
                    threading.Thread(target=self._provision_external,
                                     args=(p, url, False), daemon=True).start()
                    return p, ""
                return None, f"{base} is already a {p.kind} project here"
        present = (os.path.isdir(path) and bool(os.listdir(path))
                   and not collide)             # adopt a stray folder in place…
        safe = base if present else self._unique_project_name(base)   # …else name-2
        path = str(PROJECTS_DIR / safe)
        p = Project(pid=str(uuid.uuid4()), name=safe, path=path, repo_url=url,
                    status="cloning", created=time.time(), kind="external",
                    upstream=url)
        with self.lock:
            self.projects[p.pid] = p
        self.broadcast_projects()
        threading.Thread(target=self._provision_external,
                         args=(p, url, not present), daemon=True).start()
        return p, ""

    def _provision_external(self, project, url, clone):
        """Provisioning thread for add_external_project: gh view → fork or
        clone (when `clone`; an existing folder is adopted in place) → ensure
        the `upstream` remote → record upstream/default branch → ready. Any
        failure lands the card on error with the reason (the reconcile loop
        expires a folder-less error entry after ERROR_LINGER, like a failed
        clone)."""
        def fail(err):
            project.status, project.error = "error", (err or "failed")[-300:]
            project.error_at = time.time()
            print(f"[project {project.name}] external FAILED: {project.error}",
                  flush=True)
            self.save_registry()
            self.broadcast_projects()
        info, err = _gh_repo_info(url)
        if info is None:
            return fail(err)
        src = info["url"] or url
        project.upstream = src
        project.default_branch = info["default_branch"] or "main"
        if clone:
            if info["push"]:
                cmd = ["git", "clone", src, project.name]
                how = f"clone (push access: {info['perm']})"
            else:
                cmd = ["gh", "repo", "fork", src, "--clone", "--", project.name]
                how = f"fork (access: {info['perm'] or 'none'})"
            print(f"[project {project.name}] external → {how}", flush=True)
            try:
                r = subprocess.run(cmd, cwd=str(PROJECTS_DIR), capture_output=True,
                                   text=True, timeout=300)
            except Exception as e:
                return fail(str(e))
            if r.returncode != 0 or not os.path.isdir(os.path.join(project.path, ".git")):
                e = (r.stderr or r.stdout or "failed").strip()
                if "auth" in e.lower():
                    e += " (is `gh` authenticated in the server's environment?)"
                return fail(e)
        elif not os.path.isdir(os.path.join(project.path, ".git")):
            return fail("folder exists but is not a git repo")
        # remote shape: origin = where we push, upstream = where PRs go
        rc, have = _git(project.path, "remote", timeout=5)
        remotes = set(have.split()) if rc == 0 else set()
        if "upstream" not in remotes:
            _git(project.path, "remote", "add", "upstream", src, timeout=5)
        else:
            _git(project.path, "remote", "set-url", "upstream", src, timeout=5)
        project.repo_url = _git_remote_url(project.path) or src
        project.status, project.error = "ready", ""
        forked = Project._slug(project.repo_url) != Project._slug(src)
        print(f"[project {project.name}] external ok — origin {project.repo_url} "
              f"({'fork' if forked else 'direct'}), upstream {src}, "
              f"default {project.default_branch}", flush=True)
        self.save_registry()
        self.broadcast_projects()
        # first sync now, so the first session's spawn-time sync is a no-op
        _external_sync(project.path, project.default_branch, project.name)

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
    def create_session(self, pid, account=None, ceremony=False,
                       engine="claude"):
        if pid not in self.projects:
            return None
        proj = self.projects[pid]
        if proj.kind == "local" and proj.status == "error":
            return None                          # folder missing — Popen on a dead cwd would fail
        if proj.kind == "external" and proj.status == "ready" and EXTERNAL_SYNC \
                and not ceremony:
            # Never start stale: bring the default branch up to upstream
            # BEFORE the session exists. Synchronous by design (bounded by
            # EXTERNAL_SYNC_TIMEOUT) — see the knob's comment.
            _external_sync(proj.path, proj.default_branch, proj.name)
        engine = engine if engine in ENGINES else "claude"
        if not ENGINES[engine].routes_accounts:
            # Engines outside the subscription router (codex) spawn plainly:
            # one login per machine, no account, no config dir, no headroom
            # math. Everything below this block is Anthropic-specific.
            cid = str(uuid.uuid4())
            s = ClaudeSession(self, cid=cid, pid=pid, session_id="",
                              resuming=False, created=time.time(),
                              engine=engine)
            if not _codex_signed_in():
                s.desc = ("codex is not signed in on this machine — run "
                          "`codex login` in a terminal once, then start a "
                          "new session")
            with self.lock:
                self.sessions[cid] = s
            s.start()
            self.save_registry()
            self.broadcast_sessions()
            return s
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
                    key=lambda x: (not x.routable(),          # capability first…
                                   (x.usage or {}).get("pct", 100.0)))
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
        """📌 park a session on the pin board (or restore it). Still pure
        metadata as far as the board is concerned — the process is untouched
        and a pinned to-do can still be prompted from the card — but parking
        does now DO two things to the session: derive the human's test step
        (the blue line) and compact it, in that order (_on_pinned)."""
        s = self.get(cid)
        if not s:
            return
        s.pinned = time.time() if on else 0.0
        print(f"[session {cid[:8]}] {'pinned 📌' if on else 'unpinned'}",
              flush=True)
        if on:
            # Landing on the board = "done coding, needs a human to check it".
            # Derive that check now (async — the pin itself must stay instant);
            # the card renders it the moment the broadcast lands.
            threading.Thread(target=s._on_pinned, daemon=True).start()
        else:
            s.test_hint = ""      # off the board → stale instruction; re-pin re-asks
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

    # -- controller read queries (search / transcriptTail / screen) -----------
    def search(self, q, scope="all", limit=20):
        """Bounded server-side search over live sessions: meta fields (title/
        desc/tab/digest/blocked_on/test_hint — zero I/O) and/or each
        transcript's tail.
        Most-recently-active first, so the caps land on useful content. Powers
        the controller's one-call `find`. Closed sessions' transcripts are out
        of scope (no cid mapping). See docs/WS-PROTOCOL.md."""
        q = (q or "").strip()
        if not q:
            return {"matches": [], "scanned": 0, "truncated": False}
        limit = max(1, min(int(limit or 20), SEARCH_LIMIT_MAX))
        if scope not in ("meta", "transcript", "all"):
            scope = "all"
        ql = q.lower()
        matches, truncated, scanned = [], False, 0
        deadline = time.time() + SEARCH_BUDGET_S
        for s in self._ordered():
            if len(matches) >= limit or time.time() > deadline:
                truncated = True
                break
            scanned += 1
            base = {"cid": s.cid, "pid": s.pid,
                    "title": s.title or s._fallback_title(),
                    "lastActive": s.last_active}
            if scope in ("meta", "all"):
                for where, val in (("title", s.title), ("desc", s.desc),
                                   ("tab", s.tab), ("digest", s.digest),
                                   ("blocked_on", s.blocked_on),
                                   ("test_hint", s.test_hint),
                                   ("lastAnswer", s.last_answer)):
                    if val and ql in val.lower():
                        i = val.lower().find(ql)
                        lo = max(0, i - SEARCH_SNIPPET // 2)
                        matches.append({**base, "where": where,
                                        "snippet": val[lo:lo + SEARCH_SNIPPET]})
                        break                # one meta hit per session is plenty
            if scope in ("transcript", "all") and len(matches) < limit:
                matches += [{**base, **hit}
                            for hit in self._search_transcript(s, ql, deadline)]
        return {"matches": matches[:limit], "scanned": scanned,
                "truncated": truncated or len(matches) > limit}

    @staticmethod
    def _search_transcript(s, ql, deadline):
        """Newest-first substring scan over the tail of one session's transcript.
        Cheap pre-filter on the raw line before any JSON parse; every hit is a
        slim-event text, so no tool noise or ANSI ever reaches the caller."""
        path = s.transcript_path or s._find_transcript()
        if not path:
            return []
        try:
            size = os.path.getsize(path)
            with open(path, "r", errors="ignore") as f:
                if size > SEARCH_TAIL_BYTES:
                    f.seek(size - SEARCH_TAIL_BYTES)
                    f.readline()                  # drop the partial line
                lines = f.read().splitlines()
        except OSError:
            return []
        hits = []
        for line in reversed(lines):
            if len(hits) >= SEARCH_PER_SESSION or time.time() > deadline:
                break
            if ql not in line.lower():
                continue
            ev = s._slim_event(line)
            if not ev:
                continue
            texts = ([ev["text"]] if isinstance(ev.get("text"), str) else []) \
                + [str(r) for r in ev.get("results") or []]
            for t in texts:
                i = t.lower().find(ql)
                if i >= 0:
                    lo = max(0, i - SEARCH_SNIPPET // 2)
                    hits.append({"where": "transcript",
                                 "snippet": t[lo:lo + SEARCH_SNIPPET]})
                    break
        return hits

    def serve_read_query(self, client, frame):
        """Answer one controller read frame on a worker thread — the reply goes
        only to the requesting client (like pong/focus), with the request `id`
        echoed for correlation. Errors ride inside the result frame; bad input
        can't take the client's read loop down."""
        t = frame.get("type")
        reply = {"type": t + "Result", "id": frame.get("id")}
        try:
            if t == "search":
                reply["q"] = frame.get("q", "")
                reply.update(self.search(frame.get("q", ""),
                                         frame.get("scope", "all"),
                                         frame.get("limit", 20)))
            else:
                s = self.get(frame.get("cid"))
                if not s:
                    reply["error"] = f"no such session: {frame.get('cid')}"
                elif t == "transcriptTail":
                    reply["cid"] = s.cid
                    reply["events"] = s.tail_events(frame.get("n", 30),
                                                    frame.get("chars", 400))
                elif t == "screen":
                    reply["cid"] = s.cid
                    reply["text"] = s.screen_text(frame.get("chars", 1500))
                    reply["cols"], reply["rows"] = s.tty_cols, s.tty_rows
        except Exception as e:
            reply["error"] = f"{type(e).__name__}: {e}"
        client.send_json(reply)

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

    def broadcast_projects(self, force=False):
        """Fan the project list out — but only when it actually changed.

        Every hook bumps a session's `last_active` (on_hook), and broadcast_sessions
        chains into here, so an unfiltered version emits a `projects` frame per
        PreToolUse *and* PostToolUse — a couple per tool call of every running
        session. The browser answers each one by repainting the projects rung,
        which is what made the rung churn under the user's hands.

        The fingerprint deliberately EXCLUDES `lastTouched`: it's the warmth
        timestamp that every hook moves, and nothing renders it — it only feeds
        the sort, and the list is already sorted here, so a reorder still shows
        up as a change in pid order. `force=True` bypasses the memo.
        """
        meta = self.projects_meta()
        sig = json.dumps([{k: v for k, v in p.items() if k != "lastTouched"}
                          for p in meta], sort_keys=True, default=str)
        if not force and sig == self._projects_sig:
            return
        self._projects_sig = sig
        self.broadcast_all({"type": "projects", "projects": meta})

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
def _llm_json(sys_prompt, user_text, max_tokens=120, model=""):
    """POST one (system, user) turn to the configured gateway and return the
    parsed JSON object the model emitted, or None if naming is unconfigured, the
    call fails, or no JSON is found. Stdlib-only HTTP — the single transport both
    generate_name and generate_digest share (one place handles the
    openai/anthropic/bankr body+auth differences; no drift). `model` overrides
    BANKR_MODEL for callers whose job wants a different tier (see TEST_MODEL)."""
    if not (BANKR_API_KEY and BANKR_BASE_URL):
        return None
    model = model or BANKR_MODEL
    try:
        if BANKR_API == "anthropic":
            url = f"{BANKR_BASE_URL}/v1/messages"
            body = {"model": model, "max_tokens": max_tokens,
                    "system": sys_prompt,
                    "messages": [{"role": "user", "content": user_text}]}
            headers = {"x-api-key": BANKR_API_KEY,
                       "anthropic-version": "2023-06-01",
                       "content-type": "application/json"}
        else:  # openai-compatible (incl. bankr — same body, different auth header)
            url = f"{BANKR_BASE_URL}/chat/completions"
            body = {"model": model, "max_tokens": max_tokens, "temperature": 0.3,
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


def generate_test_hint(transcript_text):
    """Return the one-line "here's what YOU have to go test" for a 📌 pinned
    session, "" if the model judged there's nothing verifiable yet, or None if
    naming is unconfigured / the call failed (caller keeps the old hint)."""
    parsed = _llm_json(TEST_SYS_PROMPT, transcript_text, model=TEST_MODEL)
    if not parsed:
        return None
    hint = parsed.get("test")
    return hint.strip() if isinstance(hint, str) else ""


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
        if t in ("search", "transcriptTail", "screen"):
            # Controller read queries — threaded so a transcript scan can't
            # stall this client's read loop; replied only to the requester.
            threading.Thread(target=MGR.serve_read_query, args=(client, frame),
                             daemon=True).start()
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
                                   account=frame.get("account"),
                                   engine=frame.get("engine") or "claude")
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
        elif t == "addExternalProject":
            _, xerr = MGR.add_external_project(frame.get("repoUrl", ""))
            if xerr:
                client.send_json({"type": "error",
                                  "error": f"addExternalProject: {xerr}"})
        elif t == "removeProject":
            MGR.remove_project(frame.get("pid"))
        elif t == "restart":
            # force=True is the banner's "restart now" — a deliberate human act
            # that accepts cutting whatever the banner just told them is running.
            MGR.request_restart(frame.get("reason") or "manual",
                                force=bool(frame.get("force")))
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
                # A browser send always carries a `via` tag ('typed'/'quick');
                # controller/pipeline sends never do — that asymmetry is the
                # whole arming gate for AUTO_TLDR. Asking for a tldr yourself
                # (the chip, or anything tldr-shaped) doesn't arm — summarizing
                # a summary is the one loop this could build.
                s.auto_tldr_armed = (bool(frame.get("via"))
                                     and not txt.strip().lower().startswith("tldr"))
                # Delivery goes through the manager's prompt preflight (route
                # decision logged always; moves only under SUB_ROUTE_ON_PROMPT).
                # With the flag on, a move can hold delivery for seconds — a
                # thread keeps this client's WS reader loop responsive; off,
                # the inline call blocks exactly as long as send_message always
                # has (the settle sleep), so the profile is unchanged.
                if SUB_ROUTE_ON_PROMPT:
                    threading.Thread(target=MGR.send_prompt,
                                     args=(s.cid, txt),
                                     kwargs={"via": frame.get("via", "")},
                                     daemon=True).start()
                else:
                    MGR.send_prompt(s.cid, txt, via=frame.get("via", ""))
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
        # Re-check a pending restart on the tick, not only when session state
        # moves: the RESTART_MAX_WAIT ceiling is a clock, and a box quiet enough
        # to stop broadcasting `sessions` is exactly the one that would never
        # re-evaluate it. Cheap — restart_blockers is a list comp over sessions.
        if MGR.restart_pending:
            MGR._maybe_restart()


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
    _sync_shared_kit()
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
