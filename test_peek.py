#!/usr/bin/env python3
"""🔍 peek: a browser's second live terminal (the reviewer pane stacked under
the source it double-checks). Checks the wire shape (PTY bytes ride as
base64 `peekPty` JSON, every other session frame is dropped), the switch /
stop / disconnect lifecycle, and that the pane's size claim is its own —
never the main subscription's. No PTYs, no real sessions."""
import base64
import sys
import threading
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import server as srv  # noqa: E402

FAILS = []


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + (f"  — {detail}" if not ok and detail else ""))
    if not ok:
        FAILS.append(name)


class FakeClient:
    """Stands in for _Client: records what the socket would carry."""
    def __init__(self):
        self.dead = False
        self.cid = None
        self.peek = None
        self.tty_size = None
        self.tty_ts = 0.0
        self.json, self.bytes = [], []

    def send_json(self, obj):
        self.json.append(obj)

    def send_bytes(self, data):
        self.bytes.append(data)


class FakeSession:
    """The real size-ownership code (claim_resize / _set_owner /
    _owner_fallback / unsubscribe) over a stub PTY."""
    claim_resize = srv.ClaudeSession.claim_resize
    _set_owner = srv.ClaudeSession._set_owner
    _owner_fallback = srv.ClaudeSession._owner_fallback
    unsubscribe = srv.ClaudeSession.unsubscribe

    def __init__(self, cid):
        self.cid = cid
        self.clients = set()
        self.clients_lock = threading.Lock()
        self.tty_owner = None
        self.sizes = []

    def subscribe(self, client):
        with self.clients_lock:
            self.clients.add(client)
        client.send_json({"type": "hello", "cid": self.cid})   # what the real one sends first
        client.send_bytes(b"\x1b[Hscreen of " + self.cid.encode())

    def _apply_size(self, cols, rows):
        self.sizes.append((cols, rows))

    def stream(self, data):
        for c in list(self.clients):
            c.send_bytes(data)


mgr = types.SimpleNamespace(sessions={})
mgr.get = lambda cid: mgr.sessions.get(cid)
mgr.peek_client = srv.SessionManager.peek_client.__get__(mgr)
src, rev, rev2 = FakeSession("src"), FakeSession("rev"), FakeSession("rev2")
mgr.sessions = {"src": src, "rev": rev, "rev2": rev2}

c = FakeClient()
c.cid = "src"
src.clients.add(c)
mgr.peek_client(c, "rev")
pk = c.peek
check("peek attaches a _PeekClient to the reviewer's viewers", isinstance(pk, srv._PeekClient) and pk in rev.clients and pk.cid == "rev")
check("…its replay arrives as peekPty JSON (base64), never as main-terminal bytes",
      c.bytes == [] and [m["type"] for m in c.json] == ["peekPty"]
      and base64.b64decode(c.json[0]["d"]) == b"\x1b[Hscreen of rev" and c.json[0]["cid"] == "rev", str(c.json))
check("…the session's other JSON (hello, transcript…) is dropped", not any(m.get("type") == "hello" for m in c.json))
c.json.clear()
rev.stream(b"live")
check("live reviewer bytes → peekPty", [base64.b64decode(m["d"]) for m in c.json] == [b"live"])

# size: the pane's own claim
c.tty_size = (200, 50)
src.claim_resize(c, 200, 50, True)
rev.claim_resize(pk, 200, 18, True)
check("the pane claims the reviewer's PTY at its own geometry", rev.tty_owner is pk and rev.sizes[-1] == (200, 18), str(rev.sizes))
check("…and the main subscription's claim is untouched", c.tty_size == (200, 50) and src.tty_owner is c and src.sizes == [(200, 50)])

# switch
c.json.clear()
mgr.peek_client(c, "rev2")
check("peek at another cid detaches the old one (and its size claim)", pk not in rev.clients and rev.tty_owner is None and pk.dead)
check("…attaches the new one with a fresh replay", c.peek in rev2.clients and base64.b64decode(c.json[-1]["d"]).endswith(b"rev2"))
c.json.clear()
rev.stream(b"stale")
check("bytes from the old reviewer no longer reach the page", c.json == [])

# re-peek the same cid (reconnect) → fresh replay, one viewer
mgr.peek_client(c, "rev2")
check("re-peeking the same cid = one viewer + a fresh replay", len(rev2.clients) == 1 and len(c.json) == 1)

# stop + unknown
mgr.peek_client(c, "")
check("peek '' stops it", c.peek is None and not rev2.clients)
mgr.peek_client(c, "nope")
check("an unknown cid peeks nothing, no raise", c.peek is None)

# disconnect cleans up
mgr.peek_client(c, "rev")
rm = types.SimpleNamespace(all_clients={c}, clients_lock=threading.Lock(), get=mgr.get, peek_client=mgr.peek_client)
srv.SessionManager.remove_client(rm, c)
check("a disconnect drops the peek from the reviewer's viewers", c.peek is None and not rev.clients and c not in src.clients)

# the real _Client starts with no peek
check("_Client starts with peek = None", srv._Client(None).peek is None)

print()
if FAILS:
    print(f"FAILED: {len(FAILS)}"); [print("  -", f) for f in FAILS]; sys.exit(1)
print("all good — 🔍 peek: base64 PTY frames only, own size claim, switch/stop/disconnect")
