"""Groq Provider Adapter (High-speed OpenAI-compatible Chat Completions & Model Discovery)."""

from __future__ import annotations

from gateway.capability_registry.models import (
    CapabilityFlag,
    ProtocolType,
    ProviderCapabilityManifest,
    QuotaScopeType,
    build_capability_map,
)
from gateway.core.canonical import AdapterCredential, DiscoveredModel
from gateway.provider_adapters.base import AuthConfiguration, ProviderConfig
from gateway.provider_adapters.openai_adapter import OpenAIProviderAdapter


class GroqProviderAdapter(OpenAIProviderAdapter):
    """Official Groq Cloud API provider adapter."""

    provider_id = "groq"
    display_name = "Groq"
    adapter_type = "builtin_groq"
    default_base_url = "https://api.groq.com/openai/v1"
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
            ),
        )

    def get_capability_manifest(self) -> ProviderCapabilityManifest:
        supported = {
            CapabilityFlag.CHAT_COMPLETIONS,
            CapabilityFlag.TOOL_CALLING,
            CapabilityFlag.PARALLEL_TOOL_CALLS,
            CapabilityFlag.JSON_MODE,
            CapabilityFlag.STREAMING,
            CapabilityFlag.SYSTEM_INSTRUCTIONS,
            CapabilityFlag.VISION,
            CapabilityFlag.STT,
            CapabilityFlag.MODEL_DISCOVERY,
        }
        unsupported = {
            CapabilityFlag.RESPONSES_API,
            CapabilityFlag.MESSAGES_API,
            CapabilityFlag.GEMINI_GENERATE_CONTENT,
            CapabilityFlag.EMBEDDINGS,
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
            auth_header_name="Authorization",
            auth_header_prefix="Bearer ",
            notes="Supports Groq LPU OpenAI-compatible Chat Completions and model discovery with context_window.",
        )

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
            if not model_id or not item.get("active", True):
                continue

            is_whisper = "whisper" in model_id.lower()
            if is_whisper:
                caps = build_capability_map(
                    supported={CapabilityFlag.STT},
                    unsupported={
                        CapabilityFlag.CHAT_COMPLETIONS,
                        CapabilityFlag.EMBEDDINGS,
                        CapabilityFlag.TOOL_CALLING,
                    },
                )
            else:
                supported_caps = {
                    CapabilityFlag.CHAT_COMPLETIONS,
                    CapabilityFlag.STREAMING,
                    CapabilityFlag.SYSTEM_INSTRUCTIONS,
                    CapabilityFlag.JSON_MODE,
                    CapabilityFlag.TOOL_CALLING,
                }
                if "vision" in model_id.lower() or "llama-4" in model_id.lower():
                    supported_caps.add(CapabilityFlag.VISION)
                if "deepseek-r1" in model_id.lower() or "qwen-qwq" in model_id.lower():
                    supported_caps.add(CapabilityFlag.REASONING_TOKENS)
                caps = build_capability_map(
                    supported=supported_caps,
                    unsupported={
                        CapabilityFlag.EMBEDDINGS,
                        CapabilityFlag.RESPONSES_API,
                        CapabilityFlag.MESSAGES_API,
                    },
                )

            models.append(
                DiscoveredModel(
                    provider_id=self.provider_id,
                    provider_model_id=model_id,
                    canonical_model_id=f"{self.provider_id}/{model_id}",
                    display_name=model_id,
                    supported_protocols=[ProtocolType.OPENAI_CHAT],
                    context_length=item.get("context_window"),
                    max_output_tokens=item.get("max_completion_tokens"),
                    capabilities=caps,
                    raw_metadata=item,
                )
            )
        return models

