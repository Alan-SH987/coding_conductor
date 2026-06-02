import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.orchestration import router as orchestration_router
from app.api.routes import router as health_router
from app.config import settings
from app.gitops.errors import (
    DirtyRepoError,
    GitCommandError,
    GitOpsError,
    NotAGitRepo,
    WorktreeExistsError,
)
from app.storage.db import init_db


def _scrub_agent_harness_env() -> None:
    """Strip env vars that leak in when this backend is itself launched from a
    Claude Code / agent session (e.g. a dev agent's shell).

    They make the `claude` / `codex` CLI we spawn per task believe it is nested
    inside another agent (CLAUDECODE=1, CLAUDE_CODE_SESSION_ID=...), or override
    its subscription auth with an empty ANTHROPIC_API_KEY (api_key_source becomes
    "none"). Removing them lets each spawned CLI run as a clean top-level process
    on the logged-in subscription — so Conductor works no matter where it runs.
    """
    for key in list(os.environ):
        if (
            key in ("CLAUDECODE", "CLAUDE_EFFORT")
            or key.startswith("CLAUDE_CODE")
            or key.startswith("CLAUDE_AGENT")
        ):
            os.environ.pop(key, None)
    if not (os.environ.get("ANTHROPIC_API_KEY") or "").strip():
        os.environ.pop("ANTHROPIC_API_KEY", None)


# Run at import — before the app spawns any agent CLI subprocess.
_scrub_agent_harness_env()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(health_router)
app.include_router(orchestration_router)


def _error(status: int, code: str, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": code, "detail": detail})


# Map GitOps domain failures onto HTTP status codes. Order matters: register the
# specific subclasses before the GitOpsError catch-all.
@app.exception_handler(NotAGitRepo)
async def _handle_not_a_git_repo(request: Request, exc: NotAGitRepo):
    return _error(400, "not_a_git_repo", str(exc))


@app.exception_handler(DirtyRepoError)
async def _handle_dirty_repo(request: Request, exc: DirtyRepoError):
    return _error(409, "dirty_repo", str(exc))


@app.exception_handler(WorktreeExistsError)
async def _handle_worktree_exists(request: Request, exc: WorktreeExistsError):
    return _error(409, "worktree_exists", str(exc))


@app.exception_handler(GitCommandError)
async def _handle_git_command(request: Request, exc: GitCommandError):
    return _error(500, "git_command_failed", str(exc))


@app.exception_handler(GitOpsError)
async def _handle_gitops(request: Request, exc: GitOpsError):
    return _error(500, "gitops_error", str(exc))
