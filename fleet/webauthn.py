#!/usr/bin/env python3
"""webauthn.py — verify a passkey (WebAuthn) registration + assertion in PURE
Python stdlib. No deps (matches the rest of clawd-fleet).

The fleet's second auth factor is a passkey: the browser proves possession of a
hardware-backed credential (Touch ID / Face ID / a security key) via the native
`navigator.credentials` API. The private key never leaves the device's secure
enclave — phishing-resistant and non-exportable. The relay only ever *verifies*.

Two primitives aren't in the stdlib and are implemented below:
  - ECDSA-P256 (secp256r1 / ES256) signature verification
  - a minimal CBOR decoder (for the attestationObject + COSE public key)
SHA-256 and base64url come from hashlib/base64.

Public API:
  parse_registration(att_obj_b64u, client_data_b64u, challenge_b64u, rp_id, origin)
      -> {"credential_id": b64u, "pubkey": (x, y), "sign_count": int}   (raises on invalid)
  verify_assertion(pubkey, client_data_b64u, auth_data_b64u, sig_b64u,
                   challenge_b64u, rp_id, origin) -> (ok: bool, reason: str)

Both enforce: clientData.type, challenge match, origin match, rpIdHash == SHA256(rpId),
User-Present flag. (User-Verified is checked by verify_assertion when require_uv=True.)
"""
import base64
import hashlib
import json

# --------------------------------------------------------------------------- #
# base64url
# --------------------------------------------------------------------------- #
def b64u_decode(s: str) -> bytes:
    if isinstance(s, bytes):
        s = s.decode("ascii")
    s = s.replace("-", "+").replace("_", "/")
    s += "=" * (-len(s) % 4)
    return base64.b64decode(s)


def b64u_encode(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


# --------------------------------------------------------------------------- #
# minimal CBOR decoder (enough for attestationObject + COSE_Key)
# major types: 0 uint, 1 negint, 2 bytes, 3 text, 4 array, 5 map
# --------------------------------------------------------------------------- #
def _cbor_load(data: bytes, i: int):
    b = data[i]
    mt = b >> 5
    ai = b & 0x1F
    i += 1
    if ai < 24:
        val = ai
    elif ai == 24:
        val = data[i]; i += 1
    elif ai == 25:
        val = int.from_bytes(data[i:i + 2], "big"); i += 2
    elif ai == 26:
        val = int.from_bytes(data[i:i + 4], "big"); i += 4
    elif ai == 27:
        val = int.from_bytes(data[i:i + 8], "big"); i += 8
    else:
        raise ValueError("cbor: unsupported additional info")

    if mt == 0:
        return val, i
    if mt == 1:
        return -1 - val, i
    if mt == 2:
        return data[i:i + val], i + val
    if mt == 3:
        return data[i:i + val].decode("utf-8"), i + val
    if mt == 4:
        arr = []
        for _ in range(val):
            v, i = _cbor_load(data, i)
            arr.append(v)
        return arr, i
    if mt == 5:
        m = {}
        for _ in range(val):
            k, i = _cbor_load(data, i)
            v, i = _cbor_load(data, i)
            m[k] = v
        return m, i
    raise ValueError(f"cbor: unsupported major type {mt}")


def cbor_decode(data: bytes):
    val, _ = _cbor_load(data, 0)
    return val


# --------------------------------------------------------------------------- #
# ECDSA over NIST P-256 (secp256r1) — verify only
# --------------------------------------------------------------------------- #
_P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
_A = _P - 3
_B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
_GX = 0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296
_GY = 0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5
_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551


def _inv(x, m):
    return pow(x, m - 2, m)


def _pt_add(p, q):
    if p is None:
        return q
    if q is None:
        return p
    x1, y1 = p
    x2, y2 = q
    if x1 == x2 and (y1 + y2) % _P == 0:
        return None
    if x1 == x2 and y1 == y2:
        m = (3 * x1 * x1 + _A) * _inv(2 * y1, _P) % _P
    else:
        m = (y2 - y1) * _inv((x2 - x1) % _P, _P) % _P
    x3 = (m * m - x1 - x2) % _P
    y3 = (m * (x1 - x3) - y1) % _P
    return (x3, y3)


def _pt_mul(k, p):
    r = None
    k %= _N
    while k:
        if k & 1:
            r = _pt_add(r, p)
        p = _pt_add(p, p)
        k >>= 1
    return r


def _on_curve(q):
    x, y = q
    return (y * y - (x * x * x + _A * x + _B)) % _P == 0


def _der_parse_sig(sig: bytes):
    """ASN.1 DER ECDSA-Sig-Value: SEQUENCE { INTEGER r, INTEGER s }. Strict and
    fully-consuming — short-form lengths only (P-256 sigs are <128 bytes), exact
    length match, no trailing bytes. Rejects non-canonical encodings rather than
    silently ignoring them."""
    if len(sig) < 8 or sig[0] != 0x30:
        raise ValueError("der: not a sequence")
    seqlen = sig[1]
    if seqlen & 0x80 or seqlen != len(sig) - 2:
        raise ValueError("der: bad sequence length")
    i = 2
    if sig[i] != 0x02:
        raise ValueError("der: expected integer r")
    rlen = sig[i + 1]
    if rlen == 0 or rlen & 0x80 or i + 2 + rlen + 2 > len(sig):
        raise ValueError("der: bad r length")
    i += 2
    r = int.from_bytes(sig[i:i + rlen], "big"); i += rlen
    if sig[i] != 0x02:
        raise ValueError("der: expected integer s")
    slen = sig[i + 1]
    if slen == 0 or slen & 0x80 or i + 2 + slen != len(sig):
        raise ValueError("der: bad s length / trailing bytes")
    i += 2
    s = int.from_bytes(sig[i:i + slen], "big")
    return r, s


def verify_es256(pubkey, msg_hash: bytes, sig_der: bytes) -> bool:
    """ECDSA-P256 verify: pubkey=(x,y), msg_hash=SHA-256 digest bytes, DER sig."""
    try:
        qx, qy = pubkey
        if not _on_curve((qx, qy)):
            return False
        r, s = _der_parse_sig(sig_der)
        if not (1 <= r < _N and 1 <= s < _N):
            return False
        z = int.from_bytes(msg_hash, "big")
        w = _inv(s, _N)
        u1 = (z * w) % _N
        u2 = (r * w) % _N
        pt = _pt_add(_pt_mul(u1, (_GX, _GY)), _pt_mul(u2, (qx, qy)))
        if pt is None:
            return False
        return (pt[0] % _N) == r
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# COSE EC2 public key (from CBOR) → (x, y)
# --------------------------------------------------------------------------- #
def cose_to_pubkey(cose: dict):
    # kty(1)==2 EC2, alg(3)==-7 ES256, crv(-1)==1 P-256, x(-2), y(-3)
    if cose.get(1) != 2 or cose.get(3) != -7 or cose.get(-1) != 1:
        raise ValueError("cose: not an ES256 P-256 EC2 key")
    x = int.from_bytes(cose[-2], "big")
    y = int.from_bytes(cose[-3], "big")
    if not _on_curve((x, y)):
        raise ValueError("cose: point not on curve")
    return (x, y)


# --------------------------------------------------------------------------- #
# authenticatorData parsing + shared clientData checks
# --------------------------------------------------------------------------- #
FLAG_UP = 0x01  # user present
FLAG_UV = 0x04  # user verified
FLAG_AT = 0x40  # attested credential data included


def _check_client_data(client_data: bytes, expected_type, challenge_b64u, rp_id, origin):
    cd = json.loads(client_data.decode("utf-8"))
    if cd.get("type") != expected_type:
        return "bad clientData type"
    if cd.get("challenge") != challenge_b64u:
        return "challenge mismatch"
    # origin must be exactly our https origin (defends against phishing)
    if origin is not None and cd.get("origin") != origin:
        return "origin mismatch"
    return None


def _check_rpid_and_flags(auth_data: bytes, rp_id, require_uv):
    if len(auth_data) < 37:
        return "authData too short"
    if auth_data[:32] != hashlib.sha256(rp_id.encode()).digest():
        return "rpIdHash mismatch"
    flags = auth_data[32]
    if not (flags & FLAG_UP):
        return "user not present"
    if require_uv and not (flags & FLAG_UV):
        return "user not verified"
    return None


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def parse_registration(att_obj_b64u, client_data_b64u, challenge_b64u, rp_id, origin, require_uv=False):
    """Validate a navigator.credentials.create() result and extract the credential.
    Returns {"credential_id", "pubkey", "sign_count"}; raises ValueError on invalid."""
    client_data = b64u_decode(client_data_b64u)
    err = _check_client_data(client_data, "webauthn.create", challenge_b64u, rp_id, origin)
    if err:
        raise ValueError(err)

    att = cbor_decode(b64u_decode(att_obj_b64u))
    auth_data = att["authData"]
    err = _check_rpid_and_flags(auth_data, rp_id, require_uv)
    if err:
        raise ValueError(err)
    if not (auth_data[32] & FLAG_AT):
        raise ValueError("no attested credential data")

    # attestedCredentialData: aaguid(16) | credIdLen(2) | credId | COSEpubkey
    i = 37 + 16
    cred_len = int.from_bytes(auth_data[i:i + 2], "big"); i += 2
    cred_id = auth_data[i:i + cred_len]; i += cred_len
    cose = cbor_decode(auth_data[i:])
    pubkey = cose_to_pubkey(cose)
    sign_count = int.from_bytes(auth_data[33:37], "big")
    return {"credential_id": b64u_encode(cred_id), "pubkey": pubkey, "sign_count": sign_count}


def verify_assertion(pubkey, client_data_b64u, auth_data_b64u, sig_b64u,
                     challenge_b64u, rp_id, origin, require_uv=False):
    """Validate a navigator.credentials.get() result against a stored pubkey.
    Returns (ok, reason)."""
    try:
        client_data = b64u_decode(client_data_b64u)
        err = _check_client_data(client_data, "webauthn.get", challenge_b64u, rp_id, origin)
        if err:
            return (False, err)
        auth_data = b64u_decode(auth_data_b64u)
        err = _check_rpid_and_flags(auth_data, rp_id, require_uv)
        if err:
            return (False, err)
        # signed = authData || SHA256(clientDataJSON)
        signed = auth_data + hashlib.sha256(client_data).digest()
        msg_hash = hashlib.sha256(signed).digest()
        if not verify_es256(pubkey, msg_hash, b64u_decode(sig_b64u)):
            return (False, "bad signature")
        return (True, "ok")
    except Exception as e:
        return (False, f"error: {e}")
