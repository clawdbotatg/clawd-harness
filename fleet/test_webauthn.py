#!/usr/bin/env python3
"""Self-test for webauthn.py — pure stdlib, no node/browser.

The fixtures were generated with Node WebCrypto: a real ECDSA P-256 key, a
spec-shaped registration (attestationObject + COSE key) and assertion
(authenticatorData + clientDataJSON + DER signature) for rpId h.atg.link. Only
public material is embedded (pubkey, signature, data) — never a private key.

Run: python3 relay/test_webauthn.py
"""
import sys

import webauthn

RP = "h.atg.link"
ORIGIN = "https://h.atg.link"

REG_ATT = "o2NmbXRkbm9uZWdhdHRTdG10oGhhdXRoRGF0YViU6d5SZxJ2b2rIMiBwEHMIpeuB30WxMP2Ypf1XX628DYhFAAAAAAAAAAAAAAAAAAAAAAAAAAAAEGNsYXdkLWZsZWV0LXRlc3SlAQIDJiABIVggMl1Dm4bP5C8E54-z1oj99Fc3yJuWic4tv3y5vWTmJtEiWCA_Ed9nuRQ5EXbn-eQEVxs9Hm8N-uRv3t_Ofe3ea6Uibg"
REG_CLIENT = "eyJ0eXBlIjoid2ViYXV0aG4uY3JlYXRlIiwiY2hhbGxlbmdlIjoiY21WbkxXTm9ZV3hzWlc1blpTMHhNak0iLCJvcmlnaW4iOiJodHRwczovL2guYXRnLmxpbmsifQ"
REG_CHAL = "cmVnLWNoYWxsZW5nZS0xMjM"
CRED_ID = "Y2xhd2QtZmxlZXQtdGVzdA"
PX = 0x325D439B86CFE42F04E78FB3D688FDF45737C89B9689CE2DBF7CB9BD64E626D1
PY = 0x3F11DF67B914391176E7F9E404571B3D1E6F0DFAE46FDEDFCE7DEDDE6BA5226E

AS_CLIENT = "eyJ0eXBlIjoid2ViYXV0aG4uZ2V0IiwiY2hhbGxlbmdlIjoiWVhOelpYSjBMV05vWVd4c1pXNW5aUzAwTlRZIiwib3JpZ2luIjoiaHR0cHM6Ly9oLmF0Zy5saW5rIn0"
AS_AUTH = "6d5SZxJ2b2rIMiBwEHMIpeuB30WxMP2Ypf1XX628DYgFAAAAAQ"
AS_SIG = "MEUCIFwPJYe3DpAFAJVZB3zgf4fLxAvz_QNTrkeG1r5i535-AiEAm89nMWKo0OpRoC2cU385pSzn9K5OehFIim4gdeZ3qWQ"
AS_CHAL = "YXNzZXJ0LWNoYWxsZW5nZS00NTY"


def main():
    checks = []

    def check(name, cond):
        checks.append((name, bool(cond)))

    # registration: extract credential id + public key
    reg = webauthn.parse_registration(REG_ATT, REG_CLIENT, REG_CHAL, RP, ORIGIN)
    check("registration extracts cred id", reg["credential_id"] == CRED_ID)
    check("registration extracts pubkey", reg["pubkey"] == (PX, PY))
    pubkey = reg["pubkey"]

    # assertion: valid
    ok, reason = webauthn.verify_assertion(pubkey, AS_CLIENT, AS_AUTH, AS_SIG, AS_CHAL, RP, ORIGIN)
    check("assertion verifies", ok and reason == "ok")

    # negatives
    check("reject wrong challenge",
          not webauthn.verify_assertion(pubkey, AS_CLIENT, AS_AUTH, AS_SIG, "nope", RP, ORIGIN)[0])
    check("reject wrong origin",
          not webauthn.verify_assertion(pubkey, AS_CLIENT, AS_AUTH, AS_SIG, AS_CHAL, RP, "https://evil.com")[0])
    check("reject wrong rpId",
          not webauthn.verify_assertion(pubkey, AS_CLIENT, AS_AUTH, AS_SIG, AS_CHAL, "evil.com", ORIGIN)[0])
    check("reject wrong pubkey",
          not webauthn.verify_assertion((PX ^ 1, PY), AS_CLIENT, AS_AUTH, AS_SIG, AS_CHAL, RP, ORIGIN)[0])

    # tamper a byte of authData (within its 37 bytes) → signature must fail
    ad = bytearray(webauthn.b64u_decode(AS_AUTH))
    ad[35] ^= 0x01  # a sign-count byte
    check("reject tampered authData",
          not webauthn.verify_assertion(pubkey, AS_CLIENT, webauthn.b64u_encode(bytes(ad)), AS_SIG, AS_CHAL, RP, ORIGIN)[0])

    # tamper the signature → fail
    sig = bytearray(webauthn.b64u_decode(AS_SIG))
    sig[-1] ^= 0x01
    check("reject tampered signature",
          not webauthn.verify_assertion(pubkey, AS_CLIENT, AS_AUTH, webauthn.b64u_encode(bytes(sig)), AS_CHAL, RP, ORIGIN)[0])

    ok_all = True
    for name, passed in checks:
        print(f"  {'✓' if passed else '✗ FAIL'} {name}")
        ok_all = ok_all and passed
    print("PASSED" if ok_all else "FAILED")
    sys.exit(0 if ok_all else 1)


if __name__ == "__main__":
    main()
