"""Model Context Protocol (MCP) JSON-RPC 2.0 Client & Server Lifecycle Manager."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
import json
import time
from typing import Any
import uuid

import httpx
from pydantic import SecretStr

from gateway.core.canonical import HealthStatus
from gateway.core.exceptions import ConfigurationError, ResourceNotFoundError
from gateway.core.http import HTTPXClientPoolManager, TimeoutPolicy
from gateway.mcp_integration.models import (
    MCPServerRegistrationRequest,
    MCPServerStatus,
    MCPToolDescriptor,
    MCPTransportType,
)
from gateway.tool_gateway.models import (
    NormalizedToolResult,
    NormalizedToolSpec,
    ToolSourceType,
)
from gateway.tool_gateway.registry import ToolRegistry, validate_outbound_url
from gateway.tool_gateway.validator import (
    ToolExecutionError,
    ToolPermissionError,
    validate_tool_arguments,
)

MCP_PROTOCOL_VERSION = "2024-11-05"
InMemoryMCPHandler = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


class _ManagedMCPServer:
    def __init__(
        self,
        req: MCPServerRegistrationRequest,
        raw_headers: dict[str, str],
        redacted_headers: dict[str, str],
    ) -> None:
        self.server_id = req.server_id
        self.name = req.name
        self.transport_type = req.transport_type
        self.endpoint_url = req.endpoint_url
        self.raw_headers = raw_headers
        self.redacted_headers = redacted_headers
        self.timeout_seconds = req.timeout_seconds
        self.allowed_tools = set(req.allowed_tools) if req.allowed_tools is not None else None
        self.blocked_tools = set(req.blocked_tools)
        self.tool_prefix = (
            req.tool_prefix
            if req.tool_prefix is not None
            else f"mcp__{req.server_id}__"
        )
        self.allow_local_network = req.allow_local_network
        self.enabled = req.enabled
        self.health_status = HealthStatus.UNKNOWN
        self.protocol_version: str | None = None
        self.server_info: dict[str, Any] = {}
        self.discovered_tools: dict[str, MCPToolDescriptor] = {}
        self.last_sync_at: datetime | None = None
        self.last_error: str | None = None
        self.in_memory_handler: InMemoryMCPHandler | None = None

    def to_status(self) -> MCPServerStatus:
        return MCPServerStatus(
            server_id=self.server_id,
            name=self.name,
            transport_type=self.transport_type,
            endpoint_url=self.endpoint_url,
            redacted_headers=self.redacted_headers,
            timeout_seconds=self.timeout_seconds,
            allowed_tools=sorted(self.allowed_tools) if self.allowed_tools is not None else None,
            blocked_tools=sorted(self.blocked_tools),
            tool_prefix=self.tool_prefix,
            enabled=self.enabled,
            health_status=self.health_status,
            protocol_version=self.protocol_version,
            server_info=self.server_info,
            discovered_tools_count=len(self.discovered_tools),
            synced_tool_names=sorted(
                d.gateway_tool_name for d in self.discovered_tools.values()
            ),
            last_sync_at=self.last_sync_at,
            last_error=self.last_error,
        )


class MCPClientManager:
    """Manages MCP server registrations, JSON-RPC 2.0 handshakes, tool discovery, and execution."""

    def __init__(
        self,
        http_pool: HTTPXClientPoolManager,
        tool_registry: ToolRegistry,
    ) -> None:
        self._http_pool = http_pool
        self._tool_registry = tool_registry
        self._servers: dict[str, _ManagedMCPServer] = {}
        self._tool_registry.set_mcp_invoker(self._invoke_from_tool_registry)

    def register_server(
        self,
        req: MCPServerRegistrationRequest,
        *,
        in_memory_handler: InMemoryMCPHandler | None = None,
    ) -> MCPServerStatus:
        """Registers or updates an MCP server after validating transport and SSRF boundaries."""
        if req.transport_type in (MCPTransportType.HTTP_JSONRPC, MCPTransportType.SSE):
            if not req.endpoint_url:
                raise ConfigurationError(
                    f"endpoint_url is required for MCP transport {req.transport_type.value!r}",
                    param="endpoint_url",
                )
            validate_outbound_url(
                req.endpoint_url, allow_local_network=req.allow_local_network
            )
        elif req.transport_type == MCPTransportType.IN_MEMORY and in_memory_handler is None:
            raise ConfigurationError(
                "in_memory_handler is required when transport_type='in_memory'",
                param="transport_type",
            )

        raw_headers: dict[str, str] = {}
        redacted_headers: dict[str, str] = {}
        for k, v in req.auth_headers.items():
            secret_str = v.get_secret_value() if isinstance(v, SecretStr) else str(v)
            raw_headers[k] = secret_str
            redacted_headers[k] = (
                "***REDACTED***"
                if any(
                    s in k.lower()
                    for s in ("authorization", "key", "token", "secret")
                )
                else secret_str
            )

        managed = _ManagedMCPServer(req, raw_headers, redacted_headers)
        managed.in_memory_handler = in_memory_handler
        self._servers[req.server_id] = managed
        return managed.to_status()

    def unregister_server(self, server_id: str) -> None:
        if server_id not in self._servers:
            raise ResourceNotFoundError(
                f"MCP server {server_id!r} is not registered", param="server_id"
            )
        self._tool_registry.remove_mcp_server_tools(server_id)
        self._servers.pop(server_id, None)

    def get_server_status(self, server_id: str) -> MCPServerStatus:
        srv = self._get_server(server_id)
        return srv.to_status()

    def list_servers(self) -> list[MCPServerStatus]:
        return [
            self._servers[sid].to_status() for sid in sorted(self._servers.keys())
        ]

    async def check_server_health(self, server_id: str) -> MCPServerStatus:
        """Executes an MCP JSON-RPC initialize / ping probe and updates server health."""
        srv = self._get_server(server_id)
        try:
            init_res = await self._send_jsonrpc(
                srv,
                method="initialize",
                params={
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {
                        "name": "universal-ai-gateway",
                        "version": "0.1.0",
                    },
                },
            )
            srv.protocol_version = init_res.get("protocolVersion", MCP_PROTOCOL_VERSION)
            srv.server_info = init_res.get("serverInfo", {})
            srv.health_status = HealthStatus.HEALTHY
            srv.last_error = None
        except Exception as exc:
            srv.health_status = HealthStatus.UNHEALTHY
            srv.last_error = str(exc)
        return srv.to_status()

    async def sync_server_tools(
        self, server_id: str
    ) -> list[MCPToolDescriptor]:
        """Performs MCP initialize + tools/list, filters by allow/block lists, and syncs into ToolRegistry."""
        srv = self._get_server(server_id)
        if not srv.enabled:
            raise ToolPermissionError(
                f"MCP server {server_id!r} is disabled", param="server_id"
            )

        try:
            init_res = await self._send_jsonrpc(
                srv,
                method="initialize",
                params={
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {
                        "name": "universal-ai-gateway",
                        "version": "0.1.0",
                    },
                },
            )
            srv.protocol_version = init_res.get("protocolVersion", MCP_PROTOCOL_VERSION)
            srv.server_info = init_res.get("serverInfo", {})

            list_res = await self._send_jsonrpc(srv, method="tools/list", params={})
            raw_tools = list_res.get("tools", [])
        except Exception as exc:
            srv.health_status = HealthStatus.UNHEALTHY
            srv.last_error = str(exc)
            raise ToolExecutionError(
                f"Failed to sync tools from MCP server {server_id!r}: {exc}",
                param="server_id",
            ) from exc

        descriptors: dict[str, MCPToolDescriptor] = {}
        gateway_specs: list[NormalizedToolSpec] = []

        for item in raw_tools:
            if not isinstance(item, dict) or "name" not in item:
                continue
            remote_name = str(item["name"])
            if not self._is_tool_permitted(srv, remote_name):
                continue

            gw_name = f"{srv.tool_prefix}{remote_name}"
            schema = item.get("inputSchema") or {
                "type": "object",
                "properties": {},
            }
            desc = str(item.get("description") or "")

            descriptor = MCPToolDescriptor(
                server_id=srv.server_id,
                remote_name=remote_name,
                gateway_tool_name=gw_name,
                description=desc,
                input_schema=schema,
            )
            descriptors[remote_name] = descriptor

            gateway_specs.append(
                NormalizedToolSpec(
                    name=gw_name,
                    description=desc,
                    input_schema=schema,
                    source_type=ToolSourceType.MCP_SERVER,
                    mcp_server_id=srv.server_id,
                    mcp_remote_tool_name=remote_name,
                    timeout_seconds=srv.timeout_seconds,
                    enabled=srv.enabled,
                )
            )

        srv.discovered_tools = descriptors
        srv.health_status = HealthStatus.HEALTHY
        srv.last_sync_at = datetime.now(timezone.utc)
        srv.last_error = None

        self._tool_registry.sync_mcp_tools(srv.server_id, gateway_specs)
        return list(descriptors.values())

    async def call_mcp_tool(
        self,
        server_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        timeout_override: float | None = None,
    ) -> NormalizedToolResult:
        """Validates permissions and JSON Schema, invokes tools/call on the MCP server, and normalizes output."""
        srv = self._get_server(server_id)
        if not srv.enabled:
            raise ToolPermissionError(
                f"MCP server {server_id!r} is disabled", param="server_id"
            )

        # Allow caller to pass either remote_name ('search_repos') or prefixed name ('mcp__github__search_repos')
        remote_name = tool_name
        if tool_name.startswith(srv.tool_prefix):
            remote_name = tool_name[len(srv.tool_prefix) :]

        if not self._is_tool_permitted(srv, remote_name):
            raise ToolPermissionError(
                f"Tool {remote_name!r} is blocked or not permitted on MCP server {server_id!r}",
                param="tool_name",
            )

        descriptor = srv.discovered_tools.get(remote_name)
        if descriptor is not None:
            validate_tool_arguments(
                descriptor.gateway_tool_name, arguments, descriptor.input_schema
            )

        start = time.perf_counter()
        result_payload = await self._send_jsonrpc(
            srv,
            method="tools/call",
            params={"name": remote_name, "arguments": arguments},
            timeout_override=timeout_override,
        )
        elapsed_ms = round((time.perf_counter() - start) * 1000.0, 2)

        is_error = bool(result_payload.get("isError", False))
        content_items = result_payload.get("content", [])
        extracted_output = self._extract_mcp_content(content_items, result_payload)

        gw_name = (
            descriptor.gateway_tool_name
            if descriptor is not None
            else f"{srv.tool_prefix}{remote_name}"
        )
        return NormalizedToolResult(
            call_id=f"mcp_{uuid.uuid4().hex[:12]}",
            tool_name=gw_name,
            output=extracted_output,
            is_error=is_error,
            execution_time_ms=elapsed_ms,
            source_type=ToolSourceType.MCP_SERVER,
        )

    async def _invoke_from_tool_registry(
        self,
        server_id: str,
        remote_tool_name: str,
        arguments: dict[str, Any],
        timeout_seconds: float,
    ) -> NormalizedToolResult:
        return await self.call_mcp_tool(
            server_id,
            remote_tool_name,
            arguments,
            timeout_override=timeout_seconds,
        )

    def _is_tool_permitted(self, srv: _ManagedMCPServer, remote_name: str) -> bool:
        if remote_name in srv.blocked_tools:
            return False
        if srv.allowed_tools is not None and remote_name not in srv.allowed_tools:
            return False
        return True

    def _get_server(self, server_id: str) -> _ManagedMCPServer:
        srv = self._servers.get(server_id)
        if srv is None:
            raise ResourceNotFoundError(
                f"MCP server {server_id!r} is not registered", param="server_id"
            )
        return srv

    async def _send_jsonrpc(
        self,
        srv: _ManagedMCPServer,
        *,
        method: str,
        params: dict[str, Any],
        timeout_override: float | None = None,
    ) -> dict[str, Any]:
        rpc_id = str(uuid.uuid4())
        rpc_body = {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "method": method,
            "params": params,
        }

        if srv.transport_type == MCPTransportType.IN_MEMORY:
            assert srv.in_memory_handler is not None
            raw_resp = await srv.in_memory_handler(method, params)
            return self._unwrap_jsonrpc_response(srv.server_id, method, raw_resp)

        assert srv.endpoint_url is not None
        validate_outbound_url(
            srv.endpoint_url, allow_local_network=srv.allow_local_network
        )
        timeout_sec = timeout_override or srv.timeout_seconds
        client = await self._http_pool.get_client(
            f"mcp:{srv.server_id}",
            timeout_policy=TimeoutPolicy(
                connect=min(5.0, timeout_sec),
                read=timeout_sec,
                write=min(5.0, timeout_sec),
                pool=2.0,
            ),
        )
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        headers.update(srv.raw_headers)

        try:
            resp = await client.post(
                srv.endpoint_url,
                json=rpc_body,
                headers=headers,
            )
        except httpx.TimeoutException as exc:
            raise ToolExecutionError(
                f"MCP server {srv.server_id!r} timed out on {method!r} after {timeout_sec}s",
                param=srv.server_id,
            ) from exc
        except httpx.HTTPError as exc:
            raise ToolExecutionError(
                f"MCP server {srv.server_id!r} transport error on {method!r}: {exc}",
                param=srv.server_id,
            ) from exc

        if resp.status_code >= 400:
            raise ToolExecutionError(
                f"MCP server {srv.server_id!r} returned HTTP {resp.status_code} on {method!r}",
                param=srv.server_id,
                details={"status_code": resp.status_code},
            )

        content_type = resp.headers.get("content-type", "").lower()
        if "text/event-stream" in content_type:
            # Parse SSE data line containing the JSON-RPC response
            data = self._parse_sse_jsonrpc(resp.text)
        else:
            data = resp.json()

        return self._unwrap_jsonrpc_response(srv.server_id, method, data)

    def _parse_sse_jsonrpc(self, sse_text: str) -> dict[str, Any]:
        for line in sse_text.splitlines():
            stripped = line.strip()
            if stripped.startswith("data:"):
                payload_str = stripped[5:].strip()
                if payload_str:
                    return json.loads(payload_str)
        raise ToolExecutionError("MCP SSE response did not contain a valid JSON-RPC data frame")

    def _unwrap_jsonrpc_response(
        self,
        server_id: str,
        method: str,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        if "error" in data and data["error"] is not None:
            err_obj = data["error"]
            msg = (
                err_obj.get("message", str(err_obj))
                if isinstance(err_obj, dict)
                else str(err_obj)
            )
            code = err_obj.get("code") if isinstance(err_obj, dict) else None
            raise ToolExecutionError(
                f"MCP server {server_id!r} JSON-RPC error on {method!r} (code={code}): {msg}",
                param=server_id,
            )
        res = data.get("result")
        return res if isinstance(res, dict) else {"value": res}

    def _extract_mcp_content(
        self,
        content_items: Any,
        raw_result: dict[str, Any],
    ) -> Any:
        if not isinstance(content_items, list) or not content_items:
            return raw_result
        texts: list[str] = []
        for item in content_items:
            if isinstance(item, dict) and item.get("type") == "text":
                texts.append(str(item.get("text", "")))
        if len(texts) == 1:
            try:
                return json.loads(texts[0])
            except Exception:
                return texts[0]
        if texts:
            return "\n".join(texts)
        return content_items
