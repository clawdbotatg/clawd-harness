#!/usr/bin/env python3
"""kiosk-guard — keep a hands-off wall display on its home screen.

The box has no keyboard or mouse. Things still pop up in front of the home
screen (2026-09-02 on clawd-sat: a `claude setup-token` run by a launchd job
opened its OAuth login in the kiosk Chrome profile and sat there for hours;
the existing keeper only counts connected screens, so it saw nothing wrong).
This guard runs every few seconds and puts the home screen back:

  1. every Chrome page in the kiosk profile that is NOT the home URL is closed
     (Chrome DevTools Protocol — the kiosk must be launched with
     --remote-debugging-port=KIOSK_CDP_PORT; no Accessibility permission needed,
     which a headless box can never grant);
  2. if the home page is gone, it is reopened; the home window is forced back to
     fullscreen (Browser.setWindowBounds) and activated;
  3. if some OTHER app is frontmost, the kiosk Chrome process is brought to the
     front by pid (Carbon SetFrontProcessWithOptions through ctypes — works from
     a launchd agent without any TCC consent);
  4. `claude setup-token` / `claude login` processes older than
     KIOSK_STALE_LOGIN_S are killed: a browser OAuth flow can never complete on
     a box with no input devices, and a hung one keeps its launchd job wedged.

Stdlib only. Configure with env (defaults fit clawd-sat's gizmo):
  KIOSK_URL            http://127.0.0.1:7912      home screen (prefix match)
  KIOSK_PROFILE        ~/.gizmo-chrome            Chrome --user-data-dir of the kiosk
  KIOSK_CDP_PORT       9223                       --remote-debugging-port of the kiosk
  KIOSK_INTERVAL       2                          seconds between checks
  KIOSK_FULLSCREEN     1                          force the home window fullscreen
  KIOSK_STALE_LOGIN_S  180                        kill setup-token/login older than this (0 = never)
  KIOSK_ACTIVATE       1                          bring the kiosk to front when another app is
Run with --once for a single pass (prints what it would do / did).
"""
import base64
import ctypes
import json
import os
import signal
import socket
import struct
import subprocess
import sys
import time
import urllib.request
import urllib.parse

URL = os.environ.get("KIOSK_URL", "http://127.0.0.1:7912").rstrip("/")
PROFILE = os.path.expanduser(os.environ.get("KIOSK_PROFILE", "~/.gizmo-chrome")).rstrip("/")
CDP_PORT = int(os.environ.get("KIOSK_CDP_PORT", "9223"))
INTERVAL = float(os.environ.get("KIOSK_INTERVAL", "2"))
FULLSCREEN = os.environ.get("KIOSK_FULLSCREEN", "1") not in ("0", "", "no", "false")
STALE_LOGIN_S = int(os.environ.get("KIOSK_STALE_LOGIN_S", "180"))
ACTIVATE = os.environ.get("KIOSK_ACTIVATE", "1") not in ("0", "", "no", "false")
CDP = f"http://127.0.0.1:{CDP_PORT}"

_last = {}


def log(key, msg, every=0):
    """Log state changes, not heartbeats: a repeated `key` is printed at most
    once per `every` seconds (0 = only when the message changes)."""
    now = time.time()
    prev = _last.get(key)
    if prev and prev[0] == msg and (every == 0 or now - prev[1] < every):
        return
    _last[key] = (msg, now)
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def is_home(url):
    return (url or "").rstrip("/").startswith(URL)


# ---- processes ---------------------------------------------------------------
def ps():
    out = subprocess.run(["ps", "-axo", "pid=,etime=,command="], capture_output=True,
                         text=True).stdout
    rows = []
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3:
            rows.append((int(parts[0]), parts[1], parts[2]))
    return rows


def kiosk_pid(rows):
    """The kiosk Chrome's browser (main) process: our profile, not a --type= helper."""
    for pid, _, cmd in rows:
        if f"--user-data-dir={PROFILE}" in cmd and "--type=" not in cmd:
            return pid
    return None


def etime_seconds(et):
    # ps etime: [[dd-]hh:]mm:ss
    days = 0
    if "-" in et:
        d, et = et.split("-", 1)
        days = int(d)
    parts = [int(p) for p in et.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    return days * 86400 + parts[0] * 3600 + parts[1] * 60 + parts[2]


def kill_stale_logins(rows):
    if STALE_LOGIN_S <= 0:
        return
    for pid, et, cmd in rows:
        low = cmd.lower()
        if not ("claude" in low and (" setup-token" in low or low.endswith(" login")
                                     or " auth login" in low)):
            continue
        if "--type=" in cmd or "grep" in cmd:
            continue
        try:
            age = etime_seconds(et)
        except ValueError:
            continue
        if age >= STALE_LOGIN_S:
            try:
                os.kill(pid, signal.SIGTERM)
                log(f"kill{pid}", f"killed stale login flow pid {pid} ({age}s old): {cmd[:120]}")
            except ProcessLookupError:
                pass


# ---- front app (no Accessibility needed) ---------------------------------------
def front_pid():
    try:
        asn = subprocess.run(["lsappinfo", "front"], capture_output=True, text=True,
                             timeout=5).stdout.strip()
        out = subprocess.run(["lsappinfo", "info", "-only", "pid", asn], capture_output=True,
                             text=True, timeout=5).stdout
        return int(out.split("=")[1]) if "=" in out else None
    except Exception:
        return None


_AS = None


class _PSN(ctypes.Structure):
    _fields_ = [("hi", ctypes.c_uint32), ("lo", ctypes.c_uint32)]


def bring_to_front(pid):
    """Carbon Process Manager: activate an app by pid. Deprecated since 10.9 and
    still shipping in macOS 26; needs no TCC grant, unlike System Events."""
    global _AS
    if _AS is None:
        _AS = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
    psn = _PSN()
    if _AS.GetProcessForPID(ctypes.c_int(pid), ctypes.byref(psn)):
        return False
    return _AS.SetFrontProcessWithOptions(ctypes.byref(psn), ctypes.c_uint32(0)) == 0


# ---- Chrome DevTools Protocol ---------------------------------------------------
def cdp_http(path, method="GET", timeout=3):
    req = urllib.request.Request(CDP + path, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode("utf-8", "replace")
    try:
        return json.loads(body)
    except ValueError:
        return body


class WS:
    """Minimal RFC 6455 client for one CDP session (loopback, no TLS, no masking
    subtleties beyond the mandatory client mask)."""

    def __init__(self, url, timeout=5):
        u = urllib.parse.urlsplit(url)
        self.s = socket.create_connection((u.hostname, u.port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall((f"GET {u.path} HTTP/1.1\r\nHost: {u.hostname}:{u.port}\r\n"
                        "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.s.recv(4096)
            if not chunk:
                raise OSError("ws handshake closed")
            buf += chunk
        if b" 101 " not in buf.split(b"\r\n", 1)[0]:
            raise OSError("ws handshake refused: " + buf[:80].decode("latin1"))
        self.rest = buf.split(b"\r\n\r\n", 1)[1]
        self.n = 0

    def _recv(self, k):
        while len(self.rest) < k:
            chunk = self.s.recv(65536)
            if not chunk:
                raise OSError("ws closed")
            self.rest += chunk
        out, self.rest = self.rest[:k], self.rest[k:]
        return out

    def send(self, obj):
        data = json.dumps(obj).encode()
        mask = os.urandom(4)
        head = bytes([0x81])
        n = len(data)
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        self.s.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def recv(self):
        while True:
            b0, b1 = self._recv(2)
            op = b0 & 0x0F
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._recv(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._recv(8))[0]
            if b1 & 0x80:
                self._recv(4)
            payload = self._recv(n)
            if op == 1:
                return json.loads(payload.decode("utf-8", "replace"))
            if op == 8:
                raise OSError("ws close")
            # ping/pong/binary: ignore

    def call(self, method, params=None, timeout=5):
        self.n += 1
        rid = self.n
        self.send({"id": rid, "method": method, "params": params or {}})
        end = time.time() + timeout
        while time.time() < end:
            msg = self.recv()
            if msg.get("id") == rid:
                if "error" in msg:
                    raise OSError(f"{method}: {msg['error']}")
                return msg.get("result", {})
        raise OSError(f"{method}: timeout")

    def close(self):
        try:
            self.s.close()
        except OSError:
            pass


def ensure_fullscreen(target):
    """Force the home window to fullscreen. CDP wants normal before fullscreen
    when the window is minimized/maximized."""
    ws = WS(target["webSocketDebuggerUrl"])
    try:
        w = ws.call("Browser.getWindowForTarget", {"targetId": target["id"]})
        state = (w.get("bounds") or {}).get("windowState")
        if state == "fullscreen":
            return False
        if state in ("minimized", "maximized"):
            ws.call("Browser.setWindowBounds", {"windowId": w["windowId"],
                                                "bounds": {"windowState": "normal"}})
            time.sleep(0.3)
        ws.call("Browser.setWindowBounds", {"windowId": w["windowId"],
                                            "bounds": {"windowState": "fullscreen"}})
        return state
    finally:
        ws.close()


# ---- one pass ------------------------------------------------------------------
def guard_once():
    rows = ps()
    kill_stale_logins(rows)
    kpid = kiosk_pid(rows)
    if not kpid:
        log("nok", f"kiosk Chrome not running (profile {PROFILE}) — the keeper launches it")
        return
    log("nok", f"kiosk Chrome pid {kpid}")

    try:
        targets = cdp_http("/json/list")
    except Exception as e:
        log("cdp", f"no DevTools port {CDP_PORT} on the kiosk Chrome ({e.__class__.__name__}); "
                   "launch it with --remote-debugging-port — falling back to front-app policing")
        targets = None

    acted = False
    home = None
    if isinstance(targets, list):
        log("cdp", f"DevTools port {CDP_PORT} ok")
        pages = [t for t in targets if t.get("type") == "page"]
        homes = [t for t in pages if is_home(t.get("url"))]
        for t in pages:
            if t in homes:
                continue
            try:
                cdp_http(f"/json/close/{t['id']}")
                acted = True
                log("close" + t["id"], f"closed intruder page: {t.get('url', '')[:160]}")
            except Exception as e:
                log("closeerr", f"close failed for {t.get('url', '')[:80]}: {e}")
        if not homes:
            try:
                t = cdp_http("/json/new?" + URL, method="PUT")
                homes = [t] if isinstance(t, dict) and t.get("id") else []
                acted = True
                log("reopen", f"home page was gone — reopened {URL}")
            except Exception as e:
                log("reopenerr", f"reopen failed ({e}); killing kiosk Chrome so the keeper relaunches it")
                try:
                    os.kill(kpid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                return
        # keep exactly one home window: extra copies are as bad as intruders
        for t in homes[1:]:
            try:
                cdp_http(f"/json/close/{t['id']}")
                log("dup" + t["id"], "closed duplicate home page")
            except Exception:
                pass
        home = homes[0]
        if FULLSCREEN and home.get("webSocketDebuggerUrl"):
            try:
                was = ensure_fullscreen(home)
                if was:
                    acted = True
                    log("fs", f"home window was {was} — set fullscreen")
            except Exception as e:
                log("fserr", f"fullscreen check failed: {e}")

    fp = front_pid()
    if ACTIVATE and fp is not None and fp != kpid:
        who = next((c.split(" --")[0].rsplit("/", 1)[-1] for p, _, c in rows if p == fp), "?")
        if home:
            try:
                cdp_http(f"/json/activate/{home['id']}")
            except Exception:
                pass
        ok = bring_to_front(kpid)
        log("front", f"front app was pid {fp} ({who}) — brought kiosk to front: {ok}")
        acted = True
    elif acted and home:
        try:
            cdp_http(f"/json/activate/{home['id']}")
        except Exception:
            pass
    if not acted:
        log("ok", "home screen up, kiosk in front", every=3600)


def main():
    once = "--once" in sys.argv
    log("start", f"kiosk-guard: url={URL} profile={PROFILE} cdp={CDP_PORT} every {INTERVAL}s "
                 f"fullscreen={FULLSCREEN} activate={ACTIVATE} stale_login={STALE_LOGIN_S}s")
    while True:
        try:
            guard_once()
        except Exception as e:
            log("err", f"pass failed: {e.__class__.__name__}: {e}")
        if once:
            return
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
