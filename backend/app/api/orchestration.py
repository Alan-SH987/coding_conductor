"""REST surface over the Orchestrator.

The thinnest possible HTTP layer: every endpoint delegates to an Orchestrator
method. Resource shape:

    POST   /projects                       create + register a git repo
    GET    /projects
    GET    /projects/{pid}
    POST   /projects/{pid}/tasks           queue a task (draft)
    GET    /projects/{pid}/tasks
    GET    /tasks/{tid}
    POST   /tasks/{tid}/run                launch the agent (background, 202)
    GET    /tasks/{tid}/stream             SSE live event stream for the run
    GET    /tasks/{tid}/diff               captured unified diff
    POST   /tasks/{tid}/review             advisory AI review of the diff
    GET    /tasks/{tid}/review             latest review (or null)
    POST   /tasks/{tid}/revise             re-run in worktree to address review
    POST   /tasks/{tid}/approve            merge into main + cleanup
    POST   /tasks/{tid}/reject             discard the worktree
    GET    /tasks/{tid}/runs               run history (inspection)
    GET    /runs/{rid}/events              normalized event log (pre-SSE)
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app import skills
from app.api.deps import get_orchestrator
from app.orchestrator import (
    AlreadyPlanned,
    AlreadyRunning,
    NotRunnable,
    Orchestrator,
    PlanError,
    QuotaExceeded,
    ReviewError,
    ReviseError,
)
from app.orchestrator.concurrency_limiter import ConcurrencyLimitReached
from app.orchestrator.queue_manager import QueueFull, TaskAlreadyQueued
from app.storage.models import QueuePriority
from app.orchestrator.model_router import ModelRouter
from app.storage import models

router = APIRouter()

# How often the SSE stream tails the events table for new rows.
STREAM_POLL_SECONDS = 0.4


def _sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


class ProjectCreate(BaseModel):
    name: str
    path: str
    init: bool = True  # auto `git init` an empty/non-git path instead of rejecting


class TaskCreate(BaseModel):
    title: str
    description: str = ""
    agent: str = "claude"
    source_task_id: Optional[int] = None  # Provenance: task this was derived from


class AttachmentOut(BaseModel):
    filename: str
    path: str
    content_type: str
    size: int


_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024


def _safe_attachment_name(filename: str) -> str:
    name = Path(filename or "attachment").name.strip()
    name = _SAFE_FILENAME.sub("_", name).strip("._")
    return name or "attachment"


def _unique_path(directory: Path, filename: str) -> Path:
    stem = Path(filename).stem or "attachment"
    suffix = Path(filename).suffix
    candidate = directory / filename
    counter = 2
    while candidate.exists():
        candidate = directory / f"{stem}-{counter}{suffix}"
        counter += 1
    return candidate


# ---------- agents ----------
@router.get("/agents")
def list_agents(orch: Orchestrator = Depends(get_orchestrator)):
    agents = [
        {"name": name, "capabilities": sorted(adapter.capabilities)}
        for name, adapter in orch.adapters.items()
    ]
    # Add "auto" as a special option for intelligent routing
    agents.insert(0, {
        "name": "auto",
        "capabilities": ["intelligent_routing"],
        "description": "Automatically select the best model based on task complexity"
    })
    return agents


class ModelRecommendationRequest(BaseModel):
    title: str
    description: str = ""


@router.post("/agents/recommend")
def recommend_model(
    body: ModelRecommendationRequest,
    orch: Orchestrator = Depends(get_orchestrator)
):
    """Get a model recommendation based on task complexity."""
    available_models = list(orch.adapters.keys())
    router = ModelRouter(available_models=available_models)
    model = router.select_model(body.title, body.description)
    explanation = router.explain_choice(model, body.title, body.description)

    return {
        "recommended_model": model,
        "explanation": explanation,
        "available_models": available_models
    }


@router.get("/agents/health")
async def agents_health(orch: Orchestrator = Depends(get_orchestrator)):
    """Probe each agent CLI and report its current status.

    On-demand only: every call spawns the CLIs and spends a negligible probe
    (a one-word completion), so the UI triggers this from a button rather than
    on every page load. Probes run concurrently and each is bounded by a timeout
    so a wedged CLI can never hang the request.
    """
    async def probe(name: str, adapter) -> dict:
        base = {"name": name, "ok": False, "auth_ok": False, "rate_limited": False,
                "version": "", "detail": "", "status": "unavailable"}
        try:
            hs = await asyncio.wait_for(adapter.healthcheck(), timeout=45)
        except asyncio.TimeoutError:
            return {**base, "detail": "health probe timed out"}
        except Exception as exc:  # noqa: BLE001
            return {**base, "detail": str(exc)}
        if not hs.ok:
            status = "unavailable"
        elif not hs.auth_ok:
            status = "unauthenticated"
        elif hs.rate_limited:
            status = "rate_limited"
        else:
            status = "available"
        return {"name": name, "ok": hs.ok, "auth_ok": hs.auth_ok,
                "rate_limited": hs.rate_limited, "version": hs.version,
                "detail": hs.detail, "status": status}

    return await asyncio.gather(*(probe(n, a) for n, a in orch.adapters.items()))


# ---------- projects ----------
@router.post("/projects", response_model=models.Project, status_code=201)
def create_project(body: ProjectCreate, orch: Orchestrator = Depends(get_orchestrator)):
    return orch.create_project(body.name, body.path, init=body.init)


class BrowseDirectoryRequest(BaseModel):
    path: str = ""  # Empty string means home directory


class DirectoryEntry(BaseModel):
    name: str
    path: str
    is_dir: bool
    is_git: bool = False  # True if this directory contains a .git folder


class BrowseDirectoryResponse(BaseModel):
    current_path: str
    parent_path: str | None
    entries: list[DirectoryEntry]


@router.post("/browse-directory", response_model=BrowseDirectoryResponse)
def browse_directory(body: BrowseDirectoryRequest):
    """Browse directories on the server filesystem.

    Used by the frontend directory picker when adding projects.
    Only returns directories, not files.
    """
    import os

    # Start from home directory if path is empty
    target_path = body.path.strip() if body.path else str(Path.home())
    target = Path(target_path).expanduser().resolve()

    if not target.exists():
        raise HTTPException(400, f"Path does not exist: {target}")
    if not target.is_dir():
        raise HTTPException(400, f"Path is not a directory: {target}")

    entries: list[DirectoryEntry] = []
    try:
        for item in sorted(target.iterdir(), key=lambda x: x.name.lower()):
            # Skip hidden files/folders (starting with .)
            if item.name.startswith("."):
                continue
            if item.is_dir():
                is_git = (item / ".git").exists()
                entries.append(DirectoryEntry(
                    name=item.name,
                    path=str(item),
                    is_dir=True,
                    is_git=is_git,
                ))
    except PermissionError:
        raise HTTPException(403, f"Permission denied: {target}")

    parent_path = str(target.parent) if target.parent != target else None

    return BrowseDirectoryResponse(
        current_path=str(target),
        parent_path=parent_path,
        entries=entries,
    )


@router.get("/projects", response_model=list[models.Project])
def list_projects(
    include_archived: bool = False,
    orch: Orchestrator = Depends(get_orchestrator),
):
    return orch.list_projects(include_archived=include_archived)


@router.get("/projects/{project_id}", response_model=models.Project)
def get_project(project_id: int, orch: Orchestrator = Depends(get_orchestrator)):
    proj = orch.get_project(project_id)
    if proj is None:
        raise HTTPException(404, f"project {project_id} not found")
    return proj


@router.post("/projects/{project_id}/pin", response_model=models.Project)
def pin_project(project_id: int, orch: Orchestrator = Depends(get_orchestrator)):
    proj = orch.pin_project(project_id, pinned=True)
    if proj is None:
        raise HTTPException(404, f"project {project_id} not found")
    return proj


@router.post("/projects/{project_id}/unpin", response_model=models.Project)
def unpin_project(project_id: int, orch: Orchestrator = Depends(get_orchestrator)):
    proj = orch.pin_project(project_id, pinned=False)
    if proj is None:
        raise HTTPException(404, f"project {project_id} not found")
    return proj


class QuotaUpdate(BaseModel):
    quota_tokens: Optional[int] = None
    quota_cost_usd: Optional[float] = None


@router.patch("/projects/{project_id}/quotas", response_model=models.Project)
def update_project_quotas(
    project_id: int,
    body: QuotaUpdate,
    orch: Orchestrator = Depends(get_orchestrator)
):
    """Update quota settings for a project."""
    proj = orch.update_project_quotas(project_id, body.quota_tokens, body.quota_cost_usd)
    if proj is None:
        raise HTTPException(404, f"project {project_id} not found")
    return proj


class VerifyUpdate(BaseModel):
    verify_cmd: Optional[str] = None


@router.patch("/projects/{project_id}/verify", response_model=models.Project)
def update_project_verify(
    project_id: int,
    body: VerifyUpdate,
    orch: Orchestrator = Depends(get_orchestrator),
):
    """Set or clear the pre-merge verify command run before a task merges."""
    proj = orch.update_project_verify(project_id, body.verify_cmd)
    if proj is None:
        raise HTTPException(404, f"project {project_id} not found")
    return proj


class AutoHealUpdate(BaseModel):
    rounds: int = 1


@router.patch("/projects/{project_id}/auto-heal", response_model=models.Project)
def update_project_auto_heal(
    project_id: int,
    body: AutoHealUpdate,
    orch: Orchestrator = Depends(get_orchestrator),
):
    """Set the auto self-heal round budget (0 = off; clamped to [0, 5])."""
    proj = orch.update_project_auto_heal(project_id, body.rounds)
    if proj is None:
        raise HTTPException(404, f"project {project_id} not found")
    return proj


class SkillsUpdate(BaseModel):
    enabled: list[str] = []


@router.get("/skills")
def list_skills():
    """All globally-installed skills (~/.conductor/skills/<name>/SKILL.md)."""
    return skills.list_skills()


@router.patch("/projects/{project_id}/skills", response_model=models.Project)
def update_project_skills(
    project_id: int,
    body: SkillsUpdate,
    orch: Orchestrator = Depends(get_orchestrator),
):
    """Set which installed skills are enabled (injected) for this project."""
    proj = orch.update_project_skills(project_id, body.enabled)
    if proj is None:
        raise HTTPException(404, f"project {project_id} not found")
    return proj


@router.post("/projects/{project_id}/distill")
async def distill_insights(project_id: int, orch: Orchestrator = Depends(get_orchestrator)):
    """Distill this project's accumulated handoffs into high-level insights.

    Manually triggered (it runs an LLM, so it's off the task hot path). Writes
    insights.md alongside — never touching the human-curated global.md. The LLM
    call runs in the background so this request returns immediately.
    """
    if orch.get_project(project_id) is None:
        raise HTTPException(404, f"project {project_id} not found")
    try:
        result = orch.start_distill_insights(project_id)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(409, str(exc))
    return result


@router.get("/projects/{project_id}/usage")
def get_project_usage(project_id: int, orch: Orchestrator = Depends(get_orchestrator)):
    """Get current usage statistics for a project."""
    proj = orch.get_project(project_id)
    if proj is None:
        raise HTTPException(404, f"project {project_id} not found")

    usage = orch.get_project_usage(project_id)

    return {
        "project_id": project_id,
        "usage": usage,
        "quotas": {
            "quota_tokens": proj.quota_tokens,
            "quota_cost_usd": proj.quota_cost_usd,
        },
        "usage_percentage": {
            "tokens": (
                (usage["total_tokens"] / proj.quota_tokens * 100)
                if proj.quota_tokens
                else None
            ),
            "cost": (
                (usage["total_cost_usd"] / proj.quota_cost_usd * 100)
                if proj.quota_cost_usd
                else None
            ),
        },
    }


@router.post("/projects/{project_id}/archive", response_model=models.Project)
def archive_project(project_id: int, orch: Orchestrator = Depends(get_orchestrator)):
    proj = orch.archive_project(project_id, archived=True)
    if proj is None:
        raise HTTPException(404, f"project {project_id} not found")
    return proj


@router.post("/projects/{project_id}/unarchive", response_model=models.Project)
def unarchive_project(project_id: int, orch: Orchestrator = Depends(get_orchestrator)):
    proj = orch.archive_project(project_id, archived=False)
    if proj is None:
        raise HTTPException(404, f"project {project_id} not found")
    return proj


@router.delete("/projects/{project_id}", response_model=models.Project)
def delete_project(project_id: int, orch: Orchestrator = Depends(get_orchestrator)):
    proj = orch.delete_project(project_id)
    if proj is None:
        raise HTTPException(404, f"project {project_id} not found")
    return proj


# ---------- tasks ----------
@router.post("/projects/{project_id}/tasks", response_model=models.Task, status_code=201)
def create_task(
    project_id: int, body: TaskCreate, orch: Orchestrator = Depends(get_orchestrator)
):
    if orch.get_project(project_id) is None:
        raise HTTPException(404, f"project {project_id} not found")
    # "auto" is resolved to a real agent inside create_task (smart routing), so
    # it's valid here even though it isn't a registered adapter.
    if body.agent != "auto" and body.agent not in orch.adapters:
        raise HTTPException(400, f"unknown agent {body.agent!r}")
    # Validate source_task_id if provided
    if body.source_task_id is not None:
        source_task = orch.get_task(body.source_task_id)
        if source_task is None:
            raise HTTPException(400, f"source task {body.source_task_id} not found")
        # Source task must belong to the same project
        if source_task.project_id != project_id:
            raise HTTPException(400, "source task must belong to the same project")
    return orch.create_task(
        project_id, body.title, body.description, body.agent,
        source_task_id=body.source_task_id,
    )


@router.post("/tasks/{task_id}/attachments", response_model=list[AttachmentOut])
async def upload_task_attachments(
    task_id: int,
    files: list[UploadFile] = File(...),
    orch: Orchestrator = Depends(get_orchestrator),
):
    task = orch.get_task(task_id)
    if task is None:
        raise HTTPException(404, f"task {task_id} not found")
    if task.status not in {
        models.TaskStatus.draft.value,
        models.TaskStatus.queued.value,
        models.TaskStatus.failed.value,
    }:
        raise HTTPException(409, "attachments can only be added before a run")
    project = orch.get_project(task.project_id)
    if project is None:
        raise HTTPException(404, f"project {task.project_id} not found")

    root = Path(project.path).resolve()
    dest_dir = root / ".conductor" / "attachments" / f"task-{task_id}"
    dest_dir.mkdir(parents=True, exist_ok=True)

    saved: list[AttachmentOut] = []
    for upload in files:
        filename = _safe_attachment_name(upload.filename or "attachment")
        dest = _unique_path(dest_dir, filename)
        size = 0
        try:
            with dest.open("wb") as fh:
                while chunk := await upload.read(1024 * 1024):
                    size += len(chunk)
                    if size > _MAX_ATTACHMENT_BYTES:
                        raise HTTPException(
                            413,
                            f"{upload.filename or filename} exceeds 25 MB",
                        )
                    fh.write(chunk)
        except HTTPException:
            dest.unlink(missing_ok=True)
            raise
        finally:
            await upload.close()
        saved.append(AttachmentOut(
            filename=dest.name,
            path=str(dest.resolve()),
            content_type=upload.content_type or "application/octet-stream",
            size=size,
        ))
    return saved


@router.get("/projects/{project_id}/tasks", response_model=list[models.Task])
def list_tasks(project_id: int, orch: Orchestrator = Depends(get_orchestrator)):
    if orch.get_project(project_id) is None:
        raise HTTPException(404, f"project {project_id} not found")
    return orch.list_tasks(project_id)


@router.get("/tasks/{task_id}", response_model=models.Task)
def get_task(task_id: int, orch: Orchestrator = Depends(get_orchestrator)):
    task = orch.get_task(task_id)
    if task is None:
        raise HTTPException(404, f"task {task_id} not found")
    return task


class RunTaskRequest(BaseModel):
    priority: str = QueuePriority.normal.value
    enqueue_if_busy: bool = True  # If True, queue when concurrency limit reached


@router.post("/tasks/{task_id}/run", response_model=models.Task, status_code=202)
async def run_task(
    task_id: int,
    body: Optional[RunTaskRequest] = None,
    orch: Orchestrator = Depends(get_orchestrator),
):
    """Kick off the agent in the background and return immediately (202).

    The task transitions to ``running`` at once; subscribe to
    ``GET /tasks/{id}/stream`` for live events, or poll ``GET /tasks/{id}``.

    If concurrency limit is reached and enqueue_if_busy is True (default),
    the task will be queued instead (status becomes ``queued``).
    """
    task = orch.get_task(task_id)
    if task is None:
        raise HTTPException(404, f"task {task_id} not found")
    if task.assigned_agent not in orch.adapters:
        raise HTTPException(400, f"unknown agent {task.assigned_agent!r}")

    priority = body.priority if body else QueuePriority.normal.value
    enqueue_if_busy = body.enqueue_if_busy if body else True

    try:
        return orch.start_run(task_id, priority=priority, enqueue_if_busy=enqueue_if_busy)
    except AlreadyRunning:
        raise HTTPException(409, f"task {task_id} is already running")
    except TaskAlreadyQueued:
        raise HTTPException(409, f"task {task_id} is already in queue")
    except NotRunnable:
        raise HTTPException(
            409, f"task {task_id} is a planned container; run its subtasks instead"
        )
    except QuotaExceeded as e:
        raise HTTPException(429, str(e))
    except QueueFull as e:
        raise HTTPException(429, f"Queue full: {e}")
    except ConcurrencyLimitReached as e:
        raise HTTPException(429, str(e))


@router.post("/tasks/{task_id}/plan", response_model=list[models.Task], status_code=201)
async def plan_task(task_id: int, orch: Orchestrator = Depends(get_orchestrator)):
    """Decompose a task into routed draft subtasks (read-only planning).

    Blocks until the planner finishes (no streaming): returns the created child
    tasks. The parent flips to ``planned`` and becomes a container.
    """
    if orch.get_task(task_id) is None:
        raise HTTPException(404, f"task {task_id} not found")
    try:
        return await orch.plan_task(task_id)
    except AlreadyPlanned:
        raise HTTPException(409, f"task {task_id} is already planned")
    except PlanError as exc:
        raise HTTPException(502, str(exc))


@router.get("/tasks/{task_id}/stream")
async def stream_task(
    task_id: int, request: Request, orch: Orchestrator = Depends(get_orchestrator)
):
    """Server-Sent Events: tail the latest run's events until it finishes.

    Emits one ``event: ev`` frame per normalized AgentEvent (same JSON shape as
    ``GET /runs/{id}/events``) and a terminal ``event: done`` carrying the final
    task status. A late subscriber gets the full history (events are replayed by
    ``seq``), so connecting after a run started — or reconnecting — loses nothing.
    """
    if orch.get_task(task_id) is None:
        raise HTTPException(404, f"task {task_id} not found")

    terminal_task_states = {
        models.TaskStatus.awaiting_approval.value,
        models.TaskStatus.failed.value,
        models.TaskStatus.merged.value,
        models.TaskStatus.rejected.value,
    }

    async def gen():
        last_seq = -1
        last_run_id = None
        while True:
            if await request.is_disconnected():
                return
            runs = orch.list_runs(task_id)
            task = orch.get_task(task_id)
            if runs:
                run = runs[-1]
                if run.id != last_run_id:
                    last_run_id = run.id
                    last_seq = -1
                for ev in orch.list_events(run.id):
                    if ev.seq > last_seq:
                        last_seq = ev.seq
                        yield _sse("ev", jsonable_encoder(ev))
                # Status flips to terminal only after every event is committed,
                # so the drain above already captured the tail of the run.
                if run.status != models.RunStatus.running.value:
                    yield _sse("done", {"status": task.status, "run_status": run.status})
                    return
            elif task is not None and task.status in terminal_task_states:
                # Early failure (e.g. worktree creation) before any run row.
                yield _sse("done", {"status": task.status, "run_status": None})
                return
            await asyncio.sleep(STREAM_POLL_SECONDS)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/tasks/{task_id}/diff")
def get_diff(task_id: int, orch: Orchestrator = Depends(get_orchestrator)):
    if orch.get_task(task_id) is None:
        raise HTTPException(404, f"task {task_id} not found")
    return {"task_id": task_id, "diff": orch.get_diff(task_id)}


def _review_out(r: models.Review) -> dict:
    return {
        "id": r.id,
        "task_id": r.task_id,
        "run_id": r.run_id,
        "agent": r.agent,
        "verdict": r.verdict,
        "summary": r.summary,
        "findings": json.loads(r.findings_json or "[]"),
        "created_at": r.created_at,
    }


@router.post("/tasks/{task_id}/review")
async def review_task(task_id: int, orch: Orchestrator = Depends(get_orchestrator)):
    """Run an advisory AI review over the task's latest captured diff.

    Blocks until the reviewer finishes (no streaming) and returns the verdict.
    The task status is untouched — the human still gates approve/reject.
    """
    if orch.get_task(task_id) is None:
        raise HTTPException(404, f"task {task_id} not found")
    try:
        review = await orch.review_task(task_id)
    except ReviewError as exc:
        raise HTTPException(409, str(exc))
    return _review_out(review)


@router.get("/tasks/{task_id}/review")
def get_review(task_id: int, orch: Orchestrator = Depends(get_orchestrator)):
    if orch.get_task(task_id) is None:
        raise HTTPException(404, f"task {task_id} not found")
    review = orch.get_latest_review(task_id)
    return _review_out(review) if review else None


@router.get("/tasks/{task_id}/reviews")
def list_reviews(task_id: int, orch: Orchestrator = Depends(get_orchestrator)):
    """The full cross-model audit trail for a task (oldest first), each labeled
    with the agent that did the audit."""
    if orch.get_task(task_id) is None:
        raise HTTPException(404, f"task {task_id} not found")
    return [_review_out(r) for r in orch.list_reviews(task_id)]


@router.post("/tasks/{task_id}/revise", response_model=models.Task, status_code=202)
async def revise_task(task_id: int, orch: Orchestrator = Depends(get_orchestrator)):
    """Re-run an at-gate task in its worktree to address its latest review.

    Only valid at ``awaiting_approval`` with a review on file; the review's
    findings are injected as context (no file writes → the diff stays clean).
    Returns 202 and flips to ``running`` — subscribe to the same SSE stream as
    ``/run``.
    """
    task = orch.get_task(task_id)
    if task is None:
        raise HTTPException(404, f"task {task_id} not found")
    if task.assigned_agent not in orch.adapters:
        raise HTTPException(400, f"unknown agent {task.assigned_agent!r}")
    try:
        return orch.start_revise(task_id)
    except AlreadyRunning:
        raise HTTPException(409, f"task {task_id} is already running")
    except QuotaExceeded as exc:
        raise HTTPException(429, str(exc))
    except ReviseError as exc:
        raise HTTPException(409, str(exc))


@router.post("/tasks/{task_id}/approve")
def approve_task(task_id: int, orch: Orchestrator = Depends(get_orchestrator)):
    task = orch.get_task(task_id)
    if task is None:
        raise HTTPException(404, f"task {task_id} not found")
    # Idempotent: if already merged, return success without re-merging
    if task.status == "merged":
        return {
            "ok": True,
            "merged_sha": None,
            "conflict": False,
            "conflicted_files": [],
            "verify_failed": False,
            "verify_output": "",
            "dirty": False,
            "dirty_files": [],
            "push_ok": True,
            "push_output": "",
            "task": task,
        }
    res = orch.approve_task(task_id)
    return {
        "ok": res.ok,
        "merged_sha": res.merged_sha,
        "conflict": res.conflict,
        "conflicted_files": res.conflicted_files,
        "verify_failed": res.verify_failed,
        "verify_output": res.verify_output,
        "dirty": res.dirty,
        "dirty_files": res.dirty_files,
        "push_ok": res.push_ok,
        "push_output": res.push_output,
        "task": orch.get_task(task_id),
    }


@router.post("/tasks/{task_id}/reject", response_model=models.Task)
def reject_task(task_id: int, orch: Orchestrator = Depends(get_orchestrator)):
    if orch.get_task(task_id) is None:
        raise HTTPException(404, f"task {task_id} not found")
    return orch.reject_task(task_id)


@router.post("/tasks/{task_id}/stop", response_model=models.Task)
async def stop_task(task_id: int, orch: Orchestrator = Depends(get_orchestrator)):
    """Stop a running task.

    Cancels the in-flight asyncio task and marks the task as failed.
    """
    task = orch.get_task(task_id)
    if task is None:
        raise HTTPException(404, f"task {task_id} not found")
    if not orch.is_running(task_id):
        raise HTTPException(409, f"task {task_id} is not running")
    try:
        return orch.stop_task(task_id)
    except Exception as e:
        raise HTTPException(500, str(e))


# ---------- runs / events (inspection; SSE streaming comes later) ----------
@router.get("/tasks/{task_id}/runs", response_model=list[models.Run])
def list_runs(task_id: int, orch: Orchestrator = Depends(get_orchestrator)):
    if orch.get_task(task_id) is None:
        raise HTTPException(404, f"task {task_id} not found")
    return orch.list_runs(task_id)


@router.get("/runs/{run_id}/events", response_model=list[models.Event])
def list_events(run_id: int, orch: Orchestrator = Depends(get_orchestrator)):
    return orch.list_events(run_id)


# ---------- next-steps suggestions ----------
class NextStepsChoice(BaseModel):
    suggested_task_ids: list[int]
    selected_task_ids: list[int]
    action: str  # 'selected' | 'skipped' | 'created_new'


@router.get("/tasks/{task_id}/next-steps")
def get_next_steps(task_id: int, orch: Orchestrator = Depends(get_orchestrator)):
    """Get suggested next tasks to work on after completing a task."""
    if orch.get_task(task_id) is None:
        raise HTTPException(404, f"task {task_id} not found")
    return orch.get_next_steps(task_id)


@router.post("/tasks/{task_id}/next-steps", response_model=models.TaskSuggestion)
def save_next_steps_choice(
    task_id: int, body: NextStepsChoice, orch: Orchestrator = Depends(get_orchestrator)
):
    """Save user's choice for next steps."""
    if orch.get_task(task_id) is None:
        raise HTTPException(404, f"task {task_id} not found")
    return orch.save_next_steps_choice(
        task_id, body.suggested_task_ids, body.selected_task_ids, body.action
    )


# ---------- Smart Decomposition and Parallel Execution Endpoints ----------


@router.post("/tasks/{task_id}/smart-plan")
async def smart_plan_task(task_id: int, orch: Orchestrator = Depends(get_orchestrator)):
    """Enhanced planning with intelligent model assignment and dependency analysis.

    This is the FIRST PHASE of two-phase task processing:
    - Analyzes problem domain and complexity
    - Assigns optimal AI model to each subtask
    - Determines dependency relationships
    - Returns subtasks ready for parallel execution
    """
    try:
        children = await orch.smart_plan_task(task_id)
        return {
            "parent_task_id": task_id,
            "subtasks": [jsonable_encoder(c) for c in children],
            "count": len(children),
        }
    except AlreadyPlanned:
        raise HTTPException(409, f"task {task_id} already has subtasks")
    except PlanError as e:
        raise HTTPException(500, str(e))
    except ValueError as e:
        raise HTTPException(404, str(e))


class ParallelRunRequest(BaseModel):
    """Request body for parallel subtask execution."""
    batch_indices: Optional[list[int]] = None


@router.post("/tasks/{task_id}/run-parallel")
async def run_subtasks_parallel(
    task_id: int,
    body: ParallelRunRequest = ParallelRunRequest(),
    orch: Orchestrator = Depends(get_orchestrator),
):
    """Execute subtasks in parallel batches.

    This is the SECOND PHASE of two-phase task processing:
    - Runs subtasks concurrently where possible
    - Respects dependency ordering
    - Detects conflicts between subtasks
    - Returns execution summary

    Args:
        task_id: Parent task ID
        batch_indices: Optional list of subtask indices to run (None = all)
    """
    try:
        result = await orch.run_subtasks_parallel(task_id, body.batch_indices)
        return result
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.get("/tasks/{task_id}/merge-strategy")
def get_merge_strategy(task_id: int, orch: Orchestrator = Depends(get_orchestrator)):
    """Analyze subtask results and suggest optimal merge strategy.

    Returns:
        {
            "strategy": "auto" | "sequential" | "manual",
            "order": [subtask_ids] if sequential,
            "conflicts": list of conflict descriptions,
            "recommendation": human-readable explanation
        }
    """
    try:
        return orch.get_merge_strategy(task_id)
    except ValueError as e:
        raise HTTPException(404, str(e))


# ---------- queue management ----------

@router.get("/projects/{project_id}/queue")
def get_queue_status(project_id: int, orch: Orchestrator = Depends(get_orchestrator)):
    """Get queue and concurrency status for a project.

    Returns:
        {
            "project_id": int,
            "queued": int,          # Tasks waiting in queue
            "running": int,         # Tasks currently running
            "max_concurrent": int,  # Concurrency limit
            "max_queued": int,      # Queue capacity
            "priority_mode": str,   # "fifo" or "priority"
            "can_start_more": bool, # Whether more tasks can start
            "queue_full": bool,     # Whether queue is at capacity
        }
    """
    return orch.get_queue_status(project_id)


@router.get("/projects/{project_id}/queue/tasks")
def list_queued_tasks(project_id: int, orch: Orchestrator = Depends(get_orchestrator)):
    """List all tasks waiting in queue for a project."""
    return orch.list_queued_tasks(project_id)


class EnqueueRequest(BaseModel):
    priority: str = QueuePriority.normal.value


@router.post("/tasks/{task_id}/enqueue", response_model=models.Task)
def enqueue_task(
    task_id: int,
    body: EnqueueRequest = EnqueueRequest(),
    orch: Orchestrator = Depends(get_orchestrator),
):
    """Add a task to the queue without starting immediately.

    Useful for batch scheduling or when you want explicit queue control.
    """
    try:
        return orch.enqueue_task(task_id, body.priority)
    except TaskAlreadyQueued:
        raise HTTPException(409, f"task {task_id} is already in queue")
    except QueueFull as e:
        raise HTTPException(429, str(e))
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.post("/tasks/{task_id}/dequeue", response_model=models.Task)
def dequeue_task(task_id: int, orch: Orchestrator = Depends(get_orchestrator)):
    """Remove a task from queue without running it.

    The task returns to draft status.
    """
    return orch.cancel_queued_task(task_id)


class UpdatePriorityRequest(BaseModel):
    priority: str


@router.patch("/tasks/{task_id}/priority")
def update_task_priority(
    task_id: int,
    body: UpdatePriorityRequest,
    orch: Orchestrator = Depends(get_orchestrator),
):
    """Update the priority of a queued task.

    Valid priorities: low, normal, high, urgent
    """
    if body.priority not in [p.value for p in QueuePriority]:
        raise HTTPException(400, f"Invalid priority: {body.priority}")
    entry = orch.update_queue_priority(task_id, body.priority)
    if entry is None:
        raise HTTPException(404, f"task {task_id} is not in queue")
    return entry


@router.get("/projects/{project_id}/concurrency")
def get_concurrency_config(
    project_id: int,
    orch: Orchestrator = Depends(get_orchestrator),
):
    """Get concurrency configuration for a project."""
    return orch.get_concurrency_config(project_id)


class ConcurrencyConfigUpdate(BaseModel):
    max_concurrent: Optional[int] = None
    max_queued: Optional[int] = None
    priority_mode: Optional[str] = None


@router.patch("/projects/{project_id}/concurrency")
def update_concurrency_config(
    project_id: int,
    body: ConcurrencyConfigUpdate,
    orch: Orchestrator = Depends(get_orchestrator),
):
    """Update concurrency configuration for a project.

    Args:
        max_concurrent: Maximum tasks running at once (1-10)
        max_queued: Maximum tasks waiting in queue (1-100)
        priority_mode: "fifo" or "priority"
    """
    if body.max_concurrent is not None and not (1 <= body.max_concurrent <= 10):
        raise HTTPException(400, "max_concurrent must be between 1 and 10")
    if body.max_queued is not None and not (1 <= body.max_queued <= 100):
        raise HTTPException(400, "max_queued must be between 1 and 100")
    if body.priority_mode is not None and body.priority_mode not in ("fifo", "priority"):
        raise HTTPException(400, "priority_mode must be 'fifo' or 'priority'")

    return orch.update_concurrency_config(
        project_id,
        body.max_concurrent,
        body.max_queued,
        body.priority_mode,
    )


@router.post("/projects/{project_id}/queue/process")
async def process_queue(
    project_id: int,
    orch: Orchestrator = Depends(get_orchestrator),
):
    """Manually trigger queue processing for a project.

    Starts queued tasks up to the concurrency limit.
    Returns the number of tasks started.
    """
    started = await orch.process_queue(project_id)
    return {"started": started}
