#!/bin/sh
# checkall — run EVERY guard in this repo: all python tests (root + fleet) and
# every UI probe. Exists because guards were rotting silently (2026-08-29:
# tapprobe red since the 08-26 iron-detail-page removal, tabswitchprobe dead on
# a hardcoded cid — the tap-swallow class of bug shipped again with its guard
# broken and nobody knew). Probes and tests are DISCOVERED, not listed, so a
# new guard is in the gate the day it lands and a renamed one can't fall out.
#
#   cd tools && ./checkall.sh          # everything; non-zero exit on any red
#
# Excluded by name (debug tools, not guards): uiprobe (screenshot driver),
# probe-geom (needs a live pid/cid), brain_probe (screenshot driver).
# Probes need server.py running on :8787 (it usually is — launchd KeepAlive).
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(dirname "$HERE")
fail=0
run() {  # run <label> <cmd...>
  label=$1; shift
  printf '%-28s' "$label"
  out=$("$@" 2>&1)
  if [ $? -eq 0 ]; then echo PASS
  else fail=1; echo FAIL; echo "$out" | tail -6 | sed 's/^/    /'; fi
}

echo "== python tests (root) =="
for t in "$ROOT"/test_*.py; do
  run "$(basename "$t")" python3 "$t"
done
echo "== python tests (fleet) =="
for t in "$ROOT"/fleet/test_*.py; do
  run "fleet/$(basename "$t")" python3 "$t"
done
echo "== UI probes =="
cd "$HERE" || exit 2
for p in "$HERE"/*.mjs; do
  b=$(basename "$p")
  case "$b" in uiprobe.mjs|probe-geom.mjs|brain_probe.mjs) continue;; esac
  run "$b" node "$p"
done

if [ "$fail" -eq 0 ]; then echo "ALL GREEN"; else echo "RED — fix before shipping"; fi
exit "$fail"
