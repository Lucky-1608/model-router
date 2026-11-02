"""Anthropic Messages API protocol adapter (/v1/messages)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
import json
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
)
from gateway.core.exceptions import UnsupportedCapabilityError
from gateway.protocol_adapters.base import (
    BaseProtocolAdapter,
    resolve_target_provider_and_model,
)


class AnthropicMessagesProtocolAdapter(BaseProtocolAdapter):
    """Protocol adapter for Anthropic /v1/messages preserving system, tool_use, and SSE event semantics."""

    protocol_type = ProtocolType.ANTHROPIC_MESSAGES

    def parse_request(
        self, payload: Mapping[str, Any], **kwargs: Any
    ) -> CanonicalInternalRequest:
        model_raw = str(payload.get("model") or "").strip()
        if not model_raw:
            raise UnsupportedCapabilityError("Field 'model' is required", param="model")

        max_tokens = payload.get("max_tokens")
        if max_tokens is None:
            raise UnsupportedCapabilityError(
                "Anthropic Messages API requires 'max_tokens'", param="max_tokens"
            )

        target_provider, clean_model = resolve_target_provider_and_model(
            model_raw, default_provider="anthropic"
        )

        required_caps: set[CapabilityFlag] = {CapabilityFlag.CHAT_COMPLETIONS}
        if target_provider == "anthropic":
            required_caps.add(CapabilityFlag.MESSAGES_API)

        stream = bool(payload.get("stream", False))
        if stream:
            required_caps.add(CapabilityFlag.STREAMING)

        # System instructions (string or list of blocks)
        system_instructions: list[str] = []
        has_cache_control = False
        raw_sys = payload.get("system")
        if isinstance(raw_sys, str) and raw_sys:
            system_instructions.append(raw_sys)
            required_caps.add(CapabilityFlag.SYSTEM_INSTRUCTIONS)
        elif isinstance(raw_sys, list):
            required_caps.add(CapabilityFlag.SYSTEM_INSTRUCTIONS)
            for block in raw_sys:
                if isinstance(block, Mapping):
                    if block.get("cache_control"):
                        has_cache_control = True
                    if block.get("text"):
                        system_instructions.append(str(block["text"]))

        raw_messages = payload.get("messages")
        if not isinstance(raw_messages, list) or not raw_messages:
            raise UnsupportedCapabilityError(
                "Field 'messages' must be a non-empty list", param="messages"
            )

        messages: list[CanonicalMessage] = []
        for msg in raw_messages:
            if not isinstance(msg, Mapping):
                continue
            role = "assistant" if msg.get("role") == "assistant" else "user"
            content = msg.get("content")
            parts: list[CanonicalMessagePart] = []
            if isinstance(content, str):
                parts.append(CanonicalMessagePart(type="text", text=content))
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, Mapping):
                        continue
                    if block.get("cache_control"):
                        has_cache_control = True
                    btype = block.get("type")
                    if btype == "text":
                        parts.append(
                            CanonicalMessagePart(
                                type="text", text=str(block.get("text", ""))
                            )
                        )
                    elif btype == "thinking":
                        required_caps.add(CapabilityFlag.REASONING_TOKENS)
                        parts.append(
                            CanonicalMessagePart(
                                type="thinking",
                                text=str(block.get("thinking", "")),
                                provider_metadata={"signature": block.get("signature")},
                            )
                        )
                    elif btype == "image":
                        required_caps.add(CapabilityFlag.VISION)
                        src = block.get("source", {})
                        parts.append(
                            CanonicalMessagePart(
                                type="image_base64",
                                media_base64=src.get("data"),
                                mime_type=src.get("media_type", "image/png"),
                            )
                        )
                    elif btype == "tool_use":
                        inp = block.get("input", {})
                        parts.append(
                            CanonicalMessagePart(
                                type="tool_call",
                                tool_call=NormalizedToolCall(
                                    call_id=str(block.get("id", "")),
                                    name=str(block.get("name", "")),
                                    arguments_json=json.dumps(inp),
                                    arguments_dict=inp if isinstance(inp, dict) else {},
                                ),
                            )
                        )
                    elif btype == "tool_result":
                        parts.append(
                            CanonicalMessagePart(
                                type="tool_result",
                                tool_result=NormalizedToolResult(
                                    call_id=str(block.get("tool_use_id", "")),
                                    output_text=str(block.get("content", "")),
                                    is_error=bool(block.get("is_error", False)),
                                ),
                            )
                        )
            messages.append(CanonicalMessage(role=role, parts=parts))  # type: ignore[arg-type]

        if has_cache_control and target_provider != "anthropic":
            raise UnsupportedCapabilityError(
                f"Anthropic 'cache_control' prompt caching blocks cannot be translated to provider {target_provider!r}",
                param="cache_control",
            )

        tools_out: list[NormalizedToolDefinition] = []
        for t in payload.get("tools") or []:
            if not isinstance(t, Mapping):
                continue
            required_caps.add(CapabilityFlag.TOOL_CALLING)
            if t.get("type") and t.get("type") != "custom":
                # Anthropic server-side / computer-use / bash native tool
                t_type = str(t["type"])
                tools_out.append(
                    NormalizedToolDefinition(
                        name=str(t.get("name", t_type)),
                        is_provider_native=True,
                        native_type=t_type,
                        provider_metadata={"provider_id": "anthropic", "raw": dict(t)},
                    )
                )
            else:
                tools_out.append(
                    NormalizedToolDefinition(
                        name=str(t.get("name", "")),
                        description=t.get("description"),
                        parameters_schema=dict(
                            t.get("input_schema", {"type": "object", "properties": {}})
                        ),
                    )
                )

        provider_ext: dict[str, Any] = {}
        thinking_cfg = payload.get("thinking")
        if isinstance(thinking_cfg, Mapping) and thinking_cfg.get("type") == "enabled":
            required_caps.add(CapabilityFlag.REASONING_TOKENS)
            provider_ext["thinking"] = dict(thinking_cfg)

        return CanonicalInternalRequest(
            source_protocol=ProtocolType.ANTHROPIC_MESSAGES,
            requested_model=model_raw,
            provider_model_id=clean_model,
            system_instructions=system_instructions,
            messages=messages,
            tools=tools_out,
            tool_choice=payload.get("tool_choice"),
            temperature=payload.get("temperature"),
            top_p=payload.get("top_p"),
            max_output_tokens=int(max_tokens),
            stop_sequences=list(payload.get("stop_sequences") or []),
            stream=stream,
            required_capabilities=required_caps,
            provider_extensions=provider_ext,
        )

    def format_response(
        self, response: CanonicalInternalResponse
    ) -> dict[str, Any]:
        content_blocks: list[dict[str, Any]] = []
        for part in response.output_message.parts:
            if part.type == "thinking" and part.text is not None:
                tb: dict[str, Any] = {"type": "thinking", "thinking": part.text}
                if part.provider_metadata.get("signature"):
                    tb["signature"] = part.provider_metadata["signature"]
                content_blocks.append(tb)
            elif part.type == "text" and part.text is not None:
                content_blocks.append({"type": "text", "text": part.text})
            elif part.type == "tool_call" and part.tool_call is not None:
                content_blocks.append(
                    {
                        "type": "tool_use",
                        "id": part.tool_call.call_id,
                        "name": part.tool_call.name,
                        "input": part.tool_call.arguments_dict,
                    }
                )

        if not content_blocks and response.output_text:
            content_blocks.append({"type": "text", "text": response.output_text})

        stop_reason_map = {
            "stop": "end_turn",
            "length": "max_tokens",
            "tool_calls": "tool_use",
        }
        return {
            "id": response.provider_response_id or f"msg_{uuid.uuid4().hex[:20]}",
            "type": "message",
            "role": "assistant",
            "model": response.model,
            "content": content_blocks,
            "stop_reason": stop_reason_map.get(response.finish_reason, "end_turn"),
            "stop_sequence": None,
            "usage": {
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
                "cache_read_input_tokens": response.usage.cached_input_tokens,
            },
        }

    async def format_stream_events(
        self, events: AsyncIterator[CanonicalStreamEvent], model: str
    ) -> AsyncIterator[bytes]:
        msg_id = f"msg_{uuid.uuid4().hex[:20]}"
        started = False
        block_started = False
        output_tokens = 0

        async for ev in events:
            if not started:
                if ev.provider_response_id:
                    msg_id = ev.provider_response_id
                start_payload = {
                    "type": "message_start",
                    "message": {
                        "id": msg_id,
                        "type": "message",
                        "role": "assistant",
                        "model": ev.model or model,
                        "content": [],
                        "stop_reason": None,
                        "usage": {"input_tokens": 0, "output_tokens": 0},
                    },
                }
                yield f"event: message_start\ndata: {json.dumps(start_payload)}\n\n".encode(
                    "utf-8"
                )
                started = True

            if ev.event_type == "text_delta" and ev.text_delta is not None:
                if not block_started:
                    cb_start = {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {"type": "text", "text": ""},
                    }
                    yield (
                        f"event: content_block_start\ndata: {json.dumps(cb_start)}\n\n".encode(
                            "utf-8"
                        )
                    )
                    block_started = True
                cb_delta = {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": ev.text_delta},
                }
                yield f"event: content_block_delta\ndata: {json.dumps(cb_delta)}\n\n".encode(
                    "utf-8"
                )
            elif ev.event_type == "thinking_delta" and ev.thinking_delta is not None:
                cb_delta = {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "thinking_delta", "thinking": ev.thinking_delta},
                }
                yield f"event: content_block_delta\ndata: {json.dumps(cb_delta)}\n\n".encode(
                    "utf-8"
                )
            elif ev.event_type == "usage" and ev.usage is not None:
                output_tokens = ev.usage.output_tokens

        if block_started:
            cb_stop = {"type": "content_block_stop", "index": 0}
            yield f"event: content_block_stop\ndata: {json.dumps(cb_stop)}\n\n".encode(
                "utf-8"
            )

        msg_delta = {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": output_tokens},
        }
        yield f"event: message_delta\ndata: {json.dumps(msg_delta)}\n\n".encode("utf-8")
        yield b'event: message_stop\ndata: {"type": "message_stop"}\n\n'

