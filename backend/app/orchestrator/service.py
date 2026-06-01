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
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlmodel import Session, select

from app import memory
from app.adapters.base import AgentAdapter, EventType, RunContext, TaskSpec
from app.gitops import GitOpsEngine
from app.gitops.models import WorktreeHandle
from app.orchestrator.model_router import ModelRouter
from app.orchestrator.routing import select_agent
from app.storage import models
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


class Orchestrator:
    def __init__(self, adapters: dict[str, AgentAdapter], engine=None):
        self.adapters = adapters
        self.engine = engine or default_engine
        # task_id -> in-flight asyncio Task. In-memory only; on restart the
        # registry is empty and a stuck "running" task can simply be re-run.
        self._running: dict[int, asyncio.Task] = {}

    # ---------- projects ----------
    def create_project(self, name: str, path: str) -> models.Project:
        git = GitOpsEngine(path)
        info = git.inspect_repo()  # raises NotAGitRepo if invalid
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

    def _has_children(self, task_id: int) -> bool:
        with Session(self.engine) as s:
            return s.exec(
                select(models.Task.id).where(models.Task.parent_id == task_id)
            ).first() is not None

    def start_run(self, task_id: int) -> models.Task:
        """Launch run_task in the background and return immediately.

        The task is flipped to ``running`` synchronously so the caller (and the
        SSE stream) sees the transition right away; the agent then drives in a
        detached asyncio Task. Requires a running event loop.
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

        self._set_status(task_id, TaskStatus.running)
        loop_task = asyncio.create_task(self._run_guarded(task_id))
        self._running[task_id] = loop_task
        loop_task.add_done_callback(lambda _f: self._running.pop(task_id, None))
        return self.get_task(task_id)

    async def _run_guarded(self, task_id: int) -> None:
        # run_task already records failed status on error; just keep the
        # detached task from surfacing an "exception never retrieved" warning.
        try:
            await self.run_task(task_id)
        except Exception:
            logger.exception("run_task(%s) crashed", task_id)

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

        # Check quota before starting the run
        try:
            self.check_quota(project_id)
        except QuotaExceeded:
            self._set_status(task_id, TaskStatus.failed)
            raise

        if agent_name not in self.adapters:
            self._set_status(task_id, TaskStatus.failed)
            raise ValueError(f"no adapter named {agent_name!r}")

        git = GitOpsEngine(project_path)
        # A prior failed run leaves its worktree/branch behind (cleanup only
        # happens on approve/reject), so clear any stale attempt before creating
        # a fresh worktree off the default branch — otherwise a retry collides.
        git.reset_worktree(task_id)
        try:
            handle = git.create_worktree(task_id)
        except Exception:
            self._set_status(task_id, TaskStatus.failed)
            raise

        self._update_task(task_id, branch=handle.branch,
                          worktree_path=handle.path, status=TaskStatus.running.value)

        run_id = self._create_run(task_id, agent_name)
        spec = TaskSpec(goal=spec_goal)
        ctx = RunContext(
            worktree_path=handle.path,
            system_prompt=memory.build_context_bundle(project_path),
        )
        return await self._drive_run(
            task_id, run_id, agent_name, project_path, git, handle, spec, ctx
        )

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
            review = s.exec(
                select(models.Review)
                .where(models.Review.task_id == task_id)
                .order_by(models.Review.id.desc())
            ).first()

        summary = review.summary if review else ""
        findings = json.loads(review.findings_json) if review else []
        revision = self._compose_revision_prompt(summary, findings)
        bundle = memory.build_context_bundle(project_path)
        system_prompt = f"{bundle}\n\n{revision}" if bundle else revision

        git = GitOpsEngine(project_path)
        handle = WorktreeHandle(
            task_id=str(task_id), path=worktree_path or "", branch=branch or "",
            base_sha=git.merge_base(branch), created_at="",
        )
        spec = TaskSpec(goal=spec_goal)
        ctx = RunContext(worktree_path=worktree_path, system_prompt=system_prompt)
        return await self._drive_run(
            task_id, run_id, agent_name, project_path, git, handle, spec, ctx
        )

    async def _drive_run(self, task_id, run_id, agent_name, project_path,
                         git, handle, spec, ctx) -> models.Task:
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
        except Exception:
            self._finish_run(run_id, RunStatus.failed, session_id, agg, diff_ref=None)
            self._set_status(task_id, TaskStatus.failed)
            raise

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
        return self._update_task(task_id, status=final_status.value)

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

    def get_project_usage(self, project_id: int) -> dict:
        """Calculate total usage (tokens and cost) for a project."""
        with Session(self.engine) as s:
            # Get all runs for tasks in this project
            result = s.exec(
                select(models.Run)
                .join(models.Task)
                .where(models.Task.project_id == project_id)
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
            self._update_task(task_id, status=TaskStatus.merged.value)
        elif res.verify_failed:
            # Build/test gate failed — the merge was aborted, nothing landed on
            # main. Leave the task at awaiting_approval (worktree intact) so the
            # human can revise and retry rather than losing the gate state.
            pass
        else:
            self._update_task(task_id, status=TaskStatus.failed.value)
        return res

    def reject_task(self, task_id: int) -> models.Task:
        task, project = self._task_and_project(task_id)
        git = GitOpsEngine(project.path)
        git.remove_worktree(self._handle_from_task(task))
        return self._update_task(task_id, status=TaskStatus.rejected.value)

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
