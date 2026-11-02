"""OpenAI Chat Completions and Embeddings protocol adapter."""

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
from gateway.protocol_adapters.base import BaseProtocolAdapter, resolve_target_provider_and_model

OPENAI_ONLY_EXTENSION_FIELDS = frozenset(
    {"logit_bias", "logprobs", "top_logprobs", "prediction", "audio", "modalities"}
)


class OpenAIChatProtocolAdapter(BaseProtocolAdapter):
    """Protocol adapter for OpenAI /v1/chat/completions and /v1/embeddings."""

    protocol_type = ProtocolType.OPENAI_CHAT

    def parse_request(
        self, payload: Mapping[str, Any], **kwargs: Any
    ) -> CanonicalInternalRequest:
        model_raw = str(payload.get("model") or "").strip()
        if not model_raw:
            raise UnsupportedCapabilityError("Field 'model' is required", param="model")

        target_provider, clean_model = resolve_target_provider_and_model(
            model_raw, default_provider="openai"
        )

        # Never silently strip OpenAI-specific parameters when targeting another provider
        if target_provider != "openai":
            for ext_key in OPENAI_ONLY_EXTENSION_FIELDS:
                if payload.get(ext_key) is not None:
                    raise UnsupportedCapabilityError(
                        f"OpenAI-specific parameter {ext_key!r} is not supported by provider {target_provider!r}",
                        param=ext_key,
                    )
            if int(payload.get("n", 1) or 1) > 1:
                raise UnsupportedCapabilityError(
                    f"Parameter 'n > 1' is not supported by provider {target_provider!r}",
                    param="n",
                )

        required_caps: set[CapabilityFlag] = {CapabilityFlag.CHAT_COMPLETIONS}
        stream = bool(payload.get("stream", False))
        if stream:
            required_caps.add(CapabilityFlag.STREAMING)

        system_instructions: list[str] = []
        messages: list[CanonicalMessage] = []

        raw_messages = payload.get("messages")
        if not isinstance(raw_messages, list) or not raw_messages:
            raise UnsupportedCapabilityError(
                "Field 'messages' must be a non-empty list", param="messages"
            )

        for msg in raw_messages:
            if not isinstance(msg, Mapping):
                continue
            role = str(msg.get("role", "user"))
            content = msg.get("content")
            if role in ("system", "developer") and isinstance(content, str):
                system_instructions.append(content)
                required_caps.add(CapabilityFlag.SYSTEM_INSTRUCTIONS)
                continue

            parts: list[CanonicalMessagePart] = []
            if isinstance(content, str):
                parts.append(CanonicalMessagePart(type="text", text=content))
            elif isinstance(content, list):
                for item in content:
                    if not isinstance(item, Mapping):
                        continue
                    itype = item.get("type")
                    if itype == "text":
                        parts.append(
                            CanonicalMessagePart(
                                type="text", text=str(item.get("text", ""))
                            )
                        )
                    elif itype == "image_url":
                        required_caps.add(CapabilityFlag.VISION)
                        img_obj = item.get("image_url", {})
                        url = (
                            img_obj.get("url")
                            if isinstance(img_obj, Mapping)
                            else str(img_obj)
                        )
                        parts.append(
                            CanonicalMessagePart(type="image_url", media_url=str(url))
                        )
                    elif itype == "input_audio":
                        required_caps.add(CapabilityFlag.AUDIO_INPUT)
                        aud = item.get("input_audio", {})
                        parts.append(
                            CanonicalMessagePart(
                                type="audio",
                                media_base64=aud.get("data"),
                                mime_type=f"audio/{aud.get('format', 'wav')}",
                            )
                        )

            if role == "tool":
                call_id = str(msg.get("tool_call_id", ""))
                output_str = (
                    content
                    if isinstance(content, str)
                    else json.dumps(content or "")
                )
                parts = [
                    CanonicalMessagePart(
                        type="tool_result",
                        tool_result=NormalizedToolResult(
                            call_id=call_id,
                            name=msg.get("name"),
                            output_text=output_str,
                        ),
                    )
                ]

            for tc in msg.get("tool_calls") or []:
                if isinstance(tc, Mapping):
                    fn = tc.get("function", {})
                    args_raw = fn.get("arguments", "{}")
                    try:
                        args_dict = (
                            json.loads(args_raw)
                            if isinstance(args_raw, str)
                            else dict(args_raw)
                        )
                    except ValueError:
                        args_dict = {}
                    parts.append(
                        CanonicalMessagePart(
                            type="tool_call",
                            tool_call=NormalizedToolCall(
                                call_id=str(tc.get("id", "")),
                                name=str(fn.get("name", "")),
                                arguments_json=(
                                    args_raw
                                    if isinstance(args_raw, str)
                                    else json.dumps(args_raw)
                                ),
                                arguments_dict=args_dict,
                            ),
                        )
                    )

            valid_role = (
                role
                if role in ("system", "developer", "user", "assistant", "tool")
                else "user"
            )
            messages.append(
                CanonicalMessage(role=valid_role, parts=parts, name=msg.get("name"))  # type: ignore[arg-type]
            )

        tools_out: list[NormalizedToolDefinition] = []
        for t in payload.get("tools") or []:
            if not isinstance(t, Mapping):
                continue
            required_caps.add(CapabilityFlag.TOOL_CALLING)
            if t.get("type") == "function" and isinstance(t.get("function"), Mapping):
                fn = t["function"]
                tools_out.append(
                    NormalizedToolDefinition(
                        name=str(fn.get("name", "")),
                        description=fn.get("description"),
                        parameters_schema=dict(
                            fn.get(
                                "parameters", {"type": "object", "properties": {}}
                            )
                        ),
                        strict=bool(fn.get("strict", False)),
                    )
                )
            else:
                # Provider-native tool
                t_type = str(t.get("type", "unknown"))
                tools_out.append(
                    NormalizedToolDefinition(
                        name=t_type,
                        is_provider_native=True,
                        native_type=t_type,
                        provider_metadata={"provider_id": "openai", "raw": dict(t)},
                    )
                )

        parallel_tool_calls = payload.get("parallel_tool_calls")
        if parallel_tool_calls is True:
            required_caps.add(CapabilityFlag.PARALLEL_TOOL_CALLS)

        response_format_spec: ResponseFormatSpec | None = None
        rf = payload.get("response_format")
        if isinstance(rf, Mapping):
            rf_type = str(rf.get("type", "text"))
            if rf_type == "json_object":
                required_caps.add(CapabilityFlag.JSON_MODE)
                response_format_spec = ResponseFormatSpec(type="json_object")
            elif rf_type == "json_schema":
                required_caps.add(CapabilityFlag.STRUCTURED_OUTPUTS)
                js_obj = rf.get("json_schema", {})
                response_format_spec = ResponseFormatSpec(
                    type="json_schema",
                    schema_name=js_obj.get("name"),
                    json_schema=js_obj.get("schema"),
                    strict=bool(js_obj.get("strict", False)),
                )

        if payload.get("reasoning_effort") is not None:
            required_caps.add(CapabilityFlag.REASONING_TOKENS)

        stop_raw = payload.get("stop")
        stop_seq = (
            [stop_raw]
            if isinstance(stop_raw, str)
            else (list(stop_raw) if isinstance(stop_raw, list) else [])
        )

        max_tok = payload.get("max_completion_tokens") or payload.get("max_tokens")
        return CanonicalInternalRequest(
            source_protocol=ProtocolType.OPENAI_CHAT,
            requested_model=model_raw,
            provider_model_id=clean_model,
            system_instructions=system_instructions,
            messages=messages,
            tools=tools_out,
            tool_choice=payload.get("tool_choice"),
            parallel_tool_calls=parallel_tool_calls,
            response_format=response_format_spec,
            temperature=payload.get("temperature"),
            top_p=payload.get("top_p"),
            max_output_tokens=int(max_tok) if max_tok is not None else None,
            stop_sequences=stop_seq,
            stream=stream,
            required_capabilities=required_caps,
            provider_extensions={
                k: v
                for k, v in payload.items()
                if k in ("provider", "transforms", "reasoning_effort")
            },
        )

    def parse_embeddings_request(
        self, payload: Mapping[str, Any]
    ) -> CanonicalInternalRequest:
        model_raw = str(payload.get("model") or "").strip()
        if not model_raw:
            raise UnsupportedCapabilityError("Field 'model' is required", param="model")
        _, clean_model = resolve_target_provider_and_model(
            model_raw, default_provider="openai"
        )
        raw_input = payload.get("input")
        if isinstance(raw_input, str):
            inputs = [raw_input]
        elif isinstance(raw_input, list) and all(isinstance(x, str) for x in raw_input):
            inputs = list(raw_input)
        else:
            raise UnsupportedCapabilityError(
                "Field 'input' must be a string or list of strings", param="input"
            )
        return CanonicalInternalRequest(
            source_protocol=ProtocolType.OPENAI_EMBEDDINGS,
            requested_model=model_raw,
            provider_model_id=clean_model,
            input_texts=inputs,
            required_capabilities={CapabilityFlag.EMBEDDINGS},
        )

    def format_response(
        self, response: CanonicalInternalResponse
    ) -> dict[str, Any]:
        if response.embeddings:
            return {
                "object": "list",
                "model": response.model,
                "data": [
                    {
                        "object": "embedding",
                        "index": idx,
                        "embedding": vec,
                    }
                    for idx, vec in enumerate(response.embeddings)
                ],
                "usage": {
                    "prompt_tokens": response.usage.input_tokens,
                    "total_tokens": response.usage.total_tokens
                    or response.usage.input_tokens,
                },
            }

        message_dict: dict[str, Any] = {
            "role": "assistant",
            "content": response.output_text or None,
        }
        if response.tool_calls:
            message_dict["tool_calls"] = [
                {
                    "id": tc.call_id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": tc.arguments_json,
                    },
                }
                for tc in response.tool_calls
            ]

        return {
            "id": response.provider_response_id or f"chatcmpl-{uuid.uuid4().hex[:16]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": response.model,
            "choices": [
                {
                    "index": 0,
                    "message": message_dict,
                    "finish_reason": response.finish_reason,
                }
            ],
            "usage": {
                "prompt_tokens": response.usage.input_tokens,
                "completion_tokens": response.usage.output_tokens,
                "total_tokens": response.usage.total_tokens
                or (response.usage.input_tokens + response.usage.output_tokens),
            },
        }

    async def format_stream_events(
        self, events: AsyncIterator[CanonicalStreamEvent], model: str
    ) -> AsyncIterator[bytes]:
        chunk_id = f"chatcmpl-{uuid.uuid4().hex[:16]}"
        created = int(time.time())

        async for ev in events:
            if ev.provider_response_id:
                chunk_id = ev.provider_response_id
            active_model = ev.model or model

            if ev.event_type == "text_delta" and ev.text_delta is not None:
                frame = {
                    "id": chunk_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": active_model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": ev.text_delta},
                            "finish_reason": None,
                        }
                    ],
                }
                yield f"data: {json.dumps(frame)}\n\n".encode("utf-8")
            elif ev.event_type == "tool_call_delta" and ev.tool_call_delta is not None:
                tc = ev.tool_call_delta
                frame = {
                    "id": chunk_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": active_model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": tc.call_id or None,
                                        "type": "function",
                                        "function": {
                                            "name": tc.name or None,
                                            "arguments": tc.arguments_json,
                                        },
                                    }
                                ]
                            },
                            "finish_reason": None,
                        }
                    ],
                }
                yield f"data: {json.dumps(frame)}\n\n".encode("utf-8")
            elif ev.event_type == "usage" and ev.usage is not None:
                frame = {
                    "id": chunk_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": active_model,
                    "choices": [],
                    "usage": {
                        "prompt_tokens": ev.usage.input_tokens,
                        "completion_tokens": ev.usage.output_tokens,
                        "total_tokens": ev.usage.total_tokens,
                    },
                }
                yield f"data: {json.dumps(frame)}\n\n".encode("utf-8")
            elif ev.event_type == "done":
                if ev.finish_reason:
                    frame = {
                        "id": chunk_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": active_model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {},
                                "finish_reason": ev.finish_reason,
                            }
                        ],
                    }
                    yield f"data: {json.dumps(frame)}\n\n".encode("utf-8")
                yield b"data: [DONE]\n\n"
                return

        yield b"data: [DONE]\n\n"

