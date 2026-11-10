#!/usr/bin/env python3
"""Transcript reads are bounded (audit S6) and say the same thing as before.

Every reader that wants "the recent conversation" used to read and parse the
whole file — on each tab switch (twice), each Stop, each controller poll. They
now read the file's ends. This compares each one against a whole-file
reference on a multi-MB transcript and checks how much was actually read.

No SessionManager is constructed (it would resume live sessions).
"""
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


TMP = tempfile.mkdtemp(prefix="tx-reads-")


def line(role, text):
    return json.dumps({"type": role, "message": {
        "role": role, "model": "claude-x",
        "content": [{"type": "text", "text": text}]}}, ensure_ascii=False)


def noise(i):          # a fat non-conversation line, like a snapshot
    return json.dumps({"type": "file-history-snapshot", "n": i, "pad": "x" * 3000})


def build(name, turns):
    path = os.path.join(TMP, name)
    with open(path, "w") as f:
        for i in range(turns):
            f.write(line("user", f"question {i} — é ✓ " + "q" * 40) + "\n")
            f.write(noise(i) + "\n")
            f.write(line("assistant", f"answer {i} " + "a" * 200) + "\n")
    return path


def sess(path):
    s = server.ClaudeSession.__new__(server.ClaudeSession)
    s.cid, s.engine = "tx-reads-test", "claude"
    s.transcript_path, s._live_transcript = path, None
    s.model, s.ctx_tokens, s.ctx_window = "", 0, 0
    s.tty_rows = 30
    return s


def ref_events(s):
    return [e for e in (s._slim_event(l) for l in open(s.transcript_path).read().split("\n")) if e]


def ref_naming(s, cap=3500, head_frac=0.45):
    chunks = []
    for ev in ref_events(s):
        if ev.get("role") == "user" and ev.get("text"):
            chunks.append("User: " + ev["text"])
        elif ev.get("role") == "assistant" and ev.get("text"):
            chunks.append("Claude: " + ev["text"])
    text = "\n".join(chunks)
    if len(text) <= cap:
        return text
    if not head_frac:
        return text[-cap:]
    head = int(cap * head_frac)
    return text[:head] + "\n…\n" + text[-(cap - head - 3):]


class Client:
    def __init__(self):
        self.json, self.bytes = [], []
    def send_json(self, m): self.json.append(m)
    def send_bytes(self, b): self.bytes.append(b)


read = []
real_window = server.ClaudeSession._read_window


def counting(path, nbytes, from_end=True):
    lines, whole = real_window(path, nbytes, from_end)
    read.append(sum(len(l) for l in lines))
    return lines, whole


server.ClaudeSession._read_window = staticmethod(counting)

for label, turns in (("big (multi-MB)", 2500), ("small", 12), ("one turn", 1)):
    print(f"{label}:")
    path = build(f"t{turns}.jsonl", turns)
    size = os.path.getsize(path)
    s = sess(path)
    ref = ref_events(s)

    read.clear()
    c = Client()
    s._replay_history(c)
    check("replay sends the same last 150 events",
          [m["event"] for m in c.json] == ref[-150:] and all(m["history"] for m in c.json))
    if size > 2_000_000:
        check("…reading a fraction of the file", sum(read) < size / 4,
              f"read {sum(read)} of {size}")

    check("tail_events matches", s.tail_events(30, 400)
          == [dict(e, text=e["text"][:400]) for e in ref[-30:]])
    check("last assistant text matches", s._last_assistant_text()
          == next(e["text"] for e in reversed(ref) if e["role"] == "assistant"))
    check("seed scrollback matches the whole-file render",
          s._history_seed_bytes() == s._history_seed_bytes(events=ref))
    for cap, hf in ((3500, 0.45), (3500, 0), (9000, 0.45)):
        read.clear()
        got = s._transcript_text_for_naming(cap=cap, head_frac=hf)
        check(f"naming text matches (cap={cap}, head_frac={hf})",
              got == ref_naming(s, cap, hf), f"{len(got)} vs {len(ref_naming(s, cap, hf))}")
        if size > 2_000_000:
            check("…reading a fraction of the file", sum(read) < size / 4,
                  f"read {sum(read)} of {size}")
    print()

print("edges:")
s = sess(os.path.join(TMP, "missing.jsonl"))
check("a missing file is empty, not an error",
      s._recent_events(10) == [] and s._last_assistant_text() == ""
      and s._transcript_text_for_naming() == "" and s._history_seed_bytes() == b"")
path = os.path.join(TMP, "noise.jsonl")
with open(path, "w") as f:
    for i in range(400):
        f.write(noise(i) + "\n")
    f.write(line("assistant", "the only answer") + "\n")
    for i in range(400):
        f.write(noise(i) + "\n")
s = sess(path)
check("an answer buried under >1 MB of noise is still found",
      s._last_assistant_text() == "the only answer")
path = os.path.join(TMP, "fatline.jsonl")
with open(path, "w") as f:
    f.write(line("user", "start") + "\n")
    f.write(line("assistant", "z" * 700_000) + "\n")      # one line bigger than the window
s = sess(path)
check("a single line larger than the read window survives",
      [e["role"] for e in s._recent_events(5)] == ["user", "assistant"])

print("\nhooks (audit S7):")
calls = []
m = types.SimpleNamespace(
    lock=threading.RLock(), sessions={}, projects={}, accounts={},
    broadcast_all=lambda msg: None, save_registry=lambda: calls.append("save"),
    broadcast_projects=lambda: calls.append("projects"),
    broadcast_sessions=lambda: calls.append("now"))
m.broadcast_sessions_soon = lambda: calls.append("soon")
m.touch_project = lambda pid, save=True: calls.append(f"touch save={save}")
h = sess(None)
h.manager, h.pid = m, "p"
h.hook_count, h.busy, h.waiting, h.last_tool, h.last_active = 0, False, False, None, 0
h._follow_session = lambda obj: None
h.on_hook({"hook_event_name": "PreToolUse", "tool_name": "Bash"})
check("a tool hook that changes status broadcasts at once", calls == ["now"], f"{calls}")
calls.clear()
h.on_hook({"hook_event_name": "PostToolUse", "tool_name": "Bash"})
h.on_hook({"hook_event_name": "PreToolUse", "tool_name": "Bash"})
check("tool hooks that change nothing are coalesced", calls == ["soon", "soon"], f"{calls}")

mm = server.SessionManager.__new__(server.SessionManager)
mm.lock, fired = threading.RLock(), []
mm.broadcast_sessions = lambda: fired.append(time.time())
for _ in range(50):
    mm.broadcast_sessions_soon(0.15)
time.sleep(0.5)
check("50 coalesced calls → exactly one broadcast, and it does fire", len(fired) == 1, f"{len(fired)}")
mm.broadcast_sessions_soon(0.05)
time.sleep(0.3)
check("the next call after that fires again", len(fired) == 2, f"{len(fired)}")

P = types.SimpleNamespace
mm.sessions = {i: P(pid=p, busy=b, waiting=w) for i, (p, b, w) in enumerate(
    [("a", True, False), ("a", True, True), ("a", False, False), ("b", False, False)])}
mm._warmth = lambda p: 0
mm._ordered_projects = lambda: [P(pid=x, meta=lambda t, b, w, last_touched=0, x=x: (x, t, b, w))
                                for x in ("a", "b", "c")]
check("project counts in one pass are right",
      mm.projects_meta() == [("a", 3, 1, 1), ("b", 1, 0, 0), ("c", 0, 0, 0)],
      f"{mm.projects_meta()}")
check("…and agree with session_counts",
      [mm.session_counts(x) for x in "abc"] == [(3, 1, 1), (1, 0, 0), (0, 0, 0)])

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {FAILED}")
    raise SystemExit(1)
print("all green")
