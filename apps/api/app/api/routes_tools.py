"""Direct tool invocation API — used by frontend slash commands.

Bypasses the LLM. Calls registry tools directly so simple local status and
settings queries do not burn tokens.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.mcp_tools import config_tools, mcp_meta_tools
from app.mcp_tools.registry import registry


router = APIRouter()
logger = logging.getLogger(__name__)


class ToolCallRequest(BaseModel):
    tool: str
    args: dict[str, Any] = {}


class ConfigPatchRequest(BaseModel):
    patch: dict[str, Any]


class ConfigTextRequest(BaseModel):
    content: str


@router.post("/call")
async def call_tool(req: ToolCallRequest) -> dict[str, Any]:
    spec = registry.get(req.tool)
    if not spec:
        raise HTTPException(status_code=404, detail=f"Unknown tool: {req.tool}")
    try:
        result = await registry.call(req.tool, **req.args)
    except TypeError as exc:
        logger.warning("tool_call_bad_args tool=%s args=%s error=%s", req.tool, req.args, exc)
        raise HTTPException(status_code=400, detail=f"Bad arguments: {exc}")
    except Exception as exc:
        logger.exception("tool_call_failed tool=%s args=%s", req.tool, req.args)
        raise HTTPException(status_code=500, detail=str(exc))
    if isinstance(result, dict) and result.get("error"):
        logger.warning(
            "tool_call_returned_error tool=%s node_id=%s error=%s error_kind=%s",
            req.tool,
            req.args.get("node_id"),
            result.get("error"),
            result.get("error_kind"),
        )
    return {"tool": req.tool, "result": result}


@router.get("/list")
async def list_tools() -> dict[str, Any]:
    items = []
    for spec in registry.list_tools():
        items.append({
            "name": spec.name,
            "namespace": spec.namespace,
            "description": spec.description,
            "tags": spec.tags,
        })
    items.sort(key=lambda t: t["name"])
    return {"tools": items, "namespaces": registry.namespaces(), "total": len(items)}


@router.get("/mcp/servers")
async def list_mcp_servers() -> dict[str, Any]:
    """Return external MCP server status without exposing MCP meta tools."""
    servers = await mcp_meta_tools.mcp_list_servers()
    return {"servers": servers, "total": len(servers)}


@router.get("/mcp/external-tools")
async def list_mcp_external_tools(server_name: str | None = None) -> dict[str, Any]:
    tools = await mcp_meta_tools.mcp_list_external_tools(server_name=server_name)
    return {"tools": tools, "total": len(tools)}


@router.post("/mcp/servers/{server_name}/reload")
async def reload_mcp_server(server_name: str) -> dict[str, Any]:
    return await mcp_meta_tools.mcp_reload_server(server_name)


@router.get("/config/file")
async def read_config_file(mask_secrets: bool = True) -> dict[str, Any]:
    return await config_tools.config_read_file(mask_secrets=mask_secrets)


@router.get("/config/summary")
async def read_config_summary() -> dict[str, Any]:
    return await config_tools.config_list_all()


@router.post("/config/validate")
async def validate_config_text(req: ConfigTextRequest) -> dict[str, Any]:
    return await config_tools.config_validate(content=req.content)


@router.post("/config/file")
async def write_config_file(req: ConfigTextRequest) -> dict[str, Any]:
    return await config_tools.config_write_file(content=req.content)


@router.patch("/config")
async def patch_config(req: ConfigPatchRequest) -> dict[str, Any]:
    return await config_tools.config_patch(patch=req.patch)


@router.post("/config/reload")
async def reload_config() -> dict[str, Any]:
    return await config_tools.config_reload()
