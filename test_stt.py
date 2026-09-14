#!/usr/bin/env python3
"""test_stt.py — 🎤 Deepgram dictation creds (WS `stt`).

The page streams mic audio straight to Deepgram over its own socket, so the
box hands it a credential. Guards, on a sandboxed copy of server.py with
urlopen faked — never Deepgram, never the live daemon:
  1. no key → deepgram_creds() is None and `stt` answers an error frame;
  2. a key that can mint → a bearer JWT with Deepgram's ttl, cached and
     re-used until a minute before it expires, re-minted after;
  3. a grant that refuses our TTL (400) → retried with Deepgram's default;
  4. a key that can't mint (403 — Member permission) or any failure → the key
     itself as proto "token", ttl 0 (dictation never depends on the grant);
  5. the `stt` reply carries id + proto + secret + model to THIS client only.
Exits non-zero on any failure.
"""
import io, sys, json, types, shutil, tempfile   # (one line: the gitleaks bip39 rule reads a column of imports as a mnemonic)
import importlib.util
import urllib.error
from pathlib import Path

REPO = Path(__file__).resolve().parent
FAILS = []


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + (f"  — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def load_server_sandboxed():
    tmp = Path(tempfile.mkdtemp(prefix="stt-test-"))
    shutil.copy(REPO / "server.py", tmp / "server.py")
    spec = importlib.util.spec_from_file_location("server_sandbox_stt", tmp / "server.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["server_sandbox_stt"] = mod
    spec.loader.exec_module(mod)     # HERE = tmp dir → empty registry, inert
    return mod


srv = load_server_sandboxed()


class FakeResp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): return False


def opener(script):
    """script = list of ('ok', body) | ('http', code) | ('err',) consumed per call; records bodies."""
    calls = []
    def urlopen(req, timeout=0):
        calls.append(json.loads(req.data.decode()))
        step = script.pop(0)
        if step[0] == "ok":
            return FakeResp(json.dumps(step[1]).encode())
        if step[0] == "http":
            raise urllib.error.HTTPError(req.full_url, step[1], "x", {}, None)
        raise OSError("boom")
    urlopen.calls = calls
    return urlopen


class FakeClient:
    def __init__(self): self.sent = []
    def send_json(self, o): self.sent.append(o)


def reset(key):
    srv.DEEPGRAM_API_KEY = key
    srv._dg_cache.update(secret="", exp=0.0)


# 1. unconfigured
reset("")
check("no key → no creds", srv.deepgram_creds() is None)
c = FakeClient(); srv.serve_stt(c, {"type": "stt", "id": "a1"})
check("no key → stt error frame to this client", c.sent == [{"type": "stt", "id": "a1", "error": "stt not configured"}], str(c.sent))

# 2. grant works → bearer, cached
reset("dg-key")
now = [1000.0]
u = opener([("ok", {"access_token": "jwt-1", "expires_in": 3600})])
r = srv.deepgram_creds(_urlopen=u, _now=lambda: now[0])
check("grant → bearer jwt with Deepgram's ttl", r == {"proto": "bearer", "secret": "jwt-1", "ttl": 3600, "model": srv.DEEPGRAM_MODEL}, str(r))
check("grant asked for our TTL", u.calls == [{"ttl_seconds": srv.DG_GRANT_TTL}], str(u.calls))
now[0] += 3000
r2 = srv.deepgram_creds(_urlopen=opener([]), _now=lambda: now[0])   # an empty script would blow up if it re-minted
check("cached until a minute before expiry (ttl counts down)", r2["secret"] == "jwt-1" and r2["ttl"] == 600, str(r2))
now[0] += 550                                                       # 50 s left → re-mint
u3 = opener([("ok", {"access_token": "jwt-2", "expires_in": 30})])
r3 = srv.deepgram_creds(_urlopen=u3, _now=lambda: now[0])
check("re-minted inside the last minute", r3["secret"] == "jwt-2" and r3["ttl"] == 30, str(r3))

# 3. our TTL refused → Deepgram's default
reset("dg-key")
u = opener([("http", 400), ("ok", {"access_token": "jwt-d", "expires_in": 30})])
r = srv.deepgram_creds(_urlopen=u, _now=lambda: 1.0)
check("400 on our TTL → retried with the default body", u.calls == [{"ttl_seconds": srv.DG_GRANT_TTL}, {}] and r["secret"] == "jwt-d", str((u.calls, r)))

# 4. can't mint → the key itself
for label, script in (("403 (no Member permission)", [("http", 403)]), ("network failure", [("err",)]),
                      ("empty grant body", [("ok", {})])):
    reset("dg-key")
    r = srv.deepgram_creds(_urlopen=opener(script), _now=lambda: 1.0)
    check(f"{label} → the key itself, proto token, ttl 0", r == {"proto": "token", "secret": "dg-key", "ttl": 0, "model": srv.DEEPGRAM_MODEL}, str(r))
check("a failed mint caches nothing", srv._dg_cache["secret"] == "")

# 5. the verb (grant fails → key) — reply shape, this client only
reset("dg-key")
srv.urllib.request.urlopen = opener([("http", 403)])   # serve_stt uses the module default
c = FakeClient(); srv.serve_stt(c, {"type": "stt", "id": "z9"})
check("stt reply: id + proto + secret + ttl + model", c.sent == [{"type": "stt", "id": "z9", "proto": "token", "secret": "dg-key", "ttl": 0, "model": srv.DEEPGRAM_MODEL}], str(c.sent))
check("/config advertises stt only with a key", "\"stt\": bool(DEEPGRAM_API_KEY)" in (REPO / "server.py").read_text())

print()
if FAILS:
    print(f"FAIL: {len(FAILS)} check(s): " + "; ".join(FAILS)); sys.exit(1)
print("PASS — stt creds: bearer when the key can mint, the key itself when it can't, never a dependency on the grant")
