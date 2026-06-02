"""Classify agent failures for retry/recovery decisions."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ClassifiedError:
    kind: str
    recoverable: bool
    reason: str


_NON_RECOVERABLE_KINDS = {"auth", "quota"}
_RECOVERABLE_KINDS = {"network", "timeout", "transient"}
_RECOVERABLE_TERMS = (
    "connection reset",
    "connection refused",
    "connection timed out",
    "deadline exceeded",
    "dns",
    "econnreset",
    "network",
    "temporarily unavailable",
    "temporary failure",
    "timeout",
    "timed out",
    "tls",
    "transport",
    "try again",
)


def classify_error(text: str = "", kind: str | None = None) -> ClassifiedError:
    """Return whether a failure is safe to retry automatically.

    Generic runtime errors are not retried by default: they may represent a real
    task/agent problem. We only auto-retry clearly transient transport failures
    or adapter-provided transient kinds.
    """
    normalized_kind = (kind or "").strip().lower()
    normalized_text = (text or "").strip().lower()

    if normalized_kind in _NON_RECOVERABLE_KINDS:
        return ClassifiedError(normalized_kind, False, "non-recoverable agent error")
    if normalized_kind in _RECOVERABLE_KINDS:
        return ClassifiedError(normalized_kind, True, "recoverable agent error")
    if any(term in normalized_text for term in _RECOVERABLE_TERMS):
        return ClassifiedError(normalized_kind or "transient", True, "transient failure")
    return ClassifiedError(normalized_kind or "runtime", False, "not classified as transient")
