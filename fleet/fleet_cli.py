#!/usr/bin/env python3
"""fleet_cli.py — a terminal stand-in for the mobile client.

Proves the full loop today: dials the relay as a mobile, shows the live machine
roster, and lets you fire tasks at one machine or all of them and watch the
results stream back. Once this works, the real mobile UI is just `index.html`
speaking the same JSON over its WebSocket.

Commands (type at the prompt):
  list                      refresh the machine roster
  @<machine> <shell cmd>    run a command on one machine
  @* <shell cmd>            run it on every machine
  ping <machine|*>          liveness check
  quit

Run:  python3 fleet_cli.py --relay ws://127.0.0.1:8788 --token dev
"""
import argparse
import json
import os
import sys
import threading

import fleet_ws


class Mobile:
    def __init__(self, relay, token):
        self.relay = relay.rstrip("/")
        self.token = token
        self.wfile = None
        self.lock = threading.Lock()

    def send(self, obj):
        if not self.wfile:
            print("(not connected)")
            return
        fleet_ws.ws_send(self.wfile, self.lock, json.dumps(obj),
                         opcode=0x1, mask=True)  # clients MUST mask

    def _reader(self, rfile):
        while True:
            msg = fleet_ws.ws_read_message(rfile)
            if msg is None:
                print("\n[disconnected from relay]")
                os._exit(0)
            kind, data = msg
            if kind in ("close",):
                print("\n[relay closed]")
                os._exit(0)
            if kind in ("ping", "pong"):
                if kind == "ping":
                    fleet_ws.ws_send(self.wfile, self.lock, data, opcode=0xA, mask=True)
                continue
            try:
                frame = json.loads(data.decode("utf-8"))
            except Exception:
                continue
            self._show(frame)

    def _show(self, frame):
        t = frame.get("type")
        if t == "machines":
            ms = frame.get("machines", [])
            if not ms:
                print("\n[machines] (none online)")
            else:
                print("\n[machines] " + ", ".join(
                    f"{m['id']}({m['host']})" + ("" if m["online"] else " offline")
                    for m in ms))
        elif t == "machineMsg":
            mid = frame.get("machine")
            msg = frame.get("msg") or {}
            k = msg.get("kind")
            if k == "output":
                sys.stdout.write(f"[{mid}] {msg.get('data','')}")
                sys.stdout.flush()
            elif k == "exit":
                code = msg.get("code")
                err = f" error={msg['error']}" if msg.get("error") else ""
                print(f"[{mid}] ── exit {code}{err}")
            elif k == "pong":
                print(f"[{mid}] pong host={msg.get('host')} ts={msg.get('ts')}")
            else:
                print(f"[{mid}] {msg}")
        elif t == "error":
            print(f"[relay error] {frame.get('error')}")
        else:
            print(f"[relay] {frame}")
        sys.stdout.write("> ")
        sys.stdout.flush()

    def run(self):
        from urllib.parse import quote
        url = f"{self.relay}/ws?role=mobile&t={quote(self.token)}"
        sock, rfile, self.wfile = fleet_ws.client_connect(url)
        print(f"[connected to {self.relay}]")
        threading.Thread(target=self._reader, args=(rfile,), daemon=True).start()
        self._repl()

    def _repl(self):
        print("commands: list | @<machine> <cmd> | @* <cmd> | ping <machine|*> | quit")
        while True:
            try:
                line = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if not line:
                continue
            if line in ("quit", "exit"):
                return
            if line == "list":
                self.send({"type": "list"})
            elif line.startswith("ping"):
                parts = line.split(None, 1)
                target = parts[1].strip() if len(parts) > 1 else "*"
                self.send({"type": "toMachine", "machine": target,
                           "msg": {"kind": "ping"}})
            elif line.startswith("@"):
                parts = line[1:].split(None, 1)
                if len(parts) < 2:
                    print("usage: @<machine> <cmd>")
                    continue
                target, cmd = parts[0], parts[1]
                self.send({"type": "toMachine", "machine": target,
                           "msg": {"kind": "exec", "cmd": cmd}})
            else:
                print("unknown command")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--relay", default=os.environ.get("FLEET_RELAY", "ws://127.0.0.1:8788"))
    ap.add_argument("--token", default=(os.environ.get("FLEET_MOBILE_TOKEN")
                                        or os.environ.get("FLEET_TOKEN") or "dev"))
    args = ap.parse_args()
    Mobile(args.relay, args.token).run()


if __name__ == "__main__":
    main()
