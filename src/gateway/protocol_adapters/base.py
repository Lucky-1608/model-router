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
from gateway.core.auth import is_client_api_key
from gateway.core.canonical import (
    AdapterCredential,
    CanonicalInternalRequest,
    CanonicalInternalResponse,
    CanonicalStreamEvent,
)
from gateway.core.exceptions import UnsupportedCapabilityError

KNOWN_PROVIDER_IDS = frozenset(
    {
        "openai",
        "anthropic",
        "gemini",
        "openrouter",
        "groq",
        "ollama",
        "elevenlabs",
    }
)


def resolve_target_provider_and_model(
    model_identifier: str,
    default_provider: str = "openai",
    registered_providers: set[str] | None = None,
) -> tuple[str, str]:
    """Resolves 'provider/model' or infers provider from known model prefixes or dynamic registry."""
    cleaned = model_identifier.strip()
    known = (
        KNOWN_PROVIDER_IDS | set(registered_providers)
        if registered_providers
        else KNOWN_PROVIDER_IDS
    )
    if "/" in cleaned:
        prefix, rest = cleaned.split("/", 1)
        if prefix in known:
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
) -> AdapterCredential | None:
    """Extracts an upstream credential from request headers.

    If the bearer/auth token is a Gateway client key (`ugw_live_...`), it is NOT treated as an
    upstream secret. In that case, returns None (or falls back to X-Upstream-Api-Key) so that
    `CredentialManager` can select, rotate, and decrypt a managed upstream credential.
    """
    headers = request.headers
    # Explicit upstream override header always takes precedence
    secret = headers.get("X-Upstream-Api-Key") or ""

    if not secret:
        # Check standard headers, ignoring any client API key tokens (ugw_live_...)
        for hdr in ("x-api-key", "x-goog-api-key", "xi-api-key"):
            val = headers.get(hdr)
            if val and not is_client_api_key(val):
                secret = val.strip()
                break

    if not secret:
        auth_val = headers.get("Authorization") or ""
        token = ""
        if auth_val.lower().startswith("bearer "):
            token = auth_val[7:].strip()
        elif auth_val:
            token = auth_val.strip()
        if token and not is_client_api_key(token):
            secret = token

    if not secret:
        query_key = request.query_params.get("key") or ""
        if query_key and not is_client_api_key(query_key):
            secret = query_key.strip()

    if not secret:
        return None

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

