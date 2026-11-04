"""Protocol Compatibility Layer & Native Passthrough FastAPI router."""

from __future__ import annotations

from collections.abc import AsyncIterator
import json
import time
from typing import Any

from fastapi import APIRouter, File, Form, Request, Response, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse

from gateway.core.auth import (
    authenticate_client_request,
    enforce_client_key_policy,
    resolve_client_allowed_tools,
)
from gateway.core.canonical import (
    CanonicalInternalRequest,
    CanonicalStreamEvent,
    NativePassthroughRequest,
    TokenUsage,
)
from gateway.core.exceptions import UnsupportedCapabilityError
from gateway.model_registry.registry import ModelRegistry
from gateway.observability.metrics import MetricsCollector
from gateway.observability.tracing import TracingManager
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
from gateway.retry_fallback.engine import RetryFallbackEngine
from gateway.routing_engine.engine import RoutingEngine
from gateway.tool_gateway.models import ToolExecutionMode
from gateway.tool_gateway.orchestrator import ToolGatewayOrchestrator
from gateway.usage_tracking.models import UsageEventInput
from gateway.usage_tracking.tracker import UsageTracker

protocol_router = APIRouter(tags=["Universal Protocol Compatibility"])

openai_chat_proto = OpenAIChatProtocolAdapter()
openai_responses_proto = OpenAIResponsesProtocolAdapter()
anthropic_proto = AnthropicMessagesProtocolAdapter()
gemini_proto = GeminiNativeProtocolAdapter()
elevenlabs_proto = ElevenLabsProtocolAdapter()


def _get_registry(request: Request) -> ProviderRegistry:
    return request.app.state.provider_registry


def _get_model_registry(request: Request) -> ModelRegistry:
    return request.app.state.model_registry


def _get_routing_engine(request: Request) -> RoutingEngine:
    return request.app.state.routing_engine


def _get_tool_orchestrator(request: Request) -> ToolGatewayOrchestrator:
    return request.app.state.tool_orchestrator


def _get_retry_fallback(request: Request) -> RetryFallbackEngine:
    return request.app.state.retry_fallback_engine


def _get_usage_tracker(request: Request) -> UsageTracker:
    return request.app.state.usage_tracker


def _get_metrics(request: Request) -> MetricsCollector:
    return request.app.state.metrics_collector


def _get_tracing(request: Request) -> TracingManager:
    return request.app.state.tracing_manager


def _extract_fallback_enabled(request: Request) -> bool:
    raw = (request.headers.get("X-Gateway-Fallback") or "").strip().lower()
    if raw in ("false", "0", "no", "off"):
        return False
    return True


async def _record_gateway_usage(
    request: Request,
    *,
    provider_id: str,
    model_id: str,
    protocol: str,
    integration_mode: str = "unified",
    stream: bool = False,
    status_code: int = 200,
    success: bool = True,
    error_code: str | None = None,
    attempt_count: int = 1,
    fallback_used: bool = False,
    primary_provider_id: str | None = None,
    tool_rounds: int = 0,
    usage: TokenUsage | None = None,
    latency_ms: float = 0.0,
    ttft_ms: float | None = None,
    credential_id: str | None = None,
    decision_id: str | None = None,
) -> None:
    req_id = getattr(request.state, "request_id", None) or request.headers.get(
        "X-Request-ID", "req_unknown"
    )
    tok = usage or TokenUsage()
    tracker = _get_usage_tracker(request)
    metrics = _get_metrics(request)
    client_key_view = getattr(request.state, "client_key", None)
    client_key_id = client_key_view.id if client_key_view else None

    event = UsageEventInput(
        request_id=req_id,
        decision_id=decision_id,
        provider_id=provider_id,
        model_id=model_id,
        credential_id=credential_id,
        client_key_id=client_key_id,
        protocol=protocol,
        integration_mode=integration_mode,
        stream=stream,
        status_code=status_code,
        success=success,
        error_code=error_code,
        attempt_count=attempt_count,
        fallback_used=fallback_used,
        tool_rounds=tool_rounds,
        usage=tok,
        latency_ms=latency_ms,
        ttft_ms=ttft_ms,
    )
    cost_usd = tracker.enqueue_usage(event)
    metrics.record_request(
        provider_id=provider_id,
        model_id=model_id,
        protocol=protocol,
        status=str(status_code),
        latency_ms=latency_ms,
        ttft_ms=ttft_ms,
        input_tokens=tok.input_tokens,
        output_tokens=tok.output_tokens,
        cost_usd=cost_usd,
        fallback_used=fallback_used,
        primary_provider_id=primary_provider_id,
    )


async def _instrumented_stream_accounting(
    stream_iter: AsyncIterator[CanonicalStreamEvent],
    *,
    request: Request,
    provider_id: str,
    model_id: str,
    protocol: str,
    primary_provider_id: str,
    attempt_count: int,
    fallback_used: bool,
    ttft_ms: float | None,
    decision_id: str | None,
    credential_id: str | None = None,
) -> AsyncIterator[CanonicalStreamEvent]:
    start_ts = time.perf_counter()
    accumulated_usage = TokenUsage()
    first_chunk_received = False
    status_code = 200
    success = True
    error_code = None
    observed_ttft = ttft_ms

    try:
        async for event in stream_iter:
            if not first_chunk_received:
                first_chunk_received = True
                if observed_ttft is None:
                    observed_ttft = round((time.perf_counter() - start_ts) * 1000.0, 2)
            if event.usage:
                accumulated_usage.input_tokens = max(
                    accumulated_usage.input_tokens, event.usage.input_tokens
                )
                accumulated_usage.output_tokens = max(
                    accumulated_usage.output_tokens, event.usage.output_tokens
                )
                accumulated_usage.total_tokens = max(
                    accumulated_usage.total_tokens, event.usage.total_tokens
                )
                accumulated_usage.cached_input_tokens = max(
                    accumulated_usage.cached_input_tokens, event.usage.cached_input_tokens
                )
                accumulated_usage.reasoning_tokens = max(
                    accumulated_usage.reasoning_tokens, event.usage.reasoning_tokens
                )
                accumulated_usage.characters = max(
                    accumulated_usage.characters, event.usage.characters
                )
            yield event
    except Exception as exc:
        success = False
        status_code = getattr(exc, "status_code", 500)
        error_code = getattr(exc, "error_code", type(exc).__name__)
        raise
    finally:
        total_lat_ms = round((time.perf_counter() - start_ts) * 1000.0, 2)
        await _record_gateway_usage(
            request,
            provider_id=provider_id,
            model_id=model_id,
            protocol=protocol,
            integration_mode="unified",
            stream=True,
            status_code=status_code,
            success=success,
            error_code=error_code,
            attempt_count=attempt_count,
            fallback_used=fallback_used,
            primary_provider_id=primary_provider_id,
            usage=accumulated_usage,
            latency_ms=total_lat_ms,
            ttft_ms=observed_ttft,
            credential_id=credential_id,
            decision_id=decision_id,
        )


def _extract_tool_context(
    request: Request,
) -> tuple[ToolExecutionMode, set[str] | None, str | None, int | None]:
    raw_mode = (request.headers.get("X-Gateway-Tool-Mode") or "").strip().lower()
    mode = (
        ToolExecutionMode.GATEWAY_MANAGED
        if raw_mode == "gateway_managed"
        else ToolExecutionMode.PASSTHROUGH
    )
    raw_allowed = (request.headers.get("X-Gateway-Allowed-Tools") or "").strip()
    allowed_tools = (
        {item.strip() for item in raw_allowed.split(",") if item.strip()}
        if raw_allowed
        else None
    )
    client_id = (request.headers.get("X-Gateway-Client-Id") or "").strip() or None
    raw_rounds = (request.headers.get("X-Gateway-Max-Tool-Rounds") or "").strip()
    max_rounds = int(raw_rounds) if raw_rounds.isdigit() else None
    return mode, allowed_tools, client_id, max_rounds


def _is_unified_mode_requested(
    request: Request, target_provider: str, native_provider: str
) -> bool:
    """Determines whether to normalize through the unified layer or use native passthrough.

    Uses native passthrough when the user calls a provider's native protocol for that provider,
    and uses normalization only when the user explicitly chooses the unified interface
    (via X-Gateway-Mode: unified, X-Gateway-Tool-Mode: gateway_managed, or by specifying a cross-provider model).
    """
    tool_mode = (request.headers.get("X-Gateway-Tool-Mode") or "").strip().lower()
    if tool_mode == "gateway_managed":
        return True
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


async def _resolve_via_routing_or_direct(
    request: Request,
    canonical_req: CanonicalInternalRequest,
    default_provider: str = "openai",
) -> tuple[str, str, str | None, bool]:
    """Resolves (target_provider, clean_model, decision_id, routed_by_engine).

    Uses RoutingEngine if the requested model is a semantic alias (e.g. 'auto', 'coding', 'fast')
    or if X-Gateway-Policy / X-Gateway-Task headers are supplied.
    """
    model_registry = _get_model_registry(request)
    routing_engine = _get_routing_engine(request)

    policy_hdr = (request.headers.get("X-Gateway-Policy") or "").strip()
    task_hdr = (request.headers.get("X-Gateway-Task") or "").strip()
    if policy_hdr:
        canonical_req.routing_policy_name = policy_hdr
    if task_hdr:
        canonical_req.task_hint = task_hdr

    if (
        model_registry.is_alias(canonical_req.requested_model)
        or bool(policy_hdr)
        or bool(task_hdr)
    ):
        trace = await routing_engine.route_canonical_request(canonical_req)
        selected_meta = model_registry.get_model(trace.selected_model)
        canonical_req.requested_model = selected_meta.provider_model_id
        return (
            trace.selected_provider,
            selected_meta.provider_model_id,
            trace.decision_id,
            True,
        )

    provider_reg = _get_registry(request)
    registered_provs = {a.provider_id for a in provider_reg.list_adapters(enabled_only=True)}
    target_provider, clean_model = resolve_target_provider_and_model(
        canonical_req.requested_model,
        default_provider=default_provider,
        registered_providers=registered_provs,
    )
    return target_provider, clean_model, None, False


# =============================================================================
# 1. OpenAI-Compatible Interface (/v1/chat/completions, /v1/embeddings, /v1/models)
# =============================================================================
@protocol_router.post("/v1/chat/completions", summary="OpenAI-Compatible Chat Completions")
async def openai_chat_completions(request: Request) -> Response:
    registry = _get_registry(request)
    tool_orchestrator = _get_tool_orchestrator(request)
    retry_fallback = _get_retry_fallback(request)
    tracing = _get_tracing(request)
    body_bytes = await request.body()
    payload = await request.json()

    trace_id, parent_span = tracing.parse_traceparent(
        request.headers.get("traceparent")
    )
    req_start = time.perf_counter()

    canonical_req = openai_chat_proto.parse_request(payload)
    await authenticate_client_request(request)
    target_provider, clean_model, decision_id, routed_by_engine = (
        await _resolve_via_routing_or_direct(
            request, canonical_req, default_provider="openai"
        )
    )
    await enforce_client_key_policy(request, target_provider=target_provider)
    tool_mode, allowed_tools, client_id, max_rounds = _extract_tool_context(request)
    allowed_tools = resolve_client_allowed_tools(request, allowed_tools)
    tool_orchestrator.prepare_request_tools(
        canonical_req,
        target_provider=target_provider,
        mode=tool_mode,
        allowed_tools=allowed_tools,
        inject_gateway_tools=(
            tool_mode == ToolExecutionMode.GATEWAY_MANAGED and not canonical_req.tools
        ),
    )

    adapter = registry.get_adapter(target_provider)
    manifest = adapter.get_capability_manifest()
    enforce_capability_compatibility(canonical_req, manifest)
    credential = extract_request_credential(request, target_provider)
    allow_fallback = _extract_fallback_enabled(request)

    # Native passthrough if targeting OpenAI directly without unified mode override, not routed by alias, and non-streaming
    if (
        not routed_by_engine
        and not _is_unified_mode_requested(request, target_provider, "openai")
        and not canonical_req.stream
    ):
        with tracing.start_span(
            "ugw.request",
            trace_id=trace_id,
            parent_span_id=parent_span,
            attributes={
                "ugw.protocol": "openai_chat",
                "ugw.mode": "native",
                "ugw.provider": target_provider,
                "ugw.model": clean_model,
            },
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
            lat_ms = round((time.perf_counter() - req_start) * 1000.0, 2)
            parsed_usage: TokenUsage | None = native_resp.usage
            if parsed_usage is None and native_resp.body_bytes:
                try:
                    raw_json = json.loads(native_resp.body_bytes)
                    u = raw_json.get("usage")
                    if isinstance(u, dict):
                        parsed_usage = TokenUsage(
                            input_tokens=u.get("prompt_tokens") or 0,
                            output_tokens=u.get("completion_tokens") or 0,
                            total_tokens=u.get("total_tokens") or 0,
                        )
                except Exception:
                    pass
            await _record_gateway_usage(
                request,
                provider_id=target_provider,
                model_id=clean_model,
                protocol="openai_chat",
                integration_mode="native",
                status_code=native_resp.status_code,
                latency_ms=lat_ms,
                usage=parsed_usage,
                credential_id=credential.credential_id if credential else None,
            )
            return Response(
                content=native_resp.body_bytes,
                status_code=native_resp.status_code,
                media_type="application/json",
                headers={
                    "X-Gateway-Provider": target_provider,
                    "X-Gateway-Model": clean_model,
                    "X-Gateway-Mode": "native",
                    "X-Gateway-Trace-Id": trace_id,
                },
            )

    if canonical_req.stream:
        stream_iter, sel_prov, sel_mod, attempts = (
            await retry_fallback.execute_stream_resilient(
                request=canonical_req,
                primary_provider=target_provider,
                primary_model=clean_model,
                explicit_credential=credential,
                decision_id=decision_id,
                allow_fallback=allow_fallback,
            )
        )
        ttft_val = attempts[-1].latency_ms if attempts else None
        fb_used = len(attempts) > 1
        instrumented_iter = _instrumented_stream_accounting(
            stream_iter,
            request=request,
            provider_id=sel_prov,
            model_id=sel_mod,
            protocol="openai_chat",
            primary_provider_id=target_provider,
            attempt_count=len(attempts),
            fallback_used=fb_used,
            ttft_ms=ttft_val,
            decision_id=decision_id,
            credential_id=attempts[-1].credential_id if attempts else None,
        )
        stream_headers: dict[str, str] = {
            "X-Gateway-Provider": sel_prov,
            "X-Gateway-Model": sel_mod,
            "X-Gateway-Mode": "unified",
            "X-Gateway-Attempts": str(len(attempts)),
            "X-Gateway-Fallback-Used": str(fb_used).lower(),
            "X-Gateway-Trace-Id": trace_id,
        }
        if decision_id:
            stream_headers["X-Gateway-Route-Decision-Id"] = decision_id
        return StreamingResponse(
            openai_chat_proto.format_stream_events(instrumented_iter, sel_mod),
            media_type="text/event-stream",
            headers=stream_headers,
        )

    with tracing.start_span(
        "ugw.request",
        trace_id=trace_id,
        parent_span_id=parent_span,
        attributes={
            "ugw.protocol": "openai_chat",
            "ugw.mode": "unified",
            "ugw.primary_provider": target_provider,
            "ugw.primary_model": clean_model,
        },
    ) as span:
        outcome = await retry_fallback.execute_resilient(
            request=canonical_req,
            primary_provider=target_provider,
            primary_model=clean_model,
            explicit_credential=credential,
            decision_id=decision_id,
            allow_fallback=allow_fallback,
            tool_mode=tool_mode,
            client_id=client_id,
            allowed_tools=allowed_tools,
            max_tool_rounds=max_rounds,
        )
        canonical_resp = outcome.response
        span.attributes["ugw.selected_provider"] = outcome.selected_provider
        span.attributes["ugw.selected_model"] = outcome.selected_model
        span.attributes["ugw.fallback_used"] = outcome.fallback_used
        span.attributes["ugw.attempts"] = len(outcome.attempts)

    tool_rounds = int(canonical_resp.provider_metadata.get("gateway_tool_rounds", 0))
    await _record_gateway_usage(
        request,
        provider_id=outcome.selected_provider,
        model_id=canonical_resp.model,
        protocol="openai_chat",
        integration_mode="unified",
        stream=False,
        status_code=200,
        success=True,
        attempt_count=len(outcome.attempts),
        fallback_used=outcome.fallback_used,
        primary_provider_id=target_provider,
        tool_rounds=tool_rounds,
        usage=canonical_resp.usage,
        latency_ms=outcome.total_latency_ms,
        credential_id=outcome.credential_id,
        decision_id=decision_id,
    )

    formatted = openai_chat_proto.format_response(canonical_resp)
    resp_headers: dict[str, str] = {
        "X-Gateway-Provider": outcome.selected_provider,
        "X-Gateway-Model": canonical_resp.model,
        "X-Gateway-Mode": "unified",
        "X-Gateway-Attempts": str(len(outcome.attempts)),
        "X-Gateway-Fallback-Used": str(outcome.fallback_used).lower(),
        "X-Gateway-Trace-Id": trace_id,
    }
    if decision_id:
        resp_headers["X-Gateway-Route-Decision-Id"] = decision_id
    if tool_rounds > 0:
        resp_headers["X-Gateway-Tool-Rounds"] = str(tool_rounds)
    return JSONResponse(
        content=formatted,
        headers=resp_headers,
    )


@protocol_router.post("/v1/embeddings", summary="OpenAI-Compatible Embeddings")
async def openai_embeddings(request: Request) -> Response:
    registry = _get_registry(request)
    retry_fallback = _get_retry_fallback(request)
    payload = await request.json()
    canonical_req = openai_chat_proto.parse_embeddings_request(payload)
    await authenticate_client_request(request)
    target_provider, clean_model, decision_id, _ = await _resolve_via_routing_or_direct(
        request, canonical_req, default_provider="openai"
    )
    await enforce_client_key_policy(request, target_provider=target_provider)
    adapter = registry.get_adapter(target_provider)
    manifest = adapter.get_capability_manifest()
    enforce_capability_compatibility(canonical_req, manifest)
    credential = extract_request_credential(request, target_provider)
    allow_fallback = _extract_fallback_enabled(request)

    outcome = await retry_fallback.execute_resilient(
        request=canonical_req,
        primary_provider=target_provider,
        primary_model=clean_model,
        explicit_credential=credential,
        decision_id=decision_id,
        allow_fallback=allow_fallback,
    )
    canonical_resp = outcome.response
    await _record_gateway_usage(
        request,
        provider_id=outcome.selected_provider,
        model_id=canonical_resp.model,
        protocol="openai_embeddings",
        integration_mode="unified",
        attempt_count=len(outcome.attempts),
        fallback_used=outcome.fallback_used,
        primary_provider_id=target_provider,
        usage=canonical_resp.usage,
        latency_ms=outcome.total_latency_ms,
        credential_id=outcome.credential_id,
        decision_id=decision_id,
    )
    formatted = openai_chat_proto.format_response(canonical_resp)
    headers = {
        "X-Gateway-Provider": outcome.selected_provider,
        "X-Gateway-Model": canonical_resp.model,
        "X-Gateway-Attempts": str(len(outcome.attempts)),
        "X-Gateway-Fallback-Used": str(outcome.fallback_used).lower(),
    }
    if decision_id:
        headers["X-Gateway-Route-Decision-Id"] = decision_id
    return JSONResponse(content=formatted, headers=headers)


@protocol_router.get("/v1/models", summary="OpenAI-Compatible Model Listing")
async def openai_list_models(request: Request) -> dict[str, Any]:
    registry = _get_registry(request)
    model_registry = _get_model_registry(request)
    data: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    created_ts = int(time.time())

    # 1. Include provider-discovered cached models first (preserves dynamic discovery)
    for adapter in registry.list_adapters(enabled_only=True):
        cached = registry.get_cached_models(adapter.provider_id)
        for m in cached:
            seen_ids.add(m.canonical_model_id)
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

    # 2. Include ModelRegistry models not already in cached discovery
    for meta in model_registry.list_models(enabled_only=True):
        if meta.model_id in seen_ids:
            continue
        seen_ids.add(meta.model_id)
        data.append(
            {
                "id": meta.model_id,
                "object": "model",
                "created": created_ts,
                "owned_by": meta.provider_id,
                "x_gateway_capabilities": {
                    k.value: v.value for k, v in meta.capabilities.items()
                },
                "x_gateway_context_length": meta.context_length,
            }
        )
    return {"object": "list", "data": data}


# =============================================================================
# 2. OpenAI Responses API Interface (/v1/responses)
# =============================================================================
@protocol_router.post("/v1/responses", summary="OpenAI Native Responses API")
async def openai_responses_api(request: Request) -> Response:
    registry = _get_registry(request)
    tool_orchestrator = _get_tool_orchestrator(request)
    retry_fallback = _get_retry_fallback(request)
    body_bytes = await request.body()
    payload = await request.json()

    canonical_req = openai_responses_proto.parse_request(payload)
    await authenticate_client_request(request)
    target_provider, clean_model, decision_id, _ = await _resolve_via_routing_or_direct(
        request, canonical_req, default_provider="openai"
    )
    await enforce_client_key_policy(request, target_provider=target_provider)
    tool_mode, allowed_tools, client_id, max_rounds = _extract_tool_context(request)
    allowed_tools = resolve_client_allowed_tools(request, allowed_tools)
    tool_orchestrator.prepare_request_tools(
        canonical_req,
        target_provider=target_provider,
        mode=tool_mode,
        allowed_tools=allowed_tools,
    )
    adapter = registry.get_adapter(target_provider)
    manifest = adapter.get_capability_manifest()
    enforce_capability_compatibility(canonical_req, manifest)
    credential = extract_request_credential(request, target_provider)
    allow_fallback = _extract_fallback_enabled(request)

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
        stream_iter, sel_prov, sel_mod, attempts = (
            await retry_fallback.execute_stream_resilient(
                request=canonical_req,
                primary_provider=target_provider,
                primary_model=clean_model,
                explicit_credential=credential,
                allow_fallback=allow_fallback,
            )
        )
        instrumented_iter = _instrumented_stream_accounting(
            stream_iter,
            request=request,
            provider_id=sel_prov,
            model_id=sel_mod,
            protocol="openai_responses",
            primary_provider_id=target_provider,
            attempt_count=len(attempts),
            fallback_used=len(attempts) > 1,
            ttft_ms=attempts[-1].latency_ms if attempts else None,
            decision_id=None,
            credential_id=attempts[-1].credential_id if attempts else None,
        )
        return StreamingResponse(
            openai_responses_proto.format_stream_events(instrumented_iter, sel_mod),
            media_type="text/event-stream",
            headers={
                "X-Gateway-Provider": sel_prov,
                "X-Gateway-Model": sel_mod,
                "X-Gateway-Attempts": str(len(attempts)),
                "X-Gateway-Fallback-Used": str(len(attempts) > 1).lower(),
            },
        )

    outcome = await retry_fallback.execute_resilient(
        request=canonical_req,
        primary_provider=target_provider,
        primary_model=clean_model,
        explicit_credential=credential,
        allow_fallback=allow_fallback,
        tool_mode=tool_mode,
        client_id=client_id,
        allowed_tools=allowed_tools,
        max_tool_rounds=max_rounds,
    )
    canonical_resp = outcome.response
    await _record_gateway_usage(
        request,
        provider_id=outcome.selected_provider,
        model_id=canonical_resp.model,
        protocol="openai_responses",
        integration_mode="unified",
        attempt_count=len(outcome.attempts),
        fallback_used=outcome.fallback_used,
        primary_provider_id=target_provider,
        usage=canonical_resp.usage,
        latency_ms=outcome.total_latency_ms,
        credential_id=outcome.credential_id,
    )
    formatted = openai_responses_proto.format_response(canonical_resp)
    return JSONResponse(
        content=formatted,
        headers={
            "X-Gateway-Provider": outcome.selected_provider,
            "X-Gateway-Model": canonical_resp.model,
            "X-Gateway-Mode": "unified",
            "X-Gateway-Attempts": str(len(outcome.attempts)),
            "X-Gateway-Fallback-Used": str(outcome.fallback_used).lower(),
        },
    )


# =============================================================================
# 3. Anthropic Native Interface (/v1/messages)
# =============================================================================
@protocol_router.post("/v1/messages", summary="Anthropic Native Messages API")
async def anthropic_messages_api(request: Request) -> Response:
    registry = _get_registry(request)
    tool_orchestrator = _get_tool_orchestrator(request)
    retry_fallback = _get_retry_fallback(request)
    body_bytes = await request.body()
    payload = await request.json()

    canonical_req = anthropic_proto.parse_request(payload)
    await authenticate_client_request(request)
    target_provider, clean_model, decision_id, _ = await _resolve_via_routing_or_direct(
        request, canonical_req, default_provider="anthropic"
    )
    await enforce_client_key_policy(request, target_provider=target_provider)
    tool_mode, allowed_tools, client_id, max_rounds = _extract_tool_context(request)
    allowed_tools = resolve_client_allowed_tools(request, allowed_tools)
    tool_orchestrator.prepare_request_tools(
        canonical_req,
        target_provider=target_provider,
        mode=tool_mode,
        allowed_tools=allowed_tools,
    )
    adapter = registry.get_adapter(target_provider)
    manifest = adapter.get_capability_manifest()
    enforce_capability_compatibility(canonical_req, manifest)
    credential = extract_request_credential(request, target_provider)
    allow_fallback = _extract_fallback_enabled(request)

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
        stream_iter, sel_prov, sel_mod, attempts = (
            await retry_fallback.execute_stream_resilient(
                request=canonical_req,
                primary_provider=target_provider,
                primary_model=clean_model,
                explicit_credential=credential,
                allow_fallback=allow_fallback,
            )
        )
        instrumented_iter = _instrumented_stream_accounting(
            stream_iter,
            request=request,
            provider_id=sel_prov,
            model_id=sel_mod,
            protocol="anthropic_messages",
            primary_provider_id=target_provider,
            attempt_count=len(attempts),
            fallback_used=len(attempts) > 1,
            ttft_ms=attempts[-1].latency_ms if attempts else None,
            decision_id=None,
            credential_id=attempts[-1].credential_id if attempts else None,
        )
        return StreamingResponse(
            anthropic_proto.format_stream_events(instrumented_iter, sel_mod),
            media_type="text/event-stream",
            headers={
                "X-Gateway-Provider": sel_prov,
                "X-Gateway-Model": sel_mod,
                "X-Gateway-Attempts": str(len(attempts)),
                "X-Gateway-Fallback-Used": str(len(attempts) > 1).lower(),
            },
        )

    outcome = await retry_fallback.execute_resilient(
        request=canonical_req,
        primary_provider=target_provider,
        primary_model=clean_model,
        explicit_credential=credential,
        allow_fallback=allow_fallback,
        tool_mode=tool_mode,
        client_id=client_id,
        allowed_tools=allowed_tools,
        max_tool_rounds=max_rounds,
    )
    canonical_resp = outcome.response
    await _record_gateway_usage(
        request,
        provider_id=outcome.selected_provider,
        model_id=canonical_resp.model,
        protocol="anthropic_messages",
        integration_mode="unified",
        attempt_count=len(outcome.attempts),
        fallback_used=outcome.fallback_used,
        primary_provider_id=target_provider,
        usage=canonical_resp.usage,
        latency_ms=outcome.total_latency_ms,
        credential_id=outcome.credential_id,
    )
    formatted = anthropic_proto.format_response(canonical_resp)
    return JSONResponse(
        content=formatted,
        headers={
            "X-Gateway-Provider": outcome.selected_provider,
            "X-Gateway-Model": canonical_resp.model,
            "X-Gateway-Mode": "unified",
            "X-Gateway-Attempts": str(len(outcome.attempts)),
            "X-Gateway-Fallback-Used": str(outcome.fallback_used).lower(),
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
    tool_orchestrator = _get_tool_orchestrator(request)
    retry_fallback = _get_retry_fallback(request)
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
    await authenticate_client_request(request)
    target_provider, clean_model, decision_id, _ = await _resolve_via_routing_or_direct(
        request, canonical_req, default_provider="gemini"
    )
    await enforce_client_key_policy(request, target_provider=target_provider)
    tool_mode, allowed_tools, client_id, max_rounds = _extract_tool_context(request)
    allowed_tools = resolve_client_allowed_tools(request, allowed_tools)
    tool_orchestrator.prepare_request_tools(
        canonical_req,
        target_provider=target_provider,
        mode=tool_mode,
        allowed_tools=allowed_tools,
    )
    adapter = registry.get_adapter(target_provider)
    manifest = adapter.get_capability_manifest()
    enforce_capability_compatibility(canonical_req, manifest)
    credential = extract_request_credential(request, target_provider)
    allow_fallback = _extract_fallback_enabled(request)

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
        stream_iter, sel_prov, sel_mod, attempts = (
            await retry_fallback.execute_stream_resilient(
                request=canonical_req,
                primary_provider=target_provider,
                primary_model=clean_model,
                explicit_credential=credential,
                allow_fallback=allow_fallback,
            )
        )
        instrumented_iter = _instrumented_stream_accounting(
            stream_iter,
            request=request,
            provider_id=sel_prov,
            model_id=sel_mod,
            protocol="gemini_native",
            primary_provider_id=target_provider,
            attempt_count=len(attempts),
            fallback_used=len(attempts) > 1,
            ttft_ms=attempts[-1].latency_ms if attempts else None,
            decision_id=None,
            credential_id=attempts[-1].credential_id if attempts else None,
        )
        return StreamingResponse(
            gemini_proto.format_stream_events(instrumented_iter, sel_mod),
            media_type="text/event-stream",
            headers={
                "X-Gateway-Provider": sel_prov,
                "X-Gateway-Model": sel_mod,
                "X-Gateway-Attempts": str(len(attempts)),
                "X-Gateway-Fallback-Used": str(len(attempts) > 1).lower(),
            },
        )

    outcome = await retry_fallback.execute_resilient(
        request=canonical_req,
        primary_provider=target_provider,
        primary_model=clean_model,
        explicit_credential=credential,
        allow_fallback=allow_fallback,
        tool_mode=tool_mode,
        client_id=client_id,
        allowed_tools=allowed_tools,
        max_tool_rounds=max_rounds,
    )
    canonical_resp = outcome.response
    await _record_gateway_usage(
        request,
        provider_id=outcome.selected_provider,
        model_id=canonical_resp.model,
        protocol="gemini_native",
        integration_mode="unified",
        attempt_count=len(outcome.attempts),
        fallback_used=outcome.fallback_used,
        primary_provider_id=target_provider,
        usage=canonical_resp.usage,
        latency_ms=outcome.total_latency_ms,
        credential_id=outcome.credential_id,
    )
    formatted = gemini_proto.format_response(canonical_resp)
    return JSONResponse(
        content=formatted,
        headers={
            "X-Gateway-Provider": outcome.selected_provider,
            "X-Gateway-Model": canonical_resp.model,
            "X-Gateway-Mode": "unified",
            "X-Gateway-Attempts": str(len(outcome.attempts)),
            "X-Gateway-Fallback-Used": str(outcome.fallback_used).lower(),
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
