"""Data models for the central Model Registry and Model Aliases."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator

from gateway.capability_registry.models import (
    CapabilityFlag,
    CapabilityState,
    ProtocolType,
    build_capability_map,
)
from gateway.core.canonical import HealthStatus


class ModalityType(StrEnum):
    """Supported input and output modalities for registered models."""

    TEXT = "text"
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    EMBEDDING = "embedding"


class DiscoverySource(StrEnum):
    """Origin of a model entry in the Model Registry."""

    MANUAL = "manual"
    PROVIDER_DISCOVERY = "provider_discovery"
    SEEDED_MANIFEST = "seeded_manifest"


class CostMetadata(BaseModel):
    """Token or character pricing metadata. None indicates cost is UNKNOWN (never fabricated)."""

    input_cost_per_1m_usd: float | None = Field(default=None, ge=0.0)
    output_cost_per_1m_usd: float | None = Field(default=None, ge=0.0)
    cached_input_cost_per_1m_usd: float | None = Field(default=None, ge=0.0)
    per_character_usd: float | None = Field(default=None, ge=0.0)
    currency: str = "USD"

    def blended_cost_per_1m_usd(self, input_ratio: float = 0.7) -> float | None:
        """Computes a weighted blended cost per 1M tokens if pricing is known."""
        if (
            self.input_cost_per_1m_usd is not None
            and self.output_cost_per_1m_usd is not None
        ):
            return round(
                (self.input_cost_per_1m_usd * input_ratio)
                + (self.output_cost_per_1m_usd * (1.0 - input_ratio)),
                4,
            )
        if self.input_cost_per_1m_usd is not None:
            return self.input_cost_per_1m_usd
        if self.per_character_usd is not None:
            return round(self.per_character_usd * 1_000_000, 4)
        return None


class LatencyStatistics(BaseModel):
    """Measured or baseline latency statistics for a model. None indicates unmeasured."""

    p50_ms: float | None = Field(default=None, gt=0.0)
    p95_ms: float | None = Field(default=None, gt=0.0)
    ttft_ms: float | None = Field(default=None, gt=0.0)
    sample_count: int = Field(default=0, ge=0)


class ModelMetadata(BaseModel):
    """Comprehensive model specification stored in the central Model Registry."""

    model_id: str  # Canonical ID, e.g., "openai/gpt-4o"
    provider_id: str
    provider_model_id: str
    display_name: str
    discovery_source: DiscoverySource = DiscoverySource.MANUAL
    supported_protocols: list[ProtocolType] = Field(default_factory=list)
    input_modalities: list[ModalityType] = Field(
        default_factory=lambda: [ModalityType.TEXT]
    )
    output_modalities: list[ModalityType] = Field(
        default_factory=lambda: [ModalityType.TEXT]
    )
    context_length: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    capabilities: dict[CapabilityFlag, CapabilityState] = Field(
        default_factory=lambda: build_capability_map(set(), set())
    )
    cost: CostMetadata = Field(default_factory=CostMetadata)
    latency: LatencyStatistics = Field(default_factory=LatencyStatistics)
    reliability_score: float = Field(default=0.98, ge=0.0, le=1.0)
    quality_scores: dict[str, float] = Field(default_factory=dict)
    health_status: HealthStatus = HealthStatus.HEALTHY
    enabled: bool = True

    @field_validator("capabilities", mode="before")
    @classmethod
    def _normalize_capability_states(cls, v: Any) -> Any:
        if isinstance(v, dict):
            return {
                k: (val.upper() if isinstance(val, str) else val)
                for k, val in v.items()
            }
        return v
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    raw_metadata: dict[str, Any] = Field(default_factory=dict)

    def get_capability_state(self, flag: CapabilityFlag) -> CapabilityState:
        return self.capabilities.get(flag, CapabilityState.UNKNOWN)

    @property
    def tool_calling_support(self) -> CapabilityState:
        return self.get_capability_state(CapabilityFlag.TOOL_CALLING)

    @property
    def structured_output_support(self) -> CapabilityState:
        return self.get_capability_state(CapabilityFlag.STRUCTURED_OUTPUTS)

    @property
    def streaming_support(self) -> CapabilityState:
        return self.get_capability_state(CapabilityFlag.STREAMING)

    def task_quality_score(self, task: str) -> float:
        """Returns task-specific suitability score in [0.0, 1.0]."""
        if task in self.quality_scores:
            return self.quality_scores[task]
        return self.quality_scores.get("general", 0.5)


class ModelRegistrationRequest(BaseModel):
    """Payload for manually registering or updating a model in the Model Registry.

    Any capability not explicitly provided in capabilities, supported_capabilities, or
    unsupported_capabilities is marked UNKNOWN — never fabricated as SUPPORTED.
    """

    model_id: str | None = None
    provider_id: str
    provider_model_id: str
    display_name: str | None = None
    supported_protocols: list[ProtocolType] = Field(
        default_factory=lambda: [ProtocolType.OPENAI_CHAT]
    )
    input_modalities: list[ModalityType] = Field(
        default_factory=lambda: [ModalityType.TEXT]
    )
    output_modalities: list[ModalityType] = Field(
        default_factory=lambda: [ModalityType.TEXT]
    )
    context_length: int | None = None
    max_output_tokens: int | None = None
    capabilities: dict[CapabilityFlag, CapabilityState] = Field(default_factory=dict)
    supported_capabilities: list[CapabilityFlag] = Field(default_factory=list)
    unsupported_capabilities: list[CapabilityFlag] = Field(default_factory=list)
    cost: CostMetadata = Field(default_factory=CostMetadata)
    latency: LatencyStatistics = Field(default_factory=LatencyStatistics)
    reliability_score: float = Field(default=0.98, ge=0.0, le=1.0)
    quality_scores: dict[str, float] = Field(default_factory=dict)
    health_status: HealthStatus = HealthStatus.HEALTHY
    enabled: bool = True

    @field_validator("capabilities", mode="before")
    @classmethod
    def _normalize_capability_states(cls, v: Any) -> Any:
        if isinstance(v, dict):
            return {
                k: (val.upper() if isinstance(val, str) else val)
                for k, val in v.items()
            }
        return v


class ModelAliasSpec(BaseModel):
    """Defines a semantic model alias (e.g., 'coding', 'fast', 'reasoning', 'auto')."""

    alias: str
    description: str
    task_type: str
    default_policy: str = "balanced"
    required_capabilities: list[CapabilityFlag] = Field(default_factory=list)
    required_input_modalities: list[ModalityType] = Field(
        default_factory=lambda: [ModalityType.TEXT]
    )
    required_output_modalities: list[ModalityType] = Field(
        default_factory=lambda: [ModalityType.TEXT]
    )
    candidate_model_ids: list[str] = Field(default_factory=list)
