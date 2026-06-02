"""Global, tool-agnostic skills injected into the agent's system prompt.

Skills live OUTSIDE any repo at ``~/.conductor/skills/<name>/SKILL.md`` so one
install serves every project AND every tool: a skill is delivered by injecting
its instructions into the system prompt (never written into the worktree, so the
captured diff stays clean), which both the Claude and Codex CLIs honor equally.

Phase 1 is instruction-only — no scripts/resources execution yet. Install is
manual: drop a ``<name>/SKILL.md`` folder into ``~/.conductor/skills/``.

SKILL.md format (same as a Claude skill):

    ---
    name: pdf
    description: When the task involves .pdf files — extract / merge / split.
    ---
    <markdown instructions injected into the agent>
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

# Module-level so tests can monkeypatch it to a tmp dir.
SKILLS_DIR = Path.home() / ".conductor" / "skills"


def _parse_skill(md: str) -> tuple[str, str]:
    """Return (description, body) from a SKILL.md with YAML-ish frontmatter."""
    description = ""
    body = md
    if md.startswith("---"):
        end = md.find("\n---", 3)
        if end != -1:
            front = md[3:end]
            body = md[end + 4:].lstrip("\n")
            for line in front.splitlines():
                key, sep, val = line.partition(":")
                if sep and key.strip().lower() == "description":
                    description = val.strip()
    return description, body.strip()


def list_skills() -> list[dict]:
    """All installed skills as {name, description}, sorted by name."""
    if not SKILLS_DIR.is_dir():
        return []
    out: list[dict] = []
    for d in sorted(SKILLS_DIR.iterdir()):
        md = d / "SKILL.md"
        if d.is_dir() and md.is_file():
            desc, _ = _parse_skill(md.read_text())
            out.append({"name": d.name, "description": desc})
    return out


def read_skill(name: str) -> Optional[str]:
    md = SKILLS_DIR / name / "SKILL.md"
    return md.read_text() if md.is_file() else None


def parse_enabled(enabled_json: Optional[str]) -> list[str]:
    """Decode a Project.enabled_skills JSON string into a list (safe on junk)."""
    if not enabled_json:
        return []
    try:
        value = json.loads(enabled_json)
    except (ValueError, TypeError):
        return []
    return [str(x) for x in value] if isinstance(value, list) else []


def build_skills_bundle(enabled: list[str]) -> str:
    """Format the enabled skills as a system-prompt section, or '' if none."""
    sections: list[str] = []
    for name in enabled or []:
        md = read_skill(name)
        if not md:
            continue
        desc, body = _parse_skill(md)
        header = f"## Skill: {name}" + (f"\n{desc}" if desc else "")
        sections.append(f"{header}\n\n{body}".strip())
    if not sections:
        return ""
    return (
        "The following reusable skills are available — use one when its "
        "description matches the task:\n\n" + "\n\n---\n\n".join(sections)
    )
