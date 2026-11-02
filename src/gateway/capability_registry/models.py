"""Capability, protocol, and quota scope enumerations and manifests."""

from __future__ import annotations

from enum import StrEnum
from pydantic import BaseModel, Field


class CapabilityState(StrEnum):
    """Explicit tri-state capability flag — never assume SUPPORTED when unknown."""

    SUPPORTED = "SUPPORTED"
    UNSUPPORTED = "UNSUPPORTED"
    UNKNOWN = "UNKNOWN"


class CapabilityFlag(StrEnum):
    """Granular capability identifiers across text, multimodal, audio, and protocol features."""

    CHAT_COMPLETIONS = "chat_completions"
    RESPONSES_API = "responses_api"
    MESSAGES_API = "messages_api"
    GEMINI_GENERATE_CONTENT = "gemini_generate_content"
    EMBEDDINGS = "embeddings"
    TOOL_CALLING = "tool_calling"
    PARALLEL_TOOL_CALLS = "parallel_tool_calls"
    STRUCTURED_OUTPUTS = "structured_outputs"
    JSON_MODE = "json_mode"
    STREAMING = "streaming"
    SYSTEM_INSTRUCTIONS = "system_instructions"
    REASONING_TOKENS = "reasoning_tokens"
    VISION = "vision"
    AUDIO_INPUT = "audio_input"
    TTS = "tts"
    STT = "stt"
    VOICE_DISCOVERY = "voice_discovery"
    MODEL_DISCOVERY = "model_discovery"


class ProtocolType(StrEnum):
    """Supported inbound/outbound wire protocol identifiers."""

    OPENAI_CHAT = "openai_chat"
    OPENAI_RESPONSES = "openai_responses"
    OPENAI_EMBEDDINGS = "openai_embeddings"
    ANTHROPIC_MESSAGES = "anthropic_messages"
    GEMINI_GENERATE_CONTENT = "gemini_generate_content"
    GEMINI_EMBED_CONTENT = "gemini_embed_content"
    ELEVENLABS_TTS = "elevenlabs_tts"
    ELEVENLABS_STT = "elevenlabs_stt"
    ELEVENLABS_VOICES = "elevenlabs_voices"
    OLLAMA_NATIVE = "ollama_native"
    CUSTOM_HTTP = "custom_http"


class QuotaScopeType(StrEnum):
    """Quota tracking scopes supported by upstream providers."""

    CREDENTIAL = "CREDENTIAL"
    WORKSPACE = "WORKSPACE"
    ACCOUNT = "ACCOUNT"
    MODEL = "MODEL"
    ENDPOINT = "ENDPOINT"
    CONCURRENCY = "CONCURRENCY"


class ProviderCapabilityManifest(BaseModel):
    """Verified provider-level capability manifest."""

    provider_id: str
    display_name: str
    adapter_type: str
    default_base_url: str
    supported_protocols: list[ProtocolType]
    supported_quota_scopes: list[QuotaScopeType]
    capabilities: dict[CapabilityFlag, CapabilityState] = Field(default_factory=dict)
    auth_header_name: str = "Authorization"
    auth_header_prefix: str = "Bearer "
    supports_custom_base_url: bool = True
    notes: str | None = None

    def get_capability(self, flag: CapabilityFlag) -> CapabilityState:
        """Returns the explicit capability state, defaulting to UNKNOWN if not declared."""
        return self.capabilities.get(flag, CapabilityState.UNKNOWN)

    def supports(self, flag: CapabilityFlag) -> bool:
        """Returns True ONLY if the capability is explicitly verified as SUPPORTED."""
        return self.get_capability(flag) == CapabilityState.SUPPORTED


def build_capability_map(
    supported: set[CapabilityFlag],
    unsupported: set[CapabilityFlag] | None = None,
) -> dict[CapabilityFlag, CapabilityState]:
    """Constructs a full CapabilityFlag -> CapabilityState map with explicit states."""
    unsupported_set = unsupported or set()
    result: dict[CapabilityFlag, CapabilityState] = {}
    for flag in CapabilityFlag:
        if flag in supported:
            result[flag] = CapabilityState.SUPPORTED
        elif flag in unsupported_set:
            result[flag] = CapabilityState.UNSUPPORTED
        else:
            result[flag] = CapabilityState.UNKNOWN
    return result

