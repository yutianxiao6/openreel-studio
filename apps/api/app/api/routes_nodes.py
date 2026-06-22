"""Workflow nodes / edges read endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db.session import get_session
from app.services.node_service import NodeService, workflow_node_payload

router = APIRouter()


@router.get("/{project_id}")
async def list_nodes(project_id: str, db: AsyncSession = Depends(get_session)):
    svc = NodeService(db)
    nodes = await svc.list_nodes(project_id)
    edges = await svc.list_canvas_edges(project_id, nodes=nodes)
    return {
        "project_id": project_id,
        "nodes": [workflow_node_payload(n) for n in nodes],
        "edges": edges,
    }
