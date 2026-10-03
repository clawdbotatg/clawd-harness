#!/bin/sh
# checkall — run EVERY guard in this repo: all python tests (root + fleet) and
# every UI probe. Exists because guards were rotting silently (2026-08-29:
# tapprobe red since the 08-26 iron-detail-page removal, tabswitchprobe dead on
# a hardcoded cid — the tap-swallow class of bug shipped again with its guard
# broken and nobody knew). Probes and tests are DISCOVERED, not listed, so a
# new guard is in the gate the day it lands and a renamed one can't fall out.
#
#   tools/checkall.sh > /tmp/c.log     # everything; non-zero exit on any red;
#                                      # the failed list is printed LAST
#
# Excluded by name (debug tools, not guards): uiprobe (screenshot driver),
# probe-geom + tldrgeom (need a live pid/cid), brain_probe (screenshot driver).
# Probes need server.py running on :8787 (it usually is — launchd KeepAlive).
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(dirname "$HERE")
if [ "${1:-}" = --one ]; then   # one guard → "<label> PASS" or "<label> FAIL" + its tail
  f=$2; label=${f#"$ROOT"/}
  case "$f" in *.mjs) out=$(cd "$HERE" && node "$f" 2>&1) ;; *) out=$(python3 "$f" 2>&1) ;; esac
  if [ $? -eq 0 ]; then echo "$label PASS"
  else echo "$label FAIL"; echo "$out" | tail -6 | sed 's/^/    /'; fi
  exit 0
fi
# Guards run side by side (10-03: one at a time took ~8 min). Each guard's
# output is printed whole, so lines never interleave.
res=$({ ls "$ROOT"/test_*.py "$ROOT"/fleet/test_*.py
        ls "$HERE"/*.mjs | grep -vE '/(uiprobe|probe-geom|brain_probe|tldrgeom)[.]mjs$'
      } | xargs -P "${JOBS:-6}" -n 1 sh "$0" --one)
# A timing probe can flake under parallel load: rerun each failure once, alone.
fails=""
for label in $(echo "$res" | sed -n 's/ FAIL$//p'); do
  again=$(sh "$0" --one "$ROOT/$label")
  case "$again" in *" PASS") echo "$label flaky (passed on retry)" ;;
                   *) echo "$again"; fails="$fails$label
" ;; esac
done
if [ -z "$fails" ]; then echo "ALL GREEN ($(echo "$res" | wc -l | tr -d ' ') guards)"; exit 0; fi
echo
printf "RED — failed:\n%s" "$fails"
exit 1
