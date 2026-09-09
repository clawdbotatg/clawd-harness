#!/usr/bin/env python3
"""Guard: the passkey cadence is ONE number, spelled in four places.

A passkey is owed when the shortest of these lapses, so a drift in any one of
them silently shortens the cadence for everyone (the 2026-07 storms were exactly
that: an e2e.py default of 3600 under a 24h relay session). This pins the four
defaults to each other and to the intended cadence (7 days since 2026-09-05;
24h before that — a ceremony per box per day was where the time went):

  fleet/relay.py   SESSION_TTL default   (relay edge session + mobile.auth_until)
  fleet/e2e.py     MAX_TTL default       (worker hard ceiling; resume works until it)
  index.html       RESUME_TTL_MS         (browser resume material; the silent leg)
  index.html       pmt cookie max-age    (the /pm brain rides the same session)
  fleet/buildinfo  CADENCE               (the worker PINS FLEET_E2E_MAX_TTL to it;
                                          a fleet.env line is logged + ignored —
                                          the fifth, invisible copy, 2026-09-09)

Source-level on purpose: importing relay.py/e2e.py pulls in env + crypto.
"""
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CADENCE = 7 * 24 * 60 * 60  # 604800


def grab(path, pattern):
    m = re.search(pattern, path.read_text())
    assert m, f"{path.name}: pattern not found: {pattern}"
    return m.group(1)


def main():
    relay = int(grab(HERE / "relay.py", r'SESSION_TTL = int\(os\.environ\.get\("FLEET_SESSION_TTL", "(\d+)"\)\)'))
    e2e = int(grab(HERE / "e2e.py", r'MAX_TTL\s*=\s*int\(os\.environ\.get\("FLEET_E2E_MAX_TTL",\s*"(\d+)"\)\)'))
    resume_expr = grab(ROOT / "index.html", r"const RESUME_TTL_MS = ([0-9 *]+);")
    resume = eval(resume_expr, {"__builtins__": {}}) // 1000  # noqa: S307 — digits and '*' only
    pmt = int(grab(ROOT / "index.html", r"pmt=\$\{tok\}; path=/pm; max-age=(\d+);"))
    bi = int(grab(HERE / "buildinfo.py", r"CADENCE = ([0-9 *]+)\n").replace(" ", "").split("#")[0]
             and eval(grab(HERE / "buildinfo.py", r"CADENCE = ([0-9 *]+)"), {"__builtins__": {}}))  # noqa: S307
    vals = {"relay SESSION_TTL": relay, "e2e MAX_TTL": e2e, "index RESUME_TTL_MS/1000": resume,
            "index pmt max-age": pmt, "buildinfo CADENCE (worker pins to it)": bi}
    worker = (HERE / "worker.py").read_text()
    if 'os.environ["FLEET_E2E_MAX_TTL"] = want' not in worker or "_pin_cadence()" not in worker:
        print("FAIL: worker.py no longer pins FLEET_E2E_MAX_TTL to buildinfo.CADENCE — a per-box fleet.env "
              "line would silently shorten the cadence again (HISTORY 2026-09-09)")
        return 1
    bad = {k: v for k, v in vals.items() if v != CADENCE}
    for k, v in vals.items():
        print(f"  {'OK ' if v == CADENCE else 'BAD'} {k} = {v}")
    if bad:
        print(f"FAIL: passkey cadence drifted from {CADENCE}s: {bad}")
        return 1
    print(f"passkey cadence pinned at {CADENCE}s ({CADENCE // 86400} days) in all five places")
    return 0


if __name__ == "__main__":
    sys.exit(main())
