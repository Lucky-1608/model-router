"""Retry and Fallback Engine module — circuit breakers, bounded retries, and compatibility-safe fallback."""

from gateway.retry_fallback.engine import RetryFallbackEngine
from gateway.retry_fallback.guard import FallbackCompatibilityGuard
from gateway.retry_fallback.models import (
    ExecutionAttemptTrace,
    ExecutionOutcome,
    FallbackGuardVerdict,
)

__all__ = [
    "ExecutionAttemptTrace",
    "ExecutionOutcome",
    "FallbackCompatibilityGuard",
    "FallbackGuardVerdict",
    "RetryFallbackEngine",
]
