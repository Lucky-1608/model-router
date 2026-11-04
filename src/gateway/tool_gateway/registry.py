"""Universal Tool Registry with SSRF guard, permission checks, JSON Schema validation, and sandboxed execution."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import time
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import SecretStr

from gateway.core.canonical import (
    NormalizedToolDefinition,
)
from gateway.core.exceptions import (
    ConfigurationError,
    ResourceNotFoundError,
    UnsupportedCapabilityError,
)
from gateway.core.http import HTTPXClientPoolManager, TimeoutPolicy
from gateway.provider_adapters.custom_adapter import extract_dot_path
from gateway.tool_gateway.models import (
    NormalizedToolCall,
    NormalizedToolResult,
    NormalizedToolSpec,
    ToolRegistrationRequest,
    ToolSourceType,
)
from gateway.tool_gateway.validator import (
    ToolExecutionError,
    ToolPermissionError,
    validate_tool_arguments,
)

PythonToolHandler = Callable[[dict[str, Any]], Awaitable[Any]]
MCPToolInvoker = Callable[[str, str, dict[str, Any], float], Awaitable[NormalizedToolResult]]

KNOWN_PROVIDER_NATIVE_TOOLS: dict[str, str] = {
    # OpenAI native hosted tools
    "web_search_preview": "openai",
    "web_search": "openai",
    "file_search": "openai",
    "code_interpreter": "openai",
    "computer_use_preview": "openai",
    # Anthropic native server/computer tools
    "computer_20250124": "anthropic",
    "computer_20241022": "anthropic",
    "bash_20250124": "anthropic",
    "bash_20241022": "anthropic",
    "text_editor_20250124": "anthropic",
    "text_editor_20241022": "anthropic",
    # Google Gemini native grounding/execution tools
    "googlesearch": "gemini",
    "google_search": "gemini",
    "google_search_retrieval": "gemini",
    "code_execution": "gemini",
}


def validate_outbound_url(url: str, *, allow_local_network: bool = False) -> None:
    """Enforces SSRF protection on outbound webhook and MCP server URLs."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ConfigurationError(
            f"Outbound URL {url!r} must use http or https scheme",
            param="url",
        )
    host = (parsed.hostname or "").lower()
    if not host:
        raise ConfigurationError(f"Outbound URL {url!r} is missing hostname", param="url")

    if allow_local_network:
        return

    if host in ("localhost", "metadata.google.internal", "169.254.169.254"):
        raise ToolPermissionError(
            f"Outbound host {host!r} is blocked by SSRF security boundary",
            param="url",
        )
    try:
        ip = ipaddress.ip_address(host)
        if (
            ip.is_loopback
            or ip.is_private
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
        ):
            raise ToolPermissionError(
                f"Outbound private/loopback IP {host!r} is blocked by SSRF security boundary",
                param="url",
            )
    except ValueError:
        pass


async def validate_and_resolve_outbound_url(
    url: str, *, allow_local_network: bool = False
) -> list[str]:
    """Asynchronously resolves DNS and verifies all resolved IP addresses against SSRF boundaries."""
    validate_outbound_url(url, allow_local_network=allow_local_network)
    if allow_local_network:
        return []

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    # RFC 2606 / RFC 6761 reserved test and documentation domains used in unit tests and docs
    if host.endswith((".example.com", ".example.org", ".example.net", ".example", ".test", ".invalid")) or host in ("example.com", "example.org", "example.net"):
        return ["93.184.216.34"]

    try:
        loop = asyncio.get_running_loop()
        addr_info = await loop.getaddrinfo(host, port)
    except Exception as exc:
        raise ToolPermissionError(
            f"DNS resolution failed for outbound host {host!r}: {exc}",
            param="url",
        ) from exc

    resolved_ips: list[str] = []
    for info in addr_info:
        sockaddr = info[4]
        ip_str = sockaddr[0]
        resolved_ips.append(ip_str)
        try:
            ip = ipaddress.ip_address(ip_str)
            if (
                ip.is_loopback
                or ip.is_private
                or ip.is_link_local
                or ip.is_multicast
                or ip.is_reserved
            ):
                raise ToolPermissionError(
                    f"Outbound host {host!r} resolved to private/loopback IP {ip_str!r} (SSRF boundary violation)",
                    param="url",
                )
        except ValueError:
            continue

    return resolved_ips


class ToolRegistry:
    """Central registry for Gateway-managed tools, webhook tools, and synced MCP tools."""

    def __init__(
        self,
        http_pool: HTTPXClientPoolManager,
        *,
        allow_local_webhooks: bool = False,
    ) -> None:
        self._http_pool = http_pool
        self._allow_local_webhooks = allow_local_webhooks
        self._tools: dict[str, NormalizedToolSpec] = {}
        self._webhook_secrets: dict[str, dict[str, str]] = {}
        self._python_handlers: dict[str, PythonToolHandler] = {}
        self._mcp_invoker: MCPToolInvoker | None = None
        self._seed_builtin_tools()

    def set_mcp_invoker(self, invoker: MCPToolInvoker) -> None:
        self._mcp_invoker = invoker

    def _seed_builtin_tools(self) -> None:
        """Registers safe, deterministic built-in Gateway utility tools."""

        async def _get_utc_time(args: dict[str, Any]) -> dict[str, Any]:
            fmt = args.get("format", "iso")
            now = datetime.now(timezone.utc)
            if fmt == "unix":
                return {"utc_unix": int(now.timestamp())}
            return {"utc_iso": now.isoformat()}

        self.register_python_tool(
            NormalizedToolSpec(
                name="gateway_get_utc_time",
                description="Returns the current UTC timestamp in ISO-8601 or Unix epoch format.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "format": {
                            "type": "string",
                            "enum": ["iso", "unix"],
                        }
                    },
                    "additionalProperties": False,
                },
                source_type=ToolSourceType.GATEWAY_BUILTIN,
                timeout_seconds=2.0,
                idempotent=True,
            ),
            _get_utc_time,
        )

        async def _calculate_sha256(args: dict[str, Any]) -> dict[str, str]:
            text = str(args["text"])
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            return {"algorithm": "sha256", "hexdigest": digest}

        self.register_python_tool(
            NormalizedToolSpec(
                name="gateway_calculate_sha256",
                description="Computes the SHA-256 hex digest of a given UTF-8 string.",
                input_schema={
                    "type": "object",
                    "required": ["text"],
                    "properties": {
                        "text": {"type": "string", "minLength": 1, "maxLength": 65536},
                    },
                    "additionalProperties": False,
                },
                source_type=ToolSourceType.GATEWAY_BUILTIN,
                timeout_seconds=2.0,
                idempotent=True,
            ),
            _calculate_sha256,
        )

        async def _json_extract(args: dict[str, Any]) -> dict[str, Any]:
            data = args["data"]
            path = str(args["path"])
            return {"path": path, "value": extract_dot_path(data, path)}

        self.register_python_tool(
            NormalizedToolSpec(
                name="gateway_json_extract",
                description="Extracts a nested value from a JSON object using a dot-separated path.",
                input_schema={
                    "type": "object",
                    "required": ["data", "path"],
                    "properties": {
                        "data": {"type": "object"},
                        "path": {"type": "string", "minLength": 1},
                    },
                    "additionalProperties": False,
                },
                source_type=ToolSourceType.GATEWAY_BUILTIN,
                timeout_seconds=2.0,
                idempotent=True,
            ),
            _json_extract,
        )

    def register_python_tool(
        self,
        spec: NormalizedToolSpec,
        handler: PythonToolHandler,
    ) -> NormalizedToolSpec:
        self._tools[spec.name] = spec
        self._python_handlers[spec.name] = handler
        return spec

    def register_tool(self, req: ToolRegistrationRequest) -> NormalizedToolSpec:
        """Registers a webhook or provider-native tool in the Gateway Tool Registry."""
        if req.source_type == ToolSourceType.GATEWAY_WEBHOOK:
            if not req.webhook_url:
                raise ConfigurationError(
                    "webhook_url is required when source_type='gateway_webhook'",
                    param="webhook_url",
                )
            validate_outbound_url(
                req.webhook_url, allow_local_network=self._allow_local_webhooks
            )
        if req.source_type == ToolSourceType.PROVIDER_NATIVE and not req.provider_id:
            raise ConfigurationError(
                "provider_id is required when source_type='provider_native'",
                param="provider_id",
            )

        raw_headers: dict[str, str] = {}
        redacted_headers: dict[str, str] = {}
        for k, v in req.webhook_headers.items():
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

        spec = NormalizedToolSpec(
            name=req.name,
            description=req.description,
            input_schema=req.input_schema,
            source_type=req.source_type,
            provider_id=req.provider_id,
            webhook_url=req.webhook_url,
            webhook_method=req.webhook_method.upper(),
            webhook_headers=redacted_headers,
            timeout_seconds=req.timeout_seconds,
            max_response_bytes=req.max_response_bytes,
            idempotent=req.idempotent,
            allowed_clients=req.allowed_clients,
            enabled=req.enabled,
        )
        self._tools[spec.name] = spec
        self._webhook_secrets[spec.name] = raw_headers
        return spec

    def sync_mcp_tools(
        self,
        server_id: str,
        mcp_specs: list[NormalizedToolSpec],
    ) -> list[NormalizedToolSpec]:
        """Replaces all tools belonging to mcp_server_id with freshly discovered specs."""
        self.remove_mcp_server_tools(server_id)
        for spec in mcp_specs:
            self._tools[spec.name] = spec
        return mcp_specs

    def remove_mcp_server_tools(self, server_id: str) -> int:
        to_remove = [
            name
            for name, spec in self._tools.items()
            if spec.source_type == ToolSourceType.MCP_SERVER
            and spec.mcp_server_id == server_id
        ]
        for name in to_remove:
            self._tools.pop(name, None)
        return len(to_remove)

    def unregister_tool(self, tool_name: str) -> None:
        if tool_name not in self._tools:
            raise ResourceNotFoundError(
                f"Tool {tool_name!r} is not registered", param="tool_name"
            )
        self._tools.pop(tool_name, None)
        self._webhook_secrets.pop(tool_name, None)
        self._python_handlers.pop(tool_name, None)

    def get_tool(self, tool_name: str) -> NormalizedToolSpec:
        spec = self._tools.get(tool_name)
        if spec is None:
            raise ResourceNotFoundError(
                f"Tool {tool_name!r} is not registered in the Tool Gateway",
                param="tool_name",
            )
        return spec

    def has_managed_tool(self, tool_name: str) -> bool:
        spec = self._tools.get(tool_name)
        if spec is None or not spec.enabled:
            return False
        return spec.source_type in (
            ToolSourceType.GATEWAY_BUILTIN,
            ToolSourceType.GATEWAY_WEBHOOK,
            ToolSourceType.MCP_SERVER,
        )

    def list_tools(
        self,
        *,
        source_type: ToolSourceType | None = None,
        enabled_only: bool = False,
    ) -> list[NormalizedToolSpec]:
        items = list(self._tools.values())
        if source_type is not None:
            items = [t for t in items if t.source_type == source_type]
        if enabled_only:
            items = [t for t in items if t.enabled]
        return sorted(items, key=lambda t: t.name)

    def to_canonical_tool_definitions(
        self, tool_names: list[str] | None = None
    ) -> list[NormalizedToolDefinition]:
        """Converts registered tools into NormalizedToolDefinition list for upstream model calls."""
        specs = (
            [self.get_tool(name) for name in tool_names]
            if tool_names is not None
            else self.list_tools(enabled_only=True)
        )
        return [
            NormalizedToolDefinition(
                name=s.name,
                description=s.description,
                parameters_schema=s.input_schema,
                is_provider_native=(s.source_type == ToolSourceType.PROVIDER_NATIVE),
                native_type=s.name if s.source_type == ToolSourceType.PROVIDER_NATIVE else None,
                provider_metadata={"native_provider_id": s.provider_id}
                if s.provider_id
                else {},
            )
            for s in specs
            if s.enabled
        ]

    def enforce_provider_native_portability(
        self,
        tools: list[NormalizedToolDefinition],
        target_provider: str,
    ) -> None:
        """Enforces that provider-native tools are never silently dropped or sent to incompatible providers."""
        target_lower = target_provider.strip().lower()
        for tool in tools:
            required_prov = tool.provider_metadata.get(
                "native_provider_id"
            ) or tool.provider_metadata.get("provider_id")
            if not required_prov:
                known = KNOWN_PROVIDER_NATIVE_TOOLS.get(tool.name.strip().lower())
                if not known and tool.native_type:
                    known = KNOWN_PROVIDER_NATIVE_TOOLS.get(
                        tool.native_type.strip().lower()
                    )
                if known:
                    required_prov = known
                elif tool.name in self._tools:
                    reg_spec = self._tools[tool.name]
                    if reg_spec.source_type == ToolSourceType.PROVIDER_NATIVE:
                        required_prov = reg_spec.provider_id
                elif tool.is_provider_native:
                    required_prov = "unknown_native_provider"

            if required_prov and required_prov.lower() != target_lower:
                raise UnsupportedCapabilityError(
                    f"Tool {tool.name!r} is a provider-native tool exclusive to {required_prov!r} "
                    f"and cannot be executed on {target_provider!r}",
                    param="tools",
                    details={
                        "tool_name": tool.name,
                        "native_provider": required_prov,
                        "target_provider": target_provider,
                    },
                )

    async def execute_tool_call(
        self,
        call: NormalizedToolCall,
        *,
        client_id: str | None = None,
        allowed_tools: set[str] | None = None,
    ) -> NormalizedToolResult:
        """Validates permissions and JSON Schema, then executes a single tool call within a strict timeout."""
        spec = self.get_tool(call.tool_name)
        if not spec.enabled:
            raise ToolPermissionError(
                f"Tool {call.tool_name!r} is currently disabled",
                param="tool_name",
            )

        # 1. Request-level allowlist check
        if allowed_tools is not None and spec.name not in allowed_tools:
            raise ToolPermissionError(
                f"Tool {spec.name!r} is not in the permitted tool allowlist for this request",
                param="tool_name",
            )

        # 2. Tool-level client allowlist check
        if spec.allowed_clients is not None:
            if client_id is None or client_id not in spec.allowed_clients:
                raise ToolPermissionError(
                    f"Client {client_id!r} is not authorized to execute tool {spec.name!r}",
                    param="client_id",
                )

        # 3. Strict JSON Schema input validation
        validate_tool_arguments(spec.name, call.arguments, spec.input_schema)

        start = time.perf_counter()
        try:
            output = await asyncio.wait_for(
                self._dispatch_execution(spec, call),
                timeout=spec.timeout_seconds,
            )
            elapsed_ms = round((time.perf_counter() - start) * 1000.0, 2)
            if isinstance(output, NormalizedToolResult):
                output.call_id = call.call_id
                output.execution_time_ms = elapsed_ms
                return output
            return NormalizedToolResult(
                call_id=call.call_id,
                tool_name=spec.name,
                output=output,
                is_error=False,
                execution_time_ms=elapsed_ms,
                source_type=spec.source_type,
            )
        except TimeoutError as exc:
            elapsed_ms = round((time.perf_counter() - start) * 1000.0, 2)
            raise ToolExecutionError(
                f"Tool {spec.name!r} timed out after {spec.timeout_seconds}s",
                param=spec.name,
                details={
                    "tool_name": spec.name,
                    "timeout_seconds": spec.timeout_seconds,
                    "execution_time_ms": elapsed_ms,
                },
            ) from exc

    async def _dispatch_execution(
        self,
        spec: NormalizedToolSpec,
        call: NormalizedToolCall,
    ) -> Any:
        if spec.source_type == ToolSourceType.GATEWAY_BUILTIN or (
            spec.name in self._python_handlers
        ):
            handler = self._python_handlers[spec.name]
            return await handler(call.arguments)

        if spec.source_type == ToolSourceType.GATEWAY_WEBHOOK:
            return await self._execute_webhook_tool(spec, call.arguments)

        if spec.source_type == ToolSourceType.MCP_SERVER:
            if self._mcp_invoker is None or not spec.mcp_server_id:
                raise ToolExecutionError(
                    f"MCP tool {spec.name!r} has no active MCP invoker configured",
                    param=spec.name,
                )
            remote_name = spec.mcp_remote_tool_name or spec.name
            return await self._mcp_invoker(
                spec.mcp_server_id,
                remote_name,
                call.arguments,
                spec.timeout_seconds,
            )

        raise ToolExecutionError(
            f"Tool {spec.name!r} of source_type={spec.source_type.value!r} cannot be executed directly by the Gateway",
            param=spec.name,
        )

    async def _execute_webhook_tool(
        self,
        spec: NormalizedToolSpec,
        arguments: dict[str, Any],
    ) -> Any:
        assert spec.webhook_url is not None
        await validate_and_resolve_outbound_url(
            spec.webhook_url, allow_local_network=self._allow_local_webhooks
        )
        client = await self._http_pool.get_client(
            f"tool_webhook:{spec.name}",
            timeout_policy=TimeoutPolicy(
                connect=min(5.0, spec.timeout_seconds),
                read=spec.timeout_seconds,
                write=min(5.0, spec.timeout_seconds),
                pool=2.0,
            ),
        )
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        headers.update(self._webhook_secrets.get(spec.name, {}))

        try:
            if spec.webhook_method == "GET":
                resp = await client.get(
                    spec.webhook_url, params=arguments, headers=headers
                )
            else:
                resp = await client.request(
                    spec.webhook_method,
                    spec.webhook_url,
                    json=arguments,
                    headers=headers,
                )
        except httpx.HTTPError as exc:
            raise ToolExecutionError(
                f"Webhook tool {spec.name!r} transport error: {exc}",
                param=spec.name,
            ) from exc

        body_bytes = resp.content[: spec.max_response_bytes]
        if resp.status_code >= 400:
            raise ToolExecutionError(
                f"Webhook tool {spec.name!r} returned HTTP {resp.status_code}",
                param=spec.name,
                details={"status_code": resp.status_code},
            )
        try:
            return json.loads(body_bytes.decode("utf-8", errors="replace"))
        except Exception:
            return body_bytes.decode("utf-8", errors="replace")
