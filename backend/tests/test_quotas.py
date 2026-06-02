"""Tests for project quota management."""

from datetime import datetime, timezone

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.orchestrator import Orchestrator, QuotaExceeded
from app.storage import models


def _ended_at():
    return datetime.now(timezone.utc)


@pytest.fixture
def engine(tmp_path):
    """Create a test database."""
    db = tmp_path / "test.db"
    eng = create_engine(f"sqlite:///{db}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(eng)
    return eng


@pytest.fixture
def orch(engine):
    """Create an orchestrator with empty adapters for testing."""
    return Orchestrator(adapters={}, engine=engine)


@pytest.fixture
def project(engine):
    """Create a test project."""
    with Session(engine) as s:
        proj = models.Project(name="test", path="/tmp/test", default_branch="main")
        s.add(proj)
        s.commit()
        s.refresh(proj)
        return proj


@pytest.fixture
def task(engine, project):
    """Create a test task."""
    with Session(engine) as s:
        task = models.Task(
            project_id=project.id,
            title="test task",
            assigned_agent="test",
            status="draft",
        )
        s.add(task)
        s.commit()
        s.refresh(task)
        return task


def test_get_project_usage_empty(orch, project):
    """New projects should have zero usage."""
    usage = orch.get_project_usage(project.id)
    assert usage["total_tokens"] == 0
    assert usage["total_cost_usd"] == 0
    assert usage["run_count"] == 0


def test_update_project_quotas(orch, project):
    """Should be able to update quota settings."""
    updated = orch.update_project_quotas(project.id, quota_tokens=10000, quota_cost_usd=5.0)
    assert updated is not None
    assert updated.quota_tokens == 10000
    assert updated.quota_cost_usd == 5.0

    # Verify persistence
    proj = orch.get_project(project.id)
    assert proj.quota_tokens == 10000
    assert proj.quota_cost_usd == 5.0


def test_update_quotas_to_none(orch, project):
    """Should be able to remove quotas by setting to None."""
    orch.update_project_quotas(project.id, quota_tokens=1000, quota_cost_usd=1.0)
    updated = orch.update_project_quotas(project.id, quota_tokens=None, quota_cost_usd=None)
    assert updated.quota_tokens is None
    assert updated.quota_cost_usd is None


def test_check_quota_no_limits(orch, project):
    """Projects without quota limits should pass quota check."""
    orch.check_quota(project.id)  # Should not raise


def test_quota_exceeded_tokens(orch, engine, project, task):
    """Should raise QuotaExceeded when token quota is exceeded."""
    # Set a low token quota
    orch.update_project_quotas(project.id, quota_tokens=100, quota_cost_usd=None)

    # Simulate a run with high token usage
    with Session(engine) as s:
        run = models.Run(
            task_id=task.id,
            agent="test",
            status="succeeded",
            tokens_in=50,
            tokens_out=60,  # Total: 110 > 100
            cost=0.0,
            ended_at=_ended_at(),
        )
        s.add(run)
        s.commit()

    # Should raise when checking quota
    with pytest.raises(QuotaExceeded) as exc_info:
        orch.check_quota(project.id)
    assert "Token quota exceeded" in str(exc_info.value)


def test_quota_exceeded_cost(orch, engine, project, task):
    """Should raise QuotaExceeded when cost quota is exceeded."""
    # Set a low cost quota
    orch.update_project_quotas(project.id, quota_tokens=None, quota_cost_usd=1.0)

    # Simulate a run with high cost
    with Session(engine) as s:
        run = models.Run(
            task_id=task.id,
            agent="test",
            status="succeeded",
            tokens_in=100,
            tokens_out=100,
            cost=1.5,  # > 1.0
            ended_at=_ended_at(),
        )
        s.add(run)
        s.commit()

    # Should raise when checking quota
    with pytest.raises(QuotaExceeded) as exc_info:
        orch.check_quota(project.id)
    assert "Cost quota exceeded" in str(exc_info.value)


def test_quota_not_exceeded(orch, engine, project, task):
    """Should not raise when usage is below quota."""
    # Set quotas
    orch.update_project_quotas(project.id, quota_tokens=1000, quota_cost_usd=10.0)

    # Simulate a run within limits
    with Session(engine) as s:
        run = models.Run(
            task_id=task.id,
            agent="test",
            status="succeeded",
            tokens_in=100,
            tokens_out=100,
            cost=0.5,
            ended_at=_ended_at(),
        )
        s.add(run)
        s.commit()

    # Should not raise
    orch.check_quota(project.id)


def test_get_project_usage_with_runs(orch, engine, project, task):
    """Should correctly calculate usage from multiple runs."""
    with Session(engine) as s:
        # Add multiple runs
        for i in range(3):
            run = models.Run(
                task_id=task.id,
                agent="test",
                status="succeeded",
                tokens_in=100,
                tokens_out=50,
                cost=0.25,
                ended_at=_ended_at(),
            )
            s.add(run)
        s.commit()

    usage = orch.get_project_usage(project.id)
    assert usage["total_tokens"] == 450  # (100 + 50) * 3
    assert usage["total_cost_usd"] == 0.75  # 0.25 * 3
    assert usage["run_count"] == 3


def test_get_project_usage_ignores_running_runs(orch, engine, project, task):
    """In-progress runs should not count toward completed project usage."""
    with Session(engine) as s:
        s.add(
            models.Run(
                task_id=task.id,
                agent="test",
                status="running",
                tokens_in=100,
                tokens_out=50,
                cost=0.25,
            )
        )
        s.commit()

    usage = orch.get_project_usage(project.id)
    assert usage["total_tokens"] == 0
    assert usage["total_cost_usd"] == 0
    assert usage["run_count"] == 0
