#!/usr/bin/env python3
"""More small server fixes from the 2026-11 audit (S8):

  1. reading old transcript lines never moves the context gauge / model
     (only the tailer does);
  2. /upload with a junk Content-Length is a 400, not a traceback;
  3. WS unmasking is one big-int XOR — byte-identical to the per-byte loop;
  4. a ping/pong between the fragments of one message keeps the message;
  5. /config returns only what the page reads; `accounts` has no lastSwitch;
  6. a cached token equal to the stored one is tried once on a 401;
  7. the 🔍 brief prompt no longer says to replace an old file.

No SessionManager is constructed (it would resume live sessions).
"""
import io
import json
import os
import random
import struct
import tempfile
import threading
import types
import urllib.error

import server

FAILED = []


def check(label, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + label
          + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(label)


TMP = tempfile.mkdtemp(prefix="small-server2-")

# ------------------------------------------------------------------ 1
print("reads don't move the gauge:")


def asst(model, tok, text="", tool=False):
    content = ([{"type": "text", "text": text}] if text else []) \
        + ([{"type": "tool_use", "id": "t", "name": "Bash", "input": {}}] if tool else [])
    return json.dumps({"type": "assistant", "message": {
        "model": model, "role": "assistant", "content": content,
        "usage": {"input_tokens": tok}}})


def sess(engine="claude"):
    s = server.ClaudeSession.__new__(server.ClaudeSession)
    s.cid, s.engine = "small-server2-test", engine
    s.transcript_path, s._live_transcript = None, None
    s.model, s.ctx_tokens, s.ctx_window = "", 0, 0
    return s


# An old text answer (small context), padding, then a tool-only line (the
# newest, big context). The backwards walk for "last assistant TEXT" parses
# the old line LAST — on the old code that is what the gauge ended up showing.
path = os.path.join(TMP, "t.jsonl")
with open(path, "w") as f:
    f.write(asst("old-model", 100, text="the old answer") + "\n")
    f.write(json.dumps({"type": "file-history-snapshot", "pad": "x" * 4000}) + "\n")
    f.write(asst("new-model", 9000, tool=True) + "\n")
s = sess()
s.transcript_path = path
s.model, s.ctx_tokens = "new-model", 9000          # where the tailer left them
window = server.READ_WINDOW_BYTES
server.READ_WINDOW_BYTES = 1024                    # the file spans several chunks
try:
    check("the walk finds the old answer", s._last_assistant_text() == "the old answer")
    check("…and the gauge did not go back to it", (s.model, s.ctx_tokens) == ("new-model", 9000),
          f"{s.model} {s.ctx_tokens}")
    s._recent_events(50)
    s._convo_text(path, 10)
    s._convo_text(path, 10, from_end=False)
    check("history / naming reads leave it alone too",
          (s.model, s.ctx_tokens) == ("new-model", 9000), f"{s.model} {s.ctx_tokens}")
finally:
    server.READ_WINDOW_BYTES = window
ev = s._slim_event(asst("live-model", 12345, text="hi"), track=True)
check("the tailer's parse still feeds it",
      ev == {"role": "assistant", "text": "hi"} and (s.model, s.ctx_tokens) == ("live-model", 12345),
      f"{ev} {s.model} {s.ctx_tokens}")
check("…but a <synthetic> model never does",
      s._slim_event(asst("<synthetic>", 0, text="x"), track=True) and s.model == "live-model")

c = sess("codex")
tc = json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {
    "last_token_usage": {"input_tokens": 777}, "model_context_window": 258000}}})
meta = json.dumps({"type": "turn_context", "payload": {"type": "turn_context", "model": "gpt-x"}})
c._slim_event(tc)
c._slim_event(meta)
check("codex: a read sets nothing", (c.model, c.ctx_tokens, c.ctx_window) == ("", 0, 0),
      f"{c.model} {c.ctx_tokens} {c.ctx_window}")
c._slim_event(tc, track=True)
c._slim_event(meta, track=True)
check("codex: the tailer sets all three", (c.model, c.ctx_tokens, c.ctx_window) == ("gpt-x", 777, 258000),
      f"{c.model} {c.ctx_tokens} {c.ctx_window}")

# the tailer itself (the real thread) still harvests
t = sess()
t.alive, t._live_transcript, t._launched_at = True, path, 0
t._to_subscribers_json = lambda m: None
t._on_login_expired = t._on_limit_transcript = lambda *a: None
th = threading.Thread(target=t._tail_transcript, daemon=True)
th.start()
import time
deadline = time.time() + 5
while t.ctx_tokens != 9000 and time.time() < deadline:
    time.sleep(0.05)
check("the tailer thread harvests model + tokens", (t.model, t.ctx_tokens) == ("new-model", 9000),
      f"{t.model} {t.ctx_tokens}")
t.alive = False
th.join(2)

# ------------------------------------------------------------------ 2
print("\n/upload Content-Length:")


def upload(length):
    errs = []
    h = types.SimpleNamespace(
        headers={"Content-Length": length}, _token_ok=lambda: True,
        send_error=lambda code, msg="": errs.append(code))
    try:
        server.Handler._handle_upload(h)
    except Exception as e:
        return repr(e)
    return errs


check("junk → 400", upload("abc") == [400], str(upload("abc")))
check("empty → 413", upload("") == [413], str(upload("")))
check("zero → 413 (as before)", upload("0") == [413], str(upload("0")))

# ------------------------------------------------------------------ 3
print("\nWS unmask:")


def old_unmask(chunk, mask):
    return bytes(chunk[i] ^ mask[i % 4] for i in range(len(chunk)))


rnd = random.Random(1109)
bad = []
for n in list(range(0, 70)) + [125, 126, 127, 1023, 4096, 65535, 65537] \
        + [rnd.randrange(0, 20000) for _ in range(200)]:
    for mask in (bytes(rnd.randrange(256) for _ in range(4)), b"\0\0\0\0",
                 b"\xff\xff\xff\xff", b"\0\0\0\x01", b"\x80\0\0\0"):
        chunk = bytes(rnd.randrange(256) for _ in range(n)) if n % 3 else b"\0" * n
        if server._ws_unmask(chunk, mask) != old_unmask(chunk, mask):
            bad.append((n, mask))
check("identical to the per-byte loop for every length and mask", not bad, str(bad[:5]))


def frame(payload, opcode=0x1, fin=True, mask=b"\x11\x22\x33\x44"):
    b0, n = (0x80 if fin else 0) | opcode, len(payload)
    m = 0x80 if mask else 0
    if n < 126:
        h = bytes([b0, m | n])
    elif n < 65536:
        h = bytes([b0, m | 126]) + struct.pack(">H", n)
    else:
        h = bytes([b0, m | 127]) + struct.pack(">Q", n)
    if not mask:
        return h + payload
    return h + mask + old_unmask(payload, mask)


for n in (0, 1, 3, 4, 5, 9, 125, 126, 70000):
    body = bytes(rnd.randrange(256) for _ in range(n))
    got = server.ws_read_message(io.BytesIO(frame(body, opcode=0x2)))
    if got != (0x2, body):
        check(f"masked frame of {n} bytes round-trips", False)
        break
else:
    check("masked frames round-trip through ws_read_message", True)
check("a truncated mask is a dead socket, not a crash",
      server.ws_read_message(io.BytesIO(bytes([0x81, 0x83]) + b"\x01\x02")) is None)

# ------------------------------------------------------------------ 4
print("\ncontrol frames between fragments:")
stream = (frame(b"hel", fin=False) + frame(b"P", opcode=0x9) + frame(b"", opcode=0xA)
          + frame(b"lo", opcode=0x0) + frame(b"next"))
r = io.BytesIO(stream)
pings = []
got = server.ws_read_message(r)
check("a ping + pong mid-message keep the message (no callback given)",
      got == (0x1, b"hello"), repr(got))
check("…and the next message is intact", server.ws_read_message(r) == (0x1, b"next"))
try:
    got = server.ws_read_message(io.BytesIO(stream), on_ping=pings.append)
except TypeError as e:
    got = repr(e)
check("the mid-message ping is handed to on_ping", got == (0x1, b"hello") and pings == [b"P"],
      f"{got!r} {pings}")
check("a ping BETWEEN messages is still returned to the caller",
      server.ws_read_message(io.BytesIO(frame(b"P", opcode=0x9))) == ("ping", b"P"))
check("a pong between messages too",
      server.ws_read_message(io.BytesIO(frame(b"", opcode=0xA))) == ("pong", b""))
check("a close mid-message still closes",
      server.ws_read_message(io.BytesIO(frame(b"hel", fin=False) + frame(b"", opcode=0x8)))
      == ("close", b""))

# ------------------------------------------------------------------ 5
print("\ntrimmed frames:")
sent = []
h = types.SimpleNamespace(path="/config?t=x", _origin_ok=lambda: True, _token_ok=lambda: True,
                          _is_ws_upgrade=lambda: False, _serve_json=sent.append,
                          send_error=lambda *a: sent.append(a))
server.Handler.do_GET(h)
check("/config is exactly {tts, stt}", len(sent) == 1 and isinstance(sent[0], dict)
      and sorted(sent[0]) == ["stt", "tts"], str(sent))
h2 = types.SimpleNamespace(path="/config", _origin_ok=lambda: True, _token_ok=lambda: False,
                           _is_ws_upgrade=lambda: False, _serve_json=sent.append,
                           send_error=lambda *a: sent.append(a))
server.Handler.do_GET(h2)
check("…and still token-gated", sent[-1][0] == 403, str(sent[-1]))

codex_meta = server.codex_usage_meta
server.codex_usage_meta = lambda: None
try:
    m = types.SimpleNamespace(lock=threading.RLock(), active_account="a", last_switch_at=5.0,
                              _best_account=lambda: "a", _ordered_accounts=lambda: [])
    fr = server.SessionManager.accounts_meta(m)
finally:
    server.codex_usage_meta = codex_meta
check("accounts frame: no lastSwitch, the read fields stay",
      "lastSwitch" not in fr and all(k in fr for k in ("type", "active", "auto", "best", "accounts", "codex")),
      str(sorted(fr)))

# ------------------------------------------------------------------ 6
print("\nusage poll on a 401:")
calls = []


def refused(req, timeout=0):
    calls.append(req.get_header("Authorization"))
    raise urllib.error.HTTPError(req.full_url, 401, "no", {}, None)


keep = (server._dir_token, server._read_oauth_creds_ex, server.urllib.request.urlopen)
server._dir_token = lambda d: None
try:
    server.urllib.request.urlopen = refused
    for label, cached, want in (("cache == store → ONE refused call", "tok-A", 1),
                                ("cache != store → both are tried", "tok-B", 2),
                                ("no cache → one", None, 1)):
        calls.clear()
        # separate str objects, as a keychain read and a cache really are
        server._read_oauth_creds_ex = lambda d: (
            {"claudeAiOauth": {"accessToken": "".join(["tok-", "A"]), "refreshToken": "r"}}, True)
        cache = {"access": cached} if cached else {}
        got = server._fetch_usage(TMP, cache, allow_refresh=False)
        check(label, len(calls) == want and got is None, f"{calls} → {got!r}")
        if cached:
            check("  …and the refused cache entry is dropped", "access" not in cache, str(cache))
finally:
    server._dir_token, server._read_oauth_creds_ex, server.urllib.request.urlopen = keep

# ------------------------------------------------------------------ 7
print("\n🔍 brief prompt:")
p = server.CHECK_BRIEF_PROMPT.format(reviewer="codex", file="REVIEW-x.md")
check("does not ask to replace an old file (one new file per tap)",
      "replace" not in p and "REVIEW-x.md at the repo root: what you were asked" in p, p[:200])

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {FAILED}")
    raise SystemExit(1)
print("all green")
