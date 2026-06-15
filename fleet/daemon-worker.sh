#!/usr/bin/env bash
# Run the clawd-fleet harness-proxy worker as a launchd LaunchAgent so it survives
# closing the terminal, restarts if it dies (KeepAlive), and starts at login
# (RunAtLoad). This is the always-on companion to the harness daemon (daemon.sh):
# the worker dials the relay and proxies this machine into the fleet, so the phone
# can reach it without a terminal open.
#
# Config (relay url, worker token, harness ws, machine id) comes from the
# gitignored fleet.env — the worker self-loads it (_load_env_file), so the secret
# never lands in the plist. Any extra args after `install` are passed to worker.py
# (e.g. --host atg).
#
#   ./daemon-worker.sh install [-- worker args…]   # e.g. install --host atg
#   ./daemon-worker.sh uninstall
#   ./daemon-worker.sh restart
#   ./daemon-worker.sh status
#   ./daemon-worker.sh logs
set -euo pipefail

LABEL="com.clawd.fleet-worker"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/clawd-fleet-worker.log"
PY="$(command -v python3)"
DOMAIN="gui/$(id -u)"

cmd="${1:-}"
case "$cmd" in
  install)
    shift || true
    [ "${1:-}" = "--" ] && shift || true   # allow an explicit `--` before worker args
    if [ ! -f "$HERE/fleet.env" ]; then
      echo "⚠ $HERE/fleet.env not found — create it (FLEET_WORKER_TOKEN, FLEET_RELAY,"
      echo "  HARNESS_WS, FLEET_MACHINE) before installing. See fleet/CLAUDE.md." >&2
      exit 1
    fi
    # stop any other worker (manual run, full-path or relative, or a prior daemon
    # process) so the daemon is the only one registering this machine. The bracket
    # trick keeps pkill from matching its own command line.
    pkill -f "[w]orker.py" 2>/dev/null || true
    sleep 1
    mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
    # build the worker arg <string> entries from any pass-through args
    EXTRA=""
    for a in "$@"; do
      EXTRA+="    <string>$a</string>"$'\n'
    done
    cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>$HERE/worker.py</string>
$EXTRA  </array>
  <key>WorkingDirectory</key><string>$HERE</string>
  <key>EnvironmentVariables</key>
  <dict>
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
    echo "installed + loaded. log=$LOG"
    sleep 2; grep -E "connected|identity|⚠|error" "$LOG" | tail -3 || true
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
    echo "usage: $0 {install [-- worker args…]|uninstall|restart|status|logs}"; exit 1 ;;
esac
