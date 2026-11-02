"""Ollama Provider Adapter (Local self-hosted LLM server: /api/tags, /api/chat, /api/embed)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
import json
import time
from typing import Any
import uuid

from gateway.capability_registry.models import (
    CapabilityFlag,
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
)


class OllamaProviderAdapter(BaseProviderAdapter):
    """Official Ollama local/self-hosted model server adapter."""

    provider_id = "ollama"
    display_name = "Ollama (Local/Self-Hosted)"
    adapter_type = "builtin_ollama"
    default_base_url = "http://localhost:11434"
    supported_protocols = frozenset(
        {
            ProtocolType.OLLAMA_NATIVE,
            ProtocolType.OPENAI_CHAT,
            ProtocolType.OPENAI_EMBEDDINGS,
        }
    )

    def get_default_config(self) -> ProviderConfig:
        return ProviderConfig(
            provider_id=self.provider_id,
            display_name=self.display_name,
            adapter_type=self.adapter_type,
            base_url=self.default_base_url,
            allow_local_network=True,
            auth=AuthConfiguration(auth_type="none", header_name="", header_prefix=""),
        )

    def get_capability_manifest(self) -> ProviderCapabilityManifest:
        supported = {
            CapabilityFlag.CHAT_COMPLETIONS,
            CapabilityFlag.EMBEDDINGS,
            CapabilityFlag.TOOL_CALLING,
            CapabilityFlag.STRUCTURED_OUTPUTS,
            CapabilityFlag.JSON_MODE,
            CapabilityFlag.STREAMING,
            CapabilityFlag.SYSTEM_INSTRUCTIONS,
            CapabilityFlag.VISION,
            CapabilityFlag.MODEL_DISCOVERY,
        }
        unsupported = {
            CapabilityFlag.RESPONSES_API,
            CapabilityFlag.MESSAGES_API,
            CapabilityFlag.GEMINI_GENERATE_CONTENT,
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
                QuotaScopeType.CONCURRENCY,
                QuotaScopeType.ENDPOINT,
            ],
            capabilities=build_capability_map(supported, unsupported),
            auth_header_name="Authorization",
            auth_header_prefix="Bearer ",
            notes="Local Ollama server using /api/tags, /api/chat (NDJSON stream), and /api/embed.",
        )

    async def discover_models(
        self, credential: AdapterCredential | None = None
    ) -> list[DiscoveredModel]:
        client = await self.get_http_client()
        resp = await client.get("/api/tags", headers=self.build_auth_headers(credential))
        resp_headers = {k.lower(): v for k, v in resp.headers.items()}
        if resp.status_code >= 400:
            self._raise_for_upstream_error(resp.status_code, resp_headers, resp.text)

        payload = resp.json()
        models: list[DiscoveredModel] = []
        for item in payload.get("models", []):
            model_id = str(item.get("name") or item.get("model") or "")
            if not model_id:
                continue

            is_embed = "embed" in model_id.lower() or "nomic-embed" in model_id.lower()
            if is_embed:
                caps = build_capability_map(
                    supported={CapabilityFlag.EMBEDDINGS},
                    unsupported={CapabilityFlag.CHAT_COMPLETIONS, CapabilityFlag.STREAMING},
                )
                protocols = [ProtocolType.OLLAMA_NATIVE, ProtocolType.OPENAI_EMBEDDINGS]
            else:
                # Only mark verified baseline chat/streaming capabilities; leave tool_calling/vision UNKNOWN unless inspected
                caps = build_capability_map(
                    supported={
                        CapabilityFlag.CHAT_COMPLETIONS,
                        CapabilityFlag.STREAMING,
                        CapabilityFlag.SYSTEM_INSTRUCTIONS,
                        CapabilityFlag.JSON_MODE,
                        CapabilityFlag.STRUCTURED_OUTPUTS,
                    },
                    unsupported={
                        CapabilityFlag.RESPONSES_API,
                        CapabilityFlag.MESSAGES_API,
                        CapabilityFlag.TTS,
                        CapabilityFlag.STT,
                    },
                )
                protocols = [ProtocolType.OLLAMA_NATIVE, ProtocolType.OPENAI_CHAT]

            models.append(
                DiscoveredModel(
                    provider_id=self.provider_id,
                    provider_model_id=model_id,
                    canonical_model_id=f"{self.provider_id}/{model_id}",
                    display_name=model_id,
                    supported_protocols=protocols,
                    context_length=None,  # /api/tags does not expose context window; explicitly None (UNKNOWN)
                    capabilities=caps,
                    input_cost_per_1m_usd=0.0,
                    output_cost_per_1m_usd=0.0,
                    raw_metadata=item,
                )
            )
        return models

    def _build_ollama_chat_payload(
        self, request: CanonicalInternalRequest, *, stream: bool
    ) -> dict[str, Any]:
        model_name = request.provider_model_id or request.requested_model.removeprefix(
            f"{self.provider_id}/"
        )
        messages: list[dict[str, Any]] = []
        for sys_text in request.system_instructions:
            messages.append({"role": "system", "content": sys_text})
        for msg in request.messages:
            images: list[str] = [
                p.media_base64
                for p in msg.parts
                if p.type == "image_base64" and p.media_base64
            ]
            entry: dict[str, Any] = {
                "role": msg.role if msg.role != "developer" else "system",
                "content": msg.combined_text(),
            }
            if images:
                entry["images"] = images
            messages.append(entry)

        payload: dict[str, Any] = {
            "model": model_name,
            "messages": messages,
            "stream": stream,
        }
        options: dict[str, Any] = {}
        if request.temperature is not None:
            options["temperature"] = request.temperature
        if request.top_p is not None:
            options["top_p"] = request.top_p
        if request.max_output_tokens is not None:
            options["num_predict"] = request.max_output_tokens
        if request.stop_sequences:
            options["stop"] = request.stop_sequences
        if options:
            payload["options"] = options

        if request.response_format is not None:
            if request.response_format.type == "json_object":
                payload["format"] = "json"
            elif (
                request.response_format.type == "json_schema"
                and request.response_format.json_schema
            ):
                payload["format"] = request.response_format.json_schema

        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description or "",
                        "parameters": t.parameters_schema,
                    },
                }
                for t in request.tools
                if not t.is_provider_native
            ]
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
        model_name = request.provider_model_id or request.requested_model.removeprefix(
            f"{self.provider_id}/"
        )

        if request.source_protocol == ProtocolType.OPENAI_EMBEDDINGS:
            emb_payload = {
                "model": model_name,
                "input": request.input_texts
                or [m.combined_text() for m in request.messages],
            }
            resp = await client.post("/api/embed", headers=headers, json=emb_payload)
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            if resp.status_code >= 400:
                self._raise_for_upstream_error(resp.status_code, resp_headers, resp.text)
            body = resp.json()
            usage, _ = self.extract_usage_and_ratelimits(resp_headers, body)
            elapsed_ms = (time.perf_counter_ns() - start_ns) / 1_000_000.0
            return CanonicalInternalResponse(
                request_id=request.request_id,
                provider_id=self.provider_id,
                model=body.get("model", model_name),
                embeddings=body.get("embeddings", []),
                usage=usage or TokenUsage(),
                latency_ms=round(elapsed_ms, 2),
            )

        payload = self._build_ollama_chat_payload(request, stream=False)
        resp = await client.post("/api/chat", headers=headers, json=payload)
        resp_headers = {k.lower(): v for k, v in resp.headers.items()}
        if resp.status_code >= 400:
            self._raise_for_upstream_error(resp.status_code, resp_headers, resp.text)

        body = resp.json()
        usage, rate_limit = self.extract_usage_and_ratelimits(resp_headers, body)
        msg_obj = body.get("message", {})
        parts: list[CanonicalMessagePart] = []
        if msg_obj.get("content"):
            parts.append(CanonicalMessagePart(type="text", text=str(msg_obj["content"])))

        tool_calls: list[NormalizedToolCall] = []
        for tc in msg_obj.get("tool_calls", []) or []:
            fn = tc.get("function", {})
            args = fn.get("arguments", {})
            args_dict = args if isinstance(args, dict) else {}
            norm_tc = NormalizedToolCall(
                call_id=f"call_{uuid.uuid4().hex[:12]}",
                name=str(fn.get("name", "")),
                arguments_json=json.dumps(args_dict),
                arguments_dict=args_dict,
            )
            tool_calls.append(norm_tc)
            parts.append(CanonicalMessagePart(type="tool_call", tool_call=norm_tc))

        elapsed_ms = (time.perf_counter_ns() - start_ns) / 1_000_000.0
        return CanonicalInternalResponse(
            request_id=request.request_id,
            provider_id=self.provider_id,
            model=str(body.get("model", model_name)),
            output_message=CanonicalMessage(role="assistant", parts=parts),
            tool_calls=tool_calls,
            finish_reason="tool_calls" if tool_calls else "stop",
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
        payload = self._build_ollama_chat_payload(request, stream=True)

        async with client.stream("POST", "/api/chat", headers=headers, json=payload) as resp:
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            if resp.status_code >= 400:
                body_bytes = await resp.aread()
                self._raise_for_upstream_error(
                    resp.status_code, resp_headers, body_bytes.decode("utf-8", errors="replace")
                )

            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                try:
                    chunk = json.loads(line)
                except ValueError:
                    continue

                msg_obj = chunk.get("message", {})
                if msg_obj.get("content"):
                    yield CanonicalStreamEvent(
                        event_type="text_delta",
                        text_delta=str(msg_obj["content"]),
                        model=chunk.get("model"),
                        raw_event=chunk,
                    )
                if chunk.get("done"):
                    usage, _ = self.extract_usage_and_ratelimits(resp_headers, chunk)
                    if usage:
                        yield CanonicalStreamEvent(
                            event_type="usage",
                            usage=usage,
                            model=chunk.get("model"),
                            raw_event=chunk,
                        )
                    yield CanonicalStreamEvent(
                        event_type="done",
                        finish_reason=str(chunk.get("done_reason") or "stop"),
                        model=chunk.get("model"),
                        raw_event=chunk,
                    )

    def extract_usage_and_ratelimits(
        self,
        headers: Mapping[str, str],
        body: Mapping[str, Any] | None,
    ) -> tuple[TokenUsage | None, RateLimitSnapshot | None]:
        if not body:
            return None, None
        in_tok = int(body.get("prompt_eval_count", 0) or 0)
        out_tok = int(body.get("eval_count", 0) or 0)
        if in_tok == 0 and out_tok == 0:
            return None, None
        return (
            TokenUsage(
                input_tokens=in_tok,
                output_tokens=out_tok,
                total_tokens=in_tok + out_tok,
            ),
            None,
        )

    def classify_error(
        self,
        status_code: int,
        headers: Mapping[str, str],
        body: bytes | str | Mapping[str, Any] | None,
    ) -> ClassifiedProviderError:
        message = f"Ollama error (HTTP {status_code})"
        if isinstance(body, Mapping) and body.get("error"):
            message = str(body["error"])
        elif isinstance(body, (bytes, str)):
            raw_text = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else body
            try:
                loaded = json.loads(raw_text)
                if isinstance(loaded, dict) and loaded.get("error"):
                    message = str(loaded["error"])
            except ValueError:
                message = raw_text[:300] or message

        if status_code == 404:
            return ClassifiedProviderError(
                provider_id=self.provider_id,
                status_code=404,
                error_code="model_not_found",
                error_type="not_found_error",
                message=message,
                retryable=False,
                quota_scope=QuotaScopeType.MODEL,
            )
        if status_code == 429 or status_code >= 500:
            return ClassifiedProviderError(
                provider_id=self.provider_id,
                status_code=status_code,
                error_code=f"OLLAMA_HTTP_{status_code}",
                error_type="upstream_server_error" if status_code >= 500 else "rate_limit_error",
                message=message,
                retryable=True,
                quota_scope=QuotaScopeType.CONCURRENCY,
                retry_after_seconds=2.0,
            )
        return ClassifiedProviderError(
            provider_id=self.provider_id,
            status_code=status_code,
            error_code=f"OLLAMA_HTTP_{status_code}",
            error_type="invalid_request_error",
            message=message,
            retryable=False,
        )

