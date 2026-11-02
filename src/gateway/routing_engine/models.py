"""Models for Two-Stage Routing Engine decisions, score breakdowns, and explainability traces."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
import uuid

from pydantic import BaseModel, Field

from gateway.capability_registry.models import CapabilityFlag
from gateway.model_registry.models import ModalityType
from gateway.routing_policy.models import RoutingWeights


class FilteredCandidateReason(BaseModel):
    """Records why a candidate model was eliminated during Stage 1 hard capability filtering."""

    model_id: str
    provider_id: str
    reason_code: str
    details: str


class CandidateScoreBreakdown(BaseModel):
    """Detailed Stage 2 score calculation for a surviving candidate model."""

    model_id: str
    provider_id: str
    total_score: float
    quality_score: float
    reliability_score: float
    cost_score: float
    latency_score: float
    capacity_score: float
    weighted_contributions: dict[str, float] = Field(default_factory=dict)


class FallbackDecisionRecord(BaseModel):
    """Records a fallback transition or planned fallback step."""

    step: int
    from_model_id: str
    to_model_id: str
    to_provider_id: str
    reason: str


class RoutingDecisionTrace(BaseModel):
    """Complete explainability trace produced by every routing decision.

    Never exposes sensitive credential values.
    """

    decision_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    request_id: str
    original_request: dict[str, Any]
    requested_model_or_alias: str
    requested_task: str
    applied_policy: str
    weights: RoutingWeights
    required_capabilities: list[CapabilityFlag]
    required_input_modalities: list[ModalityType]
    required_output_modalities: list[ModalityType]
    required_context_length: int
    candidate_models: list[str]
    filtered_candidates: list[FilteredCandidateReason]
    ranking_scores: list[CandidateScoreBreakdown]
    selected_model: str
    selected_provider: str
    selected_credential_ref: str | None = None
    fallback_decisions: list[FallbackDecisionRecord] = Field(default_factory=list)
    decided_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class RouteSimulationRequest(BaseModel):
    """Request payload for /api/v1/routing/route to evaluate and explain a routing decision."""

    model: str = "auto"
    task: str | None = None
    policy: str | None = None
    required_capabilities: list[CapabilityFlag] = Field(default_factory=list)
    input_modalities: list[ModalityType] = Field(
        default_factory=lambda: [ModalityType.TEXT]
    )
    output_modalities: list[ModalityType] = Field(
        default_factory=lambda: [ModalityType.TEXT]
    )
    estimated_input_tokens: int = Field(default=500, ge=0)
    max_output_tokens: int | None = Field(default=None, ge=1)
    stream: bool = False
    tools_count: int = Field(default=0, ge=0)
    structured_output: bool = False
    allowed_providers: list[str] | None = None
    blocked_providers: list[str] = Field(default_factory=list)
    max_cost_per_1m_usd: float | None = Field(default=None, ge=0.0)

