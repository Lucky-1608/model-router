"""SQLAlchemy ORM and Pydantic data models for Usage Tracking and Cost Accounting."""

from __future__ import annotations

from datetime import datetime, timezone
import uuid

from pydantic import BaseModel, Field
from sqlalchemy import Boolean, DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from gateway.core.canonical import TokenUsage
from gateway.core.db import Base


class UsageRecordORM(Base):
    """Persistent usage and telemetry log record. Never stores raw prompts, completions, or secrets."""

    __tablename__ = "gateway_usage_records"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    request_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    decision_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    provider_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    model_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    credential_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    account_group_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Gateway client API key (ClientApiKeyORM.id) that issued the request; used for budget enforcement.
    client_key_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    protocol: Mapped[str] = mapped_column(String(64), nullable=False, default="openai_chat")
    integration_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="unified")
    stream: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False, default=200)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    fallback_used: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    tool_rounds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cached_input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reasoning_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    characters: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    latency_ms: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    ttft_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class UsageEventInput(BaseModel):
    """Input event recorded upon completion or failure of a gateway request."""

    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    decision_id: str | None = None
    provider_id: str
    model_id: str
    credential_id: str | None = None
    account_group_id: str | None = None
    client_key_id: str | None = None
    protocol: str = "openai_chat"
    integration_mode: str = "unified"
    stream: bool = False
    status_code: int = 200
    success: bool = True
    error_code: str | None = None
    attempt_count: int = 1
    fallback_used: bool = False
    tool_rounds: int = 0
    usage: TokenUsage = Field(default_factory=TokenUsage)
    latency_ms: float = 0.0
    ttft_ms: float | None = None


class UsageRecordView(BaseModel):
    """Redacted public view of a recorded gateway request."""

    id: str
    request_id: str
    decision_id: str | None = None
    provider_id: str
    model_id: str
    credential_id: str | None = None
    account_group_id: str | None = None
    client_key_id: str | None = None
    protocol: str
    integration_mode: str
    stream: bool
    status_code: int
    success: bool
    error_code: str | None = None
    attempt_count: int
    fallback_used: bool
    tool_rounds: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cached_input_tokens: int
    reasoning_tokens: int
    characters: int
    estimated_cost_usd: float
    latency_ms: float
    ttft_ms: float | None = None
    created_at: datetime


class AnalyticsOverviewSummary(BaseModel):
    """Aggregated gateway-wide usage, reliability, latency, and cost metrics."""

    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    success_rate: float = 1.0
    fallback_requests: int = 0
    fallback_rate: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_tokens: int = 0
    total_characters: int = 0
    total_estimated_cost_usd: float = 0.0
    avg_latency_ms: float = 0.0
    avg_ttft_ms: float | None = None


class ProviderUsageBreakdown(BaseModel):
    """Usage and cost breakdown per provider."""

    provider_id: str
    request_count: int = 0
    success_count: int = 0
    error_count: int = 0
    fallback_count: int = 0
    total_tokens: int = 0
    total_characters: int = 0
    estimated_cost_usd: float = 0.0
    avg_latency_ms: float = 0.0


class ModelUsageBreakdown(BaseModel):
    """Usage and cost breakdown per model."""

    model_id: str
    provider_id: str
    request_count: int = 0
    success_count: int = 0
    error_count: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0
    avg_latency_ms: float = 0.0
    avg_ttft_ms: float | None = None


class CredentialUsageBreakdown(BaseModel):
    """Usage breakdown per non-sensitive credential ID."""

    credential_id: str
    provider_id: str
    request_count: int = 0
    success_count: int = 0
    error_count: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0

