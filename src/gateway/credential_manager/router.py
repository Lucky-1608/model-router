"""Admin API routes for Credential Lifecycle, Account Groups, Health, Usage, and Audit Logs."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request, status

from gateway.core.auth import require_admin_access
from gateway.core.canonical import HealthCheckResult
from gateway.credential_manager.manager import CredentialManager
from gateway.credential_manager.models import (
    AccountGroupCreateRequest,
    AccountGroupPublicView,
    CredentialAuditEntryView,
    CredentialCreateRequest,
    CredentialHealthView,
    CredentialPublicView,
    CredentialUpdateRequest,
)


# `require_admin_access` now lives in `gateway.core.auth` so every control-plane router can use it
# without importing this module; it is re-exported here for backward compatibility.
__all__ = ["credential_admin_router", "require_admin_access"]


credential_admin_router = APIRouter(
    prefix="/admin",
    tags=["Credential & Quota Management (Admin)"],
)


def _get_manager(request: Request) -> CredentialManager:
    return request.app.state.credential_manager


@credential_admin_router.post(
    "/account-groups",
    response_model=AccountGroupPublicView,
    status_code=status.HTTP_201_CREATED,
    summary="Create Shared Account / Workspace Quota Group",
)
async def create_account_group(
    request: Request,
    payload: AccountGroupCreateRequest,
    auth: tuple[str, str] = Depends(require_admin_access),
) -> AccountGroupPublicView:
    actor_id, _ = auth
    return await _get_manager(request).create_account_group(
        payload, actor_id=actor_id
    )


@credential_admin_router.get(
    "/account-groups",
    response_model=list[AccountGroupPublicView],
    summary="List Account / Workspace Quota Groups",
)
async def list_account_groups(
    request: Request,
    provider_id: str | None = None,
    _auth: tuple[str, str] = Depends(require_admin_access),
) -> list[AccountGroupPublicView]:
    return await _get_manager(request).list_account_groups(provider_id=provider_id)


@credential_admin_router.get(
    "/account-groups/{account_group_id}/usage",
    summary="Get Shared Account Group Usage Summary",
)
async def get_account_group_usage(
    request: Request,
    account_group_id: str,
    _auth: tuple[str, str] = Depends(require_admin_access),
) -> dict[str, Any]:
    return await _get_manager(request).get_usage_summary(
        account_group_id=account_group_id
    )


@credential_admin_router.post(
    "/credentials",
    response_model=CredentialPublicView,
    status_code=status.HTTP_201_CREATED,
    summary="Create Encrypted Provider Credential",
)
async def create_credential(
    request: Request,
    payload: CredentialCreateRequest,
    auth: tuple[str, str] = Depends(require_admin_access),
) -> CredentialPublicView:
    actor_id, _ = auth
    return await _get_manager(request).create_credential(
        payload, actor_id=actor_id
    )


@credential_admin_router.get(
    "/credentials",
    response_model=list[CredentialPublicView],
    summary="List Stored Credentials (Secrets Redacted)",
)
async def list_credentials(
    request: Request,
    provider_id: str | None = None,
    _auth: tuple[str, str] = Depends(require_admin_access),
) -> list[CredentialPublicView]:
    return await _get_manager(request).list_credentials(provider_id=provider_id)


@credential_admin_router.get(
    "/credentials/audit-logs",
    response_model=list[CredentialAuditEntryView],
    summary="List Credential Audit Logs",
)
async def list_credential_audit_logs(
    request: Request,
    credential_id: str | None = None,
    _auth: tuple[str, str] = Depends(require_admin_access),
) -> list[CredentialAuditEntryView]:
    return await _get_manager(request).list_audit_logs(
        credential_id=credential_id
    )


@credential_admin_router.get(
    "/credentials/{credential_id}",
    response_model=CredentialPublicView,
    summary="Get Credential Metadata (Secret Never Returned)",
)
async def get_credential(
    request: Request,
    credential_id: str,
    _auth: tuple[str, str] = Depends(require_admin_access),
) -> CredentialPublicView:
    return await _get_manager(request).get_credential(credential_id)


@credential_admin_router.patch(
    "/credentials/{credential_id}",
    response_model=CredentialPublicView,
    summary="Update Credential Configuration or Rotate Secret",
)
async def update_credential(
    request: Request,
    credential_id: str,
    payload: CredentialUpdateRequest,
    auth: tuple[str, str] = Depends(require_admin_access),
) -> CredentialPublicView:
    actor_id, _ = auth
    return await _get_manager(request).update_credential(
        credential_id, payload, actor_id=actor_id
    )


@credential_admin_router.post(
    "/credentials/{credential_id}/validate",
    response_model=HealthCheckResult,
    summary="Validate Credential Against Upstream Provider",
)
async def validate_credential(
    request: Request,
    credential_id: str,
    auth: tuple[str, str] = Depends(require_admin_access),
) -> HealthCheckResult:
    actor_id, _ = auth
    return await _get_manager(request).validate_credential(
        credential_id, actor_id=actor_id
    )


@credential_admin_router.post(
    "/credentials/{credential_id}/enable",
    response_model=CredentialPublicView,
    summary="Enable Credential",
)
async def enable_credential(
    request: Request,
    credential_id: str,
    auth: tuple[str, str] = Depends(require_admin_access),
) -> CredentialPublicView:
    actor_id, _ = auth
    return await _get_manager(request).enable_credential(
        credential_id, actor_id=actor_id
    )


@credential_admin_router.post(
    "/credentials/{credential_id}/disable",
    response_model=CredentialPublicView,
    summary="Disable Credential",
)
async def disable_credential(
    request: Request,
    credential_id: str,
    auth: tuple[str, str] = Depends(require_admin_access),
) -> CredentialPublicView:
    actor_id, _ = auth
    return await _get_manager(request).disable_credential(
        credential_id, actor_id=actor_id
    )


@credential_admin_router.post(
    "/credentials/{credential_id}/revoke",
    response_model=CredentialPublicView,
    summary="Revoke Credential and Wipe Encrypted Secret",
)
async def revoke_credential(
    request: Request,
    credential_id: str,
    auth: tuple[str, str] = Depends(require_admin_access),
) -> CredentialPublicView:
    actor_id, _ = auth
    return await _get_manager(request).revoke_credential(
        credential_id, actor_id=actor_id
    )


@credential_admin_router.delete(
    "/credentials/{credential_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete Credential",
)
async def delete_credential(
    request: Request,
    credential_id: str,
    auth: tuple[str, str] = Depends(require_admin_access),
) -> None:
    actor_id, _ = auth
    await _get_manager(request).delete_credential(
        credential_id, actor_id=actor_id
    )


@credential_admin_router.get(
    "/credentials/{credential_id}/health",
    response_model=CredentialHealthView,
    summary="Get Live Credential Health, Cooldown, and Quota State",
)
async def get_credential_health(
    request: Request,
    credential_id: str,
    model: str | None = None,
    _auth: tuple[str, str] = Depends(require_admin_access),
) -> CredentialHealthView:
    return await _get_manager(request).get_credential_health(
        credential_id, model=model
    )


@credential_admin_router.get(
    "/credentials/{credential_id}/usage",
    summary="Get Credential Usage Summary",
)
async def get_credential_usage(
    request: Request,
    credential_id: str,
    _auth: tuple[str, str] = Depends(require_admin_access),
) -> dict[str, Any]:
    return await _get_manager(request).get_usage_summary(
        credential_id=credential_id
    )

