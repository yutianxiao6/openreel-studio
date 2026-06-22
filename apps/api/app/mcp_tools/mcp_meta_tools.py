"""MCP meta tools — let the Agent inspect and manage external MCP server connections."""
from __future__ import annotations

from typing import Any


async def mcp_list_servers() -> list[dict[str, Any]]:
    """List all connected external MCP servers and their tool counts."""
    from app.mcp_client import mcp_client_manager
    return mcp_client_manager.list_servers()


async def mcp_list_external_tools(server_name: str | None = None) -> list[dict[str, Any]]:
    """List tools from external MCP servers. Optionally filter by server name."""
    from app.mcp_tools.registry import registry
    tools = registry.list_tools(tag="ext")
    if server_name:
        tools = [t for t in tools if server_name in t.tags]
    return [
        {"name": t.name, "description": t.description, "server": t.metadata.get("server", "")}
        for t in tools
    ]


async def mcp_reload_server(server_name: str) -> dict[str, Any]:
    """Disconnect and reconnect a specific external MCP server."""
    from app.mcp_client import mcp_client_manager
    return await mcp_client_manager.reload_server(server_name)
