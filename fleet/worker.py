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
import re
import socket
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import quote

import fleet_ws
import sysstats

HERE = Path(__file__).resolve().parent

# How often to sample + push CPU/RAM/disk/GPU to the relay for the roster cards.
SYSSTATS_INTERVAL = float(os.environ.get("FLEET_SYSSTATS_INTERVAL", "10"))


def _load_env_file():
    """Load KEY=VALUE lines from fleet.env (gitignored) into the env *before* the
    config below reads it — the same pattern as the harness's .clawd-harness.env.
    A launchd/systemd daemon doesn't inherit your shell env, so this is how the
    secret (FLEET_WORKER_TOKEN) plus FLEET_RELAY / HARNESS_WS / FLEET_MACHINE
    reach both a manual run and the daemon, keeping the token out of the plist.
    Real environment vars always win (setdefault)."""
    try:
        text = (HERE / "fleet.env").read_text()
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, val)


_load_env_file()

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
# Opt-in trusted control: when set, this worker accepts plaintext harness frames
# from the relay's reserved controller ident (the box-resident PM brain) even
# while E2E is required for mobiles. Server-to-server trust; off by default.
CTL_IDENT = "__ctl__"
CTL_ALLOW = os.environ.get("FLEET_CTL_ALLOW", "").lower() in ("1", "true", "yes")
RP_ID = os.environ.get("FLEET_RP_ID", "h.atg.link")
ORIGIN = os.environ.get("FLEET_ORIGIN", "https://" + RP_ID)
PASSKEYS_FILE = Path(os.environ.get("FLEET_PASSKEY_FILE") or (HERE / ".clawd-fleet.passkeys.json"))
WORKER_ID_FILE = HERE / ".fleet.worker_id.json"
# Shared fleet VAPID keypair for Web Push notifications (same file the relay
# reads for the public half). This worker signs + sends the bodyless "a session
# needs you" tickle straight to the phone's push service. Guarded import: no
# cryptography → no push, worker otherwise unaffected.
VAPID_FILE = Path(os.environ.get("FLEET_VAPID_FILE") or (HERE / ".clawd-fleet.vapid.json"))
NOTIFY_THROTTLE = 8.0   # seconds; collapse a session's notification bursts
try:
    import webpush as webpushmod
    HAVE_WEBPUSH = webpushmod.HAVE_CRYPTO
except Exception:
    webpushmod = None
    HAVE_WEBPUSH = False
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
                # The trusted controller (box brain) rides a plaintext path — no
                # E2E session — so its harness frames go back via `reply`, not the
                # encrypting `reply_enc`. It also doesn't render PTY, so skip binary.
                trusted = (self.mobile_id == CTL_IDENT)
                if kind == 0x2:  # binary PTY bytes → relay (sealed, tagged with this mobile)
                    if not trusted:
                        self.worker.send_pty_enc(self.mobile_id, data)
                    continue
                # text/JSON harness frame → wrap and route to the viewer verbatim
                try:
                    frame = json.loads(data.decode("utf-8"))
                except Exception:
                    continue
                if trusted:
                    self.worker.reply(self.mobile_id, frame)
                else:
                    self.worker.reply_enc(self.mobile_id, frame)
        finally:
            self.dead = True
            err = {"type": "error", "error": "harness link closed"}
            if self.mobile_id == CTL_IDENT:
                self.worker.reply(self.mobile_id, err)
            else:
                self.worker.reply_enc(self.mobile_id, err)
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
    def __init__(self, relay, token, machine, host, harness_ws, harness_token,
                 kind="machine"):
        self.relay = relay.rstrip("/")
        self.token = token
        self.machine = machine
        self.host = host
        # Node type advertised to the relay for the roster. "machine" = a normal
        # drivable harness host; "relay" = the hub box itself (shown as infra, not
        # opened). Purely a display hint — it doesn't change wire behavior.
        self.kind = kind
        self.harness_ws = harness_ws.rstrip("/")
        self.harness_token = harness_token
        self.wfile = None
        self.wlock = threading.Lock()
        self.links = {}            # mobile_id -> HarnessLink
        self.links_lock = threading.Lock()
        # Latest plaintext aggregate counts {projects,sessions,active} read from
        # the local harness and reported to the relay for the roster. None until
        # the first poll succeeds. Just three integers — never titles or content.
        self.stats = None
        # Web Push: subscriptions handed down by the relay (the phone's, opaque)
        # and per-session notify state for "needs you" detection + throttling.
        self.push_subs = []
        self._notify = {}          # cid -> {"last": ts, "waiting": bool}
        self._sessions_meta = {}   # cid -> {"pid":..,"title":..} for deep-link payloads
        self._projects_meta = {}   # pid -> {"name":..,"repoUrl":..} → unified projectKey
        self.vapid = webpushmod.VapidKeys.load(str(VAPID_FILE)) if HAVE_WEBPUSH else None
        # Latest system stats {cpu,ram,disk,gpu} sampled locally (sysstats.collect).
        # None until the first sample; pushed to the relay on a steady timer since
        # — unlike the counts — these change every tick.
        self.sys = None
        # E2E state (per remote viewer)
        self.e2e_sessions = {}     # mobile_id -> e2e.Session (open channel)
        self.e2e_hs = {}           # mobile_id -> e2e.WorkerHandshake (in progress)
        self.e2e_seen = set()      # used challenges — cross-handshake replay defense
        self.e2e_resume = {}       # resume_id -> {master, hard} (no-passkey re-attach within TTL)
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
        if self.vapid and self.vapid.can_send:
            print(f"[worker {machine}] Web Push enabled (VAPID loaded) — will ring "
                  f"the phone when a session needs you", flush=True)
        else:
            why = "no cryptography" if not HAVE_WEBPUSH else f"no/invalid {VAPID_FILE.name}"
            print(f"[worker {machine}] Web Push off ({why}) — notifications disabled",
                  flush=True)

    # ── Web Push: ring the phone when a session needs the user ───────────────
    def cache_push_subs(self, subs, replace=False):
        """Merge subscriptions pushed down by the relay (dedupe by endpoint)."""
        by_ep = {} if replace else {s.get("endpoint"): s for s in self.push_subs}
        for s in subs:
            if s.get("endpoint"):
                by_ep[s["endpoint"]] = s
        self.push_subs = list(by_ep.values())

    def maybe_notify(self, frame):
        """Inspect a broadcast harness `hook` frame and ring the phone when the
        session crosses into 'needs you': a finished turn (Stop) or a block on the
        user (waiting=true — permission prompt / plan approval / idle nudge).
        Per-session throttle collapses bursts. No-op without subs or a signer."""
        if not self.push_subs or not (self.vapid and self.vapid.can_send):
            return
        cid = frame.get("cid")
        ev = frame.get("event")
        waiting = bool(frame.get("waiting"))
        st = self._notify.setdefault(cid, {"last": 0.0, "waiting": False})
        # Fire on Stop (turn done) or on a fresh transition into waiting (so a
        # block that lingers across hooks only rings once).
        fire = (ev == "Stop") or (waiting and not st["waiting"])
        st["waiting"] = waiting
        if not fire:
            return
        now = time.time()
        if now - st["last"] < NOTIFY_THROTTLE:
            return
        st["last"] = now
        payload = self._push_payload(cid)
        threading.Thread(target=self._send_push_all, args=(payload,), daemon=True).start()

    @staticmethod
    def _norm_repo(url):
        """Mirror of index.html normRepo(): canonicalize a git remote so the same
        repo unifies across machines. MUST stay byte-identical to the JS or the
        deep-link projectKey won't match the UI's."""
        s = (url or "").strip()
        if not s:
            return ""
        s = re.sub(r"^git@([^:]+):", r"\1/", s)            # git@host:owner/repo → host/owner/repo
        s = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", "", s)  # strip scheme
        s = re.sub(r"\.git$", "", s, flags=re.I)            # drop trailing .git
        s = re.sub(r"/+$", "", s)                           # drop trailing slash
        return s.lower()

    def _project_key(self, pid):
        """The unified projectKey the fleet UI routes on: normalized repo, else
        name:<name> (mirror of index.html projectKey())."""
        p = self._projects_meta.get(pid) or {}
        return self._norm_repo(p.get("repoUrl")) or ("name:" + (p.get("name") or ""))

    def _push_payload(self, cid):
        """Build the encrypted-push body: a friendly title + a deep link to the
        session so tapping the notification jumps straight to it. The fleet UI
        routes on `#/p/<urlencoded projectKey>/s/<cid>` and resolves the machine
        from the cid itself, so that's the form we emit (NOT a machine-prefixed
        path — parseHash doesn't understand those)."""
        meta = self._sessions_meta.get(cid) or {}
        title = meta.get("title") or "a session"
        pid = meta.get("pid")
        key = self._project_key(pid) if pid else ""
        url = f"/#/p/{quote(key, safe='')}/s/{cid}" if key else "/#/"
        return json.dumps({"title": f"{self.machine} · {title}",
                           "body": "needs you", "url": url}).encode("utf-8")

    def _send_push_all(self, payload=None):
        """Send the (encrypted, deep-linking) push to every subscription; prune any
        the push service reports permanently gone (404/410). A crypto hiccup inside
        send() degrades to a bodyless tickle rather than dropping the alert."""
        dead = []
        for sub in list(self.push_subs):
            code = self.vapid.send(sub, payload)
            if code in (404, 410):
                dead.append(sub.get("endpoint"))
        if dead:
            self.push_subs = [s for s in self.push_subs
                              if s.get("endpoint") not in dead]

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

    def report_stats(self):
        """Push the latest aggregate counts + system stats to the relay (no-op
        until something's been sampled, or if the relay link is down — the next
        poll change, the sysstats tick, or the reconnect re-send in serve_once,
        will catch it up). Always sends a *combined* snapshot (counts default to
        zeros if not yet polled) so the relay can overwrite wholesale."""
        if self.stats is None and self.sys is None:
            return
        payload = dict(self.stats or {"projects": 0, "sessions": 0, "active": 0},
                       type="stats")
        if self.sys is not None:
            payload["sys"] = self.sys
        self.send_relay(payload)

    def sysstats_loop(self):
        """Sample CPU/RAM/disk/GPU on a steady timer and push to the relay. Separate
        from stats_loop (which only fires on a count *change*) because these metrics
        move every tick — the roster wants a live read, not an edge-triggered one."""
        while True:
            try:
                self.sys = sysstats.collect()
                self.report_stats()
            except Exception as e:
                print(f"[worker {self.machine}] sysstats error: {e}", flush=True)
            time.sleep(SYSSTATS_INTERVAL)

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
        #
        # Gate: the upload bridge rides plaintext through the relay (it's an HTTP
        # POST, not an E2E record), so a malicious relay could otherwise write
        # arbitrary bytes to this machine's uploads dir at will (disk-fill DoS).
        # On an E2E-required worker, only honor it while at least one viewer holds
        # a live E2E session — i.e. a real, passkey-authenticated human is present.
        # (Residual: the bytes themselves still transit the relay un-E2E'd; routing
        # uploads through the channel is the proper fix — see E2E-PROTOCOL §6.)
        if E2E_REQUIRE:
            with self.e2e_lock:
                live = any(not s.expired() for s in self.e2e_sessions.values())
            if not live:
                return self.send_relay({"type": "uploadResult", "id": frame.get("id"),
                                        "ok": False, "error": "no authenticated session"})
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
            self.e2e_resume[e2emod.b64u(hs.keys["resume_id"])] = {
                "master": hs.keys["resume_master"], "hard": sess.hard_deadline}
        print(f"[worker {self.machine}] E2E channel open for {frm}", flush=True)
        self.reply(frm, dict(done, t="e2e.done"))

    def _e2e_resume(self, frm, msg):
        if not (HAVE_E2E and self.identity):
            return self.reply(frm, {"t": "e2e.err", "error": "e2e unavailable"})
        with self.e2e_lock:
            ent = self.e2e_resume.get(msg.get("id", ""))
            if ent and time.time() > ent["hard"]:
                self.e2e_resume.pop(msg.get("id", ""), None)
                ent = None
        if not ent:
            return self.reply(frm, {"t": "e2e.err", "error": "resume"})
        rn = os.urandom(32)
        sess = e2emod.Session(e2emod.resume_keys(ent["master"], rn), "worker")
        sess.hard_deadline = ent["hard"]   # resume never extends the hard ceiling
        with self.e2e_lock:
            self.e2e_sessions[frm] = sess
        print(f"[worker {self.machine}] E2E channel resumed for {frm}", flush=True)
        self.reply(frm, {"t": "e2e.resumed", "rn": e2emod.b64u(rn),
                         "cf": e2emod.b64u(e2emod.resume_confirm(ent["master"], rn))})

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
        if t == "e2e.resume":
            return self._e2e_resume(frm, msg)
        if t == "e2e.rec":
            return self._e2e_rec(frm, msg)
        # Harness-proxy path: a harness control frame (has a `type`). When E2E is
        # required, plaintext proxy frames are refused — everything must arrive
        # through the secure channel (decrypted in _e2e_rec). Fail closed.
        if "type" in msg:
            trusted = CTL_ALLOW and frm == CTL_IDENT      # the box-resident PM brain
            if E2E_REQUIRE and not trusted:
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
            # exec is a raw, non-E2E RCE primitive reachable from any frame the
            # relay can forge — so it must NOT exist on a production (E2E-required)
            # worker, regardless of ALLOW_EXEC. Otherwise a compromised relay would
            # be one env-flag from a remote shell, breaking "relay compromise = DoS
            # only". It survives only for the local non-E2E diagnostic smokes.
            if E2E_REQUIRE:
                self.reply(frm, {"kind": "exit", "code": -1,
                                 "error": "exec refused (E2E-required worker)"})
                return
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

    # ── roster stats (plaintext aggregate counts) ────────────────────────────
    def stats_loop(self):
        """Keep one standing harness connection open purely to count projects /
        sessions / active sessions, and report the three integers to the relay
        whenever they change. This is the ONLY harness path that isn't E2E — by
        design, because the relay folds these counts into the public roster. It
        carries no titles, prompts, or PTY bytes; just the load numbers so the
        machine list can show 'N projects · N sessions · N active' at a glance.
        Auto-reconnects with backoff if the harness is down or drops."""
        backoff = 1.0
        while True:
            try:
                self._poll_stats_once()
                backoff = 1.0
            except Exception as e:
                print(f"[worker {self.machine}] stats link error: {e}", flush=True)
            time.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    def _poll_stats_once(self):
        url = f"{self.harness_ws}/ws?t={quote(self.harness_token)}"
        sock, rfile, wfile = fleet_ws.client_connect(url)
        lock = threading.Lock()
        nproj = nsess = None
        nactive = 0
        try:
            while True:
                msg = fleet_ws.ws_read_message(rfile)
                if msg is None:
                    break
                kind, data = msg
                if kind == "close":
                    break
                if kind == "ping":
                    fleet_ws.ws_send(wfile, lock, data, opcode=0xA, mask=True)
                    continue
                if kind == "pong" or kind == 0x2:    # ignore keepalives + PTY bytes
                    continue
                try:
                    frame = json.loads(data.decode("utf-8"))
                except Exception:
                    continue
                t = frame.get("type")
                if t == "hook":
                    # Broadcast to every harness client (server.py) → this standing
                    # link sees Stop/Notification for ALL sessions even with no
                    # viewer attached, which is exactly when the phone needs a ping.
                    self.maybe_notify(frame)
                    continue
                if t == "projects":
                    projs = frame.get("projects") or []
                    nproj = len(projs)
                    # cache pid → name/repoUrl so a notification can build the
                    # unified projectKey the UI routes on (see _project_key).
                    self._projects_meta = {p.get("pid"): {"name": p.get("name"),
                                                          "repoUrl": p.get("repoUrl")}
                                           for p in projs if p.get("pid")}
                elif t == "sessions":
                    sess = frame.get("sessions") or []
                    nsess = len(sess)
                    nactive = sum(1 for s in sess if s.get("busy"))
                    # cache cid → project/title so a notification can name the
                    # session and deep-link to it (see _push_payload).
                    self._sessions_meta = {s.get("cid"): {"pid": s.get("pid"),
                                                          "title": s.get("title")}
                                           for s in sess if s.get("cid")}
                else:
                    continue
                if nproj is None or nsess is None:
                    continue
                new = {"projects": nproj, "sessions": nsess, "active": nactive}
                if new != self.stats:
                    self.stats = new
                    self.report_stats()
        finally:
            try:
                sock.close()
            except Exception:
                pass

    # ── connection loop ──────────────────────────────────────────────────────
    def url(self):
        return (f"{self.relay}/ws?role=worker&machine={quote(self.machine)}"
                f"&host={quote(self.host)}&kind={quote(self.kind)}&t={quote(self.token)}")

    def serve_once(self):
        sock, rfile, wfile = fleet_ws.client_connect(self.url())
        self.wfile = wfile
        print(f"[worker {self.machine}] connected to {self.relay}", flush=True)
        self.report_stats()   # seed the fresh roster entry with our last counts
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
                elif t == "pushSubs":          # full set on (re)connect
                    self.cache_push_subs(frame.get("subs") or [], replace=True)
                elif t == "pushSub":           # one new subscription
                    self.cache_push_subs([frame.get("sub") or {}])
        finally:
            self.wfile = None
            self._drop_all_links()
            try:
                sock.close()
            except Exception:
                pass

    def run(self):
        # A relay node has no harness behind it, so there are no stats to poll —
        # skip the loop (it would just spam "Connection refused" forever).
        if self.kind != "relay":
            threading.Thread(target=self.stats_loop, daemon=True).start()
            threading.Thread(target=self.sysstats_loop, daemon=True).start()
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
    ap.add_argument("--kind", default=os.environ.get("FLEET_KIND", "machine"),
                    help="node type for the roster: 'machine' (drivable harness host) "
                         "or 'relay' (the hub box itself, shown as infra)")
    args = ap.parse_args()
    Worker(args.relay, args.token, args.machine, args.host,
           args.harness, args.harness_token, args.kind).run()


if __name__ == "__main__":
    main()
