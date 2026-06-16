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

from .wsclient import client_connect, ws_read_message, ws_send


class RelayMachine:
    """One fleet machine, driven through the relay. Same surface as HarnessClient."""

    def __init__(self, machine_id, fleet):
        self.machine_id = machine_id
        self._fleet = fleet
        self.projects = {}
        self.sessions = {}
        self.last_answer = {}
        self.connected = True
        self._lock = threading.RLock()
        self._new_lock = threading.Lock()
        self._focus_event = threading.Event()
        self._focus_cid = None

    # -- inbound (called from the fleet reader thread) ------------------------
    def _handle(self, msg):
        t = msg.get("type")
        with self._lock:
            if t == "projects":
                self.projects = {p["pid"]: p for p in msg.get("projects", [])}
            elif t == "sessions":
                self.sessions = {s["cid"]: s for s in msg.get("sessions", [])}
            elif t == "hook":
                if msg.get("event") == "Stop":
                    self.last_answer[msg.get("cid")] = (msg.get("data") or {}).get("last", "")
            elif t == "focus":
                self._focus_cid = msg.get("cid")
                self._focus_event.set()
            elif t == "exit":
                self.sessions.pop(msg.get("cid"), None)

    # -- outbound (compile to toMachine frames) -------------------------------
    def _send(self, msg):
        return self._fleet._to_machine(self.machine_id, msg)

    def refresh(self):
        return self._send({"type": "list"})

    def new_session(self, pid, timeout=15):
        with self._new_lock:
            self._focus_event.clear()
            self._focus_cid = None
            if not self._send({"type": "new", "pid": pid}):
                return None
            return self._focus_cid if self._focus_event.wait(timeout) else None

    def send_message(self, cid, text):
        return self._send({"type": "send", "cid": cid, "text": text})

    def raw_input(self, cid, data):
        return self._send({"type": "input", "cid": cid, "data": data})

    def close_session(self, cid):
        return self._send({"type": "close", "cid": cid})

    def create_project(self, name):
        return self._send({"type": "createProject", "name": name})

    def add_project(self, repo_url):
        return self._send({"type": "addProject", "repoUrl": repo_url})

    def state(self):
        with self._lock:
            return {"machine": self.machine_id, "connected": self.connected, "boot": None,
                    "projects": [dict(p) for p in self.projects.values()],
                    "sessions": [dict(s) for s in self.sessions.values()],
                    "last_answer": dict(self.last_answer)}


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

    def _run(self):
        backoff = 0.5
        while not self._stop:
            try:
                sock, rfile, wfile = client_connect(self._url())
                self._sock, self._wfile, self.connected = sock, wfile, True
                backoff = 0.5
                self.send({"type": "list"})
                self.on_change("relay", "connected")
                while not self._stop:
                    m = ws_read_message(rfile)
                    if m is None:
                        break
                    kind, data = m
                    if kind == "ping":
                        ws_send(wfile, self._wlock, data, opcode=0xA, mask=True)
                        continue
                    if kind == "close":
                        break
                    if kind in ("pong",) or kind == 0x2:   # ignore PTY bytes
                        continue
                    try:
                        self._on_frame(json.loads(data.decode()))
                    except Exception:
                        continue
            except Exception:
                pass
            self.connected = False
            for mm in self.machines.values():
                mm.connected = False
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
            with self._lock:
                live = set()
                for m in f.get("machines", []):
                    if m.get("kind") == "relay" or not m.get("online"):
                        continue
                    mid = m["id"]
                    live.add(mid)
                    if mid not in self.machines:
                        self.machines[mid] = RelayMachine(mid, self)
                        self._to_machine(mid, {"type": "list"})   # pull projects+sessions
                    else:
                        self.machines[mid].connected = True
                for mid, mm in self.machines.items():
                    if mid not in live:
                        mm.connected = False
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
