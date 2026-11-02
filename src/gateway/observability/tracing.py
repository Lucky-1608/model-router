"""OpenTelemetry-compatible W3C Trace Context and Span Recorder."""

from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from datetime import UTC, datetime
import secrets
import time
from typing import Any, Iterator

from pydantic import BaseModel, Field


class TraceSpan(BaseModel):
    """Represents a recorded distributed trace span (`ugw.request`, `ugw.routing`, `ugw.provider.execute`, `ugw.tool.execute`).

    Never stores raw prompts, message bodies, or secret credentials.
    """

    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    name: str
    start_time: datetime
    end_time: datetime | None = None
    duration_ms: float = 0.0
    status: str = "OK"
    attributes: dict[str, Any] = Field(default_factory=dict)


class TracingManager:
    """Manages W3C traceparent parsing and records structured telemetry spans."""

    _FORBIDDEN_ATTR_KEYS = frozenset(
        {
            "api_key",
            "authorization",
            "secret",
            "token",
            "password",
            "prompt",
            "messages",
            "input",
            "output_text",
        }
    )

    def __init__(self, max_spans: int = 1000) -> None:
        self._spans: deque[TraceSpan] = deque(maxlen=max_spans)

    @staticmethod
    def parse_traceparent(header_value: str | None) -> tuple[str, str | None]:
        """Parses W3C `traceparent` header (`00-<32hex trace_id>-<16hex span_id>-<2hex flags>`).

        Generates a fresh 128-bit trace_id if absent or malformed.
        """
        if header_value:
            parts = header_value.strip().split("-")
            if len(parts) == 4 and len(parts[1]) == 32 and len(parts[2]) == 16:
                return parts[1].lower(), parts[2].lower()
        return secrets.token_hex(16), None

    @staticmethod
    def format_traceparent(trace_id: str, span_id: str, sampled: bool = True) -> str:
        flags = "01" if sampled else "00"
        return f"00-{trace_id}-{span_id}-{flags}"

    def _sanitize_attributes(self, attributes: dict[str, Any]) -> dict[str, Any]:
        clean: dict[str, Any] = {}
        for k, v in attributes.items():
            if k.lower() in self._FORBIDDEN_ATTR_KEYS:
                clean[k] = "[REDACTED]"
            else:
                clean[k] = v
        return clean

    @contextmanager
    def start_span(
        self,
        name: str,
        *,
        trace_id: str | None = None,
        parent_span_id: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> Iterator[TraceSpan]:
        """Context manager that measures and records a `TraceSpan`."""
        resolved_trace_id = trace_id or secrets.token_hex(16)
        span_id = secrets.token_hex(8)
        start_wall = datetime.now(UTC)
        start_perf = time.perf_counter()

        span = TraceSpan(
            trace_id=resolved_trace_id,
            span_id=span_id,
            parent_span_id=parent_span_id,
            name=name,
            start_time=start_wall,
            attributes=self._sanitize_attributes(attributes or {}),
        )
        try:
            yield span
        except Exception as exc:
            span.status = "ERROR"
            span.attributes["error.type"] = type(exc).__name__
            raise
        finally:
            span.end_time = datetime.now(UTC)
            span.duration_ms = round((time.perf_counter() - start_perf) * 1000.0, 2)
            span.attributes = self._sanitize_attributes(span.attributes)
            self._spans.appendleft(span)

    def list_recent_spans(
        self,
        *,
        limit: int = 100,
        trace_id: str | None = None,
        name: str | None = None,
    ) -> list[TraceSpan]:
        results: list[TraceSpan] = []
        for s in self._spans:
            if trace_id and s.trace_id != trace_id:
                continue
            if name and s.name != name:
                continue
            results.append(s)
            if len(results) >= limit:
                break
        return results

