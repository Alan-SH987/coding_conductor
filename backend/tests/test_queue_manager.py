"""Tests for task queue manager and concurrency control."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import AsyncIterator

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.adapters.base import (
    AgentAdapter,
    AgentEvent,
    EventType,
    HealthStatus,
    RunContext,
    TaskSpec,
)
from app.orchestrator import Orchestrator
from app.orchestrator.queue_manager import (
    TaskQueueManager,
    QueueFull,
    TaskAlreadyQueued,
)
from app.orchestrator.concurrency_limiter import (
    ConcurrencyLimiter,
    ConcurrencyLimitReached,
)
from app.storage import models
from app.storage.models import QueuePriority, TaskStatus


# --------------------------------------------------------------------------
# fixtures / helpers
# --------------------------------------------------------------------------

def _git(args: list[str], cwd: Path) -> None:
    import subprocess
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


class SlowAdapter(AgentAdapter):
    """Adapter that takes time to complete, useful for concurrency tests."""
    name = "slow"
    capabilities = {"code"}

    def __init__(self, delay: float = 0.1):
        self.delay = delay
        self.started_count = 0
        self.completed_count = 0

    async def run(self, spec: TaskSpec, ctx: RunContext) -> AsyncIterator[AgentEvent]:
        self.started_count += 1
        yield AgentEvent(EventType.meta, data={"session_id": f"slow-{self.started_count}"})
        await asyncio.sleep(self.delay)
        (Path(ctx.worktree_path) / "output.txt").write_text(f"task done\n")
        yield AgentEvent(EventType.final, text="done")
        self.completed_count += 1

    async def healthcheck(self) -> HealthStatus:
        return HealthStatus(ok=True, auth_ok=True)


class FastAdapter(AgentAdapter):
    """Instant adapter for queue tests."""
    name = "fast"
    capabilities = {"code"}

    async def run(self, spec: TaskSpec, ctx: RunContext) -> AsyncIterator[AgentEvent]:
        yield AgentEvent(EventType.meta, data={"session_id": "fast-1"})
        (Path(ctx.worktree_path) / "output.txt").write_text("done\n")
        yield AgentEvent(EventType.final, text="done")

    async def healthcheck(self) -> HealthStatus:
        return HealthStatus(ok=True, auth_ok=True)


# --------------------------------------------------------------------------
# TaskQueueManager tests
# --------------------------------------------------------------------------

def test_enqueue_task(engine, repo):
    """Test basic task enqueue."""
    orch = Orchestrator({"fast": FastAdapter()}, engine=engine)
    proj = orch.create_project("p", str(repo))
    task = orch.create_task(proj.id, "test task", agent="fast")

    queue_mgr = TaskQueueManager(engine)
    entry = queue_mgr.enqueue(task.id, proj.id, QueuePriority.normal.value)

    assert entry.task_id == task.id
    assert entry.project_id == proj.id
    assert entry.priority == QueuePriority.normal.value
    assert entry.started_at is None

    # Task status should be queued
    with Session(engine) as s:
        t = s.get(models.Task, task.id)
        assert t.status == TaskStatus.queued.value


def test_enqueue_already_queued_raises(engine, repo):
    """Test that enqueueing an already queued task raises."""
    orch = Orchestrator({"fast": FastAdapter()}, engine=engine)
    proj = orch.create_project("p", str(repo))
    task = orch.create_task(proj.id, "test task", agent="fast")

    queue_mgr = TaskQueueManager(engine)
    queue_mgr.enqueue(task.id, proj.id)

    with pytest.raises(TaskAlreadyQueued):
        queue_mgr.enqueue(task.id, proj.id)


def test_queue_full_raises(engine, repo):
    """Test that queue full condition raises."""
    orch = Orchestrator({"fast": FastAdapter()}, engine=engine)
    proj = orch.create_project("p", str(repo))

    queue_mgr = TaskQueueManager(engine)
    # Set max_queued to 2
    queue_mgr.update_config(proj.id, max_queued=2)

    # Enqueue 2 tasks
    for i in range(2):
        task = orch.create_task(proj.id, f"task {i}", agent="fast")
        queue_mgr.enqueue(task.id, proj.id)

    # Third should fail
    task3 = orch.create_task(proj.id, "task 3", agent="fast")
    with pytest.raises(QueueFull):
        queue_mgr.enqueue(task3.id, proj.id)


def test_dequeue_respects_fifo(engine, repo):
    """Test FIFO ordering."""
    orch = Orchestrator({"fast": FastAdapter()}, engine=engine)
    proj = orch.create_project("p", str(repo))

    queue_mgr = TaskQueueManager(engine)
    # No concurrency limit for this test
    queue_mgr.update_config(proj.id, max_concurrent=10)

    tasks = []
    for i in range(3):
        task = orch.create_task(proj.id, f"task {i}", agent="fast")
        queue_mgr.enqueue(task.id, proj.id)
        tasks.append(task)

    # Dequeue should return in FIFO order
    entry1 = queue_mgr.dequeue(proj.id)
    assert entry1.task_id == tasks[0].id

    entry2 = queue_mgr.dequeue(proj.id)
    assert entry2.task_id == tasks[1].id

    entry3 = queue_mgr.dequeue(proj.id)
    assert entry3.task_id == tasks[2].id


def test_dequeue_respects_priority(engine, repo):
    """Test priority ordering."""
    orch = Orchestrator({"fast": FastAdapter()}, engine=engine)
    proj = orch.create_project("p", str(repo))

    queue_mgr = TaskQueueManager(engine)
    queue_mgr.update_config(proj.id, max_concurrent=10, priority_mode="priority")

    # Enqueue in order: low, normal, urgent
    task_low = orch.create_task(proj.id, "low priority", agent="fast")
    queue_mgr.enqueue(task_low.id, proj.id, QueuePriority.low.value)

    task_normal = orch.create_task(proj.id, "normal priority", agent="fast")
    queue_mgr.enqueue(task_normal.id, proj.id, QueuePriority.normal.value)

    task_urgent = orch.create_task(proj.id, "urgent priority", agent="fast")
    queue_mgr.enqueue(task_urgent.id, proj.id, QueuePriority.urgent.value)

    # Dequeue should return in priority order: urgent, normal, low
    entry1 = queue_mgr.dequeue(proj.id)
    assert entry1.task_id == task_urgent.id

    entry2 = queue_mgr.dequeue(proj.id)
    assert entry2.task_id == task_normal.id

    entry3 = queue_mgr.dequeue(proj.id)
    assert entry3.task_id == task_low.id


def test_cancel_queued_task(engine, repo):
    """Test cancelling a queued task."""
    orch = Orchestrator({"fast": FastAdapter()}, engine=engine)
    proj = orch.create_project("p", str(repo))
    task = orch.create_task(proj.id, "test task", agent="fast")

    queue_mgr = TaskQueueManager(engine)
    queue_mgr.enqueue(task.id, proj.id)

    result = queue_mgr.cancel(task.id)
    assert result is True

    # Task should be back to draft
    with Session(engine) as s:
        t = s.get(models.Task, task.id)
        assert t.status == TaskStatus.draft.value


def test_dequeue_respects_concurrency_limit(engine, repo):
    """Test that dequeue returns None when at concurrency limit."""
    orch = Orchestrator({"fast": FastAdapter()}, engine=engine)
    proj = orch.create_project("p", str(repo))

    queue_mgr = TaskQueueManager(engine)
    queue_mgr.update_config(proj.id, max_concurrent=1)

    # Create and mark a task as running
    running_task = orch.create_task(proj.id, "running", agent="fast")
    with Session(engine) as s:
        t = s.get(models.Task, running_task.id)
        t.status = TaskStatus.running.value
        s.add(t)
        s.commit()

    # Queue another task
    queued_task = orch.create_task(proj.id, "queued", agent="fast")
    queue_mgr.enqueue(queued_task.id, proj.id)

    # Dequeue should return None (concurrency limit reached)
    entry = queue_mgr.dequeue(proj.id)
    assert entry is None


def test_get_queue_status(engine, repo):
    """Test queue status retrieval."""
    orch = Orchestrator({"fast": FastAdapter()}, engine=engine)
    proj = orch.create_project("p", str(repo))

    queue_mgr = TaskQueueManager(engine)
    queue_mgr.update_config(proj.id, max_concurrent=2, max_queued=10)

    # Queue 2 tasks
    for i in range(2):
        task = orch.create_task(proj.id, f"task {i}", agent="fast")
        queue_mgr.enqueue(task.id, proj.id)

    status = queue_mgr.get_queue_status(proj.id)
    assert status["queued"] == 2
    assert status["running"] == 0
    assert status["max_concurrent"] == 2
    assert status["max_queued"] == 10
    assert status["can_start_more"] is True
    assert status["queue_full"] is False


# --------------------------------------------------------------------------
# ConcurrencyLimiter tests
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrency_limiter_can_start(engine, repo):
    """Test concurrency limiter can_start check."""
    orch = Orchestrator({"fast": FastAdapter()}, engine=engine)
    proj = orch.create_project("p", str(repo))

    limiter = ConcurrencyLimiter(engine)

    # Should be able to start initially
    result = await limiter.can_start(proj.id)
    assert result is True


@pytest.mark.asyncio
async def test_concurrency_limiter_try_acquire(engine, repo):
    """Test semaphore acquisition respects running count."""
    orch = Orchestrator({"fast": FastAdapter()}, engine=engine)
    proj = orch.create_project("p", str(repo))

    # Set concurrency to 1
    queue_mgr = TaskQueueManager(engine)
    queue_mgr.update_config(proj.id, max_concurrent=1)

    # First, simulate a task already running
    task = orch.create_task(proj.id, "task", agent="fast")
    with Session(engine) as s:
        t = s.get(models.Task, task.id)
        t.status = TaskStatus.running.value
        s.add(t)
        s.commit()

    limiter = ConcurrencyLimiter(engine)

    # Acquire should fail because we already have 1 running (at limit)
    result = await limiter.try_acquire(proj.id)
    assert result is False

    # Now "complete" the task
    with Session(engine) as s:
        t = s.get(models.Task, task.id)
        t.status = TaskStatus.awaiting_approval.value
        s.add(t)
        s.commit()

    # Reconcile to pick up new state
    await limiter.reconcile(proj.id)

    # Now acquire should succeed
    result2 = await limiter.try_acquire(proj.id)
    assert result2 is True


def test_concurrency_limiter_release(engine, repo):
    """Test semaphore release."""
    orch = Orchestrator({"fast": FastAdapter()}, engine=engine)
    proj = orch.create_project("p", str(repo))

    limiter = ConcurrencyLimiter(engine)

    # Release when nothing held should not error
    limiter.release(proj.id)


# --------------------------------------------------------------------------
# Integration tests with Orchestrator
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_start_run_queues_when_busy(engine, repo):
    """Test that start_run queues task when concurrency limit reached."""
    adapter = FastAdapter()
    orch = Orchestrator({"fast": adapter}, engine=engine)
    proj = orch.create_project("p", str(repo))

    # Set concurrency to 1
    orch.update_concurrency_config(proj.id, max_concurrent=1)

    # Start first task (should run)
    task1 = orch.create_task(proj.id, "task 1", agent="fast")
    result1 = orch.start_run(task1.id)
    assert result1.status == TaskStatus.running.value

    # Start second task (should be queued)
    task2 = orch.create_task(proj.id, "task 2", agent="fast")
    result2 = orch.start_run(task2.id)
    assert result2.status == TaskStatus.queued.value


@pytest.mark.asyncio
async def test_start_run_raises_when_enqueue_disabled(engine, repo):
    """Test that start_run raises when enqueue_if_busy=False."""
    adapter = SlowAdapter(delay=0.5)
    orch = Orchestrator({"slow": adapter}, engine=engine)
    proj = orch.create_project("p", str(repo))

    # Set concurrency to 1
    orch.update_concurrency_config(proj.id, max_concurrent=1)

    # Start first task
    task1 = orch.create_task(proj.id, "task 1", agent="slow")
    orch.start_run(task1.id)

    # Second task should raise
    task2 = orch.create_task(proj.id, "task 2", agent="slow")
    with pytest.raises(ConcurrencyLimitReached):
        orch.start_run(task2.id, enqueue_if_busy=False)


@pytest.mark.asyncio
async def test_queue_processes_after_task_completion(engine, repo):
    """Test that queue is processed when a task completes."""
    adapter = FastAdapter()
    orch = Orchestrator({"fast": adapter}, engine=engine)
    proj = orch.create_project("p", str(repo))

    # Set concurrency to 1
    orch.update_concurrency_config(proj.id, max_concurrent=1)

    # Start first task
    task1 = orch.create_task(proj.id, "task 1", agent="fast")
    orch.start_run(task1.id)

    # Queue task2
    task2 = orch.create_task(proj.id, "task 2", agent="fast")
    result2 = orch.start_run(task2.id)
    assert result2.status == TaskStatus.queued.value

    # Wait for task1 to complete (which should trigger queue processing)
    await asyncio.sleep(0.3)

    # Check that task2 eventually ran
    with Session(engine) as s:
        t2 = s.get(models.Task, task2.id)
        # Should be running or completed
        assert t2.status in [
            TaskStatus.running.value,
            TaskStatus.awaiting_approval.value,
        ]


def test_queue_priority_update(engine, repo):
    """Test updating priority of queued task."""
    orch = Orchestrator({"fast": FastAdapter()}, engine=engine)
    proj = orch.create_project("p", str(repo))

    task = orch.create_task(proj.id, "test", agent="fast")
    orch.enqueue_task(task.id, QueuePriority.normal.value)

    entry = orch.update_queue_priority(task.id, QueuePriority.urgent.value)
    assert entry.priority == QueuePriority.urgent.value


def test_list_queued_tasks(engine, repo):
    """Test listing queued tasks."""
    orch = Orchestrator({"fast": FastAdapter()}, engine=engine)
    proj = orch.create_project("p", str(repo))

    # Queue multiple tasks
    for i in range(3):
        task = orch.create_task(proj.id, f"task {i}", agent="fast")
        orch.enqueue_task(task.id)

    queued = orch.list_queued_tasks(proj.id)
    assert len(queued) == 3
