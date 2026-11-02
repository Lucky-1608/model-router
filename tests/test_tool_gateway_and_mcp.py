"""Comprehensive unit and integration tests for Phase 5: Universal Tool Gateway & MCP Integration."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi.testclient import TestClient
import httpx
import pytest
import respx

from gateway.config.settings import GatewaySettings
from gateway.main import create_app
from gateway.tool_gateway.models import NormalizedToolSpec, ToolSourceType


@pytest.fixture
def tool_client() -> TestClient:
    settings = GatewaySettings(
        env="test",
        database_url="sqlite+aiosqlite:///:memory:",
        redis_url="memory://",
        master_encryption_key="test-master-encryption-key-32bytes!",
        admin_bootstrap_token="test-admin-secret-token-1234567890",
    )
    app = create_app(settings_override=settings)
    with TestClient(app) as client:
        yield client


# =============================================================================
# 1. Built-in Tools & Strict JSON Schema Input Validation
# =============================================================================
def test_builtin_tools_and_strict_json_schema_validation(
    tool_client: TestClient,
) -> None:
    list_resp = tool_client.get("/api/v1/tools")
    assert list_resp.status_code == 200
    tool_names = {t["name"] for t in list_resp.json()["tools"]}
    assert "gateway_get_utc_time" in tool_names
    assert "gateway_calculate_sha256" in tool_names
    assert "gateway_json_extract" in tool_names

    # Valid execution of gateway_calculate_sha256
    exec_resp = tool_client.post(
        "/api/v1/tools/gateway_calculate_sha256/execute",
        json={"arguments": {"text": "universal-ai-gateway"}},
    )
    assert exec_resp.status_code == 200
    res_data = exec_resp.json()
    assert res_data["is_error"] is False
    assert res_data["output"]["algorithm"] == "sha256"
    assert len(res_data["output"]["hexdigest"]) == 64

    # Valid execution of gateway_json_extract
    extract_resp = tool_client.post(
        "/api/v1/tools/gateway_json_extract/execute",
        json={
            "arguments": {
                "data": {"user": {"profile": {"tier": "enterprise"}}},
                "path": "user.profile.tier",
            }
        },
    )
    assert extract_resp.status_code == 200
    assert extract_resp.json()["output"]["value"] == "enterprise"

    # Invalid execution: missing required field 'text' -> 422 GW_TOOL_SCHEMA_INVALID
    bad_req1 = tool_client.post(
        "/api/v1/tools/gateway_calculate_sha256/execute",
        json={"arguments": {}},
    )
    assert bad_req1.status_code == 422
    assert bad_req1.json()["error"]["code"] == "GW_TOOL_SCHEMA_INVALID"

    # Invalid execution: unexpected additional property -> 422 GW_TOOL_SCHEMA_INVALID
    bad_req2 = tool_client.post(
        "/api/v1/tools/gateway_get_utc_time/execute",
        json={"arguments": {"format": "iso", "extra_field": 123}},
    )
    assert bad_req2.status_code == 422
    assert bad_req2.json()["error"]["code"] == "GW_TOOL_SCHEMA_INVALID"

    # Invalid execution: invalid enum value -> 422 GW_TOOL_SCHEMA_INVALID
    bad_req3 = tool_client.post(
        "/api/v1/tools/gateway_get_utc_time/execute",
        json={"arguments": {"format": "invalid_epoch_format"}},
    )
    assert bad_req3.status_code == 422
    assert bad_req3.json()["error"]["code"] == "GW_TOOL_SCHEMA_INVALID"


# =============================================================================
# 2. Webhook Tools, SSRF Security Boundary, Permissions, & Timeouts
# =============================================================================
@respx.mock
def test_webhook_tool_ssrf_guard_permissions_redaction_and_timeout(
    tool_client: TestClient,
) -> None:
    # 1. SSRF Guard blocks loopback, private RFC1918, and cloud metadata IPs
    for blocked_url in (
        "http://127.0.0.1:8080/internal",
        "http://localhost:9000/secret",
        "http://169.254.169.254/latest/meta-data/",
        "http://192.168.1.50/admin",
    ):
        ssrf_resp = tool_client.post(
            "/api/v1/tools",
            json={
                "name": "ssrf_test_tool",
                "source_type": "gateway_webhook",
                "webhook_url": blocked_url,
            },
        )
        assert ssrf_resp.status_code == 403
        assert ssrf_resp.json()["error"]["code"] == "GW_TOOL_FORBIDDEN"

    # 2. Register a valid external HTTPS webhook tool with client permissions and secret headers
    webhook_secret = "Bearer wh_secret_token_987654"
    reg_resp = tool_client.post(
        "/api/v1/tools",
        json={
            "name": "lookup_order_status",
            "description": "Looks up order status by order_id.",
            "source_type": "gateway_webhook",
            "webhook_url": "https://orders.example.com/api/lookup",
            "webhook_method": "POST",
            "webhook_headers": {
                "Authorization": webhook_secret,
                "X-Custom-Source": "gateway",
            },
            "input_schema": {
                "type": "object",
                "required": ["order_id"],
                "properties": {
                    "order_id": {"type": "string", "minLength": 3},
                },
                "additionalProperties": False,
            },
            "allowed_clients": ["client-alpha"],
            "timeout_seconds": 5.0,
        },
    )
    assert reg_resp.status_code == 201
    spec_json = reg_resp.json()
    # Verify secret webhook header is redacted in API output
    assert spec_json["webhook_headers"]["Authorization"] == "***REDACTED***"
    assert spec_json["webhook_headers"]["X-Custom-Source"] == "gateway"
    assert webhook_secret not in json.dumps(spec_json)

    # 3. Unauthorized client_id is rejected with 403 GW_TOOL_FORBIDDEN
    unauth_resp = tool_client.post(
        "/api/v1/tools/lookup_order_status/execute",
        json={
            "arguments": {"order_id": "ORD-100"},
            "client_id": "client-unauthorized",
        },
    )
    assert unauth_resp.status_code == 403
    assert unauth_resp.json()["error"]["code"] == "GW_TOOL_FORBIDDEN"

    # 4. Request-level allowed_tools restriction is enforced
    disallowed_resp = tool_client.post(
        "/api/v1/tools/lookup_order_status/execute",
        json={
            "arguments": {"order_id": "ORD-100"},
            "client_id": "client-alpha",
            "allowed_tools": ["gateway_get_utc_time"],
        },
    )
    assert disallowed_resp.status_code == 403
    assert disallowed_resp.json()["error"]["code"] == "GW_TOOL_FORBIDDEN"

    # 5. Authorized client executes webhook tool successfully
    respx.post("https://orders.example.com/api/lookup").mock(
        return_value=httpx.Response(
            200,
            json={"order_id": "ORD-100", "status": "SHIPPED", "eta": "2026-11-05"},
        )
    )
    ok_resp = tool_client.post(
        "/api/v1/tools/lookup_order_status/execute",
        json={
            "arguments": {"order_id": "ORD-100"},
            "client_id": "client-alpha",
            "allowed_tools": ["lookup_order_status"],
        },
    )
    assert ok_resp.status_code == 200
    assert ok_resp.json()["output"]["status"] == "SHIPPED"

    # 6. Register a slow Python tool and verify strict timeout enforcement
    async def _slow_tool(_args: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(0.5)
        return {"ok": True}

    tool_client.app.state.tool_registry.register_python_tool(  # type: ignore[union-attr]
        NormalizedToolSpec(
            name="slow_timeout_tool",
            description="Simulates a hanging tool",
            source_type=ToolSourceType.GATEWAY_BUILTIN,
            timeout_seconds=0.05,
        ),
        _slow_tool,
    )
    timeout_resp = tool_client.post(
        "/api/v1/tools/slow_timeout_tool/execute",
        json={"arguments": {}},
    )
    assert timeout_resp.status_code == 502
    assert timeout_resp.json()["error"]["code"] == "GW_TOOL_EXECUTION_FAILED"


# =============================================================================
# 3. Non-Portability of Provider-Native Tools
# =============================================================================
def test_provider_native_tools_cannot_be_ported_to_other_providers(
    tool_client: TestClient,
) -> None:
    """Verifies OpenAI-native web_search_preview is rejected with 422 GW_UNSUPPORTED_CAPABILITY on Anthropic."""
    resp = tool_client.post(
        "/v1/chat/completions",
        headers={"X-Gateway-Mode": "unified"},
        json={
            "model": "anthropic/claude-3-7-sonnet-20250219",
            "messages": [{"role": "user", "content": "Search the web for news"}],
            "tools": [{"type": "web_search_preview"}],
        },
    )
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["code"] == "GW_UNSUPPORTED_CAPABILITY"
    assert "web_search_preview" in err["message"]
    assert err["gateway_metadata"]["native_provider"] == "openai"


# =============================================================================
# 4. Model Context Protocol (MCP) Integration & Multi-Round Orchestration
# =============================================================================
@respx.mock
def test_mcp_server_lifecycle_discovery_permissions_and_multi_turn_loop(
    tool_client: TestClient,
) -> None:
    # 1. SSRF Guard blocks local MCP server when allow_local_network=False
    bad_mcp = tool_client.post(
        "/api/v1/mcp/servers",
        json={
            "server_id": "local-blocked",
            "name": "Blocked Local MCP",
            "transport_type": "http_jsonrpc",
            "endpoint_url": "http://127.0.0.1:3001/mcp",
            "allow_local_network": False,
        },
    )
    assert bad_mcp.status_code == 403

    # 2. Register an authorized external MCP server with allowed_tools and blocked_tools
    mcp_secret = "Bearer mcp_gh_secret_token_999"
    reg_mcp = tool_client.post(
        "/api/v1/mcp/servers",
        json={
            "server_id": "github",
            "name": "GitHub MCP Server",
            "transport_type": "http_jsonrpc",
            "endpoint_url": "https://mcp.github.example.com/rpc",
            "auth_headers": {"Authorization": mcp_secret},
            "blocked_tools": ["delete_repository"],
        },
    )
    assert reg_mcp.status_code == 201
    status_data = reg_mcp.json()
    assert status_data["redacted_headers"]["Authorization"] == "***REDACTED***"
    assert mcp_secret not in json.dumps(status_data)

    # Mock MCP JSON-RPC endpoint for initialize, tools/list, and tools/call
    def _mcp_rpc_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        method = body.get("method")
        rpc_id = body.get("id")
        if method == "initialize":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "serverInfo": {"name": "github-mcp-server", "version": "1.2.0"},
                        "capabilities": {"tools": {}},
                    },
                },
            )
        if method == "tools/list":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "result": {
                        "tools": [
                            {
                                "name": "search_repositories",
                                "description": "Search GitHub repositories by query.",
                                "inputSchema": {
                                    "type": "object",
                                    "required": ["query"],
                                    "properties": {
                                        "query": {"type": "string", "minLength": 1},
                                        "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                                    },
                                    "additionalProperties": False,
                                },
                            },
                            {
                                "name": "delete_repository",
                                "description": "Deletes a GitHub repository.",
                                "inputSchema": {
                                    "type": "object",
                                    "required": ["repo"],
                                    "properties": {"repo": {"type": "string"}},
                                },
                            },
                        ]
                    },
                },
            )
        if method == "tools/call":
            params = body.get("params", {})
            t_name = params.get("name")
            t_args = params.get("arguments", {})
            if t_name == "search_repositories":
                return httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "id": rpc_id,
                        "result": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": json.dumps(
                                        {
                                            "query": t_args.get("query"),
                                            "items": [
                                                {
                                                    "full_name": "org/universal-ai-gateway",
                                                    "stars": 4200,
                                                }
                                            ],
                                        }
                                    ),
                                }
                            ],
                            "isError": False,
                        },
                    },
                )
        return httpx.Response(
            400,
            json={"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -32601, "message": "Method not found"}},
        )

    respx.post("https://mcp.github.example.com/rpc").mock(side_effect=_mcp_rpc_handler)

    # 3. Sync tools from MCP server and verify blocked_tools ('delete_repository') is excluded
    sync_resp = tool_client.post("/api/v1/mcp/servers/github/sync-tools")
    assert sync_resp.status_code == 200
    sync_data = sync_resp.json()
    assert sync_data["synced_count"] == 1
    assert sync_data["tools"][0]["remote_name"] == "search_repositories"
    assert sync_data["tools"][0]["gateway_tool_name"] == "mcp__github__search_repositories"

    # Attempting to call the blocked tool directly must return 403 GW_TOOL_FORBIDDEN
    blocked_call = tool_client.post(
        "/api/v1/mcp/servers/github/call",
        json={"tool_name": "delete_repository", "arguments": {"repo": "org/repo"}},
    )
    assert blocked_call.status_code == 403
    assert blocked_call.json()["error"]["code"] == "GW_TOOL_FORBIDDEN"

    # Calling allowed tool with invalid schema (limit=0 < minimum 1) returns 422 GW_TOOL_SCHEMA_INVALID
    bad_schema_call = tool_client.post(
        "/api/v1/mcp/servers/github/call",
        json={
            "tool_name": "search_repositories",
            "arguments": {"query": "gateway", "limit": 0},
        },
    )
    assert bad_schema_call.status_code == 422
    assert bad_schema_call.json()["error"]["code"] == "GW_TOOL_SCHEMA_INVALID"

    # 4. Multi-Turn Autonomous Tool Orchestration via /v1/chat/completions (Anthropic model + MCP tool)
    anthropic_calls = 0

    def _anthropic_multi_turn(request: httpx.Request) -> httpx.Response:
        nonlocal anthropic_calls
        anthropic_calls += 1
        req_json = json.loads(request.content.decode("utf-8"))
        if anthropic_calls == 1:
            # Round 1: Model requests MCP tool execution
            return httpx.Response(
                200,
                json={
                    "id": "msg_turn_1",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-3-7-sonnet-20250219",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_mcp_01",
                            "name": "mcp__github__search_repositories",
                            "input": {"query": "universal-ai-gateway", "limit": 5},
                        }
                    ],
                    "stop_reason": "tool_use",
                    "usage": {"input_tokens": 45, "output_tokens": 22},
                },
            )
        # Round 2: Verify the Gateway appended the MCP tool_result block back into messages
        last_msg = req_json["messages"][-1]
        assert last_msg["role"] == "user"
        assert last_msg["content"][0]["type"] == "tool_result"
        assert last_msg["content"][0]["tool_use_id"] == "toolu_mcp_01"
        assert "org/universal-ai-gateway" in last_msg["content"][0]["content"]
        return httpx.Response(
            200,
            json={
                "id": "msg_turn_2",
                "type": "message",
                "role": "assistant",
                "model": "claude-3-7-sonnet-20250219",
                "content": [
                    {
                        "type": "text",
                        "text": "The top repository is org/universal-ai-gateway with 4,200 stars.",
                    }
                ],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 95, "output_tokens": 18},
            },
        )

    respx.post("https://api.anthropic.com/v1/messages").mock(
        side_effect=_anthropic_multi_turn
    )

    chat_resp = tool_client.post(
        "/v1/chat/completions",
        headers={
            "Authorization": "Bearer sk-ant-test-key",
            "X-Gateway-Tool-Mode": "gateway_managed",
            "X-Gateway-Allowed-Tools": "mcp__github__search_repositories",
        },
        json={
            "model": "anthropic/claude-3-7-sonnet-20250219",
            "messages": [
                {
                    "role": "user",
                    "content": "Find the universal-ai-gateway repo on GitHub and tell me its star count.",
                }
            ],
        },
    )
    assert chat_resp.status_code == 200
    assert chat_resp.headers["X-Gateway-Tool-Rounds"] == "1"
    chat_json = chat_resp.json()
    assert (
        "org/universal-ai-gateway"
        in chat_json["choices"][0]["message"]["content"]
    )
    # Token usage is accumulated across both rounds (45+95=140 input, 22+18=40 output)
    assert chat_json["usage"]["prompt_tokens"] == 140
    assert chat_json["usage"]["completion_tokens"] == 40
    assert chat_json["usage"]["total_tokens"] == 180

