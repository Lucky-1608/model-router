"""Data models for the Universal Tool Gateway."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
import uuid

from pydantic import BaseModel, Field, SecretStr


class ToolSourceType(StrEnum):
    """Origin and execution boundary of a tool definition."""

    CLIENT_PASSTHROUGH = "client_passthrough"
    GATEWAY_BUILTIN = "gateway_builtin"
    GATEWAY_WEBHOOK = "gateway_webhook"
    MCP_SERVER = "mcp_server"
    PROVIDER_NATIVE = "provider_native"


class ToolExecutionMode(StrEnum):
    """Determines whether the gateway returns tool_calls to client or executes them in a loop."""

    PASSTHROUGH = "passthrough"
    GATEWAY_MANAGED = "gateway_managed"


class NormalizedToolSpec(BaseModel):
    """Canonical specification of a tool registered in or passing through the Gateway."""

    name: str = Field(..., min_length=1, max_length=128)
    description: str = ""
    input_schema: dict[str, Any] = Field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )
    source_type: ToolSourceType = ToolSourceType.CLIENT_PASSTHROUGH
    provider_id: str | None = None
    mcp_server_id: str | None = None
    mcp_remote_tool_name: str | None = None
    webhook_url: str | None = None
    webhook_method: str = "POST"
    webhook_headers: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: float = Field(default=10.0, gt=0.0, le=120.0)
    max_response_bytes: int = Field(default=262_144, ge=1024, le=5_242_880)
    idempotent: bool = True
    allowed_clients: list[str] | None = None
    enabled: bool = True
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ToolRegistrationRequest(BaseModel):
    """Request payload for registering a Gateway-managed tool."""

    name: str = Field(..., min_length=1, max_length=128)
    description: str = ""
    input_schema: dict[str, Any] = Field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )
    source_type: ToolSourceType = ToolSourceType.GATEWAY_WEBHOOK
    provider_id: str | None = None
    webhook_url: str | None = None
    webhook_method: str = "POST"
    webhook_headers: dict[str, SecretStr | str] = Field(default_factory=dict)
    timeout_seconds: float = Field(default=10.0, gt=0.0, le=120.0)
    max_response_bytes: int = Field(default=262_144, ge=1024, le=5_242_880)
    idempotent: bool = True
    allowed_clients: list[str] | None = None
    enabled: bool = True


class NormalizedToolCall(BaseModel):
    """Normalized representation of a model's tool invocation request."""

    call_id: str = Field(default_factory=lambda: f"call_{uuid.uuid4().hex[:16]}")
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    raw_arguments: str | None = None
    source_type: ToolSourceType = ToolSourceType.CLIENT_PASSTHROUGH


class NormalizedToolResult(BaseModel):
    """Normalized representation of a completed tool execution."""

    call_id: str
    tool_name: str
    output: Any
    is_error: bool = False
    execution_time_ms: float = 0.0
    source_type: ToolSourceType = ToolSourceType.GATEWAY_BUILTIN

    def serialized_output(self) -> str:
        """Returns stringified output suitable for appending as a tool message."""
        import json

        if isinstance(self.output, str):
            return self.output
        try:
            return json.dumps(self.output, ensure_ascii=False)
        except Exception:
            return str(self.output)


class ToolExecutionStepTrace(BaseModel):
    """Audit/observability record for a single gateway-managed tool invocation."""

    round_index: int
    call_id: str
    tool_name: str
    source_type: ToolSourceType
    is_error: bool
    execution_time_ms: float
    error_message: str | None = None


class DirectToolExecutionRequest(BaseModel):
    """Request payload for directly testing/invoking a registered tool via the Tool Gateway API."""

    arguments: dict[str, Any] = Field(default_factory=dict)
    client_id: str | None = None
    allowed_tools: list[str] | None = None

