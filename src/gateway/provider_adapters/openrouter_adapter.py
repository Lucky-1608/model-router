"""OpenRouter Provider Adapter (OpenAI-compatible Chat, Rich Model & Pricing Discovery)."""

from __future__ import annotations

from collections.abc import Mapping
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
    ClassifiedProviderError,
    DiscoveredModel,
)
from gateway.provider_adapters.base import AuthConfiguration, ProviderConfig
from gateway.provider_adapters.openai_adapter import OpenAIProviderAdapter


class OpenRouterProviderAdapter(OpenAIProviderAdapter):
    """Official OpenRouter API provider adapter."""

    provider_id = "openrouter"
    display_name = "OpenRouter"
    adapter_type = "builtin_openrouter"
    default_base_url = "https://openrouter.ai/api/v1"
    supported_protocols = frozenset({ProtocolType.OPENAI_CHAT})

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
                static_headers={"X-Title": "Universal AI Gateway"},
            ),
        )

    def get_capability_manifest(self) -> ProviderCapabilityManifest:
        supported = {
            CapabilityFlag.CHAT_COMPLETIONS,
            CapabilityFlag.TOOL_CALLING,
            CapabilityFlag.PARALLEL_TOOL_CALLS,
            CapabilityFlag.STRUCTURED_OUTPUTS,
            CapabilityFlag.JSON_MODE,
            CapabilityFlag.STREAMING,
            CapabilityFlag.SYSTEM_INSTRUCTIONS,
            CapabilityFlag.REASONING_TOKENS,
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
                QuotaScopeType.ACCOUNT,
                QuotaScopeType.CREDENTIAL,
                QuotaScopeType.MODEL,
            ],
            capabilities=build_capability_map(supported, unsupported),
            auth_header_name="Authorization",
            auth_header_prefix="Bearer ",
            notes="Supports OpenRouter rich model catalog with per-model pricing and supported_parameters.",
        )

    def _build_chat_payload(
        self, request: CanonicalInternalRequest, *, stream: bool
    ) -> dict[str, Any]:
        payload = super()._build_chat_payload(request, stream=stream)
        # Preserve OpenRouter provider routing preferences if passed in provider_extensions
        if "provider" in request.provider_extensions:
            payload["provider"] = request.provider_extensions["provider"]
        if "transforms" in request.provider_extensions:
            payload["transforms"] = request.provider_extensions["transforms"]
        return payload

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

            supported_params = set(item.get("supported_parameters") or [])
            arch = item.get("architecture") or {}
            input_modalities = set(arch.get("input_modalities") or [])

            supported_caps = {
                CapabilityFlag.CHAT_COMPLETIONS,
                CapabilityFlag.STREAMING,
                CapabilityFlag.SYSTEM_INSTRUCTIONS,
            }
            unsupported_caps = {
                CapabilityFlag.RESPONSES_API,
                CapabilityFlag.MESSAGES_API,
                CapabilityFlag.GEMINI_GENERATE_CONTENT,
                CapabilityFlag.TTS,
                CapabilityFlag.STT,
            }

            if "tools" in supported_params:
                supported_caps.add(CapabilityFlag.TOOL_CALLING)
            if "structured_outputs" in supported_params:
                supported_caps.add(CapabilityFlag.STRUCTURED_OUTPUTS)
            if "response_format" in supported_params:
                supported_caps.add(CapabilityFlag.JSON_MODE)
            if "reasoning" in supported_params or "include_reasoning" in supported_params:
                supported_caps.add(CapabilityFlag.REASONING_TOKENS)
            if "image" in input_modalities:
                supported_caps.add(CapabilityFlag.VISION)

            pricing = item.get("pricing") or {}
            in_cost: float | None = None
            out_cost: float | None = None
            try:
                if pricing.get("prompt") is not None:
                    in_cost = round(float(pricing["prompt"]) * 1_000_000, 4)
                if pricing.get("completion") is not None:
                    out_cost = round(float(pricing["completion"]) * 1_000_000, 4)
            except (ValueError, TypeError):
                pass

            top_prov = item.get("top_provider") or {}
            models.append(
                DiscoveredModel(
                    provider_id=self.provider_id,
                    provider_model_id=model_id,
                    canonical_model_id=f"{self.provider_id}/{model_id}",
                    display_name=str(item.get("name") or model_id),
                    supported_protocols=[ProtocolType.OPENAI_CHAT],
                    context_length=item.get("context_length") or top_prov.get("context_length"),
                    max_output_tokens=top_prov.get("max_completion_tokens"),
                    capabilities=build_capability_map(supported_caps, unsupported_caps),
                    input_cost_per_1m_usd=in_cost,
                    output_cost_per_1m_usd=out_cost,
                    raw_metadata=item,
                )
            )
        return models

    def classify_error(
        self,
        status_code: int,
        headers: Mapping[str, str],
        body: bytes | str | Mapping[str, Any] | None,
    ) -> ClassifiedProviderError:
        classified = super().classify_error(status_code, headers, body)
        if status_code == 402:
            return ClassifiedProviderError(
                provider_id=self.provider_id,
                status_code=402,
                error_code="insufficient_credits",
                error_type="quota_exhausted_error",
                message=classified.message,
                retryable=True,
                quota_scope=QuotaScopeType.ACCOUNT,
                requires_admin_attention=True,
            )
        return classified

