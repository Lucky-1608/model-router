"""Main API Gateway router aggregator."""

from __future__ import annotations

from fastapi import APIRouter

from gateway.api_gateway.health import health_router
from gateway.api_gateway.versioning import version_router


def build_api_gateway_router(api_v1_prefix: str = "/api/v1") -> APIRouter:
    """Constructs the root and versioned routers for the API Gateway."""
    root_router = APIRouter()
    root_router.include_router(health_router)
    root_router.include_router(version_router, prefix=api_v1_prefix)
    root_router.include_router(version_router, prefix="/v1")
    return root_router

