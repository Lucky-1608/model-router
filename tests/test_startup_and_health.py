"""Tests for FastAPI application startup, health/readiness endpoints, versioning, and error handling."""

from __future__ import annotations

from fastapi import FastAPI
import httpx
from pydantic import BaseModel

from gateway.config import GatewaySettings
from gateway.core import UnsupportedCapabilityError
from gateway.main import create_app


async def test_liveness_endpoint(async_client: httpx.AsyncClient) -> None:
    resp = await async_client.get("/health/live")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "alive"
    assert body["service"] == "Universal AI Gateway Test"
    assert "X-Gateway-Request-Id" in resp.headers
    assert resp.headers["X-Gateway-Version"] == "0.1.0-test"
    assert "X-Gateway-Latency-Overhead-Ms" in resp.headers


async def test_readiness_endpoint_healthy(async_client: httpx.AsyncClient) -> None:
    resp = await async_client.get("/health/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert body["components"]["database"]["status"] == "healthy"
    assert body["components"]["redis"]["status"] == "healthy"
    assert body["components"]["http_pool"]["pool_count"] >= 1


async def test_api_version_endpoints(async_client: httpx.AsyncClient) -> None:
    for path in ("/api/v1/version", "/v1/version"):
        resp = await async_client.get(path)
        assert resp.status_code == 200
        body = resp.json()
        assert body["api_version"] == "v1"
        assert body["supported_integration_modes"] == ["unified", "native", "custom"]
        assert body["default_routing_weights"]["quality"] == 0.30


async def test_request_id_propagation_and_sanitization(
    async_client: httpx.AsyncClient,
) -> None:
    # Safe correlation ID is preserved
    custom_id = "req_client_trace_0001"
    resp = await async_client.get(
        "/health/live", headers={"X-Gateway-Request-Id": custom_id}
    )
    assert resp.status_code == 200
    assert resp.headers["X-Gateway-Request-Id"] == custom_id

    # Unsafe header value with CRLF/special chars is replaced with a fresh UUID
    unsafe_id = "bad\r\nid<script>"
    resp_unsafe = await async_client.get(
        "/health/live", headers={"X-Gateway-Request-Id": unsafe_id}
    )
    assert resp_unsafe.status_code == 200
    assert resp_unsafe.headers["X-Gateway-Request-Id"] != unsafe_id


class SampleInput(BaseModel):
    model: str
    temperature: float


async def test_centralized_exception_handling_and_redaction(
    test_settings: GatewaySettings,
) -> None:
    app: FastAPI = create_app(settings_override=test_settings)

    @app.post("/test/capability-error")
    async def raise_cap_error(payload: SampleInput) -> dict[str, str]:
        raise UnsupportedCapabilityError(
            f"Model {payload.model} does not support structured_outputs (key sk-1234567890abcdef)",
            param="response_format",
            details={"provider": "ollama"},
        )

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            # 1. Domain exception handler with secret redaction
            r1 = await client.post(
                "/test/capability-error",
                json={"model": "ollama/llama3", "temperature": 0.2},
            )
            assert r1.status_code == 422
            err1 = r1.json()["error"]
            assert err1["code"] == "GW_UNSUPPORTED_CAPABILITY"
            assert err1["type"] == "unsupported_capability_error"
            assert err1["param"] == "response_format"
            assert "sk-" not in err1["message"]
            assert "[REDACTED_SECRET]" in err1["message"]
            assert err1["gateway_metadata"]["provider"] == "ollama"
            assert err1["gateway_metadata"]["retryable"] is False

            # 2. RequestValidationError handler
            r2 = await client.post(
                "/test/capability-error",
                json={"model": "gpt-4o", "temperature": "not-a-float"},
            )
            assert r2.status_code == 422
            err2 = r2.json()["error"]
            assert err2["code"] == "GW_VALIDATION_ERROR"
            assert err2["type"] == "invalid_request_error"
            assert "temperature" in (err2["param"] or "")


async def test_readiness_returns_503_when_database_unhealthy(
    test_settings: GatewaySettings,
) -> None:
    app: FastAPI = create_app(settings_override=test_settings)
    async with app.router.lifespan_context(app):
        async def mock_unhealthy_db() -> dict[str, str]:
            return {"status": "unhealthy", "dialect": "sqlite", "error": "OperationalError"}

        app.state.db_manager.check_health = mock_unhealthy_db  # type: ignore[method-assign]
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            resp = await client.get("/health/ready")
            assert resp.status_code == 503
            assert resp.json()["status"] == "not_ready"


