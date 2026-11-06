"""Data models for Client Gateway API Keys and System Settings in the Admin API."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import Boolean, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from gateway.core.db import Base, TimestampUUIDMixin


class ClientApiKeyORM(Base, TimestampUUIDMixin):
    """Persistent client API key record (`ugw_live_...`).

    Stores only the SHA-256 key hash and non-sensitive prefix; raw key is returned once at creation.
    """

    __tablename__ = "gateway_client_api_keys"

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    key_prefix: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    key_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="CLIENT")
    allowed_providers_csv: Mapped[str] = mapped_column(Text, nullable=False, default="")
    allowed_tools_csv: Mapped[str] = mapped_column(Text, nullable=False, default="")
    rate_limit_rpm: Mapped[int] = mapped_column(Integer, nullable=False, default=600)
    monthly_budget_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class ControlPlaneEntityORM(Base, TimestampUUIDMixin):
    """Persisted control-plane definitions (custom providers, routing policy overrides, webhook tools)."""

    __tablename__ = "gateway_control_plane_entities"

    entity_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)  # provider, policy, tool, mcp
    entity_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)



class ClientApiKeyCreateRequest(BaseModel):
    """Payload to issue a new Client Gateway API Key."""

    name: str = Field(..., min_length=1, max_length=128)
    role: str = Field(default="CLIENT", pattern=r"^(CLIENT|OPERATOR|READ_ONLY|SUPER_ADMIN)$")
    allowed_providers: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)
    rate_limit_rpm: int = Field(default=600, ge=1, le=100000)
    monthly_budget_usd: float | None = Field(default=None, ge=0.0)


class ClientApiKeyUpdateRequest(BaseModel):
    """Payload to update an existing Client Gateway API Key."""

    name: str | None = Field(default=None, min_length=1, max_length=128)
    role: str | None = Field(default=None, pattern=r"^(CLIENT|OPERATOR|READ_ONLY|SUPER_ADMIN)$")
    allowed_providers: list[str] | None = None
    allowed_tools: list[str] | None = None
    rate_limit_rpm: int | None = Field(default=None, ge=1, le=100000)
    monthly_budget_usd: float | None = Field(default=None, ge=0.0)
    enabled: bool | None = None


class ClientApiKeyView(BaseModel):
    """Redacted public view of a Client Gateway API Key (never exposes full secret after creation)."""

    id: str
    name: str
    key_prefix: str
    role: str
    allowed_providers: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)
    rate_limit_rpm: int
    monthly_budget_usd: float | None = None
    enabled: bool
    created_at: datetime


class ClientApiKeyCreatedResponse(ClientApiKeyView):
    """Returned once upon key creation, containing the raw `api_key` (`ugw_live_...`)."""

    api_key: str


class SystemSettingsView(BaseModel):
    """Sanitized runtime system configuration and status for the System Settings view."""

    app_name: str
    version: str
    environment: str
    api_v1_prefix: str
    database_dialect: str
    redis_backend: str
    default_routing_policy: str
    default_routing_weights: dict[str, float]
    http_timeout_connect_seconds: float
    http_timeout_read_seconds: float
    http_timeout_write_seconds: float
    http_timeout_pool_seconds: float
    encryption_algorithm: str = "AES-256-GCM"
    secret_redaction_enabled: bool = True
    ssrf_protection_enabled: bool = True
    extra_metadata: dict[str, Any] = Field(default_factory=dict)

