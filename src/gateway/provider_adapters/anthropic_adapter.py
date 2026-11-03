"""Anthropic Provider Adapter (Messages API, Streaming Events, Model Discovery)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
import json
import time
from typing import Any

from gateway.capability_registry.models import (
    CapabilityFlag,
    CapabilityState,
    ProtocolType,
    ProviderCapabilityManifest,
    QuotaScopeType,
    build_capability_map,
)
from gateway.core.canonical import (
    AdapterCredential,
    CanonicalInternalRequest,
    CanonicalInternalResponse,
    CanonicalMessage,
    CanonicalMessagePart,
    CanonicalStreamEvent,
    ClassifiedProviderError,
    DiscoveredModel,
    NormalizedToolCall,
    RateLimitSnapshot,
    TokenUsage,
)
from gateway.core.http import TimeoutPolicy
from gateway.provider_adapters.base import (
    AuthConfiguration,
    BaseProviderAdapter,
    ProviderConfig,
    iter_sse_events,
    parse_duration_seconds,
    parse_int_header,
)


class AnthropicProviderAdapter(BaseProviderAdapter):
    """Official Anthropic Messages API provider adapter."""

    provider_id = "anthropic"
    display_name = "Anthropic"
    adapter_type = "builtin_anthropic"
    default_base_url = "https://api.anthropic.com"
    supported_protocols = frozenset({ProtocolType.ANTHROPIC_MESSAGES})

    def get_default_config(self) -> ProviderConfig:
        return ProviderConfig(
            provider_id=self.provider_id,
            display_name=self.display_name,
            adapter_type=self.adapter_type,
            base_url=self.default_base_url,
            auth=AuthConfiguration(
                auth_type="header",
                header_name="x-api-key",
                header_prefix="",
                static_headers={"anthropic-version": "2023-06-01"},
            ),
        )

    def get_capability_manifest(self) -> ProviderCapabilityManifest:
        supported = {
            CapabilityFlag.MESSAGES_API,
            CapabilityFlag.CHAT_COMPLETIONS,
            CapabilityFlag.TOOL_CALLING,
            CapabilityFlag.PARALLEL_TOOL_CALLS,
            CapabilityFlag.STREAMING,
            CapabilityFlag.SYSTEM_INSTRUCTIONS,
            CapabilityFlag.REASONING_TOKENS,
            CapabilityFlag.VISION,
            CapabilityFlag.MODEL_DISCOVERY,
        }
        unsupported = {
            CapabilityFlag.RESPONSES_API,
            CapabilityFlag.GEMINI_GENERATE_CONTENT,
            CapabilityFlag.EMBEDDINGS,
            CapabilityFlag.AUDIO_INPUT,
            CapabilityFlag.TTS,
            CapabilityFlag.STT,
            CapabilityFlag.VOICE_DISCOVERY,
        }
        return ProviderCapabilityManifest(
            provider_id=self.provider_id,
            display_name=self.display_name,
            adapter_type=self.adapter_type,
            default_base_url=self.default_base_url,
            supported_protocols=list(self.supported_protocols),
            supported_quota_scopes=[
                QuotaScopeType.WORKSPACE,
                QuotaScopeType.ACCOUNT,
                QuotaScopeType.MODEL,
                QuotaScopeType.CREDENTIAL,
            ],
            capabilities=build_capability_map(supported, unsupported),
            auth_header_name="x-api-key",
            auth_header_prefix="",
            notes="Uses x-api-key and anthropic-version headers; requires max_tokens on /v1/messages.",
        )

    async def discover_models(
        self, credential: AdapterCredential | None = None
    ) -> list[DiscoveredModel]:
        client = await self.get_http_client()
        resp = await client.get("/v1/models", headers=self.build_auth_headers(credential))
        resp_headers = {k.lower(): v for k, v in resp.headers.items()}
        if resp.status_code >= 400:
            self._raise_for_upstream_error(resp.status_code, resp_headers, resp.text)

        payload = resp.json()
        models: list[DiscoveredModel] = []
        for item in payload.get("data", []):
            model_id = str(item.get("id", ""))
            if not model_id:
                continue
            display_name = str(item.get("display_name") or model_id)
            caps = build_capability_map(
                supported={
                    CapabilityFlag.MESSAGES_API,
                    CapabilityFlag.CHAT_COMPLETIONS,
                    CapabilityFlag.TOOL_CALLING,
                    CapabilityFlag.PARALLEL_TOOL_CALLS,
                    CapabilityFlag.STREAMING,
                    CapabilityFlag.SYSTEM_INSTRUCTIONS,
                    CapabilityFlag.VISION,
                },
                unsupported={
                    CapabilityFlag.EMBEDDINGS,
                    CapabilityFlag.TTS,
                    CapabilityFlag.STT,
                },
            )
            if "claude-3-7" in model_id or "claude-4" in model_id or "sonnet-4" in model_id or "opus-4" in model_id:
                caps[CapabilityFlag.REASONING_TOKENS] = CapabilityState.SUPPORTED
            models.append(
                DiscoveredModel(
                    provider_id=self.provider_id,
                    provider_model_id=model_id,
                    canonical_model_id=f"{self.provider_id}/{model_id}",
                    display_name=display_name,
                    supported_protocols=[ProtocolType.ANTHROPIC_MESSAGES],
                    context_length=200000 if model_id.startswith("claude-3") or model_id.startswith("claude-sonnet") else None,
                    capabilities=caps,
                    raw_metadata=item,
                )
            )
        return models

    def _build_messages_payload(
        self, request: CanonicalInternalRequest, *, stream: bool
    ) -> dict[str, Any]:
        model_name = request.provider_model_id or request.requested_model.removeprefix(
            f"{self.provider_id}/"
        )
        system_blocks: list[str] = list(request.system_instructions)
        anthropic_messages: list[dict[str, Any]] = []

        for msg in request.messages:
            if msg.role in ("system", "developer"):
                system_blocks.append(msg.combined_text())
                continue

            role = "assistant" if msg.role == "assistant" else "user"
            content_blocks: list[dict[str, Any]] = []
            for part in msg.parts:
                if part.type == "text" and part.text is not None:
                    content_blocks.append({"type": "text", "text": part.text})
                elif part.type == "thinking" and part.text is not None:
                    thinking_block: dict[str, Any] = {
                        "type": "thinking",
                        "thinking": part.text,
                    }
                    if "signature" in part.provider_metadata:
                        thinking_block["signature"] = part.provider_metadata["signature"]
                    content_blocks.append(thinking_block)
                elif part.type == "image_base64" and part.media_base64:
                    content_blocks.append(
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": part.mime_type or "image/png",
                                "data": part.media_base64,
                            },
                        }
                    )
                elif part.type == "tool_call" and part.tool_call:
                    content_blocks.append(
                        {
                            "type": "tool_use",
                            "id": part.tool_call.call_id,
                            "name": part.tool_call.name,
                            "input": part.tool_call.arguments_dict,
                        }
                    )
                elif part.type == "tool_result" and part.tool_result:
                    content_blocks.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": part.tool_result.call_id,
                            "content": part.tool_result.output_text,
                            "is_error": part.tool_result.is_error,
                        }
                    )
            if content_blocks:
                if anthropic_messages and anthropic_messages[-1]["role"] == role:
                    anthropic_messages[-1]["content"].extend(content_blocks)
                else:
                    anthropic_messages.append({"role": role, "content": content_blocks})

        payload: dict[str, Any] = {
            "model": model_name,
            "max_tokens": request.max_output_tokens or 4096,
            "messages": anthropic_messages,
            "stream": stream,
        }
        if system_blocks:
            payload["system"] = "\n\n".join(s for s in system_blocks if s)
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.top_p is not None:
            payload["top_p"] = request.top_p
        if request.stop_sequences:
            payload["stop_sequences"] = request.stop_sequences
        if request.tools:
            payload["tools"] = [
                {
                    "name": t.name,
                    "description": t.description or "",
                    "input_schema": t.parameters_schema,
                }
                for t in request.tools
                if not t.is_provider_native
            ]
        if "thinking" in request.provider_extensions:
            payload["thinking"] = request.provider_extensions["thinking"]
        return payload

    async def execute(
        self,
        request: CanonicalInternalRequest,
        credential: AdapterCredential,
        timeout: TimeoutPolicy | None = None,
    ) -> CanonicalInternalResponse:
        client = await self.get_http_client(timeout_policy=timeout)
        headers = self.build_auth_headers(credential)
        payload = self._build_messages_payload(request, stream=False)
        start_ns = time.perf_counter_ns()

        resp = await client.post("/v1/messages", headers=headers, json=payload)
        resp_headers = {k.lower(): v for k, v in resp.headers.items()}
        if resp.status_code >= 400:
            self._raise_for_upstream_error(resp.status_code, resp_headers, resp.text)

        body = resp.json()
        usage, rate_limit = self.extract_usage_and_ratelimits(resp_headers, body)
        parts: list[CanonicalMessagePart] = []
        tool_calls: list[NormalizedToolCall] = []

        for block in body.get("content", []):
            btype = block.get("type")
            if btype == "text":
                parts.append(CanonicalMessagePart(type="text", text=str(block.get("text", ""))))
            elif btype == "thinking":
                parts.append(
                    CanonicalMessagePart(
                        type="thinking",
                        text=str(block.get("thinking", "")),
                        provider_metadata={"signature": block.get("signature")},
                    )
                )
            elif btype == "tool_use":
                inp = block.get("input", {})
                norm_tc = NormalizedToolCall(
                    call_id=str(block.get("id", "")),
                    name=str(block.get("name", "")),
                    arguments_json=json.dumps(inp),
                    arguments_dict=inp if isinstance(inp, dict) else {},
                )
                tool_calls.append(norm_tc)
                parts.append(CanonicalMessagePart(type="tool_call", tool_call=norm_tc))

        stop_reason = body.get("stop_reason", "end_turn")
        stop_map = {
            "end_turn": "stop",
            "stop_sequence": "stop",
            "max_tokens": "length",
            "tool_use": "tool_calls",
        }
        elapsed_ms = (time.perf_counter_ns() - start_ns) / 1_000_000.0
        return CanonicalInternalResponse(
            request_id=request.request_id,
            provider_id=self.provider_id,
            model=str(body.get("model", payload["model"])),
            provider_response_id=body.get("id"),
            output_message=CanonicalMessage(role="assistant", parts=parts),
            tool_calls=tool_calls,
            finish_reason=stop_map.get(stop_reason, "stop"),  # type: ignore[arg-type]
            usage=usage or TokenUsage(),
            rate_limit=rate_limit,
            latency_ms=round(elapsed_ms, 2),
        )

    async def execute_stream(
        self,
        request: CanonicalInternalRequest,
        credential: AdapterCredential,
        timeout: TimeoutPolicy | None = None,
    ) -> AsyncIterator[CanonicalStreamEvent]:
        client = await self.get_http_client(timeout_policy=timeout)
        headers = self.build_auth_headers(credential)
        payload = self._build_messages_payload(request, stream=True)

        async with client.stream("POST", "/v1/messages", headers=headers, json=payload) as resp:
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            if resp.status_code >= 400:
                body_bytes = await resp.aread()
                self._raise_for_upstream_error(
                    resp.status_code, resp_headers, body_bytes.decode("utf-8", errors="replace")
                )

            current_tool_id: str = ""
            current_tool_name: str = ""
            input_tokens = 0

            async for event_name, data_str in iter_sse_events(resp):
                try:
                    event_data = json.loads(data_str)
                except ValueError:
                    continue

                ev_type = event_name or event_data.get("type")
                if ev_type == "message_start":
                    msg_meta = event_data.get("message", {})
                    u = msg_meta.get("usage", {})
                    input_tokens = int(u.get("input_tokens", 0) or 0)
                    yield CanonicalStreamEvent(
                        event_type="start",
                        provider_response_id=msg_meta.get("id"),
                        model=msg_meta.get("model"),
                        raw_event=event_data,
                    )
                elif ev_type == "content_block_start":
                    cb = event_data.get("content_block", {})
                    if cb.get("type") == "tool_use":
                        current_tool_id = str(cb.get("id", ""))
                        current_tool_name = str(cb.get("name", ""))
                elif ev_type == "content_block_delta":
                    delta = event_data.get("delta", {})
                    dtype = delta.get("type")
                    if dtype == "text_delta":
                        yield CanonicalStreamEvent(
                            event_type="text_delta",
                            text_delta=str(delta.get("text", "")),
                            raw_event=event_data,
                        )
                    elif dtype == "thinking_delta":
                        yield CanonicalStreamEvent(
                            event_type="thinking_delta",
                            thinking_delta=str(delta.get("thinking", "")),
                            raw_event=event_data,
                        )
                    elif dtype == "input_json_delta":
                        yield CanonicalStreamEvent(
                            event_type="tool_call_delta",
                            tool_call_delta=NormalizedToolCall(
                                call_id=current_tool_id,
                                name=current_tool_name,
                                arguments_json=str(delta.get("partial_json", "")),
                            ),
                            raw_event=event_data,
                        )
                elif ev_type == "message_delta":
                    u = event_data.get("usage", {})
                    out_tok = int(u.get("output_tokens", 0) or 0)
                    yield CanonicalStreamEvent(
                        event_type="usage",
                        usage=TokenUsage(
                            input_tokens=input_tokens,
                            output_tokens=out_tok,
                            total_tokens=input_tokens + out_tok,
                        ),
                        finish_reason=event_data.get("delta", {}).get("stop_reason"),
                        raw_event=event_data,
                    )
                elif ev_type == "message_stop":
                    yield CanonicalStreamEvent(event_type="done", raw_event=event_data)

    def extract_usage_and_ratelimits(
        self,
        headers: Mapping[str, str],
        body: Mapping[str, Any] | None,
    ) -> tuple[TokenUsage | None, RateLimitSnapshot | None]:
        usage: TokenUsage | None = None
        if body and isinstance(body.get("usage"), Mapping):
            u = body["usage"]
            in_tok = int(u.get("input_tokens", 0) or 0)
            out_tok = int(u.get("output_tokens", 0) or 0)
            cache_read = int(u.get("cache_read_input_tokens", 0) or 0)
            usage = TokenUsage(
                input_tokens=in_tok,
                output_tokens=out_tok,
                total_tokens=in_tok + out_tok,
                cached_input_tokens=cache_read,
            )

        limit_req = parse_int_header(headers, "anthropic-ratelimit-requests-limit")
        rem_req = parse_int_header(headers, "anthropic-ratelimit-requests-remaining")
        limit_tok = parse_int_header(headers, "anthropic-ratelimit-tokens-limit")
        rem_tok = parse_int_header(headers, "anthropic-ratelimit-tokens-remaining")
        retry_after = parse_duration_seconds(headers.get("retry-after"))

        rate_limit: RateLimitSnapshot | None = None
        if any(v is not None for v in (limit_req, rem_req, limit_tok, rem_tok, retry_after)):
            rate_limit = RateLimitSnapshot(
                scope=QuotaScopeType.WORKSPACE,
                limit_requests=limit_req,
                remaining_requests=rem_req,
                limit_tokens=limit_tok,
                remaining_tokens=rem_tok,
                retry_after_seconds=retry_after,
            )
        return usage, rate_limit

    def classify_error(
        self,
        status_code: int,
        headers: Mapping[str, str],
        body: bytes | str | Mapping[str, Any] | None,
    ) -> ClassifiedProviderError:
        err_obj: dict[str, Any] = {}
        if isinstance(body, Mapping):
            err_obj = dict(body.get("error", body)) if isinstance(body.get("error"), Mapping) else dict(body)
        elif isinstance(body, (bytes, str)):
            raw_text = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else body
            try:
                loaded = json.loads(raw_text)
                if isinstance(loaded, dict):
                    err_obj = loaded.get("error", loaded) if isinstance(loaded.get("error"), dict) else loaded
            except ValueError:
                err_obj = {"message": raw_text[:500]}

        err_type_raw = str(err_obj.get("type") or f"HTTP_{status_code}")
        message = str(err_obj.get("message") or f"Anthropic error (HTTP {status_code})")
        retry_after = parse_duration_seconds(headers.get("retry-after"))

        if status_code == 401 or err_type_raw == "authentication_error":
            return ClassifiedProviderError(
                provider_id=self.provider_id,
                status_code=status_code,
                error_code=err_type_raw,
                error_type="authentication_error",
                message=message,
                retryable=False,
                quota_scope=QuotaScopeType.CREDENTIAL,
                requires_credential_disable=True,
            )
        if status_code == 403 or err_type_raw == "permission_error":
            return ClassifiedProviderError(
                provider_id=self.provider_id,
                status_code=status_code,
                error_code=err_type_raw,
                error_type="permission_error",
                message=message,
                retryable=False,
                quota_scope=QuotaScopeType.WORKSPACE,
                requires_admin_attention=True,
            )
        if status_code == 429 or err_type_raw == "rate_limit_error":
            return ClassifiedProviderError(
                provider_id=self.provider_id,
                status_code=status_code,
                error_code=err_type_raw,
                error_type="rate_limit_error",
                message=message,
                retryable=True,
                quota_scope=QuotaScopeType.WORKSPACE,
                retry_after_seconds=retry_after or 10.0,
            )
        if status_code in (500, 502, 503, 504, 529) or err_type_raw in (
            "api_error",
            "overloaded_error",
        ):
            return ClassifiedProviderError(
                provider_id=self.provider_id,
                status_code=status_code,
                error_code=err_type_raw,
                error_type="upstream_server_error",
                message=message,
                retryable=True,
                quota_scope=QuotaScopeType.ENDPOINT,
                retry_after_seconds=retry_after or 3.0,
            )
        if status_code == 404 or err_type_raw == "not_found_error":
            return ClassifiedProviderError(
                provider_id=self.provider_id,
                status_code=status_code,
                error_code=err_type_raw,
                error_type="not_found_error",
                message=message,
                retryable=False,
                quota_scope=QuotaScopeType.MODEL,
            )
        return ClassifiedProviderError(
            provider_id=self.provider_id,
            status_code=status_code,
            error_code=err_type_raw,
            error_type="invalid_request_error",
            message=message,
            retryable=False,
        )

