"""A mock harness: a minimal WS server speaking docs/WS-PROTOCOL.md.

Lets the controller be tested end-to-end with no real `claude` — same trick as
fleet/fleet_proxy_smoke.py, but richer: it carries the reading-phase session meta
(status/digest/blocked_on), supports new/send/input/close, broadcasts `sessions`
on every change, and can be scripted into a blocked state to exercise the
attention queue. Importable from tests; `MockHarness(port).start()`.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler
from socketserver import TCPServer, ThreadingMixIn
from urllib.parse import urlparse, parse_qs

from .wsclient import ws_send, ws_read_message, server_accept_headers

TOKEN = "mocktoken"


class _State:
    def __init__(self):
        self.lock = threading.RLock()
        self.projects = {"p1": {"pid": "p1", "name": "demo", "status": "ready",
                                "sessionCount": 0, "busyCount": 0, "waitingCount": 0,
                                "pinned": False}}
        self.sessions = {}
        self.conns = []          # (wfile, lock)
        self._n = 0

    def add_session(self, pid="p1", **kw):
        with self.lock:
            self._n += 1
            cid = f"c{self._n}"
            s = {"cid": cid, "pid": pid, "title": kw.get("title", f"session {cid}"),
                 "desc": "", "named": False, "busy": False, "waiting": False,
                 "tool": None, "status": "idle", "digest": "", "blocked_on": "",
                 "sessionId": f"s{cid}", "promptCount": 0, "lastActive": 0,
                 "created": 0, "alive": True}
            s.update({k: v for k, v in kw.items() if k in s})
            self.sessions[cid] = s
        return cid

    def set_session(self, cid, **kw):
        with self.lock:
            if cid in self.sessions:
                self.sessions[cid].update(kw)
        self.broadcast(self.sessions_frame())

    def sessions_frame(self):
        with self.lock:
            return {"type": "sessions", "current": None,
                    "sessions": [dict(s) for s in self.sessions.values()]}

    def projects_frame(self):
        with self.lock:
            return {"type": "projects", "boot": "mockboot",
                    "projects": [dict(p) for p in self.projects.values()]}

    def register(self, wfile, lock):
        with self.lock:
            self.conns.append((wfile, lock))

    def unregister(self, wfile, lock):
        with self.lock:
            try:
                self.conns.remove((wfile, lock))
            except ValueError:
                pass

    def broadcast(self, obj):
        with self.lock:
            conns = list(self.conns)
        for wf, lk in conns:
            try:
                ws_send(wf, lk, json.dumps(obj), opcode=0x1)
            except Exception:
                self.unregister(wf, lk)


def _make_handler(state):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_GET(self):
            q = parse_qs(urlparse(self.path).query)
            if urlparse(self.path).path != "/ws" or q.get("t", [""])[0] != TOKEN:
                return self.send_error(403, "bad token")
            key = self.headers.get("Sec-WebSocket-Key", "")
            self.send_response(101)
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", server_accept_headers(key))
            self.end_headers()
            self.close_connection = True
            lock = threading.Lock()

            def j(obj):
                ws_send(self.wfile, lock, json.dumps(obj), opcode=0x1)

            state.register(self.wfile, lock)
            j(state.projects_frame())
            j(state.sessions_frame())
            try:
                while True:
                    msg = ws_read_message(self.rfile)
                    if msg is None:
                        break
                    kind, data = msg
                    if kind == "close":
                        break
                    if kind == "ping":
                        ws_send(self.wfile, lock, data, opcode=0xA)
                        continue
                    if kind == "pong" or kind == 0x2:
                        continue
                    try:
                        f = json.loads(data.decode())
                    except Exception:
                        continue
                    self._dispatch(f, j)
            finally:
                state.unregister(self.wfile, lock)

        def _dispatch(self, f, j):
            t = f.get("type")
            if t == "list":
                j(state.projects_frame())
                j(state.sessions_frame())
            elif t == "new":
                cid = state.add_session(f.get("pid", "p1"))
                j({"type": "focus", "cid": cid})
                state.broadcast(state.sessions_frame())
            elif t == "subscribe":
                cid = f.get("cid")
                j({"type": "hello", "cid": cid, "pid": "p1", "sessionId": f"s{cid}",
                   "title": "demo", "workdir": "/x", "busy": False, "waiting": False,
                   "tool": None, "cols": 80, "rows": 24})
            elif t == "send":
                cid = f.get("cid")
                text = f.get("text", "")
                state.set_session(cid, busy=True, status="working", promptCount=1)
                state.broadcast({"type": "hook", "cid": cid, "event": "UserPromptSubmit",
                                 "busy": True, "waiting": False, "tool": None,
                                 "data": {"prompt": text}})
                # A prompt containing ASK? simulates the session ending blocked.
                if "ASK?" in text:
                    state.set_session(cid, busy=False, waiting=True, status="blocked",
                                      blocked_on="which option?", digest="awaiting a decision")
                    state.broadcast({"type": "hook", "cid": cid, "event": "Notification",
                                     "busy": True, "waiting": True, "tool": None,
                                     "data": {"message": "needs your input"}})
                else:
                    state.set_session(cid, busy=False, status="idle",
                                      digest=f"handled: {text[:30]}")
                    state.broadcast({"type": "hook", "cid": cid, "event": "Stop",
                                     "busy": False, "waiting": False, "tool": None,
                                     "data": {"last": f"done: {text}"}})
            elif t == "input":
                # answering a waiting prompt clears the block
                cid = f.get("cid")
                state.set_session(cid, waiting=False, status="idle", blocked_on="")
                state.broadcast({"type": "hook", "cid": cid, "event": "Stop",
                                 "busy": False, "waiting": False, "tool": None,
                                 "data": {"last": "proceeding"}})
            elif t == "close":
                cid = f.get("cid")
                with state.lock:
                    state.sessions.pop(cid, None)
                state.broadcast(state.sessions_frame())
                j({"type": "exit", "cid": cid})

    return Handler


class ThreadingHTTPServer(ThreadingMixIn, TCPServer):
    daemon_threads = True
    allow_reuse_address = True


class MockHarness:
    def __init__(self, port):
        self.port = port
        self.state = _State()
        self._srv = None

    def start(self):
        self._srv = ThreadingHTTPServer(("127.0.0.1", self.port), _make_handler(self.state))
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()
        return self

    def stop(self):
        if self._srv:
            self._srv.shutdown()

    @property
    def url(self):
        return f"ws://127.0.0.1:{self.port}"
