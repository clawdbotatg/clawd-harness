#!/bin/bash
# kick-worker.sh <machine-id> — restart a fleet box's stale worker REMOTELY,
# by spawning a claude session on that box (via the PM API on the relay) and
# asking it to do the restart. Usage:
#   tools/kick-worker.sh clawd-leftclaw
# Needs ssh access to the relay (ubuntu@174.129.67.164). Safe to re-run.
set -euo pipefail
M="${1:?usage: kick-worker.sh <machine-id>}"
RELAY="ubuntu@174.129.67.164"
API="http://127.0.0.1:8799/api/tool"

pm() {  # pm <json-payload-on-stdin> — POST one PM tool call through the relay
  ssh -o ConnectTimeout=10 "$RELAY" \
    "curl -s -m 60 -X POST $API -H 'Content-Type: application/json' --data-binary @-"
}

echo "== spawning a session in $M's clawd-harness self project…"
SPAWN=$(printf '{"name":"spawn","args":{"machine":"%s","pid":"self","confirm":true}}' "$M" | pm)
echo "$SPAWN"
CID=$(printf '%s' "$SPAWN" | python3 -c '
import json,sys
r = json.load(sys.stdin).get("result") or {}
if isinstance(r, str):
    import re; m = re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", r); print(m.group(0) if m else "")
else:
    print(r.get("cid") or (r.get("session") or {}).get("cid") or "")')
if [ -z "$CID" ]; then
  echo "!! could not extract a cid from the spawn reply above — is $M connected?" >&2
  exit 1
fi
echo "== cid=$CID — sending the restart instructions…"
sleep 5   # let the fresh claude finish booting before the prompt lands

python3 - "$M" "$CID" <<'PYEOF' | pm
import json, sys
machine, cid = sys.argv[1], sys.argv[2]
text = (
  "You are on " + machine + ". Restart this box's fleet worker — it is "
  "running stale code. Do exactly this and report each result:\n"
  "1. cd ~/clawd-harness && git pull\n"
  "2. PY=$(plutil -extract ProgramArguments.0 raw "
  "~/Library/LaunchAgents/com.clawd.fleet-worker.plist); \"$PY\" -c "
  "'import cryptography' — if the import fails, run \"$PY\" -m pip install "
  "cryptography FIRST (brew python upgrades silently wipe it; a worker "
  "restarted without it cannot do E2E and the phone loses this box).\n"
  "3. launchctl kickstart -k gui/$(id -u)/com.clawd.fleet-worker\n"
  "4. Verify: 'launchctl list | grep fleet-worker' shows a PID, and "
  "'tail -5 ~/Library/Logs/clawd-fleet-worker.log' shows a fresh "
  "'connected to wss://h.atg.link'. If anything fails, say exactly what."
)
print(json.dumps({"name": "ask",
                  "args": {"machine": machine, "cid": cid,
                           "text": text, "confirm": True}}))
PYEOF
echo
echo "== done. The session on $M is doing the restart; check it in the fleet UI"
echo "   (machine $M → clawd-harness → newest session), cid=$CID"
