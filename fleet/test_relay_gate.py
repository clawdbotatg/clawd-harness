#!/usr/bin/env python3
"""Integration test: the relay's public edge is closed to everything but a
passkey (2026-09-09 security review).

Spawns relay.py in production posture (PASSKEY_ONLY + REQUIRE_PASSKEY) with
isolated state files and asserts, over real sockets:
  1. an unknown ?role= is refused at the upgrade (it used to land in the mobile
     path with the passkey gate already satisfied);
  2. role=controller without the controller token is refused;
  3. a mobile parked at the passkey gate gets NO roster and NO worker status
     broadcast — a worker joining / reporting must not leak machine ids;
  4. POST /upload needs a live passkey session token (none / bogus → 403;
     a valid one passes the gate and fails only on the missing machine);
  5. the per-session upload quota trips (429);
  6. an oversize frame from a mobile drops the socket;
  7. the unauthenticated-socket cap refuses the N+1th parked mobile.

Run: python3 fleet/test_relay_gate.py
"""
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import quote
import urllib.request
import urllib.error

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fleet_ws  # noqa: E402
from test_webauthn import CRED_ID, PX, PY  # noqa: E402

PORT = "8802"
RELAY = f"ws://127.0.0.1:{PORT}"
HTTP = f"http://127.0.0.1:{PORT}"
WTOK = "gate-worker-token"
CTOK = "gate-controller-token"
GOOD_SESS = "gate-test-session-token-abcdefghijklmnop"
TMP = Path(tempfile.mkdtemp(prefix="clawd-fleet-gate-"))

ENV = {
    **os.environ,
    "FLEET_PORT": PORT,
    "FLEET_BIND": "127.0.0.1",
    "FLEET_PASSKEY_ONLY": "1",
    "FLEET_REQUIRE_PASSKEY": "1",
    "FLEET_WORKER_TOKEN": WTOK,
    "FLEET_CONTROLLER_TOKEN": CTOK,
    "FLEET_RP_ID": "h.atg.link",
    "FLEET_ORIGIN": "https://h.atg.link",
    "FLEET_MAX_UNAUTH": "3",
    "FLEET_UNAUTH_TTL": "10",
    "FLEET_MOBILE_MAX_MESSAGE": str(64 * 1024),
    "FLEET_UPLOAD_QUOTA": str(10 * 1024),
    # every state file isolated — never the live fleet/.clawd-fleet.*.json
    "FLEET_PASSKEY_FILE": str(TMP / "passkeys.json"),
    "FLEET_SESSIONS_FILE": str(TMP / "sessions.json"),
    "FLEET_PREFS_FILE": str(TMP / "prefs.json"),
    "FLEET_PUSH_SUBS_FILE": str(TMP / "push.json"),
    "FLEET_VAPID_FILE": str(TMP / "vapid.json"),
    "FLEET_ROSTER_FILE": str(TMP / "roster.json"),
    "FLEET_SKILLS_DIR": str(TMP / "skills"),
}


class Peer:
    """A WS client with a background reader and an inbox of JSON frames."""

    def __init__(self, url):
        self.sock, self.rfile, self.wfile = fleet_ws.client_connect(url)
        self.lock = threading.Lock()
        self.inbox = []
        self.closed = threading.Event()
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        while True:
            try:
                msg = fleet_ws.ws_read_message(self.rfile)
            except Exception:
                msg = None
            if msg is None:
                self.closed.set()
                return
            kind, data = msg
            if kind == "ping":
                try:
                    fleet_ws.ws_send(self.wfile, self.lock, data, opcode=0xA, mask=True)
                except Exception:
                    pass
                continue
            if kind == "close":
                self.closed.set()
                return
            if kind == "pong":
                continue
            try:
                self.inbox.append(json.loads(data.decode()))
            except Exception:
                pass

    def send(self, obj):
        fleet_ws.ws_send(self.wfile, self.lock, json.dumps(obj), opcode=0x1, mask=True)

    def wait_for(self, pred, timeout=3.0):
        end = time.time() + timeout
        while time.time() < end:
            for f in list(self.inbox):
                if pred(f):
                    return f
            time.sleep(0.05)
        return None

    def close(self):
        # shutdown first: sock.close() alone leaves the fd open while the
        # reader thread's makefile() still references it, so the relay would
        # never see the FIN and the conn would stay "parked".
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


def post_upload(qs, body=b"x" * 100):
    req = urllib.request.Request(f"{HTTP}/upload?{qs}", data=body, method="POST",
                                 headers={"Content-Type": "image/png"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def main():
    (TMP / "passkeys.json").write_text(json.dumps(
        [{"id": CRED_ID, "x": format(PX, "064x"), "y": format(PY, "064x"), "sign_count": 0}]))
    (TMP / "sessions.json").write_text(json.dumps({GOOD_SESS: time.time() + 3600}))
    proc = subprocess.Popen([sys.executable, "relay.py"], env=ENV, cwd=str(HERE),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.2)
    checks = []

    def check(name, cond):
        checks.append((name, bool(cond)))
        print(f"  {'✓' if cond else '✗ FAIL'} {name}")

    def refused(url):
        try:
            fleet_ws.client_connect(url)
            return False
        except ConnectionError as e:
            return "403" in str(e)

    try:
        # 1. unknown role → 403 at the upgrade
        check("unknown ?role= refused at upgrade", refused(f"{RELAY}/ws?role=security-audit-invalid"))
        # an empty role falls back to the default (mobile) — and hits the gate
        e = Peer(f"{RELAY}/ws?role=")
        check("empty ?role= is a plain mobile (gets the passkey challenge)",
              e.wait_for(lambda f: f.get("type") == "authRequired") is not None)
        e.close()
        time.sleep(0.3)
        # 2. controller without its token → 403
        check("controller without token refused", refused(f"{RELAY}/ws?role=controller"))

        # 3. parked mobile sees no roster / status when a worker joins + reports
        mob = Peer(f"{RELAY}/ws?role=mobile")
        chal = mob.wait_for(lambda f: f.get("type") == "authRequired")
        check("mobile gets passkey challenge", chal is not None)
        mob.inbox.clear()
        wk = Peer(f"{RELAY}/ws?role=worker&machine=gatebox&host=gate.local&t={quote(WTOK)}")
        wk.send({"type": "status", "msg": {"type": "hello", "secret": "titles"}})
        wk.send({"type": "stats", "projects": 1, "sessions": 2, "active": 1})
        leak = mob.wait_for(lambda f: f.get("type") in ("machines", "machineMsg", "prefs"), 1.5)
        check("parked mobile gets no roster/status/prefs", leak is None)
        check("parked mobile socket still open (ping-able, not dropped)", not mob.closed.is_set())

        # 4. upload needs a live passkey session
        check("upload with no session → 403", post_upload("machine=gatebox") == 403)
        check("upload with bogus session → 403", post_upload("machine=gatebox&s=nope") == 403)
        # a valid session passes the gate; the target machine is unknown → 502
        check("upload with valid session passes the gate (502 no such machine)",
              post_upload(f"machine=no-such-box&s={quote(GOOD_SESS)}") == 502)
        # 5. quota (10 KiB/window in this env): the second 8 KiB body trips it
        post_upload(f"machine=no-such-box&s={quote(GOOD_SESS)}", body=b"x" * 8000)
        check("upload quota trips → 429",
              post_upload(f"machine=no-such-box&s={quote(GOOD_SESS)}", body=b"x" * 8000) == 429)

        # 6. oversize frame from a mobile drops the socket (cap 64 KiB here)
        big = Peer(f"{RELAY}/ws?role=mobile")
        big.wait_for(lambda f: f.get("type") == "authRequired")
        hdr = bytes([0x81, 0x80 | 127]) + struct.pack(">Q", 1 << 20) + b"\x00\x00\x00\x00"
        with big.lock:
            big.wfile.write(hdr)
            big.wfile.flush()
        check("oversize mobile frame drops the socket", big.closed.wait(3.0))
        # a worker-size frame from a worker is still fine (cap is per role)
        wk.send({"type": "stats", "pad": "y" * (200 * 1024), "projects": 1})
        time.sleep(0.5)
        check("large worker frame accepted (per-role cap)", not wk.closed.is_set())

        noisy = Peer(f"{RELAY}/ws?role=mobile")
        noisy.wait_for(lambda f: f.get("type") == "authRequired")
        for _ in range(31):
            try:
                noisy.send({"type": "ping"})
            except OSError:
                break
        check("pre-auth message flood drops its socket", noisy.closed.wait(3))
        noisy.close()

        # 7. unauthenticated-socket cap (3 here): mob is parked (1), open two
        #    more (2, 3), the fourth is refused and closed
        p2 = Peer(f"{RELAY}/ws?role=mobile")
        p3 = Peer(f"{RELAY}/ws?role=mobile")
        p2.wait_for(lambda f: f.get("type") == "authRequired")
        p3.wait_for(lambda f: f.get("type") == "authRequired")
        p4 = Peer(f"{RELAY}/ws?role=mobile")
        err = p4.wait_for(lambda f: f.get("type") == "error", 2.0)
        check("4th parked mobile refused (FLEET_MAX_UNAUTH)",
              err and "unauthenticated" in err.get("error", "") and p4.closed.wait(3.0))
        check("earlier parked mobiles unaffected", not (mob.closed.is_set() or p3.closed.is_set()))
        p2.send({"type": "auth", "session": GOOD_SESS})
        check("valid session authenticates", p2.wait_for(lambda f: f.get("type") == "authOk") is not None)
        check("unauthenticated socket expires despite answering pings", mob.closed.wait(25))
        check("authenticated socket survives login deadline", not p2.closed.is_set())
        for p in (mob, wk, big, p2, p3, p4):
            p.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except Exception:
            proc.kill()
        for f in TMP.glob("*"):
            try:
                f.unlink() if f.is_file() else None
            except Exception:
                pass

    failed = [n for n, ok in checks if not ok]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
