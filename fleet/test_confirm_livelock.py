#!/usr/bin/env python3
"""test_confirm_livelock.py — the `confirm` passkey livelock regression gate.

The 2026-07-04 storm: 11 consecutive `E2E auth failed for m62: confirm` in the
worker log — the user paid a passkey per attempt and every one was rejected.
Mechanism: the worker files ONE in-flight handshake per mobile id
(e2e_hs[frm]); a second hello from the same id silently clobbers the first, so
the first attempt's ClientAuth — cryptographically valid, passkey paid — is
checked against the WRONG handshake instance and dies on the client-finished
HMAC (`confirm`) before the challenge-match rescue is even consulted (that
rescue only runs when the slot is EMPTY). The client immediately starts a new
attempt whose hello re-clobbers, sustaining a one-passkey-per-lap livelock
until the socket rotates.

What the fix (challenge-keyed handshake store in worker._e2e_hello/_e2e_auth)
makes true, and what this test asserts:
  1. a ClientAuth completes the handshake its assertion is challenge-bound to,
     even when a newer hello from the same mobile id has since arrived;
  2. that newer handshake SURVIVES and its own ClientAuth also completes —
     neither paid passkey is wasted.

Was RED against the mobile-id-keyed store (it reproduced the storm exactly:
`confirm`, then `no handshake` — both passkeys wasted); guards the fix now.
Exits non-zero if the livelock ever comes back.
"""
import json
import sys
import tempfile
from pathlib import Path

import e2e
import worker as workermod
from test_e2e import FakePasskey, check, FAILED
from test_worker_flap import client_auth_for, deliver_auth


def main():
    mid = "livelock-test-machine"
    pk = FakePasskey()
    tmp = Path(tempfile.mkdtemp())
    workermod.PASSKEYS_FILE = tmp / "passkeys.json"
    workermod.PASSKEYS_FILE.write_text(json.dumps([pk.record()]))
    workermod.WORKER_ID_FILE = tmp / "worker_id.json"
    workermod.RESUME_FILE = tmp / "resume.json"
    w = workermod.Worker("ws://127.0.0.1:9", "t", mid, "host", "ws://127.0.0.1:9", "t")
    w._test_pk = pk
    assert w.identity and w.passkey_verify, "E2E init failed (cryptography missing?)"

    print("the m62 storm: two overlapping attempts on ONE mobile id:")
    # Attempt A: hello filed under m62; the user is now paying its passkey.
    ca_A, keys_A = client_auth_for(w, mid, "m62")
    # Attempt B: a retry (cooldown-bypassing hsend / zombie replacement) sends a
    # second hello from the SAME id before A's ClientAuth lands — the clobber.
    ca_B, keys_B = client_auth_for(w, mid, "m62")

    # A's paid assertion arrives. Today: pops B's handshake, `confirm` fails,
    # e2e.err — the passkey is wasted. Required: complete A by challenge match.
    to, r_A = deliver_auth(w, "m62", ca_A)
    check("attempt A's paid assertion completes ITS OWN handshake",
          r_A.get("t") == "e2e.done")
    if r_A.get("t") == "e2e.done":
        expect = e2e.hmac.new(keys_A["kc_w"], b"fleet-e2e/1 server-finished",
                              e2e.hashlib.sha256).digest()
        check("A's done carries A's server-finished",
              r_A.get("cf_w") and e2e.hmac.compare_digest(expect, e2e.b64u_dec(r_A["cf_w"])))

    # B's assertion arrives next (the queued second prompt). It must ALSO land:
    # completing A must not have popped-and-discarded B's handshake.
    to, r_B = deliver_auth(w, "m62", ca_B)
    check("attempt B's paid assertion also completes (B's handshake survived A's)",
          r_B.get("t") == "e2e.done")
    if r_B.get("t") == "e2e.done":
        expect = e2e.hmac.new(keys_B["kc_w"], b"fleet-e2e/1 server-finished",
                              e2e.hashlib.sha256).digest()
        check("B's done carries B's server-finished",
              r_B.get("cf_w") and e2e.hmac.compare_digest(expect, e2e.b64u_dec(r_B["cf_w"])))

    # The zombie-error correlation echo (2026-07-09: a replaced attempt's stray
    # 'no handshake' err, landing on the phone's LIVE channel mid-resume, burned
    # the 24h resume material → an unearned Face ID, 2-3×/machine/day). The
    # client filters strays by matching `for` against its own attempt — so the
    # worker must name which attempt an auth error belongs to.
    ca_C, keys_C = client_auth_for(w, mid, "m62")
    to, r_dup = deliver_auth(w, "m62", ca_C)     # completes normally…
    check("attempt C completes", r_dup.get("t") == "e2e.done")
    ca_C["cf_m"] = e2e.b64u(b"\x00" * 32)        # …then a tampered replay of it:
    to, r_z = deliver_auth(w, "m62", ca_C)       # done-cache misses (cf_m differs) → err
    chal_C = e2e.b64u(keys_C["webauthn_challenge"])
    check("a 'no handshake' err names its attempt (for=challenge)",
          r_z.get("t") == "e2e.err" and r_z.get("for") == chal_C)
    to, r_r = None, None
    replies = []
    orig = w.reply
    w.reply = lambda t, m: replies.append((t, m))
    try:
        w._e2e_resume("m62", {"t": "e2e.resume", "id": "no-such-resume-id"})
    finally:
        w.reply = orig
    to, r_r = replies[-1]
    check("a 'resume' err names its attempt (for=resume id)",
          r_r.get("t") == "e2e.err" and r_r.get("error") == "resume"
          and r_r.get("for") == "no-such-resume-id")

    if FAILED:
        print(f"\nRED ({len(FAILED)} failing) — the confirm livelock is reproduced. "
              f"This is the regression gate for the challenge-first fix.")
        sys.exit(1)
    print("\nPASSED: overlapping same-id handshakes no longer waste paid passkeys")


if __name__ == "__main__":
    main()
