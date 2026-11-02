"""Custom HTTP Provider Adapter for user-registered REST AI services."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
import ipaddress
import json
import time
from typing import Any
from urllib.parse import urlparse

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
    RateLimitSnapshot,
    TokenUsage,
)
from gateway.core.http import HTTPXClientPoolManager, TimeoutPolicy
from gateway.provider_adapters.base import (
    BaseProviderAdapter,
    ConfigValidationResult,
    ProviderConfig,
    iter_sse_events,
    parse_duration_seconds,
)


def extract_dot_path(data: Any, dot_path: str) -> Any:
    """Extracts a nested value from dicts/lists using a dot-separated path like 'choices.0.message.content'."""
    current = data
    for segment in dot_path.split("."):
        if current is None:
            return None
        if isinstance(current, Mapping):
            current = current.get(segment)
        elif isinstance(current, list):
            try:
                idx = int(segment)
                current = current[idx] if 0 <= idx < len(current) else None
            except ValueError:
                return None
        else:
            return None
    return current


class CustomHTTPProviderAdapter(BaseProviderAdapter):
    """Configurable REST adapter for custom AI providers and self-hosted endpoints."""

    adapter_type = "custom_http"
    supported_protocols = frozenset(
        {ProtocolType.CUSTOM_HTTP, ProtocolType.OPENAI_CHAT}
    )

    def __init__(
        self,
        http_pool: HTTPXClientPoolManager,
        config: ProviderConfig,
    ) -> None:
        self.provider_id = config.provider_id
        self.display_name = config.display_name
        self.default_base_url = config.base_url
        super().__init__(http_pool=http_pool, config=config)

    def get_default_config(self) -> ProviderConfig:
        return self._config

    def validate_configuration(
        self, config: ProviderConfig | None = None
    ) -> ConfigValidationResult:
        res = super().validate_configuration(config)
        target = config or self._config
        parsed = urlparse(target.base_url)
        host = (parsed.hostname or "").lower()

        if not target.allow_local_network and host:
            if host in ("localhost", "metadata.google.internal"):
                res.errors.append(
                    f"Host {host!r} is blocked by SSRF guard unless allow_local_network=True"
                )
            else:
                try:
                    ip = ipaddress.ip_address(host)
                    if ip.is_loopback or ip.is_private or ip.is_link_local:
                        res.errors.append(
                            f"Private/local IP {host!r} is blocked by SSRF guard unless allow_local_network=True"
                        )
                except ValueError:
                    pass

        res.valid = len(res.errors) == 0
        return res

    def get_capability_manifest(self) -> ProviderCapabilityManifest:
        custom = self._config.custom_settings
        raw_supported = custom.get("supported_capabilities", ["chat_completions"])
        supported_flags: set[CapabilityFlag] = set()
        for item in raw_supported:
            try:
                supported_flags.add(CapabilityFlag(item))
            except ValueError:
                pass
        raw_unsupported = custom.get("unsupported_capabilities", [])
        unsupported_flags: set[CapabilityFlag] = set()
        for item in raw_unsupported:
            try:
                unsupported_flags.add(CapabilityFlag(item))
            except ValueError:
                pass

        return ProviderCapabilityManifest(
            provider_id=self.provider_id,
            display_name=self.display_name,
            adapter_type=self.adapter_type,
            default_base_url=self.base_url,
            supported_protocols=list(self.supported_protocols),
            supported_quota_scopes=[
                QuotaScopeType.CREDENTIAL,
                QuotaScopeType.ENDPOINT,
                QuotaScopeType.CONCURRENCY,
            ],
            capabilities=build_capability_map(supported_flags, unsupported_flags),
            auth_header_name=self._config.auth.header_name,
            auth_header_prefix=self._config.auth.header_prefix,
            notes="Custom declarative REST provider adapter.",
        )

    async def discover_models(
        self, credential: AdapterCredential | None = None
    ) -> list[DiscoveredModel]:
        custom = self._config.custom_settings
        static_models = custom.get("static_models")
        if isinstance(static_models, list) and static_models:
            return [
                DiscoveredModel(
                    provider_id=self.provider_id,
                    provider_model_id=str(m),
                    canonical_model_id=f"{self.provider_id}/{m}",
                    display_name=str(m),
                    supported_protocols=list(self.supported_protocols),
                    capabilities=self.get_capability_manifest().capabilities,
                )
                for m in static_models
            ]

        models_endpoint = str(custom.get("models_endpoint", "/v1/models"))
        client = await self.get_http_client()
        resp = await client.get(
            models_endpoint, headers=self.build_auth_headers(credential)
        )
        resp_headers = {k.lower(): v for k, v in resp.headers.items()}
        if resp.status_code >= 400:
            self._raise_for_upstream_error(resp.status_code, resp_headers, resp.text)

        payload = resp.json()
        models_list_path = str(custom.get("models_list_path", "data"))
        items = extract_dot_path(payload, models_list_path)
        if not isinstance(items, list):
            items = payload if isinstance(payload, list) else []

        discovered: list[DiscoveredModel] = []
        for item in items:
            model_id = (
                str(item.get("id") or item.get("name") or "")
                if isinstance(item, Mapping)
                else str(item)
            )
            if not model_id:
                continue
            discovered.append(
                DiscoveredModel(
                    provider_id=self.provider_id,
                    provider_model_id=model_id,
                    canonical_model_id=f"{self.provider_id}/{model_id}",
                    display_name=model_id,
                    supported_protocols=list(self.supported_protocols),
                    capabilities=self.get_capability_manifest().capabilities,
                    raw_metadata=dict(item) if isinstance(item, Mapping) else {},
                )
            )
        return discovered

    def _build_custom_request_payload(
        self, request: CanonicalInternalRequest, *, stream: bool
    ) -> dict[str, Any]:
        custom = self._config.custom_settings
        model_id = request.provider_model_id or request.requested_model.removeprefix(
            f"{self.provider_id}/"
        )
        template_mode = custom.get("request_format", "openai_compatible")
        if template_mode == "prompt_only":
            prompt_field = str(custom.get("prompt_field", "prompt"))
            combined_prompt = "\n".join(
                [*request.system_instructions, *(m.combined_text() for m in request.messages)]
            )
            payload: dict[str, Any] = {
                "model": model_id,
                prompt_field: combined_prompt,
                "stream": stream,
            }
            if request.max_output_tokens is not None:
                payload[str(custom.get("max_tokens_field", "max_tokens"))] = (
                    request.max_output_tokens
                )
            return payload

        messages = [
            {"role": "system", "content": s} for s in request.system_instructions
        ] + [
            {"role": m.role, "content": m.combined_text()} for m in request.messages
        ]
        payload = {
            "model": model_id,
            "messages": messages,
            "stream": stream,
        }
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_output_tokens is not None:
            payload["max_tokens"] = request.max_output_tokens
        return payload

    async def execute(
        self,
        request: CanonicalInternalRequest,
        credential: AdapterCredential,
        timeout: TimeoutPolicy | None = None,
    ) -> CanonicalInternalResponse:
        client = await self.get_http_client(timeout_policy=timeout)
        custom = self._config.custom_settings
        chat_endpoint = str(custom.get("chat_endpoint", "/v1/chat/completions"))
        response_text_path = str(
            custom.get("response_text_path", "choices.0.message.content")
        )
        headers = self.build_auth_headers(credential)
        params: dict[str, str] = {}
        if self._config.auth.auth_type == "query" and self._config.auth.query_param_name:
            params[self._config.auth.query_param_name] = credential.get_secret_value()

        payload = self._build_custom_request_payload(request, stream=False)
        start_ns = time.perf_counter_ns()
        resp = await client.post(
            chat_endpoint, params=params, headers=headers, json=payload
        )
        resp_headers = {k.lower(): v for k, v in resp.headers.items()}
        if resp.status_code >= 400:
            self._raise_for_upstream_error(resp.status_code, resp_headers, resp.text)

        body = resp.json()
        extracted_text = extract_dot_path(body, response_text_path)
        text_val = str(extracted_text) if extracted_text is not None else ""
        usage, rate_limit = self.extract_usage_and_ratelimits(resp_headers, body)
        elapsed_ms = (time.perf_counter_ns() - start_ns) / 1_000_000.0
        return CanonicalInternalResponse(
            request_id=request.request_id,
            provider_id=self.provider_id,
            model=str(body.get("model", payload["model"])),
            provider_response_id=body.get("id"),
            output_message=CanonicalMessage(
                role="assistant",
                parts=[CanonicalMessagePart(type="text", text=text_val)],
            ),
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
        custom = self._config.custom_settings
        chat_endpoint = str(custom.get("chat_endpoint", "/v1/chat/completions"))
        delta_path = str(custom.get("stream_delta_path", "choices.0.delta.content"))
        headers = self.build_auth_headers(credential)
        payload = self._build_custom_request_payload(request, stream=True)

        async with client.stream(
            "POST", chat_endpoint, headers=headers, json=payload
        ) as resp:
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            if resp.status_code >= 400:
                body_bytes = await resp.aread()
                self._raise_for_upstream_error(
                    resp.status_code,
                    resp_headers,
                    body_bytes.decode("utf-8", errors="replace"),
                )

            async for _event_name, data_str in iter_sse_events(resp):
                if data_str.strip() == "[DONE]":
                    yield CanonicalStreamEvent(event_type="done")
                    break
                try:
                    chunk = json.loads(data_str)
                except ValueError:
                    continue
                delta_val = extract_dot_path(chunk, delta_path)
                if delta_val:
                    yield CanonicalStreamEvent(
                        event_type="text_delta",
                        text_delta=str(delta_val),
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
            in_tok = int(u.get("prompt_tokens", u.get("input_tokens", 0)) or 0)
            out_tok = int(u.get("completion_tokens", u.get("output_tokens", 0)) or 0)
            usage = TokenUsage(
                input_tokens=in_tok,
                output_tokens=out_tok,
                total_tokens=int(u.get("total_tokens", in_tok + out_tok) or 0),
            )
        retry_after = parse_duration_seconds(headers.get("retry-after"))
        rate_limit = (
            RateLimitSnapshot(
                scope=QuotaScopeType.CREDENTIAL,
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
        custom = self._config.custom_settings
        err_msg_path = str(custom.get("error_message_path", "error.message"))
        message = f"Custom provider {self.provider_id} error (HTTP {status_code})"
        if isinstance(body, Mapping):
            extracted = extract_dot_path(body, err_msg_path)
            if extracted:
                message = str(extracted)
        elif isinstance(body, (bytes, str)):
            raw_text = (
                body.decode("utf-8", errors="replace") if isinstance(body, bytes) else body
            )
            try:
                loaded = json.loads(raw_text)
                extracted = extract_dot_path(loaded, err_msg_path)
                if extracted:
                    message = str(extracted)
            except ValueError:
                message = raw_text[:300] or message

        retry_after = parse_duration_seconds(headers.get("retry-after"))
        if status_code == 401:
            return ClassifiedProviderError(
                provider_id=self.provider_id,
                status_code=401,
                error_code="CUSTOM_AUTH_ERROR",
                error_type="authentication_error",
                message=message,
                retryable=False,
                requires_credential_disable=True,
            )
        if status_code == 429:
            return ClassifiedProviderError(
                provider_id=self.provider_id,
                status_code=429,
                error_code="CUSTOM_RATE_LIMIT",
                error_type="rate_limit_error",
                message=message,
                retryable=True,
                retry_after_seconds=retry_after or 5.0,
            )
        if status_code >= 500:
            return ClassifiedProviderError(
                provider_id=self.provider_id,
                status_code=status_code,
                error_code=f"CUSTOM_HTTP_{status_code}",
                error_type="upstream_server_error",
                message=message,
                retryable=True,
                retry_after_seconds=retry_after or 2.0,
            )
        return ClassifiedProviderError(
            provider_id=self.provider_id,
            status_code=status_code,
            error_code=f"CUSTOM_HTTP_{status_code}",
            error_type="invalid_request_error",
            message=message,
            retryable=False,
        )

