"""Canonical internal representations for requests, responses, streams, errors, and telemetry."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from gateway.capability_registry.models import (
    CapabilityFlag,
    CapabilityState,
    ProtocolType,
    QuotaScopeType,
)


class HealthStatus(StrEnum):
    """Operational health status for providers, models, and credentials."""

    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNHEALTHY = "UNHEALTHY"
    UNKNOWN = "UNKNOWN"


class AdapterCredential(BaseModel):
    """In-memory credential object passed to provider adapters. Never exposes secrets in repr/str."""

    model_config = ConfigDict(frozen=True)

    credential_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    provider_id: str
    account_group_id: str | None = None
    secret: SecretStr
    extra_headers: dict[str, str] = Field(default_factory=dict)
    organization_id: str | None = None
    project_id: str | None = None

    def get_secret_value(self) -> str:
        return self.secret.get_secret_value()

    def __repr__(self) -> str:
        return (
            f"AdapterCredential(credential_id={self.credential_id!r}, "
            f"provider_id={self.provider_id!r}, secret='[REDACTED]')"
        )

    def __str__(self) -> str:
        return self.__repr__()


class NormalizedToolDefinition(BaseModel):
    """Provider-neutral representation of a callable function/tool or provider-native tool."""

    name: str
    description: str | None = None
    parameters_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})
    strict: bool = False
    is_provider_native: bool = False
    native_type: str | None = None
    provider_metadata: dict[str, Any] = Field(default_factory=dict)


class NormalizedToolCall(BaseModel):
    """Normalized representation of a model's tool invocation request."""

    call_id: str
    name: str
    arguments_json: str = "{}"
    arguments_dict: dict[str, Any] = Field(default_factory=dict)
    provider_metadata: dict[str, Any] = Field(default_factory=dict)


class NormalizedToolResult(BaseModel):
    """Normalized representation of a tool execution output."""

    call_id: str
    name: str | None = None
    output_text: str
    is_error: bool = False
    provider_metadata: dict[str, Any] = Field(default_factory=dict)


class CanonicalMessagePart(BaseModel):
    """A single content block within a message (text, image, audio, tool_call, tool_result, thinking)."""

    type: Literal[
        "text",
        "image_url",
        "image_base64",
        "audio",
        "tool_call",
        "tool_result",
        "thinking",
    ] = "text"
    text: str | None = None
    media_url: str | None = None
    media_base64: str | None = None
    mime_type: str | None = None
    tool_call: NormalizedToolCall | None = None
    tool_result: NormalizedToolResult | None = None
    provider_metadata: dict[str, Any] = Field(default_factory=dict)


class CanonicalMessage(BaseModel):
    """A single conversational turn in the canonical representation."""

    role: Literal["system", "developer", "user", "assistant", "tool"]
    parts: list[CanonicalMessagePart] = Field(default_factory=list)
    name: str | None = None

    @classmethod
    def from_text(
        cls,
        role: Literal["system", "developer", "user", "assistant", "tool"],
        text: str,
    ) -> "CanonicalMessage":
        return cls(role=role, parts=[CanonicalMessagePart(type="text", text=text)])

    def combined_text(self) -> str:
        return "".join(part.text for part in self.parts if part.type == "text" and part.text)


class ResponseFormatSpec(BaseModel):
    """Normalized response format / structured output specification."""

    type: Literal["text", "json_object", "json_schema"] = "text"
    schema_name: str | None = None
    json_schema: dict[str, Any] | None = None
    strict: bool = False


class CanonicalInternalRequest(BaseModel):
    """Canonical internal request passed through routing to provider adapters."""

    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    source_protocol: ProtocolType = ProtocolType.OPENAI_CHAT
    integration_mode: Literal["unified", "native", "custom"] = "unified"
    requested_model: str
    provider_model_id: str | None = None
    task_hint: str | None = None
    routing_policy_name: str | None = None
    system_instructions: list[str] = Field(default_factory=list)
    messages: list[CanonicalMessage] = Field(default_factory=list)
    input_texts: list[str] = Field(default_factory=list)
    tools: list[NormalizedToolDefinition] = Field(default_factory=list)
    tool_choice: str | dict[str, Any] | None = None
    parallel_tool_calls: bool | None = None
    response_format: ResponseFormatSpec | None = None
    temperature: float | None = None
    top_p: float | None = None
    max_output_tokens: int | None = None
    stop_sequences: list[str] = Field(default_factory=list)
    stream: bool = False
    voice_id: str | None = None
    audio_bytes: bytes | None = None
    audio_filename: str | None = None
    estimated_input_tokens: int = 0
    required_capabilities: set[CapabilityFlag] = Field(default_factory=set)
    provider_extensions: dict[str, Any] = Field(default_factory=dict)


class TokenUsage(BaseModel):
    """Extracted token or character usage statistics from an upstream execution."""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cached_input_tokens: int = 0
    reasoning_tokens: int = 0
    characters: int = 0


class RateLimitSnapshot(BaseModel):
    """Extracted provider rate-limit headers from an upstream response."""

    scope: QuotaScopeType = QuotaScopeType.ACCOUNT
    limit_requests: int | None = None
    remaining_requests: int | None = None
    reset_requests_seconds: float | None = None
    limit_tokens: int | None = None
    remaining_tokens: int | None = None
    reset_tokens_seconds: float | None = None
    retry_after_seconds: float | None = None
    raw_headers: dict[str, str] = Field(default_factory=dict)


class CanonicalInternalResponse(BaseModel):
    """Normalized provider response returned by BaseProviderAdapter.execute()."""

    request_id: str
    provider_id: str
    model: str
    provider_response_id: str | None = None
    output_message: CanonicalMessage = Field(
        default_factory=lambda: CanonicalMessage(role="assistant", parts=[])
    )
    tool_calls: list[NormalizedToolCall] = Field(default_factory=list)
    embeddings: list[list[float]] = Field(default_factory=list)
    audio_content: bytes | None = None
    audio_mime_type: str | None = None
    finish_reason: Literal[
        "stop", "length", "tool_calls", "content_filter", "error", "unknown"
    ] = "stop"
    usage: TokenUsage = Field(default_factory=TokenUsage)
    rate_limit: RateLimitSnapshot | None = None
    latency_ms: float = 0.0
    provider_metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def output_text(self) -> str:
        return self.output_message.combined_text()


class CanonicalStreamEvent(BaseModel):
    """Single normalized streaming chunk yielded by BaseProviderAdapter.execute_stream()."""

    event_type: Literal[
        "start",
        "text_delta",
        "thinking_delta",
        "tool_call_delta",
        "audio_chunk",
        "usage",
        "done",
        "error",
    ]
    text_delta: str | None = None
    thinking_delta: str | None = None
    tool_call_delta: NormalizedToolCall | None = None
    audio_bytes: bytes | None = None
    finish_reason: str | None = None
    usage: TokenUsage | None = None
    provider_response_id: str | None = None
    model: str | None = None
    raw_event: dict[str, Any] = Field(default_factory=dict)


class NativePassthroughRequest(BaseModel):
    """Zero-normalization native request envelope."""

    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    provider_id: str
    method: str = "POST"
    upstream_path: str
    query_params: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    body_bytes: bytes = b""
    parsed_json: dict[str, Any] | None = None
    stream: bool = False


class NativePassthroughResponse(BaseModel):
    """Zero-normalization native response envelope with extracted telemetry."""

    status_code: int
    headers: dict[str, str] = Field(default_factory=dict)
    body_bytes: bytes = b""
    media_type: str = "application/json"
    usage: TokenUsage | None = None
    rate_limit: RateLimitSnapshot | None = None


class ClassifiedProviderError(BaseModel):
    """Normalized classification of an upstream provider HTTP or network failure."""

    provider_id: str
    status_code: int
    error_code: str
    error_type: Literal[
        "rate_limit_error",
        "quota_exhausted_error",
        "authentication_error",
        "permission_error",
        "invalid_request_error",
        "context_length_error",
        "not_found_error",
        "upstream_server_error",
        "timeout_error",
        "connection_error",
    ]
    message: str
    retryable: bool
    quota_scope: QuotaScopeType = QuotaScopeType.CREDENTIAL
    retry_after_seconds: float | None = None
    requires_credential_disable: bool = False
    requires_admin_attention: bool = False


class DiscoveredModel(BaseModel):
    """Model metadata discovered from a provider or registered manifest."""

    provider_id: str
    provider_model_id: str
    canonical_model_id: str
    display_name: str
    supported_protocols: list[ProtocolType] = Field(default_factory=list)
    context_length: int | None = None
    max_output_tokens: int | None = None
    capabilities: dict[CapabilityFlag, CapabilityState] = Field(default_factory=dict)
    input_cost_per_1m_usd: float | None = None
    output_cost_per_1m_usd: float | None = None
    raw_metadata: dict[str, Any] = Field(default_factory=dict)


class HealthCheckResult(BaseModel):
    """Result of a provider or credential health check probe."""

    provider_id: str
    status: HealthStatus
    latency_ms: float = 0.0
    message: str = "OK"
    checked_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    details: dict[str, Any] = Field(default_factory=dict)

