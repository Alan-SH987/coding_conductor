#!/usr/bin/env bash
# Coding Conductor — start backend + frontend for local dev. Ctrl-C stops both.
# Ports default to 8010 / 3000 and can be overridden: BACKEND_PORT=8011 FRONTEND_PORT=3001 ./dev.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_PORT="${BACKEND_PORT:-8010}"
FRONTEND_PORT="${FRONTEND_PORT:-3000}"

port_busy() { lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; }
for p in "$BACKEND_PORT" "$FRONTEND_PORT"; do
  if port_busy "$p"; then
    echo "ERROR: port $p is already in use. Stop what's on it, or override the port." >&2
    exit 1
  fi
done

# Keep deps in lockstep before starting. A merged Conductor task can add a dep to
# pyproject.toml / package.json without reinstalling it, so the venv / node_modules
# drift and the server then crashes at import. Re-sync here (fast no-op when already
# satisfied). Backend installs only the *declared* deps — no editable project install,
# so nothing writes an egg-info that would dirty main.
echo "syncing deps…"
(
  cd "$ROOT/backend"
  ./.venv/bin/python - <<'PY'
import pathlib, subprocess, sys, tomllib
deps = tomllib.loads(pathlib.Path("pyproject.toml").read_text())["project"]["dependencies"]
subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", *deps])
PY
)
( cd "$ROOT/frontend" && npm install --no-audit --no-fund )

pids=()
cleanup() {
  trap - INT TERM EXIT
  [ "${#pids[@]}" -gt 0 ] && kill "${pids[@]}" 2>/dev/null || true
}
trap cleanup INT TERM EXIT

echo "backend:  http://localhost:$BACKEND_PORT   (Ctrl-C stops both)"
( cd "$ROOT/backend" && exec ./.venv/bin/python -m uvicorn app.main:app --reload --port "$BACKEND_PORT" ) &
pids+=($!)

echo "frontend: http://localhost:$FRONTEND_PORT   (API_BASE -> http://localhost:$BACKEND_PORT)"
# Call next directly (not `npm run dev`, whose script hardcodes -p 3000) so FRONTEND_PORT is honored.
( cd "$ROOT/frontend" && NEXT_PUBLIC_API_BASE="http://localhost:$BACKEND_PORT" exec ./node_modules/.bin/next dev -p "$FRONTEND_PORT" ) &
pids+=($!)

wait
