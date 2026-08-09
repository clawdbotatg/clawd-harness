"""A client connection to one harness, speaking the WS protocol verbatim.

This is the controller's eyes and hands on a single machine. It maintains that
harness's slice of the world (projects + sessions, kept fresh from `projects`/
`sessions` broadcasts — which already carry status/digest/blocked_on after the
reading phase), captures each turn's final answer from `Stop` hooks, and exposes
the actuators the intent verbs compile down to (`new`, `send`, `input`, `close`).

Mirrors the fleet worker's discipline: dial out, reconnect with backoff, never
import server.py. The wire contract is docs/WS-PROTOCOL.md.
"""
import json
import threading
import time
import uuid

from .wsclient import client_connect, ws_send, ws_read_message


class HarnessClient:
    def __init__(self, machine_id, ws_url, token, on_change=None, on_hook=None):
        self.machine_id = machine_id
        self.base = ws_url.rstrip("/")
        self.token = token
        self.on_change = on_change or (lambda *a: None)
        # called with (machine_id, frame) for every `hook` frame — the Reactor's feed
        self.on_hook = on_hook or (lambda *a: None)

        self.projects = {}        # pid -> projectMeta
        self.sessions = {}        # cid -> sessionMeta (incl. status/digest/blocked_on)
        self.last_answer = {}     # cid -> last Stop assistant message
        # The last `accounts` frame (subscription pools + usage). Cached rather
        # than fetched: the harness pushes it on every change, and the PM needs
        # to be able to say "that machine is nearly out of plan" without the
        # operator asking the harness UI.
        self.accounts = {}
        self.boot = None
        self.connected = False

        self._state_lock = threading.RLock()
        self._wfile = None
        self._wlock = threading.Lock()
        self._sock = None
        self._stop = False
        self._new_lock = threading.Lock()     # serialize new_session→focus waits
        self._focus_event = threading.Event()
        self._focus_cid = None
        self._pending = {}                    # request id -> {"ev": Event, "resp": frame}

    # -- lifecycle -------------------------------------------------------------
    def start(self):
        threading.Thread(target=self._run, daemon=True, name=f"hc-{self.machine_id}").start()
        return self

    def stop(self):
        self._stop = True
        try:
            if self._sock:
                self._sock.close()
        except OSError:
            pass

    def _url(self):
        return f"{self.base}/ws?t={self.token}"

    def _run(self):
        backoff = 0.5
        while not self._stop:
            try:
                sock, rfile, wfile = client_connect(self._url())
                self._sock, self._wfile, self.connected = sock, wfile, True
                backoff = 0.5
                self.send({"type": "list"})
                self.on_change(self.machine_id, "connected")
                while not self._stop:
                    msg = ws_read_message(rfile)
                    if msg is None:
                        break
                    kind, data = msg
                    if kind == "ping":
                        ws_send(wfile, self._wlock, data, opcode=0xA, mask=True)
                        continue
                    if kind == "close":
                        break
                    if kind in ("pong",) or kind == 0x2:   # ignore PTY bytes — we read structured state
                        continue
                    try:
                        self._handle(json.loads(data.decode()))
                    except Exception:
                        continue
            except Exception:
                pass
            self.connected = False
            self._flush_pending("disconnected")
            self.on_change(self.machine_id, "disconnected")
            if self._stop:
                break
            time.sleep(backoff)
            backoff = min(backoff * 2, 10)

    # -- inbound state ---------------------------------------------------------
    def _handle(self, f):
        t = f.get("type")
        with self._state_lock:
            if t == "projects":
                self.boot = f.get("boot")
                self.projects = {p["pid"]: p for p in f.get("projects", [])}
            elif t == "sessions":
                self.sessions = {s["cid"]: s for s in f.get("sessions", [])}
            elif t == "accounts":
                self.accounts = f
            elif t == "hook":
                if f.get("event") == "Stop":
                    self.last_answer[f.get("cid")] = (f.get("data") or {}).get("last", "")
            elif t == "focus":
                self._focus_cid = f.get("cid")
                self._focus_event.set()
            elif t == "exit":
                self.sessions.pop(f.get("cid"), None)
            elif t in ("searchResult", "transcriptTailResult", "screenResult"):
                slot = self._pending.pop(f.get("id"), None)
                if slot:
                    slot["resp"] = f
                    slot["ev"].set()
        if t == "hook":
            self.on_hook(self.machine_id, f)        # → Reactor (higher-level events)
        self.on_change(self.machine_id, t)

    # -- outbound --------------------------------------------------------------
    def send(self, obj):
        wf = self._wfile
        if wf is None:
            return False
        try:
            ws_send(wf, self._wlock, json.dumps(obj), opcode=0x1, mask=True)
            return True
        except Exception:
            return False

    def refresh(self):
        return self.send({"type": "list"})

    def new_session(self, pid, timeout=12, engine="claude"):
        """Create a session in `pid` and block for the server's focus reply with
        the new cid (None on timeout). Serialized so concurrent spawns don't
        steal each other's focus event. `engine` picks the agent CLI
        ("claude" | "codex"); a harness older than the engine layer ignores it
        and spawns claude, which is the safe fallback."""
        with self._new_lock:
            self._focus_event.clear()
            self._focus_cid = None
            if not self.send({"type": "new", "pid": pid, "engine": engine}):
                return None
            if self._focus_event.wait(timeout):
                return self._focus_cid
            return None

    def send_message(self, cid, text):
        return self.send({"type": "send", "cid": cid, "text": text})

    def raw_input(self, cid, data):
        return self.send({"type": "input", "cid": cid, "data": data})

    # -- request/reply reads (search / transcriptTail / screen) ---------------
    def request(self, frame, timeout=12):
        """Send a frame with a correlation id and block for its *Result reply.
        Returns the reply frame, or {"error": …} on no-connection/timeout — a
        harness that predates the frame simply never answers, so the timeout is
        the graceful-degrade path for old servers."""
        rid = uuid.uuid4().hex[:8]
        slot = {"ev": threading.Event(), "resp": None}
        with self._state_lock:
            self._pending[rid] = slot
        if not self.send(dict(frame, id=rid)):
            with self._state_lock:
                self._pending.pop(rid, None)
            return {"error": "not connected"}
        if not slot["ev"].wait(timeout):
            with self._state_lock:
                self._pending.pop(rid, None)
            return {"error": "timeout (harness predates this frame?)"}
        return slot["resp"]

    def _flush_pending(self, why):
        with self._state_lock:
            pending, self._pending = dict(self._pending), {}
        for slot in pending.values():
            slot["resp"] = {"error": why}
            slot["ev"].set()

    def search(self, q, scope="all", limit=20):
        return self.request({"type": "search", "q": q, "scope": scope, "limit": limit})

    def transcript_tail(self, cid, n=30, chars=400):
        return self.request({"type": "transcriptTail", "cid": cid, "n": n, "chars": chars})

    def screen(self, cid, chars=1500):
        return self.request({"type": "screen", "cid": cid, "chars": chars})

    def close_session(self, cid):
        return self.send({"type": "close", "cid": cid})

    def pin_session(self, cid, on=True):
        return self.send({"type": "pin", "cid": cid, "on": bool(on)})

    def create_project(self, name):
        return self.send({"type": "createProject", "name": name})

    def add_project(self, repo_url):
        return self.send({"type": "addProject", "repoUrl": repo_url})

    def add_local_project(self, path):
        return self.send({"type": "addLocalProject", "path": path})

    def remove_project(self, pid):
        return self.send({"type": "removeProject", "pid": pid})

    # -- snapshot --------------------------------------------------------------
    def state(self):
        with self._state_lock:
            return {
                "machine": self.machine_id,
                "connected": self.connected,
                "boot": self.boot,
                "projects": [dict(p) for p in self.projects.values()],
                "sessions": [dict(s) for s in self.sessions.values()],
                "last_answer": dict(self.last_answer),
                "accounts": dict(self.accounts),
            }
