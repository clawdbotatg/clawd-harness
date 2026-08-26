#!/usr/bin/env python3
"""Integration test: the relay's shared "active machines" prefs.

A machine costs one passkey per 24h, so a fleet of N boxes costs N ceremonies
at dawn even when you only wanted one of them. The UI lets you switch a machine
off; this is the server half — the set lives on the relay so unchecking on the
phone also unchecks on the desktop.

Asserts the three properties the UI depends on:
  1. `prefs` reaches a mobile BEFORE its first `machines` frame — the page kicks
     its per-machine E2E handshakes (and therefore its passkey prompts) off the
     roster, so a roster that arrived first would unlock boxes you switched off,
     which is the exact storm this feature exists to stop.
  2. A write from one mobile is echoed to EVERY authed mobile (cross-device) and
     survives to disk (a reconnect tomorrow sees it).
  3. The deny-list is sanitized before it's persisted — this is the one place a
     mobile writes durable state on a shared production box.

Run: python3 fleet/test_relay_prefs.py
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import quote

import fleet_ws

HERE = Path(__file__).resolve().parent
PORT = "8803"
RELAY = f"ws://127.0.0.1:{PORT}"
TOKEN = "prefs-test-token"
PREFS = Path(tempfile.gettempdir()) / "clawd-fleet-test.prefs.json"

ENV = {
    **os.environ,
    "FLEET_PORT": PORT,
    "FLEET_BIND": "127.0.0.1",
    "FLEET_MOBILE_TOKEN": TOKEN,
    "FLEET_WORKER_TOKEN": "prefs-test-worker",
    "FLEET_REQUIRE_PASSKEY": "0",     # the gate itself is test_relay_passkey.py's job
    "FLEET_PREFS_FILE": str(PREFS),
    "FLEET_SESSIONS_FILE": str(Path(tempfile.gettempdir()) / "clawd-fleet-test-prefs.sessions.json"),
    "FLEET_PUSH_SUBS_FILE": str(Path(tempfile.gettempdir()) / "clawd-fleet-test-prefs.push.json"),
}


def dial():
    """A mobile connection + a background reader; returns (sock, send, inbox)."""
    sock, rfile, wfile = fleet_ws.client_connect(f"{RELAY}/ws?role=mobile&t={quote(TOKEN)}")
    lock, inbox = threading.Lock(), []

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

    return sock, send, inbox


def wait_for(inbox, pred, timeout=4.0):
    end = time.time() + timeout
    while time.time() < end:
        for f in list(inbox):
            if pred(f):
                return f
        time.sleep(0.05)
    return None


def main():
    PREFS.unlink(missing_ok=True)
    proc = subprocess.Popen([sys.executable, "relay.py"], env=ENV, cwd=str(HERE),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.2)
    checks = []

    def check(name, cond):
        checks.append((name, bool(cond)))
        print(f"  {'✓' if cond else '✗ FAIL'} {name}")

    socks = []
    try:
        # 1. fresh mobile: prefs first, and empty (nothing switched off yet)
        s1, send1, in1 = dial()
        socks.append(s1)
        first = wait_for(in1, lambda f: f.get("type") in ("prefs", "machines"))
        check("prefs precedes the first roster", first and first.get("type") == "prefs")
        check("default is empty (unknown machine = ON)", first and first.get("inactive") == [])

        # 2. a second device is listening when the first one writes
        s2, send2, in2 = dial()
        socks.append(s2)
        wait_for(in2, lambda f: f.get("type") == "prefs")
        in1.clear()
        in2.clear()
        send1({"type": "prefs", "inactive": ["clawd-head", "clawd-heart"]})
        echo = wait_for(in1, lambda f: f.get("type") == "prefs")
        check("writer gets the echo (its ack)", echo and echo.get("inactive") == ["clawd-head", "clawd-heart"])
        other = wait_for(in2, lambda f: f.get("type") == "prefs")
        check("the OTHER device is told too (cross-device)",
              other and other.get("inactive") == ["clawd-head", "clawd-heart"])

        # 3. persisted: a connection made later starts from the stored set
        s3, send3, in3 = dial()
        socks.append(s3)
        p3 = wait_for(in3, lambda f: f.get("type") == "prefs")
        check("a later connection starts from the stored set",
              p3 and p3.get("inactive") == ["clawd-head", "clawd-heart"])
        on_disk = json.loads(PREFS.read_text())
        check("written to disk", on_disk.get("inactive") == ["clawd-head", "clawd-heart"])

        # 4. sanitized: non-strings, dupes and over-long ids never reach disk
        in1.clear()
        send1({"type": "prefs", "inactive": ["ok", "ok", 42, None, {"x": 1}, "z" * 200]})
        san = wait_for(in1, lambda f: f.get("type") == "prefs")
        check("non-strings, dupes and over-long ids dropped", san and san.get("inactive") == ["ok"])

        # 5. a machine can be switched back on (the list is authoritative, not additive)
        in1.clear()
        send1({"type": "prefs", "inactive": []})
        cleared = wait_for(in1, lambda f: f.get("type") == "prefs")
        check("clearing the list switches everything back on", cleared and cleared.get("inactive") == [])

        # 6. 🔥 irons ride the same frame as a SECOND field — and per-field merge
        #    is load-bearing: an irons-only write must not blow away the deny-list
        #    (and vice versa), because the client sends them separately.
        send1({"type": "prefs", "inactive": ["clawd-heart"]})
        wait_for(in1, lambda f: f.get("type") == "prefs" and f.get("inactive") == ["clawd-heart"])
        in1.clear(); in2.clear()
        iron = {"id": "iabc123", "title": "voice", "desc": "all the voice work",
                "tags": ["speech"], "keys": ["github.com/clawdbotatg/gpt-voice"], "created": 1}
        send1({"type": "prefs", "irons": [iron]})
        got = wait_for(in1, lambda f: f.get("type") == "prefs" and f.get("irons"))
        check("irons echo back to the writer", got and got["irons"][0]["title"] == "voice"
              and got["irons"][0]["keys"] == ["github.com/clawdbotatg/gpt-voice"])
        check("an irons-only write KEEPS the deny-list", got and got.get("inactive") == ["clawd-heart"])
        got2 = wait_for(in2, lambda f: f.get("type") == "prefs" and f.get("irons"))
        check("irons reach the other device too", got2 and got2["irons"][0]["id"] == "iabc123")
        in1.clear()
        send1({"type": "prefs", "inactive": []})
        back = wait_for(in1, lambda f: f.get("type") == "prefs")
        check("an inactive-only write KEEPS the irons", back and back.get("irons")
              and back["irons"][0]["id"] == "iabc123")
        disk = json.loads(PREFS.read_text())
        check("irons written to disk", disk.get("irons") and disk["irons"][0]["title"] == "voice")

        # 7. irons are sanitized like everything a mobile persists on this box:
        #    junk entries dropped, dupes dropped, texts clipped, members bounded
        in1.clear()
        send1({"type": "prefs", "irons": [
            {"id": "ok1", "title": "  keep  ", "tags": ["a", 7, "  "], "keys": ["k1", 9, {"x": 1}]},
            {"id": "ok1", "title": "dupe id"},          # duplicate id → dropped
            {"id": "", "title": "no id"},               # bad id → dropped
            {"id": "ok2", "title": "   "},              # blank title → dropped
            "not-a-dict", 42,
            {"id": "ok3", "title": "t" * 500, "desc": "d" * 5000},
        ]})
        san2 = wait_for(in1, lambda f: f.get("type") == "prefs" and f.get("irons") is not None)
        irons = (san2 or {}).get("irons") or []
        check("junk iron entries never reach the set",
              [i["id"] for i in irons] == ["ok1", "ok3"])
        check("member keys / tags are typed + bounded",
              irons and irons[0]["keys"] == ["k1"] and irons[0]["tags"] == ["a"]
              and irons[0]["title"] == "keep")
        check("over-long texts are clipped",
              len(irons) == 2 and len(irons[1]["title"]) == 80 and len(irons[1]["desc"]) == 400)
    finally:
        for s in socks:
            try:
                s.close()
            except Exception:
                pass
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except Exception:
            proc.kill()
        PREFS.unlink(missing_ok=True)

    ok = all(c for _, c in checks)
    print("PASSED: relay prefs shared + ordered + sanitized" if ok else "FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
