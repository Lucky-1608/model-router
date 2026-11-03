"""FastAPI router for the Universal Tool Gateway."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response, status

from gateway.core.auth import require_admin_access
from gateway.tool_gateway.models import (
    DirectToolExecutionRequest,
    NormalizedToolCall,
    NormalizedToolResult,
    NormalizedToolSpec,
    ToolRegistrationRequest,
    ToolSourceType,
)
from gateway.tool_gateway.registry import ToolRegistry

tool_router = APIRouter(prefix="/tools", tags=["Universal Tool Gateway"])


def _get_tool_registry(request: Request) -> ToolRegistry:
    return request.app.state.tool_registry


@tool_router.get(
    "",
    summary="List Registered Gateway, Webhook, and MCP Tools",
)
async def list_registered_tools(
    request: Request,
    source_type: ToolSourceType | None = Query(default=None),
    enabled_only: bool = Query(default=False),
) -> dict[str, Any]:
    reg = _get_tool_registry(request)
    tools = reg.list_tools(source_type=source_type, enabled_only=enabled_only)
    return {
        "tools": [t.model_dump() for t in tools],
        "count": len(tools),
    }


@tool_router.post(
    "",
    response_model=NormalizedToolSpec,
    status_code=status.HTTP_201_CREATED,
    summary="Register a New Gateway Webhook or Provider-Native Tool",
)
async def register_gateway_tool(
    request: Request,
    payload: ToolRegistrationRequest,
    _auth: tuple[str, str] = Depends(require_admin_access),
) -> NormalizedToolSpec:
    reg = _get_tool_registry(request)
    return reg.register_tool(payload)


@tool_router.get(
    "/{tool_name}",
    response_model=NormalizedToolSpec,
    summary="Get Tool Specification and JSON Schema",
)
async def get_gateway_tool(
    request: Request,
    tool_name: str,
) -> NormalizedToolSpec:
    reg = _get_tool_registry(request)
    return reg.get_tool(tool_name)


@tool_router.delete(
    "/{tool_name}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Unregister a Tool from the Gateway",
)
async def unregister_gateway_tool(
    request: Request,
    tool_name: str,
    _auth: tuple[str, str] = Depends(require_admin_access),
) -> Response:
    reg = _get_tool_registry(request)
    reg.unregister_tool(tool_name)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@tool_router.post(
    "/{tool_name}/execute",
    response_model=NormalizedToolResult,
    summary="Validate Arguments Against JSON Schema and Execute a Registered Tool",
)
async def execute_gateway_tool(
    request: Request,
    tool_name: str,
    payload: DirectToolExecutionRequest,
) -> NormalizedToolResult:
    reg = _get_tool_registry(request)
    allowed_set = (
        set(payload.allowed_tools) if payload.allowed_tools is not None else None
    )
    call = NormalizedToolCall(
        tool_name=tool_name,
        arguments=payload.arguments,
    )
    return await reg.execute_tool_call(
        call,
        client_id=payload.client_id,
        allowed_tools=allowed_set,
    )

