"""FastAPI router for Model Context Protocol (MCP) Server Integration."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request, Response, status

from gateway.core.auth import require_admin_access
from gateway.mcp_integration.manager import MCPClientManager
from gateway.mcp_integration.models import (
    MCPServerRegistrationRequest,
    MCPServerStatus,
    MCPToolCallRequest,
)
from gateway.tool_gateway.models import NormalizedToolResult

mcp_router = APIRouter(prefix="/mcp", tags=["Model Context Protocol (MCP)"])


def _get_mcp_manager(request: Request) -> MCPClientManager:
    return request.app.state.mcp_manager


@mcp_router.get(
    "/servers",
    response_model=list[MCPServerStatus],
    summary="List Registered MCP Servers and Discovery Status",
)
async def list_mcp_servers(request: Request) -> list[MCPServerStatus]:
    mgr = _get_mcp_manager(request)
    return mgr.list_servers()


@mcp_router.post(
    "/servers",
    response_model=MCPServerStatus,
    status_code=status.HTTP_201_CREATED,
    summary="Register or Update an MCP Server",
)
async def register_mcp_server(
    request: Request,
    payload: MCPServerRegistrationRequest,
    _auth: tuple[str, str] = Depends(require_admin_access),
) -> MCPServerStatus:
    mgr = _get_mcp_manager(request)
    return mgr.register_server(payload)


@mcp_router.get(
    "/servers/{server_id}",
    response_model=MCPServerStatus,
    summary="Get MCP Server Status and Discovered Tool List",
)
async def get_mcp_server(
    request: Request,
    server_id: str,
) -> MCPServerStatus:
    mgr = _get_mcp_manager(request)
    return mgr.get_server_status(server_id)


@mcp_router.delete(
    "/servers/{server_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Unregister an MCP Server and Remove Its Synced Tools",
)
async def unregister_mcp_server(
    request: Request,
    server_id: str,
    _auth: tuple[str, str] = Depends(require_admin_access),
) -> Response:
    mgr = _get_mcp_manager(request)
    mgr.unregister_server(server_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@mcp_router.post(
    "/servers/{server_id}/health",
    response_model=MCPServerStatus,
    summary="Probe MCP Server Connectivity and Protocol Version",
)
async def check_mcp_server_health(
    request: Request,
    server_id: str,
) -> MCPServerStatus:
    mgr = _get_mcp_manager(request)
    return await mgr.check_server_health(server_id)


@mcp_router.post(
    "/servers/{server_id}/sync-tools",
    summary="Discover Tools from MCP Server and Sync into Gateway Tool Registry",
)
async def sync_mcp_server_tools(
    request: Request,
    server_id: str,
    _auth: tuple[str, str] = Depends(require_admin_access),
) -> dict[str, Any]:
    mgr = _get_mcp_manager(request)
    descriptors = await mgr.sync_server_tools(server_id)
    return {
        "server_id": server_id,
        "synced_count": len(descriptors),
        "tools": [d.model_dump() for d in descriptors],
    }


@mcp_router.post(
    "/servers/{server_id}/call",
    response_model=NormalizedToolResult,
    summary="Invoke a Tool on an MCP Server with Schema and Permission Validation",
)
async def call_mcp_server_tool(
    request: Request,
    server_id: str,
    payload: MCPToolCallRequest,
) -> NormalizedToolResult:
    mgr = _get_mcp_manager(request)
    return await mgr.call_mcp_tool(
        server_id=server_id,
        tool_name=payload.tool_name,
        arguments=payload.arguments,
    )

