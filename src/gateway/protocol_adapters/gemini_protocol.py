"""Google Gemini native protocol adapter (/v1beta/models/{model}:generateContent & :streamGenerateContent)."""

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
    ResponseFormatSpec,
)
from gateway.core.exceptions import UnsupportedCapabilityError
from gateway.protocol_adapters.base import (
    BaseProtocolAdapter,
    resolve_target_provider_and_model,
)


class GeminiNativeProtocolAdapter(BaseProtocolAdapter):
    """Protocol adapter for Google Gemini v1beta generateContent and streamGenerateContent."""

    protocol_type = ProtocolType.GEMINI_GENERATE_CONTENT

    def parse_request(
        self, payload: Mapping[str, Any], **kwargs: Any
    ) -> CanonicalInternalRequest:
        model_raw = str(kwargs.get("model") or payload.get("model") or "").strip()
        if not model_raw:
            raise UnsupportedCapabilityError("Model name is required", param="model")

        stream = bool(kwargs.get("stream", False))
        target_provider, clean_model = resolve_target_provider_and_model(
            model_raw, default_provider="gemini"
        )

        # Reject Gemini-only fields if targeting a non-Gemini provider
        if target_provider != "gemini" and payload.get("cachedContent"):
            raise UnsupportedCapabilityError(
                f"Gemini 'cachedContent' is not supported by provider {target_provider!r}",
                param="cachedContent",
            )

        required_caps: set[CapabilityFlag] = {CapabilityFlag.CHAT_COMPLETIONS}
        if target_provider == "gemini":
            required_caps.add(CapabilityFlag.GEMINI_GENERATE_CONTENT)
        if stream:
            required_caps.add(CapabilityFlag.STREAMING)

        system_instructions: list[str] = []
        sys_inst = payload.get("systemInstruction") or payload.get("system_instruction")
        if isinstance(sys_inst, Mapping):
            required_caps.add(CapabilityFlag.SYSTEM_INSTRUCTIONS)
            for p in sys_inst.get("parts", []):
                if isinstance(p, Mapping) and p.get("text"):
                    system_instructions.append(str(p["text"]))

        raw_contents = payload.get("contents")
        if not isinstance(raw_contents, list) or not raw_contents:
            raise UnsupportedCapabilityError(
                "Field 'contents' must be a non-empty list", param="contents"
            )

        messages: list[CanonicalMessage] = []
        for item in raw_contents:
            if not isinstance(item, Mapping):
                continue
            role_raw = str(item.get("role", "user"))
            role = "assistant" if role_raw == "model" else "user"
            parts: list[CanonicalMessagePart] = []
            for p in item.get("parts") or []:
                if not isinstance(p, Mapping):
                    continue
                if "text" in p:
                    parts.append(
                        CanonicalMessagePart(type="text", text=str(p["text"]))
                    )
                elif "inlineData" in p and isinstance(p["inlineData"], Mapping):
                    required_caps.add(CapabilityFlag.VISION)
                    inl = p["inlineData"]
                    parts.append(
                        CanonicalMessagePart(
                            type="image_base64",
                            media_base64=inl.get("data"),
                            mime_type=inl.get("mimeType", "image/png"),
                        )
                    )
                elif "functionCall" in p and isinstance(p["functionCall"], Mapping):
                    fc = p["functionCall"]
                    args_dict = fc.get("args", {})
                    parts.append(
                        CanonicalMessagePart(
                            type="tool_call",
                            tool_call=NormalizedToolCall(
                                call_id=str(
                                    fc.get("id") or f"call_{uuid.uuid4().hex[:12]}"
                                ),
                                name=str(fc.get("name", "")),
                                arguments_json=json.dumps(args_dict),
                                arguments_dict=(
                                    args_dict if isinstance(args_dict, dict) else {}
                                ),
                            ),
                        )
                    )
                elif "functionResponse" in p and isinstance(
                    p["functionResponse"], Mapping
                ):
                    fr = p["functionResponse"]
                    resp_obj = fr.get("response", {})
                    parts.append(
                        CanonicalMessagePart(
                            type="tool_result",
                            tool_result=NormalizedToolResult(
                                call_id=str(fr.get("id") or fr.get("name") or "tool"),
                                name=str(fr.get("name", "tool")),
                                output_text=(
                                    str(resp_obj.get("result"))
                                    if isinstance(resp_obj, Mapping)
                                    and "result" in resp_obj
                                    else json.dumps(resp_obj)
                                ),
                            ),
                        )
                    )
            messages.append(CanonicalMessage(role=role, parts=parts))  # type: ignore[arg-type]

        tools_out: list[NormalizedToolDefinition] = []
        for t_group in payload.get("tools") or []:
            if not isinstance(t_group, Mapping):
                continue
            required_caps.add(CapabilityFlag.TOOL_CALLING)
            if "functionDeclarations" in t_group:
                for fn in t_group.get("functionDeclarations") or []:
                    if isinstance(fn, Mapping):
                        tools_out.append(
                            NormalizedToolDefinition(
                                name=str(fn.get("name", "")),
                                description=fn.get("description"),
                                parameters_schema=dict(
                                    fn.get(
                                        "parameters",
                                        {"type": "object", "properties": {}},
                                    )
                                ),
                            )
                        )
            else:
                # Provider-native Gemini tool (e.g. googleSearch, codeExecution)
                for native_key in t_group.keys():
                    tools_out.append(
                        NormalizedToolDefinition(
                            name=native_key,
                            is_provider_native=True,
                            native_type=native_key,
                            provider_metadata={
                                "provider_id": "gemini",
                                "raw": dict(t_group),
                            },
                        )
                    )

        gen_cfg = payload.get("generationConfig") or {}
        temperature = gen_cfg.get("temperature") if isinstance(gen_cfg, Mapping) else None
        top_p = gen_cfg.get("topP") if isinstance(gen_cfg, Mapping) else None
        max_tokens = (
            gen_cfg.get("maxOutputTokens") if isinstance(gen_cfg, Mapping) else None
        )
        stop_seq = (
            list(gen_cfg.get("stopSequences") or [])
            if isinstance(gen_cfg, Mapping)
            else []
        )

        response_format_spec: ResponseFormatSpec | None = None
        if isinstance(gen_cfg, Mapping):
            mime = gen_cfg.get("responseMimeType")
            schema = gen_cfg.get("responseSchema")
            if mime == "application/json":
                if schema:
                    required_caps.add(CapabilityFlag.STRUCTURED_OUTPUTS)
                    response_format_spec = ResponseFormatSpec(
                        type="json_schema",
                        json_schema=dict(schema) if isinstance(schema, Mapping) else {},
                    )
                else:
                    required_caps.add(CapabilityFlag.JSON_MODE)
                    response_format_spec = ResponseFormatSpec(type="json_object")
            if gen_cfg.get("thinkingConfig"):
                required_caps.add(CapabilityFlag.REASONING_TOKENS)

        return CanonicalInternalRequest(
            source_protocol=ProtocolType.GEMINI_GENERATE_CONTENT,
            requested_model=model_raw,
            provider_model_id=clean_model,
            system_instructions=system_instructions,
            messages=messages,
            tools=tools_out,
            response_format=response_format_spec,
            temperature=temperature,
            top_p=top_p,
            max_output_tokens=int(max_tokens) if max_tokens is not None else None,
            stop_sequences=stop_seq,
            stream=stream,
            required_capabilities=required_caps,
            provider_extensions={
                k: v
                for k, v in payload.items()
                if k in ("safetySettings", "toolConfig", "generationConfig")
            },
        )

    def format_response(
        self, response: CanonicalInternalResponse
    ) -> dict[str, Any]:
        parts_out: list[dict[str, Any]] = []
        for p in response.output_message.parts:
            if p.type == "text" and p.text is not None:
                parts_out.append({"text": p.text})
            elif p.type == "tool_call" and p.tool_call is not None:
                parts_out.append(
                    {
                        "functionCall": {
                            "name": p.tool_call.name,
                            "args": p.tool_call.arguments_dict,
                        }
                    }
                )
        if not parts_out and response.output_text:
            parts_out.append({"text": response.output_text})

        finish_map = {
            "stop": "STOP",
            "length": "MAX_TOKENS",
            "tool_calls": "STOP",
            "content_filter": "SAFETY",
        }
        return {
            "responseId": response.provider_response_id
            or f"gem-{uuid.uuid4().hex[:16]}",
            "modelVersion": response.model,
            "candidates": [
                {
                    "index": 0,
                    "content": {
                        "role": "model",
                        "parts": parts_out,
                    },
                    "finishReason": finish_map.get(response.finish_reason, "STOP"),
                }
            ],
            "usageMetadata": {
                "promptTokenCount": response.usage.input_tokens,
                "candidatesTokenCount": response.usage.output_tokens,
                "totalTokenCount": response.usage.total_tokens
                or (response.usage.input_tokens + response.usage.output_tokens),
                "thoughtsTokenCount": response.usage.reasoning_tokens,
            },
        }

    async def format_stream_events(
        self, events: AsyncIterator[CanonicalStreamEvent], model: str
    ) -> AsyncIterator[bytes]:
        async for ev in events:
            if ev.event_type == "text_delta" and ev.text_delta is not None:
                frame = {
                    "candidates": [
                        {
                            "index": 0,
                            "content": {
                                "role": "model",
                                "parts": [{"text": ev.text_delta}],
                            },
                        }
                    ],
                    "modelVersion": ev.model or model,
                }
                yield f"data: {json.dumps(frame)}\n\n".encode("utf-8")
            elif ev.event_type == "tool_call_delta" and ev.tool_call_delta is not None:
                frame = {
                    "candidates": [
                        {
                            "index": 0,
                            "content": {
                                "role": "model",
                                "parts": [
                                    {
                                        "functionCall": {
                                            "name": ev.tool_call_delta.name,
                                            "args": ev.tool_call_delta.arguments_dict,
                                        }
                                    }
                                ],
                            },
                        }
                    ],
                    "modelVersion": ev.model or model,
                }
                yield f"data: {json.dumps(frame)}\n\n".encode("utf-8")
            elif ev.event_type == "usage" and ev.usage is not None:
                frame = {
                    "candidates": [
                        {
                            "index": 0,
                            "content": {"role": "model", "parts": []},
                            "finishReason": "STOP",
                        }
                    ],
                    "usageMetadata": {
                        "promptTokenCount": ev.usage.input_tokens,
                        "candidatesTokenCount": ev.usage.output_tokens,
                        "totalTokenCount": ev.usage.total_tokens,
                    },
                }
                yield f"data: {json.dumps(frame)}\n\n".encode("utf-8")

