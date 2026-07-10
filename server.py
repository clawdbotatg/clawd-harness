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
                   '{"title": "<max 5 words>", "desc": "<max 12 words>"}. '
                   "Name the session by its MAIN objective — the overarching task "
                   "it was set up to accomplish, usually established in the opening "
                   "messages. Treat later one-off questions or tangents (a passing "
                   "pricing/how-to/model question) as side-quests: do NOT let them "
                   "redefine the name unless the session's whole focus has clearly "
                   "and durably shifted to a new task. "
                   "The title is a terse label; the desc is a one-line summary.")
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
                "image/webp": ".webp"}
REGISTRY_FILE = HERE / ".clawd-harness.sessions.json"   # persists projects+sessions across restarts
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
# Local routing rule (direct mode; the fleet relay will own this fleet-wide):
# among pools with room (< EXHAUSTED), spend the one whose WEEKLY window resets
# soonest — weekly headroom is use-it-or-lose-it, so draining the earliest-
# resetting pool first forfeits the least capacity; once it resets its clock
# jumps +7d and it goes to the back of the queue. Headroom (pct) is only the
# fallback when a reset time is unknown, and the tie-break. Reset order is
# stable between polls, so a reset-driven switch needs only the DEBOUNCE; a
# pct-driven one also needs HYSTERESIS points — and an EXHAUSTED active
# account bypasses both (no loyalty to a dead account).
SUB_AUTOSWITCH = os.environ.get("SUB_AUTOSWITCH", "1") != "0"
SUB_HYSTERESIS = float(os.environ.get("SUB_HYSTERESIS", "20"))  # headroom pts
SUB_DEBOUNCE   = float(os.environ.get("SUB_DEBOUNCE", "7200"))  # seconds
SUB_EXHAUSTED  = float(os.environ.get("SUB_EXHAUSTED", "95"))   # % used
# Mid-session handoff: an IDLE session whose plan has run dry is respawned
# under the best plan with --resume (transcript symlinked across). Checked on
# every Stop; per-session cooldown so a flapping window can't churn respawns.
HANDOFF_COOLDOWN = float(os.environ.get("HANDOFF_COOLDOWN", "600"))
# A session on a HARD-dead plan (100% used / login refused) whose hooks have
# been silent this long is stuck on the limit screen (the eaten turn never
# emits Stop, so `busy` never clears) — the sweep reclaims and moves it.
BUSY_STUCK = float(os.environ.get("BUSY_STUCK", "600"))
# Rebalance = the spend-the-soonest-reset policy applied to sessions ALREADY
# RUNNING: an idle session sitting on a healthy pool still moves to the
# router's best pool when that pool's weekly window resets ≥ MARGIN sooner —
# otherwise a long-lived session pins yesterday's routing choice for days
# while the soonest-resetting pool forfeits capacity. Same handoff mechanics
# and per-session cooldown as the drain rescue; the margin keeps near-ties
# (incl. same-day resets) from churning respawns.
SUB_REBALANCE = os.environ.get("SUB_REBALANCE", "1") != "0"
SUB_REBALANCE_MARGIN = float(os.environ.get("SUB_REBALANCE_MARGIN", "21600"))  # s
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


def _read_oauth_creds(config_dir):
    """The credential JSON blob for an account dir: macOS Keychain first, then
    the Linux-style <dir>/.credentials.json. None if absent/unreadable."""
    try:
        r = subprocess.run(["security", "find-generic-password",
                            "-s", _keychain_service(config_dir),
                            "-a", os.environ.get("USER", ""), "-w"],
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            return json.loads(r.stdout.strip())
    except Exception:
        pass
    path = Path(config_dir or os.path.expanduser("~/.claude")) / ".credentials.json"
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


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


def _clog(config_dir, msg):
    """Timestamped credential-event log line. The 2026-07-09 post-mortem was
    nearly impossible because account/creds events had no timestamps — every
    credential-lifecycle event goes through here now."""
    print(f"[creds {config_dir or '~/.claude'} "
          f"{time.strftime('%m-%d %H:%M:%S')}] {msg}", flush=True)


def _refresh_grant(refresh):
    """POST a refresh grant via CURL and return (http_status, body_dict).

    Curl, not urllib, ON PURPOSE: the token endpoint (platform.claude.com)
    sits behind Cloudflare bot protection that 403s Python's TLS signature
    with 'error code: 1010' — every urllib refresh in this file's history
    FAILED AT THE EDGE without ever reaching Anthropic, and the harness
    misread that as revoked credentials (the 'idle logins keep dying'
    epidemic — see EXPECTATIONS.md 2026-07-09). Curl's signature passes.
    The token travels via stdin so it never appears in `ps` output."""
    try:
        r = subprocess.run(
            ["curl", "-sS", "-m", "15", "-w", "\n%{http_code}",
             "-X", "POST", OAUTH_TOKEN_URL,
             "-H", "Content-Type: application/json",
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
    truly needs a re-sign-in); None for everything else (network blip,
    Cloudflare block, endpoint change) — callers keep the last snapshot and
    the router stays put rather than flapping to a blind guess. NEVER map an
    infra failure to AUTH_FAIL: that exact misdiagnosis (Cloudflare 1010 read
    as revocation) caused every 'idle login died' incident before 2026-07-09.
    allow_refresh=False = poll with the stored access token only and return
    None when it's expired — for accounts whose grant a live claude process
    may also hold (two consumers of one rotating grant can kill the family).
    `tok_cache` (a mutable dict) keeps a refreshed access token in memory
    across polls (plus the 429 back-off horizon); refreshed tokens are
    written back to the credential store via _persist_refreshed."""
    oauth = (_read_oauth_creds(config_dir) or {}).get("claudeAiOauth") or {}
    access, refresh = oauth.get("accessToken"), oauth.get("refreshToken")
    cached = (tok_cache or {}).get("access")
    if not (access or cached):
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
        # A hard-limited plan 429s even its usage endpoint — which is exactly
        # when routing away matters most. Report it as fully used (not "no
        # data", which would freeze a stale-green snapshot and blind the
        # router) so autoswitch/handoff treat it like any exhausted window.
        # The next successful poll (endpoint recovers ≤ Retry-After) restores
        # real numbers.
        try:
            until = time.time() + max(60.0, float(retry_after))
        except (TypeError, ValueError):
            until = time.time() + 1800
        if tok_cache is not None:
            # honor Retry-After: re-poking a 429ing endpoint every TTL resets
            # its limiter and freezes the fake 'limited 0%' card forever
            tok_cache["no_poll_until"] = until
        win = [{"key": "rate_limited", "label": "limited", "used": 100.0,
                "resets": time.strftime("%Y-%m-%dT%H:%M:%S+00:00",
                                        time.gmtime(until))}]
        return (100.0, win, None) if want_ident else (100.0, win)
    if code != 200 or not isinstance(usage, dict):
        return None
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
                 error="", created=0.0, pinned=False):
        self.pid = pid
        self.name = name
        self.path = path                         # abs path to the repo
        self.repo_url = _scrub_url_creds(repo_url)  # never store/broadcast embedded creds
        self.status = status                     # ready | cloning | error
        self.error = error
        self.error_at = 0.0                      # when status flipped to error (in-memory only)
        self.created = created or time.time()
        self.pinned = pinned                     # the harness-itself project: top of list, not removable

    def to_registry(self):
        return {"pid": self.pid, "name": self.name, "path": self.path,
                "repo_url": self.repo_url, "status": self.status,
                "created": self.created}

    def meta(self, session_count=0, busy_count=0, waiting_count=0, last_touched=0.0):
        return {"pid": self.pid, "name": self.name, "path": self.path,
                "repoUrl": self.repo_url, "status": self.status,
                "error": self.error, "sessionCount": session_count,
                "busyCount": busy_count, "waitingCount": waiting_count,
                "created": self.created, "pinned": self.pinned,
                "lastTouched": last_touched}


# ── PTY-backed Claude session ─────────────────────────────────────────────────
class ClaudeSession:
    """One interactive `claude` process in a PTY, streamed to the websocket
    clients currently *subscribed* to it. Owned by a SessionManager."""

    def __init__(self, manager, cid, session_id, resuming, pid="",
                 title="", desc="", prompt_count=0, first_prompt="", created=0.0,
                 last_active=0.0, account="default", config_dir=""):
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

        self.title = title
        self.desc = desc
        self.prompt_count = prompt_count
        self.first_prompt = first_prompt
        self.last_active = last_active or self.created   # warmth: drives project sort

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
        self.last_tool = None
        self.digest = ""                          # volatile "what it's doing now" (LLM, refreshed each Stop)
        self.blocked_on = None                    # the open question if it ended asking the human (LLM)
        self.settings_path = None

    # -- registry shape --------------------------------------------------------
    def to_registry(self):
        return {"cid": self.cid, "pid": self.pid, "session_id": self.session_id,
                "title": self.title, "desc": self.desc,
                "prompt_count": self.prompt_count, "first_prompt": self.first_prompt,
                "created": self.created, "last_active": self.last_active,
                "account": self.account, "config_dir": self.config_dir}

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
        # blocked (needs a human now) > working (turn in flight) > idle.
        status = "blocked" if self.waiting else ("working" if self.busy else "idle")
        return {"cid": self.cid, "pid": self.pid,
                "title": self.title or self._fallback_title(),
                "desc": self.desc or "",
                "named": bool(self.title),
                "busy": self.busy, "waiting": self.waiting, "tool": self.last_tool,
                "status": status,
                "digest": self.digest or "",
                "blocked_on": self.blocked_on or "",
                "sessionId": self.session_id,
                "promptCount": self.prompt_count,
                "lastActive": self.last_active,
                "created": self.created,
                "alive": self.alive,
                "account": self.account}

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
        if self.config_dir:                      # non-default subscription account
            env["CLAUDE_CONFIG_DIR"] = self.config_dir
        else:
            # default = plain ~/.claude, always: an operator-exported
            # CLAUDE_CONFIG_DIR would strand transcripts where our globs
            # (config_dir or ~/.claude) never look.
            env.pop("CLAUDE_CONFIG_DIR", None)

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
            self._on_prompt(prompt)
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
        title, desc = generate_name(text)
        if title:
            self.title = title[:60]
            self.desc = (desc or "")[:120]
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
        self.write(text.encode("utf-8"))
        # Short one-liners only need to clear the 0.6s burst cliff; big or
        # multi-line pastes take longer to finalize, so keep the full settle.
        big = len(text) > 280 or text.count("\n") >= 1
        time.sleep(SEND_SETTLE if big else SEND_SETTLE_MIN)
        self.write(b"\r")

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
        self.alive = False
        print(f"[session {self.cid[:8]}] PTY closed / claude exited", flush=True)
        # An account handoff replaces this object under the same cid — the
        # dying child's exit must not paint "session ended" over its successor.
        if self.manager.sessions.get(self.cid) is self:
            self.manager.broadcast_all({"type": "exit", "cid": self.cid})
            self.manager.broadcast_sessions()

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
            content = (obj.get("message") or {}).get("content") or []
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
            if not e.get("path") or not os.path.isdir(e["path"]):
                continue                         # repo dir gone — drop the entry
            # Backfill the origin URL when the registry stored an empty one (legacy
            # entries adopted before this backfill existed). Every machine reporting
            # its canonical repo URL is what lets the fleet view merge the same repo
            # across boxes into one card instead of splitting name-only vs. URL keys.
            repo_url = e.get("repo_url", "") or _git_remote_url(e["path"])
            p = Project(pid=e.get("pid") or str(uuid.uuid4()),
                        name=e.get("name") or os.path.basename(e["path"]),
                        path=e["path"], repo_url=repo_url,
                        status=e.get("status", "ready") if e.get("status") != "cloning" else "ready",
                        created=e.get("created", 0.0))
            self.projects[p.pid] = p
        self._discover_projects()                # adopt repos dropped into projects/ by hand
        self._ensure_self_project()              # always offer the harness itself, pinned

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
            if not _has_creds(cfg) and not (acct_entry and not acct_entry.ready):
                alts = sorted([a for a in self.accounts.values()
                               if a.ready and not a.broken],
                              key=lambda a: (a.usage or {}).get("pct", 100.0))
                alt = next((a for a in alts if _has_creds(a.config_dir)), None)
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
                prompt_count=e.get("prompt_count", 0),
                first_prompt=e.get("first_prompt", ""),
                created=e.get("created", 0.0),
                last_active=e.get("last_active", 0.0),
                account=name, config_dir=cfg)
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
        if self._discover_projects():
            changed = True
        if changed:
            self.save_registry()
        return changed

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
            data = json.loads(REGISTRY_FILE.read_text())
        except (OSError, ValueError):
            return {}
        if isinstance(data, dict):
            return data
        return {}                                # legacy flat-list → ignored (fresh start)

    def save_registry(self):
        with self.lock:
            data = {"projects": [p.to_registry() for p in self._ordered_projects()
                                 if not p.pinned],   # self project is re-injected, not stored
                    "sessions": [s.to_registry() for s in self._ordered()],
                    "accounts": [a.to_registry() for a in self._ordered_accounts()],
                    "active_account": self.active_account,
                    "last_switch_at": self.last_switch_at}
        try:
            REGISTRY_FILE.write_text(json.dumps(data, indent=2))
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
            # remove). Already signed in — no ceremony, no session.
            with self.lock:
                if "default" in self.accounts:
                    return None
                em, org, oname = _account_identity("")
                self.accounts["default"] = Account(
                    "default", "", email=em, org=org, org_name=oname, ready=True)
            self.save_registry()
            self.broadcast_accounts()
            print("[account default] re-adopted the ~/.claude login", flush=True)
            return None
        with self.lock:
            a = self.accounts.get(slug)
            if a and a.ready and not a.broken:
                return None                      # already signed in
            if not a:
                a = Account(slug, str(ACCOUNTS_DIR / slug))
                self.accounts[slug] = a
        try:
            _link_shared_paths(a.config_dir)
            _share_projects(a.config_dir)
        except Exception as e:
            print(f"[account {slug}] share links failed: {e}", flush=True)
        self.save_registry()
        self.broadcast_accounts()
        s = self.create_session(SELF_PID, account=slug)
        if s:
            s.title = f"sign in · {slug}"
            s.desc = "complete the Claude OAuth login in this terminal"
            self.broadcast_sessions()
        print(f"[account {slug}] created — sign-in session "
              f"{s.cid[:8] if s else 'FAILED'}", flush=True)
        return s

    def _route_key(self, a):
        """Sort key for 'which pool should we spend right now' (lower wins):
        pools with room before exhausted ones; among those with room, the
        soonest WEEKLY reset first (use-it-or-lose-it — see the SUB_* comment
        block); pct is the fallback when no reset is known, and the
        tie-break."""
        pct = (a.usage or {}).get("pct")
        pct = 100.0 if pct is None else pct
        reset = _weekly_reset(a.usage)
        return (pct >= SUB_EXHAUSTED, reset is None, reset or 0.0, pct)

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
            due = [a for a in accts if a.ready and not a.broken and
                   (forced or (now - (a.usage or {}).get("checkedAt", 0) > USAGE_TTL
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
                with ThreadPoolExecutor(max_workers=min(4, len(due))) as ex:
                    got = list(ex.map(
                        lambda a: _fetch_usage(a.config_dir, a.tok,
                                               want_ident=True,
                                               allow_refresh=a.name not in live),
                        due))
                for a, res in zip(due, got):
                    if res == AUTH_FAIL:
                        # login gone/revoked → OUT of routing until re-sign-in;
                        # the pending watcher above picks it back up
                        sig = _cred_sig(a.config_dir)
                        with self.lock:
                            a.broken = True
                            a.refused_sig = sig
                        print(f"[account {a.name}] credentials refused — "
                              "excluded from routing until re-sign-in", flush=True)
                        changed = True
                    elif res:
                        pct, windows, ident = res
                        limited = bool(windows) \
                            and windows[0].get("key") == "rate_limited"
                        with self.lock:
                            a.usage = {"pct": round(pct, 1), "windows": windows,
                                       "checkedAt": now}
                            # the 'limited' card is a PLACEHOLDER (the usage
                            # endpoint 429'd) — say so, or a pool that just
                            # reset looks drained with no explanation
                            a.error = ("usage endpoint rate-limited — backing "
                                       "off per Retry-After; real numbers "
                                       "resume automatically") if limited else ""
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
            drained, dead = set(), set()
            for a in self.accounts.values():
                pct = (a.usage or {}).get("pct", 0)
                if a.broken or pct >= SUB_EXHAUSTED:
                    drained.add(a.name)
                if a.broken or pct >= 100:
                    dead.add(a.name)             # an in-flight turn CANNOT finish here
            sessions = list(self.sessions.values())
        best = self.accounts.get(self._best_account() or "")
        if not best or best.name in drained:
            return
        for s in sessions:
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
            why = None if s.busy else self._rebalance_win(s.account, best)
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

    def maybe_handoff(self, s):
        """Mid-session account handoff (SUB-ROUTING.md Phase 5): called after
        every Stop. If THIS session's plan is drained (>= SUB_EXHAUSTED used,
        or its login broke) and a better plan is ready, respawn the session
        under that plan with --resume — transcript symlinked across, so the
        conversation continues seamlessly and the user is never asked to do
        anything. The usage check hits the endpoint directly (the 10-min poll
        is too slow to catch a window dying mid-conversation)."""
        if not SUB_AUTOSWITCH or s.busy or not s.alive:
            return
        if time.time() - s.last_handoff < HANDOFF_COOLDOWN:
            return
        acct = self.accounts.get(s.account)      # may be None (e.g. removed default)
        cfg = acct.config_dir if acct else (s.config_dir or "")
        # allow_refresh=False: this session's own claude holds this grant
        got = _fetch_usage(cfg, acct.tok if acct else None, allow_refresh=False)
        drained = got == AUTH_FAIL
        if got and got != AUTH_FAIL:
            pct, windows = got
            drained = pct >= SUB_EXHAUSTED
            if acct:
                with self.lock:
                    acct.usage = {"pct": round(pct, 1), "windows": windows,
                                  "checkedAt": time.time()}
                    acct.broken = False
        elif acct and drained:
            sig = _cred_sig(acct.config_dir)
            with self.lock:
                acct.broken = True
                acct.refused_sig = sig
        if not drained:
            return
        best = self.accounts.get(self._best_account() or "")
        if (not best or best.name == s.account
                or (best.usage or {}).get("pct", 100.0) >= SUB_EXHAUSTED):
            self.broadcast_accounts()
            return                               # nowhere better to go — stay put
        self.broadcast_accounts()
        self._handoff(s, best)

    def _handoff(self, s, target, why="plan drained; resuming under the fresh one"):
        """Move one idle session to `target`'s account: link its transcript
        into the target config dir (real-file-wins, never clobber), replace
        the session object under the SAME cid with a --resume respawn, and
        move the viewers over. The old claude gets SIGTERM once we're sure."""
        if s.busy or not s.alive:                # re-check after the network call
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
        fresh = ClaudeSession(
            self, cid=s.cid, pid=s.pid, session_id=s.session_id, resuming=True,
            title=s.title, desc=s.desc, prompt_count=s.prompt_count,
            first_prompt=s.first_prompt, created=s.created,
            last_active=time.time(),
            account=target.name, config_dir=target.config_dir)
        fresh.last_handoff = s.last_handoff
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
        # The exhausted bypass only fires when the TARGET actually has room —
        # two accounts both over the threshold would otherwise ping-pong every
        # poll (each switch making the other one "best"), debounce ignored.
        # All-exhausted falls back to the debounced rules below.
        exhausted = cur_k[0] and not best_k[0]
        # Did best win on the weekly-reset clock (sooner reset, or a known
        # reset vs an unknown one)? That ordering only changes when a window
        # actually resets, so debounce alone is enough to prevent flap.
        by_reset = best_k[:3] < cur_k[:3] and best_k[1:3] != cur_k[1:3]
        if exhausted or ((by_reset or gain >= SUB_HYSTERESIS)
                         and time.time() - self.last_switch_at >= SUB_DEBOUNCE):
            if exhausted:
                why = "active exhausted"
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
    def create_session(self, pid, account=None):
        if pid not in self.projects:
            return None
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
        if not account and not _has_creds(acct.config_dir if acct else ""):
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
                if _has_creds(alt.config_dir):
                    name, acct = alt.name, alt
                    break
                alt.broken = True
            if acct is None and not _has_creds(""):
                no_creds_anywhere = True
                print("[accounts] NO plan is signed in on this machine — the "
                      "new session opens Claude's login screen; complete it "
                      "once (or sign in via the \U0001f9e0 page)", flush=True)
            self.broadcast_accounts()
        cid = str(uuid.uuid4())
        s = ClaudeSession(self, cid=cid, pid=pid, session_id=str(uuid.uuid4()),
                          resuming=False, created=time.time(),
                          account=name,
                          config_dir=acct.config_dir if acct else "")
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
    """Return (title, desc) for a coding session, or (None, None) if naming is
    unconfigured or the call fails."""
    parsed = _llm_json(NAME_SYS_PROMPT, transcript_text)
    if not parsed:
        return (None, None)
    return (parsed.get("title"), parsed.get("desc"))


def generate_digest(transcript_text):
    """Return (digest, blocked_on) — the volatile live-state summary — or
    (None, None) if naming is unconfigured or the call fails. See
    DIGEST_SYS_PROMPT and docs/CONTROLLER.md."""
    parsed = _llm_json(DIGEST_SYS_PROMPT, transcript_text)
    if not parsed:
        return (None, None)
    return (parsed.get("digest"), parsed.get("blocked_on"))


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
        """Save a pasted/dropped image to the workdir and return its path so the
        browser can fold it into the next message (claude reads it via Read)."""
        if not self._token_ok():
            return self.send_error(403, "bad token")
        n = int(self.headers.get("Content-Length", "0"))
        if n <= 0 or n > MAX_UPLOAD:
            return self.send_error(413, "bad size")
        ctype = self.headers.get("Content-Type", "image/png").split(";")[0].strip()
        ext = EXT_BY_CTYPE.get(ctype, ".png")
        data = self.rfile.read(n)
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        name = f"paste-{uuid.uuid4().hex[:8]}{ext}"
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
        elif t == "createProject":
            MGR.create_project(frame.get("name", ""))
        elif t == "addProject":
            MGR.add_project(frame.get("repoUrl", ""))
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


def main():
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
