"""Data models for Model Context Protocol (MCP) server integration."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, SecretStr

from gateway.core.canonical import HealthStatus


class MCPTransportType(StrEnum):
    """Supported transport protocols for MCP servers."""

    HTTP_JSONRPC = "http_jsonrpc"
    SSE = "sse"
    IN_MEMORY = "in_memory"


class MCPServerRegistrationRequest(BaseModel):
    """Request payload for registering an MCP server with the Gateway."""

    server_id: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    name: str = Field(..., min_length=1, max_length=128)
    transport_type: MCPTransportType = MCPTransportType.HTTP_JSONRPC
    endpoint_url: str | None = None
    auth_headers: dict[str, SecretStr | str] = Field(default_factory=dict)
    timeout_seconds: float = Field(default=15.0, gt=0.0, le=120.0)
    allowed_tools: list[str] | None = None
    blocked_tools: list[str] = Field(default_factory=list)
    tool_prefix: str | None = None
    allow_local_network: bool = False
    enabled: bool = True


class MCPToolDescriptor(BaseModel):
    """Discovered tool definition from an MCP server."""

    server_id: str
    remote_name: str
    gateway_tool_name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )


class MCPServerStatus(BaseModel):
    """Operational and discovery status of a registered MCP server."""

    server_id: str
    name: str
    transport_type: MCPTransportType
    endpoint_url: str | None = None
    redacted_headers: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: float
    allowed_tools: list[str] | None = None
    blocked_tools: list[str] = Field(default_factory=list)
    tool_prefix: str
    enabled: bool = True
    health_status: HealthStatus = HealthStatus.UNKNOWN
    protocol_version: str | None = None
    server_info: dict[str, Any] = Field(default_factory=dict)
    discovered_tools_count: int = 0
    synced_tool_names: list[str] = Field(default_factory=list)
    last_sync_at: datetime | None = None
    last_error: str | None = None


class MCPToolCallRequest(BaseModel):
    """Request payload for invoking an MCP tool directly via the MCP API."""

    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)

