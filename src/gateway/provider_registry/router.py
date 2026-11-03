"""Provider Discovery & Registry API routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, Request, status
from pydantic import SecretStr

from gateway.capability_registry.models import ProviderCapabilityManifest
from gateway.core.auth import require_admin_access
from gateway.core.canonical import AdapterCredential, DiscoveredModel, HealthCheckResult
from gateway.provider_adapters.base import ConfigValidationResult, ProviderConfig
from gateway.provider_registry.registry import ProviderRegistry, ProviderSummary

provider_router = APIRouter(prefix="/providers", tags=["Provider Registry & Discovery"])


def _get_registry(request: Request) -> ProviderRegistry:
    return request.app.state.provider_registry


@provider_router.get("", response_model=list[ProviderSummary], summary="List Registered Providers")
async def list_providers(
    request: Request,
    enabled_only: bool = False,
) -> list[ProviderSummary]:
    registry = _get_registry(request)
    return registry.list_provider_summaries(enabled_only=enabled_only)


@provider_router.get(
    "/{provider_id}",
    response_model=ProviderSummary,
    summary="Get Provider Summary & Status",
)
async def get_provider(request: Request, provider_id: str) -> ProviderSummary:
    registry = _get_registry(request)
    return registry.get_provider_summary(provider_id)


@provider_router.get(
    "/{provider_id}/capabilities",
    response_model=ProviderCapabilityManifest,
    summary="Get Verified Provider Capability Manifest",
)
async def get_provider_capabilities(
    request: Request, provider_id: str
) -> ProviderCapabilityManifest:
    registry = _get_registry(request)
    return registry.get_capability_manifest(provider_id)


@provider_router.post(
    "/{provider_id}/validate",
    response_model=ConfigValidationResult,
    summary="Validate Provider Configuration",
)
async def validate_provider_configuration(
    request: Request,
    provider_id: str,
    config: ProviderConfig | None = None,
) -> ConfigValidationResult:
    registry = _get_registry(request)
    return registry.validate_provider_config(provider_id, config=config)


@provider_router.get(
    "/{provider_id}/health",
    response_model=HealthCheckResult,
    summary="Get or Probe Provider Health Status",
)
async def get_or_probe_provider_health(
    request: Request,
    provider_id: str,
    probe: bool = False,
    x_upstream_api_key: str | None = Header(default=None, alias="X-Upstream-Api-Key"),
) -> HealthCheckResult:
    registry = _get_registry(request)
    if not probe:
        return registry.get_cached_health(provider_id)
    cred = (
        AdapterCredential(provider_id=provider_id, secret=SecretStr(x_upstream_api_key))
        if x_upstream_api_key
        else None
    )
    return await registry.check_provider_health(provider_id, credential=cred)


@provider_router.post(
    "/{provider_id}/discover-models",
    response_model=list[DiscoveredModel],
    summary="Discover Available Models From Provider",
)
async def discover_provider_models(
    request: Request,
    provider_id: str,
    x_upstream_api_key: str | None = Header(default=None, alias="X-Upstream-Api-Key"),
) -> list[DiscoveredModel]:
    registry = _get_registry(request)
    cred = (
        AdapterCredential(provider_id=provider_id, secret=SecretStr(x_upstream_api_key))
        if x_upstream_api_key
        else None
    )
    return await registry.discover_provider_models(provider_id, credential=cred)


@provider_router.post(
    "/custom",
    response_model=ProviderSummary,
    status_code=status.HTTP_201_CREATED,
    summary="Register Custom Provider Adapter",
)
async def register_custom_provider(
    request: Request,
    config: ProviderConfig,
    _auth: tuple[str, str] = Depends(require_admin_access),
) -> ProviderSummary:
    registry = _get_registry(request)
    return await registry.register_custom_provider(config)


@provider_router.delete(
    "/custom/{provider_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Unregister Custom Provider Adapter",
)
async def unregister_custom_provider(
    request: Request,
    provider_id: str,
    _auth: tuple[str, str] = Depends(require_admin_access),
) -> None:
    registry = _get_registry(request)
    await registry.unregister_provider(provider_id)

