#!/usr/bin/env python3
"""One malformed transcript line must never kill a subscriber's replay.

2026-08-27: a send queued while claude was busy WITH AN IMAGE attached writes
its queued_command attachment's `prompt` as a content-block LIST, not a
string. _strip_noise did re.sub on it → TypeError → the exception rode up
through _replay_history → subscribe → the whole WS handler died. Every
subscribe to that session killed the client's socket: the harness read as
totally bricked (found live on heart, transcript 47ef2dde…).

Three layers, tested independently so any one regressing still fails loudly:
  1. _strip_noise refuses non-strings instead of raising.
  2. the queued_command branch extracts text from a list prompt.
  3. _slim_event (the choke point for replay, tailer, tail_events, digests)
     never raises even if an engine parser blows up.
"""
import json
import types

import server

FAILS = []


def check(name, ok, detail=""):
    print(("  ok  " if ok else "  FAIL") + " " + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


print("poison transcript lines")

# 1. _strip_noise never raises on non-strings
check("1. _strip_noise on a list returns ''", server._strip_noise([{"type": "text", "text": "hi"}]) == "")
check("1b. _strip_noise on None returns ''", server._strip_noise(None) == "")
check("1c. _strip_noise still strips strings",
      server._strip_noise("<system-reminder>x</system-reminder>yo") == "yo")

# 2. the queued_command attachment with a content-block list prompt (the real
#    on-disk shape from heart) yields the user text instead of crashing
fake = types.SimpleNamespace(cid="deadbeef" * 4)
line = json.dumps({"type": "attachment", "attachment": {
    "type": "queued_command",
    "prompt": [{"type": "text", "text": "move it left [Image #1]"},
               {"type": "image", "source": {"type": "base64", "data": "AAAA"}}]}})
ev = server.ClaudeSession._slim_event_claude(fake, line)
check("2. list-prompt queued_command → user event with its text",
      ev == {"role": "user", "text": "move it left [Image #1]"}, f"ev={ev}")

line2 = json.dumps({"type": "attachment", "attachment": {
    "type": "queued_command", "prompt": "plain old string"}})
ev2 = server.ClaudeSession._slim_event_claude(fake, line2)
check("2b. string prompt unchanged",
      ev2 == {"role": "user", "text": "plain old string"}, f"ev={ev2}")

# 3. _slim_event swallows ANY engine parser explosion
class BoomEng:
    def slim_event(self, s, line):
        raise RuntimeError("parser blew up")

fake3 = types.SimpleNamespace(cid="deadbeef" * 4, eng=BoomEng())
try:
    out = server.ClaudeSession._slim_event(fake3, "whatever")
    check("3. _slim_event returns None instead of raising", out is None)
except Exception as e:
    check("3. _slim_event returns None instead of raising", False, repr(e))

if FAILS:
    print(f"\n{len(FAILS)} FAILED")
    raise SystemExit(1)
print("\nall green")
