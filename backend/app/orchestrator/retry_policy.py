"""Retry policy for recoverable task runs."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from app.orchestrator.error_classifier import ClassifiedError


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    initial_delay_seconds: float = 1.0
    backoff_factor: float = 2.0

    def should_retry(self, attempt: int, error: ClassifiedError) -> bool:
        return error.recoverable and attempt < self.max_attempts

    async def sleep_before_retry(self, attempt: int) -> None:
        delay = self.initial_delay_seconds * (self.backoff_factor ** max(0, attempt - 1))
        if delay > 0:
            await asyncio.sleep(delay)
