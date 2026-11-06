"""RBAC-Protected Admin API Router for Dashboard Control Plane, Client API Keys, and System Settings."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request, status

from gateway.admin_api.manager import AdminControlPlaneManager
from gateway.admin_api.models import (
    ClientApiKeyCreatedResponse,
    ClientApiKeyCreateRequest,
    ClientApiKeyUpdateRequest,
    ClientApiKeyView,
    SystemSettingsView,
)
from gateway.credential_manager.router import require_admin_access

admin_control_router = APIRouter(
    prefix="/admin",
    tags=["Admin Control Plane & Dashboard API"],
)


def _get_admin_manager(request: Request) -> AdminControlPlaneManager:
    return request.app.state.admin_manager


@admin_control_router.post(
    "/client-keys",
    response_model=ClientApiKeyCreatedResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Issue a New Client Gateway API Key (Raw Secret Shown Once)",
)
async def create_client_api_key(
    request: Request,
    payload: ClientApiKeyCreateRequest,
    _auth: tuple[str, str] = Depends(require_admin_access),
) -> ClientApiKeyCreatedResponse:
    return await _get_admin_manager(request).create_client_key(payload)


@admin_control_router.get(
    "/client-keys",
    response_model=list[ClientApiKeyView],
    summary="List Issued Client Gateway API Keys (Hashes/Secrets Never Returned)",
)
async def list_client_api_keys(
    request: Request,
    _auth: tuple[str, str] = Depends(require_admin_access),
) -> list[ClientApiKeyView]:
    return await _get_admin_manager(request).list_client_keys()


@admin_control_router.post(
    "/client-keys/{key_id}/revoke",
    response_model=ClientApiKeyView,
    summary="Revoke a Client Gateway API Key",
)
async def revoke_client_api_key(
    request: Request,
    key_id: str,
    _auth: tuple[str, str] = Depends(require_admin_access),
) -> ClientApiKeyView:
    return await _get_admin_manager(request).revoke_client_key(key_id)


@admin_control_router.put(
    "/client-keys/{key_id}",
    response_model=ClientApiKeyView,
    summary="Update an existing Client Gateway API Key",
)
async def update_client_api_key(
    request: Request,
    key_id: str,
    payload: ClientApiKeyUpdateRequest,
    _auth: tuple[str, str] = Depends(require_admin_access),
) -> ClientApiKeyView:
    return await _get_admin_manager(request).update_client_key(key_id, payload)


@admin_control_router.delete(
    "/client-keys/{key_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a Client Gateway API Key",
)
async def delete_client_api_key(
    request: Request,
    key_id: str,
    _auth: tuple[str, str] = Depends(require_admin_access),
) -> None:
    await _get_admin_manager(request).delete_client_key(key_id)


@admin_control_router.get(
    "/system/settings",
    response_model=SystemSettingsView,
    summary="Get Sanitized System Settings and Infrastructure Status",
)
async def get_system_settings(
    request: Request,
    _auth: tuple[str, str] = Depends(require_admin_access),
) -> SystemSettingsView:
    return await _get_admin_manager(request).get_system_settings()


@admin_control_router.get(
    "/dashboard/summary",
    summary="Unified Control-Plane Snapshot for the Management Dashboard",
)
async def get_dashboard_summary(
    request: Request,
    _auth: tuple[str, str] = Depends(require_admin_access),
) -> dict[str, Any]:
    """Aggregates live counts and statuses across all 12 gateway subsystems for the dashboard."""
    app_state = request.app.state
    providers = app_state.provider_registry.list_adapters(enabled_only=False)
    models = app_state.model_registry.list_models(enabled_only=False)
    aliases = app_state.model_registry.list_aliases()
    policies = app_state.routing_policy_engine.list_policies()
    credentials = await app_state.credential_manager.list_credentials()
    account_groups = await app_state.credential_manager.list_account_groups()
    tools = app_state.tool_registry.list_tools(enabled_only=False)
    mcp_servers = app_state.mcp_manager.list_servers()
    client_keys = await app_state.admin_manager.list_client_keys()
    analytics_overview = await app_state.usage_tracker.get_overview_summary()
    circuits = await app_state.health_monitor.list_known_circuits()
    settings_view = await app_state.admin_manager.get_system_settings()

    return {
        "overview": analytics_overview.model_dump(mode="json"),
        "counts": {
            "providers": len(providers),
            "models": len(models),
            "aliases": len(aliases),
            "routing_policies": len(policies),
            "credentials": len(credentials),
            "account_groups": len(account_groups),
            "tools": len(tools),
            "mcp_servers": len(mcp_servers),
            "client_keys": len(client_keys),
            "open_circuits": sum(1 for c in circuits if c.state.value == "OPEN"),
        },
        "system": settings_view.model_dump(mode="json"),
    }

