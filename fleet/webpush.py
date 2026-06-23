"""Minimal Web Push (VAPID) sender — worker-side, used to ring the phone when a
session needs the user.

Scope on purpose: we send **bodyless** ("tickle") pushes only. A bodyless push
skips the entire RFC 8291 payload-encryption layer (ECDH + HKDF + AES-GCM per
message) — the only crypto left is signing a small VAPID JWT (ES256 over P-256).
The phone's service worker (sw.js) shows a *generic* notification on receipt
("a session needs you"), so no session content ever leaves the machine. That
keeps this clean of the E2E boundary: the relay only brokers the opaque
subscription JSON; the worker signs + POSTs the contentless tickle straight to
the push service (Apple for an installed iOS PWA, FCM for Chrome).

`cryptography` is already a worker dep (E2E). The import is still guarded so a
machine without it simply has push disabled rather than crashing the worker.

The fleet shares ONE VAPID keypair (like the passkey file): the public key is
what the phone subscribes with (`applicationServerKey`) and what the relay serves
at GET /push/vapidPublicKey; the private key signs the tickle on each worker.
Stored together in .clawd-fleet.vapid.json (gitignored) — generate once, copy to
the box (relay reads public) and every worker machine (reads private).
"""
import base64
import json
import os
import time
import urllib.request
import urllib.error
from pathlib import Path
from urllib.parse import urlparse

try:
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    HAVE_CRYPTO = True
except Exception:                                   # pragma: no cover
    HAVE_CRYPTO = False


def _b64u(b: bytes) -> str:
    """base64url, no padding (JWT / VAPID convention)."""
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64u_dec(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _encrypt_aes128gcm(ua_public_b64u, auth_b64u, plaintext):
    """Encrypt a Web Push payload for one subscription (RFC 8291 + RFC 8188,
    aes128gcm content encoding). `ua_public_b64u` / `auth_b64u` are the
    subscription's keys.p256dh / keys.auth. Returns the body to POST with header
    `Content-Encoding: aes128gcm`. End-to-end to the phone: only the device's
    private key can decrypt — the relay and push service see ciphertext only."""
    ua_pub_bytes = _b64u_dec(ua_public_b64u)            # 65-byte uncompressed point
    auth_secret = _b64u_dec(auth_b64u)                  # 16-byte auth secret
    ua_pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_pub_bytes)
    as_priv = ec.generate_private_key(ec.SECP256R1())   # ephemeral application key
    as_pub_bytes = as_priv.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    shared = as_priv.exchange(ec.ECDH(), ua_pub)        # ECDH → 32-byte secret

    # RFC 8291: key the ECDH secret with the subscription auth secret.
    ikm = HKDF(algorithm=hashes.SHA256(), length=32, salt=auth_secret,
               info=b"WebPush: info\x00" + ua_pub_bytes + as_pub_bytes).derive(shared)
    salt = os.urandom(16)
    cek = HKDF(algorithm=hashes.SHA256(), length=16, salt=salt,
               info=b"Content-Encoding: aes128gcm\x00").derive(ikm)
    nonce = HKDF(algorithm=hashes.SHA256(), length=12, salt=salt,
                 info=b"Content-Encoding: nonce\x00").derive(ikm)

    # Single record: plaintext + 0x02 last-record delimiter, AES-128-GCM sealed.
    ct = AESGCM(cek).encrypt(nonce, plaintext + b"\x02", None)
    # RFC 8188 header: salt(16) | record_size(4) | idlen(1) | keyid(as_public).
    header = salt + (4096).to_bytes(4, "big") + bytes([len(as_pub_bytes)]) + as_pub_bytes
    return header + ct


def generate_keys():
    """Return (private_pem:str, public_b64u:str) for a fresh P-256 VAPID keypair.
    public_b64u is the 65-byte uncompressed EC point — exactly the
    applicationServerKey the browser's pushManager.subscribe() expects."""
    pk = ec.generate_private_key(ec.SECP256R1())
    priv_pem = pk.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    pub = pk.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    return priv_pem, _b64u(pub)


class VapidKeys:
    """Loaded fleet VAPID material. `private` is None on a node that only has the
    public half (e.g. the relay) — such a node can serve the key but not sign."""

    def __init__(self, public_b64u, private_pem=None):
        self.public_b64u = public_b64u
        self.private_pem = private_pem
        self._priv = None
        if private_pem and HAVE_CRYPTO:
            self._priv = serialization.load_pem_private_key(
                private_pem.encode(), password=None)

    @property
    def can_send(self):
        return self._priv is not None

    @classmethod
    def load(cls, path):
        """Load from a .clawd-fleet.vapid.json file, or None if absent/unreadable."""
        try:
            d = json.loads(Path(path).read_text())
            return cls(d["public"], d.get("private"))
        except Exception:
            return None

    @classmethod
    def load_or_create(cls, path):
        """Load if present; otherwise mint a keypair and persist it (0600). Use this
        ONCE to bootstrap, then distribute the file — letting each machine create
        its own would give mismatched keys (the phone subscribes with one public
        key; the signer must hold its private mate)."""
        existing = cls.load(path)
        if existing:
            return existing
        priv_pem, pub = generate_keys()
        p = Path(path)
        p.write_text(json.dumps({"public": pub, "private": priv_pem}))
        try:
            p.chmod(0o600)
        except Exception:
            pass
        return cls(pub, priv_pem)

    def _auth_header(self, endpoint, sub="mailto:clawd@buidlguidl.com", ttl_hours=12):
        """Build the `Authorization: vapid t=<jwt>,k=<pub>` header for one push
        endpoint. `aud` is the endpoint's origin; exp is short-lived per spec."""
        origin = "{u.scheme}://{u.netloc}".format(u=urlparse(endpoint))
        header = _b64u(json.dumps({"typ": "JWT", "alg": "ES256"},
                                  separators=(",", ":")).encode())
        claims = _b64u(json.dumps({"aud": origin,
                                   "exp": int(time.time()) + ttl_hours * 3600,
                                   "sub": sub}, separators=(",", ":")).encode())
        signing_input = f"{header}.{claims}".encode()
        der = self._priv.sign(signing_input, ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der)
        sig = _b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))  # JOSE raw r||s
        jwt = f"{header}.{claims}.{sig}"
        return f"vapid t={jwt},k={self.public_b64u}"

    def send(self, subscription, data=None, ttl=86400, timeout=10):
        """POST a push to subscription['endpoint']. With `data` (bytes) it's sent as
        an aes128gcm-encrypted payload (so the phone can deep-link); without, it's a
        bodyless tickle. Any encryption hiccup falls back to bodyless rather than
        dropping the alert. Returns the HTTP status (201 accepted, 404/410 = gone →
        caller should drop the sub) or None on a network error. Never raises."""
        if not self.can_send:
            return None
        endpoint = subscription.get("endpoint")
        if not endpoint:
            return None
        body = b""
        encoding = None
        if data is not None:
            keys = subscription.get("keys") or {}
            p256dh, auth = keys.get("p256dh"), keys.get("auth")
            if p256dh and auth:
                try:
                    body = _encrypt_aes128gcm(p256dh, auth, data)
                    encoding = "aes128gcm"
                except Exception:
                    body, encoding = b"", None     # degrade to a bodyless tickle
        req = urllib.request.Request(endpoint, data=body, method="POST")
        req.add_header("Authorization", self._auth_header(endpoint))
        req.add_header("TTL", str(ttl))
        req.add_header("Content-Length", str(len(body)))
        if encoding:
            req.add_header("Content-Encoding", encoding)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code
        except Exception:
            return None

    def send_tickle(self, subscription, ttl=86400, timeout=10):
        """Bodyless push (generic banner). Thin wrapper over send()."""
        return self.send(subscription, data=None, ttl=ttl, timeout=timeout)
