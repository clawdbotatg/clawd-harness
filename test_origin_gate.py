#!/usr/bin/env python3
"""The no-token loopback harness is same-origin only (2026-09-09 review).

`origin_allowed(origin, host, bind)` guards the /ws upgrade and every POST:
a browser's Origin must match Host, Host must be loopback on a loopback bind,
and clients without an Origin (worker, bin/, curl) pass. Also pins the
browser→harness frame cap in ws_read_message.

Sandboxed import of server.py (copy in a temp dir) — never the live tree.
"""
import importlib.util
import io
import shutil
import struct
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent
FAILS = []


def check(name, ok):
    print(("ok   " if ok else "FAIL ") + name)
    if not ok:
        FAILS.append(name)


def load_server_sandboxed():
    tmp = Path(tempfile.mkdtemp(prefix="origin-test-"))
    shutil.copy(REPO / "server.py", tmp / "server.py")
    spec = importlib.util.spec_from_file_location("server_sandbox_origin", tmp / "server.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["server_sandbox_origin"] = mod
    spec.loader.exec_module(mod)
    return mod


srv = load_server_sandboxed()
ok = srv.origin_allowed
LB = "127.0.0.1"

# browsers on this box
check("same-origin 127.0.0.1", ok("http://127.0.0.1:8787", "127.0.0.1:8787", LB))
check("same-origin localhost", ok("http://localhost:8787", "localhost:8787", LB))
check("same-origin [::1]", ok("http://[::1]:8787", "[::1]:8787", LB))
check("case-insensitive host", ok("http://LocalHost:8787", "localhost:8787", LB))
# non-browser clients (no Origin)
check("no Origin, loopback Host passes", ok(None, "127.0.0.1:8787", LB))
check("empty Origin passes", ok("", "localhost:8787", LB))
# the attack: any foreign page
check("cross-site http origin refused", not ok("http://attacker.invalid", "127.0.0.1:8787", LB))
check("cross-site https origin refused", not ok("https://evil.example", "127.0.0.1:8787", LB))
check("origin with different port refused", not ok("http://127.0.0.1:9999", "127.0.0.1:8787", LB))
check("origin 'null' refused", not ok("null", "127.0.0.1:8787", LB))
check("garbage origin refused", not ok("::::", "127.0.0.1:8787", LB))
check("non-http scheme refused", not ok("chrome-extension://abc", "127.0.0.1:8787", LB))
# DNS rebinding: resolves to 127.0.0.1 but the browser sends the evil Host
check("rebinding Host refused on loopback bind",
      not ok("http://attacker.invalid:8787", "attacker.invalid:8787", LB))
check("rebinding Host refused even without Origin", not ok(None, "attacker.invalid:8787", LB))
check("missing Host refused", not ok(None, "", LB))
# LAN bind: Host may be the LAN ip (token guards), Origin must still match
check("LAN bind: LAN host passes", ok("http://192.168.1.5:8787", "192.168.1.5:8787", "0.0.0.0"))
check("LAN bind: cross-site still refused",
      not ok("http://attacker.invalid", "192.168.1.5:8787", "0.0.0.0"))

# frame cap
def frame(n, fin=True, opcode=0x1):
    b0 = (0x80 if fin else 0) | opcode
    if n < 126:
        h = bytes([b0, n])
    elif n < 65536:
        h = bytes([b0, 126]) + struct.pack(">H", n)
    else:
        h = bytes([b0, 127]) + struct.pack(">Q", n)
    return h + b"a" * n

r = srv.ws_read_message(io.BytesIO(frame(100)))
check("small frame read", r == (0x1, b"a" * 100))
r = srv.ws_read_message(io.BytesIO(frame(10)), max_len=5)
check("declared length over cap → None", r is None)
hdr = bytes([0x81, 127]) + struct.pack(">Q", 1 << 62)
check("absurd declared length → None (no read)", srv.ws_read_message(io.BytesIO(hdr)) is None)
frags = frame(6, fin=False) + frame(6, fin=True, opcode=0x0)
check("fragments over cap → None", srv.ws_read_message(io.BytesIO(frags), max_len=10) is None)
check("fragments under cap reassemble",
      srv.ws_read_message(io.BytesIO(frags), max_len=12) == (0x1, b"a" * 12))
check("default cap is 8 MiB", srv.MAX_WS_MESSAGE == 8 * 1024 * 1024)

# Exercise handlers over HTTP: helper-only tests missed the GET route omission.
import http.client
import threading
from types import SimpleNamespace
from controller.chat_server import make_handler, ThreadingHTTPServer

def route_checks(handler, server_class, label):
    server = server_class(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    try:
        for method, path in (("GET", "/config"), ("GET", "/pm/api/tools"),
                             ("GET", "/"), ("POST", "/api/tool")):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            c.request(method, path, headers={"Host": "attacker.invalid", "Content-Length": "0"})
            r = c.getresponse()
            check(f"{label} rebinding {method} {path} refused", r.status == 403)
            r.read(); c.close()
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        c.request("GET", "/", headers={"Origin": "https://attacker.invalid"})
        r = c.getresponse()
        check(f"{label} foreign origin refused", r.status == 403)
        r.read(); c.close()
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        c.request("GET", "/missing")
        r = c.getresponse()
        check(f"{label} local proxy allowed", r.status == 404)
        check(f"{label} no wildcard CORS", r.getheader("Access-Control-Allow-Origin") != "*")
        r.read(); c.close()
    finally:
        server.shutdown(); server.server_close()

route_checks(srv.Handler, srv.ThreadingHTTPServer, "harness")
route_checks(make_handler(None, None, SimpleNamespace(autonomy="readonly"), lambda: "test"),
             ThreadingHTTPServer, "controller")
# The public manifest must not disclose the bearer credential on a LAN bind.
server = srv.ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
try:
    srv.AUTH_REQUIRED = True
    c = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=3)
    c.request("GET", "/manifest.webmanifest")
    r = c.getresponse(); body = r.read().decode(); c.close()
    check("public manifest never publishes the token", r.status == 200 and srv.TOKEN not in body
          and __import__("json").loads(body)["start_url"] == "/")
finally:
    server.shutdown(); server.server_close()

check("harness build is the startup file hash", srv.HARNESS_BUILD ==
      __import__("hashlib").sha256((REPO / "server.py").read_bytes()).hexdigest()[:12])

print(f"\n{'RED' if FAILS else 'GREEN'}: {len(FAILS)} failure(s)")
sys.exit(1 if FAILS else 0)
