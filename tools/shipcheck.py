#!/usr/bin/env python3
"""Is the thing I just wrote actually in production?

Editing index.html hot-reloads the browsers attached to *this* machine's harness
within ~1s. That feels exactly like shipping and is not shipping: production is
the fleet, and the fleet only moves on `git push`. This script exists because
that gap is invisible from the editor — a change can look perfect in a local
screenshot while every phone on h.atg.link still renders the old page.

    python3 tools/shipcheck.py            # answer now
    python3 tools/shipcheck.py --wait     # push, then block until prod catches up

Exit 0 only when production serves byte-for-byte what HEAD says it should.

What it does NOT check (say so out loud rather than imply coverage):
  * server.py / fleet/*.py on each individual harness box. Those boxes self-pull
    on their own ~5min timer and expose no version endpoint, so "the relay is
    current" is not proof that a given laptop is. A dirty worktree on any box
    silently opts it out of pulling at all (server.py auto_update_loop).
  * anything behind the passkey gate — this only reads the public UI bytes.
"""
import argparse
import hashlib
import subprocess
import sys
import time
import urllib.request

PROD = "https://h.atg.link/"
# fleet/relay.py:_serve_file injects exactly this into the page it serves, so the
# relay's bytes are never identical to the repo's. Normalize it back out before
# comparing. If that injection ever changes, this constant has to follow it.
INJECT = (b"<head><script>window.__FLEET__=true;</script>", b"<head>")

OK, BAD, WARN = "\033[32m✓\033[0m", "\033[31m✗\033[0m", "\033[33m!\033[0m"


def git(*a):
    return subprocess.run(["git", *a], capture_output=True, text=True,
                          cwd=subprocess.run(["git", "rev-parse", "--show-toplevel"],
                                             capture_output=True, text=True).stdout.strip())


def fetch_prod():
    req = urllib.request.Request(PROD, headers={"Cache-Control": "no-cache"})
    return urllib.request.urlopen(req, timeout=25).read()


def check(verbose=True):
    """Return (all_good, list_of_lines)."""
    lines, good = [], True

    dirty = git("status", "--porcelain", "--untracked-files=no").stdout.strip()
    if dirty:
        good = False
        lines.append(f"{BAD} uncommitted changes — not shipped, AND this box's "
                     f"auto-pull is disabled while the tree is dirty:")
        lines += [f"      {d}" for d in dirty.splitlines()]
    else:
        lines.append(f"{OK} working tree clean")

    git("fetch", "--quiet", "origin", "main")
    ahead = git("rev-list", "--count", "origin/main..HEAD").stdout.strip()
    behind = git("rev-list", "--count", "HEAD..origin/main").stdout.strip()
    if ahead and ahead != "0":
        good = False
        lines.append(f"{BAD} {ahead} commit(s) committed but NOT pushed — "
                     f"production cannot see them. `git push origin main`")
    elif behind and behind != "0":
        lines.append(f"{WARN} local is {behind} behind origin/main (someone else pushed)")
    else:
        lines.append(f"{OK} HEAD is pushed to origin/main")

    head_ui = git("show", "HEAD:index.html").stdout.encode()
    try:
        prod = fetch_prod()
    except Exception as e:
        return False, lines + [f"{BAD} could not reach {PROD}: {e}"]

    if prod.replace(*INJECT, 1) == head_ui:
        lines.append(f"{OK} {PROD} serves HEAD's index.html byte-for-byte "
                     f"({hashlib.sha256(head_ui).hexdigest()[:12]})")
    else:
        good = False
        lines.append(f"{BAD} {PROD} is serving DIFFERENT index.html bytes than HEAD. "
                     f"The relay pulls on a ~3min timer — retry with --wait, and if it "
                     f"never converges the relay's own checkout is stuck "
                     f"(journalctl -u clawd-fleet-pull on the box).")
    return good, lines


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wait", nargs="?", type=int, const=600, default=0,
                    metavar="SECS", help="poll until prod matches (default 600s)")
    args = ap.parse_args()

    deadline = time.time() + args.wait
    while True:
        good, lines = check()
        stuck_locally = any("NOT pushed" in l or "uncommitted" in l for l in lines)
        if good or not args.wait or stuck_locally or time.time() > deadline:
            print("\n".join(lines))
            if good:
                print("\nIN PRODUCTION. (Not checked: server.py on individual "
                      "harness boxes — they self-pull on their own timer.)")
            elif stuck_locally:
                print("\nNOT SHIPPED — and waiting will not fix it. See above.")
            else:
                print(f"\nNOT LIVE after {args.wait}s.")
            return 0 if good else 1
        time.sleep(20)


if __name__ == "__main__":
    sys.exit(main())
