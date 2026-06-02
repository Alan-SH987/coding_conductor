#!/bin/zsh
# Backend launcher for launchd. Computes its own location so there is no
# hard-coded path. Run under `zsh -lc` (login shell) by the plist so the user's
# PATH (homebrew / nvm / ~/.local/bin) is present — the backend shells out to
# git / claude / codex and needs them findable.
cd "$(dirname "$0")/../../backend" || exit 1
exec ./.venv/bin/python -m uvicorn app.main:app --reload --port 8010
