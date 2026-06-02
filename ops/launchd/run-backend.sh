#!/bin/zsh
# Backend launcher for launchd. Computes its own location so there is no
# hard-coded path. Run under `zsh -lc` (login shell) by the plist so the user's
# PATH (homebrew / nvm / ~/.local/bin) is present — the backend shells out to
# git / claude / codex and needs them findable.
cd "$(dirname "$0")/../../backend" || exit 1
# No --reload: a file-watch restart would kill an in-flight agent task run.
exec ./.venv/bin/python -m uvicorn app.main:app --port 8010
