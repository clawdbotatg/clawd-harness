#!/usr/bin/env bash
# Keep claude + codex on the latest version. Runs daily on every fleet box.
# Running sessions keep their old binary until they restart.
#
#   tools/update-clis.sh            # update now
#   tools/update-clis.sh install    # daily job (launchd on macOS, systemd --user on Linux)
#   tools/update-clis.sh uninstall
set -uo pipefail

LABEL="com.clawd.update-clis"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SELF="$HERE/update-clis.sh"
export PATH="$HOME/.local/bin:$HOME/.local/share/mise/shims:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

update() {
  echo "== $(date '+%F %T')"
  # mise-managed tools (omen): `mise upgrade` moves "latest" pins forward.
  if command -v mise >/dev/null && mise ls --current 2>/dev/null | grep -qE '^(claude|codex) '; then
    mise upgrade claude codex
    return
  fi
  if command -v claude >/dev/null; then
    claude update
  fi
  if command -v codex >/dev/null; then
    real="$(readlink -f "$(command -v codex)" 2>/dev/null || true)"
    if [[ "$real" == *node_modules/@openai/codex* ]]; then
      npm install -g @openai/codex@latest
    elif command -v brew >/dev/null && brew list codex >/dev/null 2>&1; then
      brew upgrade codex || true
    fi
    codex --version
  fi
}

case "${1:-}" in
  "") update ;;
  install)
    if [[ "$(uname)" == Darwin ]]; then
      PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
      LOG="$HOME/Library/Logs/clawd-update-clis.log"
      mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
      cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>$SELF</string></array>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>4</integer><key>Minute</key><integer>17</integer></dict>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
EOF
      launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
      launchctl bootstrap "gui/$(id -u)" "$PLIST"
      echo "installed: daily 04:17, log $LOG"
    else
      D="$HOME/.config/systemd/user"; mkdir -p "$D"
      printf '[Unit]\nDescription=update claude + codex\n[Service]\nType=oneshot\nExecStart=/bin/bash %s\n' "$SELF" > "$D/clawd-update-clis.service"
      printf '[Unit]\nDescription=daily claude + codex update\n[Timer]\nOnCalendar=*-*-* 04:17\nPersistent=true\n[Install]\nWantedBy=timers.target\n' > "$D/clawd-update-clis.timer"
      systemctl --user daemon-reload
      systemctl --user enable --now clawd-update-clis.timer
      systemctl --user start clawd-update-clis.service
      echo "installed: daily 04:17, journalctl --user -u clawd-update-clis"
    fi
    ;;
  uninstall)
    if [[ "$(uname)" == Darwin ]]; then
      launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
      rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
    else
      systemctl --user disable --now clawd-update-clis.timer 2>/dev/null || true
      rm -f "$HOME/.config/systemd/user/clawd-update-clis."{service,timer}
    fi
    echo "uninstalled."
    ;;
  *) echo "usage: $0 [install|uninstall]"; exit 1 ;;
esac
