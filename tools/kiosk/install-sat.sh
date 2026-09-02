#!/bin/bash
# Install kiosk-guard on a gizmo wall-display box (clawd-sat). Idempotent; re-run
# any time. Run AS the display user on the box itself:
#   cd ~/clawd/clawd-harness && git pull --ff-only && bash tools/kiosk/install-sat.sh
#
# What it does:
#   1. gives the kiosk Chrome a DevTools port (edits ~/gizmo/kiosk-keeper.sh's
#      launch flags — the guard closes intruder pages through that port);
#   2. installs + starts launchd agent com.gizmo.kioskguard (KeepAlive) running
#      tools/kiosk/kiosk-guard.py from this checkout, so `git pull` updates it;
#   3. bounces the kiosk Chrome so the new flag takes effect (the keeper relaunches
#      it within ~15 s), then prints a verification block.
set -u
GIZMO="${GIZMO_DIR:-$HOME/gizmo}"
KEEPER="$GIZMO/kiosk-keeper.sh"
PORT="${KIOSK_CDP_PORT:-9223}"
URL="${KIOSK_URL:-http://127.0.0.1:$(sed -n 's/^PORT=//p' "$GIZMO/.env" 2>/dev/null | tail -1)}"
[ "$URL" = "http://127.0.0.1:" ] && URL="http://127.0.0.1:7912"
PROFILE="${KIOSK_PROFILE:-$HOME/.gizmo-chrome}"
HERE="$(cd "$(dirname "$0")" && pwd)"
PY="$(command -v python3)"
LABEL=com.gizmo.kioskguard
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$GIZMO/state/kioskguard.log"
mkdir -p "$GIZMO/state" "$HOME/Library/LaunchAgents"

echo "== 1. DevTools port on the kiosk launch ($KEEPER)"
if [ ! -f "$KEEPER" ]; then
  echo "   !! $KEEPER not found — add --remote-debugging-port=$PORT to the kiosk Chrome launch by hand"
elif grep -q -- "--remote-debugging-port=" "$KEEPER"; then
  echo "   already has --remote-debugging-port ($(grep -o -- '--remote-debugging-port=[0-9]*' "$KEEPER" | head -1))"
else
  cp "$KEEPER" "$KEEPER.bak-$(date +%Y%m%d%H%M%S)"
  # the launch() block passes flags one per line ending in ' \'; hook the first one
  if grep -q -- '--autoplay-policy=no-user-gesture-required \\' "$KEEPER"; then
    sed -i '' "s|^\([[:space:]]*\)--autoplay-policy=no-user-gesture-required \\\\\$|\1--autoplay-policy=no-user-gesture-required \\\\\n\1--remote-debugging-port=$PORT \\\\|" "$KEEPER"
  fi
  if grep -q -- "--remote-debugging-port=$PORT" "$KEEPER"; then
    echo "   added --remote-debugging-port=$PORT"
    if git -C "$GIZMO" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
      git -C "$GIZMO" add kiosk-keeper.sh && git -C "$GIZMO" commit -q -m "kiosk-keeper: expose DevTools port $PORT for kiosk-guard" && echo "   committed in $GIZMO"
    fi
  else
    echo "   !! could not patch automatically — add --remote-debugging-port=$PORT to launch() in $KEEPER by hand"
  fi
fi

echo "== 2. launchd agent $LABEL"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<!-- kiosk-guard: closes anything that pops up in front of the wall display and
     puts the home screen back (see tools/kiosk/README.md in clawd-harness). -->
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$PY</string><string>$HERE/kiosk-guard.py</string></array>
  <key>WorkingDirectory</key><string>$HERE</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>KIOSK_URL</key><string>$URL</string>
    <key>KIOSK_PROFILE</key><string>$PROFILE</string>
    <key>KIOSK_CDP_PORT</key><string>$PORT</string>
    <key>KIOSK_INTERVAL</key><string>2</string>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>5</integer>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
  <key>ProcessType</key><string>Interactive</string>
</dict>
</plist>
PL
launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$PLIST" && echo "   loaded ($PY $HERE/kiosk-guard.py, log $LOG)"

echo "== 3. bounce the kiosk Chrome so it comes back with the DevTools port"
if pgrep -f "user-data-dir=$PROFILE" >/dev/null; then
  if pgrep -f "user-data-dir=$PROFILE" | xargs -I{} ps -o command= -p {} | grep -q -- "--remote-debugging-port=$PORT"; then
    echo "   already running with the port"
  else
    pkill -f "user-data-dir=$PROFILE"; echo "   killed; the keeper relaunches it (~15 s)"
  fi
else
  echo "   not running; the keeper launches it"
fi

echo "== 4. verify (waiting 30 s)"
sleep 30
echo "-- kiosk pid: $(pgrep -f "user-data-dir=$PROFILE" | head -1)   front pid: $(lsappinfo info -only pid "$(lsappinfo front)")"
echo "-- pages on the kiosk profile:"
curl -s -m 3 "http://127.0.0.1:$PORT/json/list" | python3 -c 'import json,sys
for t in json.load(sys.stdin):
    if t.get("type")=="page": print("   ", t.get("url"))' 2>/dev/null || echo "   (no DevTools port yet)"
echo "-- guard log tail:"; tail -n 12 "$LOG"
