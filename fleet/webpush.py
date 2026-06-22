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
import time
import urllib.request
import urllib.error
from pathlib import Path
from urllib.parse import urlparse

try:
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    HAVE_CRYPTO = True
except Exception:                                   # pragma: no cover
    HAVE_CRYPTO = False


def _b64u(b: bytes) -> str:
    """base64url, no padding (JWT / VAPID convention)."""
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


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

    def send_tickle(self, subscription, ttl=86400, timeout=10):
        """POST a bodyless push to subscription['endpoint']. Returns the HTTP status
        (201 = accepted, 404/410 = subscription gone → caller should drop it), or
        None on a network error. Never raises."""
        if not self.can_send:
            return None
        endpoint = subscription.get("endpoint")
        if not endpoint:
            return None
        req = urllib.request.Request(endpoint, data=b"", method="POST")
        req.add_header("Authorization", self._auth_header(endpoint))
        req.add_header("TTL", str(ttl))
        req.add_header("Content-Length", "0")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code
        except Exception:
            return None
