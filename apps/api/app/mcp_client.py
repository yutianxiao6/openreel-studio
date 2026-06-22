"""MCP Client Manager — connects to external MCP servers and registers their tools.

On startup, reads mcp_servers.json (or env MCP_SERVERS_CONFIG), launches each
configured server process (stdio transport), fetches its tool list, and registers
proxy handlers into the shared registry under the `ext.<server_name>.<tool>` namespace.

This is what makes the Agent behave like Claude Code: it can call tools from
arbitrary external MCP servers (filesystem, brave-search, databases, etc.)
without any code changes — just add a config entry.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from app.mcp_tools.registry import registry

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "mcp_servers.json"


@dataclass
class McpServerEntry:
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    enabled: bool = True


@dataclass
class ConnectedServer:
    entry: McpServerEntry
    session: ClientSession
    tools: list[str] = field(default_factory=list)


class McpClientManager:
    """Manages connections to external MCP servers."""

    def __init__(self, config_path: str | Path | None = None):
        self.config_path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
        self._servers: dict[str, ConnectedServer] = {}
        self._cleanup_tasks: list[Any] = []

    def load_config(self) -> list[McpServerEntry]:
        if not self.config_path.exists():
            return []
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to read MCP servers config: %s", exc)
            return []

        entries: list[McpServerEntry] = []
        servers = data if isinstance(data, dict) else {}
        for name, cfg in servers.items():
            if not isinstance(cfg, dict):
                continue
            entries.append(McpServerEntry(
                name=name,
                command=cfg.get("command", ""),
                args=cfg.get("args", []),
                env=cfg.get("env", {}),
                enabled=cfg.get("enabled", True),
            ))
        return entries

    async def connect_all(self) -> list[str]:
        """Connect to all configured servers. Returns list of connected names."""
        entries = self.load_config()
        connected: list[str] = []
        for entry in entries:
            if not entry.enabled or not entry.command:
                continue
            try:
                await self._connect_one(entry)
                connected.append(entry.name)
            except Exception as exc:
                logger.warning("MCP server '%s' failed to connect: %s", entry.name, exc)
        return connected

    async def _connect_one(self, entry: McpServerEntry) -> None:
        """Connect to a single MCP server and register its tools."""
        params = StdioServerParameters(
            command=entry.command,
            args=entry.args,
            env=entry.env if entry.env else None,
        )

        # stdio_client is an async context manager — we need to keep it alive
        # for the lifetime of the application. We store the cleanup coroutine.
        read_stream, write_stream = None, None

        # Use a background task to maintain the connection
        ctx = stdio_client(params)
        streams = await ctx.__aenter__()
        read_stream, write_stream = streams

        session = ClientSession(read_stream, write_stream)
        await session.initialize()

        # Fetch tools
        tools_result = await session.list_tools()
        tool_names: list[str] = []

        for tool in tools_result.tools:
            full_name = f"ext.{entry.name}.{tool.name}"
            # Create a closure that captures session + tool.name
            handler = self._make_proxy_handler(session, tool.name)
            registry.register(
                full_name,
                handler,
                description=tool.description or f"[ext:{entry.name}] {tool.name}",
                schema=tool.inputSchema if isinstance(tool.inputSchema, dict) else {},
                tags=["ext", entry.name],
                metadata={"source": "mcp_external", "server": entry.name},
                replace=True,
            )
            tool_names.append(full_name)

        self._servers[entry.name] = ConnectedServer(
            entry=entry, session=session, tools=tool_names
        )
        self._cleanup_tasks.append(ctx)
        logger.info(
            "MCP server '%s' connected: %d tools registered", entry.name, len(tool_names)
        )

    @staticmethod
    def _make_proxy_handler(session: ClientSession, tool_name: str):
        async def _proxy(**kwargs) -> dict[str, Any]:
            result = await session.call_tool(tool_name, arguments=kwargs)
            # MCP returns list[Content]; flatten to dict
            texts = [c.text for c in result.content if hasattr(c, "text")]
            combined = "\n".join(texts)
            try:
                return json.loads(combined)
            except (json.JSONDecodeError, TypeError):
                return {"text": combined}
        _proxy.__name__ = f"ext_proxy_{tool_name}"
        _proxy.__doc__ = f"Proxy call to external MCP server tool: {tool_name}"
        return _proxy

    def list_servers(self) -> list[dict[str, Any]]:
        result = []
        for name, conn in self._servers.items():
            result.append({
                "name": name,
                "command": conn.entry.command,
                "tool_count": len(conn.tools),
                "tools": conn.tools,
            })
        return result

    async def disconnect_all(self) -> None:
        """Cleanup: close all server connections."""
        for ctx in self._cleanup_tasks:
            try:
                await ctx.__aexit__(None, None, None)
            except Exception:
                pass
        # Unregister ext.* tools
        to_remove = [n for n in registry._tools if n.startswith("ext.")]
        for n in to_remove:
            registry.unregister(n)
        self._servers.clear()
        self._cleanup_tasks.clear()

    async def reload_server(self, name: str) -> dict[str, Any]:
        """Disconnect and reconnect a single server."""
        if name in self._servers:
            conn = self._servers.pop(name)
            for tool_name in conn.tools:
                registry.unregister(tool_name)
        entries = self.load_config()
        entry = next((e for e in entries if e.name == name), None)
        if not entry:
            return {"error": f"Server '{name}' not found in config"}
        try:
            await self._connect_one(entry)
            return {"status": "connected", "tools": self._servers[name].tools}
        except Exception as exc:
            return {"error": str(exc)}


# Singleton instance
mcp_client_manager = McpClientManager()
