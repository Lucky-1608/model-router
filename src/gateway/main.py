"""FastAPI application entrypoint and graceful lifespan manager."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from gateway.api_gateway import RequestContextMiddleware, build_api_gateway_router
from gateway.config import GatewaySettings, get_settings
from gateway.core import (
    DatabaseManager,
    HTTPXClientPoolManager,
    RedisManager,
    configure_logging,
    get_logger,
    register_exception_handlers,
)


def create_app(settings_override: GatewaySettings | None = None) -> FastAPI:
    """Constructs and configures the Universal AI Gateway FastAPI application."""
    settings = settings_override or get_settings()
    configure_logging(settings)
    logger = get_logger("gateway.main")

    db_manager = DatabaseManager(settings)
    redis_manager = RedisManager(settings)
    http_pool = HTTPXClientPoolManager(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logger.info(
            "gateway_starting",
            app_name=settings.app_name,
            version=settings.version,
            environment=settings.env,
        )
        db_manager.init_engine()
        redis_manager.init_client()
        await http_pool.get_client("default")
        try:
            yield
        finally:
            logger.info("gateway_shutting_down")
            await http_pool.aclose_all()
            await redis_manager.close()
            await db_manager.dispose()
            logger.info("gateway_shutdown_complete")

    app = FastAPI(
        title=settings.app_name,
        version=settings.version,
        description=(
            "Production-oriented, self-hosted Universal AI Gateway with multi-provider routing, "
            "multi-key rotation, native protocol passthrough, and MCP orchestration."
        ),
        openapi_url=f"{settings.api_v1_prefix}/openapi.json",
        docs_url=f"{settings.api_v1_prefix}/docs",
        redoc_url=f"{settings.api_v1_prefix}/redoc",
        lifespan=lifespan,
    )

    app.state.settings = settings
    app.state.db_manager = db_manager
    app.state.redis_manager = redis_manager
    app.state.http_pool = http_pool

    app.add_middleware(RequestContextMiddleware, version=settings.version)
    register_exception_handlers(app)
    app.include_router(build_api_gateway_router(api_v1_prefix=settings.api_v1_prefix))

    return app


app = create_app()

