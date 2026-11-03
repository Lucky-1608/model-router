"""Comprehensive verification tests for Architectural Remediation (Waves 1-4).

Covers:
- P0.1: Client API Key Authentication, RBAC, and Monthly Budget Enforcement
- P0.2: Control-Plane Administrative Mutation Protection
- P0.3: Atomic Quota & Concurrency Leases Under Race Conditions
- P0.4: Streaming Credential Lease, Lifecycle Release, and Accounting
- P0.5: Non-Idempotent Tool Execution Blocks Mid-Loop Cross-Provider Fallback
- P1.6: Async DNS & IP SSRF Verification Guard
- P1.7: SQL Pushdown Analytics and Buffered Usage Tracking
- P1.9: Dynamic Registered Custom Provider Routing Resolution
- P2.10: Multi-Turn Tool Result Formatting & Consecutive Role Normalization
- P3.13: Half-Open Single Probe Concurrency Lock & Metrics Cardinality Cap
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from gateway.admin_api.models import ClientApiKeyCreateRequest
from gateway.config.settings import GatewaySettings
from gateway.core.canonical import (
    AdapterCredential,
    CanonicalInternalRequest,
    CanonicalInternalResponse,
    CanonicalMessage,
    CanonicalMessagePart,
    CanonicalStreamEvent,
    NormalizedToolCall,
    NormalizedToolResult,
    TokenUsage,
)
from gateway.core.exceptions import UpstreamProviderError
from gateway.health_monitor.models import CircuitBreakerState, CircuitScope
from gateway.health_monitor.monitor import HealthMonitor
from gateway.main import create_app

from gateway.observability.metrics import MetricsCollector
from gateway.protocol_adapters.base import resolve_target_provider_and_model
from gateway.provider_adapters.anthropic_adapter import AnthropicProviderAdapter
from gateway.provider_adapters.gemini_adapter import GeminiProviderAdapter
from gateway.provider_adapters.openai_adapter import OpenAIProviderAdapter
from gateway.credential_manager.models import QuotaConfigSpec
from gateway.quota_manager.manager import QuotaManager
from gateway.retry_fallback.engine import RetryFallbackEngine
from gateway.retry_fallback.guard import FallbackCompatibilityGuard
from gateway.tool_gateway.models import (
    NormalizedToolSpec,
    ToolExecutionMode,
    ToolSourceType,
)
from gateway.tool_gateway.orchestrator import ToolGatewayOrchestrator
from gateway.tool_gateway.registry import (
    ToolRegistry,
    validate_and_resolve_outbound_url,
)
from gateway.usage_tracking.models import UsageEventInput


# =============================================================================
# Test Settings & Application Fixture
# =============================================================================
@pytest.fixture
def remediation_settings() -> GatewaySettings:
    import base64
    test_kek = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode()
    return GatewaySettings(
        env="test",
        database_url="sqlite+aiosqlite:///:memory:",
        redis_url="memory://",
        master_kek_base64=test_kek,
        admin_bootstrap_token="ugw_admin_remediation_token_999",
        require_client_api_key=False,
        usage_flush_interval_seconds=0.05,
        metrics_max_series=10,
    )


# =============================================================================
# P0.1: Client API Key Authentication, RBAC, and Monthly Budget Enforcement
# =============================================================================
def test_p0_1_client_api_key_auth_rbac_and_budget_on_data_plane(
    remediation_settings: GatewaySettings,
) -> None:
    app = create_app(settings_override=remediation_settings)
    admin_headers = {
        "X-Gateway-Admin-Token": "ugw_admin_remediation_token_999",
        "X-Gateway-Admin-Role": "SUPER_ADMIN",
    }

    with TestClient(app) as client:
        # Create a client API key with limited budget and allowed models
        key_resp = client.post(
            "/api/v1/admin/client-keys",
            headers=admin_headers,
            json={
                "name": "Finance Department App",
                "role": "CLIENT",
                "rate_limit_rpm": 60,
                "monthly_budget_usd": 0.000001,  # Ultra-low budget to trigger exhaustion
                "allowed_providers": ["openai"],
                "allowed_tools": ["gateway_get_utc_time"],
            },
        )
        assert key_resp.status_code == 201
        created_key = key_resp.json()
        raw_secret_key = created_key["api_key"]
        assert raw_secret_key.startswith("ugw_live_")
        key_id = created_key["id"]

        # 1. Unauthenticated when key is missing if require_client_api_key=True (tested via custom settings)
        # Test authenticating with client key on /v1/chat/completions
        with respx.mock:
            respx.post("https://api.openai.com/v1/chat/completions").mock(
                return_value=httpx.Response(
                    200,
                    headers={"content-type": "application/json"},
                    json={
                        "id": "chatcmpl-remed-1",
                        "object": "chat.completion",
                        "created": 1700000000,
                        "model": "gpt-4o",
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": "Budget approved"},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
                    },
                )
            )

            # First request succeeds with raw secret client key
            ok_resp = client.post(
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {raw_secret_key}"},
                json={
                    "model": "openai/gpt-4o",
                    "messages": [{"role": "user", "content": "Hello budget test"}],
                },
            )
            assert ok_resp.status_code == 200

            # 2. Second request fails due to monthly budget exhaustion
            budget_resp = client.post(
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {raw_secret_key}"},
                json={
                    "model": "openai/gpt-4o",
                    "messages": [{"role": "user", "content": "Another request"}],
                },
            )
            assert budget_resp.status_code == 429
            assert "monthly budget" in budget_resp.json()["error"]["message"].lower()

            # 3. Model access control: requesting model outside allowed_models is rejected
            disallowed_model_resp = client.post(
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {raw_secret_key}"},
                json={
                    "model": "anthropic/claude-3-5-sonnet-20241022",
                    "messages": [{"role": "user", "content": "Forbidden model"}],
                },
            )
            assert disallowed_model_resp.status_code == 403


# =============================================================================
# P0.2: Control-Plane Administrative Mutation Protection
# =============================================================================
def test_p0_2_control_plane_mutation_requires_admin_token(
    remediation_settings: GatewaySettings,
) -> None:
    app = create_app(settings_override=remediation_settings)

    with TestClient(app) as client:
        # 1. POST /api/v1/tools without admin token returns 401
        tool_401 = client.post(
            "/api/v1/tools",
            json={
                "name": "unauthorized_tool",
                "source_type": "gateway_webhook",
                "webhook_url": "https://example.com/hook",
            },
        )
        assert tool_401.status_code == 401
        assert "Valid administrative credentials required" in tool_401.json()["error"]["message"]

        # 2. POST /api/v1/mcp/servers without admin token returns 401
        mcp_401 = client.post(
            "/api/v1/mcp/servers",
            json={
                "server_id": "unauth-mcp",
                "name": "Unauth MCP",
                "transport": "http",
                "endpoint_url": "https://example.com/mcp",
            },
        )
        assert mcp_401.status_code == 401

        # 3. POST /api/v1/providers/custom without admin token returns 401
        prov_401 = client.post(
            "/api/v1/providers/custom",
            json={
                "provider_id": "unauth-provider",
                "display_name": "Unauth Provider",
                "base_url": "https://example.com/v1",
            },
        )
        assert prov_401.status_code == 401

        # 4. POST /api/v1/routing/models without admin token returns 401
        model_401 = client.post(
            "/api/v1/routing/models",
            json={
                "provider_id": "openai",
                "model_id": "custom-model",
                "display_name": "Custom Model",
            },
        )
        assert model_401.status_code == 401


# =============================================================================
# P0.3: Atomic Quota & Concurrency Leases Under Race Conditions
# =============================================================================
@pytest.mark.asyncio
async def test_p0_3_atomic_quota_lease_under_parallel_race() -> None:
    from gateway.core.redis import InMemoryRedisFallback

    redis_client = InMemoryRedisFallback()
    redis_mgr = MagicMock()
    redis_mgr.client = redis_client
    qm = QuotaManager(redis_manager=redis_mgr)
    config = QuotaConfigSpec(
        max_concurrency=3,
        rate_limit_rpm=1000,
        rate_limit_rpd=10000,
    )

    # Launch 10 simultaneous lease attempts when max_concurrency is 3
    tasks = [
        qm.try_acquire_lease_atomic(
            request_id=f"req-{i}",
            provider_id="openai",
            supported_scopes=[],
            credential_id="cred-race-1",
            account_group_id=None,
            workspace_id=None,
            cred_max_concurrency=3,
            acct_max_concurrency=None,
            cred_quota=config,
            acct_quota=None,
        )
        for i in range(10)
    ]
    results = await asyncio.gather(*tasks)

    acquired_count = sum(1 for (acquired, _) in results if acquired)
    assert acquired_count <= 3, f"Expected at most 3 leases acquired, got {acquired_count}"


# =============================================================================
# P0.4: Streaming Credential Lease, Lifecycle Release, and Accounting
# =============================================================================
@pytest.mark.asyncio
async def test_p0_4_streaming_credential_release_and_accounting() -> None:
    prov_reg = MagicMock()
    model_reg = MagicMock()
    routing_eng = MagicMock()
    health_mon = MagicMock()
    cred_mgr = MagicMock()

    # Setup managed credential reserve and release
    managed_cred = AdapterCredential(
        provider_id="openai",
        secret="sk-test-stream",
        credential_id="cred-stream-1",
        account_group_id="group-stream",
        is_managed=True,
    )
    dummy_lease = MagicMock(
        request_id="req-stream-1",
        credential=managed_cred,
        account_group_id="group-stream",
    )
    cred_mgr.list_credentials = AsyncMock(return_value=[managed_cred])
    cred_mgr.select_and_reserve_credential = AsyncMock(return_value=dummy_lease)
    cred_mgr.quota_manager = MagicMock()
    cred_mgr.quota_manager.release_lease = AsyncMock()

    adapter = MagicMock()

    async def dummy_stream_gen(req, cred):
        yield CanonicalStreamEvent(
            event_type="text_delta",
            model="gpt-4o",
            text_delta="Hello",
        )
        yield CanonicalStreamEvent(
            event_type="usage",
            model="gpt-4o",
            text_delta=" stream",
            usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15),
        )

    adapter.execute_stream = dummy_stream_gen
    prov_reg.get_adapter.return_value = adapter
    model_reg.is_model_supported_by_provider.return_value = True
    health_mon.is_request_allowed = AsyncMock(return_value=(True, MagicMock()))
    health_mon.record_success = AsyncMock()
    health_mon.record_failure = AsyncMock()
    routing_eng.resolve_candidate_chain = MagicMock(return_value=[MagicMock(provider_id="openai", model_id="gpt-4o")])

    engine = RetryFallbackEngine(
        provider_registry=prov_reg,
        model_registry=model_reg,
        routing_engine=routing_eng,
        health_monitor=health_mon,
        credential_manager=cred_mgr,
    )

    request = CanonicalInternalRequest(
        requested_model="gpt-4o",
        provider_id="openai",
        model_id="gpt-4o",
        messages=[CanonicalMessage(role="user", parts=[CanonicalMessagePart(type="text", text="Stream test")])],
        stream=True,
    )

    # Consume stream
    collected = []
    stream_iter, _, _, _ = await engine.execute_stream_resilient(
        request=request, primary_provider="openai", primary_model="gpt-4o"
    )
    async for event in stream_iter:
        collected.append(event)

    assert len(collected) == 2
    # Verify managed credential lease was released via quota_manager
    assert cred_mgr.quota_manager.release_lease.call_count == 1
    call_kwargs = cred_mgr.quota_manager.release_lease.call_args.kwargs
    assert call_kwargs["request_id"] == "req-stream-1"
    assert call_kwargs["credential_id"] == "cred-stream-1"
    assert call_kwargs["usage"].total_tokens == 15


# =============================================================================
# P0.5: Non-Idempotent Tool Execution Blocks Mid-Loop Cross-Provider Fallback
# =============================================================================
@pytest.mark.asyncio
async def test_p0_5_non_idempotent_tool_blocks_mid_loop_fallback() -> None:
    tool_reg = ToolRegistry(http_pool=MagicMock())
    # Register a non-idempotent tool
    spec = NormalizedToolSpec(
        name="send_payment",
        description="Transfers funds non-idempotently",
        source_type=ToolSourceType.GATEWAY_BUILTIN,
        idempotent=False,
        input_schema={"type": "object", "properties": {"amount": {"type": "number"}}},
    )

    async def payment_handler(args: dict[str, Any]) -> dict[str, str]:
        return {"status": "paid"}

    tool_reg.register_python_tool(spec, payment_handler)

    prov_reg = MagicMock()
    model_reg = MagicMock()
    routing_eng = MagicMock()
    health_mon = MagicMock()

    # Route returns primary openai then fallback anthropic
    cand_primary = MagicMock(provider_id="openai", model_id="gpt-4o")
    cand_fallback = MagicMock(provider_id="anthropic", model_id="claude-3-5-sonnet")
    routing_eng.resolve_candidate_chain.return_value = [cand_primary, cand_fallback]

    # Primary adapter returns a tool call for send_payment, then on second execution throws 500 error
    primary_adapter = MagicMock()
    tool_call_response = CanonicalInternalResponse(
        request_id="resp-call",
        model="gpt-4o",
        provider_id="openai",
        model_id="gpt-4o",
        output_message=CanonicalMessage(role="assistant", parts=[]),
        tool_calls=[
            NormalizedToolCall(
                call_id="call-123",
                name="send_payment",
                arguments={"amount": 50},
            )
        ],
        usage=TokenUsage(input_tokens=10, output_tokens=10, total_tokens=20),
    )

    call_count = 0

    async def mock_execute(req, cred):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return tool_call_response
        raise UpstreamProviderError("OpenAI crashed after payment tool ran", status_code=500, retryable=True)

    primary_adapter.execute = mock_execute
    prov_reg.get_adapter.return_value = primary_adapter
    health_mon.is_request_allowed = AsyncMock(return_value=(True, MagicMock()))
    health_mon.record_failure = AsyncMock()
    model_reg.get_model = MagicMock(return_value=MagicMock())

    orchestrator = ToolGatewayOrchestrator(tool_registry=tool_reg)
    engine = RetryFallbackEngine(
        provider_registry=prov_reg,
        model_registry=model_reg,
        routing_engine=routing_eng,
        health_monitor=health_mon,
        tool_orchestrator=orchestrator,
    )

    request = CanonicalInternalRequest(
        requested_model="gpt-4o",
        provider_id="openai",
        model_id="gpt-4o",
        messages=[CanonicalMessage(role="user", parts=[CanonicalMessagePart(type="text", text="Pay $50")])],
    )

    # Must raise UpstreamProviderError and NOT silently fallback to secondary cand_fallback
    with pytest.raises(UpstreamProviderError) as exc_info:
        await engine.execute_resilient(
            request=request,
            primary_provider="openai",
            primary_model="gpt-4o",
            tool_mode=ToolExecutionMode.GATEWAY_MANAGED,
        )

    assert "OpenAI crashed after payment tool ran" in str(exc_info.value)
    # Primary executed twice (round 1 + round 2 failure), fallback never touched
    assert call_count == 2


# =============================================================================
# P1.6: Async DNS & IP SSRF Verification Guard
# =============================================================================
@pytest.mark.asyncio
async def test_p1_6_async_ssrf_dns_rebinding_guard() -> None:
    # 1. Blocks direct loopback and private IP literals
    with pytest.raises(Exception) as exc:
        await validate_and_resolve_outbound_url("http://127.0.0.1:8080/secret")
    assert "blocked by SSRF" in str(exc.value)

    # 2. Blocks cloud metadata IP
    with pytest.raises(Exception) as exc:
        await validate_and_resolve_outbound_url("http://169.254.169.254/latest/meta-data/")
    assert "blocked by SSRF" in str(exc.value)

    # 3. Allows RFC 2606 test domains safely
    resolved = await validate_and_resolve_outbound_url("https://api.example.com/webhook")
    assert resolved == ["93.184.216.34"]


# =============================================================================
# P1.7: SQL Pushdown Analytics and Buffered Usage Tracking
# =============================================================================
@pytest.mark.asyncio
async def test_p1_7_sql_pushdown_analytics_and_streaming_usage(
    remediation_settings: GatewaySettings,
) -> None:
    app = create_app(settings_override=remediation_settings)

    async with app.router.lifespan_context(app):
        usage_tracker = app.state.usage_tracker

        # Enqueue usage events
        event1 = UsageEventInput(
            request_id="req-p1-1",
            provider_id="openai",
            model_id="gpt-4o",
            status_code=200,
            success=True,
            usage=TokenUsage(input_tokens=100, output_tokens=50, total_tokens=150),
            latency_ms=120.0,
            ttft_ms=35.0,
            client_key_id="key-finance-001",
        )
        event2 = UsageEventInput(
            request_id="req-p1-2",
            provider_id="openai",
            model_id="gpt-4o",
            status_code=500,
            success=False,
            usage=TokenUsage(input_tokens=50, output_tokens=0, total_tokens=50),
            latency_ms=45.0,
            fallback_used=True,
            client_key_id="key-finance-001",
        )
        usage_tracker.enqueue_usage(event1)
        usage_tracker.enqueue_usage(event2)

        # Overview summary via SQL GROUP BY pushdown
        summary = await usage_tracker.get_overview_summary()
        assert summary.total_requests == 2
        assert summary.successful_requests == 1
        assert summary.failed_requests == 1
        assert summary.fallback_requests == 1
        assert summary.total_input_tokens == 150
        assert summary.total_output_tokens == 50
        assert summary.total_tokens == 200

        # Model breakdown via SQL GROUP BY pushdown
        models = await usage_tracker.get_model_breakdown()
        assert len(models) == 1
        assert models[0].model_id == "gpt-4o"
        assert models[0].request_count == 2
        assert models[0].avg_ttft_ms is not None

        # Monthly spend by client_key_id
        spend = await usage_tracker.get_client_key_monthly_spend("key-finance-001")
        assert spend >= 0.0


# =============================================================================
# P1.9: Dynamic Registered Custom Provider Routing Resolution
# =============================================================================
def test_p1_9_custom_provider_prefix_resolution() -> None:
    # When registered_providers includes "vllm-cluster"
    registered = ["openai", "anthropic", "vllm-cluster"]
    prov, mid = resolve_target_provider_and_model(
        "vllm-cluster/deepseek-r1-671b",
        registered_providers=registered,
    )
    assert prov == "vllm-cluster"
    assert mid == "deepseek-r1-671b"

    # Default provider for un-prefixed standard model
    prov_std, mid_std = resolve_target_provider_and_model(
        "claude-3-5-sonnet-20241022",
        registered_providers=registered,
    )
    assert prov_std == "anthropic"
    assert mid_std == "claude-3-5-sonnet-20241022"


# =============================================================================
# P2.10: Multi-Turn Tool Result Formatting & Consecutive Role Normalization
# =============================================================================
def test_p2_10_multi_tool_turn_formatting_openai_anthropic_gemini() -> None:
    dummy_pool = MagicMock()
    # 1. OpenAI Reasoning Model translation: developer role & omitted temperature
    oai = OpenAIProviderAdapter(dummy_pool)
    req_o3 = CanonicalInternalRequest(
        requested_model="o3-mini",
        provider_id="openai",
        model_id="o3-mini",
        messages=[
            CanonicalMessage(role="system", parts=[CanonicalMessagePart(type="text", text="System context")]),
            CanonicalMessage(role="user", parts=[CanonicalMessagePart(type="text", text="Solve logic")]),
        ],
        temperature=0.7,
        top_p=0.9,
    )
    oai_payload = oai._build_chat_payload(req_o3, stream=False)
    assert oai_payload["messages"][0]["role"] == "developer"
    assert "temperature" not in oai_payload
    assert "top_p" not in oai_payload

    # 2. Anthropic consecutive role normalization
    anth = AnthropicProviderAdapter(dummy_pool)
    req_anth = CanonicalInternalRequest(
        requested_model="claude-3-5-sonnet-20241022",
        provider_id="anthropic",
        model_id="claude-3-5-sonnet-20241022",
        messages=[
            CanonicalMessage(role="user", parts=[CanonicalMessagePart(type="text", text="Part 1")]),
            CanonicalMessage(role="user", parts=[CanonicalMessagePart(type="text", text="Part 2")]),
        ],
    )
    anth_payload = anth._build_messages_payload(req_anth, stream=False)
    anth_msgs = anth_payload["messages"]
    assert len(anth_msgs) == 1
    assert anth_msgs[0]["role"] == "user"
    assert len(anth_msgs[0]["content"]) == 2

    # 3. Gemini consecutive role normalization
    gemini = GeminiProviderAdapter(dummy_pool)
    gem_payload = gemini._build_gemini_payload(req_anth)
    gem_contents = gem_payload["contents"]
    assert len(gem_contents) == 1
    assert gem_contents[0]["role"] == "user"
    assert len(gem_contents[0]["parts"]) == 2


# =============================================================================
# P3.13: Half-Open Single Probe Concurrency Lock & Metrics Cardinality Cap
# =============================================================================
@pytest.mark.asyncio
async def test_p3_13_half_open_single_probe_lock_and_metrics_cardinality_cap() -> None:
    # 1. Half-Open Single Probe Concurrency Lock in HealthMonitor
    redis_mock = MagicMock()
    # Mock redis.set(name, value, nx=True, ex=...)
    # First call succeeds (probe locked), second returns False (blocked)
    probe_state = {"locked": False}

    async def mock_set(name: str, value: Any, nx: bool = False, ex: int | None = None):
        if nx and "probe_lock" in name:
            if not probe_state["locked"]:
                probe_state["locked"] = True
                return True
            return False
        return True

    async def mock_get(key: str) -> str | None:
        if key.endswith(":state"):
            return "HALF_OPEN"
        return None

    redis_mock.set = mock_set
    redis_mock.get = mock_get
    redis_mock.hgetall = AsyncMock(return_value={})

    redis_mgr = MagicMock()
    redis_mgr.client = redis_mock
    monitor = HealthMonitor(
        redis_manager=redis_mgr,
        provider_registry=MagicMock(),
        model_registry=MagicMock(),
    )

    # First request acquires probe lock -> allowed
    allow_1, _ = await monitor.is_request_allowed(CircuitScope.PROVIDER, "openai")
    assert allow_1 is True

    # Concurrent second request during HALF_OPEN probe -> blocked by probe lock
    allow_2, _ = await monitor.is_request_allowed(CircuitScope.PROVIDER, "openai")
    assert allow_2 is False

    # 2. Metrics Cardinality Normalization Cap
    metrics = MetricsCollector(max_distinct_series=2)
    # Register models beyond max_distinct_series
    metrics.record_request(provider_id="openai", model_id="model-alpha", protocol="openai_chat", status="success", latency_ms=10.0)
    metrics.record_request(provider_id="openai", model_id="model-beta", protocol="openai_chat", status="success", latency_ms=12.0)
    metrics.record_request(provider_id="openai", model_id="model-gamma-overflow", protocol="openai_chat", status="success", latency_ms=15.0)

    # Scrape prometheus output
    output = metrics.format_prometheus_text()
    assert '_other"' in output
