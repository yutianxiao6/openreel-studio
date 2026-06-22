"""MCP Server — exposes all registry tools via Model Context Protocol (stdio transport).

Usage:
    uv run python -m app.mcp_server          # stdio mode (for Claude Desktop / IDE)
    # Or via the script entry point:
    drama-mcp-server

This lets any MCP client (Claude Desktop, Cursor, etc.) call the registered
tool surface with full JSON schema validation.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
from typing import Any, get_args, get_origin

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema generation from Python function signatures
# ---------------------------------------------------------------------------

_PY_TO_JSON_TYPE = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


def _annotation_to_json_schema(annotation) -> dict[str, Any]:
    """Convert a Python type annotation to a JSON Schema fragment."""
    if annotation is inspect.Parameter.empty or annotation is Any:
        return {}

    origin = get_origin(annotation)
    args = get_args(annotation)

    # Optional[X] = Union[X, None]
    if origin is type(None):
        return {"type": "null"}

    # Handle Union (including Optional)
    import types
    if origin is types.UnionType or (hasattr(origin, "__origin__") and str(origin) == "typing.Union"):
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            return _annotation_to_json_schema(non_none[0])
        return {"anyOf": [_annotation_to_json_schema(a) for a in non_none]}

    # X | None (Python 3.10+ union syntax)
    if isinstance(annotation, types.UnionType):
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            return _annotation_to_json_schema(non_none[0])
        return {"anyOf": [_annotation_to_json_schema(a) for a in non_none]}

    # list[X]
    if origin is list:
        schema: dict[str, Any] = {"type": "array"}
        if args:
            schema["items"] = _annotation_to_json_schema(args[0])
        return schema

    # dict[K, V]
    if origin is dict:
        return {"type": "object"}

    # Plain types
    json_type = _PY_TO_JSON_TYPE.get(annotation)
    if json_type:
        return {"type": json_type}

    return {}

# --- PLACEHOLDER FOR CONTINUATION ---

def _build_input_schema(handler) -> dict[str, Any]:
    """Generate a JSON Schema for a tool's input from its function signature."""
    try:
        sig = inspect.signature(handler)
    except (TypeError, ValueError):
        return {"type": "object", "properties": {}}

    properties: dict[str, Any] = {}
    required: list[str] = []

    for name, param in sig.parameters.items():
        if name in ("self", "cls"):
            continue
        prop = _annotation_to_json_schema(param.annotation)
        if not prop:
            prop = {"type": "string"}
        properties[name] = prop
        if param.default is inspect.Parameter.empty:
            required.append(name)

    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


# ---------------------------------------------------------------------------
# Server setup
# ---------------------------------------------------------------------------

def create_server() -> Server:
    """Create and configure the MCP server with all registry tools."""
    from app.mcp_tools.registry import registry

    server = Server("openreel-studio")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        tools: list[Tool] = []
        for spec in registry.list_tools():
            input_schema = spec.schema if spec.schema else _build_input_schema(spec.handler)
            tools.append(Tool(
                name=spec.name,
                description=spec.description or f"{spec.namespace}.{spec.short_name}",
                inputSchema=input_schema,
            ))
        return tools

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any] | None) -> list[TextContent]:
        spec = registry.get(name)
        if not spec:
            return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]

        kwargs = arguments or {}
        # Filter kwargs to match handler signature
        try:
            sig = inspect.signature(spec.handler)
            params = sig.parameters
            if not any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
                kwargs = {k: v for k, v in kwargs.items() if k in params}
        except (TypeError, ValueError):
            pass

        try:
            result = await registry.call(name, **kwargs)
            if isinstance(result, dict):
                text = json.dumps(result, ensure_ascii=False, default=str)
            else:
                text = str(result) if result is not None else '{"ok": true}'
            return [TextContent(type="text", text=text)]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]

    return server


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    server = create_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
