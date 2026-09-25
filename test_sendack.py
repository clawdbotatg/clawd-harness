#!/usr/bin/env python3
"""✓ send receipts (2026-09-25): a `send` frame carrying an `id` is answered
to its sender with `sendAck` the moment it arrives — before delivery into the
CLI, which can hold it for a minute+ (codex mid-step). The page turns that into
"✓ on <machine> · waiting" (tools/sendackprobe.mjs).

  1. known session + id → {type:"sendAck", id, cid}, and the prompt is still
     delivered;
  2. unknown session + id → {type:"sendAck", id, error:"no session"};
  3. no id (controller / old pages) → no reply at all, delivery unchanged.

Run: python3 test_sendack.py
"""
import sys
import time

import server

FAILS = []


def check(name, ok, detail=""):
    print(f"  {'✓' if ok else '✗ FAIL'} {name}" + ("" if ok or not detail else f" — {detail}"))
    if not ok:
        FAILS.append(name)


class Client:
    cid = None
    def __init__(self): self.out = []
    def send_json(self, o): self.out.append(o)


class Sess:
    def __init__(self, cid): self.cid, self.auto_tldr_armed = cid, False
    def bump_owner(self, client): pass


class Mgr:
    def __init__(self): self.sessions, self.sent = {"c1": Sess("c1")}, []
    def get(self, cid): return self.sessions.get(cid)
    def send_prompt(self, cid, text, via=""): self.sent.append((cid, text))


server.MGR = Mgr()
server.log_prompt = lambda *a, **k: None
dispatch = lambda c, f: server.Handler._dispatch(None, c, f)

def delivered(n):
    for _ in range(50):
        if len(server.MGR.sent) >= n: return True
        time.sleep(0.02)
    return False

c = Client()
dispatch(c, {"type": "send", "cid": "c1", "text": "hi", "id": "abc"})
check("1. receipt names the id + cid", c.out == [{"type": "sendAck", "id": "abc", "cid": "c1"}], c.out)
check("1. prompt still delivered", delivered(1) and server.MGR.sent[0] == ("c1", "hi"), server.MGR.sent)

c = Client()
dispatch(c, {"type": "send", "cid": "nope", "text": "hi", "id": "x2"})
check("2. unknown session → error receipt", c.out == [{"type": "sendAck", "id": "x2", "error": "no session"}], c.out)

c = Client()
dispatch(c, {"type": "send", "cid": "c1", "text": "again"})
check("3. no id → no reply", c.out == [], c.out)
check("3. …and still delivered", delivered(2), server.MGR.sent)

print("test_sendack: " + ("FAIL" if FAILS else "OK"))
sys.exit(1 if FAILS else 0)
