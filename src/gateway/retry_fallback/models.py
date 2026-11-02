"""Data models for Retry, Circuit-Aware Execution, and Cross-Provider Fallback."""

from __future__ import annotations

from pydantic import BaseModel, Field

from gateway.core.canonical import CanonicalInternalResponse


class FallbackGuardVerdict(BaseModel):
    """Result of evaluating FallbackCompatibilityGuard on a candidate target."""

    compatible: bool
    reason_code: str = "OK"
    details: str = ""


class ExecutionAttemptTrace(BaseModel):
    """Telemetry record for a single upstream execution attempt."""

    attempt_number: int
    provider_id: str
    model_id: str
    credential_id: str | None = None
    success: bool
    error_code: str | None = None
    error_message: str | None = None
    latency_ms: float = 0.0
    fallback_reason: str | None = None


class ExecutionOutcome(BaseModel):
    """Complete result of executing a request through the RetryFallbackEngine."""

    response: CanonicalInternalResponse
    selected_provider: str
    selected_model: str
    credential_id: str | None = None
    decision_id: str | None = None
    attempts: list[ExecutionAttemptTrace] = Field(default_factory=list)
    fallback_used: bool = False
    ttft_ms: float | None = None
    total_latency_ms: float = 0.0

