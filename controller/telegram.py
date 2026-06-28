"""Telegram front-end for the PM brain (stdlib HTTPS, no deps).

Talk to the fleet PM from your phone: messages from allowlisted users are routed
to the same brain the web chat uses; its replies come back as Telegram messages.
The Reactor can also `notify()` proactively — so a session going `blocked` pushes
you an alert without you asking.

Safety: long-polling (getUpdates) allows ONE consumer per bot token. If the
configured token is already polled elsewhere, Telegram returns 409 — we detect
that, log it, and stop (rather than spin or disrupt the other consumer). Point
CONTROLLER_TELEGRAM_TOKEN at a bot that isn't otherwise in use.
"""
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


class TelegramBridge:
    def __init__(self, token, allow_ids, router, base_url="https://api.telegram.org"):
        self.token = token
        self.allow = set(str(a) for a in (allow_ids or []) if str(a))
        self.router = router
        self.base = f"{base_url}/bot{token}"
        self.offset = 0
        self._stop = False
        self._chat_ids = set(self.allow)     # DMs: user id == chat id; seed from allowlist

    # -- Telegram Bot API ------------------------------------------------------
    def _api(self, method, params=None, timeout=40):
        data = urllib.parse.urlencode(params or {}).encode()
        req = urllib.request.Request(f"{self.base}/{method}", data=data, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())

    def get_me(self):
        return self._api("getMe", timeout=10)

    def _send(self, chat_id, text):
        try:
            self._api("sendMessage", {"chat_id": chat_id, "text": text[:4000]})
        except Exception as e:
            print(f"[telegram] send failed: {e}", flush=True)

    # -- proactive push (used by the Reactor) ---------------------------------
    def notify(self, text):
        for c in list(self._chat_ids):
            self._send(c, text)

    # -- lifecycle -------------------------------------------------------------
    def start(self):
        try:
            me = self.get_me()
            who = (me.get("result") or {}).get("username", "?")
            print(f"[telegram] connected as @{who}; allow={sorted(self.allow) or 'ANY'}", flush=True)
        except Exception as e:
            print(f"[telegram] getMe failed ({e}) — bridge not started", flush=True)
            return self
        threading.Thread(target=self._poll, daemon=True, name="tg-poll").start()
        return self

    def stop(self):
        self._stop = True

    def _poll(self):
        while not self._stop:
            try:
                resp = self._api("getUpdates", {"offset": self.offset, "timeout": 30}, timeout=40)
            except urllib.error.HTTPError as e:
                if e.code == 409:
                    print("[telegram] 409 conflict — another consumer is polling this "
                          "bot token; disabling bridge. Use a dedicated bot.", flush=True)
                    return
                time.sleep(3)
                continue
            except Exception:
                time.sleep(3)
                continue
            for upd in resp.get("result", []):
                self.offset = upd["update_id"] + 1
                self._handle(upd.get("message") or upd.get("edited_message"))

    def _handle(self, msg):
        if not msg:
            return
        chat = str((msg.get("chat") or {}).get("id"))
        frm = str((msg.get("from") or {}).get("id"))
        text = (msg.get("text") or "").strip()
        if self.allow and frm not in self.allow:
            self._send(chat, "⛔ not authorized")
            return
        self._chat_ids.add(chat)
        if not text:
            return
        if text in ("/start", "/help"):
            self._send(chat, "🛰️ Fleet PM online. Ask what needs you, or tell me to "
                             "start work on a project. /reset clears the conversation.")
            return
        if text == "/reset":
            self.router.reset()
            self._send(chat, "Conversation reset.")
            return
        # Stream the PM's work live (tool calls + narration) rather than one final
        # dump. emit() is throttled to stay under Telegram's per-chat rate limit.
        import time
        last = [0.0]

        def emit(kind, body):
            if not body:
                return
            gap = 0.5 - (time.time() - last[0])
            if gap > 0:
                time.sleep(gap)
            last[0] = time.time()
            self._send(chat, ("🔧 " + body) if kind == "tool" else body)

        try:
            if hasattr(self.router, "chat_stream"):
                self.router.chat_stream(text, emit)     # streams; emits the final answer itself
            else:
                out = self.router.chat(text)            # fallback (non-streaming router)
                self._send(chat, out.get("reply", "(no reply)"))
        except Exception as e:
            self._send(chat, f"error: {e}")
