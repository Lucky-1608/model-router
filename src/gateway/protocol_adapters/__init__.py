"""Protocol Adapters module — OpenAI, Responses, Anthropic, Gemini, ElevenLabs, and Custom protocols."""

from gateway.protocol_adapters.anthropic_protocol import (
    AnthropicMessagesProtocolAdapter,
)
from gateway.protocol_adapters.base import (
    BaseProtocolAdapter,
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
from gateway.protocol_adapters.router import protocol_router

__all__ = [
    "AnthropicMessagesProtocolAdapter",
    "BaseProtocolAdapter",
    "CustomProtocolAdapter",
    "CustomProtocolSpec",
    "ElevenLabsProtocolAdapter",
    "GeminiNativeProtocolAdapter",
    "OpenAIChatProtocolAdapter",
    "OpenAIResponsesProtocolAdapter",
    "enforce_capability_compatibility",
    "extract_request_credential",
    "protocol_router",
    "resolve_target_provider_and_model",
]
