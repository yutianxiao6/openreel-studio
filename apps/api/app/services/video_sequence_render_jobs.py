"""Persistent background jobs for frame-native video sequence exports."""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db import session as db_session
from app.db.models import VideoSequenceRenderJob, WorkflowEdge, WorkflowNode
from app.services import media_operations, project_media_history, video_sequence_renderer
from app.services.node_service import NodeService, workflow_node_payload
from app.services.video_edit_sequences import SequenceSpec


logger = logging.getLogger(__name__)
ACTIVE_STATUSES = {"queued", "running", "cancelling"}
TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def render_job_payload(job: VideoSequenceRenderJob) -> dict[str, Any]:
    result: dict[str, Any] | None = None
    if job.result_json:
        try:
            parsed = json.loads(job.result_json)
            result = parsed if isinstance(parsed, dict) else None
        except (json.JSONDecodeError, TypeError):
            result = None
    return {
        "id": job.id,
        "project_id": job.project_id,
        "source_node_id": job.source_node_id,
        "sequence_revision": job.sequence_revision,
        "title": job.title,
        "status": job.status,
        "progress": max(0, min(100, int(job.progress or 0))),
        "phase": job.phase,
        "cancel_requested": bool(job.cancel_requested),
        "output_node_id": job.output_node_id,
        "error_message": job.error_message,
        "result": result,
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat(),
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
    }


def _render_output_position(source: WorkflowNode, existing: list[WorkflowNode]) -> tuple[float, float]:
    x = float(source.position_x or 0.0) + 380.0
    y = float(source.position_y or 0.0)
    occupied = [(float(node.position_x or 0.0), float(node.position_y or 0.0)) for node in existing]
    for _ in range(24):
        if not any(abs(x - other_x) < 320.0 and abs(y - other_y) < 240.0 for other_x, other_y in occupied):
            return x, y
        y += 260.0
    return x, y


async def create_render_job(
    db: AsyncSession,
    *,
    project_id: str,
    source_node_id: str,
    sequence_revision: int,
    title: str,
    spec: SequenceSpec,
) -> tuple[VideoSequenceRenderJob, bool]:
    active = (await db.exec(
        select(VideoSequenceRenderJob)
        .where(
            VideoSequenceRenderJob.project_id == project_id,
            VideoSequenceRenderJob.source_node_id == source_node_id,
            VideoSequenceRenderJob.status.in_(ACTIVE_STATUSES),
        )
        .order_by(VideoSequenceRenderJob.created_at.desc())
    )).first()
    if active is not None:
        return active, False
    job = VideoSequenceRenderJob(
        id=str(uuid.uuid4()),
        project_id=project_id,
        source_node_id=source_node_id,
        sequence_revision=sequence_revision,
        title=title,
        spec_json=spec.model_dump_json(),
        status="queued",
        progress=0,
        phase="等待渲染",
        created_at=_now(),
        updated_at=_now(),
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)
    return job, True


async def latest_render_job(
    db: AsyncSession,
    *,
    project_id: str,
    source_node_id: str,
) -> VideoSequenceRenderJob | None:
    return (await db.exec(
        select(VideoSequenceRenderJob)
        .where(
            VideoSequenceRenderJob.project_id == project_id,
            VideoSequenceRenderJob.source_node_id == source_node_id,
        )
        .order_by(VideoSequenceRenderJob.created_at.desc())
    )).first()


async def _load_job_sources(
    db: AsyncSession,
    *,
    project_id: str,
    spec: SequenceSpec,
) -> dict[str, WorkflowNode]:
    node_ids: list[str] = []
    for clip in spec.clips:
        node_id = clip.media_id.removeprefix("embedded-audio:")
        if node_id not in node_ids:
            node_ids.append(node_id)
    nodes: dict[str, WorkflowNode] = {}
    for node_id in node_ids:
        node = await db.get(WorkflowNode, node_id)
        if node is None or node.project_id != project_id:
            raise video_sequence_renderer.SequenceRenderError(f"找不到片段媒体节点: {node_id}")
        nodes[node.id] = node
    return nodes


async def _create_output_node(
    db: AsyncSession,
    *,
    source_node: WorkflowNode,
    source_nodes: list[WorkflowNode],
    result: media_operations.MediaOperationFile,
    sequence_revision: int,
) -> tuple[WorkflowNode, list[WorkflowEdge]]:
    existing_nodes = list((await db.exec(
        select(WorkflowNode).where(WorkflowNode.project_id == source_node.project_id)
    )).all())
    position_x, position_y = _render_output_position(source_node, existing_nodes)
    source_refs = [
        f"node:{node.display_id}" if node.display_id is not None else f"node:{node.id}"
        for node in source_nodes
    ]
    output = media_operations.item_output(source_node.project_id, result)
    rendered_node = await NodeService(db).create_node(source_node.project_id, {
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
                "sequence_revision": sequence_revision,
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
            project_id=source_node.project_id,
            source_node_id=source.id,
            target_node_id=rendered_node.id,
            label="时间线导出",
            created_at=_now(),
        )
        db.add(edge)
        edges.append(edge)
    await db.commit()
    await db.refresh(rendered_node)
    project_media_history.register_node_outputs(source_node.project_id, rendered_node)
    return rendered_node, edges


async def _set_terminal_job(
    job_id: str,
    *,
    status: str,
    phase: str,
    error_message: str | None = None,
) -> None:
    async with db_session.session_scope() as db:
        job = await db.get(VideoSequenceRenderJob, job_id)
        if job is None or job.status in TERMINAL_STATUSES:
            return
        job.status = status
        job.phase = phase
        job.error_message = error_message
        job.progress = 100 if status == "completed" else int(job.progress or 0)
        job.updated_at = _now()
        job.completed_at = _now()
        db.add(job)
        await db.commit()


async def _execute_render_job(job_id: str) -> None:
    result: media_operations.MediaOperationFile | None = None
    try:
        async with db_session.session_scope() as db:
            job = await db.get(VideoSequenceRenderJob, job_id)
            if job is None:
                return
            job.status = "running"
            job.phase = "正在准备素材"
            job.progress = max(0, int(job.progress or 0))
            job.updated_at = _now()
            db.add(job)
            await db.commit()
            source_node = await db.get(WorkflowNode, job.source_node_id)
            if source_node is None or source_node.project_id != job.project_id:
                raise video_sequence_renderer.SequenceRenderError("找不到时间线源视频节点")
            spec = SequenceSpec.model_validate_json(job.spec_json)
            nodes_by_id = await _load_job_sources(db, project_id=job.project_id, spec=spec)
            last_persisted_progress = -1

            async def update_progress(progress: int, phase: str) -> None:
                nonlocal last_persisted_progress
                if progress < 100 and progress - last_persisted_progress < 2:
                    return
                await db.refresh(job)
                if job.cancel_requested:
                    raise asyncio.CancelledError
                job.status = "running"
                job.progress = progress
                job.phase = phase
                job.updated_at = _now()
                db.add(job)
                await db.commit()
                last_persisted_progress = progress

            result = await video_sequence_renderer.render_sequence(
                job.project_id,
                spec,
                revision=job.sequence_revision,
                nodes_by_id=nodes_by_id,
                title=job.title,
                progress_callback=update_progress,
            )
            await db.refresh(job)
            if job.cancel_requested:
                raise asyncio.CancelledError
            source_nodes = [
                nodes_by_id[node_id]
                for node_id in result.metadata.get("source_node_ids", [])
                if node_id in nodes_by_id
            ]
            if source_node.id not in {node.id for node in source_nodes}:
                source_nodes.insert(0, source_node)
            rendered_node, edges = await _create_output_node(
                db,
                source_node=source_node,
                source_nodes=source_nodes,
                result=result,
                sequence_revision=job.sequence_revision,
            )
            await db.refresh(job)
            job.status = "completed"
            job.progress = 100
            job.phase = "导出完成"
            job.output_node_id = rendered_node.id
            job.error_message = None
            job.result_json = json.dumps({
                "node": workflow_node_payload(rendered_node),
                "edges": [edge.model_dump(mode="json") for edge in edges],
                "render": result.metadata,
            }, ensure_ascii=False, default=str)
            job.updated_at = _now()
            job.completed_at = _now()
            db.add(job)
            await db.commit()
    except asyncio.CancelledError:
        if result is not None and result.path.exists():
            result.path.unlink(missing_ok=True)
        await _set_terminal_job(job_id, status="cancelled", phase="已取消")
    except Exception as exc:
        if result is not None and result.path.exists():
            result.path.unlink(missing_ok=True)
        logger.exception("Video sequence render job failed: %s", job_id)
        await _set_terminal_job(
            job_id,
            status="failed",
            phase="导出失败",
            error_message=str(exc) or "序列渲染失败",
        )


class VideoSequenceRenderJobManager:
    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def start(self, job_id: str) -> None:
        existing = self._tasks.get(job_id)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(_execute_render_job(job_id), name=f"video-sequence-render:{job_id}")
        self._tasks[job_id] = task
        task.add_done_callback(lambda completed: self._tasks.pop(job_id, None))

    def cancel(self, job_id: str) -> bool:
        task = self._tasks.get(job_id)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    def is_active(self, job_id: str) -> bool:
        task = self._tasks.get(job_id)
        return bool(task is not None and not task.done())


render_job_manager = VideoSequenceRenderJobManager()


async def request_job_cancel(db: AsyncSession, job: VideoSequenceRenderJob) -> VideoSequenceRenderJob:
    if job.status in TERMINAL_STATUSES or int(job.progress or 0) >= 100:
        return job
    job.cancel_requested = True
    job.status = "cancelling"
    job.phase = "正在取消"
    job.updated_at = _now()
    db.add(job)
    await db.commit()
    await db.refresh(job)
    if not render_job_manager.cancel(job.id):
        job.status = "cancelled"
        job.phase = "已取消"
        job.completed_at = _now()
        job.updated_at = _now()
        db.add(job)
        await db.commit()
        await db.refresh(job)
    return job


async def recover_interrupted_render_jobs() -> int:
    recovered = 0
    async with db_session.session_scope() as db:
        rows = list((await db.exec(
            select(VideoSequenceRenderJob).where(VideoSequenceRenderJob.status.in_(ACTIVE_STATUSES))
        )).all())
        for job in rows:
            job.status = "failed"
            job.phase = "服务重启，导出已中断"
            job.error_message = "服务重启导致后台渲染中断，请重新导出"
            job.updated_at = _now()
            job.completed_at = _now()
            db.add(job)
            recovered += 1
        if recovered:
            await db.commit()
    return recovered
