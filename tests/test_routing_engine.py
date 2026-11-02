"""Deterministic unit and integration tests for Model Registry, Routing Policy Engine, and Intelligent Routing Engine."""

from __future__ import annotations

from fastapi.testclient import TestClient
import httpx
import pytest
import respx

from gateway.config.settings import GatewaySettings
from gateway.main import create_app


@pytest.fixture
def routing_client() -> TestClient:
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


def test_all_ten_semantic_model_aliases_route_deterministically(
    routing_client: TestClient,
) -> None:
    """Verifies all 10 semantic aliases resolve through Stage 1 & Stage 2 deterministically."""
    aliases_and_expected_modalities = {
        "auto": ("text",),
        "fast": ("text",),
        "coding": ("text",),
        "reasoning": ("text",),
        "balanced": ("text",),
        "classification": ("text",),
        "summarization": ("text",),
        "speech": ("audio",),
        "embedding": ("embedding",),
        "vision": ("text",),
    }

    aliases_resp = routing_client.get("/api/v1/routing/aliases")
    assert aliases_resp.status_code == 200
    registered_aliases = {a["alias"] for a in aliases_resp.json()}
    assert set(aliases_and_expected_modalities.keys()).issubset(registered_aliases)

    for alias_name, expected_out in aliases_and_expected_modalities.items():
        resp1 = routing_client.post(
            "/api/v1/routing/route",
            json={"model": alias_name},
        )
        assert resp1.status_code == 200, f"Failed routing alias {alias_name}: {resp1.text}"
        trace1 = resp1.json()
        assert trace1["requested_model_or_alias"] == alias_name
        assert trace1["selected_model"] != ""
        assert trace1["selected_provider"] != ""
        assert tuple(trace1["required_output_modalities"]) == expected_out
        assert len(trace1["ranking_scores"]) >= 1

        # Verify deterministic repeatability: identical input produces identical ranking and winner
        resp2 = routing_client.post(
            "/api/v1/routing/route",
            json={"model": alias_name},
        )
        assert resp2.status_code == 200
        trace2 = resp2.json()
        assert trace1["selected_model"] == trace2["selected_model"]
        assert [r["model_id"] for r in trace1["ranking_scores"]] == [
            r["model_id"] for r in trace2["ranking_scores"]
        ]


def test_stage1_hard_capability_and_constraint_filtering(
    routing_client: TestClient,
) -> None:
    """Verifies Stage 1 strictly filters out UNSUPPORTED and UNKNOWN capabilities, context length, cost, and blocked providers."""
    # 1. Require structured_outputs + vision on 'auto' alias:
    # groq/llama-3.3-70b-versatile has vision=UNSUPPORTED and structured_outputs=UNSUPPORTED
    # ollama/llama3.2 has vision=UNSUPPORTED and structured_outputs=UNKNOWN
    resp = routing_client.post(
        "/api/v1/routing/route",
        json={
            "model": "auto",
            "structured_output": True,
            "input_modalities": ["text", "image"],
            "estimated_input_tokens": 50000,
            "max_output_tokens": 4000,
        },
    )
    assert resp.status_code == 200
    trace = resp.json()
    filtered_by_model = {
        f["model_id"]: f["reason_code"] for f in trace["filtered_candidates"]
    }
    assert "groq/llama-3.3-70b-versatile" in filtered_by_model
    assert "ollama/llama3.2" in filtered_by_model
    assert "openai/o3-mini" in filtered_by_model
    # anthropic/claude-3-7-sonnet-20250219 has structured_outputs=UNKNOWN so it is strictly filtered!
    assert filtered_by_model["anthropic/claude-3-7-sonnet-20250219"] == "UNKNOWN_CAPABILITY"

    surviving_ids = {r["model_id"] for r in trace["ranking_scores"]}
    assert "openai/gpt-4o" in surviving_ids
    assert "gemini/gemini-2.0-flash" in surviving_ids

    # 2. Require context length > 250,000 tokens -> only Gemini models (1M context) survive
    ctx_resp = routing_client.post(
        "/api/v1/routing/route",
        json={
            "model": "auto",
            "estimated_input_tokens": 300000,
            "max_output_tokens": 4000,
        },
    )
    assert ctx_resp.status_code == 200
    ctx_trace = ctx_resp.json()
    assert ctx_trace["selected_provider"] == "gemini"
    ctx_filtered = {
        f["model_id"]: f["reason_code"] for f in ctx_trace["filtered_candidates"]
    }
    assert ctx_filtered["openai/gpt-4o"] == "INSUFFICIENT_CONTEXT_LENGTH"
    assert ctx_filtered["anthropic/claude-3-7-sonnet-20250219"] == "INSUFFICIENT_CONTEXT_LENGTH"

    # 3. Provider blocking and max_cost ceiling
    cost_resp = routing_client.post(
        "/api/v1/routing/route",
        json={
            "model": "auto",
            "blocked_providers": ["ollama", "groq"],
            "max_cost_per_1m_usd": 1.0,
        },
    )
    assert cost_resp.status_code == 200
    cost_trace = cost_resp.json()
    cost_filtered = {
        f["model_id"]: f["reason_code"] for f in cost_trace["filtered_candidates"]
    }
    assert cost_filtered["groq/llama-3.3-70b-versatile"] == "PROVIDER_BLOCKED"
    assert cost_filtered["anthropic/claude-3-7-sonnet-20250219"] == "COST_EXCEEDS_MAX"
    cost_filtered_model_survives = {
        r["model_id"] for r in cost_trace["ranking_scores"]
    }
    assert "openai/gpt-4o-mini" in cost_filtered_model_survives
    assert "gemini/gemini-2.0-flash" in cost_filtered_model_survives


def test_stage1_no_candidates_records_explainable_trace(
    routing_client: TestClient,
) -> None:
    """Verifies that when all candidates fail Stage 1, 422 GW_NO_CANDIDATE_FOUND is returned with a valid decision_id."""
    resp = routing_client.post(
        "/api/v1/routing/route",
        json={
            "model": "auto",
            "estimated_input_tokens": 9_000_000,  # Exceeds all model context windows
        },
    )
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["code"] == "GW_NO_CANDIDATE_FOUND"
    decision_id = err["gateway_metadata"]["decision_id"]

    explain_resp = routing_client.get(f"/api/v1/routing/explain/{decision_id}")
    assert explain_resp.status_code == 200
    trace = explain_resp.json()
    assert trace["decision_id"] == decision_id
    assert trace["selected_model"] == ""
    assert len(trace["filtered_candidates"]) > 0


def test_stage2_policy_weights_shift_winner_deterministically(
    routing_client: TestClient,
) -> None:
    """Verifies that changing routing policies deterministically changes the winning model."""
    # 1. Default 'balanced' policy has exact default weights (30/25/20/20/5)
    policies_resp = routing_client.get("/api/v1/routing/policies")
    assert policies_resp.status_code == 200
    policies = {p["name"]: p for p in policies_resp.json()}
    balanced_w = policies["balanced"]["weights"]
    assert balanced_w["quality"] == pytest.approx(0.30)
    assert balanced_w["reliability"] == pytest.approx(0.25)
    assert balanced_w["cost"] == pytest.approx(0.20)
    assert balanced_w["latency"] == pytest.approx(0.20)
    assert balanced_w["capacity"] == pytest.approx(0.05)

    # 2. Compare winners on 'auto' across quality_first, latency_optimized, and cost_optimized
    q_trace = routing_client.post(
        "/api/v1/routing/route",
        json={
            "model": "auto",
            "task": "coding",
            "policy": "quality_first",
            "blocked_providers": ["ollama"],
        },
    ).json()

    l_trace = routing_client.post(
        "/api/v1/routing/route",
        json={
            "model": "auto",
            "task": "general",
            "policy": "latency_optimized",
            "blocked_providers": ["ollama"],
        },
    ).json()

    c_trace = routing_client.post(
        "/api/v1/routing/route",
        json={
            "model": "auto",
            "task": "general",
            "policy": "cost_optimized",
            "blocked_providers": ["ollama"],
        },
    ).json()

    assert q_trace["selected_model"] == "anthropic/claude-3-7-sonnet-20250219"
    assert l_trace["selected_model"] == "groq/llama-3.3-70b-versatile"
    assert c_trace["selected_model"] == "gemini/gemini-2.0-flash"

    # 3. Create a custom policy via PUT /api/v1/routing/policies/openai_only
    put_resp = routing_client.put(
        "/api/v1/routing/policies/openai_only",
        json={
            "name": "openai_only",
            "description": "Strict OpenAI-only policy prioritizing quality",
            "weights": {
                "quality": 0.70,
                "reliability": 0.20,
                "cost": 0.05,
                "latency": 0.05,
                "capacity": 0.0,
            },
            "allowed_providers": ["openai"],
            "allow_cross_provider_fallback": False,
        },
    )
    assert put_resp.status_code == 200

    custom_trace = routing_client.post(
        "/api/v1/routing/route",
        json={"model": "auto", "policy": "openai_only"},
    ).json()
    assert custom_trace["selected_provider"] == "openai"
    assert custom_trace["selected_model"] == "openai/gpt-4o"
    for fb in custom_trace["fallback_decisions"]:
        assert fb["to_provider_id"] == "openai"


@respx.mock
def test_manual_model_registration_and_provider_sync_never_fabricate_capabilities(
    routing_client: TestClient,
) -> None:
    """Verifies manually registered and discovered models default omitted capabilities to UNKNOWN (never SUPPORTED)."""
    reg_resp = routing_client.post(
        "/api/v1/routing/models",
        json={
            "model_id": "openai/custom-finetune-v1",
            "provider_id": "openai",
            "provider_model_id": "ft:gpt-4o-mini:custom-v1",
            "display_name": "Custom Fine-Tuned Model",
            "capabilities": {
                "chat_completions": "supported",
                "streaming": "supported",
            },
        },
    )
    assert reg_resp.status_code == 201
    cap_resp = routing_client.get(
        "/api/v1/routing/models/openai/custom-finetune-v1/capabilities"
    )
    assert cap_resp.status_code == 200
    cap_data = cap_resp.json()
    assert cap_data["discovery_source"] == "manual"
    assert cap_data["context_length"] is None  # Never fabricated
    assert cap_data["capabilities"]["chat_completions"] == "SUPPORTED"
    assert cap_data["capabilities"]["tool_calling"] == "UNKNOWN"
    assert cap_data["capabilities"]["vision"] == "UNKNOWN"
    assert cap_data["capabilities"]["structured_outputs"] == "UNKNOWN"

    # Attempting to route a request requiring tool_calling to this model
    # must reject it in Stage 1 because tool_calling is UNKNOWN
    route_resp = routing_client.post(
        "/api/v1/routing/route",
        json={
            "model": "openai/custom-finetune-v1",
            "tools_count": 1,
        },
    )
    assert route_resp.status_code == 422
    err = route_resp.json()["error"]
    assert err["code"] == "GW_NO_CANDIDATE_FOUND"
    assert err["gateway_metadata"]["filtered_reasons"][0]["reason_code"] == "UNKNOWN_CAPABILITY"

    # Also test provider discovery sync into ModelRegistry via POST /api/v1/routing/models/sync/ollama
    respx.get("http://localhost:11434/api/tags").mock(
        return_value=httpx.Response(
            200,
            json={
                "models": [
                    {
                        "name": "qwen2.5-coder:7b",
                        "details": {"family": "qwen2", "parameter_size": "7B"},
                    }
                ]
            },
        )
    )
    sync_resp = routing_client.post("/api/v1/routing/models/sync/ollama")
    assert sync_resp.status_code == 200
    assert sync_resp.json()["synced_count"] == 1
    synced_cap = routing_client.get(
        "/api/v1/routing/models/ollama/qwen2.5-coder:7b/capabilities"
    ).json()
    assert synced_cap["discovery_source"] == "provider_discovery"
    assert synced_cap["context_length"] is None  # Ollama /api/tags does not return context window


@respx.mock
def test_end_to_end_alias_routing_in_chat_completions_and_explain_endpoint(
    routing_client: TestClient,
) -> None:
    """Verifies /v1/chat/completions routes model='coding' via RoutingEngine and exposes explainable trace without secrets."""
    raw_secret = "sk-ant-super-secret-routing-test-key-9999"
    cred_resp = routing_client.post(
        "/api/v1/admin/credentials",
        headers={"X-Gateway-Admin-Token": "test-admin-secret-token-1234567890"},
        json={
            "provider_id": "anthropic",
            "label": "anthropic-prod-primary",
            "secret": raw_secret,
        },
    )
    assert cred_resp.status_code == 201

    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "msg_routed_123",
                "type": "message",
                "role": "assistant",
                "model": "claude-3-7-sonnet-20250219",
                "content": [
                    {
                        "type": "text",
                        "text": "def fib(n): return n if n < 2 else fib(n-1)+fib(n-2)",
                    }
                ],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 18, "output_tokens": 24},
            },
        )
    )

    chat_resp = routing_client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_secret}"},
        json={
            "model": "coding",
            "messages": [
                {"role": "user", "content": "Write a Fibonacci function in Python"}
            ],
        },
    )
    assert chat_resp.status_code == 200
    assert chat_resp.headers["X-Gateway-Provider"] == "anthropic"
    assert chat_resp.headers["X-Gateway-Mode"] == "unified"
    decision_id = chat_resp.headers.get("X-Gateway-Route-Decision-Id")
    assert decision_id is not None

    explain_resp = routing_client.get(f"/api/v1/routing/explain/{decision_id}")
    assert explain_resp.status_code == 200
    trace = explain_resp.json()
    assert trace["requested_model_or_alias"] == "coding"
    assert trace["selected_model"] == "anthropic/claude-3-7-sonnet-20250219"
    assert trace["selected_provider"] == "anthropic"
    assert trace["selected_credential_ref"] is not None
    assert raw_secret not in str(trace)
    assert "anthropic-prod-primary" in trace["selected_credential_ref"]
