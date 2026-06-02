#!/bin/zsh
# Start the Coding Conductor dev servers, detached and self-restarting.
#
# Run this ONCE from a terminal (it inherits your PATH + Desktop access, which a
# launchd agent can't get for a repo under ~/Desktop). Each server runs under a
# supervisor that restarts it on crash. `perl setsid` puts each supervisor in its
# own session with no controlling terminal, so they survive the terminal closing.
# Not boot-automatic — re-run after a reboot.
#
#   ./ops/serve/start.sh      # start
#   ./ops/serve/stop.sh       # stop
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
LOGDIR="$REPO/.conductor/logs"
PIDFILE="$REPO/.conductor/serve.pids"
SUP="$REPO/ops/serve/_supervise.sh"
# setsid (new session => own process group, detached from any terminal), then
# exec the supervisor. macOS has no setsid(1), so do it via perl.
DETACH='use POSIX; setsid(); exec @ARGV or die $!'
mkdir -p "$LOGDIR"
: > "$PIDFILE"

perl -e "$DETACH" "$SUP" backend "$REPO/backend" "$LOGDIR/backend.log" \
  ./.venv/bin/python -m uvicorn app.main:app --reload --port 8010 >/dev/null 2>&1 &
echo "backend $!" >> "$PIDFILE"
echo "started backend  (pgid $!) -> $LOGDIR/backend.log"

perl -e "$DETACH" env NEXT_PUBLIC_API_BASE=http://localhost:8010 \
  "$SUP" frontend "$REPO/frontend" "$LOGDIR/frontend.log" \
  npm run dev >/dev/null 2>&1 &
echo "frontend $!" >> "$PIDFILE"
echo "started frontend (pgid $!) -> $LOGDIR/frontend.log"

echo ""
echo "Detached. They survive this terminal closing and auto-restart on crash."
echo "  backend  :8010   frontend :3000"
echo "  logs:  $LOGDIR/"
echo "  stop:  $REPO/ops/serve/stop.sh"
