#!/usr/bin/env python3
"""test_send_backlog.py — a peer that stopped reading is dropped BEFORE the
write, never blocked on (relay.Conn._backlogged).

2026-09-18: one browser tab held 2.8 MB unread at the relay for ten hours.
Every reply routed to it blocked the sending worker's reader thread for up to
the 90 s socket timeout, the reaper dropped that worker as stale, and every
page on it rebuilt its channel every 10 s all day (e2e-timeout, black tty).
Exits non-zero on any failure.
"""
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import relay  # noqa: E402

FAILED = []


def check(name, ok):
    print(("ok   " if ok else "FAIL ") + name)
    if not ok:
        FAILED.append(name)


class FakeSock:
    def fileno(self):
        return 0

    def shutdown(self, *a):
        pass

    def close(self):
        pass


class Sink:
    """A wfile whose bytes survive close() (Conn._mark_dead closes it)."""
    def __init__(self):
        self.buf = b""

    def write(self, b):
        self.buf += b

    def flush(self):
        pass

    def close(self):
        pass

    def getvalue(self):
        return self.buf


def conn(sock=FakeSock()):
    return relay.Conn(Sink(), "mobile", "m1", sock=sock)


def with_outq(n, fn):
    orig = relay._outq_bytes
    relay._outq_bytes = lambda sock: n
    try:
        return fn()
    finally:
        relay._outq_bytes = orig


# 1. over the cap → dead, nothing written
c = conn()
with_outq(relay.SEND_BACKLOG_MAX + 1, lambda: c.send_json({"type": "pong"}))
check("backlogged peer is marked dead", c.dead)
check("nothing is written to a dead reader", c.wfile.getvalue() == b"")

# 2. at the cap → still written to
c = conn()
with_outq(relay.SEND_BACKLOG_MAX, lambda: c.send_json({"type": "pong"}))
check("draining peer stays alive", not c.dead)
check("draining peer gets the frame", bool(c.wfile.getvalue()))

# 3. binary + ping take the same gate
for label, send in (("send_binary", lambda c: c.send_binary(b"\x00")),
                    ("ping", lambda c: c.ping())):
    c = conn()
    with_outq(relay.SEND_BACKLOG_MAX + 1, lambda: send(c))
    check(f"{label} drops a backlogged peer", c.dead and c.wfile.getvalue() == b"")

# 3b. an upload in flight is credited: nine ~1 MB photos at once used to trip
# the cap and drop the worker's whole link (2026-09-30, every upload 502'd)
c = conn()
c.bulk_credit = 9 * relay.SEND_BACKLOG_MAX
with_outq(9 * relay.SEND_BACKLOG_MAX, lambda: c.send_json({"type": "pong"}))
check("upload backlog within its credit keeps the worker", not c.dead)
c.bulk_credit = 0
with_outq(relay.SEND_BACKLOG_MAX + 1, lambda: c.send_json({"type": "pong"}))
check("credit released → a stalled worker still trips", c.dead)

# 3c. the /upload handler credits its frame for exactly the wait, then releases
src = Path(relay.__file__).read_text()
up = src[src.index('uid, ev = RELAY.new_upload()'):src.index('slot = RELAY.take_upload(uid)')]
check("/upload credits the frame before sending it",
      up.index("bulk_credit += len(frame)") < up.index("send_text(frame)"))
check("/upload releases the credit in a finally",
      "finally:" in up and "bulk_credit -= len(frame)" in up.split("finally:")[1])

# 4. no sock → no-op
c = conn(sock=None)
c.send_json({"type": "pong"})
check("no sock: check never fires", not c.dead and bool(c.wfile.getvalue()))

# 5. the real ioctl on an idle socket reads 0 (and never raises)
a, b = socket.socketpair()
try:
    check("TIOCOUTQ on an idle socket is 0", relay._outq_bytes(a) == 0)
finally:
    a.close(); b.close()

if FAILED:
    print(f"\n{len(FAILED)} failed: {FAILED}")
    sys.exit(1)
print("\nall passed")
