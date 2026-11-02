"""Centralized exception hierarchy and FastAPI exception handlers."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from gateway.core.logging import get_logger
from gateway.core.security import redact_sensitive_data, request_id_ctx_var

logger = get_logger("gateway.exceptions")


class GatewayError(Exception):
    """Base exception for all Universal AI Gateway domain errors."""

    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    error_type: str = "internal_gateway_error"
    error_code: str = "GW_INTERNAL_ERROR"
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        param: str | None = None,
        status_code: int | None = None,
        error_type: str | None = None,
        error_code: str | None = None,
        retryable: bool | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.param = param
        if status_code is not None:
            self.status_code = status_code
        if error_type is not None:
            self.error_type = error_type
        if error_code is not None:
            self.error_code = error_code
        if retryable is not None:
            self.retryable = retryable
        self.details = details or {}

    def to_response_dict(self, request_id: str | None = None) -> dict[str, Any]:
        req_id = request_id or request_id_ctx_var.get()
        gateway_meta: dict[str, Any] = {
            "request_id": req_id,
            "retryable": self.retryable,
        }
        if self.details:
            gateway_meta.update(redact_sensitive_data(self.details))
        return {
            "error": {
                "message": redact_sensitive_data(self.message),
                "type": self.error_type,
                "code": self.error_code,
                "param": self.param,
                "gateway_metadata": gateway_meta,
            }
        }


class AuthenticationError(GatewayError):
    status_code = status.HTTP_401_UNAUTHORIZED
    error_type = "authentication_error"
    error_code = "GW_AUTHENTICATION_FAILED"
    retryable = False


class AuthorizationError(GatewayError):
    status_code = status.HTTP_403_FORBIDDEN
    error_type = "authorization_error"
    error_code = "GW_FORBIDDEN"
    retryable = False


class ResourceNotFoundError(GatewayError):
    status_code = status.HTTP_404_NOT_FOUND
    error_type = "not_found_error"
    error_code = "GW_NOT_FOUND"
    retryable = False


class UnsupportedCapabilityError(GatewayError):
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    error_type = "unsupported_capability_error"
    error_code = "GW_UNSUPPORTED_CAPABILITY"
    retryable = False


class NoCandidateFoundError(GatewayError):
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    error_type = "routing_no_candidate_error"
    error_code = "GW_NO_CANDIDATE_FOUND"
    retryable = False


class RateLimitExceededError(GatewayError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    error_type = "rate_limit_error"
    error_code = "GW_RATE_LIMIT_EXCEEDED"
    retryable = True


class UpstreamProviderError(GatewayError):
    status_code = status.HTTP_502_BAD_GATEWAY
    error_type = "upstream_provider_error"
    error_code = "GW_UPSTREAM_ERROR"
    retryable = True


class ServiceUnavailableError(GatewayError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    error_type = "service_unavailable_error"
    error_code = "GW_SERVICE_UNAVAILABLE"
    retryable = True


class ConfigurationError(GatewayError):
    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    error_type = "configuration_error"
    error_code = "GW_CONFIGURATION_ERROR"
    retryable = False


def register_exception_handlers(app: FastAPI) -> None:
    """Registers centralized exception handlers onto a FastAPI application."""

    @app.exception_handler(GatewayError)
    async def handle_gateway_error(request: Request, exc: GatewayError) -> JSONResponse:
        req_id = getattr(request.state, "request_id", None) or request_id_ctx_var.get()
        logger.warning(
            "gateway_domain_error",
            error_code=exc.error_code,
            error_type=exc.error_type,
            status_code=exc.status_code,
            message=exc.message,
            path=request.url.path,
        )
        headers = {"X-Gateway-Request-Id": req_id} if req_id else {}
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.to_response_dict(request_id=req_id),
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        req_id = getattr(request.state, "request_id", None) or request_id_ctx_var.get()
        errors = exc.errors()
        first_param = None
        if errors and "loc" in errors[0]:
            first_param = ".".join(str(part) for part in errors[0]["loc"])
        payload = {
            "error": {
                "message": "Request validation failed",
                "type": "invalid_request_error",
                "code": "GW_VALIDATION_ERROR",
                "param": first_param,
                "gateway_metadata": {
                    "request_id": req_id,
                    "retryable": False,
                    "validation_errors": redact_sensitive_data(
                        [
                            {
                                "loc": list(err.get("loc", ())),
                                "msg": err.get("msg"),
                                "type": err.get("type"),
                            }
                            for err in errors
                        ]
                    ),
                },
            }
        }
        headers = {"X-Gateway-Request-Id": req_id} if req_id else {}
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=payload,
            headers=headers,
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        req_id = getattr(request.state, "request_id", None) or request_id_ctx_var.get()
        payload = {
            "error": {
                "message": str(exc.detail),
                "type": "http_error",
                "code": f"GW_HTTP_{exc.status_code}",
                "param": None,
                "gateway_metadata": {
                    "request_id": req_id,
                    "retryable": exc.status_code in (408, 429, 502, 503, 504),
                },
            }
        }
        headers = {"X-Gateway-Request-Id": req_id} if req_id else {}
        return JSONResponse(
            status_code=exc.status_code,
            content=payload,
            headers=headers,
        )

    @app.exception_handler(Exception)
    async def handle_unhandled_exception(
        request: Request, exc: Exception
    ) -> JSONResponse:
        req_id = getattr(request.state, "request_id", None) or request_id_ctx_var.get()
        logger.error(
            "unhandled_gateway_exception",
            exc_type=type(exc).__name__,
            error=str(exc),
            path=request.url.path,
            exc_info=True,
        )
        payload = {
            "error": {
                "message": "An unexpected internal error occurred.",
                "type": "internal_gateway_error",
                "code": "GW_UNHANDLED_EXCEPTION",
                "param": None,
                "gateway_metadata": {
                    "request_id": req_id,
                    "retryable": False,
                },
            }
        }
        headers = {"X-Gateway-Request-Id": req_id} if req_id else {}
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=payload,
            headers=headers,
        )

