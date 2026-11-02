"""ElevenLabs Provider Adapter (TTS, Streaming Audio, STT, Voice & Model Discovery)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
import json
import time
from typing import Any

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
    RateLimitSnapshot,
    TokenUsage,
)
from gateway.core.exceptions import UnsupportedCapabilityError
from gateway.core.http import TimeoutPolicy
from gateway.provider_adapters.base import (
    AuthConfiguration,
    BaseProviderAdapter,
    ProviderConfig,
    parse_duration_seconds,
    parse_int_header,
)


class ElevenLabsProviderAdapter(BaseProviderAdapter):
    """Official ElevenLabs Audio API provider adapter."""

    provider_id = "elevenlabs"
    display_name = "ElevenLabs"
    adapter_type = "builtin_elevenlabs"
    default_base_url = "https://api.elevenlabs.io"
    supported_protocols = frozenset(
        {
            ProtocolType.ELEVENLABS_TTS,
            ProtocolType.ELEVENLABS_STT,
            ProtocolType.ELEVENLABS_VOICES,
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
                header_name="xi-api-key",
                header_prefix="",
            ),
        )

    def get_capability_manifest(self) -> ProviderCapabilityManifest:
        supported = {
            CapabilityFlag.TTS,
            CapabilityFlag.STT,
            CapabilityFlag.VOICE_DISCOVERY,
            CapabilityFlag.MODEL_DISCOVERY,
            CapabilityFlag.STREAMING,
        }
        unsupported = {
            CapabilityFlag.CHAT_COMPLETIONS,
            CapabilityFlag.RESPONSES_API,
            CapabilityFlag.MESSAGES_API,
            CapabilityFlag.GEMINI_GENERATE_CONTENT,
            CapabilityFlag.EMBEDDINGS,
            CapabilityFlag.TOOL_CALLING,
            CapabilityFlag.PARALLEL_TOOL_CALLS,
            CapabilityFlag.STRUCTURED_OUTPUTS,
            CapabilityFlag.JSON_MODE,
            CapabilityFlag.SYSTEM_INSTRUCTIONS,
            CapabilityFlag.REASONING_TOKENS,
            CapabilityFlag.VISION,
        }
        return ProviderCapabilityManifest(
            provider_id=self.provider_id,
            display_name=self.display_name,
            adapter_type=self.adapter_type,
            default_base_url=self.default_base_url,
            supported_protocols=list(self.supported_protocols),
            supported_quota_scopes=[
                QuotaScopeType.ACCOUNT,
                QuotaScopeType.CREDENTIAL,
                QuotaScopeType.CONCURRENCY,
            ],
            capabilities=build_capability_map(supported, unsupported),
            auth_header_name="xi-api-key",
            auth_header_prefix="",
            notes="Audio-specialized provider supporting TTS, STT, streaming audio, and voice discovery.",
        )

    async def discover_models(
        self, credential: AdapterCredential | None = None
    ) -> list[DiscoveredModel]:
        client = await self.get_http_client()
        resp = await client.get(
            "/v1/models", headers=self.build_auth_headers(credential)
        )
        resp_headers = {k.lower(): v for k, v in resp.headers.items()}
        if resp.status_code >= 400:
            self._raise_for_upstream_error(resp.status_code, resp_headers, resp.text)

        payload = resp.json()
        items = payload if isinstance(payload, list) else payload.get("models", [])
        models: list[DiscoveredModel] = []
        for item in items:
            model_id = str(item.get("model_id") or item.get("id") or "")
            if not model_id:
                continue
            can_tts = bool(item.get("can_do_text_to_speech", True))
            supported_caps = {CapabilityFlag.STREAMING}
            if can_tts:
                supported_caps.add(CapabilityFlag.TTS)
            if "scribe" in model_id.lower() or item.get("can_do_speech_to_text"):
                supported_caps.add(CapabilityFlag.STT)

            models.append(
                DiscoveredModel(
                    provider_id=self.provider_id,
                    provider_model_id=model_id,
                    canonical_model_id=f"{self.provider_id}/{model_id}",
                    display_name=str(item.get("name") or model_id),
                    supported_protocols=list(self.supported_protocols),
                    context_length=item.get("maximum_text_length_per_request"),
                    capabilities=build_capability_map(
                        supported=supported_caps,
                        unsupported={
                            CapabilityFlag.CHAT_COMPLETIONS,
                            CapabilityFlag.EMBEDDINGS,
                            CapabilityFlag.TOOL_CALLING,
                        },
                    ),
                    raw_metadata=item,
                )
            )
        return models

    async def discover_voices(
        self, credential: AdapterCredential | None = None
    ) -> list[dict[str, Any]]:
        """Queries /v1/voices to retrieve available ElevenLabs voice profiles."""
        client = await self.get_http_client()
        resp = await client.get(
            "/v1/voices", headers=self.build_auth_headers(credential)
        )
        resp_headers = {k.lower(): v for k, v in resp.headers.items()}
        if resp.status_code >= 400:
            self._raise_for_upstream_error(resp.status_code, resp_headers, resp.text)
        payload = resp.json()
        return list(payload.get("voices", []))

    async def execute(
        self,
        request: CanonicalInternalRequest,
        credential: AdapterCredential,
        timeout: TimeoutPolicy | None = None,
    ) -> CanonicalInternalResponse:
        client = await self.get_http_client(timeout_policy=timeout)
        headers = self.build_auth_headers(credential)
        start_ns = time.perf_counter_ns()
        model_id = request.provider_model_id or request.requested_model.removeprefix(
            f"{self.provider_id}/"
        )

        if request.source_protocol == ProtocolType.ELEVENLABS_STT:
            if not request.audio_bytes:
                raise UnsupportedCapabilityError(
                    "ElevenLabs STT requires audio_bytes in the request",
                    param="file",
                )
            files = {
                "file": (
                    request.audio_filename or "audio.mp3",
                    request.audio_bytes,
                    "audio/mpeg",
                )
            }
            data = {"model_id": model_id or "scribe_v1"}
            resp = await client.post(
                "/v1/speech-to-text", headers=headers, data=data, files=files
            )
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            if resp.status_code >= 400:
                self._raise_for_upstream_error(resp.status_code, resp_headers, resp.text)
            body = resp.json()
            text_out = str(body.get("text", ""))
            elapsed_ms = (time.perf_counter_ns() - start_ns) / 1_000_000.0
            return CanonicalInternalResponse(
                request_id=request.request_id,
                provider_id=self.provider_id,
                model=model_id or "scribe_v1",
                output_message=CanonicalMessage(
                    role="assistant",
                    parts=[CanonicalMessagePart(type="text", text=text_out)],
                ),
                usage=TokenUsage(characters=len(text_out)),
                latency_ms=round(elapsed_ms, 2),
            )

        if request.source_protocol not in (
            ProtocolType.ELEVENLABS_TTS,
            ProtocolType.OPENAI_CHAT,
        ) and not request.voice_id:
            raise UnsupportedCapabilityError(
                f"ElevenLabs adapter does not support protocol {request.source_protocol}",
                param="protocol",
            )

        voice_id = request.voice_id or request.provider_extensions.get(
            "voice_id", "21m00Tcm4TlvDq8ikWAM"
        )
        text_input = (
            request.input_texts[0]
            if request.input_texts
            else (request.messages[-1].combined_text() if request.messages else "")
        )
        tts_payload: dict[str, Any] = {
            "text": text_input,
            "model_id": model_id or "eleven_multilingual_v2",
        }
        if "voice_settings" in request.provider_extensions:
            tts_payload["voice_settings"] = request.provider_extensions["voice_settings"]

        output_format = request.provider_extensions.get("output_format", "mp3_44100_128")
        resp = await client.post(
            f"/v1/text-to-speech/{voice_id}",
            params={"output_format": output_format},
            headers=headers,
            json=tts_payload,
        )
        resp_headers = {k.lower(): v for k, v in resp.headers.items()}
        if resp.status_code >= 400:
            self._raise_for_upstream_error(resp.status_code, resp_headers, resp.text)

        usage, rate_limit = self.extract_usage_and_ratelimits(
            resp_headers, {"text_length": len(text_input)}
        )
        elapsed_ms = (time.perf_counter_ns() - start_ns) / 1_000_000.0
        return CanonicalInternalResponse(
            request_id=request.request_id,
            provider_id=self.provider_id,
            model=tts_payload["model_id"],
            provider_response_id=resp_headers.get("request-id"),
            audio_content=resp.content,
            audio_mime_type=resp_headers.get("content-type", "audio/mpeg"),
            usage=usage or TokenUsage(characters=len(text_input)),
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
        model_id = request.provider_model_id or request.requested_model.removeprefix(
            f"{self.provider_id}/"
        )
        voice_id = request.voice_id or request.provider_extensions.get(
            "voice_id", "21m00Tcm4TlvDq8ikWAM"
        )
        text_input = (
            request.input_texts[0]
            if request.input_texts
            else (request.messages[-1].combined_text() if request.messages else "")
        )
        tts_payload: dict[str, Any] = {
            "text": text_input,
            "model_id": model_id or "eleven_multilingual_v2",
        }
        if "voice_settings" in request.provider_extensions:
            tts_payload["voice_settings"] = request.provider_extensions["voice_settings"]

        async with client.stream(
            "POST",
            f"/v1/text-to-speech/{voice_id}/stream",
            headers=headers,
            json=tts_payload,
        ) as resp:
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            if resp.status_code >= 400:
                body_bytes = await resp.aread()
                self._raise_for_upstream_error(
                    resp.status_code,
                    resp_headers,
                    body_bytes.decode("utf-8", errors="replace"),
                )

            async for chunk_bytes in resp.aiter_bytes():
                if chunk_bytes:
                    yield CanonicalStreamEvent(
                        event_type="audio_chunk",
                        audio_bytes=chunk_bytes,
                        model=tts_payload["model_id"],
                    )
            char_count = parse_int_header(resp_headers, "x-character-count") or len(
                text_input
            )
            yield CanonicalStreamEvent(
                event_type="usage",
                usage=TokenUsage(characters=char_count),
                model=tts_payload["model_id"],
            )
            yield CanonicalStreamEvent(event_type="done", model=tts_payload["model_id"])

    def extract_usage_and_ratelimits(
        self,
        headers: Mapping[str, str],
        body: Mapping[str, Any] | None,
    ) -> tuple[TokenUsage | None, RateLimitSnapshot | None]:
        char_count = parse_int_header(headers, "x-character-count") or parse_int_header(
            headers, "character-cost"
        )
        if char_count is None and body and "text_length" in body:
            char_count = int(body["text_length"])
        usage = TokenUsage(characters=char_count) if char_count is not None else None
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
        detail_obj: dict[str, Any] = {}
        if isinstance(body, Mapping):
            d = body.get("detail", body)
            detail_obj = dict(d) if isinstance(d, Mapping) else {"message": str(d)}
        elif isinstance(body, (bytes, str)):
            raw_text = (
                body.decode("utf-8", errors="replace") if isinstance(body, bytes) else body
            )
            try:
                loaded = json.loads(raw_text)
                if isinstance(loaded, dict):
                    d = loaded.get("detail", loaded)
                    detail_obj = dict(d) if isinstance(d, Mapping) else {"message": str(d)}
            except ValueError:
                detail_obj = {"message": raw_text[:400]}

        err_status = str(detail_obj.get("status") or f"HTTP_{status_code}")
        message = str(
            detail_obj.get("message") or f"ElevenLabs error (HTTP {status_code})"
        )
        retry_after = parse_duration_seconds(headers.get("retry-after"))

        if status_code == 401 or err_status == "invalid_api_key":
            return ClassifiedProviderError(
                provider_id=self.provider_id,
                status_code=status_code,
                error_code=err_status,
                error_type="authentication_error",
                message=message,
                retryable=False,
                quota_scope=QuotaScopeType.CREDENTIAL,
                requires_credential_disable=True,
            )
        if err_status == "quota_exceeded":
            return ClassifiedProviderError(
                provider_id=self.provider_id,
                status_code=status_code,
                error_code=err_status,
                error_type="quota_exhausted_error",
                message=message,
                retryable=True,
                quota_scope=QuotaScopeType.ACCOUNT,
                requires_admin_attention=True,
            )
        if status_code == 429 or err_status in (
            "too_many_concurrent_requests",
            "system_busy",
        ):
            scope = (
                QuotaScopeType.CONCURRENCY
                if err_status == "too_many_concurrent_requests"
                else QuotaScopeType.ACCOUNT
            )
            return ClassifiedProviderError(
                provider_id=self.provider_id,
                status_code=status_code,
                error_code=err_status,
                error_type="rate_limit_error",
                message=message,
                retryable=True,
                quota_scope=scope,
                retry_after_seconds=retry_after or 5.0,
            )
        if status_code >= 500:
            return ClassifiedProviderError(
                provider_id=self.provider_id,
                status_code=status_code,
                error_code=err_status,
                error_type="upstream_server_error",
                message=message,
                retryable=True,
                quota_scope=QuotaScopeType.ENDPOINT,
                retry_after_seconds=retry_after or 2.0,
            )
        return ClassifiedProviderError(
            provider_id=self.provider_id,
            status_code=status_code,
            error_code=err_status,
            error_type="invalid_request_error",
            message=message,
            retryable=False,
        )

