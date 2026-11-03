"""Google Gemini Provider Adapter (generateContent, streamGenerateContent, embedContent, Model Discovery)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
import json
import time
from typing import Any
import uuid

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
)


class GeminiProviderAdapter(BaseProviderAdapter):
    """Official Google Gemini API provider adapter."""

    provider_id = "gemini"
    display_name = "Google Gemini"
    adapter_type = "builtin_gemini"
    default_base_url = "https://generativelanguage.googleapis.com"
    supported_protocols = frozenset(
        {
            ProtocolType.GEMINI_GENERATE_CONTENT,
            ProtocolType.GEMINI_EMBED_CONTENT,
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
                header_name="x-goog-api-key",
                header_prefix="",
            ),
        )

    def get_capability_manifest(self) -> ProviderCapabilityManifest:
        supported = {
            CapabilityFlag.GEMINI_GENERATE_CONTENT,
            CapabilityFlag.CHAT_COMPLETIONS,
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
            CapabilityFlag.RESPONSES_API,
            CapabilityFlag.MESSAGES_API,
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
                QuotaScopeType.MODEL,
                QuotaScopeType.CREDENTIAL,
            ],
            capabilities=build_capability_map(supported, unsupported),
            auth_header_name="x-goog-api-key",
            auth_header_prefix="",
            notes="Supports Google Gemini v1beta generateContent, streamGenerateContent, and embedContent.",
        )

    async def discover_models(
        self, credential: AdapterCredential | None = None
    ) -> list[DiscoveredModel]:
        client = await self.get_http_client()
        resp = await client.get(
            "/v1beta/models", headers=self.build_auth_headers(credential)
        )
        resp_headers = {k.lower(): v for k, v in resp.headers.items()}
        if resp.status_code >= 400:
            self._raise_for_upstream_error(resp.status_code, resp_headers, resp.text)

        payload = resp.json()
        models: list[DiscoveredModel] = []
        for item in payload.get("models", []):
            raw_name = str(item.get("name", ""))
            model_id = raw_name.removeprefix("models/")
            if not model_id:
                continue
            methods = set(item.get("supportedGenerationMethods", []))
            protocols: list[ProtocolType] = []
            supported_caps: set[CapabilityFlag] = set()
            unsupported_caps: set[CapabilityFlag] = {
                CapabilityFlag.RESPONSES_API,
                CapabilityFlag.MESSAGES_API,
            }

            if "generateContent" in methods:
                protocols.append(ProtocolType.GEMINI_GENERATE_CONTENT)
                supported_caps.update(
                    {
                        CapabilityFlag.GEMINI_GENERATE_CONTENT,
                        CapabilityFlag.CHAT_COMPLETIONS,
                        CapabilityFlag.STREAMING,
                        CapabilityFlag.SYSTEM_INSTRUCTIONS,
                        CapabilityFlag.TOOL_CALLING,
                        CapabilityFlag.STRUCTURED_OUTPUTS,
                        CapabilityFlag.JSON_MODE,
                        CapabilityFlag.VISION,
                    }
                )
            else:
                unsupported_caps.add(CapabilityFlag.GEMINI_GENERATE_CONTENT)

            if "embedContent" in methods:
                protocols.append(ProtocolType.GEMINI_EMBED_CONTENT)
                supported_caps.add(CapabilityFlag.EMBEDDINGS)
            else:
                unsupported_caps.add(CapabilityFlag.EMBEDDINGS)

            models.append(
                DiscoveredModel(
                    provider_id=self.provider_id,
                    provider_model_id=model_id,
                    canonical_model_id=f"{self.provider_id}/{model_id}",
                    display_name=str(item.get("displayName") or model_id),
                    supported_protocols=protocols,
                    context_length=item.get("inputTokenLimit"),
                    max_output_tokens=item.get("outputTokenLimit"),
                    capabilities=build_capability_map(supported_caps, unsupported_caps),
                    raw_metadata=item,
                )
            )
        return models

    def _normalize_model_path(self, request: CanonicalInternalRequest) -> str:
        raw = request.provider_model_id or request.requested_model.removeprefix(
            f"{self.provider_id}/"
        )
        return raw.removeprefix("models/")

    def _build_gemini_payload(self, request: CanonicalInternalRequest) -> dict[str, Any]:
        contents: list[dict[str, Any]] = []
        sys_parts: list[dict[str, str]] = [
            {"text": s} for s in request.system_instructions if s
        ]

        for msg in request.messages:
            if msg.role in ("system", "developer"):
                sys_parts.append({"text": msg.combined_text()})
                continue

            role = "model" if msg.role == "assistant" else "user"
            parts: list[dict[str, Any]] = []
            for p in msg.parts:
                if p.type == "text" and p.text is not None:
                    parts.append({"text": p.text})
                elif p.type == "image_base64" and p.media_base64:
                    parts.append(
                        {
                            "inlineData": {
                                "mimeType": p.mime_type or "image/png",
                                "data": p.media_base64,
                            }
                        }
                    )
                elif p.type == "tool_call" and p.tool_call:
                    parts.append(
                        {
                            "functionCall": {
                                "name": p.tool_call.name,
                                "args": p.tool_call.arguments_dict,
                            }
                        }
                    )
                elif p.type == "tool_result" and p.tool_result:
                    parts.append(
                        {
                            "functionResponse": {
                                "name": p.tool_result.name or "tool",
                                "response": {"result": p.tool_result.output_text},
                            }
                        }
                    )
            if parts:
                if contents and contents[-1]["role"] == role:
                    contents[-1]["parts"].extend(parts)
                else:
                    contents.append({"role": role, "parts": parts})

        payload: dict[str, Any] = {"contents": contents}
        if sys_parts:
            payload["systemInstruction"] = {"parts": sys_parts}

        gen_config: dict[str, Any] = {}
        if request.temperature is not None:
            gen_config["temperature"] = request.temperature
        if request.top_p is not None:
            gen_config["topP"] = request.top_p
        if request.max_output_tokens is not None:
            gen_config["maxOutputTokens"] = request.max_output_tokens
        if request.stop_sequences:
            gen_config["stopSequences"] = request.stop_sequences
        if request.response_format is not None:
            if request.response_format.type == "json_object":
                gen_config["responseMimeType"] = "application/json"
            elif request.response_format.type == "json_schema":
                gen_config["responseMimeType"] = "application/json"
                if request.response_format.json_schema:
                    gen_config["responseSchema"] = request.response_format.json_schema
        if gen_config:
            payload["generationConfig"] = gen_config

        if request.tools:
            fn_decls = [
                {
                    "name": t.name,
                    "description": t.description or "",
                    "parameters": t.parameters_schema,
                }
                for t in request.tools
                if not t.is_provider_native
            ]
            if fn_decls:
                payload["tools"] = [{"functionDeclarations": fn_decls}]
        return payload

    async def execute(
        self,
        request: CanonicalInternalRequest,
        credential: AdapterCredential,
        timeout: TimeoutPolicy | None = None,
    ) -> CanonicalInternalResponse:
        client = await self.get_http_client(timeout_policy=timeout)
        headers = self.build_auth_headers(credential)
        model_id = self._normalize_model_path(request)
        start_ns = time.perf_counter_ns()

        if request.source_protocol in (
            ProtocolType.GEMINI_EMBED_CONTENT,
            ProtocolType.OPENAI_EMBEDDINGS,
        ):
            text_val = (
                request.input_texts[0]
                if request.input_texts
                else (request.messages[0].combined_text() if request.messages else "")
            )
            emb_payload = {
                "model": f"models/{model_id}",
                "content": {"parts": [{"text": text_val}]},
            }
            resp = await client.post(
                f"/v1beta/models/{model_id}:embedContent",
                headers=headers,
                json=emb_payload,
            )
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            if resp.status_code >= 400:
                self._raise_for_upstream_error(resp.status_code, resp_headers, resp.text)
            body = resp.json()
            values = body.get("embedding", {}).get("values", [])
            elapsed_ms = (time.perf_counter_ns() - start_ns) / 1_000_000.0
            return CanonicalInternalResponse(
                request_id=request.request_id,
                provider_id=self.provider_id,
                model=model_id,
                embeddings=[values] if values else [],
                latency_ms=round(elapsed_ms, 2),
            )

        payload = self._build_gemini_payload(request)
        resp = await client.post(
            f"/v1beta/models/{model_id}:generateContent",
            headers=headers,
            json=payload,
        )
        resp_headers = {k.lower(): v for k, v in resp.headers.items()}
        if resp.status_code >= 400:
            self._raise_for_upstream_error(resp.status_code, resp_headers, resp.text)

        body = resp.json()
        usage, rate_limit = self.extract_usage_and_ratelimits(resp_headers, body)
        candidates = body.get("candidates", [])
        first_cand = candidates[0] if candidates else {}
        content_obj = first_cand.get("content", {})
        parts_out: list[CanonicalMessagePart] = []
        tool_calls: list[NormalizedToolCall] = []

        for part in content_obj.get("parts", []):
            if "text" in part:
                parts_out.append(CanonicalMessagePart(type="text", text=str(part["text"])))
            if "functionCall" in part:
                fc = part["functionCall"]
                args_dict = fc.get("args", {})
                norm_tc = NormalizedToolCall(
                    call_id=str(fc.get("id") or f"call_{uuid.uuid4().hex[:12]}"),
                    name=str(fc.get("name", "")),
                    arguments_json=json.dumps(args_dict),
                    arguments_dict=args_dict if isinstance(args_dict, dict) else {},
                )
                tool_calls.append(norm_tc)
                parts_out.append(CanonicalMessagePart(type="tool_call", tool_call=norm_tc))

        finish_reason_raw = first_cand.get("finishReason", "STOP")
        finish_reason = "tool_calls" if tool_calls else (
            "length" if finish_reason_raw == "MAX_TOKENS" else (
                "content_filter" if finish_reason_raw in ("SAFETY", "RECITATION") else "stop"
            )
        )
        elapsed_ms = (time.perf_counter_ns() - start_ns) / 1_000_000.0
        return CanonicalInternalResponse(
            request_id=request.request_id,
            provider_id=self.provider_id,
            model=model_id,
            provider_response_id=body.get("responseId"),
            output_message=CanonicalMessage(role="assistant", parts=parts_out),
            tool_calls=tool_calls,
            finish_reason=finish_reason,  # type: ignore[arg-type]
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
        model_id = self._normalize_model_path(request)
        payload = self._build_gemini_payload(request)

        async with client.stream(
            "POST",
            f"/v1beta/models/{model_id}:streamGenerateContent",
            params={"alt": "sse"},
            headers=headers,
            json=payload,
        ) as resp:
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            if resp.status_code >= 400:
                body_bytes = await resp.aread()
                self._raise_for_upstream_error(
                    resp.status_code, resp_headers, body_bytes.decode("utf-8", errors="replace")
                )

            async for _event_name, data_str in iter_sse_events(resp):
                try:
                    chunk = json.loads(data_str)
                except ValueError:
                    continue

                candidates = chunk.get("candidates", [])
                if candidates:
                    cand = candidates[0]
                    for part in cand.get("content", {}).get("parts", []):
                        if "text" in part:
                            yield CanonicalStreamEvent(
                                event_type="text_delta",
                                text_delta=str(part["text"]),
                                model=model_id,
                                raw_event=chunk,
                            )
                        if "functionCall" in part:
                            fc = part["functionCall"]
                            args_dict = fc.get("args", {})
                            yield CanonicalStreamEvent(
                                event_type="tool_call_delta",
                                tool_call_delta=NormalizedToolCall(
                                    call_id=str(fc.get("id") or f"call_{uuid.uuid4().hex[:12]}"),
                                    name=str(fc.get("name", "")),
                                    arguments_json=json.dumps(args_dict),
                                    arguments_dict=args_dict if isinstance(args_dict, dict) else {},
                                ),
                                model=model_id,
                                raw_event=chunk,
                            )
                    if cand.get("finishReason"):
                        yield CanonicalStreamEvent(
                            event_type="done",
                            finish_reason=str(cand["finishReason"]).lower(),
                            model=model_id,
                            raw_event=chunk,
                        )
                if chunk.get("usageMetadata"):
                    usage, _ = self.extract_usage_and_ratelimits(resp_headers, chunk)
                    if usage:
                        yield CanonicalStreamEvent(
                            event_type="usage",
                            usage=usage,
                            model=model_id,
                            raw_event=chunk,
                        )

    def extract_usage_and_ratelimits(
        self,
        headers: Mapping[str, str],
        body: Mapping[str, Any] | None,
    ) -> tuple[TokenUsage | None, RateLimitSnapshot | None]:
        usage: TokenUsage | None = None
        if body and isinstance(body.get("usageMetadata"), Mapping):
            um = body["usageMetadata"]
            in_tok = int(um.get("promptTokenCount", 0) or 0)
            out_tok = int(um.get("candidatesTokenCount", 0) or 0)
            total_tok = int(um.get("totalTokenCount", in_tok + out_tok) or 0)
            cached_tok = int(um.get("cachedContentTokenCount", 0) or 0)
            thoughts_tok = int(um.get("thoughtsTokenCount", 0) or 0)
            usage = TokenUsage(
                input_tokens=in_tok,
                output_tokens=out_tok,
                total_tokens=total_tok,
                cached_input_tokens=cached_tok,
                reasoning_tokens=thoughts_tok,
            )

        retry_after = parse_duration_seconds(headers.get("retry-after"))
        rate_limit = (
            RateLimitSnapshot(
                scope=QuotaScopeType.ACCOUNT,
                retry_after_seconds=retry_after,
            )
            if retry_after is not None
            else None
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

        rpc_status = str(err_obj.get("status") or f"HTTP_{status_code}")
        message = str(err_obj.get("message") or f"Gemini error (HTTP {status_code})")
        retry_after = parse_duration_seconds(headers.get("retry-after"))

        if status_code == 401 or rpc_status == "UNAUTHENTICATED":
            return ClassifiedProviderError(
                provider_id=self.provider_id,
                status_code=status_code,
                error_code=rpc_status,
                error_type="authentication_error",
                message=message,
                retryable=False,
                quota_scope=QuotaScopeType.CREDENTIAL,
                requires_credential_disable=True,
            )
        if status_code == 403 or rpc_status == "PERMISSION_DENIED":
            if "API_KEY_INVALID" in message or "API key not valid" in message:
                return ClassifiedProviderError(
                    provider_id=self.provider_id,
                    status_code=status_code,
                    error_code="API_KEY_INVALID",
                    error_type="authentication_error",
                    message=message,
                    retryable=False,
                    quota_scope=QuotaScopeType.CREDENTIAL,
                    requires_credential_disable=True,
                )
            return ClassifiedProviderError(
                provider_id=self.provider_id,
                status_code=status_code,
                error_code=rpc_status,
                error_type="permission_error",
                message=message,
                retryable=False,
                quota_scope=QuotaScopeType.ACCOUNT,
                requires_admin_attention=True,
            )
        if status_code == 429 or rpc_status == "RESOURCE_EXHAUSTED":
            return ClassifiedProviderError(
                provider_id=self.provider_id,
                status_code=status_code,
                error_code=rpc_status,
                error_type="rate_limit_error",
                message=message,
                retryable=True,
                quota_scope=QuotaScopeType.ACCOUNT,
                retry_after_seconds=retry_after or 10.0,
            )
        if status_code == 404 or rpc_status == "NOT_FOUND":
            return ClassifiedProviderError(
                provider_id=self.provider_id,
                status_code=status_code,
                error_code=rpc_status,
                error_type="not_found_error",
                message=message,
                retryable=False,
                quota_scope=QuotaScopeType.MODEL,
            )
        if status_code >= 500 or rpc_status in ("INTERNAL", "UNAVAILABLE", "DEADLINE_EXCEEDED"):
            return ClassifiedProviderError(
                provider_id=self.provider_id,
                status_code=status_code,
                error_code=rpc_status,
                error_type="upstream_server_error",
                message=message,
                retryable=True,
                quota_scope=QuotaScopeType.ENDPOINT,
                retry_after_seconds=retry_after or 2.0,
            )
        return ClassifiedProviderError(
            provider_id=self.provider_id,
            status_code=status_code,
            error_code=rpc_status,
            error_type="invalid_request_error",
            message=message,
            retryable=False,
        )

