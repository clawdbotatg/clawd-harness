#!/usr/bin/env python3
"""Smoke test: connect to clawd-harness /ws, ensure a project + session, send a
message, observe both channels (PTY visual + structured transcript).

Project-aware: with the projects layer there are no sessions until a project
exists, so this clones the harness repo itself into a throwaway project (offline,
fast), spawns a session in it, runs the assertions, then removes the project.
Robust frame reader: select+recv into a buffer, parse frames out of it."""
import socket, base64, os, struct, json, time, re, glob, pathlib

HOST = "127.0.0.1"; PORT = int(os.environ.get("PORT", "8787"))
HERE = pathlib.Path(__file__).resolve().parent
TOKEN = (os.environ.get("CONSOLE_TOKEN")
         or (HERE / ".clawd-harness.token").read_text().strip())
ANSI = re.compile(rb"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|[\x00-\x08\x0b-\x1f\x7f]")

def mask_frame(payload, opcode=0x1):
    b = bytearray([0x80 | opcode]); n = len(payload); m = os.urandom(4)
    if n < 126: b.append(0x80 | n)
    elif n < 65536: b.append(0x80 | 126); b += struct.pack(">H", n)
    else: b.append(0x80 | 127); b += struct.pack(">Q", n)
    b += m; b += bytes(payload[i] ^ m[i % 4] for i in range(n))
    return bytes(b)

class Conn:
    def __init__(self, s): self.s = s; self.buf = b""
    def _need(self, n, deadline):
        while len(self.buf) < n:
            self.s.settimeout(max(0.05, deadline - time.time()))
            try: chunk = self.s.recv(65536)
            except socket.timeout: return False
            if not chunk: return False
            self.buf += chunk
        return True
    def frames_until(self, deadline):
        out = []
        while time.time() < deadline:
            if not self._need(2, deadline): break
            b0, b1 = self.buf[0], self.buf[1]
            length = b1 & 0x7F; off = 2
            if length == 126:
                if not self._need(4, deadline): break
                length = struct.unpack(">H", self.buf[2:4])[0]; off = 4
            elif length == 127:
                if not self._need(10, deadline): break
                length = struct.unpack(">Q", self.buf[2:10])[0]; off = 10
            if not self._need(off + length, deadline): break
            data = self.buf[off:off+length]; self.buf = self.buf[off+length:]
            out.append((b0 & 0x0F, bytes(data)))
        return out

s = socket.create_connection((HOST, PORT))
key = base64.b64encode(os.urandom(16)).decode()
s.sendall((f"GET /ws?t={TOKEN} HTTP/1.1\r\nHost: {HOST}:{PORT}\r\nUpgrade: websocket\r\n"
           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
hs = b""
while b"\r\n\r\n" not in hs: hs += s.recv(1)
c = Conn(s)

def send(obj): s.sendall(mask_frame(json.dumps(obj).encode()))

pty_bytes = bytearray(); events = []; sid = None; projects = []
def collect(frames):
    global sid, projects
    for op, data in frames:
        if op == 0x2: pty_bytes.extend(data)
        elif op == 0x1:
            try: m = json.loads(data.decode("utf-8", "replace"))
            except Exception: continue
            events.append(m)
            if m.get("type") == "hello": sid = m.get("sessionId")
            if m.get("type") == "projects": projects = m.get("projects", [])

# 1) connect → read the projects/sessions snapshot
collect(c.frames_until(time.time() + 2))
print(f"[smoke] projects on connect: {[(p['name'],p['status']) for p in projects]}")

# 2) ensure a ready project — clone the harness repo itself (offline) if none
created_pid = None
ready = next((p for p in projects if p["status"] == "ready"), None)
if not ready:
    print(f"[smoke] no project — cloning {HERE} as a throwaway project…")
    send({"type": "addProject", "repoUrl": str(HERE)})
    deadline = time.time() + 60
    while time.time() < deadline:
        collect(c.frames_until(time.time() + 2))
        ready = next((p for p in projects if p["status"] == "ready"), None)
        err = next((p for p in projects if p["status"] == "error"), None)
        if ready or err: break
    assert ready, f"clone never reached ready ({err and err.get('error')})"
    created_pid = ready["pid"]
pid = ready["pid"]
print(f"[smoke] using project {ready['name']!r} pid={pid[:8]}")

# 3) spawn a session in that project and subscribe
send({"type": "new", "pid": pid})
cid = None
deadline = time.time() + 15
while time.time() < deadline and not cid:
    collect(c.frames_until(time.time() + 2))
    foc = next((e for e in events if e.get("type") == "focus"), None)
    if foc: cid = foc["cid"]; send({"type": "subscribe", "cid": cid})
assert cid, "no session was focused after /new"
collect(c.frames_until(time.time() + 4))
print(f"[smoke] sessionId={sid}  cid={cid[:8]}  pty={len(pty_bytes)}B")

# 4) send a message; observe both channels
msg = "Reply with exactly one word: pong"
print(f"[smoke] sending: {msg!r}")
send({"type": "send", "cid": cid, "text": msg})
print("[smoke] reading reply 30s…")
collect(c.frames_until(time.time() + 30))

print("\n===== STRUCTURED TRANSCRIPT EVENTS (from WS) =====")
got_user = got_asst = False
for e in events:
    if e.get("type") == "transcript":
        ev = e["event"]; role = ev.get("role")
        if role == "user": got_user = True
        if role == "assistant" and ev.get("text"): got_asst = True
        print(f"  [{role}] " + json.dumps({k:v for k,v in ev.items() if k!='role'})[:200])

tail = ANSI.sub(b"", bytes(pty_bytes))[-300:].decode("utf-8", "replace").strip()
print("\n===== PTY MIRROR TAIL (proves live visual channel) =====")
print("   " + tail.replace("\n", " | "))

print(f"\n[smoke] RESULT: pty_grew={len(pty_bytes)>5000}  user_event={got_user}  assistant_event={got_asst}")
if sid:
    tf = glob.glob(os.path.expanduser(f"~/.claude/projects/*/{sid}.jsonl"))
    print(f"[smoke] transcript file on disk: {'YES' if tf else 'NO'}")

# 5) clean up the throwaway project we created (kills its session; files stay on disk)
if created_pid:
    send({"type": "removeProject", "pid": created_pid})
    c.frames_until(time.time() + 1)
    print(f"[smoke] removed throwaway project (rm projects/{ready['name']} to delete files)")
s.close()
