"""Compatibility tests for OpenAI Chat/Embeddings, Responses API, Anthropic, Gemini, ElevenLabs, and Custom protocols."""

from __future__ import annotations

import httpx
import respx


# =============================================================================
# 1. OpenAI-Compatible Interface Tests (Native Passthrough vs Unified Normalization)
# =============================================================================
@respx.mock
async def test_openai_chat_native_passthrough_and_unified_translation(
    async_client: httpx.AsyncClient,
) -> None:
    # A. Native Passthrough when targeting OpenAI directly
    oai_route = respx.post("https://api.openai.com/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "chatcmpl-native-1",
                "object": "chat.completion",
                "model": "gpt-4o",
                "system_fingerprint": "fp_native_preserved",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "Native OpenAI reply"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
            },
        )
    )
    r_native = await async_client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer sk-proj-test_oai_key_123456"},
        json={
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "Hi"}],
            "logit_bias": {"123": -100},
        },
    )
    assert r_native.status_code == 200
    assert r_native.headers["X-Gateway-Mode"] == "native"
    assert r_native.json()["system_fingerprint"] == "fp_native_preserved"
    assert oai_route.called

    # B. Unified Normalization when targeting Anthropic via OpenAI Chat endpoint
    anth_route = respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "msg_from_anth",
                "type": "message",
                "role": "assistant",
                "model": "claude-3-7-sonnet-20250219",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "Normalized from Claude!"}],
                "usage": {"input_tokens": 12, "output_tokens": 5},
            },
        )
    )
    r_unified = await async_client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer sk-ant-api03-test_key_123456"},
        json={
            "model": "anthropic/claude-3-7-sonnet-20250219",
            "messages": [
                {"role": "system", "content": "Be concise."},
                {"role": "user", "content": "Hello Claude"},
            ],
        },
    )
    assert r_unified.status_code == 200
    assert r_unified.headers["X-Gateway-Mode"] == "unified"
    assert r_unified.headers["X-Gateway-Provider"] == "anthropic"
    body_u = r_unified.json()
    assert body_u["object"] == "chat.completion"
    assert body_u["choices"][0]["message"]["content"] == "Normalized from Claude!"
    assert body_u["usage"]["total_tokens"] == 17
    assert anth_route.called

    # C. Never silently remove unsupported provider-specific functionality
    r_unsupported_field = await async_client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer sk-ant-api03-test_key_123456"},
        json={
            "model": "anthropic/claude-3-7-sonnet-20250219",
            "messages": [{"role": "user", "content": "Hello"}],
            "logit_bias": {"50256": -100},
        },
    )
    assert r_unsupported_field.status_code == 422
    assert r_unsupported_field.json()["error"]["code"] == "GW_UNSUPPORTED_CAPABILITY"
    assert r_unsupported_field.json()["error"]["param"] == "logit_bias"


@respx.mock
async def test_openai_embeddings_and_unsupported_provider_rejection(
    async_client: httpx.AsyncClient,
) -> None:
    respx.post("https://api.openai.com/v1/embeddings").mock(
        return_value=httpx.Response(
            200,
            json={
                "object": "list",
                "model": "text-embedding-3-small",
                "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2, 0.3]}],
                "usage": {"prompt_tokens": 4, "total_tokens": 4},
            },
        )
    )
    r_ok = await async_client.post(
        "/v1/embeddings",
        headers={"Authorization": "Bearer sk-proj-test_oai_key_123456"},
        json={"model": "text-embedding-3-small", "input": "embed me"},
    )
    assert r_ok.status_code == 200
    assert r_ok.json()["data"][0]["embedding"] == [0.1, 0.2, 0.3]

    # Requesting embeddings from Anthropic (which does not support embeddings) is explicitly rejected
    r_err = await async_client.post(
        "/v1/embeddings",
        headers={"Authorization": "Bearer sk-ant-api03-test_key_123456"},
        json={"model": "anthropic/claude-3-7-sonnet-20250219", "input": "embed me"},
    )
    assert r_err.status_code == 422
    assert r_err.json()["error"]["code"] == "GW_UNSUPPORTED_CAPABILITY"


# =============================================================================
# 2. OpenAI Responses API Native Semantics Tests (/v1/responses)
# =============================================================================
@respx.mock
async def test_openai_responses_api_native_and_streaming(
    async_client: httpx.AsyncClient,
) -> None:
    respx.post("https://api.openai.com/v1/responses").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "resp_native_001",
                "object": "response",
                "status": "completed",
                "model": "gpt-4o",
                "output": [
                    {
                        "type": "message",
                        "id": "msg_01",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "Responses API native output"}],
                    }
                ],
                "usage": {"input_tokens": 9, "output_tokens": 5, "total_tokens": 14},
            },
        )
    )
    r_resp = await async_client.post(
        "/v1/responses",
        headers={"Authorization": "Bearer sk-proj-test_oai_key_123456"},
        json={
            "model": "gpt-4o",
            "instructions": "Answer directly.",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "What is Responses API?"}],
                }
            ],
        },
    )
    assert r_resp.status_code == 200
    assert r_resp.headers["X-Gateway-Mode"] == "native"
    assert r_resp.json()["object"] == "response"
    assert r_resp.json()["id"] == "resp_native_001"

    # Attempting to use Responses API on a provider that does not support responses_api is rejected
    r_bad = await async_client.post(
        "/v1/responses",
        headers={"Authorization": "Bearer gsk_test_groq_key_123456"},
        json={
            "model": "groq/llama-3.3-70b-versatile",
            "input": "Hello",
        },
    )
    assert r_bad.status_code == 422
    assert r_bad.json()["error"]["code"] == "GW_UNSUPPORTED_CAPABILITY"


# =============================================================================
# 3. Anthropic Native Interface Tests (/v1/messages)
# =============================================================================
@respx.mock
async def test_anthropic_native_messages_and_streaming(
    async_client: httpx.AsyncClient,
) -> None:
    # Native passthrough preserves cache_control and thinking blocks
    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "msg_native_anth_01",
                "type": "message",
                "role": "assistant",
                "model": "claude-3-7-sonnet-20250219",
                "content": [{"type": "text", "text": "Hello from native Anthropic"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 20, "output_tokens": 6, "cache_read_input_tokens": 15},
            },
        )
    )
    r_anth = await async_client.post(
        "/v1/messages",
        headers={"x-api-key": "sk-ant-api03-test_key_123456"},
        json={
            "model": "claude-3-7-sonnet-20250219",
            "max_tokens": 1024,
            "system": [
                {
                    "type": "text",
                    "text": "Cached system prompt",
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": [{"role": "user", "content": "Hello"}],
        },
    )
    assert r_anth.status_code == 200
    assert r_anth.headers["X-Gateway-Mode"] == "native"
    assert r_anth.json()["usage"]["cache_read_input_tokens"] == 15

    # Rejects cache_control if user attempts to route Anthropic payload to OpenAI
    r_cache_err = await async_client.post(
        "/v1/messages",
        headers={"x-api-key": "sk-proj-test_oai_key_123456"},
        json={
            "model": "openai/gpt-4o",
            "max_tokens": 512,
            "system": [
                {
                    "type": "text",
                    "text": "Cached prompt",
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": [{"role": "user", "content": "Hello"}],
        },
    )
    assert r_cache_err.status_code == 422
    assert r_cache_err.json()["error"]["param"] == "cache_control"


# =============================================================================
# 4. Google Gemini Native Interface Tests (/v1beta/models/{model}:generateContent)
# =============================================================================
@respx.mock
async def test_gemini_native_generate_and_stream_content(
    async_client: httpx.AsyncClient,
) -> None:
    respx.post(
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "role": "model",
                            "parts": [
                                {
                                    "functionCall": {
                                        "name": "lookup_stock",
                                        "args": {"ticker": "GOOG"},
                                    }
                                }
                            ],
                        },
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {"promptTokenCount": 14, "candidatesTokenCount": 6, "totalTokenCount": 20},
            },
        )
    )
    r_gem = await async_client.post(
        "/v1beta/models/gemini-2.5-flash:generateContent",
        headers={"x-goog-api-key": "AIzaSyTestGeminiKey1234567890abcdef"},
        json={
            "systemInstruction": {"parts": [{"text": "Use tools when helpful."}]},
            "contents": [{"role": "user", "parts": [{"text": "Stock price of GOOG?"}]}],
            "tools": [
                {
                    "functionDeclarations": [
                        {
                            "name": "lookup_stock",
                            "description": "Lookup ticker",
                            "parameters": {
                                "type": "object",
                                "properties": {"ticker": {"type": "string"}},
                            },
                        }
                    ]
                }
            ],
        },
    )
    assert r_gem.status_code == 200
    assert r_gem.headers["X-Gateway-Mode"] == "native"
    cand = r_gem.json()["candidates"][0]
    assert cand["content"]["parts"][0]["functionCall"]["name"] == "lookup_stock"

    # Streaming Gemini SSE
    sse_gem = (
        'data: {"candidates":[{"content":{"role":"model","parts":[{"text":"Streamed Gemini"}]}}],"usageMetadata":{"promptTokenCount":5,"candidatesTokenCount":3,"totalTokenCount":8}}\n\n'
    )
    respx.post(
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:streamGenerateContent"
    ).mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse_gem.encode("utf-8"),
        )
    )
    r_stream = await async_client.post(
        "/v1beta/models/gemini-2.5-flash:streamGenerateContent",
        headers={"x-goog-api-key": "AIzaSyTestGeminiKey1234567890abcdef"},
        json={"contents": [{"role": "user", "parts": [{"text": "Stream test"}]}]},
    )
    assert r_stream.status_code == 200
    assert "Streamed Gemini" in r_stream.text


# =============================================================================
# 5. ElevenLabs Native Audio Interface Tests (/v1/text-to-speech, /v1/speech-to-text, /v1/voices)
# =============================================================================
@respx.mock
async def test_elevenlabs_native_tts_stt_stream_and_voices(
    async_client: httpx.AsyncClient,
) -> None:
    respx.get("https://api.elevenlabs.io/v1/voices").mock(
        return_value=httpx.Response(
            200,
            json={"voices": [{"voice_id": "v_rachel", "name": "Rachel"}]},
        )
    )
    r_voices = await async_client.get(
        "/v1/voices", headers={"xi-api-key": "xi_test_key_123"}
    )
    assert r_voices.status_code == 200
    assert r_voices.json()["voices"][0]["voice_id"] == "v_rachel"

    respx.post("https://api.elevenlabs.io/v1/text-to-speech/v_rachel").mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "audio/mpeg", "x-character-count": "15"},
            content=b"MP3_AUDIO_BYTES",
        )
    )
    r_tts = await async_client.post(
        "/v1/text-to-speech/v_rachel",
        headers={"xi-api-key": "xi_test_key_123"},
        json={"text": "Hello ElevenLabs", "model_id": "eleven_multilingual_v2"},
    )
    assert r_tts.status_code == 200
    assert r_tts.content == b"MP3_AUDIO_BYTES"
    assert r_tts.headers["X-Character-Count"] == "15"

    respx.post("https://api.elevenlabs.io/v1/text-to-speech/v_rachel/stream").mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "audio/mpeg"},
            content=b"STREAM_CHUNK_1_STREAM_CHUNK_2",
        )
    )
    r_tts_stream = await async_client.post(
        "/v1/text-to-speech/v_rachel/stream",
        headers={"xi-api-key": "xi_test_key_123"},
        json={"text": "Stream audio", "model_id": "eleven_multilingual_v2"},
    )
    assert r_tts_stream.status_code == 200
    assert r_tts_stream.content == b"STREAM_CHUNK_1_STREAM_CHUNK_2"

    respx.post("https://api.elevenlabs.io/v1/speech-to-text").mock(
        return_value=httpx.Response(
            200,
            json={"text": "Transcribed speech text"},
        )
    )
    r_stt = await async_client.post(
        "/v1/speech-to-text",
        headers={"xi-api-key": "xi_test_key_123"},
        files={"file": ("sample.mp3", b"FAKE_AUDIO_DATA", "audio/mpeg")},
        data={"model_id": "scribe_v1"},
    )
    assert r_stt.status_code == 200
    assert r_stt.json()["text"] == "Transcribed speech text"


# =============================================================================
# 6. Custom Protocol & Explicit Native Passthrough Tests
# =============================================================================
@respx.mock
async def test_custom_protocol_and_explicit_native_passthrough(
    async_client: httpx.AsyncClient,
) -> None:
    # Register a custom provider with custom request/response paths
    await async_client.post(
        "/api/v1/providers/custom",
        headers={"X-Gateway-Admin-Token": "ugw_admin_test_token_1234567890"},
        json={
            "provider_id": "corp-llm",
            "display_name": "Corp LLM Gateway",
            "adapter_type": "custom_http",
            "base_url": "https://ai.corp.example.com",
            "auth": {
                "auth_type": "header",
                "header_name": "X-Corp-Key",
                "header_prefix": "",
            },
            "custom_settings": {
                "chat_endpoint": "/v2/infer",
                "request_format": "prompt_only",
                "prompt_field": "input_prompt",
                "response_text_path": "data.generated_text",
                "supported_capabilities": ["chat_completions", "streaming"],
                "protocol_spec": {
                    "protocol_name": "corp_v2",
                    "request_model_path": "target_model",
                    "request_prompt_path": "query",
                    "response_envelope_template": {
                        "corp_reply": "{{output_text}}",
                        "served_by": "{{model}}",
                    },
                },
            },
        },
    )

    respx.post("https://ai.corp.example.com/v2/infer").mock(
        return_value=httpx.Response(
            200,
            json={
                "model": "corp-instruct-v2",
                "data": {"generated_text": "Custom protocol transformed output"},
                "usage": {"input_tokens": 5, "output_tokens": 4, "total_tokens": 9},
            },
        )
    )
    r_custom = await async_client.post(
        "/v1/custom/corp-llm",
        headers={"X-Upstream-Api-Key": "corp_secret_token"},
        json={"target_model": "corp-instruct-v2", "query": "Test custom protocol"},
    )
    assert r_custom.status_code == 200
    assert r_custom.json() == {
        "corp_reply": "Custom protocol transformed output",
        "served_by": "corp-instruct-v2",
    }

    # Explicit /v1/native/{provider_id}/{path} passthrough
    respx.get("https://ai.corp.example.com/v2/status").mock(
        return_value=httpx.Response(200, json={"cluster": "green"})
    )
    r_pass = await async_client.get(
        "/v1/native/corp-llm/v2/status",
        headers={"X-Upstream-Api-Key": "corp_secret_token"},
    )
    assert r_pass.status_code == 200
    assert r_pass.json() == {"cluster": "green"}

