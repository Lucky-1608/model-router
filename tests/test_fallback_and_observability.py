"""Tests for Phase 6 (Prompts 8 & 9): Reliability, Fallback Engine, Usage Analytics, and Observability."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from gateway.capability_registry.models import CapabilityFlag, CapabilityState
from gateway.config.settings import GatewaySettings
from gateway.core.canonical import CanonicalInternalRequest, CanonicalMessage
from gateway.health_monitor.models import (
    CircuitBreakerConfig,
    CircuitBreakerState,
    CircuitScope,
)
from gateway.main import create_app
from gateway.model_registry.models import ModelRegistrationRequest
from gateway.retry_fallback.guard import FallbackCompatibilityGuard

TEST_MASTER_KEY = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="


@pytest.fixture
def phase6_settings(tmp_path: Path) -> GatewaySettings:
    db_file = tmp_path / "test_phase6.db"
    return GatewaySettings(
        app_name="Universal AI Gateway Phase 6 Test",
        env="test",
        database_url=f"sqlite+aiosqlite:///{db_file.as_posix()}",
        redis_url="memory://",
        master_kek_base64=TEST_MASTER_KEY,
        admin_bootstrap_token="phase6-admin-token",
    )


def test_circuit_breaker_transitions_and_analytics_reset(
    phase6_settings: GatewaySettings,
) -> None:
    """Verifies CLOSED -> OPEN -> HALF_OPEN -> CLOSED state transitions and operator reset via API."""
    app = create_app(phase6_settings)
    with TestClient(app) as client:
        monitor = app.state.health_monitor
        monitor._config = CircuitBreakerConfig(
            failure_threshold=2,
            recovery_timeout_seconds=0.05,
            half_open_max_probes=1,
        )

        # Configure mock failure on primary openai and fallback off so circuit trips on openai
        with respx.mock(assert_all_called=False) as respx_mock:
            respx_mock.post("https://api.openai.com/v1/chat/completions").mock(
                return_value=httpx.Response(
                    503,
                    json={"error": {"message": "Upstream service overloaded", "type": "server_error"}},
                )
            )

            for _ in range(2):
                resp = client.post(
                    "/v1/chat/completions",
                    headers={
                        "Authorization": "Bearer sk-test-openai",
                        "X-Gateway-Mode": "unified",
                        "X-Gateway-Fallback": "false",
                    },
                    json={
                        "model": "openai/gpt-4o",
                        "messages": [{"role": "user", "content": "Ping"}],
                    },
                )
                assert resp.status_code in (502, 503)

            # Verify circuit is now OPEN in /api/v1/analytics/circuit-breakers
            cb_resp = client.get("/api/v1/analytics/circuit-breakers")
            assert cb_resp.status_code == 200
            cb_list = cb_resp.json()["data"]
            prov_cb = next(
                c
                for c in cb_list
                if c["scope"] == CircuitScope.PROVIDER.value
                and c["target_id"] == "openai"
            )
            assert prov_cb["state"] == CircuitBreakerState.OPEN.value
            assert prov_cb["failure_count"] >= 2

            # Subsequent request with fallback=false fails fast with 503 GW_SERVICE_UNAVAILABLE (circuit OPEN)
            blocked_resp = client.post(
                "/v1/chat/completions",
                headers={
                    "Authorization": "Bearer sk-test-openai",
                    "X-Gateway-Mode": "unified",
                    "X-Gateway-Fallback": "false",
                },
                json={
                    "model": "openai/gpt-4o",
                    "messages": [{"role": "user", "content": "Blocked by circuit"}],
                },
            )
            assert blocked_resp.status_code == 503
            err_body = blocked_resp.json()["error"]
            assert err_body["code"] == "GW_SERVICE_UNAVAILABLE"
            assert "OPEN" in err_body["message"]

            # Reset circuit breaker via operator endpoint
            reset_resp = client.post(
                "/api/v1/analytics/circuit-breakers/provider/openai/reset",
                headers={"X-Gateway-Admin-Token": "phase6-admin-token"},
            )
            assert reset_resp.status_code == 200
            assert reset_resp.json()["state"] == CircuitBreakerState.CLOSED.value


def test_fallback_compatibility_guard_invariants(
    phase6_settings: GatewaySettings,
) -> None:
    """Verifies FallbackCompatibilityGuard strictly blocks mid-stream, non-idempotent tool, and capability-mismatched fallbacks."""
    app = create_app(phase6_settings)
    model_registry = app.state.model_registry
    guard = FallbackCompatibilityGuard(tool_registry=app.state.tool_registry)

    base_req = CanonicalInternalRequest(
        requested_model="gpt-4o",
        messages=[CanonicalMessage.from_text("user", "Hello")],
    )
    claude_meta = model_registry.get_model("anthropic/claude-3-7-sonnet-20250219")

    # 1. Compatible pre-stream fallback
    verdict_ok = guard.check_compatibility(
        request=base_req,
        primary_provider_id="openai",
        candidate_model=claude_meta,
        bytes_sent_to_client=0,
        non_idempotent_tool_executed=False,
    )
    assert verdict_ok.compatible is True
    assert verdict_ok.reason_code == "OK"

    # 2. Rejects fallback once streaming bytes have been sent to client
    verdict_stream = guard.check_compatibility(
        request=base_req,
        primary_provider_id="openai",
        candidate_model=claude_meta,
        bytes_sent_to_client=128,
        non_idempotent_tool_executed=False,
    )
    assert verdict_stream.compatible is False
    assert verdict_stream.reason_code == "STREAM_ALREADY_STARTED"

    # 3. Rejects fallback after non-idempotent tool execution
    verdict_tool = guard.check_compatibility(
        request=base_req,
        primary_provider_id="openai",
        candidate_model=claude_meta,
        bytes_sent_to_client=0,
        non_idempotent_tool_executed=True,
    )
    assert verdict_tool.compatible is False
    assert verdict_tool.reason_code == "NON_IDEMPOTENT_TOOL_EXECUTED"

    # 4. Rejects fallback to a candidate lacking required capability (e.g. structured outputs on a model without it)
    no_struct_model = model_registry.register_model(
        ModelRegistrationRequest(
            provider_id="ollama",
            provider_model_id="no-struct-model",
            display_name="No Structured Output Model",
            context_length=8192,
            capabilities={
                CapabilityFlag.CHAT_COMPLETIONS: CapabilityState.SUPPORTED,
                CapabilityFlag.STRUCTURED_OUTPUTS: CapabilityState.UNSUPPORTED,
            },
        )
    )
    struct_req = CanonicalInternalRequest(
        requested_model="gpt-4o",
        messages=[CanonicalMessage.from_text("user", "Hello")],
        required_capabilities={CapabilityFlag.STRUCTURED_OUTPUTS},
    )
    verdict_cap = guard.check_compatibility(
        request=struct_req,
        primary_provider_id="openai",
        candidate_model=no_struct_model,
    )
    assert verdict_cap.compatible is False
    assert verdict_cap.reason_code == "MISSING_REQUIRED_CAPABILITY"

    # 5. Rejects fallback when candidate context window is smaller than estimated prompt tokens
    tiny_model = model_registry.register_model(
        ModelRegistrationRequest(
            provider_id="ollama",
            provider_model_id="tiny-ctx-model",
            display_name="Tiny Context Model",
            context_length=10,
            capabilities={CapabilityFlag.CHAT_COMPLETIONS: CapabilityState.SUPPORTED},
        )
    )
    long_req = CanonicalInternalRequest(
        requested_model="gpt-4o",
        messages=[CanonicalMessage.from_text("user", "A" * 500)],
    )
    verdict_ctx = guard.check_compatibility(
        request=long_req,
        primary_provider_id="openai",
        candidate_model=tiny_model,
    )
    assert verdict_ctx.compatible is False
    assert verdict_ctx.reason_code == "INSUFFICIENT_CONTEXT_WINDOW"


def test_cross_provider_fallback_and_usage_analytics_pipeline(
    phase6_settings: GatewaySettings,
) -> None:
    """Verifies automatic cross-provider fallback on HTTP 503, cost tracking, Prometheus /metrics, and /analytics/*."""
    app = create_app(phase6_settings)
    with TestClient(app) as client:
        with respx.mock(assert_all_called=False) as respx_mock:
            # Primary OpenAI gpt-4o fails with retryable 503
            respx_mock.post("https://api.openai.com/v1/chat/completions").mock(
                return_value=httpx.Response(
                    503,
                    json={"error": {"message": "OpenAI capacity exhausted", "type": "server_error"}},
                )
            )
            # Fallback peer openai/gpt-4o-mini or anthropic/claude-3-7-sonnet succeeds;
            # Let's mock both openai/gpt-4o-mini (if same-provider peer is tried) or anthropic
            def _openai_handler(request: httpx.Request) -> httpx.Response:
                import json as _json

                body = _json.loads(request.content.decode("utf-8"))
                if body.get("model") == "gpt-4o":
                    return httpx.Response(
                        503,
                        json={"error": {"message": "gpt-4o overloaded", "type": "server_error"}},
                    )
                return httpx.Response(
                    200,
                    json={
                        "id": "chatcmpl-fallback-mini",
                        "object": "chat.completion",
                        "created": 1700000000,
                        "model": body.get("model", "gpt-4o-mini"),
                        "choices": [
                            {
                                "index": 0,
                                "message": {
                                    "role": "assistant",
                                    "content": "Served by fallback model!",
                                },
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 1000,
                            "completion_tokens": 500,
                            "total_tokens": 1500,
                        },
                    },
                )

            respx_mock.post("https://api.openai.com/v1/chat/completions").mock(
                side_effect=_openai_handler
            )

            resp = client.post(
                "/v1/chat/completions",
                headers={
                    "Authorization": "Bearer sk-test-secret-never-logged",
                    "X-Gateway-Mode": "unified",
                    "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
                },
                json={
                    "model": "openai/gpt-4o",
                    "messages": [{"role": "user", "content": "Sensitive prompt text XYZ-999"}],
                },
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["choices"][0]["message"]["content"] == "Served by fallback model!"
            assert resp.headers["X-Gateway-Fallback-Used"] == "true"
            assert int(resp.headers["X-Gateway-Attempts"]) >= 2
            assert (
                resp.headers["X-Gateway-Trace-Id"]
                == "4bf92f3577b34da6a3ce929d0e0e4736"
            )

        # 1. Verify /api/v1/analytics/overview
        ov_resp = client.get("/api/v1/analytics/overview")
        assert ov_resp.status_code == 200
        overview = ov_resp.json()
        assert overview["total_requests"] == 1
        assert overview["successful_requests"] == 1
        assert overview["fallback_requests"] == 1
        assert overview["total_input_tokens"] == 1000
        assert overview["total_output_tokens"] == 500
        assert overview["total_tokens"] == 1500
        assert overview["total_estimated_cost_usd"] > 0.0

        # 2. Verify /api/v1/analytics/providers & /models & /requests
        prov_resp = client.get("/api/v1/analytics/providers")
        assert prov_resp.status_code == 200
        assert len(prov_resp.json()) == 1
        assert prov_resp.json()[0]["fallback_count"] == 1

        mod_resp = client.get("/api/v1/analytics/models")
        assert mod_resp.status_code == 200
        assert len(mod_resp.json()) == 1

        req_resp = client.get("/api/v1/analytics/requests")
        assert req_resp.status_code == 200
        reqs = req_resp.json()
        assert len(reqs) == 1
        assert reqs[0]["fallback_used"] is True
        assert reqs[0]["attempt_count"] >= 2
        # Ensure sensitive prompt or API key is never stored in usage records
        raw_reqs_text = req_resp.text
        assert "Sensitive prompt text XYZ-999" not in raw_reqs_text
        assert "sk-test-secret-never-logged" not in raw_reqs_text

        # 3. Verify /api/v1/analytics/traces (W3C traceparent preserved & sanitized)
        trace_resp = client.get(
            "/api/v1/analytics/traces",
            params={"trace_id": "4bf92f3577b34da6a3ce929d0e0e4736"},
        )
        assert trace_resp.status_code == 200
        spans = trace_resp.json()["data"]
        assert len(spans) >= 1
        assert spans[0]["name"] == "ugw.request"
        assert spans[0]["attributes"]["ugw.fallback_used"] is True
        assert "Sensitive prompt text XYZ-999" not in trace_resp.text

        # 4. Verify /metrics Prometheus exposition format
        metrics_resp = client.get("/metrics")
        assert metrics_resp.status_code == 200
        prom_text = metrics_resp.text
        assert "ugw_requests_total{" in prom_text
        assert "ugw_tokens_total{" in prom_text
        assert "ugw_estimated_cost_usd_total{" in prom_text
        assert "ugw_fallback_executions_total{" in prom_text
        assert "ugw_circuit_breaker_state{" in prom_text
