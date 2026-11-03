"""Authentication dependencies for the control plane (admin token) and data plane (client API keys).

Kept in `gateway.core` so every router can depend on it without importing feature packages
(which previously forced control-plane routers to import `credential_manager.router`).
"""

from __future__ import annotations

import hmac
from typing import TYPE_CHECKING, Any

from fastapi import Header, Request

from gateway.core.exceptions import (
    AuthenticationError,
    AuthorizationError,
    RateLimitExceededError,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from gateway.admin_api.models import ClientApiKeyView

CLIENT_KEY_PREFIX = "ugw_live_"
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


# =============================================================================
# Control plane: administrative access
# =============================================================================
def require_admin_access(
    request: Request,
    x_gateway_admin_token: str | None = Header(
        default=None, alias="X-Gateway-Admin-Token"
    ),
    x_gateway_admin_role: str = Header(
        default="SUPER_ADMIN", alias="X-Gateway-Admin-Role"
    ),
    authorization: str | None = Header(default=None),
) -> tuple[str, str]:
    """Verifies administrative access token and role-based permissions."""
    expected_token = (
        request.app.state.settings.admin_bootstrap_token.get_secret_value()
    )
    provided = x_gateway_admin_token
    if not provided and authorization and authorization.lower().startswith("bearer "):
        provided = authorization[7:].strip()

    if not provided or not hmac.compare_digest(provided, expected_token):
        raise AuthenticationError(
            "Valid administrative credentials required (X-Gateway-Admin-Token)"
        )

    role = x_gateway_admin_role.upper()
    if request.method in MUTATING_METHODS and role == "READ_ONLY":
        raise AuthorizationError(
            "READ_ONLY role cannot mutate gateway control-plane state"
        )
    actor_id = request.headers.get("X-Gateway-Actor-Id", f"admin:{role.lower()}")
    return actor_id, role


# =============================================================================
# Data plane: Gateway client API keys (ugw_live_...)
# =============================================================================
def is_client_api_key(value: str | None) -> bool:
    return bool(value) and value.strip().startswith(CLIENT_KEY_PREFIX)  # type: ignore[union-attr]


def extract_client_api_key(request: Request) -> str | None:
    """Returns a `ugw_live_...` key from any supported auth location, or None."""
    headers = request.headers
    candidates: list[str | None] = [
        headers.get("X-Gateway-Client-Key"),
        headers.get("x-api-key"),
        headers.get("x-goog-api-key"),
        headers.get("xi-api-key"),
    ]
    auth_val = headers.get("Authorization") or ""
    if auth_val.lower().startswith("bearer "):
        candidates.append(auth_val[7:].strip())
    elif auth_val:
        candidates.append(auth_val.strip())
    candidates.append(request.query_params.get("key"))
    for cand in candidates:
        if is_client_api_key(cand):
            return cand.strip()  # type: ignore[union-attr]
    return None


async def authenticate_client_request(request: Request) -> ClientApiKeyView | None:
    """Router-level dependency: authenticates a Gateway client API key if present.

    - A `ugw_live_...` key is validated (hash lookup, enabled flag, per-key RPM).
    - With no key, the request proceeds in BYOK pass-through mode unless
      `settings.require_client_api_key` is enabled.
    The resolved key is cached on `request.state.client_key`.
    """
    if hasattr(request.state, "client_key"):
        return request.state.client_key

    settings = request.app.state.settings
    raw_key = extract_client_api_key(request)
    if raw_key is None:
        if getattr(settings, "require_client_api_key", False):
            raise AuthenticationError(
                "A valid Gateway client API key (ugw_live_...) is required",
                error_code="GW_CLIENT_KEY_REQUIRED",
            )
        request.state.client_key = None
        return None

    admin_manager = request.app.state.admin_manager
    view = await admin_manager.authenticate_client_key(raw_key)
    request.state.client_key = view
    return view


async def enforce_client_key_policy(
    request: Request, *, target_provider: str
) -> ClientApiKeyView | None:
    """Enforces per-key provider allow-list and monthly budget once the target provider is known."""
    view = await authenticate_client_request(request)
    if view is None:
        return None

    if view.allowed_providers and target_provider not in view.allowed_providers:
        raise AuthorizationError(
            f"Client API key is not authorized for provider {target_provider!r}",
            error_code="GW_CLIENT_KEY_PROVIDER_FORBIDDEN",
            details={"provider_id": target_provider},
        )

    if view.monthly_budget_usd is not None:
        tracker: Any = request.app.state.usage_tracker
        spend = await tracker.get_client_key_monthly_spend(view.id)
        if spend >= view.monthly_budget_usd:
            raise RateLimitExceededError(
                f"Client API key monthly budget (${view.monthly_budget_usd:.2f}) exhausted",
                error_code="GW_CLIENT_KEY_BUDGET_EXCEEDED",
                retryable=False,
                details={
                    "monthly_budget_usd": view.monthly_budget_usd,
                    "current_spend_usd": round(spend, 6),
                },
            )
    return view


def resolve_client_allowed_tools(
    request: Request, header_allowed: set[str] | None
) -> set[str] | None:
    """Intersects header-requested tools with the client key's tool allow-list (key always wins)."""
    view = getattr(request.state, "client_key", None)
    if view is None or not view.allowed_tools:
        return header_allowed
    key_tools = set(view.allowed_tools)
    if header_allowed is None:
        return key_tools
    return header_allowed & key_tools

