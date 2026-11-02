"""Protocol Compatibility Layer & Native Passthrough FastAPI router."""

from __future__ import annotations

import json
import time
from typing import Any

from fastapi import APIRouter, File, Form, Request, Response, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse

from gateway.core.canonical import NativePassthroughRequest
from gateway.core.exceptions import UnsupportedCapabilityError
from gateway.protocol_adapters.anthropic_protocol import (
    AnthropicMessagesProtocolAdapter,
)
from gateway.protocol_adapters.base import (
    enforce_capability_compatibility,
    extract_request_credential,
    resolve_target_provider_and_model,
)
from gateway.protocol_adapters.custom_protocol import (
    CustomProtocolAdapter,
    CustomProtocolSpec,
)
from gateway.protocol_adapters.elevenlabs_protocol import ElevenLabsProtocolAdapter
from gateway.protocol_adapters.gemini_protocol import GeminiNativeProtocolAdapter
from gateway.protocol_adapters.openai_chat_protocol import OpenAIChatProtocolAdapter
from gateway.protocol_adapters.openai_responses_protocol import (
    OpenAIResponsesProtocolAdapter,
)
from gateway.provider_registry.registry import ProviderRegistry

protocol_router = APIRouter(tags=["Universal Protocol Compatibility"])

openai_chat_proto = OpenAIChatProtocolAdapter()
openai_responses_proto = OpenAIResponsesProtocolAdapter()
anthropic_proto = AnthropicMessagesProtocolAdapter()
gemini_proto = GeminiNativeProtocolAdapter()
elevenlabs_proto = ElevenLabsProtocolAdapter()


def _get_registry(request: Request) -> ProviderRegistry:
    return request.app.state.provider_registry


def _is_unified_mode_requested(
    request: Request, target_provider: str, native_provider: str
) -> bool:
    """Determines whether to normalize through the unified layer or use native passthrough.

    Uses native passthrough when the user calls a provider's native protocol for that provider,
    and uses normalization only when the user explicitly chooses the unified interface
    (via X-Gateway-Mode: unified or by specifying a cross-provider model like 'anthropic/...').
    """
    mode_header = (request.headers.get("X-Gateway-Mode") or "").strip().lower()
    if mode_header == "unified":
        return True
    if mode_header == "native":
        if target_provider != native_provider:
            raise UnsupportedCapabilityError(
                f"Cannot execute native {native_provider!r} protocol directly on {target_provider!r} when X-Gateway-Mode='native'",
                param="model",
            )
        return False
    return target_provider != native_provider


# =============================================================================
# 1. OpenAI-Compatible Interface (/v1/chat/completions, /v1/embeddings, /v1/models)
# =============================================================================
@protocol_router.post("/v1/chat/completions", summary="OpenAI-Compatible Chat Completions")
async def openai_chat_completions(request: Request) -> Response:
    registry = _get_registry(request)
    body_bytes = await request.body()
    payload = await request.json()

    canonical_req = openai_chat_proto.parse_request(payload)
    target_provider, clean_model = resolve_target_provider_and_model(
        canonical_req.requested_model, default_provider="openai"
    )
    adapter = registry.get_adapter(target_provider)
    manifest = adapter.get_capability_manifest()
    enforce_capability_compatibility(canonical_req, manifest)
    credential = extract_request_credential(request, target_provider)

    # Native passthrough if targeting OpenAI directly without unified mode override and non-streaming
    if (
        not _is_unified_mode_requested(request, target_provider, "openai")
        and not canonical_req.stream
    ):
        native_req = NativePassthroughRequest(
            provider_id="openai",
            method="POST",
            upstream_path="/chat/completions",
            headers=dict(request.headers),
            body_bytes=body_bytes,
        )
        native_resp = await adapter.execute_native(native_req, credential=credential)
        if native_resp.status_code >= 400:
            adapter._raise_for_upstream_error(
                native_resp.status_code, native_resp.headers, native_resp.body_bytes
            )
        return Response(
            content=native_resp.body_bytes,
            status_code=native_resp.status_code,
            media_type="application/json",
            headers={
                "X-Gateway-Provider": target_provider,
                "X-Gateway-Model": clean_model,
                "X-Gateway-Mode": "native",
            },
        )

    if canonical_req.stream:
        stream_iter = adapter.execute_stream(canonical_req, credential)
        return StreamingResponse(
            openai_chat_proto.format_stream_events(stream_iter, clean_model),
            media_type="text/event-stream",
            headers={
                "X-Gateway-Provider": target_provider,
                "X-Gateway-Model": clean_model,
            },
        )

    canonical_resp = await adapter.execute(canonical_req, credential)
    formatted = openai_chat_proto.format_response(canonical_resp)
    return JSONResponse(
        content=formatted,
        headers={
            "X-Gateway-Provider": target_provider,
            "X-Gateway-Model": canonical_resp.model,
            "X-Gateway-Mode": "unified",
        },
    )


@protocol_router.post("/v1/embeddings", summary="OpenAI-Compatible Embeddings")
async def openai_embeddings(request: Request) -> Response:
    registry = _get_registry(request)
    payload = await request.json()
    canonical_req = openai_chat_proto.parse_embeddings_request(payload)
    target_provider, clean_model = resolve_target_provider_and_model(
        canonical_req.requested_model, default_provider="openai"
    )
    adapter = registry.get_adapter(target_provider)
    manifest = adapter.get_capability_manifest()
    enforce_capability_compatibility(canonical_req, manifest)
    credential = extract_request_credential(request, target_provider)

    canonical_resp = await adapter.execute(canonical_req, credential)
    formatted = openai_chat_proto.format_response(canonical_resp)
    return JSONResponse(
        content=formatted,
        headers={
            "X-Gateway-Provider": target_provider,
            "X-Gateway-Model": clean_model,
        },
    )


@protocol_router.get("/v1/models", summary="OpenAI-Compatible Model Listing")
async def openai_list_models(request: Request) -> dict[str, Any]:
    registry = _get_registry(request)
    data: list[dict[str, Any]] = []
    created_ts = int(time.time())
    for adapter in registry.list_adapters(enabled_only=True):
        cached = registry.get_cached_models(adapter.provider_id)
        for m in cached:
            data.append(
                {
                    "id": m.canonical_model_id,
                    "object": "model",
                    "created": created_ts,
                    "owned_by": m.provider_id,
                    "x_gateway_capabilities": {
                        k.value: v.value for k, v in m.capabilities.items()
                    },
                    "x_gateway_context_length": m.context_length,
                }
            )
    return {"object": "list", "data": data}


# =============================================================================
# 2. OpenAI Responses API Interface (/v1/responses)
# =============================================================================
@protocol_router.post("/v1/responses", summary="OpenAI Native Responses API")
async def openai_responses_api(request: Request) -> Response:
    registry = _get_registry(request)
    body_bytes = await request.body()
    payload = await request.json()

    canonical_req = openai_responses_proto.parse_request(payload)
    target_provider, clean_model = resolve_target_provider_and_model(
        canonical_req.requested_model, default_provider="openai"
    )
    adapter = registry.get_adapter(target_provider)
    manifest = adapter.get_capability_manifest()
    enforce_capability_compatibility(canonical_req, manifest)
    credential = extract_request_credential(request, target_provider)

    if (
        not _is_unified_mode_requested(request, target_provider, "openai")
        and not canonical_req.stream
    ):
        native_req = NativePassthroughRequest(
            provider_id="openai",
            method="POST",
            upstream_path="/responses",
            headers=dict(request.headers),
            body_bytes=body_bytes,
        )
        native_resp = await adapter.execute_native(native_req, credential=credential)
        if native_resp.status_code >= 400:
            adapter._raise_for_upstream_error(
                native_resp.status_code, native_resp.headers, native_resp.body_bytes
            )
        return Response(
            content=native_resp.body_bytes,
            status_code=native_resp.status_code,
            media_type="application/json",
            headers={
                "X-Gateway-Provider": "openai",
                "X-Gateway-Model": clean_model,
                "X-Gateway-Mode": "native",
            },
        )

    if canonical_req.stream:
        stream_iter = adapter.execute_stream(canonical_req, credential)
        return StreamingResponse(
            openai_responses_proto.format_stream_events(stream_iter, clean_model),
            media_type="text/event-stream",
            headers={"X-Gateway-Provider": target_provider, "X-Gateway-Model": clean_model},
        )

    canonical_resp = await adapter.execute(canonical_req, credential)
    formatted = openai_responses_proto.format_response(canonical_resp)
    return JSONResponse(
        content=formatted,
        headers={
            "X-Gateway-Provider": target_provider,
            "X-Gateway-Model": canonical_resp.model,
            "X-Gateway-Mode": "unified",
        },
    )


# =============================================================================
# 3. Anthropic Native Interface (/v1/messages)
# =============================================================================
@protocol_router.post("/v1/messages", summary="Anthropic Native Messages API")
async def anthropic_messages_api(request: Request) -> Response:
    registry = _get_registry(request)
    body_bytes = await request.body()
    payload = await request.json()

    canonical_req = anthropic_proto.parse_request(payload)
    target_provider, clean_model = resolve_target_provider_and_model(
        canonical_req.requested_model, default_provider="anthropic"
    )
    adapter = registry.get_adapter(target_provider)
    manifest = adapter.get_capability_manifest()
    enforce_capability_compatibility(canonical_req, manifest)
    credential = extract_request_credential(request, target_provider)

    if (
        not _is_unified_mode_requested(request, target_provider, "anthropic")
        and not canonical_req.stream
    ):
        native_req = NativePassthroughRequest(
            provider_id="anthropic",
            method="POST",
            upstream_path="/v1/messages",
            headers=dict(request.headers),
            body_bytes=body_bytes,
        )
        native_resp = await adapter.execute_native(native_req, credential=credential)
        if native_resp.status_code >= 400:
            adapter._raise_for_upstream_error(
                native_resp.status_code, native_resp.headers, native_resp.body_bytes
            )
        return Response(
            content=native_resp.body_bytes,
            status_code=native_resp.status_code,
            media_type="application/json",
            headers={
                "X-Gateway-Provider": "anthropic",
                "X-Gateway-Model": clean_model,
                "X-Gateway-Mode": "native",
            },
        )

    if canonical_req.stream:
        stream_iter = adapter.execute_stream(canonical_req, credential)
        return StreamingResponse(
            anthropic_proto.format_stream_events(stream_iter, clean_model),
            media_type="text/event-stream",
            headers={"X-Gateway-Provider": target_provider, "X-Gateway-Model": clean_model},
        )

    canonical_resp = await adapter.execute(canonical_req, credential)
    formatted = anthropic_proto.format_response(canonical_resp)
    return JSONResponse(
        content=formatted,
        headers={
            "X-Gateway-Provider": target_provider,
            "X-Gateway-Model": canonical_resp.model,
            "X-Gateway-Mode": "unified",
        },
    )


# =============================================================================
# 4. Google Gemini Native Interface (/v1beta/models/{model_action:path})
# =============================================================================
@protocol_router.post(
    "/v1beta/models/{model_action:path}",
    summary="Google Gemini Native generateContent / streamGenerateContent",
)
async def gemini_native_api(request: Request, model_action: str) -> Response:
    registry = _get_registry(request)
    body_bytes = await request.body()
    payload = await request.json()

    if ":" in model_action:
        model_name, action = model_action.rsplit(":", 1)
    else:
        model_name, action = model_action, "generateContent"

    is_stream = action == "streamGenerateContent"
    canonical_req = gemini_proto.parse_request(
        payload, model=model_name, stream=is_stream
    )
    target_provider, clean_model = resolve_target_provider_and_model(
        canonical_req.requested_model, default_provider="gemini"
    )
    adapter = registry.get_adapter(target_provider)
    manifest = adapter.get_capability_manifest()
    enforce_capability_compatibility(canonical_req, manifest)
    credential = extract_request_credential(request, target_provider)

    if (
        not _is_unified_mode_requested(request, target_provider, "gemini")
        and not is_stream
    ):
        native_req = NativePassthroughRequest(
            provider_id="gemini",
            method="POST",
            upstream_path=f"/v1beta/models/{clean_model}:{action}",
            query_params=dict(request.query_params),
            headers=dict(request.headers),
            body_bytes=body_bytes,
        )
        native_resp = await adapter.execute_native(native_req, credential=credential)
        if native_resp.status_code >= 400:
            adapter._raise_for_upstream_error(
                native_resp.status_code, native_resp.headers, native_resp.body_bytes
            )
        return Response(
            content=native_resp.body_bytes,
            status_code=native_resp.status_code,
            media_type="application/json",
            headers={
                "X-Gateway-Provider": "gemini",
                "X-Gateway-Model": clean_model,
                "X-Gateway-Mode": "native",
            },
        )

    if is_stream:
        stream_iter = adapter.execute_stream(canonical_req, credential)
        return StreamingResponse(
            gemini_proto.format_stream_events(stream_iter, clean_model),
            media_type="text/event-stream",
            headers={"X-Gateway-Provider": target_provider, "X-Gateway-Model": clean_model},
        )

    canonical_resp = await adapter.execute(canonical_req, credential)
    formatted = gemini_proto.format_response(canonical_resp)
    return JSONResponse(
        content=formatted,
        headers={
            "X-Gateway-Provider": target_provider,
            "X-Gateway-Model": canonical_resp.model,
            "X-Gateway-Mode": "unified",
        },
    )


# =============================================================================
# 5. ElevenLabs Native Interface (/v1/text-to-speech, /v1/speech-to-text, /v1/voices)
# =============================================================================
@protocol_router.post(
    "/v1/text-to-speech/{voice_id}",
    summary="ElevenLabs Native Text-to-Speech",
)
async def elevenlabs_tts_api(request: Request, voice_id: str) -> Response:
    registry = _get_registry(request)
    payload = await request.json()
    output_format = request.query_params.get("output_format", "mp3_44100_128")
    canonical_req = elevenlabs_proto.parse_request(
        payload, voice_id=voice_id, stream=False, output_format=output_format
    )
    adapter = registry.get_adapter("elevenlabs")
    enforce_capability_compatibility(canonical_req, adapter.get_capability_manifest())
    credential = extract_request_credential(request, "elevenlabs")

    resp = await adapter.execute(canonical_req, credential)
    return Response(
        content=resp.audio_content or b"",
        media_type=resp.audio_mime_type or "audio/mpeg",
        headers={
            "X-Gateway-Provider": "elevenlabs",
            "X-Gateway-Model": resp.model,
            "X-Character-Count": str(resp.usage.characters),
        },
    )


@protocol_router.post(
    "/v1/text-to-speech/{voice_id}/stream",
    summary="ElevenLabs Native Streaming Text-to-Speech",
)
async def elevenlabs_tts_stream_api(request: Request, voice_id: str) -> Response:
    registry = _get_registry(request)
    payload = await request.json()
    canonical_req = elevenlabs_proto.parse_request(
        payload, voice_id=voice_id, stream=True
    )
    adapter = registry.get_adapter("elevenlabs")
    enforce_capability_compatibility(canonical_req, adapter.get_capability_manifest())
    credential = extract_request_credential(request, "elevenlabs")

    stream_iter = adapter.execute_stream(canonical_req, credential)
    return StreamingResponse(
        elevenlabs_proto.format_stream_events(stream_iter, canonical_req.requested_model),
        media_type="audio/mpeg",
        headers={"X-Gateway-Provider": "elevenlabs"},
    )


@protocol_router.post(
    "/v1/speech-to-text",
    summary="ElevenLabs Native Speech-to-Text",
)
async def elevenlabs_stt_api(
    request: Request,
    file: UploadFile = File(...),
    model_id: str = Form(default="scribe_v1"),
) -> JSONResponse:
    registry = _get_registry(request)
    audio_bytes = await file.read()
    canonical_req = elevenlabs_proto.parse_stt_request(
        audio_bytes=audio_bytes,
        filename=file.filename or "audio.mp3",
        model_id=model_id,
    )
    adapter = registry.get_adapter("elevenlabs")
    enforce_capability_compatibility(canonical_req, adapter.get_capability_manifest())
    credential = extract_request_credential(request, "elevenlabs")

    resp = await adapter.execute(canonical_req, credential)
    return JSONResponse(
        content=elevenlabs_proto.format_response(resp),
        headers={"X-Gateway-Provider": "elevenlabs", "X-Gateway-Model": resp.model},
    )


@protocol_router.get("/v1/voices", summary="ElevenLabs Native Voice Discovery")
async def elevenlabs_voices_api(request: Request) -> dict[str, Any]:
    registry = _get_registry(request)
    adapter = registry.get_adapter("elevenlabs")
    credential = extract_request_credential(request, "elevenlabs")
    voices = await adapter.discover_voices(credential=credential)  # type: ignore[attr-defined]
    return {"voices": voices}


# =============================================================================
# 6. Custom Protocol & Explicit Native Passthrough Endpoints
# =============================================================================
@protocol_router.post(
    "/v1/custom/{provider_id}",
    summary="Execute Request via Configurable Custom Protocol & Provider",
)
async def custom_protocol_endpoint(request: Request, provider_id: str) -> Response:
    registry = _get_registry(request)
    adapter = registry.get_adapter(provider_id)
    payload = await request.json()

    proto_cfg = adapter.config.custom_settings.get("protocol_spec")
    spec = (
        CustomProtocolSpec(**proto_cfg)
        if isinstance(proto_cfg, dict)
        else CustomProtocolSpec()
    )
    custom_proto = CustomProtocolAdapter(spec)

    canonical_req = custom_proto.parse_request(payload, provider_id=provider_id)
    enforce_capability_compatibility(canonical_req, adapter.get_capability_manifest())
    credential = extract_request_credential(request, provider_id)

    if canonical_req.stream:
        stream_iter = adapter.execute_stream(canonical_req, credential)
        media = (
            "application/x-ndjson"
            if spec.stream_framing == "ndjson"
            else ("text/plain" if spec.stream_framing == "raw_text" else "text/event-stream")
        )
        return StreamingResponse(
            custom_proto.format_stream_events(stream_iter, canonical_req.requested_model),
            media_type=media,
            headers={"X-Gateway-Provider": provider_id},
        )

    resp = await adapter.execute(canonical_req, credential)
    return JSONResponse(
        content=custom_proto.format_response(resp),
        headers={"X-Gateway-Provider": provider_id, "X-Gateway-Model": resp.model},
    )


@protocol_router.api_route(
    "/v1/native/{provider_id}/{upstream_path:path}",
    methods=["GET", "POST", "PUT", "DELETE"],
    summary="Zero-Normalization Native Provider Passthrough",
)
async def explicit_native_passthrough(
    request: Request,
    provider_id: str,
    upstream_path: str,
) -> Response:
    registry = _get_registry(request)
    adapter = registry.get_adapter(provider_id)
    credential = extract_request_credential(request, provider_id)
    body_bytes = await request.body()

    native_req = NativePassthroughRequest(
        provider_id=provider_id,
        method=request.method,
        upstream_path=f"/{upstream_path.lstrip('/')}",
        query_params=dict(request.query_params),
        headers=dict(request.headers),
        body_bytes=body_bytes,
    )
    native_resp = await adapter.execute_native(native_req, credential=credential)
    if native_resp.status_code >= 400:
        adapter._raise_for_upstream_error(
            native_resp.status_code, native_resp.headers, native_resp.body_bytes
        )
    return Response(
        content=native_resp.body_bytes,
        status_code=native_resp.status_code,
        media_type=native_resp.media_type,
        headers={
            "X-Gateway-Provider": provider_id,
            "X-Gateway-Mode": "native",
        },
    )

