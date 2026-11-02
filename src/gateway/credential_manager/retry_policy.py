"""Retry policy, exponential backoff with jitter, and failure-driven credential state transitions."""

from __future__ import annotations

from dataclasses import dataclass
import random


@dataclass(frozen=True, slots=True)
class RetryPolicyConfig:
    """Configuration for bounded retry and exponential backoff with jitter."""

    max_attempts: int = 3
    base_delay_seconds: float = 0.05
    max_delay_seconds: float = 2.0
    jitter_min_factor: float = 0.5
    jitter_max_factor: float = 1.0
    respect_retry_after: bool = True
    max_honored_retry_after_seconds: float = 5.0

    def compute_backoff_seconds(
        self,
        attempt_index: int,
        retry_after_seconds: float | None = None,
    ) -> float:
        """Computes bounded exponential backoff with jitter, respecting provider retry-after when appropriate."""
        if (
            self.respect_retry_after
            and retry_after_seconds is not None
            and retry_after_seconds > 0
        ):
            return min(retry_after_seconds, self.max_honored_retry_after_seconds)

        exp_delay = min(
            self.max_delay_seconds,
            self.base_delay_seconds * (2 ** max(attempt_index, 0)),
        )
        jitter = random.uniform(self.jitter_min_factor, self.jitter_max_factor)
        return round(exp_delay * jitter, 4)

