"""Unified adapter contract.

Every agent (Claude, Codex, ...) maps its native CLI behaviour onto this one
interface: a stream of normalized `AgentEvent`s. The orchestrator never sees a
provider-specific detail.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import AsyncIterator, Optional


class EventType(str, Enum):
    meta = "meta"            # session/model info (from init)
    message = "message"      # assistant natural-language text
    thinking = "thinking"    # extended thinking
    tool_use = "tool_use"    # agent invoked a tool
    tool_result = "tool_result"
    final = "final"          # terminal assistant result (success)
    cost = "cost"            # tokens / usd / duration
    error = "error"          # auth or runtime failure
    diff_ready = "diff_ready"  # synthesized by orchestrator, not the CLI


@dataclass
class AgentEvent:
    type: EventType
    text: str = ""
    data: dict = field(default_factory=dict)


@dataclass
class TaskSpec:
    goal: str
    constraints: str = ""
    acceptance: str = ""


@dataclass
class SubtaskSpec:
    """One decomposed unit of work produced by a planner adapter."""
    title: str
    description: str = ""
    capability: str = "code"


@dataclass
class ReviewFinding:
    """A single issue raised by a reviewing adapter."""
    severity: str = "warning"  # blocker | warning | nit
    comment: str = ""
    file: str = ""


@dataclass
class ReviewResult:
    """A reviewing adapter's verdict on a captured diff."""
    verdict: str  # approve | request_changes
    summary: str = ""
    findings: list[ReviewFinding] = field(default_factory=list)


@dataclass
class RunContext:
    worktree_path: str
    system_prompt: str = ""
    permission_mode: str = "acceptEdits"
    timeout: int = 600
    resume_session_id: Optional[str] = None


@dataclass
class HealthStatus:
    ok: bool          # CLI present and runnable
    auth_ok: bool     # logged in / credentials valid
    version: str = ""
    detail: str = ""


class AgentAdapter(ABC):
    name: str = "base"
    capabilities: set[str] = set()

    @abstractmethod
    def run(self, spec: TaskSpec, ctx: RunContext) -> AsyncIterator[AgentEvent]:
        """Drive the agent on a task, yielding normalized events."""
        raise NotImplementedError

    @abstractmethod
    async def healthcheck(self) -> HealthStatus:
        """Report whether the CLI is installed and authenticated."""
        raise NotImplementedError

    def supports_resume(self) -> bool:
        return False

    async def plan(
        self, goal: str, repo_path: str, capabilities: list[str]
    ) -> list[SubtaskSpec]:
        """Decompose a high-level goal into subtasks by reading the repo.

        Read-only: a planner must never modify the working tree. Only
        plan-capable adapters override this; the rest signal unsupported.
        """
        raise NotImplementedError(f"{self.name} does not support planning")

    async def review(
        self, goal: str, diff: str, repo_path: str
    ) -> ReviewResult:
        """Review a captured diff against its goal and return a verdict.

        Read-only: a reviewer reads the diff (and the repo for context) but
        never modifies the working tree. Only review-capable adapters override
        this; the rest signal unsupported.
        """
        raise NotImplementedError(f"{self.name} does not support reviewing")
