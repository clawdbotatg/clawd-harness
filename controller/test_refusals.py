#!/usr/bin/env python3
"""A harness "no" reaches the PM (2026-11 audit P4).

wrap / check / external_project / add_local_project used to answer `ok: true`
whenever the socket write worked; the harness's `error` frame was dropped, so
a refused verb looked done. Now each waits a moment for a refusal.

  - direct mode (HarnessClient ↔ mock harness): each verb, refused and accepted
  - a refusal about ANOTHER session, or another verb, is not ours
  - fleet mode (RelayMachine): the same listener, fed by the relay reader
  - not connected is a refusal too, and doesn't wait

Run:  python3 -m controller.test_refusals
"""
import tempfile
import threading
import time

from . import harness_client, relay_client
from .harness_client import HarnessClient
from .ledger import TaskLedger
from .mock_harness import MockHarness, TOKEN
from .relay_client import RelayMachine
from .verbs import Guard, Verbs
from .world import World

PORT = 8905


def _wait(pred, timeout=6.0, what=""):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    raise AssertionError(f"timeout waiting for {what}")


def main():
    failures = []

    from . import config
    config.RELAY_URL = ""
    harness_client.REFUSAL_WAIT = relay_client.REFUSAL_WAIT = 0.6

    def check(name, fn):
        try:
            fn()
            print(f"  ✓ {name}")
        except Exception as e:
            print(f"  ✗ {name}: {type(e).__name__}: {e}")
            failures.append(name)

    mock = MockHarness(PORT).start()
    st = mock.state
    cid = st.add_session(title="work")
    other = st.add_session(title="other")
    ledger = TaskLedger(tempfile.mktemp(suffix=".jsonl"))
    client = HarnessClient("self", mock.url, TOKEN).start()
    clients = {"self": client}
    verbs = Verbs(World(clients, ledger), ledger, clients, Guard(autonomy="auto"))
    _wait(lambda: client.connected and len(client.sessions) >= 2, what="client sync")

    def t_wrap():
        st.refuse = {"wrap": "session is mid-turn"}
        r = verbs.wrap("self", cid, confirm=True)
        assert r["ok"] is False and "mid-turn" in r["error"], r
        st.refuse = {}
        r = verbs.wrap("self", cid, confirm=True)
        assert r["ok"] is True and "error" not in r and r["cid"] == cid, r
    check("wrap: refused → ok:false + the reason; accepted → ok", t_wrap)

    def t_gone():
        r = verbs.wrap("self", "nope", confirm=True)
        assert r["ok"] is False and "no such session" in r["error"], r
        r = verbs.check("self", "nope", confirm=True)
        assert r["ok"] is False and "no such session" in r["error"], r
    check("wrap / check on a session that isn't there is refused", t_gone)

    def t_check():
        st.refuse = {"check": "codex isn't signed in"}
        r = verbs.check("self", cid, confirm=True)
        assert r["ok"] is False and "signed in" in r["error"], r
        st.refuse = {}
        r = verbs.check("self", cid, confirm=True)
        assert r["ok"] is True, r
    check("check: refused → ok:false + the reason; accepted → ok", t_check)

    def t_projects():
        st.refuse = {"addExternalProject": "not a GitHub URL",
                     "addLocalProject": "path is outside the allowed roots"}
        r = verbs.external_project("self", "https://gitlab.com/a/b", confirm=True)
        assert r["ok"] is False and "not a GitHub URL" in r["error"], r
        r = verbs.add_local_project("self", "/etc", confirm=True)
        assert r["ok"] is False and "allowed roots" in r["error"], r
        st.refuse = {"addLocalProject": "missing"}
        r = verbs.add_local_project("self", "/tmp/not-there", confirm=True)
        assert r["ok"] is False and "/tmp/not-there" in r["error"], r
        st.refuse = {}
        r = verbs.external_project("self", "https://github.com/octocat/x", confirm=True)
        assert r["ok"] is True and r.get("note"), r
        r = verbs.add_local_project("self", "/tmp/fine", confirm=True)
        assert r["ok"] is True, r
    check("project adds: bad URL / bad path / missing folder are refused", t_projects)

    def t_not_ours():
        # a refusal about another session, and a project error, arrive while
        # we wait on OUR wrap — neither may fail it
        def noise():
            time.sleep(0.15)
            client._handle({"type": "error", "cid": other, "error": "wrap: nope"})
            client._handle({"type": "error", "error": "addLocalProject: nope"})
        threading.Thread(target=noise, daemon=True).start()
        r = verbs.wrap("self", cid, confirm=True)
        assert r["ok"] is True, r
        assert not client._refusals._waiters, "a waiter leaked"
    check("someone else's refusal is not ours; no waiter is left behind", t_not_ours)

    def t_fast():
        st.refuse = {"wrap": "no"}
        t0 = time.time()
        verbs.wrap("self", cid, confirm=True)
        assert time.time() - t0 < 0.4, "a refusal should answer at once"
        st.refuse = {}
    check("a refusal answers at once, not after the full wait", t_fast)

    def t_relay():
        sent = []

        class Fleet:
            up = True

            def _to_machine(self, mid, msg):
                sent.append(msg)
                return self.up
        fleet = Fleet()
        m = RelayMachine("boxb", fleet)

        def refuse():
            time.sleep(0.1)
            m._handle({"type": "error", "cid": "c9", "check": "c9",
                       "error": "can't double-check: no conversation yet"})
        threading.Thread(target=refuse, daemon=True).start()
        why = m.check_session("c9")
        assert why and "no conversation yet" in why, why
        assert sent[-1] == {"type": "check", "cid": "c9", "via": "pm"}, sent
        assert m.wrap_session("c9") is None                      # nobody said no
        fleet.up = False
        t0 = time.time()
        assert m.wrap_session("c9") == "not connected"
        assert time.time() - t0 < 0.3, "not-connected must not wait"
    check("fleet: RelayMachine hears the refusal; offline answers at once", t_relay)

    mock.stop()
    print()
    if failures:
        print(f"FAILED: {failures}")
        return 1
    print("PASSED: a harness refusal reaches the PM")
    return 0


if __name__ == "__main__":
    import os
    import sys
    rc = main()
    sys.stdout.flush()
    os._exit(rc)
