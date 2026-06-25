#!/usr/bin/env python3
"""test_e2e.py — the fleet-e2e/1 handshake + record layer, including a REAL
synthesized WebAuthn assertion verified through the worker's actual passkey
verifier (webauthn.py + sign-count + require-UV). Covers the happy path and the
failure modes an auditor cares about. Exits non-zero on any failure.
"""
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

import e2e
import worker as workermod

RP_ID = "h.atg.link"
ORIGIN = "https://h.atg.link"
FAILED = []


def check(name, cond):
    print(("  ✓ " if cond else "  ✗ ") + name)
    if not cond:
        FAILED.append(name)


class FakePasskey:
    """A software ES256 authenticator — stands in for Face ID in tests."""

    def __init__(self):
        self.priv = ec.generate_private_key(ec.SECP256R1())
        n = self.priv.public_key().public_numbers()
        self.x, self.y = n.x, n.y
        self.cred_id = os.urandom(16)
        self.count = 0

    def record(self):
        return {"id": e2e.b64u(self.cred_id), "x": format(self.x, "064x"),
                "y": format(self.y, "064x"), "sign_count": 0}

    def assertion(self, challenge, uv=True, count=None, origin=ORIGIN, rp_id=RP_ID):
        self.count = (self.count + 1) if count is None else count
        client = json.dumps({"type": "webauthn.get", "challenge": e2e.b64u(challenge),
                             "origin": origin}, separators=(",", ":")).encode()
        flags = 0x01 | (0x04 if uv else 0)
        auth = hashlib.sha256(rp_id.encode()).digest() + bytes([flags]) + self.count.to_bytes(4, "big")
        sig = self.priv.sign(auth + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256()))
        return {"credentialId": e2e.b64u(self.cred_id), "clientDataJSON": e2e.b64u(client),
                "authenticatorData": e2e.b64u(auth), "signature": e2e.b64u(sig)}


def make_worker_verifier(passkey):
    """The REAL worker verifier, pointed at a temp passkeys file with this cred."""
    f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump([passkey.record()], f)
    f.close()
    workermod.PASSKEYS_FILE = Path(f.name)
    return workermod.make_passkey_verifier()


def run_handshake(idp, pk, verify, mid="austin-laptop", seen=None,
                  assertion_for=None, tamper_shello=None):
    """Drive a full handshake; assertion_for/tamper_shello hook the attacks."""
    seen = seen if seen is not None else set()
    W = e2e.WorkerHandshake(idp, mid, verify, seen)
    C = e2e.ClientHandshake(mid, e2e.pub_raw(idp.public_key()), lambda ch: None)
    hello = C.client_hello()
    sh = W.server_hello(hello)
    if tamper_shello:
        sh = tamper_shello(sh)
    # client verifies worker sig + derives keys; we inject the assertion ourselves
    epk_w = e2e.b64u_dec(sh["epk_w"]); n_w = e2e.b64u_dec(sh["n_w"])
    ik_w = e2e.b64u_dec(sh["ik_w"]); sig = e2e.b64u_dec(sh["sig_w"])
    if not e2e.verify_raw(ik_w, sig, e2e.sha256(e2e.lp(
            e2e.PROTO, mid.encode(), C.epk_m, C.n_m, epk_w, n_w, ik_w))):
        raise e2e.E2EError("worker-sig")          # client aborts (tampered ServerHello)
    Z = e2e.ecdh(C.eph, epk_w)
    keys = e2e.key_schedule(Z, mid, C.epk_m, C.n_m, epk_w, n_w, ik_w)
    challenge = keys["webauthn_challenge"]
    assertion = (assertion_for or pk.assertion)(challenge)
    cf_m = e2e.hmac.new(keys["kc_m"], b"fleet-e2e/1 client-finished", e2e.hashlib.sha256).digest()
    done, wsess = W.finish({"assertion": assertion, "cf_m": e2e.b64u(cf_m)})
    expect = e2e.hmac.new(keys["kc_w"], b"fleet-e2e/1 server-finished", e2e.hashlib.sha256).digest()
    assert e2e.hmac.compare_digest(expect, e2e.b64u_dec(done["cf_w"]))
    return e2e.Session(keys, "mobile"), wsess


def main():
    idp = e2e.gen_ec()
    pk = FakePasskey()
    verify = make_worker_verifier(pk)

    print("happy path:")
    csess, wsess = run_handshake(idp, pk, verify)
    check("full handshake with a real WebAuthn assertion", csess.k_send == wsess.k_recv)
    rec = csess.seal(e2e.KIND_JSON, b'{"type":"input","data":"ls\\n"}')
    kind, pt = wsess.open(rec)
    check("worker decrypts an mobile control frame", kind == e2e.KIND_JSON and b"ls" in pt)
    back = wsess.seal(e2e.KIND_BIN, b"\x1b[2J terminal bytes")
    k2, pt2 = csess.open(back)
    check("mobile decrypts a worker PTY record", k2 == e2e.KIND_BIN and b"terminal" in pt2)

    print("record layer:")
    r = csess.seal(e2e.KIND_JSON, b"x")
    wsess.open(r)
    check("replayed record rejected", _raises(lambda: wsess.open(r)))
    a = csess.seal(e2e.KIND_JSON, b"a"); b = csess.seal(e2e.KIND_JSON, b"b")
    wsess.open(b)
    check("reordered (older seq) record rejected", _raises(lambda: wsess.open(a)))
    bad = bytearray(csess.seal(e2e.KIND_JSON, b"y")); bad[-1] ^= 1
    check("tampered ciphertext rejected", _raises(lambda: wsess.open(bytes(bad))))

    print("authentication failures:")
    check("wrong-challenge assertion rejected",
          _raises(lambda: run_handshake(idp, pk, verify,
                  assertion_for=lambda ch: pk.assertion(os.urandom(32)))))
    check("assertion without user-verification (no biometric) rejected",
          _raises(lambda: run_handshake(idp, pk, verify,
                  assertion_for=lambda ch: pk.assertion(ch, uv=False))))
    check("non-incrementing sign-count (cloned authenticator) rejected",
          _raises(lambda: run_handshake(idp, pk, verify,
                  assertion_for=lambda ch: pk.assertion(ch, count=1))))   # count stuck at 1
    check("tampered ServerHello (worker sig) aborts the client",
          _raises(lambda: run_handshake(idp, pk, verify,
                  tamper_shello=lambda sh: dict(sh, epk_w=e2e.b64u(b"\x04" + os.urandom(64))))))
    check("wrong worker identity (pin mismatch) aborts the client",
          _raises(lambda: _pin_attack(pk, verify)))
    check("downgraded proto rejected by worker",
          _raises(lambda: _downgrade(idp, pk, verify)))

    print("curve / encoding validation:")
    check("off-curve public point rejected by load_pub",
          _raises(lambda: e2e.load_pub(b"\x04" + b"\x01" * 64)))
    check("point-at-infinity / wrong-length point rejected",
          _raises(lambda: e2e.load_pub(b"\x00")))
    check("ServerHello with an off-curve mobile ephemeral fails the handshake",
          _raises(lambda: e2e.WorkerHandshake(idp, "austin-laptop", verify, set()).server_hello(
              {"epk_m": e2e.b64u(b"\x04" + b"\x02" * 64), "n_m": e2e.b64u(os.urandom(32)),
               "proto": "fleet-e2e/1"})))

    print("session expiry:")
    csess2, wsess2 = run_handshake(idp, pk, verify)
    future = wsess2.established + e2e.MAX_TTL + 1
    rec2 = csess2.seal(e2e.KIND_JSON, b"late")
    check("record past the hard TTL rejected", _raises(lambda: wsess2.open(rec2, now=future)))

    print("batched passkey (one Face ID, several channels):")
    # Fresh authenticator + passkey file: an Apple platform authenticator (Face ID)
    # always reports sign_count 0, and the worker's stored count stays 0, so one
    # assertion satisfies every channel. (Earlier tests bumped the shared file's
    # count above 0, which would defeat the 0/0 rule — so the batch world is clean.)
    pk_b = FakePasskey()
    verify_b = make_worker_verifier(pk_b)
    sessions, assertion, ths = run_batch(idp, pk_b, verify_b, n=3)
    check("one assertion opens all 3 channels",
          len(sessions) == 3 and all(cs.k_send == ws.k_recv for cs, ws in sessions))
    for cs, ws in sessions:
        rb = cs.seal(e2e.KIND_JSON, b'{"type":"input"}')
        ws.open(rb)   # each channel is independently usable (own keys), raises if not
    check("each batched channel has its own working keys", True)
    # The security property: a hostile relay must not be able to ride the human's
    # one assertion onto a channel the human never saw on the unlock screen.
    def _inject(add_th_to_set):
        mid = "evil-extra"
        W = e2e.WorkerHandshake(idp, mid, verify_b, set())
        C = e2e.ClientHandshake(mid, e2e.pub_raw(idp.public_key()), lambda ch: None)
        sh = W.server_hello(C.client_hello())
        epk_w = e2e.b64u_dec(sh["epk_w"]); n_w = e2e.b64u_dec(sh["n_w"]); ik_w = e2e.b64u_dec(sh["ik_w"])
        keys = e2e.key_schedule(e2e.ecdh(C.eph, epk_w), mid, C.epk_m, C.n_m, epk_w, n_w, ik_w)
        lst = [e2e.b64u(t) for t in ths] + ([e2e.b64u(keys["Th"])] if add_th_to_set else [])
        cf_m = e2e.hmac.new(keys["kc_m"], b"fleet-e2e/1 client-finished", e2e.hashlib.sha256).digest()
        W.finish({"assertion": assertion, "cf_m": e2e.b64u(cf_m), "batch": lst})
    check("relay can't ride the assertion onto an unseen channel (Th not in set)",
          _raises(lambda: _inject(False)))     # membership check refuses
    check("relay can't smuggle the channel into the set (challenge no longer matches)",
          _raises(lambda: _inject(True)))      # adding the Th changes the committed set → assertion invalid

    if FAILED:
        print("\nFAILURES:", FAILED)
        sys.exit(1)
    print("\nPASSED: fleet-e2e/1 handshake + records + passkey checks all hold")


def _raises(fn):
    try:
        fn(); return False
    except Exception:
        return True


def run_batch(idp, pk, verify, n=3):
    """The batched ceremony: N concurrent handshakes share ONE passkey assertion
    (one Face ID). Returns (sessions, assertion, ths). Uses sign_count 0 to model an
    Apple platform authenticator (Face ID), which always reports 0; real machines
    also keep independent counters, so one assertion satisfies each worker."""
    seen, hs = set(), []
    for i in range(n):
        mid = f"machine-{i}"
        W = e2e.WorkerHandshake(idp, mid, verify, seen)
        C = e2e.ClientHandshake(mid, e2e.pub_raw(idp.public_key()), lambda ch: None)
        sh = W.server_hello(C.client_hello())
        epk_w = e2e.b64u_dec(sh["epk_w"]); n_w = e2e.b64u_dec(sh["n_w"]); ik_w = e2e.b64u_dec(sh["ik_w"])
        keys = e2e.key_schedule(e2e.ecdh(C.eph, epk_w), mid, C.epk_m, C.n_m, epk_w, n_w, ik_w)
        hs.append((W, keys))
    ths = [keys["Th"] for (_, keys) in hs]
    assertion = pk.assertion(e2e.batch_challenge(ths), count=0)   # Apple platform authenticator → 0
    sessions = []
    for (W, keys) in hs:
        cf_m = e2e.hmac.new(keys["kc_m"], b"fleet-e2e/1 client-finished", e2e.hashlib.sha256).digest()
        done, wsess = W.finish({"assertion": assertion, "cf_m": e2e.b64u(cf_m),
                                "batch": [e2e.b64u(t) for t in ths]})
        sessions.append((e2e.Session(keys, "mobile"), wsess))
    return sessions, assertion, ths


def _pin_attack(pk, verify):
    # client pinned to idp1, but a different worker identity answers
    idp1, idp2 = e2e.gen_ec(), e2e.gen_ec()
    mid = "austin-laptop"
    W = e2e.WorkerHandshake(idp2, mid, verify, set())
    C = e2e.ClientHandshake(mid, e2e.pub_raw(idp1.public_key()), lambda ch: None)
    C.client_auth(W.server_hello(C.client_hello()))   # raises pin


def _downgrade(idp, pk, verify):
    mid = "austin-laptop"
    W = e2e.WorkerHandshake(idp, mid, verify, set())
    W.server_hello({"epk_m": e2e.b64u(b"\x04" + os.urandom(64)),
                    "n_m": e2e.b64u(os.urandom(32)), "proto": "fleet-e2e/2"})  # raises proto


if __name__ == "__main__":
    main()
