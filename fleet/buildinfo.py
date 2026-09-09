"""What a worker is ACTUALLY running — shared by worker.py (reports it) and
tools/shipcheck.py (checks it against HEAD). Dependency-free on purpose:
shipcheck imports this by path and must not inherit worker.py's env side effects.

Why a content hash and not a git sha: workers only restart when one of WATCH
changes, so after a docs-only push every box's git sha is "behind" forever while
its running code is exactly HEAD's. Hashing the watched files answers the real
question — is the code in this process the code in HEAD?

History: 2026-09-09. The 09-05 passkey cadence change (24h→7d) sat dead on
clawd-heart for four days because its worker re-exec'd with a stale env; every
file-level check was green because nothing looked at the running process. Now
the process says what it runs (`build` in its stats frame), the relay writes the
roster to disk, and shipcheck refuses to say "in production" until every online
box reports HEAD's code hash and the intended TTL.
"""
import hashlib

# The worker restarts when any of these change on disk (worker.RESTART_WATCH is
# this tuple). buildinfo.py is in the list: a change to the hashing itself must
# also roll the fleet.
WATCH = ("worker.py", "e2e.py", "fleet_ws.py", "webauthn.py",
         "webpush.py", "sysstats.py", "buildinfo.py")

# The passkey cadence (seconds). fleet/test_passkey_ttl.py pins the four code
# defaults to this; shipcheck pins every RUNNING worker to it.
CADENCE = 7 * 24 * 60 * 60


def code_hash(read):
    """12-hex digest of the WATCH files. `read(name) -> bytes` (a missing file
    hashes as empty, so an optional module absent on one box still compares)."""
    h = hashlib.sha256()
    for name in WATCH:
        try:
            data = read(name)
        except Exception:
            data = b""
        h.update(name.encode() + b"\0" + (data or b"") + b"\0")
    return h.hexdigest()[:12]
