"""Data models for Health Monitor and Redis-Coordinated Circuit Breakers."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum

from pydantic import BaseModel, Field

from gateway.core.canonical import HealthStatus


class CircuitBreakerState(StrEnum):
    """Tri-state circuit breaker status."""

    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitScope(StrEnum):
    """Granularity of a circuit breaker."""

    PROVIDER = "provider"
    MODEL = "model"
    CREDENTIAL = "credential"


class CircuitBreakerConfig(BaseModel):
    """Configuration parameters for circuit breaker state transitions."""

    failure_threshold: int = Field(default=5, ge=1, le=100)
    recovery_timeout_seconds: float = Field(default=30.0, gt=0.0, le=3600.0)
    half_open_max_probes: int = Field(default=1, ge=1, le=10)
    rolling_window_seconds: int = Field(default=120, ge=10, le=3600)


class CircuitBreakerSnapshot(BaseModel):
    """Current state and telemetry of a circuit breaker."""

    scope: CircuitScope
    target_id: str
    state: CircuitBreakerState = CircuitBreakerState.CLOSED
    failure_count: int = 0
    consecutive_successes: int = 0
    opened_until_epoch: float | None = None
    remaining_open_seconds: float = 0.0
    last_error_code: str | None = None
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    health_status: HealthStatus = HealthStatus.HEALTHY

