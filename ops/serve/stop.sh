#!/bin/zsh
# Stop the detached dev servers started by start.sh (kills each supervisor's
# whole process group, so the server and its workers go too).
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
PIDFILE="$REPO/.conductor/serve.pids"
[ -f "$PIDFILE" ] || { echo "nothing to stop (no $PIDFILE)"; exit 0; }

while read -r name pgid; do
  [ -n "$pgid" ] || continue
  if kill -TERM -"$pgid" 2>/dev/null; then
    echo "stopped $name (pgid $pgid)"
  else
    echo "$name (pgid $pgid) was not running"
  fi
done < "$PIDFILE"
rm -f "$PIDFILE"
echo "Done."
