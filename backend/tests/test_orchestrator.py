"""Offline state-machine tests for the Orchestrator.

No login, no network: a `FakeAdapter` stands in for a real CLI agent. It emits
the normalized event stream (meta/message/tool_use/final/cost) *and* writes a
file into the worktree, so `snapshot_and_diff` produces a real, non-empty diff
and the merge/reject paths exercise actual git.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import AsyncIterator

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.adapters.base import (
    AgentAdapter,
    AgentEvent,
    EventType,
    HealthStatus,
    RunContext,
    TaskSpec,
)
from app.orchestrator import Orchestrator
from app.orchestrator.retry_policy import RetryPolicy
from app.storage import models


# --------------------------------------------------------------------------
# fixtures / helpers
# --------------------------------------------------------------------------
def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True,
                   capture_output=True, text=True)


def _init_repo(path: Path) -> None:
    _git(["init", "-b", "main"], path)
    _git(["config", "user.name", "Test"], path)
    _git(["config", "user.email", "test@example.com"], path)
    (path / "README.md").write_text("# test repo\n")
    _git(["add", "-A"], path)
    _git(["commit", "-m", "init"], path)


@pytest.fixture
def engine(tmp_path):
    db = tmp_path / "test.db"
    eng = create_engine(f"sqlite:///{db}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(eng)
    return eng


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _init_repo(r)
    return r


def _get_task(engine, task_id: int) -> models.Task:
    with Session(engine) as s:
        return s.get(models.Task, task_id)


def _latest_run(engine, task_id: int) -> models.Run:
    with Session(engine) as s:
        return s.exec(
            select(models.Run)
            .where(models.Run.task_id == task_id)
            .order_by(models.Run.id.desc())
        ).first()


# --------------------------------------------------------------------------
# fake adapters
# --------------------------------------------------------------------------
class FakeAdapter(AgentAdapter):
    name = "fake"
    capabilities = {"code"}

    async def run(self, spec: TaskSpec, ctx: RunContext) -> AsyncIterator[AgentEvent]:
        yield AgentEvent(EventType.meta, data={"session_id": "fake-sess-1"})
        yield AgentEvent(EventType.message, text="working on it")
        # Do the actual "work" the agent would do: edit the worktree.
        (Path(ctx.worktree_path) / "hello.txt").write_text("hello from fake adapter\n")
        yield AgentEvent(EventType.tool_use, text="Write", data={"name": "Write"})
        yield AgentEvent(EventType.final, text="done", data={"session_id": "fake-sess-1"})
        yield AgentEvent(EventType.cost, data={
            "cost_usd": 0.0123, "input_tokens": 100,
            "output_tokens": 50, "duration_ms": 4567,
        })

    async def healthcheck(self) -> HealthStatus:
        return HealthStatus(ok=True, auth_ok=True, version="fake-1.0")


class ErrorAdapter(AgentAdapter):
    """Streams a normal start then an `error` event (no exception raised)."""
    name = "fake"
    capabilities = {"code"}

    async def run(self, spec: TaskSpec, ctx: RunContext) -> AsyncIterator[AgentEvent]:
        yield AgentEvent(EventType.meta, data={"session_id": "err-sess"})
        yield AgentEvent(EventType.error, text="boom", data={"kind": "runtime"})

    async def healthcheck(self) -> HealthStatus:
        return HealthStatus(ok=True, auth_ok=True)


class CrashAdapter(AgentAdapter):
    """Blows up mid-stream to exercise the exception path."""
    name = "fake"
    capabilities = {"code"}

    async def run(self, spec: TaskSpec, ctx: RunContext) -> AsyncIterator[AgentEvent]:
        yield AgentEvent(EventType.meta, data={"session_id": "crash-sess"})
        raise RuntimeError("adapter exploded")

    async def healthcheck(self) -> HealthStatus:
        return HealthStatus(ok=True, auth_ok=True)


class FailThenSucceedAdapter(AgentAdapter):
    """First run leaves partial work then errors (task → failed, worktree kept);
    the retry behaves like FakeAdapter and reaches awaiting_approval."""
    name = "fake"
    capabilities = {"code"}

    def __init__(self):
        self.calls = 0

    async def run(self, spec: TaskSpec, ctx: RunContext) -> AsyncIterator[AgentEvent]:
        self.calls += 1
        if self.calls == 1:
            (Path(ctx.worktree_path) / "partial.txt").write_text("half-done\n")
            yield AgentEvent(EventType.meta, data={"session_id": "fail-sess"})
            yield AgentEvent(EventType.error, text="boom", data={"kind": "runtime"})
            return
        (Path(ctx.worktree_path) / "hello.txt").write_text("hello from retry\n")
        yield AgentEvent(EventType.meta, data={"session_id": "ok-sess"})
        yield AgentEvent(EventType.final, text="done", data={"session_id": "ok-sess"})

    async def healthcheck(self) -> HealthStatus:
        return HealthStatus(ok=True, auth_ok=True)


class TransientThenSucceedAdapter(AgentAdapter):
    """A transient transport failure is retried in the same worktree/session."""
    name = "fake"
    capabilities = {"code"}

    def __init__(self):
        self.calls = 0
        self.resume_session_ids: list[str | None] = []

    def supports_resume(self) -> bool:
        return True

    async def run(self, spec: TaskSpec, ctx: RunContext) -> AsyncIterator[AgentEvent]:
        self.calls += 1
        self.resume_session_ids.append(ctx.resume_session_id)
        if self.calls == 1:
            (Path(ctx.worktree_path) / "partial.txt").write_text("kept progress\n")
            yield AgentEvent(EventType.meta, data={"session_id": "transient-sess"})
            yield AgentEvent(
                EventType.error,
                text="network timeout while streaming",
                data={"kind": "network"},
            )
            return
        (Path(ctx.worktree_path) / "recovered.txt").write_text("finished after retry\n")
        yield AgentEvent(EventType.meta, data={"session_id": "transient-sess"})
        yield AgentEvent(EventType.final, text="done", data={"session_id": "transient-sess"})

    async def healthcheck(self) -> HealthStatus:
        return HealthStatus(ok=True, auth_ok=True)


# --------------------------------------------------------------------------
# tests
# --------------------------------------------------------------------------
def test_run_task_reaches_awaiting_approval(engine, repo):
    orch = Orchestrator({"fake": FakeAdapter()}, engine=engine)
    proj = orch.create_project("p", str(repo))
    task = orch.create_task(proj.id, "do thing", agent="fake")
    assert task.status == "draft"

    result = asyncio.run(orch.run_task(task.id))

    assert result.status == "awaiting_approval"
    assert result.branch == f"conductor/task-{task.id}"
    assert result.worktree_path

    # diff captured and readable
    diff = orch.get_diff(task.id)
    assert "hello from fake adapter" in diff

    # run persisted with aggregated metrics
    run = _latest_run(engine, task.id)
    assert run.status == "succeeded"
    assert run.session_id == "fake-sess-1"
    assert run.cost == pytest.approx(0.0123)
    assert run.tokens_in == 100
    assert run.tokens_out == 50
    assert run.duration_ms == 4567
    assert run.diff_ref

    # every streamed event persisted in order
    with Session(engine) as s:
        events = s.exec(
            select(models.Event)
            .where(models.Event.run_id == run.id)
            .order_by(models.Event.seq)
        ).all()
    assert [e.type for e in events] == ["meta", "message", "tool_use", "final", "cost"]
    assert [e.seq for e in events] == [0, 1, 2, 3, 4]


def test_approve_task_merges_into_main(engine, repo):
    orch = Orchestrator({"fake": FakeAdapter()}, engine=engine)
    proj = orch.create_project("p", str(repo))
    task = orch.create_task(proj.id, "do thing", agent="fake")
    asyncio.run(orch.run_task(task.id))

    wt = Path(_get_task(engine, task.id).worktree_path)
    res = orch.approve_task(task.id)

    assert res.ok
    assert res.merged_sha
    # change landed on main's working tree
    merged_file = repo / "hello.txt"
    assert merged_file.exists()
    assert merged_file.read_text() == "hello from fake adapter\n"
    # task closed out, worktree cleaned up
    assert _get_task(engine, task.id).status == "merged"
    assert not wt.exists()


def test_reject_task_discards_worktree(engine, repo):
    orch = Orchestrator({"fake": FakeAdapter()}, engine=engine)
    proj = orch.create_project("p", str(repo))
    task = orch.create_task(proj.id, "do thing", agent="fake")
    asyncio.run(orch.run_task(task.id))

    wt = Path(_get_task(engine, task.id).worktree_path)
    assert wt.exists()

    result = orch.reject_task(task.id)

    assert result.status == "rejected"
    assert not wt.exists()
    # nothing leaked into main
    assert not (repo / "hello.txt").exists()


def test_error_event_marks_failed(engine, repo):
    orch = Orchestrator({"fake": ErrorAdapter()}, engine=engine)
    proj = orch.create_project("p", str(repo))
    task = orch.create_task(proj.id, "do thing", agent="fake")

    result = asyncio.run(orch.run_task(task.id))

    assert result.status == "failed"
    assert _latest_run(engine, task.id).status == "failed"


def test_failed_task_can_be_rerun(engine, repo):
    """A failed run leaves its worktree/branch behind; re-running must reset
    that stale attempt and reach awaiting_approval with a clean diff."""
    orch = Orchestrator({"fake": FailThenSucceedAdapter()}, engine=engine)
    proj = orch.create_project("p", str(repo))
    task = orch.create_task(proj.id, "do thing", agent="fake")

    first = asyncio.run(orch.run_task(task.id))
    assert first.status == "failed"

    second = asyncio.run(orch.run_task(task.id))
    assert second.status == "awaiting_approval"
    assert second.branch == f"conductor/task-{task.id}"
    # a fresh run row was recorded for the retry (the bug created none)
    assert len(orch.list_runs(task.id)) == 2

    diff = orch.get_diff(task.id)
    assert "hello from retry" in diff
    # the failed attempt's partial work must not leak into the retry's diff
    assert "half-done" not in diff


def test_transient_error_auto_retries_with_resume(engine, repo):
    adapter = TransientThenSucceedAdapter()
    orch = Orchestrator(
        {"fake": adapter},
        engine=engine,
        retry_policy=RetryPolicy(max_attempts=3, initial_delay_seconds=0),
    )
    proj = orch.create_project("p", str(repo))
    task = orch.create_task(proj.id, "do thing", agent="fake")

    result = asyncio.run(orch.run_task(task.id))

    assert result.status == "awaiting_approval"
    assert adapter.calls == 2
    assert adapter.resume_session_ids == [None, "transient-sess"]
    runs = orch.list_runs(task.id)
    assert [r.status for r in runs] == ["failed", "succeeded"]
    assert [r.session_id for r in runs] == ["transient-sess", "transient-sess"]
    diff = orch.get_diff(task.id)
    assert "kept progress" in diff
    assert "finished after retry" in diff


def test_adapter_crash_marks_failed_and_raises(engine, repo):
    orch = Orchestrator({"fake": CrashAdapter()}, engine=engine)
    proj = orch.create_project("p", str(repo))
    task = orch.create_task(proj.id, "do thing", agent="fake")

    with pytest.raises(RuntimeError):
        asyncio.run(orch.run_task(task.id))

    assert _get_task(engine, task.id).status == "failed"
    assert _latest_run(engine, task.id).status == "failed"


def test_unknown_agent_marks_failed(engine, repo):
    orch = Orchestrator({"fake": FakeAdapter()}, engine=engine)
    proj = orch.create_project("p", str(repo))
    task = orch.create_task(proj.id, "do thing", agent="ghost")

    with pytest.raises(ValueError):
        asyncio.run(orch.run_task(task.id))

    assert _get_task(engine, task.id).status == "failed"
