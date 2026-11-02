"""Provider Adapters module — upstream AI provider execution and capability contracts."""

from gateway.provider_adapters.anthropic_adapter import AnthropicProviderAdapter
from gateway.provider_adapters.base import (
    AuthConfiguration,
    BaseProviderAdapter,
    ConfigValidationResult,
    ProviderConfig,
    iter_sse_events,
    parse_duration_seconds,
)
from gateway.provider_adapters.custom_adapter import CustomHTTPProviderAdapter
from gateway.provider_adapters.elevenlabs_adapter import ElevenLabsProviderAdapter
from gateway.provider_adapters.gemini_adapter import GeminiProviderAdapter
from gateway.provider_adapters.groq_adapter import GroqProviderAdapter
from gateway.provider_adapters.ollama_adapter import OllamaProviderAdapter
from gateway.provider_adapters.openai_adapter import OpenAIProviderAdapter
from gateway.provider_adapters.openrouter_adapter import OpenRouterProviderAdapter

BUILTIN_ADAPTER_CLASSES: tuple[type[BaseProviderAdapter], ...] = (
    OpenAIProviderAdapter,
    AnthropicProviderAdapter,
    GeminiProviderAdapter,
    OpenRouterProviderAdapter,
    GroqProviderAdapter,
    OllamaProviderAdapter,
    ElevenLabsProviderAdapter,
)

__all__ = [
    "AnthropicProviderAdapter",
    "AuthConfiguration",
    "BUILTIN_ADAPTER_CLASSES",
    "BaseProviderAdapter",
    "ConfigValidationResult",
    "CustomHTTPProviderAdapter",
    "ElevenLabsProviderAdapter",
    "GeminiProviderAdapter",
    "GroqProviderAdapter",
    "OllamaProviderAdapter",
    "OpenAIProviderAdapter",
    "OpenRouterProviderAdapter",
    "ProviderConfig",
    "iter_sse_events",
    "parse_duration_seconds",
]
