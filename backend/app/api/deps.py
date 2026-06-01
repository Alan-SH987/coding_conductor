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
    adapters = {
        # Claude adapters (default, single tier for now)
        "claude": ClaudeAdapter(),

        # Codex adapters with different model tiers
        "codex": CodexAdapter(),  # Default model
        "haiku": CodexAdapter(model="haiku"),
        "sonnet-3-5": CodexAdapter(model="sonnet-3-5"),
        "sonnet-4-5": CodexAdapter(model="sonnet-4-5"),
        "opus": CodexAdapter(model="opus"),
    }
    return Orchestrator(adapters)
