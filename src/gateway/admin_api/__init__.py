"""Admin API module — RBAC-protected management endpoints for dashboard and automation."""

from gateway.admin_api.manager import AdminControlPlaneManager
from gateway.admin_api.models import (
    ClientApiKeyCreatedResponse,
    ClientApiKeyCreateRequest,
    ClientApiKeyORM,
    ClientApiKeyView,
    SystemSettingsView,
)
from gateway.admin_api.router import admin_control_router

__all__ = [
    "AdminControlPlaneManager",
    "ClientApiKeyCreateRequest",
    "ClientApiKeyCreatedResponse",
    "ClientApiKeyORM",
    "ClientApiKeyView",
    "SystemSettingsView",
    "admin_control_router",
]
