"""Structured logging configuration with automatic secret redaction."""

from __future__ import annotations

import logging
import sys
from typing import Any, MutableMapping

import structlog

from gateway.config.settings import GatewaySettings
from gateway.core.security import redact_sensitive_data, request_id_ctx_var


def _inject_request_context(
    _logger: Any, _method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    req_id = request_id_ctx_var.get()
    if req_id and "request_id" not in event_dict:
        event_dict["request_id"] = req_id
    return event_dict


def _redact_event_dict(
    _logger: Any, _method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    return redact_sensitive_data(dict(event_dict))


def configure_logging(settings: GatewaySettings) -> None:
    """Configures structlog and standard library logging with mandatory redaction."""
    log_level = getattr(logging, settings.log_level.upper(), logging.INFO)

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level,
        force=True,
    )

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        _inject_request_context,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        _redact_event_dict,
    ]

    if settings.log_format == "console":
        renderer: Any = structlog.dev.ConsoleRenderer()
    else:
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = "gateway") -> structlog.stdlib.BoundLogger:
    """Returns a configured structlog bound logger."""
    return structlog.get_logger(name)

