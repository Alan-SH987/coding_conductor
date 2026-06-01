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

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.api.deps import get_orchestrator
from app.orchestrator import (
    AlreadyPlanned,
    AlreadyRunning,
    NotRunnable,
    Orchestrator,
    PlanError,
    ReviewError,
    ReviseError,
)
from app.storage import models

router = APIRouter()

# How often the SSE stream tails the events table for new rows.
STREAM_POLL_SECONDS = 0.4


def _sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


class ProjectCreate(BaseModel):
    name: str
    path: str


class TaskCreate(BaseModel):
    title: str
    description: str = ""
    agent: str = "claude"


# ---------- agents ----------
@router.get("/agents")
def list_agents(orch: Orchestrator = Depends(get_orchestrator)):
    return [
        {"name": name, "capabilities": sorted(adapter.capabilities)}
        for name, adapter in orch.adapters.items()
    ]


# ---------- projects ----------
@router.post("/projects", response_model=models.Project, status_code=201)
def create_project(body: ProjectCreate, orch: Orchestrator = Depends(get_orchestrator)):
    return orch.create_project(body.name, body.path)


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
    if body.agent not in orch.adapters:
        raise HTTPException(400, f"unknown agent {body.agent!r}")
    return orch.create_task(project_id, body.title, body.description, body.agent)


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


@router.post("/tasks/{task_id}/run", response_model=models.Task, status_code=202)
async def run_task(task_id: int, orch: Orchestrator = Depends(get_orchestrator)):
    """Kick off the agent in the background and return immediately (202).

    The task transitions to ``running`` at once; subscribe to
    ``GET /tasks/{id}/stream`` for live events, or poll ``GET /tasks/{id}``.
    """
    task = orch.get_task(task_id)
    if task is None:
        raise HTTPException(404, f"task {task_id} not found")
    if task.assigned_agent not in orch.adapters:
        raise HTTPException(400, f"unknown agent {task.assigned_agent!r}")
    try:
        return orch.start_run(task_id)
    except AlreadyRunning:
        raise HTTPException(409, f"task {task_id} is already running")
    except NotRunnable:
        raise HTTPException(
            409, f"task {task_id} is a planned container; run its subtasks instead"
        )


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
        while True:
            if await request.is_disconnected():
                return
            runs = orch.list_runs(task_id)
            task = orch.get_task(task_id)
            if runs:
                run = runs[-1]
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
    except ReviseError as exc:
        raise HTTPException(409, str(exc))


@router.post("/tasks/{task_id}/approve")
def approve_task(task_id: int, orch: Orchestrator = Depends(get_orchestrator)):
    if orch.get_task(task_id) is None:
        raise HTTPException(404, f"task {task_id} not found")
    res = orch.approve_task(task_id)
    return {
        "ok": res.ok,
        "merged_sha": res.merged_sha,
        "conflict": res.conflict,
        "conflicted_files": res.conflicted_files,
        "task": orch.get_task(task_id),
    }


@router.post("/tasks/{task_id}/reject", response_model=models.Task)
def reject_task(task_id: int, orch: Orchestrator = Depends(get_orchestrator)):
    if orch.get_task(task_id) is None:
        raise HTTPException(404, f"task {task_id} not found")
    return orch.reject_task(task_id)


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
