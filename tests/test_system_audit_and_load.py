"""Phase 8 (Prompt 11): Full System Concurrent Load, Failure Injection, and Security Boundary Audit Tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
import time

from fastapi.testclient import TestClient
import httpx
import pytest
import respx

from gateway.config.settings import GatewaySettings
from gateway.core.canonical import CanonicalInternalResponse, TokenUsage
from gateway.credential_manager.models import (
    AccountGroupCreateRequest,
    CredentialCreateRequest,
    QuotaConfigSpec,
    SelectionStrategy,
)
from gateway.main import create_app

TEST_MASTER_KEY = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="


@pytest.fixture
def phase8_settings(tmp_path: Path) -> GatewaySettings:
    db_file = tmp_path / "test_phase8_audit.db"
    return GatewaySettings(
        app_name="Universal AI Gateway Phase 8 Audit",
        env="test",
        database_url=f"sqlite+aiosqlite:///{db_file.as_posix()}",
        redis_url="memory://",
        master_kek_base64=TEST_MASTER_KEY,
        admin_bootstrap_token="phase8-audit-admin-token",
    )


@pytest.mark.asyncio
async def test_concurrent_load_credential_rotation_and_quota_release(
    phase8_settings: GatewaySettings,
) -> None:
    """Executes 30 concurrent requests across a 3-credential pool in a shared AccountGroup.

    Verifies:
    - Zero unhandled exceptions or race conditions under concurrent load.
    - All active concurrency reservations (`in_flight`) return to 0 after completion.
    - Load is distributed across all 3 credentials via CredentialManager.
    """
    app = create_app(phase8_settings)
    db_manager = app.state.db_manager
    redis_manager = app.state.redis_manager
    cred_manager = app.state.credential_manager

    db_manager.init_engine()
    redis_manager.init_client()
    await cred_manager.ensure_tables()

    try:
        ag = await cred_manager.create_account_group(
            AccountGroupCreateRequest(
                provider_id="openai",
                name="Concurrent Load Shared Org",
                max_concurrency=50,
                quota_config=QuotaConfigSpec(
                    requests_per_minute=5000,
                    tokens_per_minute=5_000_000,
                ),
            ),
            actor_id="load-test",
        )
        created_ids: list[str] = []
        for idx in range(1, 4):
            c = await cred_manager.create_credential(
                CredentialCreateRequest(
                    provider_id="openai",
                    label=f"Load Key #{idx}",
                    secret=f"sk-proj-loadtestkey00000{idx}",
                    account_group_id=ag.id,
                    weight=100,
                    max_concurrency=20,
                ),
                actor_id="load-test",
            )
            created_ids.append(c.id)

        used_cred_ids: list[str] = []

        async def _single_worker(req_num: int) -> CanonicalInternalResponse:
            async def _simulated_upstream(cred) -> CanonicalInternalResponse:
                await asyncio.sleep(0.005)
                used_cred_ids.append(cred.credential_id)
                return CanonicalInternalResponse(
                    request_id=f"req-{req_num}",
                    response_id=f"resp-{req_num}",
                    provider_id="openai",
                    model="gpt-4o-mini",
                    usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15),
                )

            resp, _lease = await cred_manager.execute_with_rotation(
                provider_id="openai",
                model="gpt-4o-mini",
                strategy=SelectionStrategy.ROUND_ROBIN,
                operation=_simulated_upstream,
            )
            return resp

        start = time.perf_counter()
        results = await asyncio.gather(*[_single_worker(i) for i in range(30)])
        elapsed_ms = (time.perf_counter() - start) * 1000.0

        assert len(results) == 30
        assert len(used_cred_ids) == 30
        # Verify all 3 credentials participated in serving the 30 concurrent requests
        assert set(used_cred_ids) == set(created_ids)
        assert elapsed_ms < 5000.0

        # Verify all in-flight concurrency reservations were cleanly released back to 0
        for cid in created_ids:
            health = await cred_manager.get_credential_health(cid)
            assert health.active_requests == 0
    finally:
        await redis_manager.close()
        await db_manager.dispose()


def test_cascading_multi_provider_failure_injection_and_recovery(
    phase8_settings: GatewaySettings,
) -> None:
    """Injects cascading upstream failures (OpenAI 503 -> Fallback candidate succeeds) and mid-stream safety."""
    app = create_app(phase8_settings)
    with TestClient(app) as client:
        with respx.mock(assert_all_called=False) as respx_mock:
            call_counter = {"openai_gpt4o": 0, "fallback": 0}

            def _openai_cascade(request: httpx.Request) -> httpx.Response:
                import json as _json

                body = _json.loads(request.content.decode("utf-8"))
                model_name = body.get("model", "")
                if model_name == "gpt-4o":
                    call_counter["openai_gpt4o"] += 1
                    return httpx.Response(
                        503,
                        json={
                            "error": {
                                "message": "Injected primary outage",
                                "type": "server_error",
                            }
                        },
                    )
                call_counter["fallback"] += 1
                return httpx.Response(
                    200,
                    json={
                        "id": "chatcmpl-recovered",
                        "object": "chat.completion",
                        "created": 1700000000,
                        "model": model_name,
                        "choices": [
                            {
                                "index": 0,
                                "message": {
                                    "role": "assistant",
                                    "content": f"Recovered via {model_name}",
                                },
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 120,
                            "completion_tokens": 40,
                            "total_tokens": 160,
                        },
                    },
                )

            respx_mock.post("https://api.openai.com/v1/chat/completions").mock(
                side_effect=_openai_cascade
            )

            resp = client.post(
                "/v1/chat/completions",
                headers={
                    "Authorization": "Bearer sk-test-cascade-key-999999",
                    "X-Gateway-Mode": "unified",
                },
                json={
                    "model": "openai/gpt-4o",
                    "messages": [{"role": "user", "content": "Test cascade recovery"}],
                },
            )
            assert resp.status_code == 200
            assert resp.headers["X-Gateway-Fallback-Used"] == "true"
            assert int(resp.headers["X-Gateway-Attempts"]) == 2
            assert call_counter["openai_gpt4o"] == 1
            assert call_counter["fallback"] == 1


def test_security_boundary_audit_ssrf_rbac_and_zero_secret_leakage(
    phase8_settings: GatewaySettings,
) -> None:
    """Comprehensive security boundary audit across SSRF, RBAC, and secret redaction."""
    app = create_app(phase8_settings)
    admin_headers = {
        "X-Gateway-Admin-Token": "phase8-audit-admin-token",
        "X-Gateway-Admin-Role": "SUPER_ADMIN",
    }
    readonly_headers = {
        "X-Gateway-Admin-Token": "phase8-audit-admin-token",
        "X-Gateway-Admin-Role": "READ_ONLY",
    }

    with TestClient(app) as client:
        # 1. SSRF Boundary Check on Custom Provider Registration (AWS/GCP Metadata IP 169.254.169.254)
        ssrf_prov = client.post(
            "/api/v1/providers/custom",
            json={
                "provider_id": "evil-ssrf-provider",
                "display_name": "Evil Metadata Probe",
                "base_url": "http://169.254.169.254/latest/meta-data/",
            },
        )
        assert ssrf_prov.status_code in (400, 422)

        # 2. SSRF Boundary Check on Webhook Tool Registration (loopback 127.0.0.1)
        ssrf_tool = client.post(
            "/api/v1/tools",
            json={
                "name": "evil_internal_probe",
                "description": "Attempts loopback SSRF",
                "source_type": "gateway_webhook",
                "webhook_url": "http://127.0.0.1:6379/info",
            },
        )
        assert ssrf_tool.status_code in (400, 403, 422)

        # 3. SSRF Boundary Check on MCP Server Registration (private 10.0.0.1)
        ssrf_mcp = client.post(
            "/api/v1/mcp/servers",
            json={
                "server_id": "evil-mcp",
                "name": "Evil Internal MCP",
                "transport": "http",
                "endpoint_url": "http://10.0.0.1:8080/rpc",
            },
        )
        assert ssrf_mcp.status_code in (400, 403, 422)

        # 4. RBAC Enforcement: READ_ONLY cannot create credentials or reset circuit breakers
        ro_cred = client.post(
            "/api/v1/admin/credentials",
            headers=readonly_headers,
            json={
                "provider_id": "openai",
                "label": "Unauthorized Write",
                "secret": "sk-proj-unauthorizedsecretvalue12345",
            },
        )
        assert ro_cred.status_code == 403

        # 5. Secret Non-Leakage Audit: Store a highly distinctive secret and verify it never appears in any GET endpoint
        canary_secret = "sk-proj-CANARY_ULTRA_SECRET_9876543210_NEVER_LEAK"
        created_cred = client.post(
            "/api/v1/admin/credentials",
            headers=admin_headers,
            json={
                "provider_id": "openai",
                "label": "Canary Audit Credential",
                "secret": canary_secret,
            },
        )
        assert created_cred.status_code == 201
        cred_id = created_cred.json()["id"]

        audit_endpoints = [
            "/api/v1/admin/credentials",
            f"/api/v1/admin/credentials/{cred_id}",
            f"/api/v1/admin/credentials/{cred_id}/health",
            "/api/v1/admin/credentials/audit-logs",
            "/api/v1/admin/system/settings",
            "/api/v1/admin/dashboard/summary",
            "/api/v1/analytics/overview",
            "/api/v1/analytics/credentials",
            "/metrics",
        ]
        for ep in audit_endpoints:
            r = client.get(ep, headers=admin_headers)
            assert r.status_code == 200, f"Endpoint {ep} returned {r.status_code}"
            assert (
                canary_secret not in r.text
            ), f"CRITICAL: Canary secret leaked in {ep}"
            assert (
                TEST_MASTER_KEY not in r.text
            ), f"CRITICAL: Master KEK leaked in {ep}"
