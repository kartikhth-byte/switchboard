"""Retry policy shared by the jobs worker and inline tool retries (SHR-007)."""

from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 5
    base_s: float = 2.0
    max_s: float = 300.0
    jitter: float = 0.2  # ±20%, so retries from a burst don't line up

    def delay_s(self, attempt: int) -> float:
        """Delay before retry number `attempt` (1 = first retry): base * 2^(attempt-1), capped."""
        raw = min(self.max_s, self.base_s * float(2 ** max(0, attempt - 1)))
        if self.jitter:
            raw *= 1 + random.uniform(-self.jitter, self.jitter)  # noqa: S311 — not crypto
        return max(0.0, raw)


class PermanentJobError(Exception):
    """Raise from a job handler when retrying cannot help (bad payload, opted-out lead)."""
