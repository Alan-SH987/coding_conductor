from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TaskStatus(str, Enum):
    draft = "draft"
    planning = "planning"
    planned = "planned"
    queued = "queued"
    running = "running"
    review = "review"
    awaiting_approval = "awaiting_approval"
    merged = "merged"
    rejected = "rejected"
    failed = "failed"


class RunStatus(str, Enum):
    running = "running"
    succeeded = "succeeded"
    failed = "failed"


class Project(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    path: str
    default_branch: str = "main"
    is_pinned: bool = False
    is_archived: bool = False
    deleted_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=utcnow)
    # Quota management (single-user, project-level)
    quota_tokens: Optional[int] = None  # Max tokens allowed (None = unlimited)
    quota_cost_usd: Optional[float] = None  # Max cost in USD (None = unlimited)
    # Pre-merge gate: a shell command run in the repo working dir against the
    # merged result before a task is committed to main. None = no gate.
    verify_cmd: Optional[str] = None


class Task(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id")
    parent_id: Optional[int] = Field(default=None, foreign_key="task.id")
    title: str
    description: str = ""
    status: str = Field(default=TaskStatus.draft.value)
    assigned_agent: Optional[str] = None
    branch: Optional[str] = None
    worktree_path: Optional[str] = None
    deleted_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Run(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    task_id: int = Field(foreign_key="task.id")
    agent: str
    session_id: Optional[str] = None
    status: str = Field(default=RunStatus.running.value)
    tokens_in: int = 0
    tokens_out: int = 0
    cost: float = 0.0
    duration_ms: int = 0
    diff_ref: Optional[str] = None
    started_at: datetime = Field(default_factory=utcnow)
    ended_at: Optional[datetime] = None


class Event(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: int = Field(foreign_key="run.id")
    seq: int
    type: str
    payload_json: str = "{}"
    ts: datetime = Field(default_factory=utcnow)


class Review(SQLModel, table=True):
    """An advisory AI verdict on a task's captured diff (latest = max id)."""
    id: Optional[int] = Field(default=None, primary_key=True)
    task_id: int = Field(foreign_key="task.id")
    run_id: Optional[int] = Field(default=None, foreign_key="run.id")
    agent: str
    verdict: str  # approve | request_changes
    summary: str = ""
    findings_json: str = "[]"
    created_at: datetime = Field(default_factory=utcnow)


class TaskSuggestion(SQLModel, table=True):
    """Records next-step suggestions presented to user after task completion."""
    id: Optional[int] = Field(default=None, primary_key=True)
    task_id: int = Field(foreign_key="task.id")
    suggested_task_ids: str = "[]"  # JSON array of task IDs suggested
    selected_task_ids: str = "[]"  # JSON array of task IDs user selected
    action_taken: Optional[str] = None  # 'selected' | 'skipped' | 'created_new'
    created_at: datetime = Field(default_factory=utcnow)
