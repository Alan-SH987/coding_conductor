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
from typing import Optional

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
    QuotaExceeded,
    ReviewError,
    ReviseError,
)
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


class TaskCreate(BaseModel):
    title: str
    description: str = ""
    agent: str = "claude"


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
    except QuotaExceeded as e:
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
        "verify_failed": res.verify_failed,
        "verify_output": res.verify_output,
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
