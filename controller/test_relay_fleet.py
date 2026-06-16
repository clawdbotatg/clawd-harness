#!/usr/bin/env python3
"""End-to-end smoke for the trusted-control path (the box-brain driving the fleet):

  RelayFleet → relay (role=controller) → worker (FLEET_CTL_ALLOW, E2E REQUIRED)
            → mock harness → back

Proves the box-resident controller sees a machine's projects/sessions and can
drive it (send + spawn) over the trusted plaintext path *while E2E is required*
for mobiles. No real claude. Run: python3 -m controller.test_relay_fleet
"""
import os
import signal
import subprocess
import sys
import time

from .mock_harness import MockHarness, TOKEN as HARNESS_TOKEN
from .relay_client import RelayFleet

FLEET_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fleet")
RELAY_PORT = "8795"
HARNESS_PORT = 8794
CTL_TOKEN = "ctl-secret"
WORKER_TOKEN = "wk"
procs = []


def spawn(args, env):
    p = subprocess.Popen([sys.executable, *args], cwd=FLEET_DIR,
                         env={**os.environ, **env},
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    procs.append(p)
    return p


def wait(pred, timeout=8.0, what=""):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    raise AssertionError(f"timeout waiting for {what}")


def main():
    failures = []

    def check(name, fn):
        try:
            fn()
            print(f"  ✓ {name}")
        except Exception as e:
            print(f"  ✗ {name}: {e}")
            failures.append(name)

    mock = MockHarness(HARNESS_PORT).start()
    mock.state.add_session(title="remote work")          # c1

    spawn(["relay.py"], {"FLEET_PORT": RELAY_PORT, "FLEET_BIND": "127.0.0.1",
                         "FLEET_CONTROLLER_TOKEN": CTL_TOKEN, "FLEET_WORKER_TOKEN": WORKER_TOKEN,
                         "FLEET_REQUIRE_PASSKEY": "0", "FLEET_PASSKEY_FILE": "/tmp/ctl-pk.json"})
    time.sleep(1.2)
    spawn(["worker.py", "--machine", "mockbox", "--host", "mock",
           "--harness", f"ws://127.0.0.1:{HARNESS_PORT}", "--harness-token", HARNESS_TOKEN],
          {"FLEET_RELAY": f"ws://127.0.0.1:{RELAY_PORT}", "FLEET_WORKER_TOKEN": WORKER_TOKEN,
           "FLEET_CTL_ALLOW": "1", "FLEET_E2E_REQUIRE": "1"})   # E2E ON — trusted path must still work
    time.sleep(1.8)

    fleet = RelayFleet(f"ws://127.0.0.1:{RELAY_PORT}", CTL_TOKEN).start()

    def t_connect():
        wait(lambda: fleet.connected, what="controller→relay connect")
        wait(lambda: "mockbox" in fleet.machines, what="roster shows mockbox")
    check("controller connects + sees the machine via roster", t_connect)

    def t_sees_sessions():
        wait(lambda: any(s["cid"] == "c1" for s in fleet.machines["mockbox"].state()["sessions"]),
             what="mockbox sessions (projects/sessions pulled through the trusted path)")
        st = fleet.machines["mockbox"].state()
        assert any(p["pid"] == "p1" for p in st["projects"]), st["projects"]
    check("sees the machine's projects + sessions (E2E still required for mobiles)", t_sees_sessions)

    def t_drive_send():
        mm = fleet.machines["mockbox"]
        mm.send_message("c1", "do it")
        wait(lambda: "handled: do it" in (
            next((s for s in mm.state()["sessions"] if s["cid"] == "c1"), {}).get("digest", "")),
            what="send round-trips and the session digest updates")
    check("drives a session (send) over the trusted path", t_drive_send)

    def t_spawn():
        cid = fleet.machines["mockbox"].new_session("p1")
        assert cid, "new_session returned no cid"
        wait(lambda: any(s["cid"] == cid for s in fleet.machines["mockbox"].state()["sessions"]),
             what="spawned session appears")
    check("spawns a new session (new) over the trusted path", t_spawn)

    fleet.stop()
    mock.stop()
    print()
    if failures:
        print(f"FAILED: {failures}")
        return 1
    print("PASSED: trusted-control path — box-brain sees + drives the fleet (E2E on)")
    return 0


def cleanup():
    for p in procs:
        try:
            p.send_signal(signal.SIGTERM)
        except Exception:
            pass


if __name__ == "__main__":
    rc = 1
    try:
        rc = main()
    finally:
        cleanup()
    sys.stdout.flush()
    os._exit(rc)
