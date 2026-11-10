#!/usr/bin/env python3
"""Three server fixes from the 2026-11 audit (S3, S4, S5):

  S3  "is there a conversation to resume / check?" is the ENGINE's question —
      a codex rollout has no claude-shaped lines, so the old substring test
      said "nothing yet" for every codex session.
  S4  a resumed session's transcript opens full of old turns; the tailer must
      not stream them to viewers again as live.
  S5  a send to a session still parked by the boot stagger waits for its TUI,
      and two callers racing the wake never leave one writing to no PTY.

No SessionManager is constructed (it would resume live sessions).
"""
import datetime
import json
import os
import tempfile
import threading
import time
import types

import server

FAILED = []


def check(label, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + label
          + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(label)


NOW = time.time()
TMP = tempfile.mkdtemp(prefix="wake-send-")


def iso(seconds):
    return datetime.datetime.fromtimestamp(
        NOW + seconds, datetime.timezone.utc).isoformat()


def bare(engine="claude"):
    s = server.ClaudeSession.__new__(server.ClaudeSession)
    s.cid, s.engine = "wake-send-test", engine
    s.transcript_path = s._live_transcript = None
    s.model, s.ctx_tokens, s.ctx_window = "", 0, 0
    return s


def write(name, lines):
    path = os.path.join(TMP, name)
    with open(path, "w") as f:
        for ln in lines:
            f.write(json.dumps(ln, separators=(",", ":")) + "\n")
    return path


# ---------------------------------------------------------------- S3
print("S3 — has-conversation is engine-aware:")
CODEX_HEAD = [
    {"timestamp": iso(-60), "type": "session_meta",
     "payload": {"id": "abc", "cwd": "/tmp", "model": "gpt-5"}},
    {"timestamp": iso(-59), "type": "response_item", "payload": {
        "type": "message", "role": "developer",
        "content": [{"type": "input_text", "text": "skills preamble"}]}},
]
CODEX_TURN = [
    {"timestamp": iso(-50), "type": "response_item", "payload": {
        "type": "message", "role": "user",
        "content": [{"type": "input_text", "text": "fix the bug"}]}},
    {"timestamp": iso(-40), "type": "response_item", "payload": {
        "type": "message", "role": "assistant",
        "content": [{"type": "output_text", "text": "done"}]}},
]
cx = bare("codex")
cx.transcript_path = write("codex-empty.jsonl", CODEX_HEAD)
check("codex: header + preamble only is NOT a conversation",
      cx._has_conversation() is False)
cx.transcript_path = write("codex-turn.jsonl", CODEX_HEAD + CODEX_TURN)
check("codex: a real turn IS a conversation", cx._has_conversation() is True)

cl = bare("claude")
cl.transcript_path = write("claude-empty.jsonl", [
    {"type": "permission-mode", "permissionMode": "default"},
    {"type": "file-history-snapshot", "snapshot": {}}])
check("claude: mode/snapshot lines only is NOT a conversation",
      cl._has_conversation() is False)
cl.transcript_path = write("claude-turn.jsonl", [
    {"type": "permission-mode", "permissionMode": "default"},
    {"type": "user", "timestamp": iso(-30),
     "message": {"role": "user", "content": "hello"}}])
check("claude: a user line IS a conversation", cl._has_conversation() is True)
cl.transcript_path = os.path.join(TMP, "missing.jsonl")
check("a missing file is not a conversation", cl._has_conversation() is False)


# ---------------------------------------------------------------- S4
print("\nS4 — a resumed transcript is not re-streamed as live:")


def line(role, text, seconds):
    return {"type": role, "timestamp": iso(seconds), "message": {
        "role": role, "model": "claude-x",
        "content": [{"type": "text", "text": text}]}}


path = write("resumed.jsonl", [line("user", "old question", -7200),
                               line("assistant", "old answer", -7190)])
t = bare("claude")
t.alive = True
t._launched_at = NOW
t._live_transcript = path
sent = []
t._to_subscribers_json = lambda msg: sent.append(msg["event"].get("text"))
t._on_login_expired = lambda: None
t._on_limit_transcript = lambda raw: None
th = threading.Thread(target=t._tail_transcript, daemon=True)
th.start()
time.sleep(0.7)
check("old turns are not broadcast", sent == [], f"sent={sent}")
with open(path, "a") as f:
    f.write(json.dumps(line("user", "new question", 1)) + "\n")
    f.write(json.dumps(line("assistant", "new answer", 2)) + "\n")
deadline = time.time() + 5
while len(sent) < 2 and time.time() < deadline:
    time.sleep(0.05)
check("turns from this launch are broadcast, once each, in order",
      sent == ["new question", "new answer"], f"sent={sent}")
t.alive = False
th.join(2)

fresh_path = write("fresh.jsonl", [line("user", "first", 1), line("assistant", "reply", 2)])
t2 = bare("claude")
t2.alive, t2._launched_at, t2._live_transcript = True, NOW, fresh_path
sent2 = []
t2._to_subscribers_json = lambda msg: sent2.append(msg["event"].get("text"))
t2._on_login_expired = lambda: None
t2._on_limit_transcript = lambda raw: None
th2 = threading.Thread(target=t2._tail_transcript, daemon=True)
th2.start()
deadline = time.time() + 5
while len(sent2) < 2 and time.time() < deadline:
    time.sleep(0.05)
t2.alive = False
th2.join(2)
check("a fresh session streams everything", sent2 == ["first", "reply"], f"sent={sent2}")


# ---------------------------------------------------------------- S5
print("\nS5 — waking a parked session:")
w = bare("claude")
w.starting, w._waking = True, False
w._start_lock = threading.Lock()
w.master_fd = None
order = []


def slow_start():
    order.append("start")
    time.sleep(0.4)
    w.master_fd = 99
    order.append("started")


w.start = slow_start
seen_fd = []


def racer():
    w.ensure_started()
    seen_fd.append(w.master_fd)


a, b = threading.Thread(target=racer), threading.Thread(target=racer)
a.start(); time.sleep(0.05); b.start(); a.join(); b.join()
check("start() runs exactly once", order == ["start", "started"], f"order={order}")
check("the second caller sees a PTY, never None", seen_fd == [99, 99], f"fds={seen_fd}")
check("a later call is a no-op", w.ensure_started() is False)

log = []
w.wait_ready = lambda timeout=0: (log.append("wait"), True)[1]
w.write = lambda data: log.append("write")
w._send_watchdog = lambda *a: None
w.hook_count = 0
old_sleep = server.time.sleep
try:
    server.time.sleep = lambda _s: None
    w.send_message("hello")
    first = list(log)
    w.send_message("again")
finally:
    server.time.sleep = old_sleep
check("first send after the wake waits for the TUI before typing",
      first[:2] == ["wait", "write"], f"log={first}")
check("only that first send waits", log.count("wait") == 1, f"log={log}")

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {FAILED}")
    raise SystemExit(1)
print("all green")
