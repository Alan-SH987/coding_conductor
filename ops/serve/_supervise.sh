#!/bin/zsh
# Supervisor: run a server and restart it if it exits. Not called directly —
# start.sh launches one of these per server, detached via nohup.
#   _supervise.sh <name> <workdir> <logfile> <cmd> [args...]
name="$1"; workdir="$2"; log="$3"; shift 3
cd "$workdir" || exit 1
while true; do
  echo "[$(date '+%F %T')] starting $name" >> "$log"
  "$@" >> "$log" 2>&1
  echo "[$(date '+%F %T')] $name exited ($?); restarting in 2s" >> "$log"
  sleep 2
done
