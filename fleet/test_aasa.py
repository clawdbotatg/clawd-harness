#!/usr/bin/env python3
"""The relay serves /.well-known/apple-app-site-association for the native iOS
wrapper (clawd-dictate/ios → ClawdHarness): a WKWebView may use the fleet
passkey only when the domain names the app there. Asserts over a real socket:
  1. FLEET_AASA_APPS set → 200, application/json, webcredentials.apps = the list;
  2. unset → 404 (nothing to associate; never an empty apps list).

Run: python3 fleet/test_aasa.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
TMP = Path(tempfile.mkdtemp(prefix="clawd-fleet-aasa-"))
APPS = ["TEAMID1234.com.example.harness", "TEAMID1234.com.example.harness.dev"]


def env(port, apps):
    e = {
        **os.environ,
        "FLEET_PORT": str(port), "FLEET_BIND": "127.0.0.1",
        "FLEET_PASSKEY_ONLY": "1", "FLEET_REQUIRE_PASSKEY": "1",
        "FLEET_WORKER_TOKEN": "aasa-worker-token", "FLEET_CONTROLLER_TOKEN": "aasa-controller-token",
        "FLEET_RP_ID": "h.atg.link", "FLEET_ORIGIN": "https://h.atg.link",
        # every state file isolated — never the live fleet/.clawd-fleet.*.json
        "FLEET_PASSKEY_FILE": str(TMP / "passkeys.json"), "FLEET_SESSIONS_FILE": str(TMP / "sessions.json"),
        "FLEET_PREFS_FILE": str(TMP / "prefs.json"), "FLEET_PUSH_SUBS_FILE": str(TMP / "push.json"),
        "FLEET_VAPID_FILE": str(TMP / "vapid.json"), "FLEET_ROSTER_FILE": str(TMP / "roster.json"),
        "FLEET_SKILLS_DIR": str(TMP / "skills"),
    }
    e.pop("FLEET_AASA_APPS", None)
    if apps is not None:
        e["FLEET_AASA_APPS"] = " , ".join(apps) + ","   # sloppy spacing/trailing comma must be tolerated
    return e


def fetch(port):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/.well-known/apple-app-site-association")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.headers.get("Content-Type"), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type"), e.read()


def run(port, apps):
    (TMP / "passkeys.json").write_text("[]")
    proc = subprocess.Popen([sys.executable, "relay.py"], env=env(port, apps), cwd=str(HERE),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.time() + 8
        while True:
            try:
                return fetch(port)
            except (urllib.error.URLError, ConnectionError, OSError):
                if time.time() > deadline:
                    raise
                time.sleep(0.15)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def main():
    code, ctype, body = run(8811, APPS)
    assert code == 200, (code, body)
    assert ctype and ctype.startswith("application/json"), ctype
    assert json.loads(body) == {"webcredentials": {"apps": APPS}}, body
    print("ok  1. FLEET_AASA_APPS set → 200 application/json with the app ids")

    code, _, _ = run(8812, None)
    assert code == 404, code
    print("ok  2. unset → 404")
    print("test_aasa: all green")


if __name__ == "__main__":
    main()
