#!/usr/bin/env python3
"""relay.py — the public rendezvous hub (this is what runs on AWS).

Workers (each machine running clawd) and mobile clients both **dial out** to
this one public box and hold a persistent WebSocket open. The relay never
connects back into anyone — it only routes:

    mobile  --toMachine-->  relay  --task-->   worker
    worker  --reply------>  relay  --machineMsg-->  mobile

That direction is the whole point: worker machines sit behind NAT/home routers
and can't accept inbound connections, but they can dial out and stay connected.

Protocol (all JSON text frames):
  worker→relay:
    (handshake via query: ?role=worker&machine=<id>&host=<name>&t=<token>)
    {type:"reply",  to:<mobileId>, msg:{...}}   # route a result to one mobile
    {type:"status", msg:{...}}                  # broadcast (e.g. busy/idle)
    {type:"stats", projects:N, sessions:N, active:N}  # plaintext aggregate
        counts only (no titles/content) — for the at-a-glance roster load
  mobile→relay:
    (handshake via query: ?role=mobile&t=<token>)
    {type:"list"}                               # ask for the machine roster
    {type:"toMachine", machine:<id|"*">, msg:{...}}
  relay→mobile:
    {type:"machines", machines:[{id,host,kind,online,lastSeen,stats:{projects,sessions,active}|null}]}
    {type:"machineMsg", machine:<id>, msg:{...}}
    {type:"error", error:"..."}
  relay→worker:
    {type:"task", from:<mobileId>, msg:{...}}
    {type:"mobileGone", mobile:<mobileId>}      # a viewer left; tear down its harness link

Binary frames (WS opcode 0x2) carry raw PTY bytes for the harness-proxy worker,
length-prefixed with a routing id so no JSON envelope is needed (roadmap #2):
    worker→relay:  [1 byte L][mobileId ascii (L)][PTY bytes…]   → routed to that mobile
    relay→mobile:  [1 byte L][machineId ascii (L)][PTY bytes…]  ← tagged with the source

Auth: a shared token in `?t=` (FLEET_TOKEN env, or .clawd-fleet.token, else a
dev default with a loud warning). Good enough for a prototype; harden before
exposing publicly.

Run:  python3 relay.py            # binds 0.0.0.0:8788
"""
import base64
import hmac
import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from socketserver import TCPServer, ThreadingMixIn
from urllib.parse import parse_qs, urlparse

import fleet_ws
import webauthn

HERE = Path(__file__).resolve().parent
BIND = os.environ.get("FLEET_BIND", "0.0.0.0")
PORT = int(os.environ.get("FLEET_PORT", "8788"))
PING_EVERY = 20.0  # keep NAT mappings warm

# Passkey-only mode: there is NO mobile URL token — the passkey is the sole user
# credential (verified at the relay edge as a doorman, and AUTHORITATIVELY by the
# machine's worker over the E2E channel). The worker token still gates machine
# registration. Implies REQUIRE_PASSKEY (otherwise mobiles would be unauthed).
PASSKEY_ONLY = os.environ.get("FLEET_PASSKEY_ONLY", "").lower() in ("1", "true", "yes")


def _base_token():
    env = os.environ.get("FLEET_TOKEN")
    if env:
        return env
    f = HERE / ".clawd-fleet.token"
    if f.exists():
        return f.read_text().strip()
    return ""


def _load_tokens():
    """The **worker** token authorizes a machine to register. The **mobile** token
    is unused in PASSKEY_ONLY mode (the passkey is the sole user credential);
    otherwise it gates the mobile URL. Both fall back to FLEET_TOKEN /
    .clawd-fleet.token; absent a needed one, a loud dev default."""
    base = _base_token()
    mobile = os.environ.get("FLEET_MOBILE_TOKEN") or base
    worker = os.environ.get("FLEET_WORKER_TOKEN") or base
    if (not mobile and not PASSKEY_ONLY) or not worker:
        print("[relay] ⚠ no token configured — using dev default. Set "
              "FLEET_WORKER_TOKEN (and FLEET_MOBILE_TOKEN unless FLEET_PASSKEY_ONLY) "
              "before exposing publicly.", flush=True)
        mobile = mobile or "dev"
        worker = worker or "dev"
    return mobile, worker


MOBILE_TOKEN, WORKER_TOKEN = _load_tokens()

# Cap upload bodies so an authed (or token-stolen) client can't OOM the shared
# box with one giant POST. Images are small; 25 MB is generous headroom.
MAX_UPLOAD = int(os.environ.get("FLEET_MAX_UPLOAD", str(25 * 1024 * 1024)))


def _token_ok(provided, expected):
    """Constant-time token check — no early-exit timing oracle. compare_digest
    rejects non-ASCII by raising, so guard the types."""
    try:
        return hmac.compare_digest(provided, expected)
    except (TypeError, ValueError):
        return False
# Optional worker allowlist: only these machine ids may register (comma-separated
# FLEET_WORKER_ALLOW). Empty = allow any machine that has the worker token.
WORKER_ALLOW = {m.strip() for m in os.environ.get("FLEET_WORKER_ALLOW", "").split(",") if m.strip()}

# Passkey: a mobile must prove a hardware-backed WebAuthn assertion (Touch ID /
# Face ID) — verified here via webauthn.py as the relay-edge doorman. The
# AUTHORITATIVE check is the per-machine E2E handshake on the worker; this gate is
# anti-abuse + keeps the roster non-public. Set FLEET_REQUIRE_PASSKEY=0 only for
# the routing smokes. (There is no web enrollment — the passkey public key is
# provisioned by an admin into .clawd-fleet.passkeys.json; see docs/fleet/DEPLOY.md.)
RP_ID = os.environ.get("FLEET_RP_ID", "h.atg.link")
ORIGIN = os.environ.get("FLEET_ORIGIN", "https://" + RP_ID)
REQUIRE_PASSKEY = (os.environ.get("FLEET_REQUIRE_PASSKEY", "1").lower() not in ("0", "false", "no")) or PASSKEY_ONLY
SESSION_TTL = int(os.environ.get("FLEET_SESSION_TTL", "86400"))  # passkey session validity (24h)
# Configurable so tests/dev never clobber a real worker's provisioned file.
PASSKEY_FILE = Path(os.environ.get("FLEET_PASSKEY_FILE") or (HERE / ".clawd-fleet.passkeys.json"))
_passkey_lock = threading.Lock()


def load_passkeys():
    try:
        return json.loads(PASSKEY_FILE.read_text())
    except Exception:
        return []


def save_passkeys(creds):
    with _passkey_lock:
        PASSKEY_FILE.write_text(json.dumps(creds))


def find_passkey(cred_id):
    for c in load_passkeys():
        if c.get("id") == cred_id:
            return c
    return None


def new_challenge():
    return webauthn.b64u_encode(secrets.token_bytes(32))


# Sessions: a successful passkey assertion mints a bearer token good for
# SESSION_TTL, so reconnects within the window don't re-prompt Face ID.
SESSIONS = {}  # token -> expiry epoch
_sessions_lock = threading.Lock()


def new_session(ttl):
    tok = secrets.token_urlsafe(32)
    exp = time.time() + ttl
    with _sessions_lock:
        SESSIONS[tok] = exp
        for k in [k for k, v in SESSIONS.items() if v < time.time()]:
            SESSIONS.pop(k, None)
    return tok, exp


def session_valid(tok):
    with _sessions_lock:
        exp = SESSIONS.get(tok)
    return exp if (exp and time.time() < exp) else None


class Conn:
    """One connected peer (worker or mobile). Owns a send lock for thread-safe
    fan-out from any thread."""
    _seq = 0
    _seq_lock = threading.Lock()

    def __init__(self, wfile, role, ident, host="", kind="machine"):
        self.wfile = wfile
        self.lock = threading.Lock()
        self.role = role
        self.ident = ident
        self.host = host
        # Node type advertised by a worker at handshake ("machine" | "relay"),
        # folded into the roster as a display hint (relay = the hub box itself).
        self.kind = kind
        self.dead = False
        self.last_seen = time.time()
        # Passkey second factor (mobiles). Workers are gated by their token at the
        # handshake; mobiles must additionally prove a WebAuthn assertion before any
        # frame is routed. mfa_ok flips true after a valid `auth` frame; auth_until
        # is when the passkey session lapses (epoch seconds); challenge is the
        # single-use WebAuthn challenge we issued for this connection.
        self.mfa_ok = not REQUIRE_PASSKEY if role == "mobile" else True
        self.auth_until = float("inf")
        self.challenge = ""
        # Plaintext aggregate counts a proxy worker reports for its harness
        # ({projects,sessions,active}) — three integers, no titles/content; shown
        # on the roster for an at-a-glance per-machine load. None until reported.
        self.stats = None

    @classmethod
    def next_mobile_id(cls):
        with cls._seq_lock:
            cls._seq += 1
            return f"m{cls._seq}"

    def send_json(self, obj):
        if self.dead:
            return
        try:
            fleet_ws.ws_send(self.wfile, self.lock, json.dumps(obj), opcode=0x1)
        except Exception:
            self.dead = True

    def send_binary(self, data):
        """Forward a raw binary frame (PTY bytes) unchanged. Servers MUST NOT mask."""
        if self.dead:
            return
        try:
            fleet_ws.ws_send(self.wfile, self.lock, data, opcode=0x2)
        except Exception:
            self.dead = True

    def ping(self):
        if self.dead:
            return
        try:
            fleet_ws.ws_send(self.wfile, self.lock, b"", opcode=0x9)
        except Exception:
            self.dead = True


class Relay:
    def __init__(self):
        self.workers = {}   # machine_id -> Conn
        self.mobiles = {}   # mobile_id  -> Conn
        self.lock = threading.Lock()
        self.uploads = {}   # upload_id -> {"event":Event, "result":dict|None}
        self.upload_seq = 0

    # ── image upload bridge (HTTP POST → worker over WS → harness → back) ─────
    def new_upload(self):
        with self.lock:
            self.upload_seq += 1
            uid = f"u{self.upload_seq}"
            ev = threading.Event()
            self.uploads[uid] = {"event": ev, "result": None}
        return uid, ev

    def finish_upload(self, uid, result):
        with self.lock:
            slot = self.uploads.get(uid)
        if slot:
            slot["result"] = result
            slot["event"].set()

    def take_upload(self, uid):
        with self.lock:
            return self.uploads.pop(uid, None)

    # ── roster ──────────────────────────────────────────────────────────────
    def roster(self):
        with self.lock:
            return [{"id": w.ident, "host": w.host, "kind": w.kind,
                     "online": not w.dead,
                     "lastSeen": int(w.last_seen), "stats": w.stats}
                    for w in self.workers.values()]

    def broadcast_roster(self):
        msg = {"type": "machines", "machines": self.roster()}
        with self.lock:
            mobiles = list(self.mobiles.values())
        for m in mobiles:
            m.send_json(msg)

    # ── worker lifecycle ────────────────────────────────────────────────────
    def add_worker(self, conn):
        with self.lock:
            old = self.workers.get(conn.ident)
            if old and old is not conn:
                old.dead = True  # a reconnect supersedes the stale connection
            self.workers[conn.ident] = conn
        print(f"[relay] worker online: {conn.ident} ({conn.host})", flush=True)
        self.broadcast_roster()

    def drop_worker(self, conn):
        with self.lock:
            if self.workers.get(conn.ident) is conn:
                del self.workers[conn.ident]
        print(f"[relay] worker offline: {conn.ident}", flush=True)
        self.broadcast_roster()

    # ── mobile lifecycle ────────────────────────────────────────────────────
    def add_mobile(self, conn):
        with self.lock:
            self.mobiles[conn.ident] = conn
        print(f"[relay] mobile online: {conn.ident}", flush=True)
        if conn.mfa_ok:
            conn.send_json({"type": "machines", "machines": self.roster()})
        else:
            conn.send_json(self._auth_challenge(conn))

    def _auth_challenge(self, conn, reason=None):
        """Mint a single-use passkey challenge for this connection and describe
        what the browser needs (rpId + enrolled credential ids)."""
        conn.challenge = new_challenge()
        creds = load_passkeys()
        msg = {"type": "authRequired", "method": "passkey", "challenge": conn.challenge,
               "rpId": RP_ID, "credentialIds": [c["id"] for c in creds], "enrolled": bool(creds)}
        if reason:
            msg["reason"] = reason
        return msg

    def _mobile_authed(self, conn):
        """True only if the passkey factor is satisfied and not expired. Expiry is
        re-checked on every frame ('checked at each step'); a lapse re-prompts."""
        if conn.mfa_ok and time.time() < conn.auth_until:
            return True
        if not conn.mfa_ok:
            return False
        # was authed but the passkey session lapsed
        conn.mfa_ok = not REQUIRE_PASSKEY
        conn.send_json(self._auth_challenge(conn, reason="expired"))
        return conn.mfa_ok

    def drop_mobile(self, conn):
        with self.lock:
            if self.mobiles.get(conn.ident) is conn:
                del self.mobiles[conn.ident]
            workers = list(self.workers.values())
        print(f"[relay] mobile offline: {conn.ident}", flush=True)
        # tell every worker so it can tear down any harness link it opened for
        # this viewer (the proxy worker keeps one harness connection per mobile).
        gone = {"type": "mobileGone", "mobile": conn.ident}
        for w in workers:
            w.send_json(gone)

    # ── routing ───────────────────────────────────────────────────────────────
    def from_mobile(self, mobile, frame):
        t = frame.get("type")

        # ── passkey second factor ─────────────────────────────────────────────
        if t == "auth":
            # Fast path: a still-valid session from a prior passkey auth — skips
            # Face ID on reconnects within the 24h window.
            sess = frame.get("session")
            if sess:
                exp = session_valid(sess)
                if exp:
                    mobile.mfa_ok = True
                    mobile.auth_until = exp
                    mobile.send_json({"type": "authOk"})
                    mobile.send_json({"type": "machines", "machines": self.roster()})
                else:
                    mobile.send_json(self._auth_challenge(mobile, reason="expired"))
                return
            # Passkey assertion path.
            cred = find_passkey(frame.get("id") or "")
            if not cred or not mobile.challenge:
                mobile.send_json({"type": "error", "error": "auth: unknown credential"})
                return
            pubkey = (int(cred["x"], 16), int(cred["y"], 16))
            ok, reason = webauthn.verify_assertion(
                pubkey, frame.get("clientDataJSON") or "", frame.get("authenticatorData") or "",
                frame.get("signature") or "", mobile.challenge, RP_ID, ORIGIN)
            mobile.challenge = ""  # single-use, win or lose
            if ok:
                mobile.mfa_ok = True
                mobile.auth_until = time.time() + SESSION_TTL
                tok, exp = new_session(SESSION_TTL)
                print(f"[relay] mobile authed: {mobile.ident}", flush=True)
                mobile.send_json({"type": "authOk", "session": tok, "expires": int(exp * 1000)})
                mobile.send_json({"type": "machines", "machines": self.roster()})
            else:
                print(f"[relay] mobile auth rejected ({reason}): {mobile.ident}", flush=True)
                mobile.send_json({"type": "error", "error": f"auth: {reason}"})
            return

        # Everything else requires a satisfied (and unexpired) wallet factor.
        if not self._mobile_authed(mobile):
            return

        if t == "list":
            mobile.send_json({"type": "machines", "machines": self.roster()})
            return
        if t == "toMachine":
            target = frame.get("machine")
            msg = frame.get("msg") or {}
            task = {"type": "task", "from": mobile.ident, "msg": msg}
            with self.lock:
                if target == "*":
                    targets = list(self.workers.values())
                else:
                    w = self.workers.get(target)
                    targets = [w] if w else []
            if not targets:
                mobile.send_json({"type": "error",
                                  "error": f"no such machine: {target}"})
                return
            for w in targets:
                w.send_json(task)
            return

    def from_worker_binary(self, worker, data):
        """A length-prefixed binary frame from a proxy worker: route the PTY
        bytes to one mobile, re-tagged with the source machine id."""
        if not data:
            return
        n = data[0]
        mobile_id = data[1:1 + n].decode("ascii", "replace")
        payload = data[1 + n:]
        with self.lock:
            m = self.mobiles.get(mobile_id)
        if not m:
            return
        mid = worker.ident.encode("ascii", "replace")
        m.send_binary(bytes([len(mid)]) + mid + payload)

    def from_worker(self, worker, frame):
        t = frame.get("type")
        if t == "reply":
            to = frame.get("to")
            out = {"type": "machineMsg", "machine": worker.ident,
                   "msg": frame.get("msg") or {}}
            with self.lock:
                m = self.mobiles.get(to)
            if m:
                m.send_json(out)
            return
        if t == "status":
            out = {"type": "machineMsg", "machine": worker.ident,
                   "msg": frame.get("msg") or {}}
            with self.lock:
                mobiles = list(self.mobiles.values())
            for m in mobiles:
                m.send_json(out)
            return
        if t == "stats":             # plaintext aggregate counts for the roster
            worker.stats = {"projects": int(frame.get("projects") or 0),
                            "sessions": int(frame.get("sessions") or 0),
                            "active": int(frame.get("active") or 0)}
            self.broadcast_roster()
            return
        if t == "uploadResult":      # answer to a pending HTTP /upload
            self.finish_upload(frame.get("id"), frame)
            return

    # ── keepalive ──────────────────────────────────────────────────────────
    def ping_loop(self):
        while True:
            time.sleep(PING_EVERY)
            with self.lock:
                conns = list(self.workers.values()) + list(self.mobiles.values())
            for c in conns:
                c.ping()


RELAY = Relay()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _q(self):
        return parse_qs(urlparse(self.path).query)

    def _serve_file(self, name, ctype):
        # The shared, mode-aware UI (index.html/favicon.png) lives either next to
        # relay.py (the flat box layout: ~/clawd-fleet/) or one level up (the
        # monorepo: clawd-harness/fleet/relay.py → clawd-harness/index.html).
        # Serve the first that exists so both layouts work.
        f = next((c for c in (HERE / name, HERE.parent / name) if c.exists()), None)
        if f is None:
            return self.send_error(404, "not found")
        body = f.read_bytes()
        # Mode flag for the shared, mode-aware UI: the harness serves index.html
        # untouched (direct mode); the relay injects this so the SAME page knows
        # to run in fleet mode (machines rung + passkey + toMachine wrapping).
        if name == "index.html":
            body = body.replace(b"<head>", b"<head><script>window.__FLEET__=true;</script>", 1)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # (No web enrollment: the passkey public key is provisioned by an admin into
    # .clawd-fleet.passkeys.json — see docs/fleet/DEPLOY.md. Verification only.)

    def do_GET(self):
        q = self._q()
        path = self.path.split("?")[0]
        # Serve the fleet mobile UI (token comes from ?t= in the page's query, like
        # the harness). The page then dials /ws as a mobile and speaks the protocol.
        if path == "/" or path == "/index.html":
            return self._serve_file("index.html", "text/html; charset=utf-8")
        if path == "/favicon.png":
            return self._serve_file("favicon.png", "image/png")
        if path == "/logo-ui.png":
            return self._serve_file("logo-ui.png", "image/png")
        if path != "/ws":
            return self.send_error(404, "not found")
        up = (self.headers.get("Upgrade", "").lower() == "websocket")
        if not up:
            return self.send_error(400, "expected websocket")
        role = q.get("role", ["mobile"])[0]
        # The worker token gates machine registration. The mobile's sole credential
        # is the passkey (PASSKEY_ONLY → no mobile token; the passkey gate runs after
        # connect, and the worker re-verifies authoritatively over the E2E channel).
        expected = WORKER_TOKEN if role == "worker" else MOBILE_TOKEN
        if (role == "worker" or not PASSKEY_ONLY) and not _token_ok(q.get("t", [""])[0], expected):
            return self.send_error(403, "bad token")
        if role == "worker":
            ident = q.get("machine", [""])[0]
            if not ident:
                return self.send_error(400, "worker needs ?machine=")
            if WORKER_ALLOW and ident not in WORKER_ALLOW:
                print(f"[relay] rejected worker not in allowlist: {ident}", flush=True)
                return self.send_error(403, "machine not allowed")
            host = q.get("host", [""])[0]
            kind = q.get("kind", ["machine"])[0] or "machine"
        else:
            ident = Conn.next_mobile_id()
            host = ""
            kind = "machine"
        self._serve_ws(role, ident, host, kind)

    def do_POST(self):
        # Image-upload bridge: the mobile POSTs image bytes here for a given
        # machine; we forward to that worker over its WS, which POSTs to its LOCAL
        # harness /upload and returns {path,name}. The path is local to the worker
        # machine — exactly where that session's claude will Read it.
        q = self._q()
        path = self.path.split("?")[0]
        if path != "/upload" or (not PASSKEY_ONLY and not _token_ok(q.get("t", [""])[0], MOBILE_TOKEN)):
            self.close_connection = True
            return self.send_error(403 if path == "/upload" else 404, "denied")
        try:
            n = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            n = 0
        if n > MAX_UPLOAD:
            # Refuse oversized bodies up front; don't read them into memory.
            self.close_connection = True
            return self.send_error(413, "upload too large")
        body = self.rfile.read(n) if n > 0 else b""   # always drain to keep the stream aligned
        machine = q.get("machine", [""])[0]
        with RELAY.lock:
            w = RELAY.workers.get(machine)
        if not w:
            return self.send_error(502, "no such machine")
        if not body:
            return self.send_error(400, "empty body")
        ctype = self.headers.get("Content-Type", "application/octet-stream")
        uid, ev = RELAY.new_upload()
        w.send_json({"type": "upload", "id": uid, "ctype": ctype,
                     "data": base64.b64encode(body).decode()})
        ok = ev.wait(timeout=35)
        slot = RELAY.take_upload(uid)
        result = slot["result"] if slot else None
        if not ok or not result or not result.get("ok"):
            err = (result or {}).get("error", "upload timed out")
            return self.send_error(502, f"upload failed: {err}")
        payload = json.dumps({"path": result.get("path"),
                              "name": result.get("name")}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _serve_ws(self, role, ident, host, kind="machine"):
        key = self.headers.get("Sec-WebSocket-Key", "")
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", fleet_ws.server_accept_headers(key))
        self.end_headers()
        self.close_connection = True

        conn = Conn(self.wfile, role, ident, host, kind)
        if role == "worker":
            RELAY.add_worker(conn)
        else:
            RELAY.add_mobile(conn)
        try:
            while True:
                try:
                    msg = fleet_ws.ws_read_message(self.rfile)
                except Exception:
                    break
                if msg is None:
                    break
                kind, data = msg
                if kind == "close":
                    break
                if kind == "ping":
                    try:
                        fleet_ws.ws_send(self.wfile, conn.lock, data, opcode=0xA)
                    except Exception:
                        break
                    continue
                if kind == "pong":
                    conn.last_seen = time.time()
                    continue
                conn.last_seen = time.time()
                if kind == 0x2:  # binary PTY frame (only workers send these)
                    if role == "worker":
                        RELAY.from_worker_binary(conn, data)
                    continue
                try:
                    frame = json.loads(data.decode("utf-8"))
                except Exception:
                    continue
                if role == "worker":
                    RELAY.from_worker(conn, frame)
                else:
                    RELAY.from_mobile(conn, frame)
        finally:
            conn.dead = True
            if role == "worker":
                RELAY.drop_worker(conn)
            else:
                RELAY.drop_mobile(conn)


class ThreadingHTTPServer(ThreadingMixIn, TCPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    threading.Thread(target=RELAY.ping_loop, daemon=True).start()
    srv = ThreadingHTTPServer((BIND, PORT), Handler)
    print(f"[relay] listening on ws://{BIND}:{PORT}/ws  (token required)", flush=True)
    # Never print the token: the relay runs on a shared box and its stdout lands
    # in the systemd journal, which is a leak. Show only that auth is configured.
    print(f"[relay]   worker url: ws://<host>:{PORT}/ws?role=worker&machine=<id>&t=<WORKER_TOKEN>", flush=True)
    if PASSKEY_ONLY:
        print(f"[relay]   mobile url: ws://<host>:{PORT}/ws?role=mobile   (PASSKEY-ONLY — no token)", flush=True)
    else:
        print(f"[relay]   mobile url: ws://<host>:{PORT}/ws?role=mobile&t=<MOBILE_TOKEN>", flush=True)
    allow = ",".join(sorted(WORKER_ALLOW)) if WORKER_ALLOW else "any (set FLEET_WORKER_ALLOW to restrict)"
    print(f"[relay]   auth: {'passkey-only (no mobile token)' if PASSKEY_ONLY else 'mobile+worker tokens'}; worker allowlist: {allow}", flush=True)
    nkeys = len(load_passkeys())
    pk = f"required (rpId={RP_ID}, {nkeys} provisioned)" if REQUIRE_PASSKEY else "DISABLED (set FLEET_REQUIRE_PASSKEY=1 to require)"
    print(f"[relay]   passkey: {pk}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[relay] shutting down", flush=True)
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
