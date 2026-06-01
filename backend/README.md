# Coding Conductor — Backend

Credential-less orchestrator over headless AI coding CLIs. See [`../docs/mvp-foundation.md`](../docs/mvp-foundation.md).

## Dev setup

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"      # or: pip install fastapi "uvicorn[standard]" sqlmodel pydantic-settings pytest httpx
```

## Run

```bash
uvicorn app.main:app --reload --port 8010
# health check:
curl localhost:8010/health
```

## Test

```bash
pytest
```

## Layout

```
app/
  main.py          # FastAPI entry (lifespan -> init_db)
  config.py        # settings (env prefix CC_)
  api/routes.py    # /health + placeholders
  gitops/          # GitOps Engine: worktree / branch / diff / merge
  storage/         # SQLite models + engine
tests/             # gitops + app smoke tests
```
