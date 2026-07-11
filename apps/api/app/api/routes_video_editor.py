"""Frame-native sequence, source-index, waveform, and render endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db.models import VideoSequenceRenderJob, WorkflowNode
from app.db.session import get_session
from app.services import (
    media_operations,
    timeline_media_index,
    timeline_thumbnails,
    timeline_waveforms,
    video_edit_sequences,
    video_sequence_render_jobs,
)


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


@router.post("/{project_id}/nodes/{node_id}/sequence/render", status_code=202)
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
    job, created = await video_sequence_render_jobs.create_render_job(
        db,
        project_id=project_id,
        source_node_id=node_id,
        sequence_revision=document.revision,
        title=req.title or f"{source_node.title or '视频'} · 时间线成片",
        spec=document.spec,
    )
    video_sequence_render_jobs.render_job_manager.start(job.id)
    payload = video_sequence_render_jobs.render_job_payload(job)
    payload["created"] = created
    return payload


async def _load_render_job(
    db: AsyncSession,
    *,
    project_id: str,
    node_id: str,
    job_id: str,
) -> VideoSequenceRenderJob:
    job = await db.get(VideoSequenceRenderJob, job_id)
    if job is None or job.project_id != project_id or job.source_node_id != node_id:
        raise HTTPException(status_code=404, detail="Render job not found")
    return job


@router.get("/{project_id}/nodes/{node_id}/sequence/render")
async def get_latest_video_sequence_render_job(
    project_id: str,
    node_id: str,
    db: AsyncSession = Depends(get_session),
):
    await _load_video_node(db, project_id=project_id, node_id=node_id)
    job = await video_sequence_render_jobs.latest_render_job(
        db,
        project_id=project_id,
        source_node_id=node_id,
    )
    return video_sequence_render_jobs.render_job_payload(job) if job else None


@router.get("/{project_id}/nodes/{node_id}/sequence/render/{job_id}")
async def get_video_sequence_render_job(
    project_id: str,
    node_id: str,
    job_id: str,
    db: AsyncSession = Depends(get_session),
):
    job = await _load_render_job(db, project_id=project_id, node_id=node_id, job_id=job_id)
    return video_sequence_render_jobs.render_job_payload(job)


@router.post("/{project_id}/nodes/{node_id}/sequence/render/{job_id}/cancel")
async def cancel_video_sequence_render_job(
    project_id: str,
    node_id: str,
    job_id: str,
    db: AsyncSession = Depends(get_session),
):
    job = await _load_render_job(db, project_id=project_id, node_id=node_id, job_id=job_id)
    job = await video_sequence_render_jobs.request_job_cancel(db, job)
    return video_sequence_render_jobs.render_job_payload(job)


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
