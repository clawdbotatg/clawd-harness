#!/usr/bin/env python3
"""The 📚 picker's DIRECT-mode path: server.py proxying the skill library.

The library lives on the relay (one list everywhere — see
docs/fleet/SKILLS.md); in fleet mode the browser asks the relay itself, but a
direct-mode page (127.0.0.1:8787) has no relay socket, so the harness proxies
`skillsLib`/`skillsRm` over the relay's worker-token HTTP. Boots a REAL relay
on a tmp store and drives server.serve_skills_lib with a fake client:

  * skillsLib replies the library with descriptions + full bodies,
  * skillsRm removes on the relay, then replies the fresh list,
  * an unreachable relay replies an explanatory error, never crashes/hangs.

Run: python3 test_skills_frame.py
"""
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from urllib.parse import quote

import server

HERE = Path(__file__).resolve().parent
PORT = "8809"
TOKEN = "frame-test-worker"
TMP = Path(tempfile.mkdtemp(prefix="clawd-skillsframe-test."))
BODY = "---\nname: vesta\ndescription: put words on the Vestaboard\n---\nthe how-to\n"

FAILS = []


def check(name, ok, detail=""):
    print(f"  {'ok' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


class FakeClient:
    def __init__(self):
        self.sent = []

    def send_json(self, obj):
        self.sent.append(obj)


def ask(frame):
    c = FakeClient()
    server.serve_skills_lib(c, frame)
    return c.sent[-1] if c.sent else None


def main():
    env = {
        **os.environ,
        "FLEET_PORT": PORT, "FLEET_BIND": "127.0.0.1",
        "FLEET_MOBILE_TOKEN": "frame-test-mobile", "FLEET_WORKER_TOKEN": TOKEN,
        "FLEET_REQUIRE_PASSKEY": "0",
        "FLEET_SKILLS_DIR": str(TMP / "store"),
        "FLEET_PREFS_FILE": str(TMP / "prefs.json"),
        "FLEET_SESSIONS_FILE": str(TMP / "sessions.json"),
        "FLEET_PUSH_SUBS_FILE": str(TMP / "push.json"),
    }
    proc = subprocess.Popen([sys.executable, "relay.py"], env=env,
                            cwd=str(HERE / "fleet"),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.2)
    # point the proxy's config at OUR relay (env beats fleet/fleet.env)
    os.environ["FLEET_RELAY"] = f"ws://127.0.0.1:{PORT}"
    os.environ["FLEET_WORKER_TOKEN"] = TOKEN
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{PORT}/skills/put?t={quote(TOKEN)}",
            data=json.dumps({"name": "vesta", "files": {
                "SKILL.md": base64.b64encode(BODY.encode()).decode()}}).encode(),
            method="POST")
        with urllib.request.urlopen(req, timeout=10):
            pass

        got = ask({"type": "skillsLib"})
        one = ((got or {}).get("skills") or [{}])[0]
        check("skillsLib proxies the library (desc + full body)",
              got and got.get("type") == "skillsLib"
              and one.get("name") == "vesta"
              and one.get("description") == "put words on the Vestaboard"
              and one.get("body") == BODY, json.dumps(got)[:200])

        got = ask({"type": "skillsRm", "name": "vesta"})
        check("skillsRm removes on the relay + replies the fresh list",
              got and got.get("skills") == [] and not got.get("error"))

        os.environ["FLEET_RELAY"] = "ws://127.0.0.1:1"   # nothing listens here
        got = ask({"type": "skillsLib"})
        check("unreachable relay → explanatory error, no crash",
              got and got.get("skills") == [] and "unreachable" in (got.get("error") or ""))
    finally:
        proc.terminate()
        proc.wait(timeout=5)
        shutil.rmtree(TMP, ignore_errors=True)

    print("\nall skills-frame checks passed" if not FAILS else f"\nFAILED: {FAILS}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
