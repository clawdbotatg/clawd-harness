#!/usr/bin/env python3
"""tldr_e2e — the 🟦/🔊 feature end to end, in an ISOLATED copy of the harness.

Copies server.py + index.html + fleet/ into a scratch dir with its own
registry/token/ports (so it can never touch the live sessions — running the
real server.py from the repo dir would --resume them), starts it with the API
tee on a spare port, spawns ONE real claude session (subscription) in a
throwaway local project, sends one prompt, and prints every `tldr`/`say`
frame with timestamps. Then closes the session and the server.

    python3 tools/tldr_e2e.py                 # summary only
    python3 tools/tldr_e2e.py --voice         # + the voice (say frames)
    python3 tools/tldr_e2e.py --prompt "…"    # your own prompt (no tools, prose)

Costs one claude turn on the subscription plus ~10 haiku passes. Not in the
gate on purpose. See docs/TLDR-VOICE.md.
"""
import argparse, json, os, shutil, subprocess, sys, tempfile, threading, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT, TEE_PORT = 8800, 8792
DEFAULT_PROMPT = ("Do not use any tools. In about 350 words of prose, explain how you'd "
                  "add rate limiting to a small Flask API: where it lives, what to key "
                  "on, what to return, one pitfall, and end with one question for me.")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--voice", action="store_true")
    ap.add_argument("--prompt", default=DEFAULT_PROMPT)
    ap.add_argument("--keep", action="store_true", help="leave the scratch dir behind")
    a = ap.parse_args()
    iso = Path(tempfile.mkdtemp(prefix="tldr-e2e-"))
    (iso / "work").mkdir()
    proj = iso.parent / f"{iso.name}-proj"     # OUTSIDE the harness dir (it refuses its own tree)
    proj.mkdir(); subprocess.run(["git", "-C", str(proj), "init", "-q"])
    for f in ("server.py", "index.html"):
        shutil.copy(ROOT / f, iso / f)
    shutil.copytree(ROOT / "fleet", iso / "fleet")
    if (ROOT / "share").exists():
        shutil.copytree(ROOT / "share", iso / "share")
    env = {k: v for k, v in os.environ.items()
           if k not in ("ANTHROPIC_BASE_URL", "CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT")
           and not k.startswith("CLAUDE_CODE_")}
    env.update({"AUTO_PULL": "0", "PORT": str(PORT), "API_TEE_PORT": str(TEE_PORT),
                "BIND": "127.0.0.1", "WORKDIR": str(iso / "work"), "AUTO_TLDR": "0"})
    log = open(iso / "server.log", "w")
    srv = subprocess.Popen([sys.executable, "server.py"], cwd=iso, env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        time.sleep(4)
        tok = (iso / ".clawd-harness.token").read_text().strip()
        sys.path.insert(0, str(iso / "fleet"))
        import fleet_ws
        sock, rfile, wfile = fleet_ws.client_connect(f"ws://127.0.0.1:{PORT}/ws?t={tok}")
        lock = threading.Lock()
        frames, T0 = [], time.time()
        def send(o): fleet_ws.ws_send(wfile, lock, json.dumps(o), opcode=0x1, mask=True)
        def reader():
            while True:
                m = fleet_ws.ws_read_message(rfile)
                if m is None: return
                kind, data = m
                if kind == "ping": fleet_ws.ws_send(wfile, lock, data, opcode=0xA, mask=True); continue
                if kind in ("pong", 0x2, "close"): continue
                try: frames.append((time.time() - T0, json.loads(data.decode())))
                except Exception: pass
        threading.Thread(target=reader, daemon=True).start()
        def wait(pred, timeout):
            dl = time.time() + timeout
            while time.time() < dl:
                for t, f in frames:
                    if pred(f): return f
                time.sleep(0.2)
            return None
        send({"type": "addLocalProject", "path": str(proj), "create": True})
        pf = wait(lambda f: f.get("type") == "projects" and any(p.get("kind") == "local" for p in f["projects"]), 10)
        if not pf:
            print("no project frame; errors:", [f for t, f in frames if f.get("type") == "error"]); return 1
        pid = [p for p in pf["projects"] if p.get("kind") == "local"][0]["pid"]
        send({"type": "new", "pid": pid})
        cid = wait(lambda f: f.get("type") == "focus", 15)["cid"]
        send({"type": "subscribe", "cid": cid}); send({"type": "tldr", "cid": cid, "on": True})
        if a.voice: send({"type": "tldr", "cid": cid, "voice": True})
        ok = wait(lambda f: f.get("type") == "hook" and f.get("event") == "SessionStart" and f.get("cid") == cid, 60)
        print("SessionStart:", "ok" if ok else "MISSING")
        time.sleep(2); t0 = time.time() - T0
        send({"type": "send", "text": a.prompt, "via": "typed"})
        stop = wait(lambda f: f.get("type") == "hook" and f.get("event") == "Stop" and f.get("cid") == cid, 180)
        time.sleep(12)
        print("Stop:", "yes" if stop else "NO")
        for t, f in frames:
            if f.get("type") == "tldr" and f.get("text"):
                st = sum(1 for _, ok in f.get("sents", []) if ok)
                print(f"  tldr +{t - t0:5.1f}s final={f['final']} settled={st}/{len(f.get('sents', []))} · {f['text'][:90]}")
            if f.get("type") == "say":
                print(f"🔊 say  +{t - t0:5.1f}s urgent={f['urgent']}: {f['text']}")
        send({"type": "close", "cid": cid}); time.sleep(2)
        print("---- server log (tee/voice):")
        for line in (iso / "server.log").read_text().splitlines():
            if "[tee " in line or "[voice" in line or "Traceback" in line: print("  " + line[:140])
        return 0
    finally:
        srv.terminate()
        try: srv.wait(5)
        except Exception: srv.kill()
        if not a.keep:
            shutil.rmtree(iso, ignore_errors=True); shutil.rmtree(proj, ignore_errors=True)
        else:
            print("kept:", iso)

if __name__ == "__main__":
    sys.exit(main() or 0)
