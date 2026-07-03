#!/usr/bin/env python3
"""test_worker_flap.py — the iOS Face-ID socket-flap recovery paths in worker.py.

The WebAuthn sheet drops the relay socket, so a handshake's ClientAuth (or our
e2e.done reply) routinely arrives from / departs to a mobile id that no longer
exists. worker._e2e_auth must:
  1. complete a handshake by CHALLENGE match when the ClientAuth arrives from a
     new mobile id (the handshake is filed under the old one);
  2. answer a duplicate ClientAuth from the done-cache (our done was lost with
     the old socket) and re-attach the session to the new id;
  3. still reject an assertion that matches no handshake and no cache.
Exits non-zero on any failure.
"""
import json
import sys
import tempfile
from pathlib import Path

import e2e
import worker as workermod
from test_e2e import FakePasskey, check, FAILED, RP_ID, ORIGIN


def client_auth_for(worker_obj, mid, frm_hello):
    """Send hello as `frm_hello`, capture shello, derive the client side, and
    return (client_auth_msg, keys) ready to be delivered from ANY mobile id."""
    C = e2e.ClientHandshake(mid, e2e.pub_raw(worker_obj.identity.public_key()), lambda ch: None)
    replies = []
    orig = worker_obj.reply
    worker_obj.reply = lambda to, msg: replies.append((to, msg))
    try:
        worker_obj._e2e_hello(frm_hello, C.client_hello())
    finally:
        worker_obj.reply = orig
    to, sh = replies[-1]
    assert sh["t"] == "e2e.shello", sh
    epk_w = e2e.b64u_dec(sh["epk_w"]); n_w = e2e.b64u_dec(sh["n_w"])
    ik_w = e2e.b64u_dec(sh["ik_w"])
    Z = e2e.ecdh(C.eph, epk_w)
    keys = e2e.key_schedule(Z, mid, C.epk_m, C.n_m, epk_w, n_w, ik_w)
    assertion = worker_obj._test_pk.assertion(keys["webauthn_challenge"])
    cf_m = e2e.hmac.new(keys["kc_m"], b"fleet-e2e/1 client-finished", e2e.hashlib.sha256).digest()
    return {"assertion": assertion, "cf_m": e2e.b64u(cf_m)}, keys


def deliver_auth(worker_obj, frm, msg):
    replies = []
    orig = worker_obj.reply
    worker_obj.reply = lambda to, m: replies.append((to, m))
    try:
        worker_obj._e2e_auth(frm, msg)
    finally:
        worker_obj.reply = orig
    return replies[-1]


def main():
    mid = "flap-test-machine"
    pk = FakePasskey()
    tmp = Path(tempfile.mkdtemp())
    workermod.PASSKEYS_FILE = tmp / "passkeys.json"
    workermod.PASSKEYS_FILE.write_text(json.dumps([pk.record()]))
    workermod.WORKER_ID_FILE = tmp / "worker_id.json"
    workermod.RESUME_FILE = tmp / "resume.json"
    w = workermod.Worker("ws://127.0.0.1:9", "t", mid, "host", "ws://127.0.0.1:9", "t")
    w._test_pk = pk
    assert w.identity and w.passkey_verify, "E2E init failed (cryptography missing?)"

    print("flap recovery (ClientAuth from a new mobile id):")
    ca, keys = client_auth_for(w, mid, "mA")
    to, done = deliver_auth(w, "mB", ca)          # socket flapped: mA is gone, mB answers
    check("handshake completes by challenge match", done.get("t") == "e2e.done")
    expect = e2e.hmac.new(keys["kc_w"], b"fleet-e2e/1 server-finished", e2e.hashlib.sha256).digest()
    check("done carries a valid server-finished", done.get("cf_w") and
          e2e.hmac.compare_digest(expect, e2e.b64u_dec(done["cf_w"])))
    check("session attached to the NEW id", "mB" in w.e2e_sessions and "mA" not in w.e2e_sessions)
    check("resume material persisted", workermod.RESUME_FILE.exists())

    print("lost-done recovery (duplicate ClientAuth):")
    to, done2 = deliver_auth(w, "mC", ca)         # our done to mB was lost; mC re-sends
    check("duplicate answered from the done-cache", done2.get("t") == "e2e.done"
          and done2.get("cf_w") == done.get("cf_w"))
    check("session re-attached to the newest id", w.e2e_sessions.get("mC") is w.e2e_sessions.get("mB"))

    print("still fail-closed:")
    ca2, _ = client_auth_for(w, mid, "mD")
    bogus = json.loads(json.dumps(ca2))
    cdj = json.loads(e2e.b64u_dec(bogus["assertion"]["clientDataJSON"]))
    cdj["challenge"] = e2e.b64u(b"\x00" * 32)     # unknown challenge
    bogus["assertion"]["clientDataJSON"] = e2e.b64u(json.dumps(cdj, separators=(",", ":")).encode())
    to, err = deliver_auth(w, "mE", bogus)
    check("unknown challenge from a new id rejected", err.get("t") == "e2e.err")

    print("normal same-id path unchanged:")
    ca3, _ = client_auth_for(w, mid, "mF")
    to, done3 = deliver_auth(w, "mF", ca3)
    check("same-id handshake still completes", done3.get("t") == "e2e.done")

    if FAILED:
        print(f"\nFAILED: {len(FAILED)}"); sys.exit(1)
    print("\nPASSED: worker survives the Face-ID socket flap without a second passkey")


if __name__ == "__main__":
    main()
