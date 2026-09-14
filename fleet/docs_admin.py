#!/usr/bin/env python3
"""Offline administrator tool. Run on the relay host, not through HTTP.

Provision emits a private credential FILE, never a token on stdout. Transfer that
file securely to ~/.config/fleet-docs/credential.json on the selected machine.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import tempfile

from docs_store import Store


def private_json(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as f:
            tmp = f.name
            json.dump(value, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, default=Path(__file__).parent / ".clawd-fleet.docs.credentials.json")
    sub = parser.add_subparsers(dest="command", required=True)
    provision = sub.add_parser("provision")
    provision.add_argument("id")
    provision.add_argument("--out", required=True, type=Path)
    provision.add_argument("--permissions", nargs="+", choices=["read", "write", "delete"],
                           default=["read", "write", "delete"])
    provision.add_argument("--prefix", action="append", default=None)
    sub.add_parser("revoke").add_argument("id")
    args = parser.parse_args()
    Store.validate(args.id)
    entries = json.loads(args.registry.read_text()) if args.registry.exists() else []
    if not isinstance(entries, list):
        parser.error("registry must contain a list")
    if args.command == "provision":
        if any(e["id"] == args.id for e in entries):
            parser.error("identity already exists; revoke it before reprovisioning")
        if args.out.exists():
            parser.error("output file already exists")
        if len(entries) >= 128:
            parser.error("credential registry is full")
        token = secrets.token_urlsafe(32)
        entries.append({"id": args.id, "sha256": hashlib.sha256(token.encode()).hexdigest(),
                        "permissions": args.permissions, "prefixes": args.prefix or [""]})
        private_json(args.out, {"id": args.id, "token": token})
        private_json(args.registry, entries)
        print(f"Provisioned {args.id}; transfer {args.out} privately, then remove that staging copy.")
    else:
        private_json(args.registry, [e for e in entries if e["id"] != args.id])
        print(f"Revoked {args.id}; effective on the next request.")


if __name__ == "__main__":
    main()
