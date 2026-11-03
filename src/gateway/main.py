"""FastAPI application entrypoint and graceful lifespan manager."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from gateway.admin_api import AdminControlPlaneManager
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
from gateway.credential_manager import CredentialManager
from gateway.health_monitor import HealthMonitor
from gateway.mcp_integration import MCPClientManager
from gateway.model_registry import ModelRegistry
from gateway.observability import MetricsCollector, TracingManager
from gateway.provider_registry import ProviderRegistry
from gateway.retry_fallback import RetryFallbackEngine
from gateway.routing_engine import RoutingEngine
from gateway.routing_policy import RoutingPolicyEngine
from gateway.tool_gateway import ToolGatewayOrchestrator, ToolRegistry
from gateway.usage_tracking import UsageTracker


def create_app(settings_override: GatewaySettings | None = None) -> FastAPI:
    """Constructs and configures the Universal AI Gateway FastAPI application."""
    settings = settings_override or get_settings()
    configure_logging(settings)
    logger = get_logger("gateway.main")

    db_manager = DatabaseManager(settings)
    redis_manager = RedisManager(settings)
    http_pool = HTTPXClientPoolManager(settings)
    provider_registry = ProviderRegistry(http_pool)
    provider_registry.register_builtin_adapters()
    credential_manager = CredentialManager(
        settings=settings,
        db_manager=db_manager,
        redis_manager=redis_manager,
        provider_registry=provider_registry,
    )
    model_registry = ModelRegistry()
    routing_policy_engine = RoutingPolicyEngine()
    routing_engine = RoutingEngine(
        model_registry=model_registry,
        policy_engine=routing_policy_engine,
        provider_registry=provider_registry,
        credential_manager=credential_manager,
    )
    tool_registry = ToolRegistry(http_pool)
    mcp_manager = MCPClientManager(http_pool=http_pool, tool_registry=tool_registry)
    tool_orchestrator = ToolGatewayOrchestrator(tool_registry=tool_registry)
    health_monitor = HealthMonitor(
        redis_manager=redis_manager,
        model_registry=model_registry,
        provider_registry=provider_registry,
    )
    retry_fallback_engine = RetryFallbackEngine(
        provider_registry=provider_registry,
        model_registry=model_registry,
        routing_engine=routing_engine,
        health_monitor=health_monitor,
        credential_manager=credential_manager,
        tool_orchestrator=tool_orchestrator,
    )
    usage_tracker = UsageTracker(
        db_manager=db_manager,
        model_registry=model_registry,
        flush_interval_seconds=settings.usage_flush_interval_seconds,
        flush_batch_size=settings.usage_flush_batch_size,
        queue_max_size=settings.usage_queue_max_size,
    )
    admin_manager = AdminControlPlaneManager(
        settings=settings,
        db_manager=db_manager,
        redis_manager=redis_manager,
    )
    metrics_collector = MetricsCollector()
    tracing_manager = TracingManager()

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
        await credential_manager.ensure_tables()
        await usage_tracker.ensure_tables()
        await admin_manager.ensure_tables()
        await http_pool.get_client("default")
        await provider_registry.startup_all()
        usage_tracker.start_background_writer()
        try:
            yield
        finally:
            logger.info("gateway_shutting_down")
            await usage_tracker.stop_background_writer()
            await provider_registry.shutdown_all()
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
    app.state.provider_registry = provider_registry
    app.state.credential_manager = credential_manager
    app.state.model_registry = model_registry
    app.state.routing_policy_engine = routing_policy_engine
    app.state.routing_engine = routing_engine
    app.state.tool_registry = tool_registry
    app.state.mcp_manager = mcp_manager
    app.state.tool_orchestrator = tool_orchestrator
    app.state.health_monitor = health_monitor
    app.state.retry_fallback_engine = retry_fallback_engine
    app.state.usage_tracker = usage_tracker
    app.state.admin_manager = admin_manager
    app.state.metrics_collector = metrics_collector
    app.state.tracing_manager = tracing_manager

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(RequestContextMiddleware, version=settings.version)
    register_exception_handlers(app)
    app.include_router(build_api_gateway_router(api_v1_prefix=settings.api_v1_prefix))

    return app


app = create_app()
