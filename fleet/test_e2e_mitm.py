#!/usr/bin/env python3
"""test_e2e_mitm.py — put a MALICIOUS relay between the mobile and the worker and
prove every attack fails closed: it cannot read the channel, cannot substitute
ephemerals, cannot impersonate either side, and cannot inject records. This is
the property that lets us treat the relay as untrusted. Exits non-zero on any
hole. Reuses the FakePasskey + worker verifier from test_e2e.py.
"""
import os
import sys

import e2e
from test_e2e import FakePasskey, make_worker_verifier, run_handshake

FAILED = []


def check(name, cond):
    print(("  ✓ " if cond else "  ✗ ") + name)
    if not cond:
        FAILED.append(name)


def raises(fn):
    try:
        fn(); return False
    except Exception:
        return True


def main():
    idp = e2e.gen_ec()              # the real worker identity
    ik_w = e2e.pub_raw(idp.public_key())
    pk = FakePasskey()
    verify = make_worker_verifier(pk)
    mid = "austin-laptop"

    # Establish one honest session to use as the victim for read/inject tests.
    csess, wsess = run_handshake(idp, pk, verify)

    print("confidentiality (relay sees only ciphertext):")
    record = csess.seal(e2e.KIND_JSON, b'{"type":"input","data":"secret command\\n"}')
    # The relay observed every handshake message but holds no ephemeral private key.
    # Its best attempt — ECDH with a freshly minted key — yields unrelated key bytes.
    relay_eph = e2e.gen_ec()
    relay_guess = e2e.key_schedule(e2e.ecdh(relay_eph, e2e.pub_raw(relay_eph.public_key())),
                                   mid, b"?", b"?", b"?", b"?", ik_w)
    forged = e2e.Session(relay_guess, "worker")
    check("relay cannot decrypt a captured record", raises(lambda: forged.open(record)))
    check("real worker still decrypts it", wsess.open(record)[0] == e2e.KIND_JSON)

    print("integrity (relay cannot inject/forge a record):")
    inject = forged.seal(e2e.KIND_JSON, b'{"type":"input","data":"rm -rf /\\n"}')
    check("relay-forged record rejected by the worker", raises(lambda: wsess.open(inject)))

    print("ephemeral substitution:")
    # (a) relay swaps the mobile's ephemeral toward the worker → the worker's
    # expected passkey challenge no longer matches what the mobile signed.
    check("swap mobile ephemeral → worker rejects the assertion",
          raises(lambda: _swap_epk_m(idp, pk, verify, mid)))
    # (b) relay swaps the worker's ephemeral toward the mobile → the worker's
    # signature over the transcript no longer verifies.
    check("swap worker ephemeral → mobile aborts on the signature",
          raises(lambda: run_handshake(idp, pk, verify,
                 tamper_shello=lambda sh: dict(sh, epk_w=e2e.b64u(b"\x04" + os.urandom(64))))))

    print("impersonation:")
    # (c) relay tries to be the worker to the mobile with its OWN identity key.
    # A mobile that has pinned the real worker refuses.
    check("relay impersonating the worker (own identity) → pin mismatch",
          raises(lambda: _impersonate_worker(pk, verify, ik_w, mid)))
    # (d) relay tries to be the mobile to the worker: it has no passkey, so it
    # cannot produce a valid channel-bound assertion.
    check("relay impersonating the mobile (no passkey) → worker rejects",
          raises(lambda: _impersonate_mobile(idp, verify, mid)))

    if FAILED:
        print("\nMITM HOLES:", FAILED)
        sys.exit(1)
    print("\nPASSED: a malicious relay is reduced to denial-of-service")


def _swap_epk_m(idp, pk, verify, mid):
    """Worker sees a relay-substituted mobile ephemeral; mobile signs the real one."""
    W = e2e.WorkerHandshake(idp, mid, verify, set())
    C = e2e.ClientHandshake(mid, e2e.pub_raw(idp.public_key()), lambda ch: None)
    hello = C.client_hello()
    relay_eph = e2e.gen_ec()
    hello_evil = dict(hello, epk_m=e2e.b64u(e2e.pub_raw(relay_eph.public_key())))  # swap
    sh = W.server_hello(hello_evil)
    # mobile derives keys/challenge from ITS real ephemeral + the worker's epk_w
    epk_w = e2e.b64u_dec(sh["epk_w"]); n_w = e2e.b64u_dec(sh["n_w"]); ikw = e2e.b64u_dec(sh["ik_w"])
    Z = e2e.ecdh(C.eph, epk_w)
    keys = e2e.key_schedule(Z, mid, C.epk_m, C.n_m, epk_w, n_w, ikw)
    assertion = pk.assertion(keys["webauthn_challenge"])
    cf_m = e2e.hmac.new(keys["kc_m"], b"fleet-e2e/1 client-finished", e2e.hashlib.sha256).digest()
    W.finish({"assertion": assertion, "cf_m": e2e.b64u(cf_m)})   # worker: confirm/challenge mismatch


def _impersonate_worker(pk, verify, real_ik_w, mid):
    """Relay answers ServerHello with its own identity; mobile pinned the real one."""
    idp_relay = e2e.gen_ec()
    W = e2e.WorkerHandshake(idp_relay, mid, verify, set())
    C = e2e.ClientHandshake(mid, real_ik_w, lambda ch: None)   # pinned to the REAL worker
    C.client_auth(W.server_hello(C.client_hello()))            # raises pin


def _impersonate_mobile(idp, verify, mid):
    """Relay (no passkey) tries to complete the handshake as the mobile."""
    W = e2e.WorkerHandshake(idp, mid, verify, set())
    C = e2e.ClientHandshake(mid, e2e.pub_raw(idp.public_key()), lambda ch: None)
    sh = W.server_hello(C.client_hello())
    epk_w = e2e.b64u_dec(sh["epk_w"]); n_w = e2e.b64u_dec(sh["n_w"]); ikw = e2e.b64u_dec(sh["ik_w"])
    Z = e2e.ecdh(C.eph, epk_w)
    keys = e2e.key_schedule(Z, mid, C.epk_m, C.n_m, epk_w, n_w, ikw)
    # forge an assertion with an attacker key (not enrolled) over the right challenge
    forge = FakePasskey()
    assertion = forge.assertion(keys["webauthn_challenge"])
    cf_m = e2e.hmac.new(keys["kc_m"], b"fleet-e2e/1 client-finished", e2e.hashlib.sha256).digest()
    W.finish({"assertion": assertion, "cf_m": e2e.b64u(cf_m)})   # unknown credential → reject


if __name__ == "__main__":
    main()
