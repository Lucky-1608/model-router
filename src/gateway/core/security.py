"""Security utilities, secret redaction patterns, and context variables."""

from __future__ import annotations

from contextvars import ContextVar
import re
from typing import Any

request_id_ctx_var: ContextVar[str | None] = ContextVar("request_id", default=None)

SENSITIVE_HEADER_KEYS: frozenset[str] = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "x-api-key",
        "xi-api-key",
        "x-goog-api-key",
        "x-gateway-api-key",
        "cookie",
        "set-cookie",
        "master_kek_base64",
        "admin_bootstrap_token",
        "encrypted_secret",
        "secret",
        "api_key",
        "password",
        "token",
    }
)

SECRET_TOKEN_REGEX = re.compile(
    r"("
    r"sk-ant-[A-Za-z0-9_\-]{8,}"
    r"|sk-proj-[A-Za-z0-9_\-]{8,}"
    r"|sk-[A-Za-z0-9_\-]{12,}"
    r"|AIza[0-9A-Za-z_\-]{20,}"
    r"|gsk_[A-Za-z0-9_\-]{12,}"
    r"|ugw_(?:live|test|admin)_[A-Za-z0-9_\-]{8,}"
    r"|Bearer\s+[A-Za-z0-9_\-\.~+/]+=*"
    r")"
)


def redact_string(value: str) -> str:
    """Redacts known API key patterns and Bearer tokens from a string."""
    return SECRET_TOKEN_REGEX.sub("[REDACTED_SECRET]", value)


def redact_sensitive_data(data: Any) -> Any:
    """Recursively scrubs sensitive keys and secret token patterns from dicts/lists/strings."""
    if isinstance(data, str):
        return redact_string(data)
    if isinstance(data, dict):
        sanitized: dict[Any, Any] = {}
        for key, val in data.items():
            key_lower = str(key).lower()
            if key_lower in SENSITIVE_HEADER_KEYS or key_lower.endswith(("_secret", "_api_key", "_token", "_password")):
                sanitized[key] = "[REDACTED]"
            else:
                sanitized[key] = redact_sensitive_data(val)
        return sanitized
    if isinstance(data, list):
        return [redact_sensitive_data(item) for item in data]
    if isinstance(data, tuple):
        return tuple(redact_sensitive_data(item) for item in data)
    return data

