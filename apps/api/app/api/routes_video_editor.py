"""Read-only support endpoints for the interactive video editor."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db.models import WorkflowNode
from app.db.session import get_session
from app.services import media_operations, timeline_thumbnails, video_edit_sequences


router = APIRouter()


class SaveSequenceRequest(BaseModel):
    expected_revision: int = Field(ge=0)
    spec: video_edit_sequences.SequenceSpec


class RestoreSequenceRequest(BaseModel):
    expected_revision: int = Field(ge=1)
    target_revision: int = Field(ge=1)


async def _load_video_node(
    db: AsyncSession,
    *,
    project_id: str,
    node_id: str,
) -> WorkflowNode:
    node = await db.get(WorkflowNode, node_id)
    if not node or node.project_id != project_id:
        raise HTTPException(status_code=404, detail="Node not found")
    if node.type != "video":
        raise HTTPException(status_code=400, detail="Video editor requires a video node")
    return node


@router.get(
    "/{project_id}/nodes/{node_id}/sequence",
    response_model=video_edit_sequences.SequenceDocument | None,
)
async def get_video_edit_sequence(
    project_id: str,
    node_id: str,
    db: AsyncSession = Depends(get_session),
):
    await _load_video_node(db, project_id=project_id, node_id=node_id)
    return await video_edit_sequences.read_sequence(db, node_id)


@router.put(
    "/{project_id}/nodes/{node_id}/sequence",
    response_model=video_edit_sequences.SequenceDocument,
)
async def save_video_edit_sequence(
    project_id: str,
    node_id: str,
    req: SaveSequenceRequest,
    db: AsyncSession = Depends(get_session),
):
    await _load_video_node(db, project_id=project_id, node_id=node_id)
    try:
        return await video_edit_sequences.save_sequence(
            db,
            project_id=project_id,
            node_id=node_id,
            expected_revision=req.expected_revision,
            spec=req.spec,
        )
    except video_edit_sequences.SequenceRevisionConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Sequence revision conflict",
                "current_revision": exc.current_revision,
            },
        ) from exc


@router.get(
    "/{project_id}/nodes/{node_id}/sequence/history",
    response_model=list[video_edit_sequences.SequenceHistoryItem],
)
async def get_video_edit_sequence_history(
    project_id: str,
    node_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_session),
):
    await _load_video_node(db, project_id=project_id, node_id=node_id)
    return await video_edit_sequences.sequence_history(
        db,
        project_id=project_id,
        node_id=node_id,
        limit=limit,
    )


@router.post(
    "/{project_id}/nodes/{node_id}/sequence/restore",
    response_model=video_edit_sequences.SequenceDocument,
)
async def restore_video_edit_sequence(
    project_id: str,
    node_id: str,
    req: RestoreSequenceRequest,
    db: AsyncSession = Depends(get_session),
):
    await _load_video_node(db, project_id=project_id, node_id=node_id)
    try:
        return await video_edit_sequences.restore_sequence(
            db,
            project_id=project_id,
            node_id=node_id,
            expected_revision=req.expected_revision,
            target_revision=req.target_revision,
        )
    except video_edit_sequences.SequenceRevisionConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Sequence revision conflict",
                "current_revision": exc.current_revision,
            },
        ) from exc
    except video_edit_sequences.SequenceNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


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
    node = await _load_video_node(db, project_id=project_id, node_id=node_id)
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
