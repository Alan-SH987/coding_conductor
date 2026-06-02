#!/bin/sh
# Stop and remove the Coding Conductor launchd agents.
set -eu

AGENTS="$HOME/Library/LaunchAgents"
for label in com.codingconductor.backend com.codingconductor.frontend; do
  plist="$AGENTS/$label.plist"
  launchctl unload "$plist" 2>/dev/null || true
  rm -f "$plist"
  echo "removed $label"
done
echo "Done. The dev servers are no longer launchd-managed (running ones stopped)."
