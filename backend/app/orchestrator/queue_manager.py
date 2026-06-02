"""Task queue manager for FIFO/priority-based task scheduling.

Provides:
- Enqueue tasks with priority
- Dequeue next task respecting concurrency limits
- Query queue status
- Cancel queued tasks
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Session, select, func

from app.storage import models
from app.storage.models import (
    QueueEntry,
    QueuePriority,
    ConcurrencyConfig,
    TaskStatus,
)

logger = logging.getLogger(__name__)

# Priority order for sorting (lower number = higher priority)
PRIORITY_ORDER = {
    QueuePriority.urgent.value: 0,
    QueuePriority.high.value: 1,
    QueuePriority.normal.value: 2,
    QueuePriority.low.value: 3,
}

# Default concurrency settings
DEFAULT_MAX_CONCURRENT = 3
DEFAULT_MAX_QUEUED = 50


class QueueFull(Exception):
    """Raised when queue is at capacity."""

    def __init__(self, project_id: int, max_queued: int):
        self.project_id = project_id
        self.max_queued = max_queued
        super().__init__(f"Queue full for project {project_id} (max: {max_queued})")


class TaskAlreadyQueued(Exception):
    """Raised when trying to enqueue a task that's already queued."""

    def __init__(self, task_id: int):
        self.task_id = task_id
        super().__init__(f"Task {task_id} is already in queue")


class TaskQueueManager:
    """Manages task queue with priority and concurrency control."""

    def __init__(self, engine):
        self.engine = engine
        self._process_lock = asyncio.Lock()
        # Callback to start a task (set by OrchestratorService)
        self._start_task_callback: Optional[callable] = None

    def set_start_callback(self, callback: callable) -> None:
        """Set the callback function to start a task."""
        self._start_task_callback = callback

    # -------------------------------------------------------------------------
    # Configuration
    # -------------------------------------------------------------------------

    def get_config(self, project_id: Optional[int] = None) -> ConcurrencyConfig:
        """Get concurrency config for project, falling back to global."""
        with Session(self.engine) as s:
            # Try project-specific first
            if project_id is not None:
                cfg = s.exec(
                    select(ConcurrencyConfig).where(
                        ConcurrencyConfig.project_id == project_id
                    )
                ).first()
                if cfg:
                    return cfg

            # Fall back to global
            cfg = s.exec(
                select(ConcurrencyConfig).where(
                    ConcurrencyConfig.project_id.is_(None)
                )
            ).first()
            if cfg:
                return cfg

            # Create default global config
            cfg = ConcurrencyConfig(
                project_id=None,
                max_concurrent=DEFAULT_MAX_CONCURRENT,
                max_queued=DEFAULT_MAX_QUEUED,
                priority_mode="fifo",
            )
            s.add(cfg)
            s.commit()
            s.refresh(cfg)
            return cfg

    def update_config(
        self,
        project_id: Optional[int] = None,
        max_concurrent: Optional[int] = None,
        max_queued: Optional[int] = None,
        priority_mode: Optional[str] = None,
    ) -> ConcurrencyConfig:
        """Update or create concurrency config."""
        with Session(self.engine) as s:
            if project_id is not None:
                cfg = s.exec(
                    select(ConcurrencyConfig).where(
                        ConcurrencyConfig.project_id == project_id
                    )
                ).first()
            else:
                cfg = s.exec(
                    select(ConcurrencyConfig).where(
                        ConcurrencyConfig.project_id.is_(None)
                    )
                ).first()

            if not cfg:
                cfg = ConcurrencyConfig(project_id=project_id)
                s.add(cfg)

            if max_concurrent is not None:
                cfg.max_concurrent = max_concurrent
            if max_queued is not None:
                cfg.max_queued = max_queued
            if priority_mode is not None:
                cfg.priority_mode = priority_mode
            cfg.updated_at = datetime.now(timezone.utc)

            s.commit()
            s.refresh(cfg)
            return cfg

    # -------------------------------------------------------------------------
    # Queue operations
    # -------------------------------------------------------------------------

    def enqueue(
        self,
        task_id: int,
        project_id: int,
        priority: str = QueuePriority.normal.value,
    ) -> QueueEntry:
        """Add a task to the queue.

        Raises:
            QueueFull: If queue is at capacity
            TaskAlreadyQueued: If task is already queued
        """
        with Session(self.engine) as s:
            # Check if already queued
            existing = s.exec(
                select(QueueEntry).where(
                    QueueEntry.task_id == task_id,
                    QueueEntry.started_at.is_(None),
                    QueueEntry.removed_reason.is_(None),
                )
            ).first()
            if existing:
                raise TaskAlreadyQueued(task_id)

            # Check queue capacity
            cfg = self.get_config(project_id)
            queue_size = s.exec(
                select(func.count(QueueEntry.id)).where(
                    QueueEntry.project_id == project_id,
                    QueueEntry.started_at.is_(None),
                    QueueEntry.removed_reason.is_(None),
                )
            ).one()
            if queue_size >= cfg.max_queued:
                raise QueueFull(project_id, cfg.max_queued)

            # Get next position
            max_pos = s.exec(
                select(func.max(QueueEntry.position)).where(
                    QueueEntry.project_id == project_id
                )
            ).one()
            next_pos = (max_pos or 0) + 1

            # Create entry
            entry = QueueEntry(
                task_id=task_id,
                project_id=project_id,
                priority=priority,
                position=next_pos,
            )
            s.add(entry)

            # Update task status to queued
            task = s.get(models.Task, task_id)
            if task:
                task.status = TaskStatus.queued.value
                task.updated_at = datetime.now(timezone.utc)
                s.add(task)

            s.commit()
            s.refresh(entry)
            logger.info(
                f"Task {task_id} enqueued at position {next_pos} "
                f"(priority: {priority})"
            )
            return entry

    def dequeue(self, project_id: int) -> Optional[QueueEntry]:
        """Get and mark the next task to run from queue.

        Returns None if queue is empty or concurrency limit reached.
        """
        with Session(self.engine) as s:
            cfg = self.get_config(project_id)

            # Check current running count
            running_count = s.exec(
                select(func.count(models.Task.id)).where(
                    models.Task.project_id == project_id,
                    models.Task.status == TaskStatus.running.value,
                    models.Task.deleted_at.is_(None),
                )
            ).one()

            if running_count >= cfg.max_concurrent:
                logger.debug(
                    f"Concurrency limit reached for project {project_id} "
                    f"({running_count}/{cfg.max_concurrent})"
                )
                return None

            # Build query for next task
            query = (
                select(QueueEntry)
                .where(
                    QueueEntry.project_id == project_id,
                    QueueEntry.started_at.is_(None),
                    QueueEntry.removed_reason.is_(None),
                )
            )

            if cfg.priority_mode == "priority":
                # Order by priority (urgent first), then by position (FIFO within priority)
                query = query.order_by(
                    # SQLite doesn't have CASE, use explicit ordering
                    QueueEntry.priority.asc(),  # 'high' < 'low' < 'normal' < 'urgent' alphabetically - need workaround
                    QueueEntry.position.asc(),
                )
            else:
                # Pure FIFO
                query = query.order_by(QueueEntry.position.asc())

            entries = s.exec(query).all()
            if not entries:
                return None

            # For priority mode, sort properly in Python
            if cfg.priority_mode == "priority":
                entries = sorted(
                    entries,
                    key=lambda e: (PRIORITY_ORDER.get(e.priority, 2), e.position)
                )

            entry = entries[0]
            entry.started_at = datetime.now(timezone.utc)
            s.add(entry)
            s.commit()
            s.refresh(entry)

            logger.info(f"Dequeued task {entry.task_id} from position {entry.position}")
            return entry

    def cancel(self, task_id: int, reason: str = "cancelled") -> bool:
        """Remove a task from queue without running it.

        Returns True if task was in queue and removed.
        """
        with Session(self.engine) as s:
            entry = s.exec(
                select(QueueEntry).where(
                    QueueEntry.task_id == task_id,
                    QueueEntry.started_at.is_(None),
                    QueueEntry.removed_reason.is_(None),
                )
            ).first()

            if not entry:
                return False

            entry.removed_reason = reason
            s.add(entry)

            # Revert task status to draft
            task = s.get(models.Task, task_id)
            if task and task.status == TaskStatus.queued.value:
                task.status = TaskStatus.draft.value
                task.updated_at = datetime.now(timezone.utc)
                s.add(task)

            s.commit()
            logger.info(f"Task {task_id} removed from queue: {reason}")
            return True

    def update_priority(
        self, task_id: int, priority: str
    ) -> Optional[QueueEntry]:
        """Update priority of a queued task."""
        with Session(self.engine) as s:
            entry = s.exec(
                select(QueueEntry).where(
                    QueueEntry.task_id == task_id,
                    QueueEntry.started_at.is_(None),
                    QueueEntry.removed_reason.is_(None),
                )
            ).first()

            if not entry:
                return None

            entry.priority = priority
            s.add(entry)
            s.commit()
            s.refresh(entry)
            logger.info(f"Task {task_id} priority updated to {priority}")
            return entry

    # -------------------------------------------------------------------------
    # Query operations
    # -------------------------------------------------------------------------

    def list_queued(
        self, project_id: Optional[int] = None
    ) -> list[QueueEntry]:
        """List all queued (waiting) tasks."""
        with Session(self.engine) as s:
            query = select(QueueEntry).where(
                QueueEntry.started_at.is_(None),
                QueueEntry.removed_reason.is_(None),
            )
            if project_id is not None:
                query = query.where(QueueEntry.project_id == project_id)

            query = query.order_by(QueueEntry.position.asc())
            return list(s.exec(query).all())

    def get_queue_status(self, project_id: int) -> dict:
        """Get queue status for a project."""
        with Session(self.engine) as s:
            cfg = self.get_config(project_id)

            queued_count = s.exec(
                select(func.count(QueueEntry.id)).where(
                    QueueEntry.project_id == project_id,
                    QueueEntry.started_at.is_(None),
                    QueueEntry.removed_reason.is_(None),
                )
            ).one()

            running_count = s.exec(
                select(func.count(models.Task.id)).where(
                    models.Task.project_id == project_id,
                    models.Task.status == TaskStatus.running.value,
                    models.Task.deleted_at.is_(None),
                )
            ).one()

            return {
                "project_id": project_id,
                "queued": queued_count,
                "running": running_count,
                "max_concurrent": cfg.max_concurrent,
                "max_queued": cfg.max_queued,
                "priority_mode": cfg.priority_mode,
                "can_start_more": running_count < cfg.max_concurrent,
                "queue_full": queued_count >= cfg.max_queued,
            }

    def get_position(self, task_id: int) -> Optional[int]:
        """Get a task's position in queue (1-indexed), or None if not queued."""
        with Session(self.engine) as s:
            entry = s.exec(
                select(QueueEntry).where(
                    QueueEntry.task_id == task_id,
                    QueueEntry.started_at.is_(None),
                    QueueEntry.removed_reason.is_(None),
                )
            ).first()

            if not entry:
                return None

            # Count how many are ahead
            ahead = s.exec(
                select(func.count(QueueEntry.id)).where(
                    QueueEntry.project_id == entry.project_id,
                    QueueEntry.position < entry.position,
                    QueueEntry.started_at.is_(None),
                    QueueEntry.removed_reason.is_(None),
                )
            ).one()

            return ahead + 1

    # -------------------------------------------------------------------------
    # Queue processing
    # -------------------------------------------------------------------------

    async def process_queue(self, project_id: int) -> int:
        """Process queue and start tasks up to concurrency limit.

        Returns number of tasks started.
        """
        if not self._start_task_callback:
            logger.warning("No start_task_callback set, cannot process queue")
            return 0

        async with self._process_lock:
            started = 0
            while True:
                entry = self.dequeue(project_id)
                if not entry:
                    break

                try:
                    # Call the orchestrator's start_run
                    await self._start_task_callback(entry.task_id)
                    started += 1
                except Exception as e:
                    logger.error(f"Failed to start task {entry.task_id}: {e}")
                    # Mark entry as failed
                    with Session(self.engine) as s:
                        entry = s.get(QueueEntry, entry.id)
                        if entry:
                            entry.removed_reason = f"start_failed: {e}"
                            s.add(entry)
                            s.commit()

            if started:
                logger.info(f"Started {started} tasks from queue for project {project_id}")
            return started

    async def on_task_completed(self, project_id: int) -> None:
        """Called when a task completes to potentially start queued tasks."""
        await self.process_queue(project_id)
