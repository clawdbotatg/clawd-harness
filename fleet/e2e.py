"""fleet/e2e.py — end-to-end channel crypto for the fleet (protocol fleet-e2e/1).

Implements the worker (and a reference client, for tests) side of the
passkey-bound authenticated key exchange + AES-GCM record layer specified in
docs/fleet/E2E-PROTOCOL.md. The browser implements the matching half in
index.html with WebCrypto; every byte string here must match it exactly.

Uses pyca/cryptography for ECDH / ECDSA / AES-GCM (audit-grade primitives);
HKDF and HMAC are stdlib (RFC 5869 / RFC 2104, HMAC-SHA256, C-backed). The
relay never imports this module — it routes ciphertext blindly.
"""
import os, json, hmac, hashlib, struct, base64, threading, time

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature, encode_dss_signature)
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

PROTO = b"fleet-e2e/1"
CURVE = ec.SECP256R1()

IDLE_TTL = int(os.environ.get("FLEET_E2E_IDLE_TTL", "600"))   # slide-on-activity
MAX_TTL  = int(os.environ.get("FLEET_E2E_MAX_TTL",  "3600"))  # hard ceiling

DIR_M2W = 0x4D  # 'M' — mobile→worker
DIR_W2M = 0x57  # 'W' — worker→mobile
KIND_JSON = 0x01
KIND_BIN  = 0x02


class E2EError(Exception):
    """Generic, opaque failure — callers MUST NOT leak which check failed."""


# ── encodings ────────────────────────────────────────────────────────────────
def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def b64u_dec(s):
    if isinstance(s, str):
        s = s.encode()
    return base64.urlsafe_b64decode(s + b"=" * (-len(s) % 4))


def lp(*parts):
    """Length-prefixed concat: each field as uint16-BE(len) ‖ bytes."""
    out = b""
    for p in parts:
        if len(p) > 0xFFFF:
            raise E2EError("field too long")
        out += struct.pack(">H", len(p)) + p
    return out


def sha256(b):
    return hashlib.sha256(b).digest()


# ── HKDF (RFC 5869, HMAC-SHA256) ─────────────────────────────────────────────
def hkdf_extract(salt, ikm):
    return hmac.new(salt, ikm, hashlib.sha256).digest()


def hkdf_expand(prk, info, length=32):
    out, t, i = b"", b"", 1
    while len(out) < length:
        t = hmac.new(prk, t + info + bytes([i]), hashlib.sha256).digest()
        out += t
        i += 1
    return out[:length]


# ── EC helpers ───────────────────────────────────────────────────────────────
def gen_ec():
    return ec.generate_private_key(CURVE)


def pub_raw(pub):
    return pub.public_bytes(serialization.Encoding.X962,
                            serialization.PublicFormat.UncompressedPoint)


def load_pub(raw):
    return ec.EllipticCurvePublicKey.from_encoded_point(CURVE, raw)


def ecdh(priv, peer_raw):
    return priv.exchange(ec.ECDH(), load_pub(peer_raw))  # 32-byte X coordinate


def sign_raw(priv, data):
    r, s = decode_dss_signature(priv.sign(data, ec.ECDSA(hashes.SHA256())))
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def verify_raw(pub_raw_bytes, sig_raw, data):
    if len(sig_raw) != 64:
        return False
    try:
        pub = load_pub(pub_raw_bytes)
        der = encode_dss_signature(int.from_bytes(sig_raw[:32], "big"),
                                   int.from_bytes(sig_raw[32:], "big"))
        pub.verify(der, data, ec.ECDSA(hashes.SHA256()))
        return True
    except Exception:
        return False


# ── worker long-term identity ────────────────────────────────────────────────
def load_or_create_identity(path):
    """Load (or generate + persist, chmod 600) the worker's ECDSA identity key."""
    if os.path.exists(path):
        with open(path) as f:
            pem = json.load(f)["priv_pem"].encode()
        return serialization.load_pem_private_key(pem, password=None)
    priv = gen_ec()
    pem = priv.private_bytes(serialization.Encoding.PEM,
                             serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"priv_pem": pem.decode()}, f)
    os.replace(tmp, path)
    os.chmod(path, 0o600)
    return priv


def fingerprint(pub_raw_bytes):
    """Short human-verifiable fingerprint of a worker identity public key."""
    s = base64.b32encode(sha256(pub_raw_bytes)).decode().rstrip("=")[:20]
    return " ".join(s[i:i + 5] for i in range(0, 20, 5))


# ── key schedule (§5) ────────────────────────────────────────────────────────
def key_schedule(Z, mid, epk_m, n_m, epk_w, n_w, ik_w):
    mid_b = mid.encode() if isinstance(mid, str) else mid
    salt = sha256(lp(n_m, n_w))
    Th = sha256(lp(PROTO, mid_b, epk_m, n_m, epk_w, n_w, ik_w))
    prk = hkdf_extract(salt, Z)

    def exp(label):
        return hkdf_expand(prk, label + Th, 32)

    return {
        "Th": Th,
        "k_m2w": exp(b"fleet-e2e/1 key m2w"),
        "k_w2m": exp(b"fleet-e2e/1 key w2m"),
        "iv_m2w": exp(b"fleet-e2e/1 iv m2w")[:4],
        "iv_w2m": exp(b"fleet-e2e/1 iv w2m")[:4],
        "kc_m": exp(b"fleet-e2e/1 confirm-m"),
        "kc_w": exp(b"fleet-e2e/1 confirm-w"),
        "webauthn_challenge": sha256(b"fleet-e2e/1 webauthn-challenge" + Th),
        # Resumption: a master secret + opaque id both sides derive. Lets a
        # reconnecting mobile re-attach within the TTL without a fresh passkey
        # (§7). The master never crosses the wire; the relay can't derive keys.
        "resume_master": exp(b"fleet-e2e/1 resume-master"),
        "resume_id": exp(b"fleet-e2e/1 resume-id")[:16],
    }


def _confirm(kc, tag):
    return hmac.new(kc, tag, hashlib.sha256).digest()


# ── resumption (§7): fresh per-resume keys from the master + a worker nonce ───
def resume_keys(resume_master, rn):
    """Derive a fresh directional key set for a resumed session. A new `rn` each
    resume → fresh keys → seq restarts at 0 safely (no nonce reuse)."""
    prk = hkdf_extract(rn, resume_master)
    return {
        "k_m2w": hkdf_expand(prk, b"fleet-e2e/1 rkey m2w"),
        "k_w2m": hkdf_expand(prk, b"fleet-e2e/1 rkey w2m"),
        "iv_m2w": hkdf_expand(prk, b"fleet-e2e/1 riv m2w")[:4],
        "iv_w2m": hkdf_expand(prk, b"fleet-e2e/1 riv w2m")[:4],
    }


def resume_confirm(resume_master, rn):
    """Worker→mobile proof it holds the same master (so the mobile trusts the rn)."""
    return hmac.new(resume_master, b"fleet-e2e/1 resume-confirm" + rn, hashlib.sha256).digest()


# ── record layer (§6) ────────────────────────────────────────────────────────
class Session:
    """An open E2E channel: directional AES-GCM keys + sequence + TTL."""

    def __init__(self, keys, role, now=None):
        if role == "worker":
            self.k_send, self.iv_send, self.dir_send = keys["k_w2m"], keys["iv_w2m"], DIR_W2M
            self.k_recv, self.iv_recv, self.dir_recv = keys["k_m2w"], keys["iv_m2w"], DIR_M2W
        elif role == "mobile":
            self.k_send, self.iv_send, self.dir_send = keys["k_m2w"], keys["iv_m2w"], DIR_M2W
            self.k_recv, self.iv_recv, self.dir_recv = keys["k_w2m"], keys["iv_w2m"], DIR_W2M
        else:
            raise E2EError("role")
        self._send = AESGCM(self.k_send)
        self._recv = AESGCM(self.k_recv)
        self.seq_send = 0
        self.seq_recv_max = -1
        now = now if now is not None else time.time()
        self.established = now
        self.idle_deadline = now + IDLE_TTL
        self.hard_deadline = now + MAX_TTL
        self._lock = threading.Lock()

    def expired(self, now=None):
        now = now if now is not None else time.time()
        return now > self.idle_deadline or now > self.hard_deadline

    def seal(self, kind, payload):
        with self._lock:
            seq = self.seq_send
            self.seq_send += 1
        nonce = self.iv_send + struct.pack(">Q", seq)
        aad = bytes([self.dir_send]) + struct.pack(">Q", seq)
        ct = self._send.encrypt(nonce, bytes([kind]) + payload, aad)
        return struct.pack(">Q", seq) + ct

    def open(self, record, now=None):
        if len(record) < 8 + 16:
            raise E2EError("short record")
        now = now if now is not None else time.time()
        if self.expired(now):
            raise E2EError("expired")
        seq = struct.unpack(">Q", record[:8])[0]
        with self._lock:
            if seq <= self.seq_recv_max:
                raise E2EError("replay/reorder")
        nonce = self.iv_recv + struct.pack(">Q", seq)
        aad = bytes([self.dir_recv]) + struct.pack(">Q", seq)
        try:
            inner = self._recv.decrypt(nonce, record[8:], aad)
        except Exception:
            raise E2EError("auth")
        with self._lock:
            if seq <= self.seq_recv_max:        # re-check under lock (concurrent opens)
                raise E2EError("replay/reorder")
            self.seq_recv_max = seq
            self.idle_deadline = now + IDLE_TTL  # slide only on authenticated frames
        return inner[0], inner[1:]


# ── handshake: worker side (§4) ──────────────────────────────────────────────
class WorkerHandshake:
    """One handshake attempt for one viewer. verify_assertion(assertion, challenge)
    -> truthy credential record on success, falsy on failure (wraps webauthn.py).
    seen_challenges is a shared set for cross-handshake replay defense."""

    def __init__(self, identity_priv, mid, verify_assertion, seen_challenges):
        self.idp = identity_priv
        self.ik_w = pub_raw(identity_priv.public_key())
        self.mid = mid
        self.verify_assertion = verify_assertion
        self.seen = seen_challenges
        self.keys = None

    def server_hello(self, client_hello):
        if client_hello.get("proto") != PROTO.decode():
            raise E2EError("proto")
        epk_m = b64u_dec(client_hello["epk_m"])
        n_m = b64u_dec(client_hello["n_m"])
        if len(epk_m) != 65 or len(n_m) != 32:
            raise E2EError("hello")
        self.epk_m, self.n_m = epk_m, n_m
        self.eph = gen_ec()
        self.epk_w = pub_raw(self.eph.public_key())
        self.n_w = os.urandom(32)
        T1 = sha256(lp(PROTO, self.mid.encode(), epk_m, n_m, self.epk_w, self.n_w, self.ik_w))
        sig = sign_raw(self.idp, T1)
        Z = ecdh(self.eph, epk_m)
        self.keys = key_schedule(Z, self.mid, epk_m, n_m, self.epk_w, self.n_w, self.ik_w)
        return {"epk_w": b64u(self.epk_w), "n_w": b64u(self.n_w),
                "ik_w": b64u(self.ik_w), "sig_w": b64u(sig)}

    def finish(self, client_auth):
        if self.keys is None:
            raise E2EError("order")
        expect = _confirm(self.keys["kc_m"], b"fleet-e2e/1 client-finished")
        if not hmac.compare_digest(expect, b64u_dec(client_auth["cf_m"])):
            raise E2EError("confirm")
        ch = self.keys["webauthn_challenge"]
        if not self.verify_assertion(client_auth["assertion"], ch):
            raise E2EError("passkey")
        ckey = b64u(ch)
        if ckey in self.seen:
            raise E2EError("replay")
        self.seen.add(ckey)
        cf_w = _confirm(self.keys["kc_w"], b"fleet-e2e/1 server-finished")
        return {"cf_w": b64u(cf_w)}, Session(self.keys, "worker")


# ── handshake: reference client (browser mirror, for tests/MITM sim) ──────────
class ClientHandshake:
    """Pure-Python mirror of the browser half — used by tests and the MITM sim.
    sign_assertion(challenge_bytes) -> assertion dict (a real WebAuthn assertion
    in tests; the browser uses navigator.credentials.get)."""

    def __init__(self, mid, pinned_ik_w, sign_assertion):
        self.mid = mid
        self.pin = pinned_ik_w
        self.sign_assertion = sign_assertion
        self.keys = None

    def client_hello(self):
        self.eph = gen_ec()
        self.epk_m = pub_raw(self.eph.public_key())
        self.n_m = os.urandom(32)
        return {"epk_m": b64u(self.epk_m), "n_m": b64u(self.n_m), "proto": PROTO.decode()}

    def client_auth(self, server_hello):
        epk_w = b64u_dec(server_hello["epk_w"])
        n_w = b64u_dec(server_hello["n_w"])
        ik_w = b64u_dec(server_hello["ik_w"])
        sig = b64u_dec(server_hello["sig_w"])
        if self.pin is not None and not hmac.compare_digest(ik_w, self.pin):
            raise E2EError("pin")
        T1 = sha256(lp(PROTO, self.mid.encode(), self.epk_m, self.n_m, epk_w, n_w, ik_w))
        if not verify_raw(ik_w, sig, T1):
            raise E2EError("worker-sig")
        Z = ecdh(self.eph, epk_w)
        self.keys = key_schedule(Z, self.mid, self.epk_m, self.n_m, epk_w, n_w, ik_w)
        assertion = self.sign_assertion(self.keys["webauthn_challenge"])
        cf_m = _confirm(self.keys["kc_m"], b"fleet-e2e/1 client-finished")
        return {"assertion": assertion, "cf_m": b64u(cf_m)}

    def confirm(self, server_done):
        if self.keys is None:
            raise E2EError("order")
        expect = _confirm(self.keys["kc_w"], b"fleet-e2e/1 server-finished")
        if not hmac.compare_digest(expect, b64u_dec(server_done["cf_w"])):
            raise E2EError("server-confirm")
        return Session(self.keys, "mobile")
