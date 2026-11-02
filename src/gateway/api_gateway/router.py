"""Main API Gateway router aggregator."""

from __future__ import annotations

from fastapi import APIRouter

from gateway.admin_api.router import admin_control_router
from gateway.api_gateway.health import health_router
from gateway.api_gateway.versioning import version_router
from gateway.credential_manager.router import credential_admin_router
from gateway.mcp_integration.router import mcp_router
from gateway.observability.router import analytics_router, metrics_router
from gateway.protocol_adapters.router import protocol_router
from gateway.provider_registry.router import provider_router
from gateway.routing_engine.router import routing_router
from gateway.tool_gateway.router import tool_router


def build_api_gateway_router(api_v1_prefix: str = "/api/v1") -> APIRouter:
    """Constructs the root and versioned routers for the API Gateway."""
    root_router = APIRouter()
    root_router.include_router(health_router)
    root_router.include_router(metrics_router)
    root_router.include_router(version_router, prefix=api_v1_prefix)
    root_router.include_router(version_router, prefix="/v1")
    root_router.include_router(provider_router, prefix=api_v1_prefix)
    root_router.include_router(provider_router, prefix="/v1")
    root_router.include_router(routing_router, prefix=api_v1_prefix)
    root_router.include_router(routing_router, prefix="/v1")
    root_router.include_router(tool_router, prefix=api_v1_prefix)
    root_router.include_router(tool_router, prefix="/v1")
    root_router.include_router(mcp_router, prefix=api_v1_prefix)
    root_router.include_router(mcp_router, prefix="/v1")
    root_router.include_router(analytics_router, prefix=api_v1_prefix)
    root_router.include_router(analytics_router, prefix="/v1")
    root_router.include_router(credential_admin_router, prefix=api_v1_prefix)
    root_router.include_router(admin_control_router, prefix=api_v1_prefix)
    root_router.include_router(protocol_router)
    return root_router
