r"""Single-task orchestration state machine.

Glues GitOps (isolation) + Adapter (execution) + Storage (persistence) +
Memory into one flow:

    create_task -> run_task(worktree -> inject -> drive adapter -> persist
    runs/events -> capture diff) -> awaiting_approval
    -> approve_task (merge + cleanup) | reject_task (discard worktree)

Status flow: draft -> running -> awaiting_approval -> merged | rejected
                                              \-> failed (error/exception)
"""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlmodel import Session, select

from app import memory, skills
from app.adapters.base import AgentAdapter, EventType, RunContext, TaskSpec
from app.gitops import GitOpsEngine, NotAGitRepo
from app.gitops.models import WorktreeHandle
from app.orchestrator.concurrency_limiter import ConcurrencyLimiter, ConcurrencyLimitReached
from app.orchestrator.error_classifier import classify_error
from app.orchestrator.model_router import ModelRouter
from app.orchestrator.queue_manager import TaskQueueManager, QueueFull, TaskAlreadyQueued
from app.orchestrator.retry_policy import RetryPolicy
from app.orchestrator.routing import select_agent
from app.orchestrator.smart_decomposer import (
    SmartDecomposer,
    SubtaskSpec,
    ResultMerger,
)
from app.storage import models
from app.storage.models import QueuePriority
from app.storage.db import engine as default_engine

logger = logging.getLogger(__name__)

TaskStatus = models.TaskStatus
RunStatus = models.RunStatus


class AlreadyRunning(Exception):
    """Raised when a run is requested for a task that is already in flight."""


class AlreadyPlanned(Exception):
    """Raised when planning is requested for a task that already has subtasks."""


class PlanError(Exception):
    """Raised when the planner could not produce usable subtasks."""


class NotRunnable(Exception):
    """Raised when a run is requested for a planned container task."""


class ReviewError(Exception):
    """Raised when a review cannot be produced (no diff / no review agent)."""


class QuotaExceeded(Exception):
    """Raised when a project has exceeded its usage quota."""


class ReviseError(Exception):
    """Raised when a task cannot be revised (not at the gate / no review)."""


@dataclass
class _DriveResult:
    task: models.Task
    success: bool
    error_text: str = ""
    error_kind: str = ""
    session_id: Optional[str] = None
    exception: Optional[Exception] = None


class Orchestrator:
    def __init__(
        self,
        adapters: dict[str, AgentAdapter],
        engine=None,
        retry_policy: Optional[RetryPolicy] = None,
    ):
        self.adapters = adapters
        self.engine = engine or default_engine
        self.retry_policy = retry_policy or RetryPolicy()
        # task_id -> in-flight asyncio Task. In-memory only; on restart the
        # registry is empty and a stuck "running" task can simply be re-run.
        self._running: dict[int, asyncio.Task] = {}
        # Task queue and concurrency control
        self.queue_manager = TaskQueueManager(self.engine)
        self.concurrency_limiter = ConcurrencyLimiter(self.engine)
        # Wire up the queue to start tasks through this orchestrator
        self.queue_manager.set_start_callback(self._start_from_queue)

    # ---------- projects ----------
    def create_project(self, name: str, path: str, init: bool = True) -> models.Project:
        git = GitOpsEngine(path)
        try:
            info = git.inspect_repo()  # raises NotAGitRepo if invalid
        except (NotAGitRepo, FileNotFoundError):
            # Empty / non-existent / non-git path: auto-create a git repo so the
            # project is usable, instead of rejecting it.
            if not init:
                raise
            info = git.init_repo()
        memory.ensure_conductor(git.repo_path)
        with Session(self.engine) as s:
            proj = models.Project(
                name=name, path=str(git.repo_path), default_branch=info.default_branch
            )
            s.add(proj)
            s.commit()
            s.refresh(proj)
            return proj

    # ---------- tasks ----------
    def create_task(self, project_id: int, title: str, description: str = "",
                    agent: str = "claude",
                    parent_id: Optional[int] = None) -> models.Task:
        # Use intelligent routing if "auto" is specified
        if agent == "auto":
            available_models = list(self.adapters.keys())
            router = ModelRouter(available_models=available_models)
            agent = router.select_model(title, description)
            logger.info(f"Auto-selected model {agent} for task: {title}")

        with Session(self.engine) as s:
            task = models.Task(
                project_id=project_id, title=title, description=description,
                assigned_agent=agent, status=TaskStatus.draft.value,
                parent_id=parent_id,
            )
            s.add(task)
            s.commit()
            s.refresh(task)
            return task

    async def plan_task(self, task_id: int) -> list[models.Task]:
        """Decompose a task into routed draft subtasks (read-only planning).

        A plan-capable agent reads the repo and proposes subtasks; each is
        created in ``draft`` with an agent chosen by the Router. The parent
        becomes a non-runnable container (status ``planned``). Nothing executes
        here — the human still gates each subtask's run.
        """
        with Session(self.engine) as s:
            task = s.get(models.Task, task_id)
            if task is None:
                raise ValueError(f"task {task_id} not found")
            project = s.get(models.Project, task.project_id)
            project_id = task.project_id
            project_path = project.path
            prior_status = task.status
            prefer_agent = task.assigned_agent
            goal = "\n".join(p for p in (task.title, task.description) if p)
            has_children = s.exec(
                select(models.Task.id).where(models.Task.parent_id == task_id)
            ).first() is not None
        if has_children:
            raise AlreadyPlanned(task_id)

        planner_name = select_agent("plan", self.adapters)
        planner = self.adapters.get(planner_name) if planner_name else None
        if planner is None or "plan" not in planner.capabilities:
            raise PlanError("no planning-capable agent is available")

        capabilities = sorted({c for a in self.adapters.values() for c in a.capabilities})

        self._set_status(task_id, TaskStatus.planning)
        try:
            specs = await planner.plan(goal, project_path, capabilities)
        except Exception:
            self._update_task(task_id, status=prior_status)
            raise
        if not specs:
            self._update_task(task_id, status=prior_status)
            raise PlanError("planner returned no subtasks")

        children: list[models.Task] = []
        for spec in specs:
            agent = select_agent(spec.capability, self.adapters, prefer=prefer_agent)
            children.append(self.create_task(
                project_id, spec.title, spec.description, agent, parent_id=task_id
            ))

        self._set_status(task_id, TaskStatus.planned)
        return children

    def is_running(self, task_id: int) -> bool:
        t = self._running.get(task_id)
        return t is not None and not t.done()

    def stop_task(self, task_id: int) -> models.Task:
        """Cancel a running task.

        Cancels the in-flight asyncio Task and marks the task as failed with a
        clear message. The worktree is left intact so a retry can resume.
        """
        loop_task = self._running.get(task_id)
        if loop_task is None or loop_task.done():
            raise ValueError(f"task {task_id} is not running")
        loop_task.cancel()
        self._running.pop(task_id, None)
        return self._update_task(
            task_id,
            status=TaskStatus.failed.value,
            error="stopped by user — click Retry to run again",
        )

    def _has_children(self, task_id: int) -> bool:
        with Session(self.engine) as s:
            return s.exec(
                select(models.Task.id).where(models.Task.parent_id == task_id)
            ).first() is not None

    def start_run(
        self,
        task_id: int,
        priority: str = QueuePriority.normal.value,
        enqueue_if_busy: bool = True,
    ) -> models.Task:
        """Launch run_task in the background and return immediately.

        The task is flipped to ``running`` synchronously so the caller (and the
        SSE stream) sees the transition right away; the agent then drives in a
        detached asyncio Task. Requires a running event loop.

        If concurrency limit is reached and enqueue_if_busy is True, the task
        is added to the queue instead. Set enqueue_if_busy=False to raise
        ConcurrencyLimitReached instead.
        """
        task = self.get_task(task_id)
        if task is None:
            raise ValueError(f"task {task_id} not found")
        if task.assigned_agent not in self.adapters:
            raise KeyError(task.assigned_agent)
        if self.is_running(task_id):
            raise AlreadyRunning(task_id)
        if self._has_children(task_id):
            raise NotRunnable(task_id)

        self.check_quota(task.project_id)

        # Check concurrency limit
        status = self.concurrency_limiter.get_status(task.project_id)
        if not status["can_start"]:
            if enqueue_if_busy:
                # Add to queue instead of running
                self.queue_manager.enqueue(task_id, task.project_id, priority)
                return self.get_task(task_id)  # status is now "queued"
            else:
                raise ConcurrencyLimitReached(
                    task.project_id, status["running"], status["max_concurrent"]
                )

        return self._start_run_immediate(task_id)

    def _start_run_immediate(self, task_id: int) -> models.Task:
        """Actually start running a task (bypasses queue check)."""
        self._set_status(task_id, TaskStatus.running)
        loop_task = asyncio.create_task(self._run_guarded(task_id))
        self._running[task_id] = loop_task
        loop_task.add_done_callback(lambda _f: self._running.pop(task_id, None))
        return self.get_task(task_id)

    async def _start_from_queue(self, task_id: int) -> models.Task:
        """Callback for queue manager to start a task."""
        task = self.get_task(task_id)
        if task is None:
            raise ValueError(f"task {task_id} not found")
        self.check_quota(task.project_id)
        return self._start_run_immediate(task_id)

    async def _run_guarded(self, task_id: int) -> None:
        # run_task already records failed status on error; just keep the
        # detached task from surfacing an "exception never retrieved" warning.
        project_id = None
        try:
            task = self.get_task(task_id)
            if task:
                project_id = task.project_id
            await self.run_task(task_id)
        except Exception:
            logger.exception("run_task(%s) crashed", task_id)
        finally:
            # Task finished (success or failure), process queue for this project
            if project_id is not None:
                try:
                    await self.queue_manager.on_task_completed(project_id)
                except Exception:
                    logger.exception("queue processing after task %s failed", task_id)

    def start_revise(self, task_id: int) -> models.Task:
        """Re-run an at-gate task in its existing worktree to address review.

        Bundle A (minimal handoff loop): only valid when the task sits at
        ``awaiting_approval`` with a review on file. The agent's prior edits are
        still on disk in the same worktree; the review's findings are injected
        via the system prompt (never written to a file, so the captured diff
        stays clean). A fresh Run is created synchronously so the SSE stream
        tails the revision, then the agent drives in the background.
        """
        task = self.get_task(task_id)
        if task is None:
            raise ValueError(f"task {task_id} not found")
        if task.assigned_agent not in self.adapters:
            raise KeyError(task.assigned_agent)
        if self.is_running(task_id):
            raise AlreadyRunning(task_id)
        if task.status != TaskStatus.awaiting_approval.value:
            raise ReviseError("task is not awaiting approval")
        if not task.worktree_path or not Path(task.worktree_path).exists():
            raise ReviseError("no worktree to revise in")
        if self.get_latest_review(task_id) is None:
            raise ReviseError("no review to revise from")
        self.check_quota(task.project_id)

        run_id = self._create_run(task_id, task.assigned_agent)
        self._set_status(task_id, TaskStatus.running)
        loop_task = asyncio.create_task(self._revise_guarded(task_id, run_id))
        self._running[task_id] = loop_task
        loop_task.add_done_callback(lambda _f: self._running.pop(task_id, None))
        return self.get_task(task_id)

    async def _revise_guarded(self, task_id: int, run_id: int) -> None:
        try:
            await self.revise_task(task_id, run_id)
        except Exception:
            logger.exception("revise_task(%s) crashed", task_id)

    async def run_task(self, task_id: int) -> models.Task:
        with Session(self.engine) as s:
            task = s.get(models.Task, task_id)
            if task is None:
                raise ValueError(f"task {task_id} not found")
            project = s.get(models.Project, task.project_id)
            agent_name = task.assigned_agent
            spec_goal = task.description or task.title
            project_path = project.path
            project_id = task.project_id
            enabled_skills = project.enabled_skills
            task_tags = json.loads(task.tags) if task.tags else None

        # Check quota before starting the run
        try:
            self.check_quota(project_id)
        except QuotaExceeded as exc:
            self._set_failed(task_id, f"quota exceeded: {exc}")
            raise

        if agent_name not in self.adapters:
            self._set_failed(task_id, f"no adapter named {agent_name!r}")
            raise ValueError(f"no adapter named {agent_name!r}")

        adapter = self.adapters[agent_name]
        git = GitOpsEngine(project_path)
        try:
            handle, resume_session_id = self._prepare_run_handle(task, git, adapter)
        except Exception as exc:
            self._set_failed(task_id, f"worktree setup failed: {exc}")
            raise

        spec = TaskSpec(goal=spec_goal)
        bundle_parts = [
            memory.build_context_bundle(project_path, query=spec_goal, query_tags=task_tags),
            skills.build_skills_bundle(skills.parse_enabled(enabled_skills)),
        ]
        return await self._drive_run_with_retries(
            task_id=task_id,
            agent_name=agent_name,
            project_path=project_path,
            git=git,
            handle=handle,
            spec=spec,
            system_prompt="\n\n".join(p for p in bundle_parts if p),
            resume_session_id=resume_session_id,
        )

    def _prepare_run_handle(
        self,
        task: models.Task,
        git: GitOpsEngine,
        adapter: AgentAdapter,
    ) -> tuple[WorktreeHandle, Optional[str]]:
        """Create or recover the task worktree for a new run.

        A failed task may still have a valid worktree and resumable agent
        session. Reuse both so manual retries and automatic retries continue
        from the failed attempt instead of discarding partial progress.
        """
        latest = self._latest_run(task.id)
        can_resume = (
            task.status == TaskStatus.failed.value
            and bool(task.worktree_path)
            and bool(task.branch)
            and Path(task.worktree_path or "").exists()
            and adapter.supports_resume()
            and latest is not None
            and bool(latest.session_id)
        )
        if can_resume:
            handle = WorktreeHandle(
                task_id=str(task.id),
                path=task.worktree_path or "",
                branch=task.branch or "",
                base_sha=git.merge_base(task.branch or ""),
                created_at="",
            )
            self._update_task(task.id, status=TaskStatus.running.value, error=None)
            return handle, latest.session_id

        # If there is no resumable state, clear any stale attempt before
        # creating a fresh branch/worktree off the default branch.
        git.reset_worktree(task.id)
        handle = git.create_worktree(task.id)
        self._update_task(
            task.id,
            branch=handle.branch,
            worktree_path=handle.path,
            status=TaskStatus.running.value,
            error=None,
        )
        return handle, None

    async def revise_task(self, task_id: int, run_id: int) -> models.Task:
        """Re-run the agent in the task's existing worktree to address review.

        Reuses the worktree/branch (its prior edits are still on disk, so the
        agent has natural context — no session resume) and restores the diff base
        via ``git merge-base`` so the re-snapshot accumulates onto the same
        branch. The latest review's findings are injected through the system
        prompt, never written to a file, so the captured diff stays clean.
        """
        with Session(self.engine) as s:
            task = s.get(models.Task, task_id)
            if task is None:
                raise ValueError(f"task {task_id} not found")
            project = s.get(models.Project, task.project_id)
            agent_name = task.assigned_agent
            spec_goal = task.description or task.title
            project_path = project.path
            worktree_path = task.worktree_path
            branch = task.branch
            enabled_skills = project.enabled_skills
            task_tags = json.loads(task.tags) if task.tags else None
            review = s.exec(
                select(models.Review)
                .where(models.Review.task_id == task_id)
                .order_by(models.Review.id.desc())
            ).first()

        summary = review.summary if review else ""
        findings = json.loads(review.findings_json) if review else []
        revision = self._compose_revision_prompt(summary, findings)
        bundle_parts = [
            memory.build_context_bundle(project_path, query=spec_goal, query_tags=task_tags),
            skills.build_skills_bundle(skills.parse_enabled(enabled_skills)),
            revision,
        ]
        system_prompt = "\n\n".join(p for p in bundle_parts if p)

        git = GitOpsEngine(project_path)
        handle = WorktreeHandle(
            task_id=str(task_id), path=worktree_path or "", branch=branch or "",
            base_sha=git.merge_base(branch), created_at="",
        )
        spec = TaskSpec(goal=spec_goal)
        ctx = RunContext(worktree_path=worktree_path, system_prompt=system_prompt)
        result = await self._drive_run_once(
            task_id, run_id, agent_name, project_path, git, handle, spec, ctx
        )
        if result.exception:
            raise result.exception
        return result.task

    async def _drive_run_with_retries(
        self,
        task_id: int,
        agent_name: str,
        project_path: str,
        git: GitOpsEngine,
        handle: WorktreeHandle,
        spec: TaskSpec,
        system_prompt: str,
        resume_session_id: Optional[str] = None,
    ) -> models.Task:
        attempt = 1
        next_resume_session_id = resume_session_id

        while True:
            run_id = self._create_run(task_id, agent_name)
            ctx = RunContext(
                worktree_path=handle.path,
                system_prompt=system_prompt,
                resume_session_id=next_resume_session_id,
            )
            result = await self._drive_run_once(
                task_id, run_id, agent_name, project_path, git, handle, spec, ctx
            )
            if result.success:
                return result.task

            classified = classify_error(result.error_text, result.error_kind)
            if not self.retry_policy.should_retry(attempt, classified):
                if result.exception:
                    raise result.exception
                return result.task

            next_resume_session_id = result.session_id or next_resume_session_id
            self._update_task(
                task_id,
                status=TaskStatus.running.value,
                error=(
                    f"retrying after {classified.reason} "
                    f"(attempt {attempt + 1}/{self.retry_policy.max_attempts})"
                ),
            )
            await self.retry_policy.sleep_before_retry(attempt)
            attempt += 1

    async def _drive_run_once(self, task_id, run_id, agent_name, project_path,
                              git, handle, spec, ctx) -> _DriveResult:
        """Stream an adapter run into storage, capture the diff, settle status.

        Shared tail of run_task and revise_task: only the worktree setup (fresh
        vs reused) and the injected context differ. From here both persist
        events/cost, snapshot the worktree, and land on awaiting_approval (or
        failed on error/exception).
        """
        adapter = self.adapters[agent_name]
        seq = 0
        session_id: Optional[str] = None
        had_error = False
        error_text = ""
        error_kind = ""
        agg = {"cost": 0.0, "in": 0, "out": 0, "dur": 0}
        try:
            async for ev in adapter.run(spec, ctx):
                self._save_event(run_id, seq, ev)
                seq += 1
                if ev.type == EventType.meta and ev.data.get("session_id"):
                    session_id = ev.data["session_id"]
                elif ev.type == EventType.final:
                    session_id = ev.data.get("session_id") or session_id
                elif ev.type == EventType.cost:
                    agg["cost"] += ev.data.get("cost_usd") or 0
                    agg["in"] += ev.data.get("input_tokens") or 0
                    agg["out"] += ev.data.get("output_tokens") or 0
                    agg["dur"] += ev.data.get("duration_ms") or 0
                elif ev.type == EventType.error:
                    had_error = True
                    error_text = ev.text or error_text
                    error_kind = ev.data.get("kind") or error_kind
        except Exception as exc:
            self._finish_run(run_id, RunStatus.failed, session_id, agg, diff_ref=None)
            self._set_failed(task_id, f"run error: {exc}")
            return _DriveResult(
                task=self.get_task(task_id),
                success=False,
                error_text=str(exc),
                error_kind="runtime",
                session_id=session_id,
                exception=exc,
            )

        diff = git.snapshot_and_diff(handle)
        diff_ref = memory.save_diff(project_path, task_id, diff.unified_diff)

        self._finish_run(
            run_id, RunStatus.failed if had_error else RunStatus.succeeded,
            session_id, agg, diff_ref,
        )
        memory.record_run(project_path, {
            "task_id": task_id, "agent": agent_name,
            "status": "failed" if had_error else "awaiting_approval",
            "cost_usd": agg["cost"], "files": [f.path for f in diff.files],
        })

        final_status = TaskStatus.failed if had_error else TaskStatus.awaiting_approval
        updated = self._update_task(
            task_id,
            status=final_status.value,
            error=(error_text.strip()[:2000] or "the agent reported an error") if had_error else None,
        )
        return _DriveResult(
            task=updated,
            success=not had_error,
            error_text=error_text,
            error_kind=error_kind,
            session_id=session_id,
        )

    @staticmethod
    def _compose_revision_prompt(summary: str, findings: list[dict]) -> str:
        lines = [
            "You previously attempted this task in THIS worktree; your earlier "
            "changes are already on disk. A code review requested changes. Revise "
            "your work to address every point below, then make sure the result is "
            "correct and complete. Do not revert unrelated prior work.",
        ]
        if summary:
            lines.append(f"\nReview summary:\n{summary}")
        bullets: list[str] = []
        for f in findings or []:
            if not isinstance(f, dict):
                continue
            comment = str(f.get("comment", "")).strip()
            if not comment:
                continue
            sev = str(f.get("severity", "")).strip() or "warning"
            loc = str(f.get("file", "")).strip()
            prefix = f"[{sev}] {loc}: " if loc else f"[{sev}] "
            bullets.append(f"- {prefix}{comment}")
        if bullets:
            lines.append("\nRequested changes:\n" + "\n".join(bullets))
        return "\n".join(lines)

    def get_diff(self, task_id: int) -> str:
        with Session(self.engine) as s:
            run = s.exec(
                select(models.Run)
                .where(models.Run.task_id == task_id)
                .order_by(models.Run.id.desc())
            ).first()
        if run and run.diff_ref:
            return memory.read_diff(run.diff_ref)
        return ""

    def _changed_files(self, task_id: int) -> list[str]:
        """File paths from a task's captured diff ('diff --git a/x b/x' -> x)."""
        files: list[str] = []
        for line in self.get_diff(task_id).splitlines():
            if line.startswith("diff --git "):
                head, sep, tail = line.partition(" b/")
                if sep:
                    files.append(tail.strip())
        return files

    def _last_agent_message(self, task_id: int) -> str:
        """The agent's own 'what I did' from the latest run (its final message,
        else the last message). Reused as the handoff's execution summary — no
        extra LLM call."""
        runs = self.list_runs(task_id)
        if not runs:
            return ""
        finals: list[str] = []
        messages: list[str] = []
        for e in self.list_events(runs[-1].id):
            if e.type not in ("final", "message"):
                continue
            try:
                text = (json.loads(e.payload_json).get("text") or "").strip()
            except (ValueError, TypeError):
                text = ""
            if text:
                (finals if e.type == "final" else messages).append(text)
        return finals[-1] if finals else (messages[-1] if messages else "")

    def _record_handoff(self, task, project, outcome: str = "merged") -> None:
        """Distill a finished task into a shared-memory handoff entry (best-effort).

        Write side of the memory loop: cheap/deterministic (no extra LLM call) —
        title + changed files + the existing AI review + auto-extracted tags.
        ``outcome`` is "merged" or "rejected"; rejected entries also list the
        review findings so future runs learn what to avoid.
        """
        try:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            lines = [f"### task {task.id}: {task.title} ({outcome}, {today})"]
            files = self._changed_files(task.id)
            if files:
                shown = ", ".join(files[:10]) + (" …" if len(files) > 10 else "")
                lines.append(f"- files: {shown}")

            # Extract tags from title, description, and changed files
            tags = memory.extract_tags(task.title, task.description or "", files)
            if tags:
                lines.append(f"- tags: {', '.join(tags)}")
                # Update task.tags in database if not already set
                if not task.tags:
                    self._update_task(task.id, tags=json.dumps(tags))

            summary = self._last_agent_message(task.id)
            if summary:
                lines.append(f"- summary: {' '.join(summary.split())[:300]}")
            review = self.get_latest_review(task.id)
            if review:
                summary = " ".join((review.summary or "").split())[:200]
                lines.append(f"- review: {review.verdict}" + (f" — {summary}" if summary else ""))
                if outcome == "rejected":
                    try:
                        findings = json.loads(review.findings_json or "[]")
                    except (ValueError, TypeError):
                        findings = []
                    for f in findings[:5]:
                        if isinstance(f, dict) and f.get("comment"):
                            sev = f.get("severity") or "note"
                            loc = (f.get("file") or "").strip()
                            where = f"{loc}: " if loc else ""
                            lines.append(f"  - [{sev}] {where}{f['comment']}")
            memory.record_handoff(project.path, "\n".join(lines))
        except Exception:  # noqa: BLE001 - memory is best-effort, never block
            logger.exception("record_handoff(%s) failed", task.id)

    # ---------- distillation (manual, LLM-backed) ----------
    async def distill_insights(self, project_id: int) -> str:
        """Summarize accumulated handoffs into a few durable, high-level insights
        and write them to insights.md (separate from the human-curated global.md).

        The one memory step that needs an LLM, so it's manually triggered — never
        on the hot path of a task run. Returns the distilled text ('' if no
        handoffs yet).
        """
        project = self.get_project(project_id)
        if project is None:
            raise ValueError(f"project {project_id} not found")
        handoffs = memory.read_handoffs(project.path)
        if not handoffs:
            return ""
        adapter = self.adapters.get("claude") or next(iter(self.adapters.values()), None)
        if adapter is None:
            raise RuntimeError("no agent available to distill insights")
        prompt = (
            "You are maintaining a software project's long-term memory. Below are "
            "accumulated per-task handoff notes. Distill them into a SHORT list of "
            "durable, high-level insights about this codebase — recurring patterns, "
            "gotchas, conventions, and decisions that will help future tasks. Write "
            "3-8 concise bullets. Do NOT restate individual tasks or file lists. "
            "Output only the bullets.\n\n=== HANDOFFS ===\n" + handoffs[-6000:]
        )
        text = (await self._summarize(adapter, prompt)).strip()
        if text:
            memory.write_insights(project.path, text)
        return text

    async def _summarize(self, adapter, prompt: str, timeout: int = 180) -> str:
        """Run an adapter once in a throwaway dir and return its final/last message.

        Used for one-off LLM calls (e.g. distillation) that aren't tied to a task
        run, so nothing is persisted and the worktree is never touched.
        """
        with tempfile.TemporaryDirectory() as tmp:
            spec = TaskSpec(goal=prompt)
            ctx = RunContext(worktree_path=tmp, timeout=timeout)
            finals: list[str] = []
            messages: list[str] = []
            async for ev in adapter.run(spec, ctx):
                if ev.type == EventType.final and ev.text:
                    finals.append(ev.text.strip())
                elif ev.type == EventType.message and ev.text:
                    messages.append(ev.text.strip())
        return finals[-1] if finals else (messages[-1] if messages else "")

    # ---------- review (advisory) ----------
    async def review_task(self, task_id: int) -> models.Review:
        """Run an advisory AI review over the task's latest captured diff.

        Read-only: a review-capable agent critiques the diff (reading the repo
        for context in plan mode, no edits) and returns a verdict. The task
        status is untouched — the human still gates approve/reject.
        """
        with Session(self.engine) as s:
            task = s.get(models.Task, task_id)
            if task is None:
                raise ValueError(f"task {task_id} not found")
            project = s.get(models.Project, task.project_id)
            project_path = project.path
            goal = "\n".join(p for p in (task.title, task.description) if p)
            run = s.exec(
                select(models.Run)
                .where(models.Run.task_id == task_id)
                .order_by(models.Run.id.desc())
            ).first()
            run_id = run.id if run else None
            diff_ref = run.diff_ref if run else None

        diff = memory.read_diff(diff_ref) if diff_ref else ""
        if not diff.strip():
            raise ReviewError("no captured diff to review")

        reviewer_name = select_agent("review", self.adapters)
        reviewer = self.adapters.get(reviewer_name) if reviewer_name else None
        if reviewer is None or "review" not in reviewer.capabilities:
            raise ReviewError("no review-capable agent is available")

        result = await reviewer.review(goal, diff, project_path)
        return self._create_review(task_id, run_id, reviewer_name, result)

    def get_latest_review(self, task_id: int) -> Optional[models.Review]:
        """Latest review — but only if it still matches the task's latest run.

        After a revise creates a new run, the prior review describes a now-stale
        diff, so it is hidden until a fresh review runs against the new run. A
        legacy review with no run linkage is always shown (staleness unknown).
        """
        with Session(self.engine) as s:
            review = s.exec(
                select(models.Review)
                .where(models.Review.task_id == task_id)
                .order_by(models.Review.id.desc())
            ).first()
            if review is None:
                return None
            latest_run_id = s.exec(
                select(models.Run.id)
                .where(models.Run.task_id == task_id)
                .order_by(models.Run.id.desc())
            ).first()
            if (review.run_id is not None and latest_run_id is not None
                    and review.run_id != latest_run_id):
                return None
            return review

    def _create_review(self, task_id, run_id, agent, result) -> models.Review:
        findings = [asdict(f) for f in result.findings]
        with Session(self.engine) as s:
            review = models.Review(
                task_id=task_id, run_id=run_id, agent=agent,
                verdict=result.verdict, summary=result.summary,
                findings_json=json.dumps(findings, ensure_ascii=False),
            )
            s.add(review)
            s.commit()
            s.refresh(review)
            return review

    # ---------- project management ----------
    def pin_project(self, project_id: int, pinned: bool = True) -> Optional[models.Project]:
        with Session(self.engine) as s:
            proj = s.get(models.Project, project_id)
            if proj is None or proj.deleted_at is not None:
                return None
            proj.is_pinned = pinned
            s.add(proj)
            s.commit()
            s.refresh(proj)
            return proj

    def archive_project(self, project_id: int, archived: bool = True) -> Optional[models.Project]:
        with Session(self.engine) as s:
            proj = s.get(models.Project, project_id)
            if proj is None or proj.deleted_at is not None:
                return None
            proj.is_archived = archived
            s.add(proj)
            s.commit()
            s.refresh(proj)
            return proj

    def delete_project(self, project_id: int) -> Optional[models.Project]:
        with Session(self.engine) as s:
            proj = s.get(models.Project, project_id)
            if proj is None or proj.deleted_at is not None:
                return None
            now = datetime.now(timezone.utc)
            proj.deleted_at = now
            # Cascade soft-delete to all tasks belonging to this project
            tasks = s.exec(
                select(models.Task).where(
                    models.Task.project_id == project_id,
                    models.Task.deleted_at.is_(None),
                )
            ).all()
            for task in tasks:
                task.deleted_at = now
                s.add(task)
            s.add(proj)
            s.commit()
            s.refresh(proj)
            return proj

    # ---------- reads ----------
    def list_projects(self, include_archived: bool = False) -> list[models.Project]:
        with Session(self.engine) as s:
            query = select(models.Project).where(models.Project.deleted_at.is_(None))
            if not include_archived:
                query = query.where(models.Project.is_archived.is_(False))
            query = query.order_by(models.Project.is_pinned.desc(), models.Project.id)
            return list(s.exec(query).all())

    def get_project(self, project_id: int) -> Optional[models.Project]:
        with Session(self.engine) as s:
            proj = s.get(models.Project, project_id)
            if proj is None or proj.deleted_at is not None:
                return None
            return proj

    def update_project_quotas(
        self, project_id: int, quota_tokens: Optional[int], quota_cost_usd: Optional[float]
    ) -> Optional[models.Project]:
        """Update quota settings for a project."""
        with Session(self.engine) as s:
            proj = s.get(models.Project, project_id)
            if proj is None or proj.deleted_at is not None:
                return None
            proj.quota_tokens = quota_tokens
            proj.quota_cost_usd = quota_cost_usd
            s.add(proj)
            s.commit()
            s.refresh(proj)
            return proj

    def update_project_verify(
        self, project_id: int, verify_cmd: Optional[str]
    ) -> Optional[models.Project]:
        """Set (or clear, when falsy) the pre-merge verify command."""
        with Session(self.engine) as s:
            proj = s.get(models.Project, project_id)
            if proj is None or proj.deleted_at is not None:
                return None
            proj.verify_cmd = verify_cmd or None
            s.add(proj)
            s.commit()
            s.refresh(proj)
            return proj

    def update_project_skills(
        self, project_id: int, enabled: list[str]
    ) -> Optional[models.Project]:
        """Set the list of enabled skill names for a project (stored as JSON)."""
        with Session(self.engine) as s:
            proj = s.get(models.Project, project_id)
            if proj is None or proj.deleted_at is not None:
                return None
            proj.enabled_skills = json.dumps([str(x) for x in (enabled or [])])
            s.add(proj)
            s.commit()
            s.refresh(proj)
            return proj

    def get_project_usage(self, project_id: int) -> dict:
        """Calculate total usage (tokens and cost) for a project."""
        with Session(self.engine) as s:
            # Get all runs for tasks in this project
            result = s.exec(
                select(models.Run)
                .join(models.Task)
                .where(models.Task.project_id == project_id)
                .where(models.Run.ended_at.is_not(None))
            ).all()

            total_tokens = sum(run.tokens_in + run.tokens_out for run in result)
            total_cost = sum(run.cost for run in result)

            return {
                "total_tokens": total_tokens,
                "total_cost_usd": total_cost,
                "run_count": len(list(result)),
            }

    def check_quota(self, project_id: int) -> None:
        """Check if project has exceeded quota limits.

        Raises QuotaExceeded if any quota is exceeded.
        """
        with Session(self.engine) as s:
            proj = s.get(models.Project, project_id)
            if proj is None:
                raise ValueError(f"project {project_id} not found")

            # If no quotas are set, allow unlimited usage
            if proj.quota_tokens is None and proj.quota_cost_usd is None:
                return

            usage = self.get_project_usage(project_id)

            # Check token quota
            if proj.quota_tokens is not None and usage["total_tokens"] >= proj.quota_tokens:
                raise QuotaExceeded(
                    f"Token quota exceeded: {usage['total_tokens']}/{proj.quota_tokens} tokens used"
                )

            # Check cost quota
            if proj.quota_cost_usd is not None and usage["total_cost_usd"] >= proj.quota_cost_usd:
                raise QuotaExceeded(
                    f"Cost quota exceeded: ${usage['total_cost_usd']:.2f}/${proj.quota_cost_usd:.2f} used"
                )

    def list_tasks(self, project_id: int) -> list[models.Task]:
        with Session(self.engine) as s:
            return list(
                s.exec(
                    select(models.Task)
                    .where(
                        models.Task.project_id == project_id,
                        models.Task.deleted_at.is_(None),
                    )
                    .order_by(models.Task.id)
                ).all()
            )

    def get_task(self, task_id: int) -> Optional[models.Task]:
        with Session(self.engine) as s:
            task = s.get(models.Task, task_id)
            if task is None or task.deleted_at is not None:
                return None
            return task

    def list_runs(self, task_id: int) -> list[models.Run]:
        with Session(self.engine) as s:
            return list(
                s.exec(
                    select(models.Run)
                    .where(models.Run.task_id == task_id)
                    .order_by(models.Run.id)
                ).all()
            )

    def _latest_run(self, task_id: int) -> Optional[models.Run]:
        with Session(self.engine) as s:
            return s.exec(
                select(models.Run)
                .where(models.Run.task_id == task_id)
                .order_by(models.Run.id.desc())
            ).first()

    def list_events(self, run_id: int) -> list[models.Event]:
        with Session(self.engine) as s:
            return list(
                s.exec(
                    select(models.Event)
                    .where(models.Event.run_id == run_id)
                    .order_by(models.Event.seq)
                ).all()
            )

    # ---------- approval gate ----------
    def approve_task(self, task_id: int):
        task, project = self._task_and_project(task_id)
        git = GitOpsEngine(project.path)
        res = git.merge_to(self._handle_from_task(task), verify_cmd=project.verify_cmd)
        if res.ok:
            git.remove_worktree(self._handle_from_task(task))
            self._update_task(task_id, status=TaskStatus.merged.value, error=None)
            self._record_handoff(task, project)
        elif res.dirty or res.verify_failed:
            # Blocked, not failed: main has uncommitted changes, or the verify
            # gate rejected the build. Nothing landed on main; leave the task at
            # awaiting_approval (worktree intact) so it can be retried after the
            # human cleans up / fixes, rather than losing the gate state.
            pass
        else:
            # Merge conflict (or other merge failure) — record WHY so the failed
            # task isn't a black box.
            reason = (
                "merge conflict in: " + ", ".join(res.conflicted_files)
                if res.conflict
                else (res.verify_output or "merge failed")
            )
            self._set_failed(task_id, reason)
        return res

    def reject_task(self, task_id: int) -> models.Task:
        task, project = self._task_and_project(task_id)
        git = GitOpsEngine(project.path)
        git.remove_worktree(self._handle_from_task(task))
        result = self._update_task(task_id, status=TaskStatus.rejected.value)
        self._record_handoff(task, project, "rejected")
        return result

    # ---------- helpers ----------
    def _task_and_project(self, task_id: int):
        with Session(self.engine) as s:
            task = s.get(models.Task, task_id)
            if task is None:
                raise ValueError(f"task {task_id} not found")
            project = s.get(models.Project, task.project_id)
            return task, project

    def _handle_from_task(self, task) -> WorktreeHandle:
        return WorktreeHandle(
            task_id=str(task.id), path=task.worktree_path or "",
            branch=task.branch or "", base_sha="", created_at="",
        )

    def _update_task(self, task_id: int, **fields) -> models.Task:
        with Session(self.engine) as s:
            task = s.get(models.Task, task_id)
            for k, v in fields.items():
                setattr(task, k, v)
            task.updated_at = datetime.now(timezone.utc)
            s.add(task)
            s.commit()
            s.refresh(task)
            return task

    def _set_status(self, task_id: int, status: TaskStatus) -> None:
        self._update_task(task_id, status=status.value)

    def _set_failed(self, task_id: int, reason: str) -> None:
        """Mark a task failed AND record why, so the UI can show it."""
        self._update_task(
            task_id, status=TaskStatus.failed.value, error=(reason or "").strip()[:2000] or None
        )

    # ---------- queue management ----------
    def enqueue_task(
        self,
        task_id: int,
        priority: str = QueuePriority.normal.value,
    ) -> models.Task:
        """Add a task to the queue without starting it immediately."""
        task = self.get_task(task_id)
        if task is None:
            raise ValueError(f"task {task_id} not found")
        self.queue_manager.enqueue(task_id, task.project_id, priority)
        return self.get_task(task_id)

    def cancel_queued_task(self, task_id: int) -> models.Task:
        """Remove a task from queue without running it."""
        self.queue_manager.cancel(task_id)
        return self.get_task(task_id)

    def get_queue_status(self, project_id: int) -> dict:
        """Get queue and concurrency status for a project."""
        queue_status = self.queue_manager.get_queue_status(project_id)
        concurrency_status = self.concurrency_limiter.get_status(project_id)
        return {**queue_status, **concurrency_status}

    def list_queued_tasks(self, project_id: Optional[int] = None) -> list:
        """List all queued tasks."""
        return self.queue_manager.list_queued(project_id)

    def update_queue_priority(
        self, task_id: int, priority: str
    ) -> Optional[models.QueueEntry]:
        """Update priority of a queued task."""
        return self.queue_manager.update_priority(task_id, priority)

    def get_concurrency_config(
        self, project_id: Optional[int] = None
    ) -> models.ConcurrencyConfig:
        """Get concurrency configuration."""
        return self.queue_manager.get_config(project_id)

    def update_concurrency_config(
        self,
        project_id: Optional[int] = None,
        max_concurrent: Optional[int] = None,
        max_queued: Optional[int] = None,
        priority_mode: Optional[str] = None,
    ) -> models.ConcurrencyConfig:
        """Update concurrency configuration."""
        cfg = self.queue_manager.update_config(
            project_id, max_concurrent, max_queued, priority_mode
        )
        # Also update the limiter's in-memory state
        if max_concurrent is not None and project_id is not None:
            self.concurrency_limiter.update_limit(project_id, max_concurrent)
        return cfg

    async def process_queue(self, project_id: int) -> int:
        """Manually trigger queue processing for a project."""
        return await self.queue_manager.process_queue(project_id)

    def reconcile_orphaned_runs(self) -> int:
        """Reconcile tasks left 'running' by a server restart (called at startup).

        The in-flight-run registry is in-memory, so after a restart any task still
        marked ``running`` is orphaned — its run process is gone. Flip it to
        ``failed`` with a clear, retryable reason instead of leaving it stuck.
        """
        with Session(self.engine) as s:
            tasks = list(
                s.exec(select(models.Task).where(
                    models.Task.status == TaskStatus.running.value,
                    models.Task.deleted_at.is_(None),
                )).all()
            )
            for t in tasks:
                t.status = TaskStatus.failed.value
                t.error = ("interrupted: the server restarted while this task was "
                           "running — click Retry to run it again")
                t.updated_at = datetime.now(timezone.utc)
                s.add(t)
            for r in s.exec(select(models.Run).where(
                models.Run.status == RunStatus.running.value
            )).all():
                r.status = RunStatus.failed.value
                r.ended_at = datetime.now(timezone.utc)
                s.add(r)
            s.commit()
            return len(tasks)

    def _create_run(self, task_id: int, agent: str) -> int:
        with Session(self.engine) as s:
            run = models.Run(task_id=task_id, agent=agent, status=RunStatus.running.value)
            s.add(run)
            s.commit()
            s.refresh(run)
            return run.id

    def _save_event(self, run_id: int, seq: int, ev) -> None:
        with Session(self.engine) as s:
            s.add(models.Event(
                run_id=run_id, seq=seq, type=ev.type.value,
                payload_json=json.dumps({"text": ev.text, "data": ev.data}, ensure_ascii=False),
            ))
            s.commit()

    def _finish_run(self, run_id: int, status: RunStatus, session_id, agg, diff_ref) -> None:
        with Session(self.engine) as s:
            run = s.get(models.Run, run_id)
            run.status = status.value
            run.session_id = session_id
            run.cost = agg["cost"]
            run.tokens_in = agg["in"]
            run.tokens_out = agg["out"]
            run.duration_ms = agg["dur"]
            run.diff_ref = diff_ref
            run.ended_at = datetime.now(timezone.utc)
            s.add(run)
            s.commit()

    # ---------- next-steps suggestions ----------
    def get_next_steps(self, task_id: int) -> dict:
        """Get suggested next tasks to work on after completing a task."""
        task, project = self._task_and_project(task_id)

        with Session(self.engine) as s:
            # Get all pending tasks in the project (draft or queued)
            pending_tasks = list(
                s.exec(
                    select(models.Task)
                    .where(
                        models.Task.project_id == project.id,
                        models.Task.deleted_at.is_(None),
                        models.Task.status.in_([TaskStatus.draft.value, TaskStatus.queued.value]),
                    )
                    .order_by(models.Task.created_at)
                ).all()
            )

            # Get sibling tasks if this is a subtask
            sibling_tasks = []
            if task.parent_id:
                sibling_tasks = list(
                    s.exec(
                        select(models.Task)
                        .where(
                            models.Task.parent_id == task.parent_id,
                            models.Task.id != task_id,
                            models.Task.deleted_at.is_(None),
                            models.Task.status.in_([TaskStatus.draft.value, TaskStatus.queued.value]),
                        )
                        .order_by(models.Task.created_at)
                    ).all()
                )

            # Get parent task info if exists
            parent = None
            if task.parent_id:
                parent = s.get(models.Task, task.parent_id)

            return {
                "task": task,
                "parent": parent,
                "pending_tasks": pending_tasks,
                "sibling_tasks": sibling_tasks,
            }

    def save_next_steps_choice(
        self, task_id: int, suggested_task_ids: list[int], selected_task_ids: list[int], action: str
    ) -> models.TaskSuggestion:
        """Save user's choice for next steps."""
        with Session(self.engine) as s:
            suggestion = models.TaskSuggestion(
                task_id=task_id,
                suggested_task_ids=json.dumps(suggested_task_ids),
                selected_task_ids=json.dumps(selected_task_ids),
                action_taken=action,
            )
            s.add(suggestion)
            s.commit()
            s.refresh(suggestion)
            return suggestion

    # ---------- Smart Decomposition and Parallel Execution ----------

    async def smart_plan_task(self, task_id: int) -> list[models.Task]:
        """Enhanced task planning with intelligent model assignment.

        This is the FIRST PHASE of the two-phase approach:
        1. Analyze the problem and categorize domains
        2. Decompose into subtasks
        3. Assign optimal AI model to each subtask based on complexity and domain
        4. Analyze dependencies for parallel execution

        Returns list of created subtasks with enhanced metadata.
        """
        with Session(self.engine) as s:
            task = s.get(models.Task, task_id)
            if task is None:
                raise ValueError(f"task {task_id} not found")
            project = s.get(models.Project, task.project_id)
            project_id = task.project_id
            project_path = project.path
            prior_status = task.status
            prefer_agent = task.assigned_agent
            goal = "\n".join(p for p in (task.title, task.description) if p)
            has_children = s.exec(
                select(models.Task.id).where(models.Task.parent_id == task_id)
            ).first() is not None

        if has_children:
            raise AlreadyPlanned(task_id)

        # Get planner and model router
        planner_name = select_agent("plan", self.adapters)
        planner = self.adapters.get(planner_name) if planner_name else None
        if planner is None or "plan" not in planner.capabilities:
            raise PlanError("no planning-capable agent is available")

        model_router = ModelRouter()
        smart_decomposer = SmartDecomposer(planner, model_router)

        capabilities = sorted({c for a in self.adapters.values() for c in a.capabilities})

        self._set_status(task_id, TaskStatus.planning)
        try:
            # Use smart decomposer instead of basic planner
            enhanced_specs = await smart_decomposer.decompose_with_analysis(
                goal, project_path, capabilities
            )
        except Exception:
            self._update_task(task_id, status=prior_status)
            raise

        if not enhanced_specs:
            self._update_task(task_id, status=prior_status)
            raise PlanError("planner returned no subtasks")

        # Create subtasks with enhanced metadata
        children: list[models.Task] = []
        for spec in enhanced_specs:
            # Select agent based on recommended model
            agent = select_agent(spec.capability, self.adapters, prefer=prefer_agent)

            # Store enhanced metadata in description
            enhanced_description = spec.description
            if spec.domain or spec.complexity or spec.recommended_model:
                metadata = f"\n\n[Metadata: domain={spec.domain.value}, complexity={spec.complexity}, model={spec.recommended_model}, impact={spec.estimated_impact}]"
                enhanced_description += metadata

            child = self.create_task(
                project_id,
                spec.title,
                enhanced_description,
                agent,
                parent_id=task_id
            )
            children.append(child)

        self._set_status(task_id, TaskStatus.planned)

        # Log parallel execution plan
        batches = smart_decomposer.get_parallel_batches(enhanced_specs)
        logger.info(f"Task {task_id} decomposed into {len(children)} subtasks")
        logger.info(f"Parallel execution plan: {len(batches)} batch(es)")
        for i, batch in enumerate(batches):
            logger.info(f"  Batch {i+1}: subtasks {batch} can run in parallel")

        return children

    async def run_subtasks_parallel(
        self,
        parent_task_id: int,
        batch_indices: Optional[list[int]] = None,
    ) -> dict:
        """Execute subtasks in parallel batches.

        This is the SECOND PHASE of the two-phase approach:
        1. Identify which subtasks can run in parallel
        2. Execute them concurrently
        3. Monitor all subtask statuses
        4. Detect potential conflicts

        Args:
            parent_task_id: ID of the parent (planned) task
            batch_indices: Optional list of subtask indices to run.
                          If None, runs all pending subtasks in parallel batches.

        Returns:
            {
                "batches_completed": int,
                "subtasks_run": list[int],
                "conflicts_detected": list[ConflictInfo],
            }
        """
        with Session(self.engine) as s:
            parent_task = s.get(models.Task, parent_task_id)
            if parent_task is None:
                raise ValueError(f"task {parent_task_id} not found")

            if parent_task.status != TaskStatus.planned:
                raise ValueError(f"task {parent_task_id} is not in planned state")

            # Get all subtasks
            subtasks = list(s.exec(
                select(models.Task)
                .where(models.Task.parent_id == parent_task_id)
                .order_by(models.Task.id)
            ))

        if not subtasks:
            raise ValueError(f"task {parent_task_id} has no subtasks")

        # Parse enhanced specs from subtask descriptions
        enhanced_specs = []
        for st in subtasks:
            # Extract metadata if present
            import re
            match = re.search(
                r'\[Metadata: domain=(\w+), complexity=(\w+), model=([\w\-\.]+), impact=(\w+)\]',
                st.description or ""
            )
            if match:
                from app.orchestrator.smart_decomposer import ProblemDomain
                enhanced_specs.append(SubtaskSpec(
                    title=st.title,
                    description=st.description,
                    capability="code",  # default
                    domain=ProblemDomain(match.group(1)),
                    complexity=match.group(2),
                    recommended_model=match.group(3),
                    depends_on=[],  # Will be reconstructed
                    estimated_impact=match.group(4),
                ))
            else:
                # Fallback for subtasks without metadata
                from app.orchestrator.smart_decomposer import ProblemDomain
                enhanced_specs.append(SubtaskSpec(
                    title=st.title,
                    description=st.description or "",
                    capability="code",
                    domain=ProblemDomain.GENERAL,
                    complexity="powerful",
                    recommended_model="sonnet-4-5",
                    depends_on=[],
                    estimated_impact="medium",
                ))

        # Get parallel batches
        model_router = ModelRouter()
        planner_name = select_agent("plan", self.adapters)
        planner = self.adapters.get(planner_name)
        smart_decomposer = SmartDecomposer(planner, model_router)
        batches = smart_decomposer.get_parallel_batches(enhanced_specs)

        # Filter batches if specific indices requested
        if batch_indices is not None:
            indices_set = set(batch_indices)
            batches = [[i for i in batch if i in indices_set] for batch in batches]
            batches = [b for b in batches if b]  # Remove empty batches

        # Execute batches sequentially, tasks within each batch in parallel
        batches_completed = 0
        subtasks_run = []

        for batch_idx, batch in enumerate(batches):
            logger.info(f"Starting batch {batch_idx + 1}/{len(batches)}: {batch}")

            # Run tasks in this batch concurrently
            tasks_to_run = [subtasks[i] for i in batch]
            run_tasks = []

            for subtask in tasks_to_run:
                # Skip if already completed or running
                if subtask.status in [TaskStatus.merged, TaskStatus.awaiting_approval]:
                    logger.info(f"Skipping subtask {subtask.id} (status: {subtask.status})")
                    continue

                logger.info(f"Starting subtask {subtask.id}: {subtask.title}")
                run_tasks.append(self.run_task(subtask.id))
                subtasks_run.append(subtask.id)

            # Wait for all tasks in batch to complete
            if run_tasks:
                await asyncio.gather(*run_tasks, return_exceptions=True)

            batches_completed += 1
            logger.info(f"Batch {batch_idx + 1} completed")

        # Analyze conflicts after all batches complete
        diffs = []
        for subtask in subtasks:
            with Session(self.engine) as s:
                latest_run = s.exec(
                    select(models.Run)
                    .where(models.Run.task_id == subtask.id)
                    .order_by(models.Run.id.desc())
                ).first()
                if latest_run and latest_run.diff_ref:
                    diff_content = memory.load_diff(latest_run.diff_ref)
                    if diff_content:
                        diffs.append(diff_content)

        conflicts = ResultMerger.analyze_conflicts(diffs, enhanced_specs)

        return {
            "batches_completed": batches_completed,
            "subtasks_run": subtasks_run,
            "conflicts_detected": [
                {
                    "file_path": c.file_path,
                    "subtask_indices": c.subtask_indices,
                    "severity": c.severity,
                    "description": c.description,
                }
                for c in conflicts
            ],
        }

    def get_merge_strategy(self, parent_task_id: int) -> dict:
        """Analyze subtask results and suggest merge strategy.

        Returns:
            {
                "strategy": "auto" | "sequential" | "manual",
                "order": [subtask_ids] if sequential,
                "conflicts": list of conflict descriptions,
                "recommendation": str,
            }
        """
        with Session(self.engine) as s:
            subtasks = list(s.exec(
                select(models.Task)
                .where(models.Task.parent_id == parent_task_id)
                .order_by(models.Task.id)
            ))

        if not subtasks:
            return {
                "strategy": "auto",
                "conflicts": [],
                "recommendation": "No subtasks to merge",
            }

        # Get diffs and specs
        diffs = []
        enhanced_specs = []

        for st in subtasks:
            with Session(self.engine) as s:
                latest_run = s.exec(
                    select(models.Run)
                    .where(models.Run.task_id == st.id)
                    .order_by(models.Run.id.desc())
                ).first()
                if latest_run and latest_run.diff_ref:
                    diff_content = memory.load_diff(latest_run.diff_ref)
                    if diff_content:
                        diffs.append(diff_content)
                    else:
                        diffs.append("")
                else:
                    diffs.append("")

            # Parse metadata
            import re
            match = re.search(
                r'\[Metadata: domain=(\w+), complexity=(\w+), model=([\w\-\.]+), impact=(\w+)\]',
                st.description or ""
            )
            if match:
                from app.orchestrator.smart_decomposer import ProblemDomain
                enhanced_specs.append(SubtaskSpec(
                    title=st.title,
                    description=st.description,
                    capability="code",
                    domain=ProblemDomain(match.group(1)),
                    complexity=match.group(2),
                    recommended_model=match.group(3),
                    depends_on=[],
                    estimated_impact=match.group(4),
                ))
            else:
                from app.orchestrator.smart_decomposer import ProblemDomain
                enhanced_specs.append(SubtaskSpec(
                    title=st.title,
                    description=st.description or "",
                    capability="code",
                    domain=ProblemDomain.GENERAL,
                    complexity="powerful",
                    recommended_model="sonnet-4-5",
                    depends_on=[],
                    estimated_impact="medium",
                ))

        # Analyze conflicts
        conflicts = ResultMerger.analyze_conflicts(diffs, enhanced_specs)
        strategy = ResultMerger.suggest_merge_strategy(conflicts, enhanced_specs)

        # Convert subtask indices to IDs
        if "order" in strategy:
            strategy["order"] = [subtasks[i].id for i in strategy["order"]]

        return strategy
