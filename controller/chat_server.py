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
import time
from http.server import BaseHTTPRequestHandler
from socketserver import TCPServer, ThreadingMixIn
from urllib.parse import parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
CHAT_HTML = os.path.join(HERE, "chat.html")
DEBUG_HTML = os.path.join(HERE, "debug.html")


class ThreadingHTTPServer(ThreadingMixIn, TCPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_handler(router, verbs, guard, backend_getter, reactor=None, mcp=None,
                 prompt_brain=None, turn_lock=None, autopilot=None):
    # One turn at a time — shared with the autopilot when serve passes its
    # lock in, so event-driven turns and operator chats never interleave.
    chat_lock = turn_lock or threading.Lock()
    from . import config
    from .mcp import TOOLS

    def current_model():
        # Live brain model (runtime override or env pin); empty → Claude Code's default.
        return getattr(prompt_brain, "model", None) or config.AGENT_MODEL or ""

    def model_label():
        # The PM is a minimal claude-p-agent: real Claude on the subscription.
        m = current_model()
        return f"claude · {m}" if m else "claude (subscription)"

    # Choices offered on the debug page's Config tab (a custom id is also accepted).
    KNOWN_MODELS = ["claude-fable-5", "claude-opus-4-8", "claude-sonnet-5",
                    "claude-haiku-4-5-20251001"]

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
            # CORS: let the main harness UI (a different port → different origin)
            # fetch this API so the PM can be folded into index.html as a panel.
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/":
                try:
                    with open(CHAT_HTML, "rb") as f:
                        return self._send(200, f.read(), "text/html; charset=utf-8")
                except OSError:
                    return self._send(500, {"error": "chat.html missing"})
            if path == "/debug":
                try:
                    with open(DEBUG_HTML, "rb") as f:
                        return self._send(200, f.read(), "text/html; charset=utf-8")
                except OSError:
                    return self._send(500, {"error": "debug.html missing"})
            if path == "/api/tools":
                return self._send(200, {"tools": [
                    {"name": n, "description": d, "inputSchema": s} for n, d, s in TOOLS]})
            if path == "/api/prompt":
                if not prompt_brain:
                    return self._send(200, {"prompt": "", "overridden": False,
                                            "note": "active backend has no editable prompt"})
                return self._send(200, {
                    "prompt": prompt_brain.current_prompt(),
                    "default": prompt_brain.default_prompt(),
                    "overridden": prompt_brain.prompt_override is not None})
            if path == "/api/world":
                return self._send(200, verbs.get_world())
            if path == "/api/attention":
                return self._send(200, verbs.get_attention())
            if path == "/api/tasks":
                return self._send(200, verbs.list_tasks())
            if path == "/api/notifications":
                return self._send(200, {"events": reactor.recent() if reactor else []})
            if path == "/api/autopilot":
                if not autopilot:
                    return self._send(200, {"available": False})
                return self._send(200, {"available": True, **autopilot.status()})
            if path == "/api/turn":
                # live progress of the in-flight PM turn (or {"active":false}) —
                # lets a front-end show "working: 7 tools, last: find gmail"
                # instead of silence while a long turn runs.
                st = dict(getattr(prompt_brain, "turn_status", None) or {"active": False})
                if st.get("active") and st.get("started"):
                    st["elapsed_s"] = round(time.time() - st["started"], 1)
                return self._send(200, st)
            if path == "/api/threads":
                return self._send(200, router.list_threads())
            if path == "/api/thread/messages":
                qs = self.path.split("?", 1)[1] if "?" in self.path else ""
                tid = (parse_qs(qs).get("id") or [None])[0]
                return self._send(200, router.thread_messages(tid))
            if path == "/api/voice/lore":
                # One clawd-md knowledge-base page for the voice model's
                # read_lore tool (?name=index lists pages). Bounded + sanitized
                # in voice.read_lore.
                from . import voice
                qs = self.path.split("?", 1)[1] if "?" in self.path else ""
                name = (parse_qs(qs).get("name") or ["index"])[0]
                return self._send(200, voice.read_lore(name))
            if path == "/api/state":
                snap = verbs.get_world()
                # harness link info so the UI can build deep links into sessions
                # (rebuilt against the browser's own hostname client-side).
                import urllib.parse as _u
                hbase = config.harness_http_base()
                hparsed = _u.urlparse(hbase)
                return self._send(200, {
                    "autonomy": guard.autonomy, "backend": backend_getter(),
                    "model": model_label(), "model_id": current_model(),
                    "models": KNOWN_MODELS,
                    "harness": {"base": hbase, "token": config.harness_token(),
                                "port": hparsed.port or (443 if hparsed.scheme == "https" else 80)},
                    # compact world: per-machine session count now rides "sessions"
                    "machines": [{"id": m["id"], "connected": m["connected"],
                                  "sessions": m.get("sessions", 0)} for m in snap["machines"]],
                    "attention_count": snap.get("attention_count", 0)})
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
            if path == "/api/model":         # pick the PM's claude --model (debug page)
                if not prompt_brain or not hasattr(prompt_brain, "set_model"):
                    return self._send(400, {"error": "active backend has no model knob"})
                model = (data.get("model") or "").strip()
                # ids are passed straight to `claude --model` (argv, no shell) — just
                # keep them sane-looking; empty clears the override.
                if model and not all(c.isalnum() or c in "._:-" for c in model):
                    return self._send(400, {"error": "bad model id"})
                prompt_brain.set_model(model)
                return self._send(200, {"ok": True, "model_id": current_model(),
                                        "model": model_label()})
            if path == "/api/autopilot":
                if not autopilot:
                    return self._send(400, {"error": "autopilot unavailable"})
                if "on" in data:                         # the kill switch
                    autopilot.set_enabled(bool(data["on"]))
                if data.get("flush"):                    # push the digest now
                    return self._send(200, {"ok": True,
                                            "flushed": autopilot.flush_digest(),
                                            **autopilot.status()})
                return self._send(200, {"ok": True, **autopilot.status()})
            if path == "/api/autonomy":
                mode = (data.get("autonomy") or "").lower()
                if mode in ("readonly", "confirm", "auto"):
                    guard.autonomy = mode
                    return self._send(200, {"ok": True, "autonomy": mode})
                return self._send(400, {"error": "autonomy must be readonly|confirm|auto"})
            if path == "/api/reset":
                with chat_lock:
                    router.reset()
                return self._send(200, {"ok": True})
            # -- PM threads (multiple conversations) --------------------------
            # These do NOT take chat_lock. A turn runs for minutes, and parking
            # thread bookkeeping behind it made every control in the PM tab dead
            # for the length of the turn: ＋ new, a thread tab, ✕ archive all
            # blocked until the reply landed, so the tab read as frozen (worse,
            # each dead click burned one of the browser's ~6 connections to the
            # origin, eventually stalling the rest of the page too). Safety comes
            # from `Threads`' own short lock plus `Router.chat` pinning its `tid`
            # up front — `current` moving mid-turn can't misroute the transcript.
            if path == "/api/thread/new":
                return self._send(200, router.new_thread(data.get("title")))
            if path == "/api/thread/select":
                tid = data.get("id")
                if not tid:
                    return self._send(400, {"error": "id required"})
                return self._send(200, router.select_thread(tid))
            if path == "/api/thread/archive":
                return self._send(200, router.archive_thread(data.get("id")))
            # Clear is the exception: it drops the thread's *brain* memory, which
            # an in-flight turn is actively writing. Don't block on the turn (that
            # is the freeze we just fixed) — refuse fast so the UI can say why.
            if path == "/api/thread/clear":
                if not chat_lock.acquire(blocking=False):
                    return self._send(409, {"error": "a PM turn is in flight — "
                                                     "clear once it finishes"})
                try:
                    return self._send(200, router.clear_thread(data.get("id")))
                finally:
                    chat_lock.release()
            if path == "/api/voice/token":
                # Mint an ephemeral OpenAI realtime secret for the 🎙 voice PM
                # (the real key stays server-side; see controller/voice.py).
                from . import voice
                if not voice.enabled():
                    return self._send(503, {"error": "voice not configured "
                                            "(OPENAI_API_KEY unset on the controller)"})
                try:
                    return self._send(200, voice.mint(verbs))
                except Exception as e:
                    return self._send(502, {"error": f"voice mint failed: "
                                            f"{type(e).__name__}: {e}"})
            if path == "/api/tool":          # invoke a tool by hand (debug page)
                if not mcp:
                    return self._send(503, {"error": "tool runner unavailable"})
                name = data.get("name")
                args = data.get("args") or {}
                try:
                    result = mcp.call_tool(name, args)
                except Exception as e:
                    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                return self._send(200, {"tool": name, "args": args, "result": result})
            if path == "/api/prompt":        # edit / reset the system prompt
                if not prompt_brain:
                    return self._send(400, {"error": "active backend has no editable prompt"})
                prompt_brain.set_prompt("" if data.get("reset") else data.get("prompt", ""))
                return self._send(200, {"ok": True,
                                        "overridden": prompt_brain.prompt_override is not None})
            return self._send(404, {"error": "not found"})

    return Handler


def serve_with_router(router, verbs, guard, backend_getter, port, bind="127.0.0.1",
                      reactor=None, mcp=None, prompt_brain=None,
                      turn_lock=None, autopilot=None):
    srv = ThreadingHTTPServer((bind, port),
                              make_handler(router, verbs, guard, backend_getter,
                                           reactor, mcp, prompt_brain,
                                           turn_lock, autopilot))
    print(f"[controller] chat UI on http://{bind}:{port}  ·  debug http://{bind}:{port}/debug  "
          f"(backend={backend_getter()}, autonomy={guard.autonomy})", flush=True)
    srv.serve_forever()
