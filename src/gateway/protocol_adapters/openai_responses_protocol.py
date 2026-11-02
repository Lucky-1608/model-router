"""OpenAI Responses API protocol adapter (/v1/responses)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
import json
import time
from typing import Any
import uuid

from gateway.capability_registry.models import CapabilityFlag, ProtocolType
from gateway.core.canonical import (
    CanonicalInternalRequest,
    CanonicalInternalResponse,
    CanonicalMessage,
    CanonicalMessagePart,
    CanonicalStreamEvent,
    NormalizedToolCall,
    NormalizedToolDefinition,
    NormalizedToolResult,
    ResponseFormatSpec,
)
from gateway.core.exceptions import UnsupportedCapabilityError
from gateway.protocol_adapters.base import (
    BaseProtocolAdapter,
    resolve_target_provider_and_model,
)


class OpenAIResponsesProtocolAdapter(BaseProtocolAdapter):
    """Protocol adapter for OpenAI Responses API (/v1/responses) preserving item-based semantics."""

    protocol_type = ProtocolType.OPENAI_RESPONSES

    def parse_request(
        self, payload: Mapping[str, Any], **kwargs: Any
    ) -> CanonicalInternalRequest:
        model_raw = str(payload.get("model") or "").strip()
        if not model_raw:
            raise UnsupportedCapabilityError("Field 'model' is required", param="model")

        target_provider, clean_model = resolve_target_provider_and_model(
            model_raw, default_provider="openai"
        )

        # Responses API stateful continuation (previous_response_id) or store is only supported natively on OpenAI
        if target_provider != "openai" and payload.get("previous_response_id"):
            raise UnsupportedCapabilityError(
                f"Responses API parameter 'previous_response_id' is not supported by provider {target_provider!r}",
                param="previous_response_id",
            )

        required_caps: set[CapabilityFlag] = {CapabilityFlag.RESPONSES_API}
        stream = bool(payload.get("stream", False))
        if stream:
            required_caps.add(CapabilityFlag.STREAMING)

        system_instructions: list[str] = []
        if payload.get("instructions"):
            system_instructions.append(str(payload["instructions"]))
            required_caps.add(CapabilityFlag.SYSTEM_INSTRUCTIONS)

        raw_input = payload.get("input")
        if raw_input is None:
            raise UnsupportedCapabilityError("Field 'input' is required", param="input")

        messages: list[CanonicalMessage] = []
        if isinstance(raw_input, str):
            messages.append(CanonicalMessage.from_text("user", raw_input))
        elif isinstance(raw_input, list):
            for item in raw_input:
                if not isinstance(item, Mapping):
                    continue
                item_type = item.get("type", "message")
                if item_type == "function_call_output":
                    messages.append(
                        CanonicalMessage(
                            role="tool",
                            parts=[
                                CanonicalMessagePart(
                                    type="tool_result",
                                    tool_result=NormalizedToolResult(
                                        call_id=str(item.get("call_id", "")),
                                        output_text=str(item.get("output", "")),
                                    ),
                                )
                            ],
                        )
                    )
                elif item_type == "function_call":
                    args_str = str(item.get("arguments", "{}"))
                    try:
                        args_dict = json.loads(args_str)
                    except ValueError:
                        args_dict = {}
                    messages.append(
                        CanonicalMessage(
                            role="assistant",
                            parts=[
                                CanonicalMessagePart(
                                    type="tool_call",
                                    tool_call=NormalizedToolCall(
                                        call_id=str(item.get("call_id") or item.get("id") or ""),
                                        name=str(item.get("name", "")),
                                        arguments_json=args_str,
                                        arguments_dict=args_dict if isinstance(args_dict, dict) else {},
                                    ),
                                )
                            ],
                        )
                    )
                else:
                    role = str(item.get("role", "user"))
                    content = item.get("content")
                    parts: list[CanonicalMessagePart] = []
                    if isinstance(content, str):
                        parts.append(CanonicalMessagePart(type="text", text=content))
                    elif isinstance(content, list):
                        for block in content:
                            if not isinstance(block, Mapping):
                                continue
                            btype = block.get("type")
                            if btype in ("input_text", "output_text", "text"):
                                parts.append(
                                    CanonicalMessagePart(
                                        type="text", text=str(block.get("text", ""))
                                    )
                                )
                            elif btype == "input_image":
                                required_caps.add(CapabilityFlag.VISION)
                                parts.append(
                                    CanonicalMessagePart(
                                        type="image_url",
                                        media_url=str(block.get("image_url", "")),
                                    )
                                )
                    valid_role = (
                        role
                        if role in ("system", "developer", "user", "assistant", "tool")
                        else "user"
                    )
                    messages.append(CanonicalMessage(role=valid_role, parts=parts))  # type: ignore[arg-type]

        tools_out: list[NormalizedToolDefinition] = []
        for t in payload.get("tools") or []:
            if not isinstance(t, Mapping):
                continue
            required_caps.add(CapabilityFlag.TOOL_CALLING)
            t_type = str(t.get("type", "function"))
            if t_type == "function":
                tools_out.append(
                    NormalizedToolDefinition(
                        name=str(t.get("name", "")),
                        description=t.get("description"),
                        parameters_schema=dict(
                            t.get("parameters", {"type": "object", "properties": {}})
                        ),
                        strict=bool(t.get("strict", False)),
                    )
                )
            else:
                tools_out.append(
                    NormalizedToolDefinition(
                        name=t_type,
                        is_provider_native=True,
                        native_type=t_type,
                        provider_metadata={"provider_id": "openai", "raw": dict(t)},
                    )
                )

        response_format_spec: ResponseFormatSpec | None = None
        text_cfg = payload.get("text")
        if isinstance(text_cfg, Mapping) and isinstance(text_cfg.get("format"), Mapping):
            fmt = text_cfg["format"]
            fmt_type = str(fmt.get("type", "text"))
            if fmt_type == "json_object":
                required_caps.add(CapabilityFlag.JSON_MODE)
                response_format_spec = ResponseFormatSpec(type="json_object")
            elif fmt_type == "json_schema":
                required_caps.add(CapabilityFlag.STRUCTURED_OUTPUTS)
                response_format_spec = ResponseFormatSpec(
                    type="json_schema",
                    schema_name=fmt.get("name"),
                    json_schema=fmt.get("schema"),
                    strict=bool(fmt.get("strict", False)),
                )

        if payload.get("reasoning") is not None:
            required_caps.add(CapabilityFlag.REASONING_TOKENS)

        return CanonicalInternalRequest(
            source_protocol=ProtocolType.OPENAI_RESPONSES,
            requested_model=model_raw,
            provider_model_id=clean_model,
            system_instructions=system_instructions,
            messages=messages,
            tools=tools_out,
            tool_choice=payload.get("tool_choice"),
            parallel_tool_calls=payload.get("parallel_tool_calls"),
            response_format=response_format_spec,
            temperature=payload.get("temperature"),
            top_p=payload.get("top_p"),
            max_output_tokens=payload.get("max_output_tokens"),
            stream=stream,
            required_capabilities=required_caps,
            provider_extensions={
                k: v
                for k, v in payload.items()
                if k in ("previous_response_id", "reasoning", "metadata", "store")
            },
        )

    def format_response(
        self, response: CanonicalInternalResponse
    ) -> dict[str, Any]:
        resp_id = response.provider_response_id or f"resp_{uuid.uuid4().hex[:24]}"
        output_items: list[dict[str, Any]] = []

        if response.output_text:
            output_items.append(
                {
                    "type": "message",
                    "id": f"msg_{uuid.uuid4().hex[:16]}",
                    "status": "completed",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": response.output_text,
                            "annotations": [],
                        }
                    ],
                }
            )

        for tc in response.tool_calls:
            output_items.append(
                {
                    "type": "function_call",
                    "id": f"fc_{uuid.uuid4().hex[:16]}",
                    "call_id": tc.call_id,
                    "name": tc.name,
                    "arguments": tc.arguments_json,
                    "status": "completed",
                }
            )

        return {
            "id": resp_id,
            "object": "response",
            "created_at": int(time.time()),
            "status": "completed",
            "model": response.model,
            "output": output_items,
            "output_text": response.output_text,
            "usage": {
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
                "total_tokens": response.usage.total_tokens
                or (response.usage.input_tokens + response.usage.output_tokens),
                "output_tokens_details": {
                    "reasoning_tokens": response.usage.reasoning_tokens
                },
            },
        }

    async def format_stream_events(
        self, events: AsyncIterator[CanonicalStreamEvent], model: str
    ) -> AsyncIterator[bytes]:
        resp_id = f"resp_{uuid.uuid4().hex[:24]}"
        created_event = {
            "type": "response.created",
            "response": {"id": resp_id, "object": "response", "status": "in_progress", "model": model},
        }
        yield f"event: response.created\ndata: {json.dumps(created_event)}\n\n".encode("utf-8")

        accumulated_text: list[str] = []
        usage_dict = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

        async for ev in events:
            if ev.event_type == "text_delta" and ev.text_delta is not None:
                accumulated_text.append(ev.text_delta)
                delta_frame = {
                    "type": "response.output_text.delta",
                    "response_id": resp_id,
                    "delta": ev.text_delta,
                }
                yield (
                    f"event: response.output_text.delta\ndata: {json.dumps(delta_frame)}\n\n".encode(
                        "utf-8"
                    )
                )
            elif ev.event_type == "usage" and ev.usage is not None:
                usage_dict = {
                    "input_tokens": ev.usage.input_tokens,
                    "output_tokens": ev.usage.output_tokens,
                    "total_tokens": ev.usage.total_tokens,
                }

        completed_frame = {
            "type": "response.completed",
            "response": {
                "id": resp_id,
                "object": "response",
                "status": "completed",
                "model": model,
                "output_text": "".join(accumulated_text),
                "usage": usage_dict,
            },
        }
        yield f"event: response.completed\ndata: {json.dumps(completed_frame)}\n\n".encode(
            "utf-8"
        )

