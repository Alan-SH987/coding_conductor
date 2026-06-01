"""Unit tests for the capability-based Router (pure function, no I/O)."""

from __future__ import annotations

from app.orchestrator.routing import select_agent


class _Stub:
    """Minimal adapter stand-in: the router only reads `.capabilities`."""

    def __init__(self, *caps: str):
        self.capabilities = set(caps)


def _two_agents():
    # claude is plan-capable; codex is not — mirrors the real registry.
    return {"claude": _Stub("plan", "code", "review"), "codex": _Stub("code", "review")}


def test_prefer_wins_when_capable():
    assert select_agent("code", _two_agents(), prefer="codex") == "codex"


def test_prefer_ignored_when_incapable():
    # codex can't plan, so the router falls through to a capable agent.
    assert select_agent("plan", _two_agents(), prefer="codex") == "claude"


def test_priority_breaks_ties():
    # both capable; DEFAULT_PRIORITY puts claude first regardless of dict order.
    adapters = {"codex": _Stub("code"), "claude": _Stub("code")}
    assert select_agent("code", adapters) == "claude"


def test_only_capable_agent_chosen():
    assert select_agent("plan", _two_agents()) == "claude"


def test_fallback_to_priority_head_when_nobody_capable():
    # unknown capability: still return a deterministic choice, never None.
    assert select_agent("deploy", _two_agents()) == "claude"


def test_empty_registry_returns_none():
    assert select_agent("code", {}) is None
