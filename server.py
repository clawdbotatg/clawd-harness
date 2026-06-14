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
import fcntl
import glob
import hashlib
import json
import os
import pty
import re
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
BIND       = os.environ.get("BIND", "0.0.0.0")   # 0.0.0.0 = reachable on the LAN
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

# A fresh id per server process. Sent to every client on connect; when a client
# reconnects (e.g. after a daemon restart) and sees a *different* boot id, it
# hard-reloads — fresh state clears any stale "thinking" spinner left mid-turn.
BOOT_ID = uuid.uuid4().hex

# Re-name the session at prompt 1, then every 5 prompts (5, 10, 15, 20, …) so a
# long-running session's title/desc keep sharpening. Naming is cheap + async, so
# the steady cadence is worth it. The instant first-prompt naming lives in
# _on_prompt; this gate fires on Stop once the turn's transcript exists.
def name_at_prompt(count):
    return count == 1 or (count >= 5 and count % 5 == 0)
# The naming instruction — a module constant so bench_naming.py tests the exact
# same prompt the app uses (single source of truth; no drift).
NAME_SYS_PROMPT = ("You name software-engineering sessions. Given a transcript, "
                   "reply with ONLY compact JSON and nothing else: "
                   '{"title": "<max 5 words>", "desc": "<max 12 words>"}. '
                   "The title is a terse label; the desc is a one-line summary.")

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
# The harness always offers *itself* as a pinned project (path = HERE, outside
# PROJECTS_DIR) so you can open a session and live-edit the app you're running.
# Stable sentinel pid so its sessions resume across restarts; never persisted to
# the registry (always re-injected) and never removable.
SELF_PID = "self"

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
        tok = uuid.uuid4().hex[:16]
        tok_file.write_text(tok)
        return tok

TOKEN = _load_or_make_token()


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


def _transcript_exists(session_id):
    return bool(glob.glob(os.path.expanduser(
        f"~/.claude/projects/*/{session_id}.jsonl")))


def _safe_name(name):
    """A filesystem/repo-safe slug from a free-text project name."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", (name or "").strip()).strip("-._")
    return slug or "project"


def _git_remote_url(path):
    """Best-effort origin URL for a repo (empty string if none)."""
    try:
        r = subprocess.run(["git", "remote", "get-url", "origin"],
                           cwd=path, capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


# ── project: a git repo under PROJECTS_DIR that sessions run inside ───────────
class Project:
    """One git repo we drive. Owns no processes itself — it's the workdir N
    ClaudeSessions launch in. `status` tracks an async clone/create."""

    def __init__(self, pid, name, path, repo_url="", status="ready",
                 error="", created=0.0, pinned=False):
        self.pid = pid
        self.name = name
        self.path = path                         # abs path to the repo
        self.repo_url = repo_url
        self.status = status                     # ready | cloning | error
        self.error = error
        self.created = created or time.time()
        self.pinned = pinned                     # the harness-itself project: top of list, not removable

    def to_registry(self):
        return {"pid": self.pid, "name": self.name, "path": self.path,
                "repo_url": self.repo_url, "status": self.status,
                "created": self.created}

    def meta(self, session_count=0, busy_count=0, waiting_count=0):
        return {"pid": self.pid, "name": self.name, "path": self.path,
                "repoUrl": self.repo_url, "status": self.status,
                "error": self.error, "sessionCount": session_count,
                "busyCount": busy_count, "waitingCount": waiting_count,
                "created": self.created, "pinned": self.pinned}


# ── PTY-backed Claude session ─────────────────────────────────────────────────
class ClaudeSession:
    """One interactive `claude` process in a PTY, streamed to the websocket
    clients currently *subscribed* to it. Owned by a SessionManager."""

    def __init__(self, manager, cid, session_id, resuming, pid="",
                 title="", desc="", prompt_count=0, first_prompt="", created=0.0):
        self.manager = manager
        self.pid = pid                           # owning project id
        self.cid = cid                           # stable console id (ours; survives claude rotation)
        self.session_id = session_id             # claude's id (rotates on compaction/resume)
        self.resuming = resuming
        self.created = created or time.time()

        self.title = title
        self.desc = desc
        self.prompt_count = prompt_count
        self.first_prompt = first_prompt
        self.last_active = self.created

        self.master_fd = None
        self.os_pid = None                       # claude's process pid (not the project pid)
        self.proc = None
        self.alive = False

        self.ring = bytearray()                  # recent PTY output for late joiners
        self.ring_lock = threading.Lock()

        self.clients = set()                     # _Clients currently viewing this session
        self.clients_lock = threading.Lock()

        self.transcript_path = None
        self._live_transcript = None             # live path from hooks; may rotate on compaction
        self.busy = False                        # working (turn in flight) vs idle
        self.waiting = False                      # blocked on an interactive prompt (permission / question)
        self.last_tool = None
        self.settings_path = None

    # -- registry shape --------------------------------------------------------
    def to_registry(self):
        return {"cid": self.cid, "pid": self.pid, "session_id": self.session_id,
                "title": self.title, "desc": self.desc,
                "prompt_count": self.prompt_count, "first_prompt": self.first_prompt,
                "created": self.created}

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
        return {"cid": self.cid, "pid": self.pid,
                "title": self.title or self._fallback_title(),
                "desc": self.desc or "",
                "named": bool(self.title),
                "busy": self.busy, "waiting": self.waiting, "tool": self.last_tool,
                "sessionId": self.session_id,
                "promptCount": self.prompt_count,
                "lastActive": self.last_active,
                "created": self.created,
                "alive": self.alive}

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
              f"session_id={self.session_id} "
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
            # re-name at the 1/5/10/15/… milestones to sharpen as it grows.
            if (not self.title) or name_at_prompt(self.prompt_count):
                threading.Thread(target=self._regenerate_name, daemon=True).start()
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
        milestones (1, then every 5) re-name from the full transcript to sharpen."""
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
        return text[-cap:]

    def resize(self, cols, rows):
        if self.master_fd is not None and cols and rows:
            try:
                self._set_winsize(self.master_fd, int(rows), int(cols))
            except OSError:
                pass

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
        self.manager.broadcast_all({"type": "exit", "cid": self.cid})
        self.manager.broadcast_sessions()

    # -- read channel: transcript JSONL -> structured events -------------------
    def _find_transcript(self):
        # Locate by session-id across all project dirs (robust to path encoding).
        hits = glob.glob(os.path.expanduser(
            f"~/.claude/projects/*/{self.session_id}.jsonl"))
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
        recent screen bytes, a hello, and the structured history."""
        with self.clients_lock:
            self.clients.add(client)
        with self.ring_lock:
            snapshot = bytes(self.ring)
        if snapshot:
            client.send_bytes(snapshot)
        client.send_json({"type": "hello",
                          "cid": self.cid, "pid": self.pid,
                          "sessionId": self.session_id,
                          "title": self.title or self._fallback_title(),
                          "workdir": self.workdir(),
                          "busy": self.busy, "waiting": self.waiting, "tool": self.last_tool,
                          "cols": COLS, "rows": ROWS})
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
            p = Project(pid=e.get("pid") or str(uuid.uuid4()),
                        name=e.get("name") or os.path.basename(e["path"]),
                        path=e["path"], repo_url=e.get("repo_url", ""),
                        status=e.get("status", "ready") if e.get("status") != "cloning" else "ready",
                        created=e.get("created", 0.0))
            self.projects[p.pid] = p
        self._discover_projects()                # adopt repos dropped into projects/ by hand
        self._ensure_self_project()              # always offer the harness itself, pinned

        known = set(self.projects)
        for e in reg.get("sessions", []):
            pid = e.get("pid")
            if pid not in known:
                continue                         # orphaned session — its project is gone
            sid = e.get("session_id")
            resuming = bool(sid and _transcript_exists(sid))
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
                created=e.get("created", 0.0))
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
            if path in known_paths or not os.path.isdir(os.path.join(path, ".git")):
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
        and in-flight clones/errors (transient, in-memory only) are left alone.
        Returns True if the set of projects changed. Cheap; runs on the watch
        loop so the list follows disk within ~1s for every open browser."""
        base = str(PROJECTS_DIR) + os.sep
        try:
            on_disk = {str(PROJECTS_DIR / n) for n in os.listdir(PROJECTS_DIR)}
        except OSError:
            on_disk = set()
        with self.lock:
            gone = [pid for pid, p in self.projects.items()
                    if not p.pinned and p.status == "ready"
                    and p.path.startswith(base) and p.path not in on_disk]
        changed = False
        for pid in gone:
            with self.lock:
                p = self.projects.pop(pid, None)
                cids = [c for c, s in self.sessions.items() if s.pid == pid]
            if not p:
                continue
            print(f"[project {p.name}] folder gone from disk → dropped", flush=True)
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
                    "sessions": [s.to_registry() for s in self._ordered()]}
        try:
            REGISTRY_FILE.write_text(json.dumps(data, indent=2))
        except OSError:
            pass

    # -- project crud ----------------------------------------------------------
    def _readopt(self, base):
        """If `base` names a dir already on disk in projects/ (left behind by a
        non-destructive remove, or a partial clone), re-register it in place and
        SKIP cloning — the files are already there, so a clone would only fail
        (e.g. the remote was renamed/deleted). Returns the (re)adopted Project, or
        None when there's nothing on disk to adopt (→ clone fresh)."""
        path = str(PROJECTS_DIR / base)
        with self.lock:
            for p in self.projects.values():
                if p.path == path:               # already registered → reuse it
                    return p
        try:
            present = os.path.isdir(path) and bool(os.listdir(path))
        except OSError:
            present = False
        if not present:
            return None                          # nothing on disk → clone fresh
        is_git = os.path.isdir(os.path.join(path, ".git"))
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
        re-adopt it in place rather than spinning up a `name-2`."""
        base = _safe_name(name)
        existing = self._readopt(base)
        if existing:
            return existing
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
                err = (r.stderr or r.stdout or "failed").strip()
                if kind == "create" and ("auth" in err.lower() or "gh auth" in err.lower()):
                    err += " (is `gh` authenticated in the server's environment?)"
                project.error = err[-300:]
                print(f"[project {project.name}] {kind} FAILED: {project.error}",
                      flush=True)
        except Exception as e:
            project.status = "error"
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

    def _ordered_projects(self):
        # pinned (the harness itself) first, then by creation time
        return sorted(self.projects.values(), key=lambda p: (not p.pinned, p.created))

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
        return [p.meta(*self.session_counts(p.pid))
                for p in self._ordered_projects()]

    # -- session crud ----------------------------------------------------------
    def create_session(self, pid):
        if pid not in self.projects:
            return None
        cid = str(uuid.uuid4())
        s = ClaudeSession(self, cid=cid, pid=pid, session_id=str(uuid.uuid4()),
                          resuming=False, created=time.time())
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
        if not s:
            return
        if client.cid and client.cid != cid:
            old = self.get(client.cid)
            if old:
                old.unsubscribe(client)
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
def generate_name(transcript_text):
    """Return (title, desc) for a coding session, or (None, None) if naming is
    unconfigured or the call fails. Stdlib-only HTTP."""
    if not (BANKR_API_KEY and BANKR_BASE_URL):
        return (None, None)
    sys_prompt = NAME_SYS_PROMPT
    try:
        if BANKR_API == "anthropic":
            url = f"{BANKR_BASE_URL}/v1/messages"
            body = {"model": BANKR_MODEL, "max_tokens": 120,
                    "system": sys_prompt,
                    "messages": [{"role": "user", "content": transcript_text}]}
            headers = {"x-api-key": BANKR_API_KEY,
                       "anthropic-version": "2023-06-01",
                       "content-type": "application/json"}
        else:  # openai-compatible (incl. bankr — same body, different auth header)
            url = f"{BANKR_BASE_URL}/chat/completions"
            body = {"model": BANKR_MODEL, "max_tokens": 120, "temperature": 0.3,
                    "messages": [{"role": "system", "content": sys_prompt},
                                 {"role": "user", "content": transcript_text}]}
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
        if not m:
            return (None, None)
        parsed = json.loads(m.group(0))
        return (parsed.get("title"), parsed.get("desc"))
    except Exception as e:
        print(f"[name] generation failed: {e}", flush=True)
        return (None, None)


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
        return self._query().get("t", [""])[0] == TOKEN

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
        if path == "/config":
            # no token here, and no token returned — the page builds the phone
            # URL from the token already in its own address bar
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
        self.send_error(404, "not found")

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
        if t == "subscribe":
            MGR.subscribe_client(client, frame.get("cid"))
        elif t == "list":
            client.send_json({"type": "projects", "projects": MGR.projects_meta()})
            client.send_json({"type": "sessions",
                              "sessions": MGR.sessions_meta(),
                              "current": MGR.default_cid()})
        elif t == "new":
            s = MGR.create_session(frame.get("pid"))
            if s:
                client.send_json({"type": "focus", "cid": s.cid})
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
                s.write(frame.get("data", "").encode("utf-8"))
            elif t == "send":
                txt = frame.get("text", "")
                print(f"[ws {s.cid[:8]}] send: {txt[:60]!r}", flush=True)
                s.send_message(txt)
            elif t == "resize":
                s.resize(frame.get("cols"), frame.get("rows"))


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
    srv = ThreadingHTTPServer((BIND, PORT), Handler)
    ip = lan_ip()
    print(f"[http] clawd-harness (token required)", flush=True)
    print(f"[http]   workdir : {WORKDIR}", flush=True)
    print(f"[http]   sessions: {len(MGR.sessions)}", flush=True)
    print(f"[http]   local : http://127.0.0.1:{PORT}/?t={TOKEN}", flush=True)
    print(f"[http]   phone : http://{ip}:{PORT}/?t={TOKEN}", flush=True)
    if not (BANKR_API_KEY and BANKR_BASE_URL):
        print("[http]   note  : AI naming off (set BANKR_API_KEY + BANKR_BASE_URL "
              "to enable); using first-prompt titles.", flush=True)
    if BIND == "0.0.0.0":
        print("[http]   ⚠ reachable on your LAN with bypass-permissions — "
              "the token is the only thing gating command execution.", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[http] shutting down", flush=True)
    finally:
        MGR.shutdown()
        srv.server_close()


if __name__ == "__main__":
    main()
