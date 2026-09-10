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

The FLEET leg (2026-09-09): every worker reports what its PROCESS runs — a hash
of its code files (fleet/buildinfo.py) and the E2E TTLs it resolved — in its
stats frame; the relay dumps the roster to disk; this reads it over ssh and
refuses "IN PRODUCTION" until every ONLINE box reports HEAD's hash and the
intended passkey cadence. Born of the 09-05 cadence change that sat dead on one
box for four days while every file-level check here was green.

What it does NOT check (say so out loud rather than imply coverage):
  * server.py on each individual harness box (no version report yet). Those
    boxes self-pull on their own ~5min timer; a dirty worktree on any box
    silently opts it out of pulling at all (server.py auto_update_loop).
  * OFFLINE boxes — listed, not failed. They come back running whatever they
    had and self-restart within 30 min of the pull.
  * SWITCHED-OFF boxes (the relay's inactive list) — shown with their mismatch
    but not failed: the page opens no channel to them and they prompt for
    nothing, so their worker code cannot reach the user.
  * anything behind the passkey gate — this only reads the public UI bytes.
"""
import argparse
import hashlib
import importlib.util
import json
import os
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

RELAY_SSH = os.environ.get("FLEET_RELAY_SSH", "zkllmapi")
ROSTER_PATH = "~/clawd-harness/fleet/.clawd-fleet.roster.json"
PREFS_PATH = "~/clawd-harness/fleet/.clawd-fleet.prefs.json"   # {"inactive":[…]} — switched-off boxes
ROSTER_MAX_AGE = 120   # the relay rewrites it on every stats tick (~10s per box)


def _buildinfo():
    """fleet/buildinfo.py by path — never `import worker` (env side effects)."""
    root = git("rev-parse", "--show-toplevel").stdout.strip()
    spec = importlib.util.spec_from_file_location("buildinfo", f"{root}/fleet/buildinfo.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def head_code_hash(bi):
    return bi.code_hash(lambda n: git("show", f"HEAD:fleet/{n}").stdout.encode())


def fetch_roster():
    """The relay's roster dump + its switched-off list, one ssh round trip."""
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=12", RELAY_SSH,
                        f"cat {ROSTER_PATH}; echo; cat {PREFS_PATH} 2>/dev/null || echo '{{}}'"],
                       capture_output=True, text=True, timeout=40)
    if r.returncode != 0:
        raise RuntimeError((r.stderr.strip() or "ssh failed").splitlines()[-1])
    roster_txt, _, prefs_txt = r.stdout.partition("\n")
    roster = json.loads(roster_txt)
    try:
        roster["inactive"] = set(json.loads(prefs_txt or "{}").get("inactive") or [])
    except Exception:
        roster["inactive"] = set()
    return roster


def fleet_check(lines):
    """Append per-box lines; return True iff every ONLINE worker runs HEAD's code
    at the intended cadence. Unreachable relay/roster = not verified = False."""
    bi = _buildinfo()
    want = head_code_hash(bi)
    try:
        roster = fetch_roster()
    except Exception as e:
        lines.append(f"{BAD} fleet: cannot read the relay's roster ({RELAY_SSH}): {e} — "
                     f"the boxes are NOT verified")
        return False
    age = time.time() - roster.get("ts", 0)
    if age > ROSTER_MAX_AGE:
        lines.append(f"{BAD} fleet: roster on the relay is {int(age)}s stale — is the relay "
                     f"running post-09-09 code? (systemctl status clawd-fleet-relay)")
        return False
    good = True
    inactive = roster.get("inactive", set())
    for m in sorted(roster.get("machines", []), key=lambda m: m["id"]):
        # A box switched OFF from the machines tab opens no channel and prompts
        # for nothing, so its worker code can't reach the user: shown, not failed.
        off = m["id"] in inactive
        if not m.get("online"):
            lines.append(f"{WARN} fleet: {m['id']:<14} offline (last seen "
                         f"{int((time.time() - m.get('lastSeen', 0)) / 60)} min ago) — not verified")
            continue
        b = (m.get("stats") or {}).get("build")
        if not b:
            good = False
            lines.append(f"{BAD} fleet: {m['id']:<14} online but reports no build — worker "
                         f"predates 09-09 (never restarted for real?)")
            continue
        code_ok = b.get("code") == want
        ttl = b.get("ttl")
        ttl_ok = ttl is None and m.get("kind") == "relay" or ttl == bi.CADENCE
        ok = code_ok and ttl_ok
        good &= ok or off
        mark = OK if ok else (WARN if off else BAD)
        ch = b.get("chan") or {}
        if ch.get("n"):
            due = f" · {ch['n']} channel{'s' if ch['n'] != 1 else ''}, next passkey due " \
                  f"{time.strftime('%m-%d %H:%M', time.localtime(ch['next']))}"
        elif "chan" in b:
            due = " · no live channels (next open pays a passkey)"
        else:
            due = ""
        lines.append(f"{mark} fleet: {m['id']:<14} " + ("(switched off) " if off else "") +
                     f"code {b.get('code')} {'= HEAD' if code_ok else '≠ HEAD ' + want}"
                     f"{'' if ttl is None else f' · ttl {ttl}s' + ('' if ttl_ok else f' (want {bi.CADENCE})')}"
                     f" · up since {time.strftime('%m-%d %H:%M', time.localtime(b.get('started') or 0))}" + due)
    return good


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
        lines.append(f"{BAD} uncommitted changes — whatever is in these files is NOT in "
                     f"production, and this box's auto-pull is disabled while the tree "
                     f"is dirty (so it also blocks other people's pushes from landing here):")
        lines += [f"      {d}" for d in dirty.splitlines()]
        lines.append("      (sessions share this worktree — `git diff` before assuming "
                     "it's yours; it may be another session mid-edit.)")
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
    if not fleet_check(lines):
        good = False
        lines.append(f"      fleet: a worker restarts at its next lull after the pull, "
                     f"30 min at most with a viewer attached — --wait 2100 covers it.")
    return good, lines


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wait", nargs="?", type=int, const=600, default=0,
                    metavar="SECS", help="poll until prod matches (default 600s)")
    args = ap.parse_args()

    deadline = time.time() + args.wait
    while True:
        good, lines = check()
        # Two independent verdicts. "Is my pushed commit live yet?" is worth waiting
        # on — the relay pulls on a ~3min timer. "Is there an unpushed commit?" is
        # not: no amount of waiting pushes it. A DIRTY TREE also doesn't stop the
        # poll, because the dirt is often another session mid-edit in this shared
        # worktree while the commit I care about is already on its way to prod.
        unpushed = any("NOT pushed" in l for l in lines)
        prod_behind = any("serving DIFFERENT" in l for l in lines) or \
            any(l.startswith(BAD) and "fleet:" in l for l in lines)
        if good or not args.wait or unpushed or not prod_behind or time.time() > deadline:
            print("\n".join(lines))
            if good:
                print("\nIN PRODUCTION — UI bytes on h.atg.link and the worker code + "
                      "passkey TTL on every ONLINE box match HEAD. (Not checked: "
                      "server.py on the boxes; offline boxes.)")
            elif unpushed:
                print("\nNOT SHIPPED — and waiting will not fix it. Push. See above.")
            elif prod_behind:
                print(f"\nNOT LIVE after {args.wait}s.")
            else:
                print("\nHEAD is live in production, but the checks above are not all "
                      "green — read them before calling this done.")
            return 0 if good else 1
        time.sleep(20)


if __name__ == "__main__":
    sys.exit(main())
