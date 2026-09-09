#!/usr/bin/env python3
"""Guard: the "what is this box actually running" report stays wired end to end.

  1. buildinfo.code_hash is deterministic and covers worker.py/e2e.py/buildinfo.py;
  2. worker.RESTART_WATCH IS buildinfo.WATCH (one list, or shipcheck compares
     the wrong files) and worker.BUILD carries code/ttl/idle/started;
  3. relay.py passes `build` from a stats frame into the roster and dumps the
     roster to ROSTER_FILE (source-level: importing relay has env side effects);
  4. tools/shipcheck.py loads buildinfo by path and compares HEAD's hash.

Run: python3 fleet/test_buildinfo.py
"""
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import buildinfo  # noqa: E402

FAILED = []


def check(name, ok):
    print(f"  {'OK ' if ok else 'BAD'} {name}")
    if not ok:
        FAILED.append(name)


def main():
    read = lambda n: (HERE / n).read_bytes()  # noqa: E731
    h1, h2 = buildinfo.code_hash(read), buildinfo.code_hash(read)
    check("code_hash deterministic, 12 hex", h1 == h2 and re.fullmatch(r"[0-9a-f]{12}", h1))
    for f in ("worker.py", "e2e.py", "buildinfo.py"):
        check(f"WATCH covers {f}", f in buildinfo.WATCH)
    tweaked = buildinfo.code_hash(lambda n: read(n) + (b"#" if n == "e2e.py" else b""))
    check("a one-byte change in e2e.py changes the hash", tweaked != h1)
    check("CADENCE is 7 days", buildinfo.CADENCE == 604800)

    w = (HERE / "worker.py").read_text()
    check("worker.RESTART_WATCH = buildinfo.WATCH", "RESTART_WATCH = buildinfo.WATCH" in w)
    check("worker builds BUILD after the env load",
          w.find("_load_env_file()\n") < w.find("BUILD = _build_info()"))
    check("worker reports build in every stats frame", 'payload["build"] = BUILD' in w)
    check("BUILD reads the TTLs from e2e", '"ttl"], info["idle"] = _e2e.MAX_TTL, _e2e.IDLE_TTL' in w)

    r = (HERE / "relay.py").read_text()
    check("relay passes build into worker.stats", 'st["build"] = {k: build.get(k) for k in ("code", "ttl", "idle", "started")}' in r)
    check("relay dumps the roster on broadcast", "self._dump_roster(msg[\"machines\"])" in r and "ROSTER_FILE" in r)

    sc = (ROOT / "tools" / "shipcheck.py").read_text()
    check("shipcheck loads buildinfo by path", "fleet/buildinfo.py" in sc and not re.search(r"^\s*import worker", sc, re.M))
    check("shipcheck compares HEAD's hash + CADENCE", "HEAD:fleet/" in sc and "bi.CADENCE" in sc)
    check("shipcheck fails the verdict on a fleet mismatch", "if not fleet_check(lines):" in sc)

    if FAILED:
        print(f"FAILED: {FAILED}")
        return 1
    print("PASSED: build report wired worker → relay → shipcheck")
    return 0


if __name__ == "__main__":
    sys.exit(main())
