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
import re
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


# Lightweight tokens for keyword matching: English whole words plus Chinese
# character bigrams (CJK has no word boundaries).
_CJK_RANGE = "一-鿿"


def _terms(text: str) -> set[str]:
    text = text.lower()
    latin = set(re.findall(r"[a-z0-9]{3,}", text))
    cjk = re.findall(rf"[{_CJK_RANGE}]", text)
    bigrams = {a + b for a, b in zip(cjk, cjk[1:])}
    return latin | bigrams


# ---------- auto-tagging ----------
# Domain/stack keywords to look for (extendable)
_TAG_PATTERNS: dict[str, list[str]] = {
    # Domains
    "#auth": ["auth", "login", "logout", "session", "token", "jwt", "oauth", "password", "认证", "登录"],
    "#api": ["api", "endpoint", "rest", "graphql", "route", "接口"],
    "#ui": ["ui", "component", "button", "form", "modal", "dialog", "界面", "组件"],
    "#db": ["database", "db", "sql", "query", "migration", "schema", "数据库"],
    "#test": ["test", "spec", "mock", "fixture", "测试"],
    "#doc": ["doc", "readme", "comment", "文档"],
    "#perf": ["perf", "performance", "optimize", "cache", "性能", "优化"],
    "#bug": ["bug", "fix", "error", "issue", "crash", "修复", "错误"],
    "#refactor": ["refactor", "cleanup", "重构"],
    "#feature": ["feature", "add", "new", "implement", "功能", "新增"],
    # Tech stacks
    "#python": ["python", "py", "pip", "django", "flask", "fastapi"],
    "#typescript": ["typescript", "ts", "tsx"],
    "#javascript": ["javascript", "js", "jsx", "node", "npm"],
    "#react": ["react", "hook", "useState", "useEffect", "component"],
    "#nextjs": ["nextjs", "next.js", "next"],
    "#docker": ["docker", "container", "compose", "dockerfile"],
    "#git": ["git", "commit", "branch", "merge", "rebase"],
    "#ci": ["ci", "cd", "pipeline", "github actions", "workflow"],
}


def extract_tags(title: str, description: str = "", files: list[str] | None = None) -> list[str]:
    """Extract relevant tags from task title, description, and changed files.

    Returns a list of tags like ["#auth", "#python", "#api"]. The tags are
    deterministic and based on keyword matching — no LLM involved.
    """
    text = f"{title} {description}".lower()

    # Also extract from file extensions and paths
    if files:
        for f in files:
            f_lower = f.lower()
            text += f" {f_lower}"
            # Add extension hints
            if f_lower.endswith(".py"):
                text += " python"
            elif f_lower.endswith((".ts", ".tsx")):
                text += " typescript"
            elif f_lower.endswith((".js", ".jsx")):
                text += " javascript"
            elif f_lower.endswith(".sql"):
                text += " sql database"

    found: list[str] = []
    for tag, keywords in _TAG_PATTERNS.items():
        for kw in keywords:
            if kw in text:
                found.append(tag)
                break  # one match per tag is enough

    return sorted(set(found))


def _retrieve_handoffs(
    handoff_md: str, query: str, k: int = 5, query_tags: list[str] | None = None
) -> list[str]:
    """Up to k handoff entries most relevant to ``query`` by keyword overlap,
    falling back to the most recent when there's no query or no match.

    If ``query_tags`` are provided, entries with matching tags get a bonus
    (tag matches are weighted 2x compared to keyword matches).
    """
    body = handoff_md.split("\n", 1)[-1] if handoff_md.startswith("# Handoff") else handoff_md
    entries = [e.strip() for e in re.split(r"(?=^### )", body, flags=re.M) if e.strip()]
    if not entries:
        return []

    qterms = _terms(query)
    qtags = set(query_tags or [])

    if not qterms and not qtags:
        return entries[-k:]

    def score_entry(entry: str) -> float:
        """Score an entry: keyword overlap + tag bonus."""
        entry_terms = _terms(entry)
        keyword_score = len(qterms & entry_terms)

        # Extract tags mentioned in the entry (e.g., from "- tags: #auth, #api")
        tag_match = re.search(r"- tags:\s*(.+)", entry)
        if tag_match and qtags:
            entry_tags = set(re.findall(r"#\w+", tag_match.group(1)))
            tag_score = len(qtags & entry_tags) * 2  # tags weighted 2x
        else:
            tag_score = 0

        return keyword_score + tag_score

    scored = [(score_entry(e), i, e) for i, e in enumerate(entries)]
    relevant = [
        e for (s, i, e) in sorted(scored, key=lambda x: (x[0], x[1]), reverse=True)
        if s > 0
    ][:k]
    return relevant or entries[-k:]


def build_context_bundle(
    repo_path: str | Path, query: str = "", query_tags: list[str] | None = None
) -> str:
    """Return shared memory to inject as system prompt, or '' if none.

    The human-curated global.md PLUS the task handoffs most relevant to ``query``
    (the current task) by keyword overlap — recency as a fallback. Read side of
    the memory loop; bounded so the prompt stays small.

    If ``query_tags`` are provided (e.g., ["#auth", "#api"]), handoff entries
    with matching tags will be weighted higher during retrieval.
    """
    mem = memory_dir(repo_path)
    parts: list[str] = []

    glob = mem / "global.md"
    if glob.exists():
        content = glob.read_text().strip()
        if content:
            parts.append(content)

    insights = mem / "insights.md"
    if insights.exists():
        c = insights.read_text().strip()
        if c:
            parts.append(c)

    handoff = mem / "handoff.md"
    if handoff.exists():
        h = handoff.read_text().strip()
        if h and "(none)" not in h:  # real entries exist (seed placeholder gone)
            entries = _retrieve_handoffs(h, query, k=5, query_tags=query_tags)
            if entries:
                parts.append("## Relevant task handoffs\n\n" + "\n\n".join(entries)[-3000:])

    if not parts:
        return ""
    return (
        "You are working within Coding Conductor. The following is shared "
        "project memory for this repository:\n\n" + "\n\n".join(parts)
    )


def read_handoffs(repo_path: str | Path) -> str:
    """The accumulated handoff entries, or '' if none (seed placeholder dropped)."""
    f = memory_dir(repo_path) / "handoff.md"
    if not f.exists():
        return ""
    h = f.read_text().strip()
    return "" if (not h or "(none)" in h) else h


def write_insights(repo_path: str | Path, text: str) -> None:
    """Overwrite insights.md with a fresh distillation (machine-managed).

    Kept SEPARATE from the human-curated global.md so auto-distillation never
    clobbers hand-written memory. Injected into every run via build_context_bundle.
    """
    if not text or not text.strip():
        return
    f = memory_dir(repo_path) / "insights.md"
    f.parent.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    f.write_text(f"# Distilled insights (auto-generated, {today})\n\n{text.strip()}\n")


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
