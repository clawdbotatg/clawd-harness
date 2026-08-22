"""Drive the whole fleet through the relay's trusted-control path.

The box-resident brain has no local harness; it reaches every machine by
connecting to the relay as the trusted `role=controller` and speaking the same
mobile protocol the browser does (`list` / `toMachine` / `machineMsg`) — but
plaintext, over the server-to-server trust path (no E2E/passkey; that's the
phone's path). The relay tags its control as the reserved ident; opted-in workers
(FLEET_CTL_ALLOW=1) bridge it to their local harness.

`RelayFleet.machines` is a live {machine_id: RelayMachine} dict that World/Verbs
read exactly like the local HarnessClient map — each RelayMachine mirrors the
slice of that interface they use. See docs/CONTROLLER.md.
"""
import json
import threading
import time
import uuid

from .wsclient import client_connect, ws_read_message, ws_send

# Relay PING_EVERY is 20s; consider the link wedged after ~3 missed pings.
READ_TIMEOUT = 75.0
# Our own keepalive: ping the relay every 20s so the reader always hears a pong
# even if the relay's ping cadence hiccups — READ_TIMEOUT is then a true backstop
# for genuinely dead links, not a trigger on quiet-but-alive ones.
KEEPALIVE_EVERY = 20.0


class RelayMachine:
    """One fleet machine, driven through the relay. Same surface as HarnessClient."""

    def __init__(self, machine_id, fleet):
        self.machine_id = machine_id
        self._fleet = fleet
        self.projects = {}
        self.sessions = {}
        self.last_answer = {}
        self.accounts = {}          # last `accounts` frame (subscription usage)
        self.connected = True
        self._lock = threading.RLock()
        self._new_lock = threading.Lock()
        self._focus_event = threading.Event()
        self._focus_cid = None
        self._pending = {}            # request id -> {"ev": Event, "resp": frame}

    # -- inbound (called from the fleet reader thread) ------------------------
    def _handle(self, msg):
        t = msg.get("type")
        with self._lock:
            if t == "projects":
                self.projects = {p["pid"]: p for p in msg.get("projects", [])}
            elif t == "sessions":
                self.sessions = {s["cid"]: s for s in msg.get("sessions", [])}
            elif t == "accounts":
                self.accounts = msg
            elif t == "hook":
                if msg.get("event") == "Stop":
                    self.last_answer[msg.get("cid")] = (msg.get("data") or {}).get("last", "")
            elif t == "focus":
                self._focus_cid = msg.get("cid")
                self._focus_event.set()
            elif t == "exit":
                self.sessions.pop(msg.get("cid"), None)
            elif t in ("searchResult", "transcriptTailResult", "screenResult"):
                slot = self._pending.pop(msg.get("id"), None)
                if slot:
                    slot["resp"] = msg
                    slot["ev"].set()

    # -- outbound (compile to toMachine frames) -------------------------------
    def _send(self, msg):
        return self._fleet._to_machine(self.machine_id, msg)

    def refresh(self):
        return self._send({"type": "list"})

    def new_session(self, pid, timeout=15, engine="claude"):
        with self._new_lock:
            self._focus_event.clear()
            self._focus_cid = None
            if not self._send({"type": "new", "pid": pid, "engine": engine}):
                return None
            return self._focus_cid if self._focus_event.wait(timeout) else None

    def send_message(self, cid, text):
        return self._send({"type": "send", "cid": cid, "text": text})

    def raw_input(self, cid, data):
        return self._send({"type": "input", "cid": cid, "data": data})

    # -- request/reply reads (search / transcriptTail / screen) ---------------
    # 15s default: the extra relay+worker hop on top of the harness's own budget.
    def request(self, frame, timeout=15):
        rid = uuid.uuid4().hex[:8]
        slot = {"ev": threading.Event(), "resp": None}
        with self._lock:
            self._pending[rid] = slot
        if not self._send(dict(frame, id=rid)):
            with self._lock:
                self._pending.pop(rid, None)
            return {"error": "not connected"}
        if not slot["ev"].wait(timeout):
            with self._lock:
                self._pending.pop(rid, None)
            return {"error": "timeout (machine offline, or its harness predates this frame)"}
        return slot["resp"]

    def _flush_pending(self, why):
        """Resolve every in-flight request immediately on disconnect so callers
        never sit out a full timeout across a redial."""
        with self._lock:
            pending, self._pending = dict(self._pending), {}
        for slot in pending.values():
            slot["resp"] = {"error": why}
            slot["ev"].set()

    def search(self, q, scope="all", limit=20):
        return self.request({"type": "search", "q": q, "scope": scope, "limit": limit})

    def transcript_tail(self, cid, n=30, chars=400):
        return self.request({"type": "transcriptTail", "cid": cid, "n": n, "chars": chars})

    def screen(self, cid, chars=1500):
        return self.request({"type": "screen", "cid": cid, "chars": chars})

    def close_session(self, cid):
        return self._send({"type": "close", "cid": cid})

    def pin_session(self, cid, on=True):
        return self._send({"type": "pin", "cid": cid, "on": bool(on)})

    def create_project(self, name):
        return self._send({"type": "createProject", "name": name})

    def add_project(self, repo_url):
        return self._send({"type": "addProject", "repoUrl": repo_url})

    def add_local_project(self, path):
        return self._send({"type": "addLocalProject", "path": path})

    def add_external_project(self, repo_url):
        return self._send({"type": "addExternalProject", "repoUrl": repo_url})

    def remove_project(self, pid):
        return self._send({"type": "removeProject", "pid": pid})

    def state(self):
        with self._lock:
            return {"machine": self.machine_id, "connected": self.connected, "boot": None,
                    "projects": [dict(p) for p in self.projects.values()],
                    "sessions": [dict(s) for s in self.sessions.values()],
                    "last_answer": dict(self.last_answer),
                    "accounts": dict(self.accounts)}


class RelayFleet:
    def __init__(self, relay_url, token, on_hook=None, on_change=None):
        self.base = relay_url.rstrip("/")
        self.token = token
        self.on_hook = on_hook or (lambda *a: None)
        self.on_change = on_change or (lambda *a: None)
        self.machines = {}                 # live; World/Verbs read this as `clients`
        self.connected = False
        self._lock = threading.RLock()
        self._wfile = None
        self._wlock = threading.Lock()
        self._sock = None
        self._stop = False

    def start(self):
        threading.Thread(target=self._run, daemon=True, name="relayfleet").start()
        return self

    def stop(self):
        self._stop = True
        try:
            if self._sock:
                self._sock.close()
        except OSError:
            pass

    def _url(self):
        return f"{self.base}/ws?role=controller&t={self.token}"

    def _keepalive(self, wfile, gen_stop):
        """Ping the relay every KEEPALIVE_EVERY until this connection generation
        ends. The relay answers pong inline, so the reader always hears traffic
        and READ_TIMEOUT only fires on a genuinely dead link."""
        while not gen_stop.wait(KEEPALIVE_EVERY):
            try:
                ws_send(wfile, self._wlock, b"", opcode=0x9, mask=True)
            except Exception:
                return

    def _run(self):
        backoff = 0.5
        while not self._stop:
            gen_stop = threading.Event()
            up_at = None
            reason = "connect failed"
            try:
                sock, rfile, wfile = client_connect(self._url())
                # The relay pings every peer every ~20s, so a healthy link is
                # never silent for long. Without a read timeout, a relay that
                # goes quiet on us (its send path marked this conn dead but the
                # TCP socket stayed ESTAB) starves this reader FOREVER and the
                # whole world freezes at its last snapshot — the PM ran blind
                # for days exactly this way. ~3 missed pings → tear down, redial.
                sock.settimeout(READ_TIMEOUT)
                self._sock, self._wfile, self.connected = sock, wfile, True
                backoff = 0.5
                up_at = time.time()
                print("[relay] control link up", flush=True)
                threading.Thread(target=self._keepalive, args=(wfile, gen_stop),
                                 daemon=True, name="relayfleet-keepalive").start()
                self.send({"type": "list"})
                self.on_change("relay", "connected")
                while not self._stop:
                    m = ws_read_message(rfile)
                    if m is None:
                        reason = "peer closed (EOF)"
                        break
                    kind, data = m
                    if kind == "ping":
                        ws_send(wfile, self._wlock, data, opcode=0xA, mask=True)
                        continue
                    if kind == "close":
                        reason = "peer sent close"
                        break
                    if kind in ("pong",) or kind == 0x2:   # ignore PTY bytes
                        continue
                    try:
                        self._on_frame(json.loads(data.decode()))
                    except Exception:
                        continue
            except Exception as e:
                reason = f"{type(e).__name__}: {e}"
            gen_stop.set()
            try:
                if self._sock:
                    self._sock.close()   # don't leak the fd across redials
            except OSError:
                pass
            self._sock, self._wfile = None, None
            self.connected = False
            for mm in self.machines.values():
                mm.connected = False
                mm._flush_pending("disconnected")
            if up_at is not None or not self._stop:
                uptime = f" after {time.time() - up_at:.0f}s" if up_at else ""
                print(f"[relay] control link down{uptime}: {reason}", flush=True)
            self.on_change("relay", "disconnected")
            if self._stop:
                break
            time.sleep(backoff)
            backoff = min(backoff * 2, 10)

    def send(self, obj):
        wf = self._wfile
        if wf is None:
            return False
        try:
            ws_send(wf, self._wlock, json.dumps(obj), opcode=0x1, mask=True)
            return True
        except Exception:
            return False

    def _to_machine(self, machine, msg):
        return self.send({"type": "toMachine", "machine": machine, "msg": msg})

    def _on_frame(self, f):
        t = f.get("type")
        if t == "machines":
            prime = []
            with self._lock:
                live = set()
                for m in f.get("machines", []):
                    if m.get("kind") == "relay" or not m.get("online"):
                        continue
                    mid = m["id"]
                    live.add(mid)
                    mm = self.machines.get(mid)
                    if mm is None:
                        self.machines[mid] = RelayMachine(mid, self)
                        prime.append(mid)               # new machine → pull state
                    elif not mm.connected:
                        mm.connected = True
                        prime.append(mid)               # reconnected → re-pull state
                for mid, mm in self.machines.items():
                    if mid not in live:
                        mm.connected = False
            # (re)request projects+sessions outside the lock; the harness then
            # pushes updates over the link automatically.
            for mid in prime:
                self._to_machine(mid, {"type": "list"})
            self.on_change("relay", "machines")
        elif t == "machineMsg":
            mid = f.get("machine")
            msg = f.get("msg") or {}
            with self._lock:
                mm = self.machines.get(mid)
            if mm:
                mm._handle(msg)
                if msg.get("type") == "hook":
                    self.on_hook(mid, msg)
                self.on_change(mid, msg.get("type"))
