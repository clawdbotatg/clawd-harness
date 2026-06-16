#!/usr/bin/env bash
# Run the clawd-harness AI controller (the fleet PM brain + chat UI) as a launchd
# LaunchAgent: survives closing the terminal, restarts if it dies (KeepAlive),
# starts at login (RunAtLoad). It's a *client* of the harness — it dials the
# harness WS, so the harness daemon (com.clawd.harness) should be running too.
#
#   ./daemon-controller.sh install     # chat UI on CHAT_PORT (default 8799)
#   ./daemon-controller.sh uninstall
#   ./daemon-controller.sh restart
#   ./daemon-controller.sh status
#   ./daemon-controller.sh logs
#
# Env knobs (override before install): CHAT_PORT, CONTROLLER_AUTONOMY
# (readonly|confirm|auto), CONTROLLER_BRAIN (bankr|claude-code), CONTROLLER_MODEL.
set -euo pipefail

LABEL="com.clawd.controller"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/clawd-controller.log"
PY="$(command -v python3)"
CLAUDE_BIN="${CLAUDE_BIN:-$(command -v claude)}"   # for the claude-code brain backend
CHAT_PORT="${CHAT_PORT:-8799}"
AUTONOMY="${CONTROLLER_AUTONOMY:-confirm}"          # safe-but-useful default
BRAIN="${CONTROLLER_BRAIN:-bankr}"
DOMAIN="gui/$(id -u)"

cmd="${1:-}"
case "$cmd" in
  install)
    pkill -f "controller.*serve" 2>/dev/null || true
    sleep 1
    mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
    cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>-m</string>
    <string>controller</string>
    <string>serve</string>
  </array>
  <key>WorkingDirectory</key><string>$HERE</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>CONTROLLER_CHAT_PORT</key><string>$CHAT_PORT</string>
    <key>CONTROLLER_AUTONOMY</key><string>$AUTONOMY</string>
    <key>CONTROLLER_BRAIN</key><string>$BRAIN</string>
    <key>CLAUDE_BIN</key><string>$CLAUDE_BIN</string>
    <key>PATH</key><string>$HOME/.local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
PLISTEOF
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    launchctl bootstrap "$DOMAIN" "$PLIST"
    launchctl enable "$DOMAIN/$LABEL" 2>/dev/null || true
    echo "installed + loaded. chat UI → http://127.0.0.1:$CHAT_PORT  (autonomy=$AUTONOMY, brain=$BRAIN)"
    echo "log=$LOG"
    sleep 2; tail -3 "$LOG" 2>/dev/null || true
    ;;
  uninstall)
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "uninstalled."
    ;;
  restart)
    launchctl kickstart -k "$DOMAIN/$LABEL"
    echo "restarted."
    ;;
  status)
    launchctl print "$DOMAIN/$LABEL" 2>/dev/null | grep -E "state =|pid =" || echo "not loaded"
    ;;
  logs)
    tail -f "$LOG"
    ;;
  *)
    echo "usage: $0 {install|uninstall|restart|status|logs}"; exit 1 ;;
esac
