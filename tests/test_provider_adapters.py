"""Mocked HTTPX (respx) integration tests for all 7 built-in provider adapters and custom adapter."""

from __future__ import annotations

import httpx
from pydantic import SecretStr
import pytest
import respx

from gateway.capability_registry import CapabilityFlag, CapabilityState, ProtocolType, QuotaScopeType
from gateway.config import GatewaySettings
from gateway.core import HTTPXClientPoolManager, UpstreamProviderError
from gateway.core.canonical import (
    AdapterCredential,
    CanonicalInternalRequest,
    CanonicalMessage,
    NormalizedToolDefinition,
)
from gateway.provider_adapters import (
    AnthropicProviderAdapter,
    AuthConfiguration,
    CustomHTTPProviderAdapter,
    ElevenLabsProviderAdapter,
    GeminiProviderAdapter,
    GroqProviderAdapter,
    OllamaProviderAdapter,
    OpenAIProviderAdapter,
    OpenRouterProviderAdapter,
    ProviderConfig,
)


@pytest.fixture
def http_pool(test_settings: GatewaySettings) -> HTTPXClientPoolManager:
    return HTTPXClientPoolManager(test_settings)


# =============================================================================
# 1. OpenAI Provider Adapter Tests
# =============================================================================
@respx.mock
async def test_openai_adapter_discovery_execution_streaming_and_errors(
    http_pool: HTTPXClientPoolManager,
) -> None:
    adapter = OpenAIProviderAdapter(http_pool)
    cred = AdapterCredential(
        provider_id="openai",
        secret=SecretStr("sk-proj-test_openai_secret_key_123456"),
        organization_id="org-test123",
        project_id="proj-test456",
    )
    assert "sk-proj" not in repr(cred)

    # 1. Model Discovery
    respx.get("https://api.openai.com/v1/models").mock(
        return_value=httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {"id": "gpt-4o", "object": "model"},
                    {"id": "o3-mini", "object": "model"},
                    {"id": "text-embedding-3-small", "object": "model"},
                    {"id": "ft:custom-unknown-model", "object": "model"},
                ],
            },
        )
    )
    models = await adapter.discover_models(cred)
    assert len(models) == 4
    by_id = {m.provider_model_id: m for m in models}
    assert by_id["gpt-4o"].capabilities[CapabilityFlag.TOOL_CALLING] == CapabilityState.SUPPORTED
    assert by_id["o3-mini"].capabilities[CapabilityFlag.REASONING_TOKENS] == CapabilityState.SUPPORTED
    assert by_id["text-embedding-3-small"].capabilities[CapabilityFlag.EMBEDDINGS] == CapabilityState.SUPPORTED
    assert by_id["text-embedding-3-small"].capabilities[CapabilityFlag.CHAT_COMPLETIONS] == CapabilityState.UNSUPPORTED
    # Unknown fine-tuned model does not fabricate capabilities
    assert by_id["ft:custom-unknown-model"].capabilities[CapabilityFlag.TOOL_CALLING] == CapabilityState.UNKNOWN
    assert by_id["gpt-4o"].context_length is None

    # 2. Non-streaming Chat Execution with Tool Call & RateLimit headers
    chat_route = respx.post("https://api.openai.com/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            headers={
                "x-ratelimit-limit-requests": "5000",
                "x-ratelimit-remaining-requests": "4998",
                "x-ratelimit-reset-requests": "1m30s",
                "x-ratelimit-limit-tokens": "800000",
                "x-ratelimit-remaining-tokens": "799500",
                "x-ratelimit-reset-tokens": "250ms",
            },
            json={
                "id": "chatcmpl-123",
                "model": "gpt-4o-2024-08-06",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": "Checking weather...",
                            "tool_calls": [
                                {
                                    "id": "call_abc123",
                                    "type": "function",
                                    "function": {
                                        "name": "get_weather",
                                        "arguments": '{"city": "Tokyo"}',
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 25,
                    "completion_tokens": 15,
                    "total_tokens": 40,
                    "prompt_tokens_details": {"cached_tokens": 10},
                    "completion_tokens_details": {"reasoning_tokens": 5},
                },
            },
        )
    )
    req = CanonicalInternalRequest(
        requested_model="openai/gpt-4o",
        system_instructions=["You are helpful."],
        messages=[CanonicalMessage.from_text("user", "Weather in Tokyo?")],
        tools=[
            NormalizedToolDefinition(
                name="get_weather",
                description="Get city weather",
                parameters_schema={"type": "object", "properties": {"city": {"type": "string"}}},
            )
        ],
    )
    resp = await adapter.execute(req, cred)
    assert chat_route.called
    sent_headers = chat_route.calls.last.request.headers
    assert sent_headers["OpenAI-Organization"] == "org-test123"
    assert sent_headers["OpenAI-Project"] == "proj-test456"
    assert resp.finish_reason == "tool_calls"
    assert resp.output_text == "Checking weather..."
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].name == "get_weather"
    assert resp.tool_calls[0].arguments_dict == {"city": "Tokyo"}
    assert resp.usage.input_tokens == 25
    assert resp.usage.cached_input_tokens == 10
    assert resp.usage.reasoning_tokens == 5
    assert resp.rate_limit is not None
    assert resp.rate_limit.remaining_requests == 4998
    assert resp.rate_limit.reset_requests_seconds == 90.0
    assert resp.rate_limit.reset_tokens_seconds == 0.25

    # 3. Streaming Chat Execution
    sse_payload = (
        'data: {"id":"chatcmpl-s1","model":"gpt-4o","choices":[{"delta":{"content":"Hello "}}]}\n\n'
        'data: {"id":"chatcmpl-s1","model":"gpt-4o","choices":[{"delta":{"content":"world!"},"finish_reason":"stop"}]}\n\n'
        'data: {"id":"chatcmpl-s1","model":"gpt-4o","choices":[],"usage":{"prompt_tokens":8,"completion_tokens":2,"total_tokens":10}}\n\n'
        "data: [DONE]\n\n"
    )
    respx.post("https://api.openai.com/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse_payload.encode("utf-8"),
        )
    )
    events = [ev async for ev in adapter.execute_stream(req, cred)]
    text_chunks = [ev.text_delta for ev in events if ev.event_type == "text_delta"]
    assert "".join(t for t in text_chunks if t) == "Hello world!"
    usage_events = [ev for ev in events if ev.event_type == "usage"]
    assert usage_events[0].usage is not None
    assert usage_events[0].usage.total_tokens == 10

    # 4. Error Classification (401 invalid_api_key vs 429 insufficient_quota)
    err_401 = adapter.classify_error(
        401,
        {},
        {"error": {"code": "invalid_api_key", "message": "Incorrect API key provided"}},
    )
    assert err_401.retryable is False
    assert err_401.requires_credential_disable is True

    err_429_quota = adapter.classify_error(
        429,
        {},
        {"error": {"code": "insufficient_quota", "message": "You exceeded your current quota"}},
    )
    assert err_429_quota.error_type == "quota_exhausted_error"
    assert err_429_quota.requires_admin_attention is True
    assert err_429_quota.quota_scope == QuotaScopeType.ACCOUNT
    await http_pool.aclose_all()


# =============================================================================
# 2. Anthropic Provider Adapter Tests
# =============================================================================
@respx.mock
async def test_anthropic_adapter_discovery_execution_streaming_and_errors(
    http_pool: HTTPXClientPoolManager,
) -> None:
    adapter = AnthropicProviderAdapter(http_pool)
    cred = AdapterCredential(
        provider_id="anthropic",
        secret=SecretStr("sk-ant-api03-test_key_value_123456"),
    )

    manifest = adapter.get_capability_manifest()
    assert manifest.supports(CapabilityFlag.MESSAGES_API) is True
    assert manifest.supports(CapabilityFlag.EMBEDDINGS) is False

    # 1. Model Discovery
    respx.get("https://api.anthropic.com/v1/models").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "claude-3-7-sonnet-20250219",
                        "display_name": "Claude 3.7 Sonnet",
                    }
                ]
            },
        )
    )
    models = await adapter.discover_models(cred)
    assert len(models) == 1
    assert models[0].capabilities[CapabilityFlag.REASONING_TOKENS] == CapabilityState.SUPPORTED

    # 2. Non-streaming Messages API with thinking & tool_use
    msg_route = respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(
            200,
            headers={
                "anthropic-ratelimit-requests-limit": "1000",
                "anthropic-ratelimit-requests-remaining": "995",
                "anthropic-ratelimit-tokens-limit": "80000",
                "anthropic-ratelimit-tokens-remaining": "78000",
            },
            json={
                "id": "msg_01XYZ",
                "model": "claude-3-7-sonnet-20250219",
                "stop_reason": "tool_use",
                "content": [
                    {
                        "type": "thinking",
                        "thinking": "Let me query the database...",
                        "signature": "sig_123",
                    },
                    {"type": "text", "text": "Running query."},
                    {
                        "type": "tool_use",
                        "id": "toolu_01ABC",
                        "name": "run_sql",
                        "input": {"query": "SELECT 1"},
                    },
                ],
                "usage": {
                    "input_tokens": 50,
                    "output_tokens": 30,
                    "cache_read_input_tokens": 20,
                },
            },
        )
    )
    req = CanonicalInternalRequest(
        requested_model="anthropic/claude-3-7-sonnet-20250219",
        system_instructions=["Follow SQL safety rules."],
        messages=[CanonicalMessage.from_text("user", "Check DB")],
    )
    resp = await adapter.execute(req, cred)
    assert msg_route.calls.last.request.headers["anthropic-version"] == "2023-06-01"
    assert msg_route.calls.last.request.headers["x-api-key"] == "sk-ant-api03-test_key_value_123456"
    assert resp.finish_reason == "tool_calls"
    assert resp.output_text == "Running query."
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].name == "run_sql"
    assert resp.usage.cached_input_tokens == 20
    assert resp.rate_limit is not None
    assert resp.rate_limit.scope == QuotaScopeType.WORKSPACE
    assert resp.rate_limit.remaining_requests == 995

    # 3. Streaming Anthropic SSE
    sse_stream = (
        'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_s1","model":"claude-3-7-sonnet","usage":{"input_tokens":12}}}\n\n'
        'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Anthropic stream"}}\n\n'
        'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":4}}\n\n'
        'event: message_stop\ndata: {"type":"message_stop"}\n\n'
    )
    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse_stream.encode("utf-8"),
        )
    )
    events = [ev async for ev in adapter.execute_stream(req, cred)]
    assert any(ev.text_delta == "Anthropic stream" for ev in events)
    usage_ev = next(ev for ev in events if ev.event_type == "usage")
    assert usage_ev.usage is not None
    assert usage_ev.usage.total_tokens == 16

    # 4. Overloaded 529 error classification
    err_529 = adapter.classify_error(
        529,
        {"retry-after": "7"},
        {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}},
    )
    assert err_529.retryable is True
    assert err_529.retry_after_seconds == 7.0
    await http_pool.aclose_all()


# =============================================================================
# 3. Google Gemini Provider Adapter Tests
# =============================================================================
@respx.mock
async def test_gemini_adapter_discovery_execution_streaming_and_errors(
    http_pool: HTTPXClientPoolManager,
) -> None:
    adapter = GeminiProviderAdapter(http_pool)
    cred = AdapterCredential(
        provider_id="gemini",
        secret=SecretStr("AIzaSyTestGeminiApiKey1234567890abcdef"),
    )

    # 1. Model Discovery
    respx.get("https://generativelanguage.googleapis.com/v1beta/models").mock(
        return_value=httpx.Response(
            200,
            json={
                "models": [
                    {
                        "name": "models/gemini-2.5-pro",
                        "displayName": "Gemini 2.5 Pro",
                        "inputTokenLimit": 1048576,
                        "outputTokenLimit": 65536,
                        "supportedGenerationMethods": ["generateContent"],
                    },
                    {
                        "name": "models/text-embedding-004",
                        "displayName": "Text Embedding 004",
                        "inputTokenLimit": 2048,
                        "supportedGenerationMethods": ["embedContent"],
                    },
                ]
            },
        )
    )
    models = await adapter.discover_models(cred)
    assert len(models) == 2
    assert models[0].context_length == 1048576
    assert models[0].capabilities[CapabilityFlag.GEMINI_GENERATE_CONTENT] == CapabilityState.SUPPORTED
    assert models[1].capabilities[CapabilityFlag.EMBEDDINGS] == CapabilityState.SUPPORTED
    assert models[1].capabilities[CapabilityFlag.CHAT_COMPLETIONS] == CapabilityState.UNKNOWN

    # 2. generateContent execution
    respx.post(
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-pro:generateContent"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "responseId": "gem-resp-1",
                "candidates": [
                    {
                        "content": {
                            "role": "model",
                            "parts": [{"text": "Gemini response text"}],
                        },
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 18,
                    "candidatesTokenCount": 7,
                    "totalTokenCount": 29,
                    "thoughtsTokenCount": 4,
                },
            },
        )
    )
    req = CanonicalInternalRequest(
        requested_model="gemini/gemini-2.5-pro",
        messages=[CanonicalMessage.from_text("user", "Hello Gemini")],
    )
    resp = await adapter.execute(req, cred)
    assert resp.output_text == "Gemini response text"
    assert resp.usage.input_tokens == 18
    assert resp.usage.reasoning_tokens == 4

    # 3. Error classification (429 RESOURCE_EXHAUSTED)
    err = adapter.classify_error(
        429,
        {},
        {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "Quota exceeded"}},
    )
    assert err.error_type == "rate_limit_error"
    assert err.retryable is True
    await http_pool.aclose_all()


# =============================================================================
# 4. OpenRouter Provider Adapter Tests
# =============================================================================
@respx.mock
async def test_openrouter_adapter_pricing_discovery_and_402_error(
    http_pool: HTTPXClientPoolManager,
) -> None:
    adapter = OpenRouterProviderAdapter(http_pool)
    cred = AdapterCredential(
        provider_id="openrouter",
        secret=SecretStr("sk-or-v1-test_openrouter_key_123456"),
    )

    respx.get("https://openrouter.ai/api/v1/models").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "anthropic/claude-3.5-sonnet",
                        "name": "Anthropic: Claude 3.5 Sonnet",
                        "context_length": 200000,
                        "pricing": {"prompt": "0.000003", "completion": "0.000015"},
                        "supported_parameters": ["tools", "structured_outputs"],
                        "architecture": {"input_modalities": ["text", "image"]},
                        "top_provider": {"max_completion_tokens": 8192},
                    }
                ]
            },
        )
    )
    models = await adapter.discover_models(cred)
    assert len(models) == 1
    m = models[0]
    assert m.context_length == 200000
    assert m.max_output_tokens == 8192
    assert m.input_cost_per_1m_usd == 3.0
    assert m.output_cost_per_1m_usd == 15.0
    assert m.capabilities[CapabilityFlag.TOOL_CALLING] == CapabilityState.SUPPORTED
    assert m.capabilities[CapabilityFlag.STRUCTURED_OUTPUTS] == CapabilityState.SUPPORTED
    assert m.capabilities[CapabilityFlag.VISION] == CapabilityState.SUPPORTED

    err_402 = adapter.classify_error(
        402, {}, {"error": {"message": "Insufficient credits"}}
    )
    assert err_402.error_type == "quota_exhausted_error"
    assert err_402.requires_admin_attention is True
    await http_pool.aclose_all()


# =============================================================================
# 5. Groq Provider Adapter Tests
# =============================================================================
@respx.mock
async def test_groq_adapter_discovery_and_capabilities(
    http_pool: HTTPXClientPoolManager,
) -> None:
    adapter = GroqProviderAdapter(http_pool)
    cred = AdapterCredential(
        provider_id="groq",
        secret=SecretStr("gsk_test_groq_key_1234567890abcdef"),
    )

    manifest = adapter.get_capability_manifest()
    assert manifest.supports(CapabilityFlag.CHAT_COMPLETIONS) is True
    assert manifest.supports(CapabilityFlag.EMBEDDINGS) is False

    respx.get("https://api.groq.com/openai/v1/models").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "llama-3.3-70b-versatile",
                        "active": True,
                        "context_window": 128000,
                        "max_completion_tokens": 32768,
                    },
                    {
                        "id": "whisper-large-v3-turbo",
                        "active": True,
                        "context_window": 448,
                    },
                ]
            },
        )
    )
    models = await adapter.discover_models(cred)
    assert len(models) == 2
    assert models[0].context_length == 128000
    assert models[0].max_output_tokens == 32768
    assert models[1].capabilities[CapabilityFlag.STT] == CapabilityState.SUPPORTED
    assert models[1].capabilities[CapabilityFlag.CHAT_COMPLETIONS] == CapabilityState.UNSUPPORTED
    await http_pool.aclose_all()


# =============================================================================
# 6. Ollama Provider Adapter Tests
# =============================================================================
@respx.mock
async def test_ollama_adapter_discovery_chat_and_ndjson_streaming(
    http_pool: HTTPXClientPoolManager,
) -> None:
    adapter = OllamaProviderAdapter(http_pool)
    cred = AdapterCredential(provider_id="ollama", secret=SecretStr(""))

    respx.get("http://localhost:11434/api/tags").mock(
        return_value=httpx.Response(
            200,
            json={"models": [{"name": "qwen2.5-coder:7b"}, {"name": "nomic-embed-text"}]},
        )
    )
    models = await adapter.discover_models(cred)
    assert len(models) == 2
    assert models[0].input_cost_per_1m_usd == 0.0
    assert models[0].context_length is None  # Not fabricated from /api/tags
    assert models[1].capabilities[CapabilityFlag.EMBEDDINGS] == CapabilityState.SUPPORTED

    # Non-streaming /api/chat
    respx.post("http://localhost:11434/api/chat").mock(
        return_value=httpx.Response(
            200,
            json={
                "model": "qwen2.5-coder:7b",
                "message": {"role": "assistant", "content": "def add(a, b): return a + b"},
                "done": True,
                "prompt_eval_count": 14,
                "eval_count": 9,
            },
        )
    )
    req = CanonicalInternalRequest(
        requested_model="ollama/qwen2.5-coder:7b",
        messages=[CanonicalMessage.from_text("user", "Write add function")],
    )
    resp = await adapter.execute(req, cred)
    assert "def add" in resp.output_text
    assert resp.usage.input_tokens == 14
    assert resp.usage.output_tokens == 9

    # NDJSON streaming /api/chat
    ndjson_stream = (
        '{"model":"qwen2.5-coder:7b","message":{"role":"assistant","content":"part1 "},"done":false}\n'
        '{"model":"qwen2.5-coder:7b","message":{"role":"assistant","content":"part2"},"done":true,"prompt_eval_count":5,"eval_count":4}\n'
    )
    respx.post("http://localhost:11434/api/chat").mock(
        return_value=httpx.Response(200, content=ndjson_stream.encode("utf-8"))
    )
    events = [ev async for ev in adapter.execute_stream(req, cred)]
    assert "".join(ev.text_delta or "" for ev in events if ev.event_type == "text_delta") == "part1 part2"
    await http_pool.aclose_all()


# =============================================================================
# 7. ElevenLabs Provider Adapter Tests
# =============================================================================
@respx.mock
async def test_elevenlabs_adapter_tts_stt_voices_and_errors(
    http_pool: HTTPXClientPoolManager,
) -> None:
    adapter = ElevenLabsProviderAdapter(http_pool)
    cred = AdapterCredential(
        provider_id="elevenlabs",
        secret=SecretStr("xi_test_elevenlabs_secret_key"),
    )

    manifest = adapter.get_capability_manifest()
    assert manifest.supports(CapabilityFlag.TTS) is True
    assert manifest.supports(CapabilityFlag.STT) is True
    assert manifest.supports(CapabilityFlag.CHAT_COMPLETIONS) is False

    # 1. Model & Voice Discovery
    respx.get("https://api.elevenlabs.io/v1/models").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "model_id": "eleven_multilingual_v2",
                    "name": "Eleven Multilingual v2",
                    "can_do_text_to_speech": True,
                    "maximum_text_length_per_request": 10000,
                }
            ],
        )
    )
    models = await adapter.discover_models(cred)
    assert len(models) == 1
    assert models[0].context_length == 10000

    respx.get("https://api.elevenlabs.io/v1/voices").mock(
        return_value=httpx.Response(
            200,
            json={"voices": [{"voice_id": "voice_1", "name": "Rachel"}]},
        )
    )
    voices = await adapter.discover_voices(cred)
    assert len(voices) == 1
    assert voices[0]["name"] == "Rachel"

    # 2. TTS Execution
    respx.post("https://api.elevenlabs.io/v1/text-to-speech/voice_1").mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "audio/mpeg", "x-character-count": "11"},
            content=b"ID3_FAKE_MP3_BYTES",
        )
    )
    tts_req = CanonicalInternalRequest(
        source_protocol=ProtocolType.ELEVENLABS_TTS,
        requested_model="elevenlabs/eleven_multilingual_v2",
        voice_id="voice_1",
        input_texts=["Hello world"],
    )
    tts_resp = await adapter.execute(tts_req, cred)
    assert tts_resp.audio_content == b"ID3_FAKE_MP3_BYTES"
    assert tts_resp.usage.characters == 11

    # 3. Error classification (Concurrency vs Quota)
    err_conc = adapter.classify_error(
        429,
        {},
        {"detail": {"status": "too_many_concurrent_requests", "message": "Concurrency limit reached"}},
    )
    assert err_conc.quota_scope == QuotaScopeType.CONCURRENCY
    assert err_conc.retryable is True
    await http_pool.aclose_all()


# =============================================================================
# 8. Custom HTTP Provider Adapter & SSRF Guard Tests
# =============================================================================
@respx.mock
async def test_custom_http_provider_adapter_and_ssrf_guard(
    http_pool: HTTPXClientPoolManager,
) -> None:
    # SSRF guard blocks loopback/private IP when allow_local_network=False
    bad_cfg = ProviderConfig(
        provider_id="ssrf-attempt",
        display_name="SSRF Attempt",
        adapter_type="custom_http",
        base_url="http://169.254.169.254/latest/meta-data",
        allow_local_network=False,
    )
    bad_adapter = CustomHTTPProviderAdapter(http_pool, bad_cfg)
    validation = bad_adapter.validate_configuration()
    assert validation.valid is False
    assert any("SSRF guard" in e for e in validation.errors)

    # Valid custom REST provider with custom dot-path extraction
    good_cfg = ProviderConfig(
        provider_id="acme-ai",
        display_name="Acme Internal LLM",
        adapter_type="custom_http",
        base_url="https://llm.acme.example.com",
        auth=AuthConfiguration(
            auth_type="header",
            header_name="X-Acme-Token",
            header_prefix="Token ",
        ),
        custom_settings={
            "chat_endpoint": "/api/generate",
            "response_text_path": "result.output_text",
            "static_models": ["acme-70b-instruct"],
            "supported_capabilities": ["chat_completions", "streaming"],
        },
    )
    custom_adapter = CustomHTTPProviderAdapter(http_pool, good_cfg)
    assert custom_adapter.validate_configuration().valid is True

    models = await custom_adapter.discover_models()
    assert len(models) == 1
    assert models[0].canonical_model_id == "acme-ai/acme-70b-instruct"
    # Undeclared capability defaults to UNKNOWN
    assert models[0].capabilities[CapabilityFlag.TOOL_CALLING] == CapabilityState.UNKNOWN

    respx.post("https://llm.acme.example.com/api/generate").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "acme-1",
                "model": "acme-70b-instruct",
                "result": {"output_text": "Hello from Acme AI!"},
                "usage": {"input_tokens": 6, "output_tokens": 5},
            },
        )
    )
    cred = AdapterCredential(provider_id="acme-ai", secret=SecretStr("acme_secret_999"))
    req = CanonicalInternalRequest(
        requested_model="acme-ai/acme-70b-instruct",
        messages=[CanonicalMessage.from_text("user", "Hi Acme")],
    )
    resp = await custom_adapter.execute(req, cred)
    assert resp.output_text == "Hello from Acme AI!"
    assert resp.usage.total_tokens == 11
    await http_pool.aclose_all()

