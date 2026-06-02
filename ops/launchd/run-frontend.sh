#!/bin/zsh
# Frontend launcher for launchd. Same rationale as run-backend.sh. The inline
# NEXT_PUBLIC_API_BASE is required — Next.js bakes it at dev-server start so the
# browser talks to the backend on :8010.
cd "$(dirname "$0")/../../frontend" || exit 1
export NEXT_PUBLIC_API_BASE=http://localhost:8010
exec npm run dev
