"""Base protocol adapter contract, capability verification, and provider resolution."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping
from typing import Any

from fastapi import Request
from pydantic import SecretStr

from gateway.capability_registry.models import (
    CapabilityFlag,
    ProtocolType,
    ProviderCapabilityManifest,
)
from gateway.core.canonical import (
    AdapterCredential,
    CanonicalInternalRequest,
    CanonicalInternalResponse,
    CanonicalStreamEvent,
)
from gateway.core.exceptions import UnsupportedCapabilityError


def resolve_target_provider_and_model(
    model_identifier: str,
    default_provider: str = "openai",
) -> tuple[str, str]:
    """Resolves 'provider/model' or infers provider from known model prefixes."""
    cleaned = model_identifier.strip()
    if "/" in cleaned:
        prefix, rest = cleaned.split("/", 1)
        if prefix in (
            "openai",
            "anthropic",
            "gemini",
            "openrouter",
            "groq",
            "ollama",
            "elevenlabs",
        ):
            return prefix, rest

    lower = cleaned.lower()
    if lower.startswith("claude-"):
        return "anthropic", cleaned
    if lower.startswith(("gemini-", "models/gemini-", "text-embedding-004")):
        return "gemini", cleaned.removeprefix("models/")
    if lower.startswith(("eleven_", "scribe_")):
        return "elevenlabs", cleaned
    if lower.startswith(("gpt-", "o1", "o3", "o4", "text-embedding-3")):
        return "openai", cleaned
    return default_provider, cleaned


def extract_request_credential(
    request: Request,
    provider_id: str,
) -> AdapterCredential:
    """Extracts an upstream credential from request headers for Phase 2 execution."""
    headers = request.headers
    secret = (
        headers.get("X-Upstream-Api-Key")
        or headers.get("x-api-key")
        or headers.get("x-goog-api-key")
        or headers.get("xi-api-key")
        or ""
    )
    if not secret:
        auth_val = headers.get("Authorization") or ""
        if auth_val.lower().startswith("bearer "):
            secret = auth_val[7:].strip()
        elif auth_val:
            secret = auth_val.strip()

    if not secret:
        secret = request.query_params.get("key") or ""

    return AdapterCredential(
        provider_id=provider_id,
        secret=SecretStr(secret),
        organization_id=headers.get("OpenAI-Organization"),
        project_id=headers.get("OpenAI-Project"),
    )


def enforce_capability_compatibility(
    request: CanonicalInternalRequest,
    manifest: ProviderCapabilityManifest,
) -> None:
    """Ensures every required capability of the request is explicitly SUPPORTED by the target provider.

    Never silently drops unsupported capabilities or provider-specific functionality.
    """
    for cap in request.required_capabilities:
        if not manifest.supports(cap):
            state = manifest.get_capability(cap)
            raise UnsupportedCapabilityError(
                f"Provider {manifest.provider_id!r} does not support required capability "
                f"{cap.value!r} (state={state.value})",
                param=cap.value,
                details={
                    "provider_id": manifest.provider_id,
                    "capability": cap.value,
                    "capability_state": state.value,
                },
            )

    for tool in request.tools:
        if tool.is_provider_native:
            owner_provider = tool.provider_metadata.get("provider_id")
            if owner_provider and owner_provider != manifest.provider_id:
                raise UnsupportedCapabilityError(
                    f"Provider-native tool {tool.name!r} ({owner_provider}) is not portable to provider {manifest.provider_id!r}",
                    param="tools",
                    details={
                        "tool_name": tool.name,
                        "source_provider": owner_provider,
                        "target_provider": manifest.provider_id,
                    },
                )


class BaseProtocolAdapter(ABC):
    """Abstract contract for wire-protocol adapters."""

    protocol_type: ProtocolType

    @abstractmethod
    def parse_request(
        self, payload: Mapping[str, Any], **kwargs: Any
    ) -> CanonicalInternalRequest:
        """Validates and converts a wire-protocol request into a CanonicalInternalRequest."""

    @abstractmethod
    def format_response(
        self, response: CanonicalInternalResponse
    ) -> dict[str, Any]:
        """Formats a CanonicalInternalResponse into the wire protocol's response schema."""

    @abstractmethod
    def format_stream_events(
        self, events: AsyncIterator[CanonicalStreamEvent], model: str
    ) -> AsyncIterator[bytes]:
        """Formats a stream of CanonicalStreamEvents into wire-protocol SSE/bytes frames."""

