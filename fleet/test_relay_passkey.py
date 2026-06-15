#!/usr/bin/env python3
"""Integration test: the relay's passkey gate over a real WS.

Spawns relay.py with REQUIRE_PASSKEY on and a pre-seeded credential (the public
fixture from test_webauthn), connects as a mobile, and asserts the gate:
authRequired(method=passkey, with the enrolled cred id + a fresh challenge),
pre-auth frames ignored, unknown-credential rejected, and a *replayed* assertion
(carrying a stale challenge) rejected. The positive path (a valid live assertion)
is exercised by Face ID in the browser; the verifier itself is unit-tested in
test_webauthn.py.

Run: python3 relay/test_relay_passkey.py
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import fleet_ws
from test_webauthn import AS_AUTH, AS_CLIENT, AS_SIG, CRED_ID, PX, PY

HERE = Path(__file__).resolve().parent
PORT = "8801"
RELAY = f"ws://127.0.0.1:{PORT}"
TOKEN = "smoke-mobile-token"
# Isolated temp store — NEVER the real fleet/.clawd-fleet.passkeys.json (a live
# worker uses that path; clobbering it would wipe a provisioned passkey).
STORE = Path(tempfile.gettempdir()) / "clawd-fleet-test.passkeys.json"

ENV = {
    **os.environ,
    "FLEET_PORT": PORT,
    "FLEET_BIND": "127.0.0.1",
    "FLEET_MOBILE_TOKEN": TOKEN,
    "FLEET_WORKER_TOKEN": "smoke-worker-token",
    "FLEET_REQUIRE_PASSKEY": "1",
    "FLEET_RP_ID": "h.atg.link",
    "FLEET_ORIGIN": "https://h.atg.link",
    "FLEET_PASSKEY_FILE": str(STORE),
}


def main():
    STORE.write_text(json.dumps([{"id": CRED_ID, "x": format(PX, "064x"),
                                  "y": format(PY, "064x"), "sign_count": 0}]))
    proc = subprocess.Popen([sys.executable, "relay.py"], env=ENV, cwd=str(HERE),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.2)
    checks = []

    def check(name, cond):
        checks.append((name, bool(cond)))
        print(f"  {'✓' if cond else '✗ FAIL'} {name}")

    try:
        from urllib.parse import quote

        sock, rfile, wfile = fleet_ws.client_connect(f"{RELAY}/ws?role=mobile&t={quote(TOKEN)}")
        lock = threading.Lock()
        inbox = []

        def reader():
            while True:
                msg = fleet_ws.ws_read_message(rfile)
                if msg is None:
                    return
                kind, data = msg
                if kind == "ping":
                    fleet_ws.ws_send(wfile, lock, data, opcode=0xA, mask=True)
                    continue
                if kind in ("pong", "close"):
                    continue
                try:
                    inbox.append(json.loads(data.decode()))
                except Exception:
                    pass

        threading.Thread(target=reader, daemon=True).start()

        def send(obj):
            fleet_ws.ws_send(wfile, lock, json.dumps(obj), opcode=0x1, mask=True)

        def wait_for(pred, timeout=4.0):
            end = time.time() + timeout
            while time.time() < end:
                for f in list(inbox):
                    if pred(f):
                        return f
                time.sleep(0.05)
            return None

        # 1. connect → passkey challenge, roster withheld
        first = wait_for(lambda f: f.get("type") in ("authRequired", "machines"))
        ok_chal = (first and first.get("type") == "authRequired"
                   and first.get("method") == "passkey" and first.get("challenge")
                   and first.get("enrolled") is True and CRED_ID in (first.get("credentialIds") or []))
        check("connect → passkey challenge with enrolled cred", ok_chal)

        # 2. pre-auth list ignored
        inbox.clear()
        send({"type": "list"})
        check("pre-auth list ignored", wait_for(lambda f: f.get("type") == "machines", 1.5) is None)

        # 3. unknown credential rejected
        inbox.clear()
        send({"type": "auth", "id": "bogus", "clientDataJSON": AS_CLIENT,
              "authenticatorData": AS_AUTH, "signature": AS_SIG})
        rej = wait_for(lambda f: f.get("type") == "error")
        check("unknown credential rejected", rej and "unknown credential" in rej.get("error", ""))

        # 4. replayed assertion (stale challenge) rejected — a fresh connection challenge
        #    won't match the fixture's baked-in challenge
        sock2, rfile2, wfile2 = fleet_ws.client_connect(f"{RELAY}/ws?role=mobile&t={quote(TOKEN)}")
        lock2 = threading.Lock()
        inbox2 = []

        def reader2():
            while True:
                msg = fleet_ws.ws_read_message(rfile2)
                if msg is None:
                    return
                kind, data = msg
                if kind == "ping":
                    fleet_ws.ws_send(wfile2, lock2, data, opcode=0xA, mask=True)
                    continue
                if kind in ("pong", "close"):
                    continue
                try:
                    inbox2.append(json.loads(data.decode()))
                except Exception:
                    pass

        threading.Thread(target=reader2, daemon=True).start()
        time.sleep(0.4)
        fleet_ws.ws_send(wfile2, lock2, json.dumps(
            {"type": "auth", "id": CRED_ID, "clientDataJSON": AS_CLIENT,
             "authenticatorData": AS_AUTH, "signature": AS_SIG}), opcode=0x1, mask=True)
        end = time.time() + 4
        got = None
        while time.time() < end:
            for f in list(inbox2):
                if f.get("type") == "error":
                    got = f
                    break
            if got:
                break
            time.sleep(0.05)
        check("replayed assertion (stale challenge) rejected",
              got and "challenge mismatch" in got.get("error", ""))
        sock2.close()
        sock.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except Exception:
            proc.kill()
        STORE.unlink(missing_ok=True)

    ok = all(c for _, c in checks)
    print("PASSED: relay passkey gate enforced" if ok else "FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
