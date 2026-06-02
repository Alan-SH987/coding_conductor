"""File-based shared memory, co-located in each managed repo under `.conductor/`.

MVP scope:
  - ensure_conductor: idempotently scaffold .conductor/memory/* + .git/info/exclude
  - build_context_bundle: read global.md to inject as system prompt (clean: does
    not write into the worktree, so it never pollutes the captured diff)
  - save_diff / read_diff: persist a task's unified diff under .conductor/diffs/
  - record_run: append one line to task_history.jsonl
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

CONDUCTOR_DIR = ".conductor"
MEMORY_DIR = "memory"
DIFFS_DIR = "diffs"

GLOBAL_TEMPLATE = """# Project Memory — global

(Conductor-managed. Edit freely; this is injected into every agent run.)

## Overview
-

## Conventions
-

## Constraints
-
"""

_SEED_FILES = {
    "architecture.md": "# Architecture\n",
    "decisions.md": "# Decisions (ADR)\n",
    "handoff.md": "# Handoff\n(none)\n",
}


def conductor_root(repo_path: str | Path) -> Path:
    return Path(repo_path) / CONDUCTOR_DIR


def memory_dir(repo_path: str | Path) -> Path:
    return conductor_root(repo_path) / MEMORY_DIR


def ensure_conductor(repo_path: str | Path) -> None:
    repo = Path(repo_path)
    mem = memory_dir(repo)
    mem.mkdir(parents=True, exist_ok=True)
    (conductor_root(repo) / DIFFS_DIR).mkdir(parents=True, exist_ok=True)

    glob = mem / "global.md"
    if not glob.exists():
        glob.write_text(GLOBAL_TEMPLATE)
    for name, seed in _SEED_FILES.items():
        f = mem / name
        if not f.exists():
            f.write_text(seed)
    hist = mem / "task_history.jsonl"
    if not hist.exists():
        hist.touch()
    _ensure_git_exclude(repo)


def _ensure_git_exclude(repo: Path) -> None:
    """Add Conductor's ignore patterns to .git/info/exclude (per-repo, untracked).

    Unlike .gitignore this file is not tracked, so scaffolding never dirties the
    work tree — which is what lets a repo's very first Run branch off a clean main.
    """
    git_dir = repo / ".git"
    if not git_dir.is_dir():
        return
    info = git_dir / "info"
    info.mkdir(parents=True, exist_ok=True)
    exclude = info / "exclude"
    needed = [".cc-worktrees/", ".conductor/diffs/"]
    existing = exclude.read_text().splitlines() if exclude.exists() else []
    missing = [n for n in needed if n not in existing]
    if not missing:
        return
    with exclude.open("a") as fh:
        if existing and existing[-1].strip():
            fh.write("\n")
        fh.write("\n".join(missing) + "\n")


def build_context_bundle(repo_path: str | Path) -> str:
    """Return shared memory to inject as system prompt, or '' if none.

    Includes the human-curated global.md PLUS recent task handoffs accumulated by
    record_handoff — bounded to the most recent slice so the prompt stays small.
    This is the read side of the memory loop: each merged task's distilled entry
    feeds the next run.
    """
    mem = memory_dir(repo_path)
    parts: list[str] = []

    glob = mem / "global.md"
    if glob.exists():
        content = glob.read_text().strip()
        if content:
            parts.append(content)

    handoff = mem / "handoff.md"
    if handoff.exists():
        h = handoff.read_text().strip()
        if h and "(none)" not in h:  # real entries exist (seed placeholder gone)
            parts.append("## Recent task handoffs (most recent last)\n\n" + h[-3000:])

    if not parts:
        return ""
    return (
        "You are working within Coding Conductor. The following is shared "
        "project memory for this repository:\n\n" + "\n\n".join(parts)
    )


def record_handoff(repo_path: str | Path, entry: str) -> None:
    """Append a distilled memory entry to handoff.md (read back into future runs).

    Drops the seed '(none)' placeholder on the first real entry. Lives under
    .conductor/ (git-excluded), so it never pollutes a captured diff.
    """
    if not entry or not entry.strip():
        return
    f = memory_dir(repo_path) / "handoff.md"
    f.parent.mkdir(parents=True, exist_ok=True)
    existing = f.read_text() if f.exists() else "# Handoff\n"
    if "(none)" in existing:
        existing = "# Handoff\n"
    f.write_text(existing.rstrip() + "\n\n" + entry.strip() + "\n")


def save_diff(repo_path: str | Path, task_id: int | str, unified_diff: str) -> str:
    d = conductor_root(repo_path) / DIFFS_DIR
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"task-{task_id}.diff"
    path.write_text(unified_diff or "")
    return str(path)


def read_diff(diff_ref: str | Path) -> str:
    p = Path(diff_ref)
    return p.read_text() if p.exists() else ""


def record_run(repo_path: str | Path, record: dict) -> None:
    entry = {**record, "ts": datetime.now(timezone.utc).isoformat()}
    hist = memory_dir(repo_path) / "task_history.jsonl"
    hist.parent.mkdir(parents=True, exist_ok=True)
    with hist.open("a") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
