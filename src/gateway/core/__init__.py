"""Core infrastructure primitives for Universal AI Gateway."""

from gateway.core.db import Base, DatabaseManager, TimestampUUIDMixin
from gateway.core.exceptions import (
    AuthenticationError,
    AuthorizationError,
    ConfigurationError,
    GatewayError,
    NoCandidateFoundError,
    RateLimitExceededError,
    ResourceNotFoundError,
    ServiceUnavailableError,
    UnsupportedCapabilityError,
    UpstreamProviderError,
    register_exception_handlers,
)
from gateway.core.http import HTTPXClientPoolManager, TimeoutPolicy
from gateway.core.logging import configure_logging, get_logger
from gateway.core.redis import InMemoryRedisFallback, RedisManager
from gateway.core.security import (
    redact_sensitive_data,
    redact_string,
    request_id_ctx_var,
)

__all__ = [
    "AuthenticationError",
    "AuthorizationError",
    "Base",
    "ConfigurationError",
    "DatabaseManager",
    "GatewayError",
    "HTTPXClientPoolManager",
    "InMemoryRedisFallback",
    "NoCandidateFoundError",
    "RateLimitExceededError",
    "RedisManager",
    "ResourceNotFoundError",
    "ServiceUnavailableError",
    "TimeoutPolicy",
    "TimestampUUIDMixin",
    "UnsupportedCapabilityError",
    "UpstreamProviderError",
    "configure_logging",
    "get_logger",
    "redact_sensitive_data",
    "redact_string",
    "register_exception_handlers",
    "request_id_ctx_var",
]

