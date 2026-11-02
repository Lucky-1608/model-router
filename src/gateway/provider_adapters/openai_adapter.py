"""OpenAI Provider Adapter (Chat Completions, Responses API, Embeddings, Model Discovery)."""

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


class OpenAIProviderAdapter(BaseProviderAdapter):
    """Official OpenAI API provider adapter."""

    provider_id = "openai"
    display_name = "OpenAI"
    adapter_type = "builtin_openai"
    default_base_url = "https://api.openai.com/v1"
    supported_protocols = frozenset(
        {
            ProtocolType.OPENAI_CHAT,
            ProtocolType.OPENAI_RESPONSES,
            ProtocolType.OPENAI_EMBEDDINGS,
        }
    )

    def get_default_config(self) -> ProviderConfig:
        return ProviderConfig(
            provider_id=self.provider_id,
            display_name=self.display_name,
            adapter_type=self.adapter_type,
            base_url=self.default_base_url,
            auth=AuthConfiguration(
                auth_type="header",
                header_name="Authorization",
                header_prefix="Bearer ",
            ),
        )

    def get_capability_manifest(self) -> ProviderCapabilityManifest:
        supported = {
            CapabilityFlag.CHAT_COMPLETIONS,
            CapabilityFlag.RESPONSES_API,
            CapabilityFlag.EMBEDDINGS,
            CapabilityFlag.TOOL_CALLING,
            CapabilityFlag.PARALLEL_TOOL_CALLS,
            CapabilityFlag.STRUCTURED_OUTPUTS,
            CapabilityFlag.JSON_MODE,
            CapabilityFlag.STREAMING,
            CapabilityFlag.SYSTEM_INSTRUCTIONS,
            CapabilityFlag.REASONING_TOKENS,
            CapabilityFlag.VISION,
            CapabilityFlag.AUDIO_INPUT,
            CapabilityFlag.MODEL_DISCOVERY,
        }
        unsupported = {
            CapabilityFlag.MESSAGES_API,
            CapabilityFlag.GEMINI_GENERATE_CONTENT,
            CapabilityFlag.VOICE_DISCOVERY,
        }
        return ProviderCapabilityManifest(
            provider_id=self.provider_id,
            display_name=self.display_name,
            adapter_type=self.adapter_type,
            default_base_url=self.default_base_url,
            supported_protocols=list(self.supported_protocols),
            supported_quota_scopes=[
                QuotaScopeType.ACCOUNT,
                QuotaScopeType.WORKSPACE,
                QuotaScopeType.MODEL,
                QuotaScopeType.CREDENTIAL,
            ],
            capabilities=build_capability_map(supported, unsupported),
            auth_header_name="Authorization",
            auth_header_prefix="Bearer ",
            notes="Supports OpenAI Chat Completions, Responses API, and Embeddings.",
        )

    def build_auth_headers(
        self, credential: AdapterCredential | None
    ) -> dict[str, str]:
        headers = super().build_auth_headers(credential)
        if credential is not None:
            if credential.organization_id:
                headers["OpenAI-Organization"] = credential.organization_id
            if credential.project_id:
                headers["OpenAI-Project"] = credential.project_id
        return headers

    async def discover_models(
        self, credential: AdapterCredential | None = None
    ) -> list[DiscoveredModel]:
        client = await self.get_http_client()
        resp = await client.get("/models", headers=self.build_auth_headers(credential))
        resp_headers = {k.lower(): v for k, v in resp.headers.items()}
        if resp.status_code >= 400:
            self._raise_for_upstream_error(resp.status_code, resp_headers, resp.text)

        payload = resp.json()
        models: list[DiscoveredModel] = []
        for item in payload.get("data", []):
            model_id = str(item.get("id", ""))
            if not model_id:
                continue
            caps = self._infer_verified_model_capabilities(model_id)
            protocols = (
                [ProtocolType.OPENAI_EMBEDDINGS]
                if "embedding" in model_id
                else [ProtocolType.OPENAI_CHAT, ProtocolType.OPENAI_RESPONSES]
            )
            models.append(
                DiscoveredModel(
                    provider_id=self.provider_id,
                    provider_model_id=model_id,
                    canonical_model_id=f"{self.provider_id}/{model_id}",
                    display_name=model_id,
                    supported_protocols=protocols,
                    context_length=None,  # OpenAI /v1/models does not return context_length; mark None (UNKNOWN) unless statically registered
                    capabilities=caps,
                    raw_metadata=item,
                )
            )
        return models

    def _infer_verified_model_capabilities(
        self, model_id: str
    ) -> dict[CapabilityFlag, CapabilityState]:
        if "embedding" in model_id:
            return build_capability_map(
                supported={CapabilityFlag.EMBEDDINGS},
                unsupported={
                    CapabilityFlag.CHAT_COMPLETIONS,
                    CapabilityFlag.TOOL_CALLING,
                    CapabilityFlag.STREAMING,
                    CapabilityFlag.VISION,
                },
            )
        if model_id.startswith(("gpt-4o", "gpt-4.1", "o1", "o3", "o4")):
            supported = {
                CapabilityFlag.CHAT_COMPLETIONS,
                CapabilityFlag.RESPONSES_API,
                CapabilityFlag.TOOL_CALLING,
                CapabilityFlag.PARALLEL_TOOL_CALLS,
                CapabilityFlag.STRUCTURED_OUTPUTS,
                CapabilityFlag.JSON_MODE,
                CapabilityFlag.STREAMING,
                CapabilityFlag.SYSTEM_INSTRUCTIONS,
                CapabilityFlag.VISION,
            }
            if model_id.startswith(("o1", "o3", "o4")):
                supported.add(CapabilityFlag.REASONING_TOKENS)
            return build_capability_map(
                supported=supported,
                unsupported={CapabilityFlag.EMBEDDINGS, CapabilityFlag.TTS, CapabilityFlag.STT},
            )
        # For unknown OpenAI models, do not fabricate capabilities
        return build_capability_map(supported=set(), unsupported=set())

    def _build_chat_payload(
        self, request: CanonicalInternalRequest, *, stream: bool
    ) -> dict[str, Any]:
        model_name = request.provider_model_id or request.requested_model.removeprefix(
            f"{self.provider_id}/"
        )
        messages: list[dict[str, Any]] = []
        for sys_text in request.system_instructions:
            messages.append({"role": "system", "content": sys_text})

        for msg in request.messages:
            if len(msg.parts) == 1 and msg.parts[0].type == "text":
                messages.append({"role": msg.role, "content": msg.parts[0].text or ""})
                continue

            content_blocks: list[dict[str, Any]] = []
            tool_calls_out: list[dict[str, Any]] = []
            for part in msg.parts:
                if part.type == "text" and part.text is not None:
                    content_blocks.append({"type": "text", "text": part.text})
                elif part.type == "image_url" and part.media_url:
                    content_blocks.append(
                        {"type": "image_url", "image_url": {"url": part.media_url}}
                    )
                elif part.type == "tool_call" and part.tool_call:
                    tool_calls_out.append(
                        {
                            "id": part.tool_call.call_id,
                            "type": "function",
                            "function": {
                                "name": part.tool_call.name,
                                "arguments": part.tool_call.arguments_json,
                            },
                        }
                    )
                elif part.type == "tool_result" and part.tool_result:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": part.tool_result.call_id,
                            "content": part.tool_result.output_text,
                        }
                    )
            if content_blocks or tool_calls_out:
                entry: dict[str, Any] = {"role": msg.role}
                if content_blocks:
                    entry["content"] = content_blocks
                if tool_calls_out:
                    entry["tool_calls"] = tool_calls_out
                messages.append(entry)

        payload: dict[str, Any] = {
            "model": model_name,
            "messages": messages,
            "stream": stream,
        }
        if stream:
            payload["stream_options"] = {"include_usage": True}
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.top_p is not None:
            payload["top_p"] = request.top_p
        if request.max_output_tokens is not None:
            payload["max_completion_tokens"] = request.max_output_tokens
        if request.stop_sequences:
            payload["stop"] = request.stop_sequences
        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description or "",
                        "parameters": t.parameters_schema,
                        **({"strict": True} if t.strict else {}),
                    },
                }
                for t in request.tools
                if not t.is_provider_native
            ]
        if request.tool_choice is not None:
            payload["tool_choice"] = request.tool_choice
        if request.parallel_tool_calls is not None:
            payload["parallel_tool_calls"] = request.parallel_tool_calls
        if request.response_format is not None:
            if request.response_format.type == "json_object":
                payload["response_format"] = {"type": "json_object"}
            elif (
                request.response_format.type == "json_schema"
                and request.response_format.json_schema
            ):
                payload["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": request.response_format.schema_name or "response",
                        "schema": request.response_format.json_schema,
                        "strict": request.response_format.strict,
                    },
                }
        return payload

    async def execute(
        self,
        request: CanonicalInternalRequest,
        credential: AdapterCredential,
        timeout: TimeoutPolicy | None = None,
    ) -> CanonicalInternalResponse:
        client = await self.get_http_client(timeout_policy=timeout)
        headers = self.build_auth_headers(credential)
        start_ns = time.perf_counter_ns()

        if request.source_protocol == ProtocolType.OPENAI_EMBEDDINGS:
            model_name = request.provider_model_id or request.requested_model.removeprefix(
                f"{self.provider_id}/"
            )
            emb_payload = {
                "model": model_name,
                "input": request.input_texts
                or [m.combined_text() for m in request.messages],
            }
            resp = await client.post("/embeddings", headers=headers, json=emb_payload)
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            if resp.status_code >= 400:
                self._raise_for_upstream_error(resp.status_code, resp_headers, resp.text)
            body = resp.json()
            usage, rate_limit = self.extract_usage_and_ratelimits(resp_headers, body)
            vectors = [item.get("embedding", []) for item in body.get("data", [])]
            elapsed_ms = (time.perf_counter_ns() - start_ns) / 1_000_000.0
            return CanonicalInternalResponse(
                request_id=request.request_id,
                provider_id=self.provider_id,
                model=body.get("model", model_name),
                embeddings=vectors,
                usage=usage or TokenUsage(),
                rate_limit=rate_limit,
                latency_ms=round(elapsed_ms, 2),
            )

        chat_payload = self._build_chat_payload(request, stream=False)
        resp = await client.post("/chat/completions", headers=headers, json=chat_payload)
        resp_headers = {k.lower(): v for k, v in resp.headers.items()}
        if resp.status_code >= 400:
            self._raise_for_upstream_error(resp.status_code, resp_headers, resp.text)

        body = resp.json()
        usage, rate_limit = self.extract_usage_and_ratelimits(resp_headers, body)
        choices = body.get("choices", [])
        first_choice = choices[0] if choices else {}
        message_obj = first_choice.get("message", {})
        parts: list[CanonicalMessagePart] = []
        if message_obj.get("content"):
            parts.append(
                CanonicalMessagePart(type="text", text=str(message_obj["content"]))
            )

        tool_calls: list[NormalizedToolCall] = []
        for tc in message_obj.get("tool_calls", []) or []:
            fn = tc.get("function", {})
            args_str = fn.get("arguments", "{}")
            try:
                args_dict = json.loads(args_str) if isinstance(args_str, str) else args_str
            except ValueError:
                args_dict = {}
            norm_tc = NormalizedToolCall(
                call_id=str(tc.get("id", "")),
                name=str(fn.get("name", "")),
                arguments_json=args_str if isinstance(args_str, str) else json.dumps(args_str),
                arguments_dict=args_dict if isinstance(args_dict, dict) else {},
            )
            tool_calls.append(norm_tc)
            parts.append(CanonicalMessagePart(type="tool_call", tool_call=norm_tc))

        raw_finish = first_choice.get("finish_reason", "stop")
        finish_map = {
            "stop": "stop",
            "length": "length",
            "tool_calls": "tool_calls",
            "content_filter": "content_filter",
        }
        elapsed_ms = (time.perf_counter_ns() - start_ns) / 1_000_000.0
        return CanonicalInternalResponse(
            request_id=request.request_id,
            provider_id=self.provider_id,
            model=str(body.get("model", chat_payload["model"])),
            provider_response_id=body.get("id"),
            output_message=CanonicalMessage(role="assistant", parts=parts),
            tool_calls=tool_calls,
            finish_reason=finish_map.get(raw_finish, "stop"),  # type: ignore[arg-type]
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
        chat_payload = self._build_chat_payload(request, stream=True)

        async with client.stream(
            "POST", "/chat/completions", headers=headers, json=chat_payload
        ) as resp:
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            if resp.status_code >= 400:
                body_bytes = await resp.aread()
                self._raise_for_upstream_error(
                    resp.status_code, resp_headers, body_bytes.decode("utf-8", errors="replace")
                )

            async for _event_name, data_str in iter_sse_events(resp):
                if data_str.strip() == "[DONE]":
                    yield CanonicalStreamEvent(event_type="done")
                    break
                try:
                    chunk = json.loads(data_str)
                except ValueError:
                    continue

                resp_id = chunk.get("id")
                model_name = chunk.get("model")
                choices = chunk.get("choices", [])
                if choices:
                    choice = choices[0]
                    delta = choice.get("delta", {})
                    if delta.get("content"):
                        yield CanonicalStreamEvent(
                            event_type="text_delta",
                            text_delta=str(delta["content"]),
                            provider_response_id=resp_id,
                            model=model_name,
                            raw_event=chunk,
                        )
                    for tc_delta in delta.get("tool_calls", []) or []:
                        fn = tc_delta.get("function", {})
                        yield CanonicalStreamEvent(
                            event_type="tool_call_delta",
                            tool_call_delta=NormalizedToolCall(
                                call_id=str(tc_delta.get("id", "")),
                                name=str(fn.get("name", "")),
                                arguments_json=str(fn.get("arguments", "")),
                            ),
                            provider_response_id=resp_id,
                            model=model_name,
                            raw_event=chunk,
                        )
                    if choice.get("finish_reason"):
                        yield CanonicalStreamEvent(
                            event_type="done",
                            finish_reason=str(choice["finish_reason"]),
                            provider_response_id=resp_id,
                            model=model_name,
                            raw_event=chunk,
                        )
                if chunk.get("usage"):
                    usage, _ = self.extract_usage_and_ratelimits(resp_headers, chunk)
                    if usage:
                        yield CanonicalStreamEvent(
                            event_type="usage",
                            usage=usage,
                            provider_response_id=resp_id,
                            model=model_name,
                            raw_event=chunk,
                        )

    def extract_usage_and_ratelimits(
        self,
        headers: Mapping[str, str],
        body: Mapping[str, Any] | None,
    ) -> tuple[TokenUsage | None, RateLimitSnapshot | None]:
        usage: TokenUsage | None = None
        if body and isinstance(body.get("usage"), Mapping):
            u = body["usage"]
            prompt_tok = int(u.get("prompt_tokens", u.get("input_tokens", 0)) or 0)
            comp_tok = int(u.get("completion_tokens", u.get("output_tokens", 0)) or 0)
            total_tok = int(u.get("total_tokens", prompt_tok + comp_tok) or 0)
            prompt_details = u.get("prompt_tokens_details") or u.get("input_tokens_details") or {}
            comp_details = u.get("completion_tokens_details") or u.get("output_tokens_details") or {}
            usage = TokenUsage(
                input_tokens=prompt_tok,
                output_tokens=comp_tok,
                total_tokens=total_tok,
                cached_input_tokens=int(prompt_details.get("cached_tokens", 0) or 0),
                reasoning_tokens=int(comp_details.get("reasoning_tokens", 0) or 0),
            )

        limit_req = parse_int_header(headers, "x-ratelimit-limit-requests")
        rem_req = parse_int_header(headers, "x-ratelimit-remaining-requests")
        reset_req = parse_duration_seconds(headers.get("x-ratelimit-reset-requests"))
        limit_tok = parse_int_header(headers, "x-ratelimit-limit-tokens")
        rem_tok = parse_int_header(headers, "x-ratelimit-remaining-tokens")
        reset_tok = parse_duration_seconds(headers.get("x-ratelimit-reset-tokens"))
        retry_after = parse_duration_seconds(headers.get("retry-after"))

        rate_limit: RateLimitSnapshot | None = None
        if any(
            v is not None
            for v in (limit_req, rem_req, reset_req, limit_tok, rem_tok, reset_tok, retry_after)
        ):
            rate_limit = RateLimitSnapshot(
                scope=QuotaScopeType.ACCOUNT,
                limit_requests=limit_req,
                remaining_requests=rem_req,
                reset_requests_seconds=reset_req,
                limit_tokens=limit_tok,
                remaining_tokens=rem_tok,
                reset_tokens_seconds=reset_tok,
                retry_after_seconds=retry_after,
            )
        return usage, rate_limit

    def classify_error(
        self,
        status_code: int,
        headers: Mapping[str, str],
        body: bytes | str | Mapping[str, Any] | None,
    ) -> ClassifiedProviderError:
        parsed_err: dict[str, Any] = {}
        if isinstance(body, Mapping):
            parsed_err = dict(body.get("error", body)) if isinstance(body.get("error"), Mapping) else dict(body)
        elif isinstance(body, (bytes, str)):
            raw_text = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else body
            try:
                loaded = json.loads(raw_text)
                if isinstance(loaded, dict):
                    parsed_err = loaded.get("error", loaded) if isinstance(loaded.get("error"), dict) else loaded
            except ValueError:
                parsed_err = {"message": raw_text[:500]}

        err_code = str(parsed_err.get("code") or parsed_err.get("type") or f"HTTP_{status_code}")
        message = str(parsed_err.get("message") or f"OpenAI error (HTTP {status_code})")
        retry_after = parse_duration_seconds(headers.get("retry-after")) or parse_duration_seconds(
            headers.get("x-ratelimit-reset-requests")
        )

        if status_code == 401 or err_code == "invalid_api_key":
            return ClassifiedProviderError(
                provider_id=self.provider_id,
                status_code=status_code,
                error_code=err_code,
                error_type="authentication_error",
                message=message,
                retryable=False,
                quota_scope=QuotaScopeType.CREDENTIAL,
                requires_credential_disable=True,
            )
        if status_code == 429:
            if err_code == "insufficient_quota":
                return ClassifiedProviderError(
                    provider_id=self.provider_id,
                    status_code=status_code,
                    error_code=err_code,
                    error_type="quota_exhausted_error",
                    message=message,
                    retryable=True,
                    quota_scope=QuotaScopeType.ACCOUNT,
                    requires_admin_attention=True,
                )
            return ClassifiedProviderError(
                provider_id=self.provider_id,
                status_code=status_code,
                error_code=err_code,
                error_type="rate_limit_error",
                message=message,
                retryable=True,
                quota_scope=QuotaScopeType.ACCOUNT,
                retry_after_seconds=retry_after or 5.0,
            )
        if status_code == 403:
            return ClassifiedProviderError(
                provider_id=self.provider_id,
                status_code=status_code,
                error_code=err_code,
                error_type="permission_error",
                message=message,
                retryable=False,
                quota_scope=QuotaScopeType.CREDENTIAL,
                requires_admin_attention=True,
            )
        if status_code == 404 or err_code == "model_not_found":
            return ClassifiedProviderError(
                provider_id=self.provider_id,
                status_code=status_code,
                error_code=err_code,
                error_type="not_found_error",
                message=message,
                retryable=False,
                quota_scope=QuotaScopeType.MODEL,
            )
        if err_code == "context_length_exceeded":
            return ClassifiedProviderError(
                provider_id=self.provider_id,
                status_code=status_code,
                error_code=err_code,
                error_type="context_length_error",
                message=message,
                retryable=False,
                quota_scope=QuotaScopeType.MODEL,
            )
        if status_code >= 500:
            return ClassifiedProviderError(
                provider_id=self.provider_id,
                status_code=status_code,
                error_code=err_code,
                error_type="upstream_server_error",
                message=message,
                retryable=True,
                quota_scope=QuotaScopeType.ENDPOINT,
                retry_after_seconds=retry_after or 2.0,
            )
        return ClassifiedProviderError(
            provider_id=self.provider_id,
            status_code=status_code,
            error_code=err_code,
            error_type="invalid_request_error",
            message=message,
            retryable=False,
        )

