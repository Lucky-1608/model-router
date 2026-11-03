"""FastAPI router for Model Registry, Routing Policy Engine, and Intelligent Routing Engine."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from gateway.core.auth import require_admin_access
from gateway.model_registry.models import (
    ModelAliasSpec,
    ModelMetadata,
    ModelRegistrationRequest,
)
from gateway.model_registry.registry import ModelRegistry
from gateway.provider_registry.registry import ProviderRegistry
from gateway.routing_engine.engine import RoutingEngine
from gateway.routing_engine.models import RouteSimulationRequest, RoutingDecisionTrace
from gateway.routing_policy.engine import RoutingPolicyEngine
from gateway.routing_policy.models import RoutingPolicySpec

routing_router = APIRouter(prefix="/routing", tags=["Intelligent Routing & Model Registry"])


def _get_model_registry(request: Request) -> ModelRegistry:
    return request.app.state.model_registry


def _get_policy_engine(request: Request) -> RoutingPolicyEngine:
    return request.app.state.routing_policy_engine


def _get_routing_engine(request: Request) -> RoutingEngine:
    return request.app.state.routing_engine


def _get_provider_registry(request: Request) -> ProviderRegistry:
    return request.app.state.provider_registry


@routing_router.post(
    "/route",
    response_model=RoutingDecisionTrace,
    summary="Execute Two-Stage Intelligent Model Routing & Return Decision Trace",
)
async def simulate_or_execute_route(
    request: Request,
    payload: RouteSimulationRequest,
) -> RoutingDecisionTrace:
    engine = _get_routing_engine(request)
    return await engine.evaluate_route(payload)


@routing_router.get(
    "/explain/{decision_or_request_id}",
    response_model=RoutingDecisionTrace,
    summary="Inspect Full Explainable Routing Decision Trace by Decision ID or Request ID",
)
async def explain_routing_decision(
    request: Request,
    decision_or_request_id: str,
) -> RoutingDecisionTrace:
    engine = _get_routing_engine(request)
    return engine.get_trace(decision_or_request_id)


@routing_router.get(
    "/models",
    summary="List Registered Models and Semantic Aliases",
)
async def list_routing_models(
    request: Request,
    provider_id: str | None = Query(default=None),
    enabled_only: bool = Query(default=False),
) -> dict[str, Any]:
    reg = _get_model_registry(request)
    models = reg.list_models(provider_id=provider_id, enabled_only=enabled_only)
    aliases = reg.list_aliases()
    return {
        "models": [m.model_dump() for m in models],
        "aliases": [a.model_dump() for a in aliases],
        "count": len(models),
    }


@routing_router.post(
    "/models",
    response_model=ModelMetadata,
    status_code=201,
    summary="Manually Register or Update a Model in the Model Registry",
)
async def register_routing_model(
    request: Request,
    payload: ModelRegistrationRequest,
    _auth: tuple[str, str] = Depends(require_admin_access),
) -> ModelMetadata:
    reg = _get_model_registry(request)
    return reg.register_model(payload)


@routing_router.post(
    "/models/sync/{provider_id}",
    summary="Discover Models from Provider Adapter and Sync into Model Registry",
)
async def sync_provider_models_to_registry(
    request: Request,
    provider_id: str,
    _auth: tuple[str, str] = Depends(require_admin_access),
) -> dict[str, Any]:
    prov_reg = _get_provider_registry(request)
    model_reg = _get_model_registry(request)
    discovered = await prov_reg.discover_provider_models(provider_id)
    synced = model_reg.sync_discovered_models(discovered)
    return {
        "provider_id": provider_id,
        "synced_count": len(synced),
        "models": [m.model_dump() for m in synced],
    }


@routing_router.get(
    "/models/{model_id:path}/capabilities",
    summary="Inspect Model Capability Manifest, Modalities, Cost, and Latency Metadata",
)
async def get_model_capabilities(
    request: Request,
    model_id: str,
) -> dict[str, Any]:
    reg = _get_model_registry(request)
    meta = reg.get_model(model_id)
    return {
        "model_id": meta.model_id,
        "provider_id": meta.provider_id,
        "provider_model_id": meta.provider_model_id,
        "display_name": meta.display_name,
        "discovery_source": meta.discovery_source.value,
        "context_length": meta.context_length,
        "max_output_tokens": meta.max_output_tokens,
        "input_modalities": [m.value for m in meta.input_modalities],
        "output_modalities": [m.value for m in meta.output_modalities],
        "capabilities": {k.value: v.value for k, v in meta.capabilities.items()},
        "cost": meta.cost.model_dump(),
        "latency": meta.latency.model_dump(),
        "quality_scores": meta.quality_scores,
        "reliability_score": meta.reliability_score,
        "health_status": meta.health_status.value,
        "enabled": meta.enabled,
    }


@routing_router.get(
    "/policies",
    response_model=list[RoutingPolicySpec],
    summary="List Registered Routing Policies and Scoring Weights",
)
async def list_routing_policies(request: Request) -> list[RoutingPolicySpec]:
    policy_engine = _get_policy_engine(request)
    return policy_engine.list_policies()


@routing_router.put(
    "/policies/{policy_name}",
    response_model=RoutingPolicySpec,
    summary="Create or Update a Named Routing Policy and Weights",
)
async def upsert_routing_policy(
    request: Request,
    policy_name: str,
    payload: RoutingPolicySpec,
    _auth: tuple[str, str] = Depends(require_admin_access),
) -> RoutingPolicySpec:
    policy_engine = _get_policy_engine(request)
    updated = payload.model_copy(update={"name": policy_name})
    return policy_engine.upsert_policy(updated)


@routing_router.get(
    "/aliases",
    response_model=list[ModelAliasSpec],
    summary="List Semantic Model Aliases",
)
async def list_model_aliases(request: Request) -> list[ModelAliasSpec]:
    reg = _get_model_registry(request)
    return reg.list_aliases()


@routing_router.put(
    "/aliases/{alias_name}",
    response_model=ModelAliasSpec,
    summary="Create or Update a Semantic Model Alias",
)
async def upsert_model_alias(
    request: Request,
    alias_name: str,
    payload: ModelAliasSpec,
    _auth: tuple[str, str] = Depends(require_admin_access),
) -> ModelAliasSpec:
    reg = _get_model_registry(request)
    updated = payload.model_copy(update={"alias": alias_name})
    return reg.upsert_alias(updated)
