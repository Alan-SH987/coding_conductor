"""FastAPI dependency wiring.

A single process-wide Orchestrator holds the adapter registry. Tests override
`get_orchestrator` to inject a fake-adapter orchestrator on a throwaway engine.
"""

from __future__ import annotations

from functools import lru_cache

from app.adapters.claude import ClaudeAdapter
from app.adapters.codex import CodexAdapter
from app.orchestrator import Orchestrator


@lru_cache
def get_orchestrator() -> Orchestrator:
    adapters = {"claude": ClaudeAdapter(), "codex": CodexAdapter()}
    return Orchestrator(adapters)
