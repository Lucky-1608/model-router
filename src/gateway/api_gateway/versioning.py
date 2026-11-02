"""API versioning and capability discovery metadata endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

version_router = APIRouter(tags=["API Versioning"])


@version_router.get("/version", summary="Get Gateway Version and Supported API Modes")
async def get_version_info(request: Request) -> dict[str, Any]:
    """Returns version metadata, supported integration modes, and API prefixes."""
    settings = request.app.state.settings
    return {
        "service": settings.app_name,
        "version": settings.version,
        "api_version": "v1",
        "environment": settings.env,
        "supported_integration_modes": ["unified", "native", "custom"],
        "namespaces": {
            "unified": "/v1",
            "native_passthrough": "/v1/native/{provider}",
            "custom_plugins": "/v1/custom/{plugin_slug}",
            "admin": f"{settings.api_v1_prefix}/admin",
            "routing": f"{settings.api_v1_prefix}/routing",
        },
        "default_routing_weights": {
            "quality": settings.default_weight_quality,
            "reliability": settings.default_weight_reliability,
            "cost": settings.default_weight_cost,
            "latency": settings.default_weight_latency,
            "capacity": settings.default_weight_capacity,
        },
    }

