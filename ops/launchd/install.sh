#!/bin/sh
# Install launchd agents so the Coding Conductor dev servers run as persistent
# user services: start at login, restart on crash, survive terminal close.
#
#   ./ops/launchd/install.sh     # generate plists + load them
#   ./ops/launchd/uninstall.sh   # stop + remove them
#
# These are LaunchAgents (run as you, no sudo/root). They invoke the runner
# scripts via `zsh -lc` so your normal PATH is available.
set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"      # <repo>/ops/launchd
REPO="$(cd "$HERE/../.." && pwd)"
AGENTS="$HOME/Library/LaunchAgents"
LOGDIR="$REPO/.conductor/logs"             # gitignored
mkdir -p "$AGENTS" "$LOGDIR"

write_and_load() {
  label="$1"; script="$2"
  plist="$AGENTS/$label.plist"
  cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/zsh</string>
    <string>-lc</string>
    <string>exec '$script'</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$LOGDIR/$label.log</string>
  <key>StandardErrorPath</key><string>$LOGDIR/$label.log</string>
</dict>
</plist>
EOF
  launchctl unload "$plist" 2>/dev/null || true
  launchctl load "$plist"
  echo "loaded $label"
}

write_and_load com.codingconductor.backend  "$REPO/ops/launchd/run-backend.sh"
write_and_load com.codingconductor.frontend "$REPO/ops/launchd/run-frontend.sh"

echo ""
echo "Done. backend :8010 + frontend :3000 are now launchd-managed."
echo "  logs:   $LOGDIR/"
echo "  status: launchctl list | grep codingconductor"
echo "  stop:   $REPO/ops/launchd/uninstall.sh"
