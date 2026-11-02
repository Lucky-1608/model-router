"""Routing Policy definitions, weight specifications, and constraint models."""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


class RoutingWeights(BaseModel):
    """Configurable weighted scoring distribution across the 5 routing dimensions."""

    quality: float = Field(default=0.30, ge=0.0, le=1.0)
    reliability: float = Field(default=0.25, ge=0.0, le=1.0)
    cost: float = Field(default=0.20, ge=0.0, le=1.0)
    latency: float = Field(default=0.20, ge=0.0, le=1.0)
    capacity: float = Field(default=0.05, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_weights_sum(self) -> "RoutingWeights":
        total = (
            self.quality
            + self.reliability
            + self.cost
            + self.latency
            + self.capacity
        )
        if abs(total - 1.0) > 1e-4:
            raise ValueError(
                f"Routing policy weights must sum to 1.0 (got {total:.4f})"
            )
        return self


class RoutingPolicySpec(BaseModel):
    """Declarative routing policy combining scoring weights and optional hard constraints."""

    name: str
    description: str
    weights: RoutingWeights = Field(default_factory=RoutingWeights)
    max_cost_per_1m_usd: float | None = Field(default=None, ge=0.0)
    max_latency_p95_ms: float | None = Field(default=None, gt=0.0)
    min_reliability_score: float | None = Field(default=None, ge=0.0, le=1.0)
    allowed_providers: list[str] | None = None
    blocked_providers: list[str] = Field(default_factory=list)
    allow_cross_provider_fallback: bool = True

