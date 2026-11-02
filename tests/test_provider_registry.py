"""Tests for ProviderRegistry lifecycle, custom registration, and Provider Discovery API."""

from __future__ import annotations

import httpx
import respx


async def test_list_builtin_providers_via_api(async_client: httpx.AsyncClient) -> None:
    resp = await async_client.get("/api/v1/providers")
    assert resp.status_code == 200
    providers = resp.json()
    provider_ids = {p["provider_id"] for p in providers}
    assert provider_ids == {
        "openai",
        "anthropic",
        "gemini",
        "openrouter",
        "groq",
        "ollama",
        "elevenlabs",
    }


async def test_get_provider_and_capability_manifest(
    async_client: httpx.AsyncClient,
) -> None:
    resp = await async_client.get("/api/v1/providers/anthropic/capabilities")
    assert resp.status_code == 200
    manifest = resp.json()
    assert manifest["provider_id"] == "anthropic"
    assert manifest["capabilities"]["messages_api"] == "SUPPORTED"
    assert manifest["capabilities"]["embeddings"] == "UNSUPPORTED"

    resp_404 = await async_client.get("/api/v1/providers/nonexistent-provider")
    assert resp_404.status_code == 404
    assert resp_404.json()["error"]["code"] == "GW_NOT_FOUND"


@respx.mock
async def test_provider_health_probe_and_model_discovery_api(
    async_client: httpx.AsyncClient,
) -> None:
    respx.get("https://api.openai.com/v1/models").mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"id": "gpt-4o"}, {"id": "o3-mini"}]},
        )
    )
    disc_resp = await async_client.post(
        "/api/v1/providers/openai/discover-models",
        headers={"X-Upstream-Api-Key": "sk-proj-test_discovery_key_123456"},
    )
    assert disc_resp.status_code == 200
    discovered = disc_resp.json()
    assert len(discovered) == 2

    health_resp = await async_client.get("/api/v1/providers/openai/health")
    assert health_resp.status_code == 200
    assert health_resp.json()["status"] == "HEALTHY"


async def test_register_and_unregister_custom_provider_api(
    async_client: httpx.AsyncClient,
) -> None:
    custom_payload = {
        "provider_id": "vllm-cluster",
        "display_name": "Internal vLLM Cluster",
        "adapter_type": "custom_http",
        "base_url": "https://vllm.internal.example.org",
        "auth": {
            "auth_type": "header",
            "header_name": "Authorization",
            "header_prefix": "Bearer ",
        },
        "custom_settings": {
            "static_models": ["deepseek-coder-33b"],
            "supported_capabilities": ["chat_completions", "streaming"],
        },
    }
    create_resp = await async_client.post("/api/v1/providers/custom", json=custom_payload)
    assert create_resp.status_code == 201
    created = create_resp.json()
    assert created["provider_id"] == "vllm-cluster"
    assert created["manifest"]["capabilities"]["chat_completions"] == "SUPPORTED"
    assert created["manifest"]["capabilities"]["tool_calling"] == "UNKNOWN"

    # Discover static models on custom provider
    disc_resp = await async_client.post("/api/v1/providers/vllm-cluster/discover-models")
    assert disc_resp.status_code == 200
    assert disc_resp.json()[0]["canonical_model_id"] == "vllm-cluster/deepseek-coder-33b"

    # Unregister custom provider
    del_resp = await async_client.delete("/api/v1/providers/custom/vllm-cluster")
    assert del_resp.status_code == 204

    check_resp = await async_client.get("/api/v1/providers/vllm-cluster")
    assert check_resp.status_code == 404

