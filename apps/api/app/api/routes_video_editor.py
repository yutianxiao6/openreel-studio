"""Frame-native sequence, source-index, waveform, and render endpoints."""
from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db.models import WorkflowEdge, WorkflowNode
from app.db.session import get_session
from app.services import (
    media_operations,
    project_media_history,
    timeline_media_index,
    timeline_thumbnails,
    timeline_waveforms,
    video_edit_sequences,
    video_sequence_renderer,
)
from app.services.node_service import NodeService, workflow_node_payload


router = APIRouter()


class SaveSequenceRequest(BaseModel):
    expected_revision: int = Field(ge=0)
    spec: video_edit_sequences.SequenceSpec


class RestoreSequenceRequest(BaseModel):
    expected_revision: int = Field(ge=1)
    target_revision: int = Field(ge=1)


class RenderSequenceRequest(BaseModel):
    expected_revision: int = Field(ge=1)
    title: str | None = Field(default=None, max_length=160)


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


def _render_output_position(source: WorkflowNode, existing: list[WorkflowNode]) -> tuple[float, float]:
    x = float(source.position_x or 0.0) + 380.0
    y = float(source.position_y or 0.0)
    occupied = [(float(node.position_x or 0.0), float(node.position_y or 0.0)) for node in existing]
    for _ in range(24):
        if not any(abs(x - other_x) < 320.0 and abs(y - other_y) < 240.0 for other_x, other_y in occupied):
            return x, y
        y += 260.0
    return x, y


@router.post("/{project_id}/nodes/{node_id}/sequence/render")
async def render_video_edit_sequence(
    project_id: str,
    node_id: str,
    req: RenderSequenceRequest,
    db: AsyncSession = Depends(get_session),
):
    source_node = await _load_video_node(db, project_id=project_id, node_id=node_id)
    document = await video_edit_sequences.read_sequence(db, node_id)
    if document is None:
        raise HTTPException(status_code=400, detail="请先保存时间线再导出")
    if document.revision != req.expected_revision:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Sequence revision conflict",
                "current_revision": document.revision,
            },
        )
    media_node_ids: list[str] = []
    for clip in document.spec.clips:
        media_node_id = clip.media_id.removeprefix("embedded-audio:")
        if media_node_id not in media_node_ids:
            media_node_ids.append(media_node_id)
    nodes_by_id: dict[str, WorkflowNode] = {}
    for media_node_id in media_node_ids:
        node = await db.get(WorkflowNode, media_node_id)
        if node is None or node.project_id != project_id:
            raise HTTPException(status_code=400, detail=f"找不到片段媒体节点: {media_node_id}")
        nodes_by_id[node.id] = node
    try:
        result = await video_sequence_renderer.render_sequence(
            project_id,
            document.spec,
            revision=document.revision,
            nodes_by_id=nodes_by_id,
            title=req.title or f"{source_node.title or '视频'} · 时间线成片",
        )
    except media_operations.MediaOperationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    source_nodes = [nodes_by_id[node_id] for node_id in result.metadata.get("source_node_ids", []) if node_id in nodes_by_id]
    if source_node.id not in {node.id for node in source_nodes}:
        source_nodes.insert(0, source_node)
    existing_nodes = list((await db.exec(
        select(WorkflowNode).where(WorkflowNode.project_id == project_id)
    )).all())
    position_x, position_y = _render_output_position(source_node, existing_nodes)
    source_refs = [f"node:{node.display_id}" if node.display_id is not None else f"node:{node.id}" for node in source_nodes]
    output = media_operations.item_output(project_id, result)
    service = NodeService(db)
    rendered_node = await service.create_node(project_id, {
        "type": "video",
        "title": result.title,
        "status": "completed",
        "position_x": position_x,
        "position_y": position_y,
        "input_json": {
            "surface": "video_editor_sequence_render",
            "title": result.title,
            "source": {
                "kind": "video_editor_sequence",
                "source_node_id": source_node.id,
                "sequence_revision": document.revision,
            },
            "depends_on": source_refs,
            "fields": {
                "media_operation": result.metadata,
                "source_node_refs": source_refs,
            },
        },
        "output_json": output,
        "model_config_json": {
            "surface": "video_editor_sequence_render",
            "_ui_creator": "user",
            "created_by": "user",
        },
        "prompt": None,
        "error_message": None,
    })
    edges: list[WorkflowEdge] = []
    for source in source_nodes:
        edge = WorkflowEdge(
            id=str(uuid.uuid4()),
            project_id=project_id,
            source_node_id=source.id,
            target_node_id=rendered_node.id,
            label="时间线导出",
            created_at=datetime.utcnow(),
        )
        db.add(edge)
        edges.append(edge)
    await db.commit()
    await db.refresh(rendered_node)
    project_media_history.register_node_outputs(project_id, rendered_node)
    return {
        "ok": True,
        "sequence_revision": document.revision,
        "node": workflow_node_payload(rendered_node),
        "edges": [edge.model_dump(mode="json") for edge in edges],
        "render": result.metadata,
    }


@router.get("/{project_id}/nodes/{node_id}/media-index")
async def get_video_media_index(
    project_id: str,
    node_id: str,
    db: AsyncSession = Depends(get_session),
):
    node = await _load_video_node(db, project_id=project_id, node_id=node_id)
    try:
        source = await media_operations.media_path_for_node(project_id, node, "video")
        manifest = await timeline_media_index.ensure_media_index(project_id, source)
    except (media_operations.MediaOperationError, timeline_media_index.TimelineMediaIndexError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return manifest.summary()


@router.get("/{project_id}/nodes/{node_id}/frames")
async def get_video_frame_index_page(
    project_id: str,
    node_id: str,
    start: int = Query(default=0, ge=0),
    limit: int = Query(default=500, ge=1, le=2_000),
    db: AsyncSession = Depends(get_session),
):
    node = await _load_video_node(db, project_id=project_id, node_id=node_id)
    try:
        source = await media_operations.media_path_for_node(project_id, node, "video")
        manifest = await timeline_media_index.ensure_media_index(project_id, source)
    except (media_operations.MediaOperationError, timeline_media_index.TimelineMediaIndexError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return timeline_media_index.frame_page(manifest, start=start, limit=limit)


@router.get("/{project_id}/nodes/{node_id}/frame-tiles/{tile_index}")
async def get_video_frame_tile(
    project_id: str,
    node_id: str,
    tile_index: int,
    columns: int = Query(default=8, ge=1, le=16),
    rows: int = Query(default=4, ge=1, le=8),
    frame_width: int = Query(default=96, ge=48, le=192),
    frame_height: int = Query(default=54, ge=28, le=108),
    db: AsyncSession = Depends(get_session),
):
    node = await _load_video_node(db, project_id=project_id, node_id=node_id)
    try:
        source = await media_operations.media_path_for_node(project_id, node, "video")
        tile, manifest, start_frame, actual_count = await timeline_media_index.ensure_frame_tile(
            project_id,
            source,
            tile_index=tile_index,
            columns=columns,
            rows=rows,
            frame_width=frame_width,
            frame_height=frame_height,
        )
    except (media_operations.MediaOperationError, timeline_media_index.TimelineMediaIndexError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return FileResponse(
        path=str(tile),
        media_type="image/jpeg",
        headers={
            "Cache-Control": "private, max-age=86400, immutable",
            "Content-Disposition": f'inline; filename="{tile.name}"',
            "X-OpenReel-Cache-Key": manifest.cache_key,
            "X-OpenReel-Start-Frame": str(start_frame),
            "X-OpenReel-Frame-Count": str(actual_count),
            "X-OpenReel-Tile-Columns": str(columns),
            "X-OpenReel-Tile-Rows": str(rows),
        },
    )


@router.get("/{project_id}/nodes/{node_id}/waveform/manifest")
async def get_video_waveform_manifest(
    project_id: str,
    node_id: str,
    db: AsyncSession = Depends(get_session),
):
    node = await _load_video_node(db, project_id=project_id, node_id=node_id)
    try:
        source = await media_operations.media_path_for_node(project_id, node, "video")
        manifest, _ = await timeline_waveforms.ensure_waveform(project_id, source)
    except (
        media_operations.MediaOperationError,
        timeline_media_index.TimelineMediaIndexError,
        timeline_waveforms.TimelineWaveformError,
    ) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return manifest.model_dump(mode="json")


@router.get("/{project_id}/nodes/{node_id}/waveform")
async def get_video_waveform_page(
    project_id: str,
    node_id: str,
    level: int = Query(default=0, ge=0, le=30),
    start_bucket: int = Query(default=0, ge=0),
    limit: int = Query(default=2_000, ge=1, le=10_000),
    db: AsyncSession = Depends(get_session),
):
    node = await _load_video_node(db, project_id=project_id, node_id=node_id)
    try:
        source = await media_operations.media_path_for_node(project_id, node, "video")
        manifest, peaks_path = await timeline_waveforms.ensure_waveform(project_id, source)
        return timeline_waveforms.waveform_page(
            manifest,
            peaks_path,
            level=level,
            start_bucket=start_bucket,
            limit=limit,
        )
    except (
        media_operations.MediaOperationError,
        timeline_media_index.TimelineMediaIndexError,
        timeline_waveforms.TimelineWaveformError,
    ) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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
