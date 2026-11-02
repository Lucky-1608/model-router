"""FastAPI Router for Prometheus Metrics (`/metrics`) and Usage Analytics (`/api/v1/analytics/*`)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response

from gateway.credential_manager.router import require_admin_access
from gateway.health_monitor.models import CircuitScope
from gateway.health_monitor.monitor import HealthMonitor
from gateway.observability.metrics import MetricsCollector
from gateway.observability.tracing import TracingManager
from gateway.usage_tracking.models import (
    AnalyticsOverviewSummary,
    CredentialUsageBreakdown,
    ModelUsageBreakdown,
    ProviderUsageBreakdown,
    UsageRecordView,
)
from gateway.usage_tracking.tracker import UsageTracker

metrics_router = APIRouter(tags=["Observability & Metrics"])
analytics_router = APIRouter(
    prefix="/analytics",
    tags=["Usage Analytics & Observability"],
)


def _get_metrics(request: Request) -> MetricsCollector:
    return request.app.state.metrics_collector


def _get_tracing(request: Request) -> TracingManager:
    return request.app.state.tracing_manager


def _get_usage_tracker(request: Request) -> UsageTracker:
    return request.app.state.usage_tracker


def _get_health_monitor(request: Request) -> HealthMonitor:
    return request.app.state.health_monitor


@metrics_router.get(
    "/metrics",
    summary="Prometheus Text Exposition Endpoint",
)
async def prometheus_metrics_endpoint(request: Request) -> Response:
    collector = _get_metrics(request)
    health_monitor = _get_health_monitor(request)
    snapshots = await health_monitor.list_known_circuits()
    body = collector.format_prometheus_text(circuit_snapshots=snapshots)
    return Response(
        content=body,
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


@analytics_router.get(
    "/overview",
    response_model=AnalyticsOverviewSummary,
    summary="Get Aggregated Gateway Usage, Cost, Token, and Latency Overview",
)
async def get_analytics_overview(request: Request) -> AnalyticsOverviewSummary:
    tracker = _get_usage_tracker(request)
    return await tracker.get_overview_summary()


@analytics_router.get(
    "/providers",
    response_model=list[ProviderUsageBreakdown],
    summary="Get Provider-Level Usage, Token, Cost, and Latency Breakdown",
)
async def get_analytics_providers(request: Request) -> list[ProviderUsageBreakdown]:
    tracker = _get_usage_tracker(request)
    return await tracker.get_provider_breakdown()


@analytics_router.get(
    "/models",
    response_model=list[ModelUsageBreakdown],
    summary="Get Model-Level Usage, Token, Cost, Latency, and TTFT Breakdown",
)
async def get_analytics_models(request: Request) -> list[ModelUsageBreakdown]:
    tracker = _get_usage_tracker(request)
    return await tracker.get_model_breakdown()


@analytics_router.get(
    "/credentials",
    response_model=list[CredentialUsageBreakdown],
    summary="Get Credential-Level Usage and Cost Breakdown (No Secrets Exposed)",
)
async def get_analytics_credentials(request: Request) -> list[CredentialUsageBreakdown]:
    tracker = _get_usage_tracker(request)
    return await tracker.get_credential_breakdown()


@analytics_router.get(
    "/requests",
    response_model=list[UsageRecordView],
    summary="List Recent Gateway Request Usage Records",
)
async def get_analytics_requests(
    request: Request,
    limit: int = Query(default=50, ge=1, le=500),
    provider_id: str | None = Query(default=None),
    success_only: bool | None = Query(default=None),
) -> list[UsageRecordView]:
    tracker = _get_usage_tracker(request)
    return await tracker.list_recent_requests(
        limit=limit,
        provider_id=provider_id,
        success_only=success_only,
    )


@analytics_router.get(
    "/circuit-breakers",
    summary="List Known Provider, Model, and Credential Circuit Breakers",
)
async def list_circuit_breakers(
    request: Request,
    scope: CircuitScope | None = Query(default=None),
) -> dict[str, Any]:
    monitor = _get_health_monitor(request)
    snapshots = await monitor.list_known_circuits(scope=scope)
    return {
        "object": "list",
        "data": [s.model_dump(mode="json") for s in snapshots],
    }


@analytics_router.post(
    "/circuit-breakers/{scope}/{target_id:path}/reset",
    summary="Reset a Circuit Breaker to CLOSED State",
)
async def reset_circuit_breaker(
    request: Request,
    scope: CircuitScope,
    target_id: str,
    _auth: tuple[str, str] = Depends(require_admin_access),
) -> dict[str, Any]:
    monitor = _get_health_monitor(request)
    snap = await monitor.reset_circuit(scope, target_id)
    return snap.model_dump(mode="json")


@analytics_router.get(
    "/traces",
    summary="List Recent OpenTelemetry Trace Spans (Prompts/Secrets Redacted)",
)
async def list_recent_traces(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    trace_id: str | None = Query(default=None),
    name: str | None = Query(default=None),
) -> dict[str, Any]:
    tracing = _get_tracing(request)
    spans = tracing.list_recent_spans(limit=limit, trace_id=trace_id, name=name)
    return {
        "object": "list",
        "data": [s.model_dump(mode="json") for s in spans],
    }
