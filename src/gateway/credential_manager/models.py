"""Database models, state enums, and Pydantic schemas for multi-key credential management."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
import uuid

from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import JSON, DateTime, ForeignKey, Integer, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column

from gateway.capability_registry.models import QuotaScopeType
from gateway.core.db import Base, TimestampUUIDMixin


class CredentialStatus(StrEnum):
    """Explicit operational states for managed credentials."""

    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"
    INVALID = "INVALID"
    COOLING_DOWN = "COOLING_DOWN"
    TEMPORARILY_UNAVAILABLE = "TEMPORARILY_UNAVAILABLE"
    REQUIRES_ADMIN_ATTENTION = "REQUIRES_ADMIN_ATTENTION"
    REVOKED = "REVOKED"


class SelectionStrategy(StrEnum):
    """Supported multi-credential selection algorithms."""

    ROUND_ROBIN = "ROUND_ROBIN"
    WEIGHTED = "WEIGHTED"
    LEAST_ACTIVE = "LEAST_ACTIVE"
    LEAST_RECENTLY_USED = "LEAST_RECENTLY_USED"
    RATE_LIMIT_AWARE = "RATE_LIMIT_AWARE"


# =============================================================================
# SQLAlchemy 2.0 Persistent Models
# =============================================================================
class AccountGroupRecord(Base, TimestampUUIDMixin):
    """Shared account/organization/workspace quota bucket across sibling credentials."""

    __tablename__ = "account_groups"

    provider_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    workspace_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    external_org_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    max_concurrency: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quota_config: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, nullable=False
    )


class CredentialRecord(Base, TimestampUUIDMixin):
    """Encrypted provider API credential stored in PostgreSQL."""

    __tablename__ = "credentials"

    provider_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    account_group_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("account_groups.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    workspace_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, index=True
    )
    label: Mapped[str] = mapped_column(String(128), nullable=False)
    key_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    masked_preview: Mapped[str] = mapped_column(String(32), nullable=False)
    encrypted_secret: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    encrypted_dek: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    key_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[str] = mapped_column(
        String(40), default=CredentialStatus.ACTIVE.value, nullable=False, index=True
    )
    weight: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    max_concurrency: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quota_config: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, nullable=False
    )
    allowed_models: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    organization_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    project_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    extra_headers: Mapped[dict[str, str]] = mapped_column(
        JSON, default=dict, nullable=False
    )


class CredentialUsageRecord(Base):
    """Persistent credential-level and account-level usage accounting record."""

    __tablename__ = "credential_usage_records"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    credential_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("credentials.id", ondelete="CASCADE"), nullable=False, index=True
    )
    account_group_id: Mapped[uuid.UUID | None] = mapped_column(
        nullable=True, index=True
    )
    workspace_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provider_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    endpoint: Mapped[str | None] = mapped_column(String(128), nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    characters: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, default=200, nullable=False)
    error_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class CredentialAuditLogRecord(Base):
    """Immutable audit log record for administrative credential mutations."""

    __tablename__ = "credential_audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    credential_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    provider_id: Mapped[str] = mapped_column(String(64), nullable=False)
    details_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


# =============================================================================
# Pydantic Schemas (Public Views NEVER include secret material)
# =============================================================================
class QuotaConfigSpec(BaseModel):
    """Quota limits configurable at Credential, Workspace, Account, Model, or Endpoint scope."""

    requests_per_minute: int | None = Field(default=None, ge=1)
    tokens_per_minute: int | None = Field(default=None, ge=1)
    characters_per_minute: int | None = Field(default=None, ge=1)
    model_rpm_limits: dict[str, int] = Field(default_factory=dict)
    endpoint_rpm_limits: dict[str, int] = Field(default_factory=dict)


class AccountGroupCreateRequest(BaseModel):
    """Request payload for creating a shared account/workspace quota group."""

    provider_id: str
    name: str
    workspace_id: str | None = None
    external_org_id: str | None = None
    max_concurrency: int | None = Field(default=None, ge=1, le=10000)
    quota_config: QuotaConfigSpec = Field(default_factory=QuotaConfigSpec)


class AccountGroupPublicView(BaseModel):
    """Public representation of an AccountGroup."""

    id: str
    provider_id: str
    name: str
    workspace_id: str | None = None
    external_org_id: str | None = None
    max_concurrency: int | None = None
    quota_config: QuotaConfigSpec
    created_at: datetime


class CredentialCreateRequest(BaseModel):
    """Request payload for creating a new encrypted provider credential."""

    provider_id: str
    label: str
    secret: SecretStr
    account_group_id: str | None = None
    workspace_id: str | None = None
    weight: int = Field(default=100, ge=1, le=10000)
    max_concurrency: int | None = Field(default=None, ge=1, le=5000)
    quota_config: QuotaConfigSpec = Field(default_factory=QuotaConfigSpec)
    allowed_models: list[str] | None = None
    organization_id: str | None = None
    project_id: str | None = None
    extra_headers: dict[str, str] = Field(default_factory=dict)


class CredentialUpdateRequest(BaseModel):
    """Request payload for updating an existing credential."""

    label: str | None = None
    secret: SecretStr | None = None
    account_group_id: str | None = None
    workspace_id: str | None = None
    weight: int | None = Field(default=None, ge=1, le=10000)
    max_concurrency: int | None = Field(default=None, ge=1, le=5000)
    quota_config: QuotaConfigSpec | None = None
    allowed_models: list[str] | None = None
    organization_id: str | None = None
    project_id: str | None = None
    extra_headers: dict[str, str] | None = None


class CredentialPublicView(BaseModel):
    """Safe, secret-redacted representation of a credential returned by Admin APIs."""

    model_config = ConfigDict(extra="forbid")

    id: str
    provider_id: str
    account_group_id: str | None = None
    workspace_id: str | None = None
    label: str
    key_fingerprint: str
    masked_preview: str
    key_version: int
    status: CredentialStatus
    weight: int
    max_concurrency: int | None = None
    quota_config: QuotaConfigSpec
    allowed_models: list[str] | None = None
    organization_id: str | None = None
    project_id: str | None = None
    created_at: datetime
    updated_at: datetime


class CredentialHealthView(BaseModel):
    """Live health, cooldown, quota, and concurrency telemetry for a credential."""

    credential_id: str
    provider_id: str
    account_group_id: str | None = None
    workspace_id: str | None = None
    persistent_status: CredentialStatus
    effective_status: CredentialStatus
    is_eligible: bool
    active_requests: int = 0
    max_concurrency: int | None = None
    account_active_requests: int = 0
    account_max_concurrency: int | None = None
    cooldown_remaining_seconds: float = 0.0
    cooldown_scope: QuotaScopeType | None = None
    consecutive_failures: int = 0
    failed_auth_events: int = 0
    rate_limit_events: int = 0
    remaining_requests: int | None = None
    remaining_tokens: int | None = None
    last_used_ms: int | None = None
    supported_quota_scopes: list[QuotaScopeType] = Field(default_factory=list)


class CredentialAuditEntryView(BaseModel):
    """Public view of an immutable credential audit log record."""

    id: str
    actor_id: str
    action: str
    credential_id: str
    provider_id: str
    details: dict[str, Any]
    created_at: datetime

