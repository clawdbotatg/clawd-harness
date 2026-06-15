#!/usr/bin/env python3
"""worker.py — the agent that runs on each fleet machine.

Dials OUT to the relay, registers a stable machine id, then waits for tasks and
streams results back. Reconnects with backoff if the link drops (laptop sleeps,
wifi blips, relay restarts) — that resilience is the whole reason workers dial
out instead of being dialed into.

Two task families share one relay link, disambiguated by the field the mobile
sends:

  • Prototype/diagnostic (msg.kind) — self-contained, no harness needed:
      {kind:"ping"}                 -> {kind:"pong", host, machine, ts}
      {kind:"exec", cmd:"<shell>"}  -> {kind:"output",…}… {kind:"exit",code}

  • Harness proxy (msg.type) — THE point of the fleet (roadmap #1+#2): the worker
    is "just another harness client". It opens one connection to the local
    harness (ws://127.0.0.1:8787) *per remote viewer* and pumps frames both ways:
      mobile → relay → worker → harness   (control: subscribe/new/send/input/…)
      harness → worker → relay → mobile   (JSON metadata, and binary PTY bytes)
    The worker never interprets the harness protocol — it forwards it verbatim,
    so the fleet drives the harness without modifying it. See
    ../clawd-harness/docs/WS-PROTOCOL.md for the contract.

⚠ `exec` runs arbitrary shell, gated only by the relay token (same trust model
as the harness's bypass-permissions claude). Fine on machines you own; not for
an open relay. It's kept as a diagnostic next to the real proxy path.

Run:  FLEET_RELAY=wss://h.atg.link FLEET_TOKEN=… python3 worker.py --machine my-laptop
      python3 worker.py --machine my-laptop --relay ws://127.0.0.1:8788
Env:  HARNESS_WS (default ws://127.0.0.1:8787), HARNESS_TOKEN (auto-discovered).
"""
import argparse
import base64
import json
import os
import socket
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import quote

import fleet_ws

HERE = Path(__file__).resolve().parent

# The `exec` shell handler is a diagnostic, not part of the product (the harness
# proxy is). It's the most direct RCE primitive, so it's OFF unless explicitly
# enabled — even a leaked mobile token then can't get a raw remote shell.
ALLOW_EXEC = os.environ.get("FLEET_ALLOW_EXEC", "").lower() in ("1", "true", "yes")

# End-to-end channel (fleet-e2e/1). The worker independently verifies a
# channel-bound passkey and encrypts ALL harness traffic, so a compromised relay
# can neither drive this machine nor read its sessions. See docs/fleet/E2E-PROTOCOL.md.
# Needs pyca/cryptography (worker only); the box's diagnostic worker may lack it,
# so the import is optional — without it, E2E is unavailable (proxy refused if
# required, but ping/diagnostics still work).
E2E_REQUIRE = os.environ.get("FLEET_E2E_REQUIRE", "1").lower() not in ("0", "false", "no")
RP_ID = os.environ.get("FLEET_RP_ID", "h.atg.link")
ORIGIN = os.environ.get("FLEET_ORIGIN", "https://" + RP_ID)
PASSKEYS_FILE = Path(os.environ.get("FLEET_PASSKEY_FILE") or (HERE / ".clawd-fleet.passkeys.json"))
WORKER_ID_FILE = HERE / ".fleet.worker_id.json"
try:
    import e2e as e2emod
    import webauthn
    HAVE_E2E = True
except Exception as _e2e_err:        # cryptography missing → diagnostics-only worker
    e2emod = None
    HAVE_E2E = False


def _load_passkeys():
    try:
        return json.loads(PASSKEYS_FILE.read_text())
    except Exception:
        return []


def _save_passkeys(creds):
    tmp = str(PASSKEYS_FILE) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(creds, f)
    os.replace(tmp, PASSKEYS_FILE)


def make_passkey_verifier():
    """A WorkerHandshake verify callback: validates a channel-bound assertion
    against an enrolled credential, requires user-verification (biometric), and
    enforces sign-count monotonicity. Returns truthy on success."""
    lock = threading.Lock()

    def verify(assertion, challenge_bytes):
        with lock:
            creds = _load_passkeys()
            cred = next((c for c in creds if c.get("id") == assertion.get("credentialId")), None)
            if not cred:
                return False
            pubkey = (int(cred["x"], 16), int(cred["y"], 16))
            ok, _reason = webauthn.verify_assertion(
                pubkey, assertion.get("clientDataJSON", ""),
                assertion.get("authenticatorData", ""), assertion.get("signature", ""),
                e2emod.b64u(challenge_bytes), RP_ID, ORIGIN, require_uv=True)
            if not ok:
                return False
            try:
                ad = e2emod.b64u_dec(assertion["authenticatorData"])
                new_count = int.from_bytes(ad[33:37], "big")
            except Exception:
                return False
            old = int(cred.get("sign_count", 0))
            if (new_count or old) and new_count <= old:   # 0/0 ok (Apple platform), else monotone
                return False
            cred["sign_count"] = new_count
            _save_passkeys(creds)
            return True

    return verify


def default_machine_id():
    # stable per-host id, persisted so reconnects keep the same identity
    f = HERE / ".clawd-fleet.machine"
    if f.exists():
        v = f.read_text().strip()
        if v:
            return v
    v = socket.gethostname().split(".")[0] or "worker"
    try:
        f.write_text(v)
    except OSError:
        pass
    return v


def default_harness_token():
    env = os.environ.get("HARNESS_TOKEN")
    if env:
        return env
    # the harness writes its token next to its own server.py; try the usual spots
    candidates = [
        HERE.parent / ".clawd-harness.token",            # monorepo: clawd-harness/fleet/worker.py
        HERE.parent.parent / ".clawd-harness.token",     # legacy: clawd-harness/projects/clawd-fleet
        Path.home() / "clawd-harness" / ".clawd-harness.token",
        HERE / ".clawd-harness.token",
    ]
    for c in candidates:
        try:
            if c.exists():
                v = c.read_text().strip()
                if v:
                    return v
        except OSError:
            pass
    return ""


class HarnessLink:
    """One client connection to the local harness, dedicated to a single remote
    viewer (mobile). Per-viewer because the harness subscribes a connection to at
    most one session at a time, so two phones watching two sessions need two
    connections (see WS-PROTOCOL.md). Forwards harness→relay; close() tears down."""

    def __init__(self, worker, mobile_id):
        self.worker = worker
        self.mobile_id = mobile_id
        self.sock = None
        self.wfile = None
        self.lock = threading.Lock()
        self.dead = False

    def connect(self):
        url = (f"{self.worker.harness_ws}/ws?t={quote(self.worker.harness_token)}")
        self.sock, rfile, self.wfile = fleet_ws.client_connect(url)
        threading.Thread(target=self._reader, args=(rfile,), daemon=True).start()

    def _reader(self, rfile):
        try:
            while True:
                msg = fleet_ws.ws_read_message(rfile)
                if msg is None:
                    break
                kind, data = msg
                if kind == "close":
                    break
                if kind == "ping":
                    fleet_ws.ws_send(self.wfile, self.lock, data, opcode=0xA, mask=True)
                    continue
                if kind == "pong":
                    continue
                if kind == 0x2:  # binary PTY bytes → relay (sealed, tagged with this mobile)
                    self.worker.send_pty_enc(self.mobile_id, data)
                    continue
                # text/JSON harness frame → wrap and route to the mobile verbatim
                try:
                    frame = json.loads(data.decode("utf-8"))
                except Exception:
                    continue
                self.worker.reply_enc(self.mobile_id, frame)
        finally:
            self.dead = True
            self.worker.reply_enc(self.mobile_id,
                                  {"type": "error", "error": "harness link closed"})
            self.close()

    def send_text(self, frame):
        if self.dead or not self.wfile:
            return
        fleet_ws.ws_send(self.wfile, self.lock, json.dumps(frame),
                         opcode=0x1, mask=True)  # clients MUST mask

    def close(self):
        self.dead = True
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass


class Worker:
    def __init__(self, relay, token, machine, host, harness_ws, harness_token):
        self.relay = relay.rstrip("/")
        self.token = token
        self.machine = machine
        self.host = host
        self.harness_ws = harness_ws.rstrip("/")
        self.harness_token = harness_token
        self.wfile = None
        self.wlock = threading.Lock()
        self.links = {}            # mobile_id -> HarnessLink
        self.links_lock = threading.Lock()
        # E2E state (per remote viewer)
        self.e2e_sessions = {}     # mobile_id -> e2e.Session (open channel)
        self.e2e_hs = {}           # mobile_id -> e2e.WorkerHandshake (in progress)
        self.e2e_seen = set()      # used challenges — cross-handshake replay defense
        self.e2e_lock = threading.Lock()
        self.identity = None
        self.passkey_verify = None
        if HAVE_E2E:
            try:
                self.identity = e2emod.load_or_create_identity(str(WORKER_ID_FILE))
                self.passkey_verify = make_passkey_verifier()
                fp = e2emod.fingerprint(e2emod.pub_raw(self.identity.public_key()))
                n = len(_load_passkeys())
                print(f"[worker {machine}] E2E identity {fp} · {n} passkey(s) enrolled", flush=True)
                if n == 0 and E2E_REQUIRE:
                    print(f"[worker {machine}] ⚠ no passkeys enrolled — the harness "
                          f"proxy will refuse every viewer until one is propagated "
                          f"(see docs/fleet/DEPLOY.md)", flush=True)
            except Exception as e:
                print(f"[worker {machine}] E2E init failed: {e}", flush=True)
                self.identity = None
        # Fail-closed startup banners: make a missing/disabled E2E impossible to miss.
        if not E2E_REQUIRE:
            print(f"[worker {machine}] ⚠⚠ FLEET_E2E_REQUIRE=0 — end-to-end encryption "
                  f"is OFF; the relay can read and inject harness traffic. Use only "
                  f"for local transport smokes, NEVER in production.", flush=True)
        elif not (HAVE_E2E and self.identity):
            print(f"[worker {machine}] ⚠ E2E required but unavailable "
                  f"({'cryptography missing' if not HAVE_E2E else 'identity init failed'}) "
                  f"— the harness proxy is disabled (diagnostics still work).", flush=True)

    # ── outbound frames to the relay ─────────────────────────────────────────
    def reply(self, to, msg):
        if not self.wfile:
            return
        try:
            fleet_ws.ws_send(self.wfile, self.wlock,
                             json.dumps({"type": "reply", "to": to, "msg": msg}),
                             opcode=0x1, mask=True)  # clients MUST mask
        except Exception:
            pass

    def send_relay(self, obj):
        """Send a top-level JSON frame to the relay (not wrapped for a mobile)."""
        if not self.wfile:
            return
        try:
            fleet_ws.ws_send(self.wfile, self.wlock, json.dumps(obj),
                             opcode=0x1, mask=True)
        except Exception:
            pass

    def harness_http(self):
        base = self.harness_ws
        if base.startswith("wss://"):
            return "https://" + base[len("wss://"):]
        if base.startswith("ws://"):
            return "http://" + base[len("ws://"):]
        return base

    def handle_upload(self, frame):
        # An image bytes blob (base64) the relay forwarded on a mobile's behalf.
        # POST it to the LOCAL harness /upload (same as a browser would) and send
        # the resulting {path,name} back; the path is local to this machine, which
        # is exactly where this session's claude will Read it.
        threading.Thread(target=self._do_upload, args=(frame,), daemon=True).start()

    def _do_upload(self, frame):
        uid = frame.get("id")
        out = {"type": "uploadResult", "id": uid, "ok": False}
        try:
            raw = base64.b64decode(frame.get("data") or "")
            ctype = frame.get("ctype") or "application/octet-stream"
            url = f"{self.harness_http()}/upload?t={quote(self.harness_token)}"
            req = urllib.request.Request(url, data=raw, method="POST",
                                         headers={"Content-Type": ctype})
            with urllib.request.urlopen(req, timeout=30) as r:
                j = json.loads(r.read().decode("utf-8"))
            out.update(ok=True, path=j.get("path"), name=j.get("name"))
        except Exception as e:
            out["error"] = str(e)
        self.send_relay(out)

    def send_pty(self, mobile_id, payload):
        """Binary PTY bytes to the relay, length-prefixed with the target mobile."""
        if not self.wfile:
            return
        mid = mobile_id.encode("ascii", "replace")
        try:
            fleet_ws.ws_send(self.wfile, self.wlock,
                             bytes([len(mid)]) + mid + payload,
                             opcode=0x2, mask=True)
        except Exception:
            pass

    # ── E2E channel (per remote viewer) ──────────────────────────────────────
    def _e2e_hello(self, frm, msg):
        if not (HAVE_E2E and self.identity):
            return self.reply(frm, {"t": "e2e.err", "error": "e2e unavailable"})
        try:
            hs = e2emod.WorkerHandshake(self.identity, self.machine,
                                        self.passkey_verify, self.e2e_seen)
            sh = hs.server_hello(msg)
        except Exception as e:
            print(f"[worker {self.machine}] E2E hello failed for {frm}: {e}", flush=True)
            return self.reply(frm, {"t": "e2e.err", "error": "hello"})
        with self.e2e_lock:
            self.e2e_hs[frm] = hs
        self.reply(frm, dict(sh, t="e2e.shello"))

    def _e2e_auth(self, frm, msg):
        with self.e2e_lock:
            hs = self.e2e_hs.pop(frm, None)
        if hs is None:
            return self.reply(frm, {"t": "e2e.err", "error": "no handshake"})
        try:
            done, sess = hs.finish(msg)
        except Exception as e:
            print(f"[worker {self.machine}] E2E auth failed for {frm}: {e}", flush=True)
            return self.reply(frm, {"t": "e2e.err", "error": "auth"})
        with self.e2e_lock:
            self.e2e_sessions[frm] = sess
        print(f"[worker {self.machine}] E2E channel open for {frm}", flush=True)
        self.reply(frm, dict(done, t="e2e.done"))

    def _e2e_rec(self, frm, msg):
        sess = self.e2e_sessions.get(frm)
        if sess is None:
            return self.reply(frm, {"t": "e2e.err", "error": "no session"})
        try:
            kind, payload = sess.open(e2emod.b64u_dec(msg.get("r", "")))
        except e2emod.E2EError:
            if sess.expired():
                with self.e2e_lock:
                    self.e2e_sessions.pop(frm, None)
                self.drop_mobile(frm)
                self.reply(frm, {"t": "e2e.err", "error": "expired"})
            return                                  # drop bad/replayed frame silently
        if kind == e2emod.KIND_JSON:
            try:
                frame = json.loads(payload.decode("utf-8"))
            except Exception:
                return
            link = self._link_for(frm)
            if link is not None:
                link.send_text(frame)

    def _expire(self, to):
        with self.e2e_lock:
            self.e2e_sessions.pop(to, None)
        self.reply(to, {"t": "e2e.err", "error": "expired"})
        self.drop_mobile(to)

    def reply_enc(self, to, frame):
        """A harness JSON frame back to the mobile, sealed with its channel."""
        sess = self.e2e_sessions.get(to)
        if sess is None:
            return None if E2E_REQUIRE else self.reply(to, frame)
        if sess.expired():
            return self._expire(to)
        try:
            rec = sess.seal(e2emod.KIND_JSON, json.dumps(frame).encode("utf-8"))
        except Exception:
            return
        self.reply(to, {"t": "e2e.rec", "r": e2emod.b64u(rec)})

    def send_pty_enc(self, to, payload):
        """Binary PTY bytes back to the mobile, sealed with its channel."""
        sess = self.e2e_sessions.get(to)
        if sess is None:
            return None if E2E_REQUIRE else self.send_pty(to, payload)
        if sess.expired():
            return self._expire(to)
        try:
            rec = sess.seal(e2emod.KIND_BIN, payload)
        except Exception:
            return
        self.send_pty(to, rec)

    # ── task handling ────────────────────────────────────────────────────────
    def handle_task(self, frm, msg):
        # E2E channel frames (handshake + encrypted records) come first.
        t = msg.get("t")
        if t == "e2e.hello":
            return self._e2e_hello(frm, msg)
        if t == "e2e.auth":
            return self._e2e_auth(frm, msg)
        if t == "e2e.rec":
            return self._e2e_rec(frm, msg)
        # Harness-proxy path: a harness control frame (has a `type`). When E2E is
        # required, plaintext proxy frames are refused — everything must arrive
        # through the secure channel (decrypted in _e2e_rec). Fail closed.
        if "type" in msg:
            if E2E_REQUIRE:
                self.reply(frm, {"t": "e2e.err", "error": "secure channel required"})
                return
            link = self._link_for(frm)
            if link is not None:
                link.send_text(msg)
            return
        # Prototype/diagnostic path (has a `kind`).
        kind = msg.get("kind")
        if kind == "ping":
            self.reply(frm, {"kind": "pong", "host": self.host,
                             "machine": self.machine, "ts": int(time.time())})
            return
        if kind == "exec":
            if not ALLOW_EXEC:
                self.reply(frm, {"kind": "exit", "code": -1,
                                 "error": "exec disabled (set FLEET_ALLOW_EXEC=1)"})
                return
            cmd = (msg.get("cmd") or "").strip()
            if not cmd:
                self.reply(frm, {"kind": "exit", "code": -1, "error": "empty cmd"})
                return
            # run in a thread so one long task doesn't block the read loop
            threading.Thread(target=self._run, args=(frm, cmd), daemon=True).start()
            return
        self.reply(frm, {"kind": "error", "error": f"unknown kind: {kind}"})

    def _link_for(self, mobile_id):
        """Return this viewer's harness connection, opening it on first use.
        Returns None (and notifies the mobile) if the harness can't be reached."""
        with self.links_lock:
            link = self.links.get(mobile_id)
            if link and not link.dead:
                return link
            link = HarnessLink(self, mobile_id)
            try:
                link.connect()
            except Exception as e:
                self.reply(mobile_id,
                           {"type": "error", "error": f"no local harness: {e}"})
                return None
            self.links[mobile_id] = link
            return link

    def drop_mobile(self, mobile_id):
        with self.links_lock:
            link = self.links.pop(mobile_id, None)
        with self.e2e_lock:
            self.e2e_sessions.pop(mobile_id, None)
            self.e2e_hs.pop(mobile_id, None)
        if link:
            link.close()

    def _drop_all_links(self):
        with self.links_lock:
            links = list(self.links.values())
            self.links.clear()
        for link in links:
            link.close()

    def _run(self, frm, cmd):
        try:
            proc = subprocess.Popen(cmd, shell=True, cwd=str(HERE),
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, bufsize=1)
        except Exception as e:
            self.reply(frm, {"kind": "exit", "code": -1, "error": str(e)})
            return
        for line in proc.stdout:
            self.reply(frm, {"kind": "output", "stream": "stdout", "data": line})
        proc.wait()
        self.reply(frm, {"kind": "exit", "code": proc.returncode})

    # ── connection loop ──────────────────────────────────────────────────────
    def url(self):
        return (f"{self.relay}/ws?role=worker&machine={quote(self.machine)}"
                f"&host={quote(self.host)}&t={quote(self.token)}")

    def serve_once(self):
        sock, rfile, wfile = fleet_ws.client_connect(self.url())
        self.wfile = wfile
        print(f"[worker {self.machine}] connected to {self.relay}", flush=True)
        try:
            while True:
                msg = fleet_ws.ws_read_message(rfile)
                if msg is None:
                    break
                kind, data = msg
                if kind == "close":
                    break
                if kind == "ping":
                    fleet_ws.ws_send(wfile, self.wlock, data, opcode=0xA, mask=True)
                    continue
                if kind == "pong":
                    continue
                try:
                    frame = json.loads(data.decode("utf-8"))
                except Exception:
                    continue
                t = frame.get("type")
                if t == "task":
                    self.handle_task(frame.get("from"), frame.get("msg") or {})
                elif t == "mobileGone":
                    self.drop_mobile(frame.get("mobile"))
                elif t == "upload":
                    self.handle_upload(frame)
        finally:
            self.wfile = None
            self._drop_all_links()
            try:
                sock.close()
            except Exception:
                pass

    def run(self):
        backoff = 1.0
        while True:
            try:
                self.serve_once()
                backoff = 1.0  # clean disconnect → reset
            except Exception as e:
                print(f"[worker {self.machine}] link error: {e}", flush=True)
            print(f"[worker {self.machine}] reconnecting in {backoff:.0f}s…", flush=True)
            time.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--relay", default=os.environ.get("FLEET_RELAY", "ws://127.0.0.1:8788"))
    ap.add_argument("--token", default=(os.environ.get("FLEET_WORKER_TOKEN")
                                        or os.environ.get("FLEET_TOKEN") or "dev"))
    ap.add_argument("--machine", default=os.environ.get("FLEET_MACHINE", default_machine_id()))
    ap.add_argument("--host", default=socket.gethostname())
    ap.add_argument("--harness", default=os.environ.get("HARNESS_WS", "ws://127.0.0.1:8787"),
                    help="local harness WebSocket base URL")
    ap.add_argument("--harness-token", default=default_harness_token(),
                    help="harness token (default: auto-discovered or $HARNESS_TOKEN)")
    args = ap.parse_args()
    Worker(args.relay, args.token, args.machine, args.host,
           args.harness, args.harness_token).run()


if __name__ == "__main__":
    main()
