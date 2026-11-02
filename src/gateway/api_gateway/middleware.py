"""Request context, correlation ID, and timing middleware."""

from __future__ import annotations

import re
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from gateway.core.security import request_id_ctx_var

SAFE_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9_\-]{4,64}$")


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Binds a correlation request_id and emits standard Gateway telemetry headers."""

    def __init__(self, app, version: str = "0.1.0") -> None:
        super().__init__(app)
        self._version = version

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        incoming_id = request.headers.get("X-Gateway-Request-Id") or request.headers.get(
            "X-Request-Id"
        )
        if incoming_id and SAFE_REQUEST_ID_PATTERN.match(incoming_id):
            request_id = incoming_id
        else:
            request_id = str(uuid.uuid4())

        request.state.request_id = request_id
        token = request_id_ctx_var.set(request_id)
        start_ns = time.perf_counter_ns()
        try:
            response = await call_next(request)
            elapsed_ms = (time.perf_counter_ns() - start_ns) / 1_000_000.0
            response.headers["X-Gateway-Request-Id"] = request_id
            response.headers["X-Gateway-Version"] = self._version
            response.headers["X-Gateway-Latency-Overhead-Ms"] = f"{elapsed_ms:.2f}"
            return response
        finally:
            request_id_ctx_var.reset(token)

