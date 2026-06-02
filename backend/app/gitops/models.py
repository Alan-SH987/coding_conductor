from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RepoInfo:
    is_git: bool
    default_branch: str
    is_dirty: bool
    head_sha: str


@dataclass
class WorktreeHandle:
    task_id: str
    path: str
    branch: str
    base_sha: str
    created_at: str


@dataclass
class DiffFile:
    path: str
    status: str  # A (added) / M (modified) / D (deleted) / R (renamed) ...
    additions: int
    deletions: int


@dataclass
class DiffResult:
    files: list[DiffFile]
    unified_diff: str
    stat: str
    is_empty: bool


@dataclass
class MergeResult:
    ok: bool
    merged_sha: str | None
    conflict: bool
    conflicted_files: list[str] = field(default_factory=list)
    # Pre-merge verify gate outcome (only meaningful when ok is False).
    verify_failed: bool = False
    verify_output: str = ""
    # Main had uncommitted changes — the merge was refused (commit/stash first).
    dirty: bool = False
    dirty_files: list[str] = field(default_factory=list)
    # Post-merge push outcome (only meaningful when ok is True).
    push_ok: bool = False
    push_output: str = ""
