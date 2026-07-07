"""Workflow extension and node-type APIs."""
from __future__ import annotations

from fastapi import APIRouter

from app.services import workflow_plugins


router = APIRouter()


@router.get("/node-types")
async def list_workflow_node_types() -> dict:
    """Return built-in and plugin workflow node types for the workflow panel."""
    return workflow_plugins.workflow_node_types()


@router.get("/plugins")
async def list_workflow_plugins() -> dict:
    return {
        "ok": True,
        "plugins": workflow_plugins.list_plugins(),
        "errors": workflow_plugins.plugin_errors(),
    }


@router.post("/plugins/reload")
async def reload_workflow_plugins() -> dict:
    workflow_plugins.reload_plugins()
    return {
        "ok": True,
        "plugins": workflow_plugins.list_plugins(),
        "errors": workflow_plugins.plugin_errors(),
    }
