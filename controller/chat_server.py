"""HTTP chat surface for the PM brain + a live fleet panel.

Serves a small single-page chat UI and a JSON API over the same verb surface the
MCP server uses. This is how you talk to the project manager in a browser: type,
it reads/acts on the fleet, and shows what it did (the tool trace). The fleet
panel reads the live world/attention so you see state next to the conversation.

Stdlib http.server, bound to localhost. Brain turns are serialized (one
conversation, one user) so request races can't corrupt history.
"""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler
from socketserver import TCPServer, ThreadingMixIn

HERE = os.path.dirname(os.path.abspath(__file__))
CHAT_HTML = os.path.join(HERE, "chat.html")


class ThreadingHTTPServer(ThreadingMixIn, TCPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_handler(router, verbs, guard, backend_getter, reactor=None):
    chat_lock = threading.Lock()
    from . import config

    def model_label():
        return config.BRAIN_MODEL if backend_getter() == "bankr" else "claude-code -p"

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            if isinstance(body, (dict, list)):
                body = json.dumps(body).encode()
            elif isinstance(body, str):
                body = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/":
                try:
                    with open(CHAT_HTML, "rb") as f:
                        return self._send(200, f.read(), "text/html; charset=utf-8")
                except OSError:
                    return self._send(500, {"error": "chat.html missing"})
            if path == "/api/world":
                return self._send(200, verbs.get_world())
            if path == "/api/attention":
                return self._send(200, verbs.get_attention())
            if path == "/api/tasks":
                return self._send(200, verbs.list_tasks())
            if path == "/api/notifications":
                return self._send(200, {"events": reactor.recent() if reactor else []})
            if path == "/api/state":
                snap = verbs.get_world()
                return self._send(200, {
                    "autonomy": guard.autonomy, "backend": backend_getter(),
                    "model": model_label(),
                    "machines": [{"id": m["id"], "connected": m["connected"],
                                  "sessions": m["session_total"]} for m in snap["machines"]],
                    "attention_count": snap["attention_count"]})
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            path = self.path.split("?")[0]
            n = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            try:
                data = json.loads(raw.decode() or "{}")
            except Exception:
                return self._send(400, {"error": "bad json"})
            if path == "/api/chat":
                msg = (data.get("message") or "").strip()
                if not msg:
                    return self._send(400, {"error": "empty message"})
                with chat_lock:
                    out = router.chat(msg)
                return self._send(200, out)
            if path == "/api/autonomy":
                mode = (data.get("autonomy") or "").lower()
                if mode in ("readonly", "confirm", "auto"):
                    guard.autonomy = mode
                    return self._send(200, {"ok": True, "autonomy": mode})
                return self._send(400, {"error": "autonomy must be readonly|confirm|auto"})
            if path == "/api/backend":
                name = (data.get("backend") or "").lower()
                if name in ("bankr", "claude-code"):
                    router.switch(name)
                    return self._send(200, {"ok": True, "backend": name})
                return self._send(400, {"error": "backend must be bankr|claude-code"})
            if path == "/api/reset":
                router.reset()
                return self._send(200, {"ok": True})
            return self._send(404, {"error": "not found"})

    return Handler


def serve_with_router(router, verbs, guard, backend_getter, port, bind="127.0.0.1", reactor=None):
    srv = ThreadingHTTPServer((bind, port),
                              make_handler(router, verbs, guard, backend_getter, reactor))
    print(f"[controller] chat UI on http://{bind}:{port}  "
          f"(backend={backend_getter()}, autonomy={guard.autonomy})", flush=True)
    srv.serve_forever()
