"""Direct tool invocation API — used by frontend slash commands.

Bypasses the LLM. Calls registry tools directly so simple local status and
settings queries do not burn tokens.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db.models import Project
from app.db.session import get_session
from app.mcp_tools import config_tools, mcp_meta_tools
from app.mcp_tools.registry import registry
from app.services import media_provider, node_contract


router = APIRouter()
logger = logging.getLogger(__name__)


class ToolCallRequest(BaseModel):
    tool: str
    args: dict[str, Any] = {}


class ConfigPatchRequest(BaseModel):
    patch: dict[str, Any]


class ConfigTextRequest(BaseModel):
    content: str


class NodeContractRequest(BaseModel):
    project_id: str
    type: str
    fields: dict[str, Any] = Field(default_factory=dict)


async def _emit_direct_tool_canvas_events(
    tool_name: str,
    args: dict[str, Any],
    result: Any,
) -> None:
    """Mirror direct registry mutations to the project event stream."""
    if tool_name != "node.update" or not isinstance(result, dict) or result.get("ok") is False:
        return
    project_id = str(args.get("project_id") or "").strip()
    if not project_id:
        return
    items = result.get("results") if isinstance(result.get("results"), list) else [result]
    try:
        from app.agent.orchestrator import emit_canvas_event

        for item in items:
            if not isinstance(item, dict) or item.get("ok") is False:
                continue
            node_id = item.get("_canvas_id") or item.get("_canvas_node_id") or item.get("id")
            if not node_id:
                continue
            payload = dict(item)
            payload["id"] = node_id
            await emit_canvas_event(
                {"type": "canvas_action", "action": "update_node", "payload": payload},
                project_id=project_id,
            )
            edge_sync = item.get("edge_sync")
            if not isinstance(edge_sync, dict):
                continue
            for edge in edge_sync.get("added_edges") or []:
                if isinstance(edge, dict):
                    await emit_canvas_event(
                        {"type": "canvas_action", "action": "add_edge", "payload": edge},
                        project_id=project_id,
                    )
            for edge in edge_sync.get("removed_edges") or []:
                if isinstance(edge, dict):
                    await emit_canvas_event(
                        {"type": "canvas_action", "action": "delete_edge", "payload": edge},
                        project_id=project_id,
                    )
    except Exception:
        logger.exception("direct tool canvas event failed tool=%s project=%s", tool_name, project_id)


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
    await _emit_direct_tool_canvas_events(req.tool, req.args, result)
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


@router.post("/node-contract")
async def describe_node_contract(
    req: NodeContractRequest,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Return the current node/provider contract and a side-effect-free preflight."""
    project = await db.get(Project, req.project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    try:
        project_state = json.loads(project.state_json or "{}")
    except (TypeError, ValueError):
        project_state = {}
    config = await config_tools.config_read(mask_secrets=True)
    catalog = {
        "image": media_provider.list_image_http_v1_protocol_catalog,
        "video": media_provider.list_video_http_v1_protocol_catalog,
        "audio": media_provider.list_audio_http_v1_protocol_catalog,
    }.get(req.type)
    return node_contract.build_node_contract(
        node_type=req.type,
        fields=req.fields,
        config=config,
        project_state=project_state,
        protocol_catalog=catalog() if catalog else {},
    )


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


@router.get("/config/video-protocols")
async def read_video_protocols() -> dict[str, Any]:
    return media_provider.list_video_http_v1_protocol_catalog()


@router.get("/config/image-protocols")
async def read_image_protocols() -> dict[str, Any]:
    return media_provider.list_image_http_v1_protocol_catalog()


@router.get("/config/audio-protocols")
async def read_audio_protocols() -> dict[str, Any]:
    return media_provider.list_audio_http_v1_protocol_catalog()


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
