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
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingMixIn, TCPServer

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
BANKR_API      = os.environ.get("BANKR_API", "openai").lower()   # openai | anthropic
NAME_AT_PROMPTS = {1, 3, 10}      # regenerate title/desc after these prompt counts

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
UPLOAD_DIR = Path(WORKDIR) / ".clawd-harness-uploads"   # pasted images land here
MAX_UPLOAD = 25 * 1024 * 1024
EXT_BY_CTYPE = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
                "image/webp": ".webp"}
REGISTRY_FILE = HERE / ".clawd-harness.sessions.json"   # persists all sessions across restarts
LEGACY_SESSION_FILE = HERE / ".clawd-harness.session"   # pre-multi-session single id (migrated)

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


# ── PTY-backed Claude session ─────────────────────────────────────────────────
class ClaudeSession:
    """One interactive `claude` process in a PTY, streamed to the websocket
    clients currently *subscribed* to it. Owned by a SessionManager."""

    def __init__(self, manager, cid, session_id, resuming,
                 title="", desc="", prompt_count=0, first_prompt="", created=0.0):
        self.manager = manager
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
        self.pid = None
        self.proc = None
        self.alive = False

        self.ring = bytearray()                  # recent PTY output for late joiners
        self.ring_lock = threading.Lock()

        self.clients = set()                     # _Clients currently viewing this session
        self.clients_lock = threading.Lock()

        self.transcript_path = None
        self._live_transcript = None             # live path from hooks; may rotate on compaction
        self.busy = False                        # working (turn in flight) vs idle
        self.last_tool = None
        self.settings_path = None

    # -- registry shape --------------------------------------------------------
    def to_registry(self):
        return {"cid": self.cid, "session_id": self.session_id,
                "title": self.title, "desc": self.desc,
                "prompt_count": self.prompt_count, "first_prompt": self.first_prompt,
                "created": self.created}

    def _fallback_title(self):
        if self.first_prompt:
            words = self.first_prompt.split()
            t = " ".join(words[:7])
            return (t[:46] + "…") if len(t) > 47 else t
        return "new session"

    def meta(self):
        """Menu-level snapshot broadcast to every client."""
        return {"cid": self.cid,
                "title": self.title or self._fallback_title(),
                "desc": self.desc or "",
                "named": bool(self.title),
                "busy": self.busy, "tool": self.last_tool,
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
            cmd, cwd=WORKDIR, env=env,
            stdin=slave, stdout=slave, stderr=slave,
            preexec_fn=_preexec, close_fds=True,
        )
        os.close(slave)                          # parent only needs the master
        self.master_fd = master
        self.pid = self.proc.pid
        self.alive = True
        print(f"[session {self.cid[:8]}] claude pid={self.pid} "
              f"session_id={self.session_id} "
              f"({'resumed' if self.resuming else 'new'})", flush=True)

        threading.Thread(target=self._pump_pty, daemon=True).start()
        threading.Thread(target=self._tail_transcript, daemon=True).start()

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
        data = {}
        if ev == "UserPromptSubmit":
            self.busy = True
            prompt = obj.get("prompt", "")
            data = {"prompt": prompt}
            self._on_prompt(prompt)
        elif ev == "PreToolUse":
            self.busy = True
            self.last_tool = obj.get("tool_name")
            data = {"tool": obj.get("tool_name")}
        elif ev == "PostToolUse":
            self.busy = True
            data = {"tool": obj.get("tool_name"),
                    "duration_ms": obj.get("duration_ms")}
        elif ev == "Stop":
            self.busy = False
            self.last_tool = None
            data = {"last": obj.get("last_assistant_message", "")}
        elif ev == "Notification":
            data = {"message": obj.get("message", "")}
        elif ev == "SessionStart":
            self.busy = False
            data = {"source": obj.get("source"), "model": obj.get("model")}
        elif ev == "SessionEnd":
            data = {"reason": obj.get("reason")}
        self.manager.broadcast_all({"type": "hook", "cid": self.cid, "event": ev,
                                    "busy": self.busy, "tool": self.last_tool,
                                    "data": data})
        self.manager.broadcast_sessions()

    def _on_prompt(self, prompt):
        """First/Nth user prompt: keep a fallback title and (re)generate the AI
        name at the chosen milestones."""
        self.prompt_count += 1
        if not self.first_prompt and prompt:
            self.first_prompt = prompt.strip().splitlines()[0][:200]
        self.manager.save_registry()
        if self.prompt_count in NAME_AT_PROMPTS:
            threading.Thread(target=self._regenerate_name, daemon=True).start()

    def _regenerate_name(self):
        text = self._transcript_text_for_naming()
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
        while self.alive and target:
            self.transcript_path = target
            print(f"[transcript {self.cid[:8]}] tailing {target}", flush=True)
            try:
                f = open(target, "r")
            except OSError:
                time.sleep(0.25)
                target = self._live_transcript or target
                continue
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
                          "cid": self.cid,
                          "sessionId": self.session_id,
                          "title": self.title or self._fallback_title(),
                          "workdir": WORKDIR,
                          "busy": self.busy, "tool": self.last_tool,
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


# ── session manager: registry of ClaudeSessions ───────────────────────────────
class SessionManager:
    def __init__(self):
        self.sessions = {}                       # cid -> ClaudeSession
        self.lock = threading.RLock()
        self.all_clients = set()                 # every connected browser
        self.clients_lock = threading.Lock()

    # -- startup / persistence -------------------------------------------------
    def load(self):
        entries = self._read_registry()
        if not entries:
            legacy = self._migrate_legacy()
            entries = [legacy] if legacy else []
        for e in entries:
            sid = e.get("session_id")
            resuming = bool(sid and _transcript_exists(sid))
            if sid and not resuming:
                # transcript gone (e.g. cleared history) — start it fresh instead
                # of resuming into nothing.
                sid = str(uuid.uuid4())
            s = ClaudeSession(
                self, cid=e.get("cid") or str(uuid.uuid4()),
                session_id=sid or str(uuid.uuid4()), resuming=resuming,
                title=e.get("title", ""), desc=e.get("desc", ""),
                prompt_count=e.get("prompt_count", 0),
                first_prompt=e.get("first_prompt", ""),
                created=e.get("created", 0.0))
            self.sessions[s.cid] = s
            s.start()
        if not self.sessions:
            self.create()                        # always have at least one
        self.save_registry()

    def _read_registry(self):
        try:
            data = json.loads(REGISTRY_FILE.read_text())
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def _migrate_legacy(self):
        """One-time: lift the pre-multi-session single saved id into the registry
        so an upgrade keeps the existing conversation."""
        try:
            saved = LEGACY_SESSION_FILE.read_text().strip()
        except OSError:
            return None
        if saved and _transcript_exists(saved):
            return {"cid": str(uuid.uuid4()), "session_id": saved}
        return None

    def save_registry(self):
        with self.lock:
            data = [s.to_registry() for s in self._ordered()]
        try:
            REGISTRY_FILE.write_text(json.dumps(data, indent=2))
        except OSError:
            pass

    # -- session crud ----------------------------------------------------------
    def create(self):
        cid = str(uuid.uuid4())
        s = ClaudeSession(self, cid=cid, session_id=str(uuid.uuid4()),
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

    def close(self, cid):
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
        with self.lock:
            if not self.sessions:                # never leave the app empty
                self.create()
        self.save_registry()
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
        client.send_json({"type": "sessions",
                          "sessions": self.sessions_meta(),
                          "current": self.default_cid()})
        cur = self.default_cid()
        if cur:
            client.send_json({"type": "focus", "cid": cur})

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

    def broadcast_sessions(self):
        self.broadcast_all({"type": "sessions",
                            "sessions": self.sessions_meta(),
                            "current": self.default_cid()})

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
    sys_prompt = ("You name software-engineering sessions. Given a transcript, "
                  "reply with ONLY compact JSON and nothing else: "
                  '{"title": "<max 5 words>", "desc": "<max 12 words>"}. '
                  "The title is a terse label; the desc is a one-line summary.")
    try:
        if BANKR_API == "anthropic":
            url = f"{BANKR_BASE_URL}/v1/messages"
            body = {"model": BANKR_MODEL, "max_tokens": 120,
                    "system": sys_prompt,
                    "messages": [{"role": "user", "content": transcript_text}]}
            headers = {"x-api-key": BANKR_API_KEY,
                       "anthropic-version": "2023-06-01",
                       "content-type": "application/json"}
        else:  # openai-compatible
            url = f"{BANKR_BASE_URL}/chat/completions"
            body = {"model": BANKR_MODEL, "max_tokens": 120, "temperature": 0.3,
                    "messages": [{"role": "system", "content": sys_prompt},
                                 {"role": "user", "content": transcript_text}]}
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
            })
        self.send_error(404, "not found")

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/hook":
            return self._handle_hook()
        if path == "/upload":
            return self._handle_upload()
        self.send_error(404, "not found")

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
            client.send_json({"type": "sessions",
                              "sessions": MGR.sessions_meta(),
                              "current": MGR.default_cid()})
        elif t == "new":
            s = MGR.create()
            client.send_json({"type": "focus", "cid": s.cid})
        elif t == "close":
            MGR.close(frame.get("cid"))
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


def main():
    MGR.load()
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
