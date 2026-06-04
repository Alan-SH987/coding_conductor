"""Capability-based agent routing.

A deliberately tiny pure function: pick the agent that should handle a unit of
work given its required capability. It's forward-looking — it matters once there
are many agents. Today claude/codex overlap heavily, so most calls resolve
trivially, and that's fine.
"""

from __future__ import annotations

from typing import Optional

from app.adapters.base import AgentAdapter

# Tie-break order when several adapters share a capability.
DEFAULT_PRIORITY = ["claude", "codex"]


def select_agent(
    capability: str,
    adapters: dict[str, AgentAdapter],
    prefer: Optional[str] = None,
    priority: Optional[list[str]] = None,
    avoid: Optional[str] = None,
) -> Optional[str]:
    """Return the name of the agent best suited to ``capability``.

    Rules: a capable ``prefer`` wins; otherwise the first capable adapter in
    priority order, preferring one that isn't ``avoid`` (cross-model audit: the
    reviewer should differ from the implementer); otherwise fall back so a routing
    decision is always made. Returns ``None`` only when no adapters exist at all.
    """
    if not adapters:
        return None
    if prefer and prefer in adapters and capability in adapters[prefer].capabilities:
        return prefer
    order = priority or DEFAULT_PRIORITY
    ordered = [n for n in order if n in adapters]
    ordered += [n for n in adapters if n not in ordered]
    capable = [n for n in ordered if capability in adapters[n].capabilities]
    # Prefer a capable agent other than `avoid`; fall back to the avoided one only
    # if it's the sole capable adapter (single-agent setups still work).
    preferred = [n for n in capable if n != avoid]
    if preferred:
        return preferred[0]
    if capable:
        return capable[0]
    return ordered[0]
