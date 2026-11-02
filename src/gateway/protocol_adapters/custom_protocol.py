"""Custom protocol interface supporting configurable methods, transformations, streaming parsers, and error mappings."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
import json
from typing import Any, Literal

from pydantic import BaseModel, Field

from gateway.capability_registry.models import CapabilityFlag, ProtocolType
from gateway.core.canonical import (
    CanonicalInternalRequest,
    CanonicalInternalResponse,
    CanonicalMessage,
    CanonicalStreamEvent,
)
from gateway.core.exceptions import UnsupportedCapabilityError
from gateway.protocol_adapters.base import BaseProtocolAdapter
from gateway.provider_adapters.custom_adapter import extract_dot_path


class CustomProtocolSpec(BaseModel):
    """Declarative specification for a custom wire protocol transformation."""

    protocol_name: str = "custom_rest"
    http_method: Literal["POST", "GET", "PUT"] = "POST"
    request_model_path: str = "model"
    request_prompt_path: str = "prompt"
    request_messages_path: str = "messages"
    request_stream_path: str = "stream"
    response_envelope_template: dict[str, Any] = Field(
        default_factory=lambda: {
            "id": "{{request_id}}",
            "model": "{{model}}",
            "output": "{{output_text}}",
            "tokens": "{{total_tokens}}",
        }
    )
    stream_framing: Literal["sse_json", "ndjson", "raw_text"] = "sse_json"


def _render_template(template: Any, context: Mapping[str, Any]) -> Any:
    if isinstance(template, str):
        if template.startswith("{{") and template.endswith("}}"):
            key = template[2:-2].strip()
            if key in context:
                return context[key]
        out = template
        for k, v in context.items():
            out = out.replace(f"{{{{{k}}}}}", str(v))
        return out
    if isinstance(template, Mapping):
        return {k: _render_template(v, context) for k, v in template.items()}
    if isinstance(template, list):
        return [_render_template(item, context) for item in template]
    return template


class CustomProtocolAdapter(BaseProtocolAdapter):
    """Configurable protocol adapter for custom REST client/provider integrations."""

    protocol_type = ProtocolType.CUSTOM_HTTP

    def __init__(self, spec: CustomProtocolSpec | None = None) -> None:
        self._spec = spec or CustomProtocolSpec()

    @property
    def spec(self) -> CustomProtocolSpec:
        return self._spec

    def parse_request(
        self, payload: Mapping[str, Any], **kwargs: Any
    ) -> CanonicalInternalRequest:
        provider_id = str(kwargs.get("provider_id") or "")
        model_val = extract_dot_path(payload, self._spec.request_model_path) or kwargs.get(
            "model"
        )
        if not model_val:
            raise UnsupportedCapabilityError(
                f"Model not found at path {self._spec.request_model_path!r}",
                param=self._spec.request_model_path,
            )
        model_str = str(model_val)
        canonical_model = (
            f"{provider_id}/{model_str}"
            if provider_id and "/" not in model_str
            else model_str
        )

        stream = bool(extract_dot_path(payload, self._spec.request_stream_path) or False)
        required_caps = {CapabilityFlag.CHAT_COMPLETIONS}
        if stream:
            required_caps.add(CapabilityFlag.STREAMING)

        messages: list[CanonicalMessage] = []
        raw_msgs = extract_dot_path(payload, self._spec.request_messages_path)
        if isinstance(raw_msgs, list) and raw_msgs:
            for item in raw_msgs:
                if isinstance(item, Mapping):
                    role = str(item.get("role", "user"))
                    text = str(item.get("content") or item.get("text") or "")
                    valid_role = (
                        role
                        if role in ("system", "developer", "user", "assistant", "tool")
                        else "user"
                    )
                    messages.append(CanonicalMessage.from_text(valid_role, text))  # type: ignore[arg-type]
        else:
            prompt_val = extract_dot_path(payload, self._spec.request_prompt_path)
            if prompt_val is None:
                raise UnsupportedCapabilityError(
                    f"Neither {self._spec.request_messages_path!r} nor {self._spec.request_prompt_path!r} provided",
                    param=self._spec.request_prompt_path,
                )
            messages.append(CanonicalMessage.from_text("user", str(prompt_val)))

        return CanonicalInternalRequest(
            source_protocol=ProtocolType.CUSTOM_HTTP,
            integration_mode="custom",
            requested_model=canonical_model,
            provider_model_id=model_str,
            messages=messages,
            stream=stream,
            required_capabilities=required_caps,
        )

    def format_response(
        self, response: CanonicalInternalResponse
    ) -> dict[str, Any]:
        ctx = {
            "request_id": response.request_id,
            "provider_id": response.provider_id,
            "model": response.model,
            "output_text": response.output_text,
            "finish_reason": response.finish_reason,
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
            "total_tokens": response.usage.total_tokens,
        }
        rendered = _render_template(self._spec.response_envelope_template, ctx)
        return dict(rendered) if isinstance(rendered, Mapping) else {"output": rendered}

    async def format_stream_events(
        self, events: AsyncIterator[CanonicalStreamEvent], model: str
    ) -> AsyncIterator[bytes]:
        async for ev in events:
            if ev.event_type == "text_delta" and ev.text_delta is not None:
                if self._spec.stream_framing == "raw_text":
                    yield ev.text_delta.encode("utf-8")
                elif self._spec.stream_framing == "ndjson":
                    yield (
                        json.dumps({"model": ev.model or model, "delta": ev.text_delta})
                        + "\n"
                    ).encode("utf-8")
                else:
                    yield (
                        f"data: {json.dumps({'model': ev.model or model, 'delta': ev.text_delta})}\n\n"
                    ).encode("utf-8")
            elif ev.event_type == "done":
                if self._spec.stream_framing == "sse_json":
                    yield b"data: [DONE]\n\n"

