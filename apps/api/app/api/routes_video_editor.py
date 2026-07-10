"""Read-only support endpoints for the interactive video editor."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db.models import WorkflowNode
from app.db.session import get_session
from app.services import media_operations, timeline_thumbnails


router = APIRouter()


@router.get("/{project_id}/nodes/{node_id}/timeline-sprite")
async def get_timeline_sprite(
    project_id: str,
    node_id: str,
    frame_count: int = Query(default=18, ge=6, le=48),
    duration_seconds: float = Query(ge=0.1, le=7200.0),
    frame_width: int = Query(default=128, ge=80, le=192),
    frame_height: int = Query(default=72, ge=45, le=108),
    db: AsyncSession = Depends(get_session),
):
    node = await db.get(WorkflowNode, node_id)
    if not node or node.project_id != project_id:
        raise HTTPException(status_code=404, detail="Node not found")
    if node.type != "video":
        raise HTTPException(status_code=400, detail="Timeline sprites require a video node")
    try:
        source = await media_operations.media_path_for_node(project_id, node, "video")
        sprite = await timeline_thumbnails.ensure_timeline_sprite(
            project_id,
            source,
            frame_count=frame_count,
            duration_seconds=duration_seconds,
            frame_width=frame_width,
            frame_height=frame_height,
        )
    except (media_operations.MediaOperationError, timeline_thumbnails.TimelineThumbnailError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return FileResponse(
        path=str(sprite),
        media_type="image/jpeg",
        headers={
            "Cache-Control": "private, max-age=86400, immutable",
            "Content-Disposition": f'inline; filename="{sprite.name}"',
            "X-OpenReel-Frame-Count": str(frame_count),
        },
    )
