"""Base provider adapter contract and shared HTTP/SSE utilities."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping
import json
import re
import time
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field

from gateway.capability_registry.models import (
    CapabilityFlag,
    ProtocolType,
    ProviderCapabilityManifest,
    QuotaScopeType,
)
from gateway.core.canonical import (
    AdapterCredential,
    CanonicalInternalRequest,
    CanonicalInternalResponse,
    CanonicalStreamEvent,
    ClassifiedProviderError,
    DiscoveredModel,
    HealthCheckResult,
    HealthStatus,
    NativePassthroughRequest,
    NativePassthroughResponse,
    RateLimitSnapshot,
    TokenUsage,
)
from gateway.core.exceptions import UpstreamProviderError
from gateway.core.http import HTTPXClientPoolManager, TimeoutPolicy

DURATION_TOKEN_RE = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h)")


def parse_duration_seconds(raw_value: str | None) -> float | None:
    """Parses provider reset headers like '12', '1.5s', '2m30s', '450ms' into seconds."""
    if not raw_value:
        return None
    val = raw_value.strip()
    try:
        return float(val)
    except ValueError:
        pass
    matches = DURATION_TOKEN_RE.findall(val)
    if not matches:
        return None
    total = 0.0
    for amount_str, unit in matches:
        amount = float(amount_str)
        if unit == "ms":
            total += amount / 1000.0
        elif unit == "s":
            total += amount
        elif unit == "m":
            total += amount * 60.0
        elif unit == "h":
            total += amount * 3600.0
    return round(total, 4)


def parse_int_header(headers: Mapping[str, str], key: str) -> int | None:
    raw = headers.get(key) or headers.get(key.lower())
    if raw is None:
        return None
    try:
        return int(raw.strip())
    except ValueError:
        return None


async def iter_sse_events(
    response: httpx.Response,
) -> AsyncIterator[tuple[str | None, str]]:
    """Yields (event_name, data_str) tuples from an SSE HTTPX streaming response."""
    event_name: str | None = None
    data_lines: list[str] = []
    async for raw_line in response.aiter_lines():
        line = raw_line.strip("\r")
        if not line:
            if data_lines:
                yield event_name, "\n".join(data_lines)
                event_name = None
                data_lines = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event_name = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:") :].lstrip())
    if data_lines:
        yield event_name, "\n".join(data_lines)


class AuthConfiguration(BaseModel):
    """Authentication configuration for a provider."""

    auth_type: str = "header"  # "header", "query", "none"
    header_name: str = "Authorization"
    header_prefix: str = "Bearer "
    query_param_name: str | None = None
    static_headers: dict[str, str] = Field(default_factory=dict)


class ProviderConfig(BaseModel):
    """Configuration model for a registered provider instance."""

    provider_id: str
    display_name: str
    adapter_type: str
    base_url: str
    auth: AuthConfiguration = Field(default_factory=AuthConfiguration)
    enabled: bool = True
    allow_local_network: bool = False
    custom_settings: dict[str, Any] = Field(default_factory=dict)


class ConfigValidationResult(BaseModel):
    """Result of validating a ProviderConfig."""

    valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class BaseProviderAdapter(ABC):
    """Abstract contract for all AI Provider Adapters in the Universal AI Gateway."""

    provider_id: str
    display_name: str
    adapter_type: str
    default_base_url: str
    supported_protocols: frozenset[ProtocolType]

    def __init__(
        self,
        http_pool: HTTPXClientPoolManager,
        config: ProviderConfig | None = None,
    ) -> None:
        self._http_pool = http_pool
        self._config = config or self.get_default_config()
        self._started = False

    @property
    def config(self) -> ProviderConfig:
        return self._config

    @property
    def base_url(self) -> str:
        return self._config.base_url.rstrip("/")

    @abstractmethod
    def get_default_config(self) -> ProviderConfig:
        """Returns the default ProviderConfig for this adapter."""

    @abstractmethod
    def get_capability_manifest(self) -> ProviderCapabilityManifest:
        """Returns verified provider capabilities and supported quota scopes."""

    async def startup(self) -> None:
        """Initializes adapter resources and HTTP pool."""
        await self._http_pool.get_client(self.provider_id, base_url=self.base_url)
        self._started = True

    async def shutdown(self) -> None:
        """Marks adapter as stopped."""
        self._started = False

    async def get_http_client(
        self, timeout_policy: TimeoutPolicy | None = None
    ) -> httpx.AsyncClient:
        """Returns the pooled HTTPX AsyncClient for this provider."""
        return await self._http_pool.get_client(
            self.provider_id,
            base_url=self.base_url,
            timeout_policy=timeout_policy,
        )

    def build_auth_headers(
        self, credential: AdapterCredential | None
    ) -> dict[str, str]:
        """Constructs authentication and provider static headers for an upstream call."""
        headers: dict[str, str] = dict(self._config.auth.static_headers)
        if credential is not None:
            headers.update(credential.extra_headers)
            if self._config.auth.auth_type == "header":
                secret = credential.get_secret_value()
                if secret:
                    headers[self._config.auth.header_name] = (
                        f"{self._config.auth.header_prefix}{secret}"
                    )
        return headers

    def validate_configuration(
        self, config: ProviderConfig | None = None
    ) -> ConfigValidationResult:
        """Validates provider endpoint URLs, schemes, and authentication configuration."""
        target = config or self._config
        errors: list[str] = []
        warnings: list[str] = []

        if not target.provider_id.strip():
            errors.append("provider_id must not be empty")

        parsed = urlparse(target.base_url)
        if parsed.scheme not in ("http", "https"):
            errors.append(f"base_url must use http or https scheme, got {parsed.scheme!r}")
        if not parsed.netloc:
            errors.append("base_url must include a valid host")

        if parsed.scheme == "http" and not target.allow_local_network:
            host_only = (parsed.hostname or "").lower()
            if host_only not in ("localhost", "127.0.0.1"):
                warnings.append("Using unencrypted http:// scheme for remote upstream")

        if target.auth.auth_type not in ("header", "query", "none"):
            errors.append(f"Unsupported auth_type: {target.auth.auth_type!r}")
        if target.auth.auth_type == "header" and not target.auth.header_name.strip():
            errors.append("header_name is required when auth_type='header'")
        if target.auth.auth_type == "query" and not (
            target.auth.query_param_name and target.auth.query_param_name.strip()
        ):
            errors.append("query_param_name is required when auth_type='query'")

        return ConfigValidationResult(
            valid=len(errors) == 0,
            errors=errors,
            warnings=warnings,
        )

    @abstractmethod
    async def discover_models(
        self, credential: AdapterCredential | None = None
    ) -> list[DiscoveredModel]:
        """Discovers available models from the upstream provider."""

    @abstractmethod
    async def execute(
        self,
        request: CanonicalInternalRequest,
        credential: AdapterCredential,
        timeout: TimeoutPolicy | None = None,
    ) -> CanonicalInternalResponse:
        """Executes a non-streaming canonical request against the provider."""

    @abstractmethod
    def execute_stream(
        self,
        request: CanonicalInternalRequest,
        credential: AdapterCredential,
        timeout: TimeoutPolicy | None = None,
    ) -> AsyncIterator[CanonicalStreamEvent]:
        """Executes a streaming canonical request and yields normalized stream events."""

    async def execute_native(
        self,
        native_request: NativePassthroughRequest,
        credential: AdapterCredential | None = None,
        timeout: TimeoutPolicy | None = None,
    ) -> NativePassthroughResponse:
        """Executes a zero-normalization native request while extracting usage and rate limits."""
        client = await self.get_http_client(timeout_policy=timeout)
        headers = {
            k: v
            for k, v in native_request.headers.items()
            if k.lower()
            not in (
                "host",
                "content-length",
                "authorization",
                "x-api-key",
                "xi-api-key",
                "x-goog-api-key",
            )
            and not k.lower().startswith("x-gateway-")
        }
        headers.update(self.build_auth_headers(credential))

        path = native_request.upstream_path
        if not path.startswith("/"):
            path = f"/{path}"

        resp = await client.request(
            method=native_request.method,
            url=path,
            params=native_request.query_params,
            headers=headers,
            content=native_request.body_bytes,
        )
        resp_headers = {k.lower(): v for k, v in resp.headers.items()}
        parsed_body: dict[str, Any] | None = None
        if "application/json" in resp_headers.get("content-type", ""):
            try:
                parsed_body = resp.json()
            except ValueError:
                parsed_body = None

        usage, rate_limit = self.extract_usage_and_ratelimits(resp_headers, parsed_body)
        return NativePassthroughResponse(
            status_code=resp.status_code,
            headers=resp_headers,
            body_bytes=resp.content,
            media_type=resp_headers.get("content-type", "application/json"),
            usage=usage,
            rate_limit=rate_limit,
        )

    @abstractmethod
    def classify_error(
        self,
        status_code: int,
        headers: Mapping[str, str],
        body: bytes | str | Mapping[str, Any] | None,
    ) -> ClassifiedProviderError:
        """Classifies upstream HTTP errors into Gateway error taxonomy and quota scopes."""

    @abstractmethod
    def extract_usage_and_ratelimits(
        self,
        headers: Mapping[str, str],
        body: Mapping[str, Any] | None,
    ) -> tuple[TokenUsage | None, RateLimitSnapshot | None]:
        """Extracts token/character usage and rate-limit snapshot from upstream response."""

    async def check_health(
        self, credential: AdapterCredential | None = None
    ) -> HealthCheckResult:
        """Default health check probing model discovery or provider readiness endpoint."""
        start_ns = time.perf_counter_ns()
        try:
            models = await self.discover_models(credential)
            elapsed_ms = (time.perf_counter_ns() - start_ns) / 1_000_000.0
            return HealthCheckResult(
                provider_id=self.provider_id,
                status=HealthStatus.HEALTHY,
                latency_ms=round(elapsed_ms, 2),
                message=f"Healthy ({len(models)} models discovered)",
                details={"discovered_model_count": len(models)},
            )
        except UpstreamProviderError as exc:
            elapsed_ms = (time.perf_counter_ns() - start_ns) / 1_000_000.0
            status = (
                HealthStatus.DEGRADED
                if exc.status_code == 429
                else HealthStatus.UNHEALTHY
            )
            return HealthCheckResult(
                provider_id=self.provider_id,
                status=status,
                latency_ms=round(elapsed_ms, 2),
                message=exc.message,
                details={"error_code": exc.error_code, "status_code": exc.status_code},
            )
        except Exception as exc:
            elapsed_ms = (time.perf_counter_ns() - start_ns) / 1_000_000.0
            return HealthCheckResult(
                provider_id=self.provider_id,
                status=HealthStatus.UNHEALTHY,
                latency_ms=round(elapsed_ms, 2),
                message=str(exc),
                details={"exception_type": type(exc).__name__},
            )

    def _raise_for_upstream_error(
        self,
        status_code: int,
        headers: Mapping[str, str],
        body: bytes | str | Mapping[str, Any] | None,
    ) -> None:
        classified = self.classify_error(status_code, headers, body)
        raise UpstreamProviderError(
            classified.message,
            status_code=status_code,
            error_type=classified.error_type,
            error_code=classified.error_code,
            retryable=classified.retryable,
            details={
                "provider_id": self.provider_id,
                "quota_scope": classified.quota_scope.value,
                "retry_after_seconds": classified.retry_after_seconds,
                "requires_credential_disable": classified.requires_credential_disable,
                "requires_admin_attention": classified.requires_admin_attention,
            },
        )

