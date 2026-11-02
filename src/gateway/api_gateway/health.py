"""Health and readiness probe endpoints."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

health_router = APIRouter(tags=["Health & Readiness"])


@health_router.get("/health/live", summary="Liveness Probe")
async def liveness_probe(request: Request) -> dict[str, Any]:
    """Returns 200 OK if the gateway process is alive and event loop is responsive."""
    settings = request.app.state.settings
    return {
        "status": "alive",
        "service": settings.app_name,
        "version": settings.version,
        "environment": settings.env,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@health_router.get("/health/ready", summary="Readiness Probe")
async def readiness_probe(request: Request) -> JSONResponse:
    """Probes PostgreSQL, Redis, and HTTPX client pool readiness."""
    settings = request.app.state.settings
    db_manager = request.app.state.db_manager
    redis_manager = request.app.state.redis_manager
    http_pool = request.app.state.http_pool

    db_health, redis_health = await asyncio.gather(
        db_manager.check_health(),
        redis_manager.check_health(),
    )

    db_ok = db_health.get("status") == "healthy"
    redis_status = redis_health.get("status")
    redis_ok = redis_status in ("healthy", "degraded")

    overall_ready = db_ok and redis_ok
    if db_ok and redis_status == "healthy":
        overall_status = "ready"
    elif overall_ready:
        overall_status = "degraded"
    else:
        overall_status = "not_ready"

    http_status = (
        status.HTTP_200_OK if overall_ready else status.HTTP_503_SERVICE_UNAVAILABLE
    )

    payload = {
        "status": overall_status,
        "service": settings.app_name,
        "version": settings.version,
        "environment": settings.env,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "components": {
            "database": db_health,
            "redis": redis_health,
            "http_pool": http_pool.get_pool_stats(),
        },
    }
    return JSONResponse(status_code=http_status, content=payload)

