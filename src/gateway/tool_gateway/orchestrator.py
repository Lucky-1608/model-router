"""Multi-turn Universal Tool Gateway Orchestrator."""

from __future__ import annotations

import asyncio
from typing import Any

from gateway.capability_registry.models import CapabilityFlag
from gateway.core.canonical import (
    AdapterCredential,
    CanonicalInternalRequest,
    CanonicalInternalResponse,
    CanonicalMessage,
    CanonicalMessagePart,
    NormalizedToolResult as CanonicalToolResult,
    TokenUsage,
)
from gateway.provider_adapters.base import BaseProviderAdapter
from gateway.tool_gateway.models import (
    NormalizedToolCall,
    NormalizedToolResult,
    ToolExecutionMode,
    ToolExecutionStepTrace,
)
from gateway.tool_gateway.registry import ToolRegistry
from gateway.tool_gateway.validator import (
    ToolExecutionError,
    ToolInputValidationError,
    ToolPermissionError,
)


class ToolGatewayOrchestrator:
    """Orchestrates tool portability checks, schema validation, and multi-round tool loops."""

    def __init__(
        self,
        tool_registry: ToolRegistry,
        *,
        default_max_tool_rounds: int = 5,
    ) -> None:
        self._tool_registry = tool_registry
        self._default_max_tool_rounds = default_max_tool_rounds

    @property
    def tool_registry(self) -> ToolRegistry:
        return self._tool_registry

    def prepare_request_tools(
        self,
        request: CanonicalInternalRequest,
        *,
        target_provider: str,
        mode: ToolExecutionMode = ToolExecutionMode.PASSTHROUGH,
        allowed_tools: set[str] | None = None,
        inject_gateway_tools: bool = False,
    ) -> None:
        """Validates provider-native tool portability and optionally injects Gateway/MCP tools."""
        if mode == ToolExecutionMode.GATEWAY_MANAGED and inject_gateway_tools:
            existing_names = {t.name for t in request.tools}
            for spec in self._tool_registry.list_tools(enabled_only=True):
                if allowed_tools is not None and spec.name not in allowed_tools:
                    continue
                if spec.name not in existing_names:
                    defs = self._tool_registry.to_canonical_tool_definitions([spec.name])
                    request.tools.extend(defs)
                    existing_names.add(spec.name)
            if request.tools:
                request.required_capabilities.add(CapabilityFlag.TOOL_CALLING)

        self._tool_registry.enforce_provider_native_portability(
            request.tools, target_provider
        )

    async def execute_with_tool_orchestration(
        self,
        *,
        adapter: BaseProviderAdapter,
        request: CanonicalInternalRequest,
        credential: AdapterCredential | None,
        mode: ToolExecutionMode = ToolExecutionMode.PASSTHROUGH,
        client_id: str | None = None,
        allowed_tools: set[str] | None = None,
        max_tool_rounds: int | None = None,
    ) -> CanonicalInternalResponse:
        """Executes a request and, if mode == GATEWAY_MANAGED, runs the multi-turn tool loop."""
        self.prepare_request_tools(
            request,
            target_provider=adapter.provider_id,
            mode=mode,
            allowed_tools=allowed_tools,
            inject_gateway_tools=(mode == ToolExecutionMode.GATEWAY_MANAGED and not request.tools),
        )

        response = await adapter.execute(request, credential)
        if mode != ToolExecutionMode.GATEWAY_MANAGED or not response.tool_calls:
            return response

        rounds_limit = max_tool_rounds or self._default_max_tool_rounds
        accumulated_usage = TokenUsage(
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            total_tokens=response.usage.total_tokens,
            cached_input_tokens=response.usage.cached_input_tokens,
            reasoning_tokens=response.usage.reasoning_tokens,
            characters=response.usage.characters,
        )
        # Track whether any executed tool call is non-idempotent
        if request.provider_extensions is None:
            request.provider_extensions = {}
        for tc in response.tool_calls:
            spec = self._tool_registry.get_tool(tc.name)
            if spec and not getattr(spec, "idempotent", True):
                request.provider_extensions["non_idempotent_tool_executed"] = True

        traces: list[ToolExecutionStepTrace] = []
        round_idx = 0

        while response.tool_calls and round_idx < rounds_limit:
            # Only intercept if every requested tool call is a managed Gateway/MCP tool.
            # If the model called a client-defined passthrough function not registered in ToolRegistry,
            # return the response to the client so the client can execute its own function.
            if not all(
                self._tool_registry.has_managed_tool(tc.name)
                for tc in response.tool_calls
            ):
                break

            round_idx += 1

            # Append the assistant's tool_call message to the conversation history
            if response.output_message and any(
                p.type == "tool_call" for p in response.output_message.parts
            ):
                request.messages.append(response.output_message)
            else:
                assistant_parts = list(response.output_message.parts)
                for tc in response.tool_calls:
                    assistant_parts.append(
                        CanonicalMessagePart(type="tool_call", tool_call=tc)
                    )
                request.messages.append(
                    CanonicalMessage(role="assistant", parts=assistant_parts)
                )

            # Execute all tool calls in this round
            results = await self._execute_round_calls(
                round_index=round_idx,
                canonical_calls=response.tool_calls,
                parallel=(request.parallel_tool_calls is not False),
                client_id=client_id,
                allowed_tools=allowed_tools,
                traces=traces,
            )

            # Append tool results to conversation history
            tool_result_parts = [
                CanonicalMessagePart(
                    type="tool_result",
                    tool_result=CanonicalToolResult(
                        call_id=res.call_id,
                        name=res.tool_name,
                        output_text=res.serialized_output(),
                        is_error=res.is_error,
                    ),
                )
                for res in results
            ]
            request.messages.append(
                CanonicalMessage(role="tool", parts=tool_result_parts)
            )

            # Call the model for the next turn
            response = await adapter.execute(request, credential)
            accumulated_usage.input_tokens += response.usage.input_tokens
            accumulated_usage.output_tokens += response.usage.output_tokens
            accumulated_usage.total_tokens += response.usage.total_tokens
            accumulated_usage.cached_input_tokens += response.usage.cached_input_tokens
            accumulated_usage.reasoning_tokens += response.usage.reasoning_tokens

        response.usage = accumulated_usage
        if traces:
            response.provider_metadata["gateway_tool_rounds"] = round_idx
            response.provider_metadata["gateway_tool_traces"] = [
                t.model_dump() for t in traces
            ]
        return response

    async def _execute_round_calls(
        self,
        *,
        round_index: int,
        canonical_calls: list[Any],
        parallel: bool,
        client_id: str | None,
        allowed_tools: set[str] | None,
        traces: list[ToolExecutionStepTrace],
    ) -> list[NormalizedToolResult]:
        async def _run_one(tc: Any) -> NormalizedToolResult:
            norm_call = NormalizedToolCall(
                call_id=tc.call_id,
                tool_name=tc.name,
                arguments=tc.arguments_dict,
                raw_arguments=tc.arguments_json,
            )
            spec = self._tool_registry.get_tool(tc.name)
            try:
                res = await self._tool_registry.execute_tool_call(
                    norm_call,
                    client_id=client_id,
                    allowed_tools=allowed_tools,
                )
                traces.append(
                    ToolExecutionStepTrace(
                        round_index=round_index,
                        call_id=tc.call_id,
                        tool_name=tc.name,
                        source_type=spec.source_type,
                        is_error=res.is_error,
                        execution_time_ms=res.execution_time_ms,
                    )
                )
                return res
            except (ToolPermissionError, ToolInputValidationError):
                # Security / schema errors must fail immediately and never be swallowed silently
                raise
            except ToolExecutionError as exc:
                traces.append(
                    ToolExecutionStepTrace(
                        round_index=round_index,
                        call_id=tc.call_id,
                        tool_name=tc.name,
                        source_type=spec.source_type,
                        is_error=True,
                        execution_time_ms=float(
                            exc.details.get("execution_time_ms", 0.0)
                        ),
                        error_message=exc.message,
                    )
                )
                return NormalizedToolResult(
                    call_id=tc.call_id,
                    tool_name=tc.name,
                    output={"error": exc.message},
                    is_error=True,
                    source_type=spec.source_type,
                )

        if parallel and len(canonical_calls) > 1:
            return list(await asyncio.gather(*(_run_one(tc) for tc in canonical_calls)))

        results: list[NormalizedToolResult] = []
        for tc in canonical_calls:
            results.append(await _run_one(tc))
        return results

