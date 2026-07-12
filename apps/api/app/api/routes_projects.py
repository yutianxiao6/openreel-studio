"""Project CRUD endpoints."""
from __future__ import annotations

import json
import mimetypes
import uuid
import base64
import binascii
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Optional

from sqlalchemy import or_
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.db.models import Asset, Message, WorkflowEdge, WorkflowNode
from app.db.session import get_session
from app.agent import canvas_workflow_templates, workflow_spec_artifacts, workflow_template_store
from app.agent.workflow_audit import WorkflowAuditError
from app.mcp_tools import canvas_tools, panel_tools, workflow_tools
from app.services import image_operations, media_history, media_operations, project_media_history
from app.services.node_service import NodeService, workflow_node_payload
from app.services.node_ids import next_node_display_id, node_display_id_allocation
from app.services.node_public_ids import internal_to_public_id_map, publicize_node_refs, resolve_internal_node_id
from app.services.node_recovery import cleanup_interrupted_media_nodes
from app.services.project_service import DEFAULT_EPISODE_COUNT, ProjectService
from app.services.reference_mentions import refresh_node_reference_mentions

router = APIRouter()


NODE_MEDIA_UPLOAD_MAX_BYTES: dict[str, int] = {
    "image": 50 * 1024 * 1024,
    "video": 1024 * 1024 * 1024,
}
NODE_MEDIA_UPLOAD_EXTENSIONS: dict[str, tuple[str, ...]] = {
    "image": (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"),
    "video": (".mp4", ".webm", ".mov", ".m4v"),
}
NODE_MEDIA_UPLOAD_FALLBACK_EXTENSION = {
    "image": ".png",
    "video": ".mp4",
}
ACTIVE_WORKFLOW_STATE_KEY = "active_workflow"


class CreateProjectRequest(BaseModel):
    title: str
    description: Optional[str] = None
    genre: Optional[str] = None
    format: Optional[str] = "竖屏短剧"
    episode_count: int = DEFAULT_EPISODE_COUNT
    duration_per_episode: int = 90
    budget_level: Optional[str] = "low"


class UpdateProjectRequest(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    genre: Optional[str] = None
    episode_count: Optional[int] = None
    duration_per_episode: Optional[int] = None
    budget_level: Optional[str] = None
    status: Optional[str] = None


class PanelLayoutRequest(BaseModel):
    mode: str = "tier"


class NodePositionRequest(BaseModel):
    x: float
    y: float


class CanvasNodeCreateRequest(BaseModel):
    type: str
    title: Optional[str] = None
    x: float = 0.0
    y: float = 0.0


class CanvasNodeUpdateRequest(BaseModel):
    title: Optional[str] = None
    prompt: Optional[str] = None
    input: Optional[dict[str, Any]] = None
    output: Optional[Any] = None


class CanvasNodesDeleteRequest(BaseModel):
    node_ids: list[str] = Field(default_factory=list)


class CanvasNodeHistorySwitchRequest(BaseModel):
    history_id: Optional[str] = None
    index: Optional[int] = None


class CanvasNodeImageEditRequest(BaseModel):
    action: str = "preview"
    source_ref: Optional[str] = None
    candidate_ref: Optional[str] = None
    operations: list[dict[str, Any]] = Field(default_factory=list)


class CanvasNodeImageCurvePreviewRequest(BaseModel):
    source_ref: Optional[str] = None
    color: str = "#22d3ee"
    detail: float = 0.78
    line_strength: float = 0.92
    base_visibility: float = 0.12


class CanvasPanoramaCaptureRequest(BaseModel):
    title: Optional[str] = None
    data_url: str
    x: float = 0.0
    y: float = 0.0
    source_node_id: Optional[str] = None
    mode: Literal["single", "four", "eight"] = "single"


class CanvasEdgeRequest(BaseModel):
    source_node_id: str
    target_node_id: str
    label: Optional[str] = None


class MediaOperationPosition(BaseModel):
    x: float
    y: float


class MediaOperationRange(BaseModel):
    start_seconds: float = Field(ge=0.0)
    end_seconds: float = Field(gt=0.0)


class ProjectMediaOperationRequest(BaseModel):
    operation: Literal[
        "video.export_frame",
        "video.split_tracks",
        "video.trim",
        "video.concat",
        "audio.concat",
    ]
    source_node_id: Optional[str] = None
    source_node_ids: list[str] = Field(default_factory=list)
    frame_mode: Literal["tail", "time"] = "tail"
    time_seconds: Optional[float] = Field(default=None, ge=0.0)
    range: Optional[MediaOperationRange] = None
    position: Optional[MediaOperationPosition] = None
    title: Optional[str] = None


class ProjectWorkflowMaterializeRequest(BaseModel):
    template_id: Optional[str] = None
    workflow: Optional[dict[str, Any]] = None
    artifact_ref: Optional[str] = None
    title: Optional[str] = None
    inputs: dict[str, Any] = Field(default_factory=dict)
    context: dict[str, Any] = Field(default_factory=dict)
    ui_overrides: dict[str, Any] = Field(default_factory=dict)
    origin_x: float = 120.0
    origin_y: float = 120.0
    spacing_x: float = 360.0
    spacing_y: float = 240.0


class ProjectWorkflowPreviewRequest(BaseModel):
    template_id: Optional[str] = None
    workflow: Optional[dict[str, Any]] = None
    artifact_ref: Optional[str] = None
    instance_id: Optional[str] = None
    inputs: dict[str, Any] = Field(default_factory=dict)
    context: dict[str, Any] = Field(default_factory=dict)


class ProjectWorkflowRunStepRequest(ProjectWorkflowMaterializeRequest):
    step_id: str
    instance_id: Optional[str] = None


class ProjectWorkflowRunNextRequest(ProjectWorkflowMaterializeRequest):
    instance_id: Optional[str] = None


class ProjectWorkflowRunAllRequest(ProjectWorkflowRunNextRequest):
    max_steps: int = 0


class ProjectWorkflowRuntimePauseRequest(BaseModel):
    template_id: Optional[str] = None
    reason: Optional[str] = None


class ProjectWorkflowActiveRequest(BaseModel):
    kind: Literal["template", "artifact", "imported"]
    template_id: Optional[str] = None
    artifact_ref: Optional[str] = None
    workflow: Optional[dict[str, Any]] = None
    name: Optional[str] = None
    description: Optional[str] = None


class ProjectWorkflowTemplateSaveRequest(BaseModel):
    workflow: dict[str, Any]
    template_id: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = "user"
    applies_to: Optional[str] = None
    version: Optional[str] = None
    replace_existing: bool = False
    inputs: dict[str, Any] = Field(default_factory=dict)


class CanvasNodeSnapshot(BaseModel):
    id: str
    type: str
    title: Optional[str] = None
    status: Optional[str] = "idle"
    position: Optional[dict[str, float]] = None
    input: Optional[dict[str, Any]] = None
    output: Optional[Any] = None
    prompt: Optional[str] = None
    error_message: Optional[str] = None
    version: Optional[int] = 1
    supersedes_id: Optional[str] = None
    creator: Optional[str] = "user"


class CanvasEdgeSnapshot(BaseModel):
    id: Optional[str] = None
    source_node_id: Optional[str] = None
    target_node_id: Optional[str] = None
    source: Optional[str] = None
    target: Optional[str] = None
    label: Optional[str] = None


class CanvasRestoreSnapshotRequest(BaseModel):
    nodes: list[CanvasNodeSnapshot] = []
    edges: list[CanvasEdgeSnapshot] = []


class ProjectMediaHistoryRestoreRequest(BaseModel):
    x: float = 0.0
    y: float = 0.0
    title: Optional[str] = None


def _parse_json_dict(raw: object) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _parse_json_value(raw: object) -> object:
    if raw is None:
        return None
    if isinstance(raw, str) and raw.strip():
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return raw
    return raw


def _json_dumps_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def _project_active_workflow_payload(project_id: str, state: dict[str, Any] | None) -> dict[str, Any] | None:
    active = state.get(ACTIVE_WORKFLOW_STATE_KEY) if isinstance(state, dict) else None
    if not isinstance(active, dict):
        return None
    kind = str(active.get("kind") or "").strip().lower()
    if kind == "template":
        template_id = str(active.get("template_id") or "").strip()
        if not template_id:
            return None
        return {
            "kind": "template",
            "template_id": template_id,
            "updated_at": active.get("updated_at") or "",
        }
    if kind == "artifact":
        artifact_ref = str(active.get("artifact_ref") or "").strip()
        if not artifact_ref:
            return None
        payload: dict[str, Any] = {
            "kind": "artifact",
            "artifact_ref": artifact_ref,
            "name": active.get("name") or "",
            "description": active.get("description") or "",
            "updated_at": active.get("updated_at") or "",
        }
        try:
            artifact = workflow_spec_artifacts.load_workflow_spec_artifact(project_id, artifact_ref)
            workflow = artifact.get("workflow") if isinstance(artifact.get("workflow"), dict) else {}
            preview = artifact.get("preview") if isinstance(artifact.get("preview"), dict) else {}
            if workflow:
                payload["workflow"] = workflow
            if preview:
                payload["preview"] = preview
                payload["name"] = payload["name"] or preview.get("name") or ""
                payload["description"] = payload["description"] or preview.get("description") or ""
        except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
            payload["error"] = str(exc)
        return payload
    if kind == "imported":
        workflow = active.get("workflow")
        if not isinstance(workflow, dict):
            return None
        preview = workflow_spec_artifacts.workflow_spec_preview(workflow)
        if active.get("name"):
            preview["name"] = active.get("name")
        if active.get("description"):
            preview["description"] = active.get("description")
        return {
            "kind": "imported",
            "workflow": workflow,
            "preview": preview,
            "name": active.get("name") or preview.get("name") or "",
            "description": active.get("description") or preview.get("description") or "",
            "updated_at": active.get("updated_at") or "",
        }
    return None


def _workflow_id_from_active_payload(active: dict[str, Any] | None) -> str:
    if not isinstance(active, dict):
        return ""
    if active.get("kind") == "template":
        return str(active.get("template_id") or "").strip()
    workflow = active.get("workflow") if isinstance(active.get("workflow"), dict) else {}
    preview = active.get("preview") if isinstance(active.get("preview"), dict) else {}
    return str(workflow.get("id") or preview.get("id") or "").strip()


def _project_workflow_runtime_payload(state: dict[str, Any] | None, workflow_id: str = "") -> dict[str, Any] | None:
    if not isinstance(state, dict):
        return None
    payload = workflow_tools.workflow_runtime_public_payload(state, template_id=workflow_id)
    if not payload.get("steps"):
        return None
    return payload


def _project_workflow_runtime_payloads(state: dict[str, Any] | None, workflow_id: str = "") -> list[dict[str, Any]]:
    if not isinstance(state, dict):
        return []
    return workflow_tools.workflow_runtime_public_payloads(state, template_id=workflow_id)


def _active_workflow_state_from_request(req: ProjectWorkflowActiveRequest) -> dict[str, Any]:
    updated_at = datetime.utcnow().isoformat()
    if req.kind == "template":
        template_id = str(req.template_id or "").strip()
        if not template_id:
            raise HTTPException(status_code=400, detail="template_id is required")
        try:
            canvas_workflow_templates.get_template(template_id)
        except canvas_workflow_templates.WorkflowTemplateError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "kind": "template",
            "template_id": template_id,
            "updated_at": updated_at,
        }
    if req.kind == "artifact":
        artifact_ref = str(req.artifact_ref or "").strip()
        if not artifact_ref:
            raise HTTPException(status_code=400, detail="artifact_ref is required")
        return {
            "kind": "artifact",
            "artifact_ref": artifact_ref,
            "name": str(req.name or "").strip(),
            "description": str(req.description or "").strip(),
            "updated_at": updated_at,
        }
    workflow = req.workflow if isinstance(req.workflow, dict) else None
    if not workflow:
        raise HTTPException(status_code=400, detail="workflow is required")
    if not isinstance(workflow.get("steps"), list):
        raise HTTPException(status_code=400, detail="workflow.steps is required")
    try:
        canvas_workflow_templates.normalize_inline_workflow(workflow)
    except canvas_workflow_templates.WorkflowTemplateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "kind": "imported",
        "workflow": workflow,
        "name": str(req.name or "").strip(),
        "description": str(req.description or "").strip(),
        "updated_at": updated_at,
    }


def _classify_node_media_upload(filename: str, mime_type: str | None) -> str | None:
    suffix = Path(filename).suffix.lower()
    mime = (mime_type or "").lower()
    if suffix in NODE_MEDIA_UPLOAD_EXTENSIONS["image"] or mime.startswith("image/"):
        return "image"
    if suffix in NODE_MEDIA_UPLOAD_EXTENSIONS["video"] or mime.startswith("video/"):
        return "video"
    return None


def _safe_node_media_upload_filename(
    raw_name: str,
    *,
    node: WorkflowNode,
    kind: str,
    mime_type: str | None = None,
) -> str:
    original = Path(raw_name or "upload").name
    stem = Path(original).stem.strip()[:64] or "upload"
    safe_stem = "".join(
        char if char.isascii() and (char.isalnum() or char in {"-", "_"}) else "-"
        for char in stem
    ).strip("-_")
    if not safe_stem:
        safe_stem = "upload"
    suffix = Path(original).suffix.lower()
    if suffix not in NODE_MEDIA_UPLOAD_EXTENSIONS.get(kind, ()):
        guessed_mime, _ = mimetypes.guess_type(original)
        suffix = (
            mimetypes.guess_extension(mime_type or guessed_mime or "")
            or NODE_MEDIA_UPLOAD_FALLBACK_EXTENSION[kind]
        )
        if suffix == ".jpe":
            suffix = ".jpg"
        if suffix not in NODE_MEDIA_UPLOAD_EXTENSIONS.get(kind, ()):
            suffix = NODE_MEDIA_UPLOAD_FALLBACK_EXTENSION[kind]
    node_label = f"n{node.display_id}" if node.display_id is not None else node.id[:8]
    timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    return f"{node_label}-{timestamp}-{uuid.uuid4().hex[:8]}-{safe_stem}{suffix}"


def _build_uploaded_node_media_output(
    *,
    project_id: str,
    node: WorkflowNode,
    rel_path: str,
    target_path: Path,
    original_filename: str,
    mime_type: str | None,
    size: int,
    uploaded_at: str,
    current_output: Any,
    current_input: dict[str, Any],
) -> dict[str, Any]:
    item = project_media_history.file_payload(project_id, rel_path, target_path)
    if not item:
        raise ValueError("Unsupported uploaded media")
    item.update({
        "source": "node_upload",
        "source_node_id": node.id,
        "source_node_title": node.title,
        "title": node.title or item.get("title"),
        "prompt": media_history.prompt_from_state(current_output, current_input, node.prompt),
    })
    output = _media_output_for_history_item(item)
    output.update({
        "ok": True,
        "source": "uploaded_node_media",
        "rel_path": rel_path,
        "filename": original_filename,
        "stored_filename": target_path.name,
        "mime_type": mime_type or item.get("mime_type"),
        "size": size,
        "uploaded_at": uploaded_at,
    })
    history = media_history.media_history_from_output(current_output)
    entry = media_history.make_history_entry(
        current_output,
        node_type=node.type,
        prompt=node.prompt or current_input.get("prompt"),
        input_data=current_input,
        label="previous_output",
    )
    if entry:
        history = [entry, *history]
    return media_history.attach_media_history(output, history)


async def _write_upload_file(target: Path, file: UploadFile, *, max_bytes: int) -> int:
    size = 0
    chunk_size = 1 << 20
    with target.open("wb") as fh:
        while True:
            chunk = await file.read(chunk_size)
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                fh.close()
                target.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail=f"File exceeds {max_bytes} bytes")
            fh.write(chunk)
    return size


def _media_output_for_history_item(item: dict[str, Any]) -> dict[str, Any]:
    return project_media_history.output_for_item(item)


def _first_nonempty_text(*values: object) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float, bool)):
            return str(value)
    return ""


def _text_history_entry_id(project_id: str, node_id: str, entry_id: str, index: int) -> str:
    source = f"{project_id}:{node_id}:{entry_id}:{index}"
    return f"text_{uuid.uuid5(uuid.NAMESPACE_URL, source).hex[:18]}"


def _node_text_output_content(input_data: dict[str, Any], output_data: object) -> str:
    if isinstance(output_data, str):
        return output_data.strip()
    output_obj = output_data if isinstance(output_data, dict) else {}
    return _first_nonempty_text(
        output_obj.get("content"),
        output_obj.get("text"),
        output_obj.get("reply"),
        output_obj.get("response"),
        output_obj.get("output"),
        output_obj.get("result"),
        input_data.get("content"),
        input_data.get("text"),
    )


def _text_history_items_from_node(project_id: str, node: WorkflowNode) -> list[dict[str, Any]]:
    if node.type != "text":
        return []
    input_data = _parse_json_dict(node.input_json)
    output_data = _parse_json_value(node.output_json)
    output_obj = output_data if isinstance(output_data, dict) else {}
    raw_history = (
        input_data.get("text_chat_history")
        or input_data.get("chat_history")
        or output_obj.get("text_chat_history")
        or output_obj.get("chat_history")
        or []
    )
    items: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    if isinstance(raw_history, list):
        for index, raw_entry in enumerate(raw_history):
            if not isinstance(raw_entry, dict):
                continue
            prompt = _first_nonempty_text(raw_entry.get("prompt"), raw_entry.get("input"), raw_entry.get("user"))
            content = _first_nonempty_text(raw_entry.get("content"), raw_entry.get("reply"), raw_entry.get("response"), raw_entry.get("output"))
            if not content:
                continue
            key = (prompt, content)
            seen.add(key)
            source_entry_id = _first_nonempty_text(raw_entry.get("id")) or str(index)
            created_at = _first_nonempty_text(raw_entry.get("created_at"), raw_entry.get("completed_at"))
            items.append({
                "id": _text_history_entry_id(project_id, node.id, source_entry_id, index),
                "project_id": project_id,
                "kind": "text",
                "rel_path": "",
                "url": "",
                "filename": f"{node.title or '文本节点'}-{index + 1}.txt",
                "title": node.title or "文本节点",
                "created_at": created_at or (node.updated_at.isoformat() if node.updated_at else None),
                "updated_at": node.updated_at.isoformat() if node.updated_at else None,
                "size": len(content.encode("utf-8")),
                "mime_type": "text/plain",
                "source": "node",
                "source_node_id": node.id,
                "source_node_title": node.title,
                "prompt": prompt or None,
                "content": content,
                "model": _first_nonempty_text(raw_entry.get("model")) or None,
            })
    current_content = _node_text_output_content(input_data, output_data)
    current_prompt = _first_nonempty_text(output_obj.get("prompt"), input_data.get("prompt"), node.prompt)
    if current_content and (current_prompt, current_content) not in seen:
        items.append({
            "id": _text_history_entry_id(project_id, node.id, "current", len(items)),
            "project_id": project_id,
            "kind": "text",
            "rel_path": "",
            "url": "",
            "filename": f"{node.title or '文本节点'}-current.txt",
            "title": node.title or "文本节点",
            "created_at": node.updated_at.isoformat() if node.updated_at else None,
            "updated_at": node.updated_at.isoformat() if node.updated_at else None,
            "size": len(current_content.encode("utf-8")),
            "mime_type": "text/plain",
            "source": "node",
            "source_node_id": node.id,
            "source_node_title": node.title,
            "prompt": current_prompt or None,
            "content": current_content,
            "model": _first_nonempty_text(output_obj.get("model"), input_data.get("model")) or None,
        })
    return items


async def _list_project_media_history_items(project_id: str, db: AsyncSession) -> list[dict[str, Any]]:
    try:
        items = await project_media_history.list_items(project_id, db)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    result = await db.exec(select(WorkflowNode).where(WorkflowNode.project_id == project_id, WorkflowNode.type == "text"))
    for node in result.all():
        items.extend(_text_history_items_from_node(project_id, node))
    return sorted(items, key=lambda item: str(item.get("created_at") or ""), reverse=True)


async def _find_project_media_history_item(project_id: str, item_id: str, db: AsyncSession) -> dict[str, Any] | None:
    try:
        return await project_media_history.find_item(project_id, item_id, db)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _strip_ui_private(data: dict) -> dict:
    return {key: value for key, value in data.items() if key not in {"_ui_creator", "created_by"}}


_DEPENDENCY_FIELD_KEYS = {"depends_on", "references", "reference_images"}


def _has_dependency_fields(data: dict[str, Any]) -> bool:
    containers: list[dict[str, Any]] = [data]
    fields = data.get("fields")
    if isinstance(fields, dict):
        containers.append(fields)
    return any(any(key in container for key in _DEPENDENCY_FIELD_KEYS) for container in containers)


_IMAGE_RENDER_FRESHNESS_KEYS = {
    "prompt",
    "image_prompt",
    "visual_prompt",
    "negative_prompt",
    "aspect_ratio",
    "resolution",
    "quality",
    "clarity",
    "model",
    "seed",
    "style",
    "references",
    "reference_images",
    "depends_on",
}


def _stable_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(value)


def _image_render_inputs_changed(
    old_input: dict[str, Any],
    new_input: dict[str, Any],
    old_prompt: str,
    new_prompt: str,
) -> bool:
    if old_prompt.strip() != new_prompt.strip():
        return True
    for key in _IMAGE_RENDER_FRESHNESS_KEYS:
        if _stable_json(old_input.get(key)) != _stable_json(new_input.get(key)):
            return True
    return False


def _node_render_state(node: WorkflowNode) -> str | None:
    if node.type != "image":
        return None
    input_data = _parse_json_dict(node.input_json)
    raw = input_data.get("render_state")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    if node.status == "completed" and node.output_json:
        return "fresh"
    return None


def _node_creator(node: WorkflowNode) -> str:
    model_config = _parse_json_dict(node.model_config_json)
    input_data = _parse_json_dict(node.input_json)
    raw = (
        model_config.get("_ui_creator")
        or model_config.get("created_by")
        or input_data.get("_ui_creator")
        or input_data.get("created_by")
        or "agent"
    )
    return "user" if raw == "user" else "agent"


def _node_detail_payload(node: WorkflowNode, id_map: dict[str, str] | None = None) -> dict[str, Any]:
    mapping = id_map or {}
    input_data = _strip_ui_private(_parse_json_dict(node.input_json))
    output_data = _parse_json_value(node.output_json)
    return {
        "id": node.id,
        "display_id": node.display_id,
        "project_id": node.project_id,
        "type": node.type,
        "title": node.title,
        "status": node.status,
        "position": {"x": node.position_x, "y": node.position_y},
        "input": publicize_node_refs(input_data, mapping) or None,
        "output": publicize_node_refs(output_data, mapping),
        "prompt": node.prompt,
        "render_state": _node_render_state(node),
        "error_message": node.error_message,
        "version": node.version,
        "supersedes_id": node.supersedes_id,
        "creator": _node_creator(node),
        "created_at": node.created_at.isoformat() if node.created_at else None,
        "updated_at": node.updated_at.isoformat() if node.updated_at else None,
    }


async def _public_id_map(project_id: str, db: AsyncSession) -> dict[str, str]:
    return await internal_to_public_id_map(db, project_id)


async def _node_detail_response(node: WorkflowNode, project_id: str, db: AsyncSession) -> dict[str, Any]:
    return _node_detail_payload(node, await _public_id_map(project_id, db))


_CHANGE_LABELS = {
    "title": "标题",
    "prompt": "提示词",
    "content": "内容",
    "description": "描述",
    "aspect_ratio": "画幅",
    "resolution": "分辨率",
    "quality": "质量",
    "clarity": "清晰度",
    "duration_seconds": "时长",
    "production_path": "制作方式",
    "reference_images": "引用图",
    "references": "参考",
    "depends_on": "依赖",
}


def _change_text(value: Any, limit: int = 800) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    elif isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False)
    else:
        text = str(value)
    return text[:limit]


def _node_update_changes(
    *,
    old_title: str,
    new_title: str,
    old_prompt: str,
    new_prompt: str,
    old_input: dict[str, Any],
    new_input: dict[str, Any],
) -> list[dict[str, str]]:
    changes: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(field: str, before: Any, after: Any, limit: int = 800) -> None:
        if field in seen:
            return
        if _change_text(before, limit) == _change_text(after, limit):
            return
        seen.add(field)
        changes.append({
            "field": field,
            "label": _CHANGE_LABELS.get(field, field),
            "before": _change_text(before, limit),
            "after": _change_text(after, limit),
        })

    for key in sorted(set(old_input) | set(new_input)):
        if key in {"title", "prompt", "_ui_creator", "created_by"}:
            continue
        add(key, old_input.get(key), new_input.get(key), 500)
    add("prompt", old_prompt, new_prompt, 800)
    add("title", old_title, new_title, 500)
    return changes


def _dependency_values(raw: Any) -> list[str]:
    if raw is None:
        return []
    items = raw if isinstance(raw, list) else [raw]
    values: list[str] = []
    for item in items:
        if item is None:
            continue
        text = _reference_value(item).strip()
        if text and text not in values:
            values.append(text)
    return values


def _reference_value(raw: Any) -> str:
    if isinstance(raw, dict):
        for key in ("ref", "reference", "reference_input", "node_id", "nodeId", "source_node_id", "sourceNodeId", "id", "value"):
            value = raw.get(key)
            if value is not None:
                text = str(value).strip()
                return (
                    f"node:{text}"
                    if key in {"node_id", "nodeId", "source_node_id", "sourceNodeId"} and text and not text.startswith("node:")
                    else text
                )
        return ""
    return str(raw or "").strip()


def _public_node_ref(node: WorkflowNode | str) -> str:
    if isinstance(node, WorkflowNode) and node.display_id is not None:
        return f"node:{node.display_id}"
    node_id = node.id if isinstance(node, WorkflowNode) else str(node)
    return f"node:{node_id}"


def _node_dependency_aliases(node: WorkflowNode | str) -> set[str]:
    node_id = node.id if isinstance(node, WorkflowNode) else str(node)
    aliases = {node_id, f"node:{node_id}", f"@{node_id}", f"@node:{node_id}"}
    display_id = node.display_id if isinstance(node, WorkflowNode) else None
    if display_id is not None:
        public_id = str(display_id).strip()
        aliases.update({
            public_id,
            f"#{public_id}",
            f"node:{public_id}",
            f"node:#{public_id}",
            f"@{public_id}",
            f"@#{public_id}",
            f"@node:{public_id}",
            f"@node:#{public_id}",
        })
    return aliases


def _reference_role(raw: Any) -> str:
    if not isinstance(raw, dict):
        return ""
    return (
        str(raw.get("role") or raw.get("usage") or raw.get("purpose") or "")
        .strip()
        .lower()
        .replace("-", "_")
    )


def _manual_edge_reference_role(source: WorkflowNode, target: WorkflowNode) -> str | None:
    if source.type == "image" and target.type in {"text", "image", "video", "audio"}:
        return "visual_reference"
    return None


def _add_edge_dependency(target: WorkflowNode, source: WorkflowNode | str) -> bool:
    source_node_id = source.id if isinstance(source, WorkflowNode) else source
    source_ref = _public_node_ref(source)
    input_data = _parse_json_dict(target.input_json)
    aliases = _node_dependency_aliases(source)

    def add_to_container(container: dict[str, Any]) -> bool:
        changed = False
        deps = _dependency_values(container.get("depends_on"))
        if not any(dep in aliases for dep in deps):
            deps.append(source_ref)
            container["depends_on"] = deps
            changed = True

        if isinstance(source, WorkflowNode):
            role = _manual_edge_reference_role(source, target)
            if role:
                refs = container.get("references")
                ref_items = refs if isinstance(refs, list) else ([refs] if refs else [])
                if not any(_reference_value(item) in aliases for item in ref_items):
                    ref_items.append({"ref": source_ref, "role": role})
                    container["references"] = ref_items
                    changed = True
                if target.type in {"text", "image", "video"}:
                    reference_images = _dependency_values(container.get("reference_images"))
                    if not any(ref in aliases for ref in reference_images):
                        reference_images.append(source_ref)
                        container["reference_images"] = reference_images
                        changed = True
        return changed

    changed = add_to_container(input_data)
    fields = input_data.get("fields")
    if isinstance(fields, dict) and any(key in fields for key in _DEPENDENCY_FIELD_KEYS):
        if add_to_container(fields):
            changed = True

    if changed and target.type == "image":
        input_data["render_state"] = "stale"
    if not changed:
        return False
    target.input_json = json.dumps(_strip_ui_private(input_data), ensure_ascii=False)
    target.updated_at = datetime.utcnow()
    return True


def _remove_edge_dependency(target: WorkflowNode, source: WorkflowNode | str) -> bool:
    input_data = _parse_json_dict(target.input_json)
    aliases = _node_dependency_aliases(source)

    def remove_from_container(container: dict[str, Any]) -> bool:
        changed = False
        if "depends_on" in container:
            deps_raw = container.get("depends_on")
            deps = deps_raw if isinstance(deps_raw, list) else ([deps_raw] if deps_raw else [])
            next_deps = [dep for dep in deps if _reference_value(dep) not in aliases]
            if next_deps != deps:
                container["depends_on"] = next_deps
                changed = True
        for key in ("references", "reference_images"):
            if key not in container:
                continue
            value = container.get(key)
            refs = value if isinstance(value, list) else ([value] if value else [])
            next_refs = [item for item in refs if _reference_value(item) not in aliases]
            if next_refs != refs:
                container[key] = next_refs
                changed = True
        return changed

    changed = remove_from_container(input_data)
    fields = input_data.get("fields")
    if isinstance(fields, dict):
        if remove_from_container(fields):
            changed = True
    if changed and target.type == "image":
        input_data["render_state"] = "stale"
    if not changed:
        return False
    target.input_json = json.dumps(_strip_ui_private(input_data), ensure_ascii=False)
    target.updated_at = datetime.utcnow()
    return True


def _media_operation_default_position(source: WorkflowNode | None) -> dict[str, float]:
    if source is None:
        return {"x": 160.0, "y": 160.0}
    return {
        "x": float(source.position_x or 0.0) + 380.0,
        "y": float(source.position_y or 0.0),
    }


def _media_operation_position_overlaps(
    position: dict[str, float],
    occupied: list[dict[str, float]],
) -> bool:
    return any(
        abs(position["x"] - item["x"]) < 320.0 and abs(position["y"] - item["y"]) < 240.0
        for item in occupied
    )


def _next_available_media_operation_position(
    position: dict[str, float],
    occupied: list[dict[str, float]],
) -> dict[str, float]:
    candidate = {"x": float(position["x"]), "y": float(position["y"])}
    for _ in range(24):
        if not _media_operation_position_overlaps(candidate, occupied):
            occupied.append(candidate)
            return candidate
        candidate = {"x": candidate["x"], "y": candidate["y"] + 260.0}
    occupied.append(candidate)
    return candidate


async def _create_dependency_edge(
    project_id: str,
    source: WorkflowNode,
    target: WorkflowNode,
    db: AsyncSession,
    *,
    label: str | None = None,
) -> WorkflowEdge:
    existing = (await db.exec(
        select(WorkflowEdge).where(
            WorkflowEdge.project_id == project_id,
            WorkflowEdge.source_node_id == source.id,
            WorkflowEdge.target_node_id == target.id,
        )
    )).first()
    if existing is None:
        existing = WorkflowEdge(
            id=str(uuid.uuid4()),
            project_id=project_id,
            source_node_id=source.id,
            target_node_id=target.id,
            label=label,
            created_at=datetime.utcnow(),
        )
        db.add(existing)
    if _add_edge_dependency(target, source):
        db.add(target)
    await db.commit()
    await db.refresh(existing)
    return existing


async def _create_media_operation_node(
    project_id: str,
    result: media_operations.MediaOperationFile,
    *,
    source_nodes: list[WorkflowNode],
    position: dict[str, float],
    db: AsyncSession,
) -> WorkflowNode:
    output = media_operations.item_output(project_id, result)
    source_refs = [_public_node_ref(source) for source in source_nodes]
    input_json: dict[str, Any] = {
        "surface": "media_operation",
        "title": result.title,
        "source": {
            "kind": "media_operation",
            "operation": result.metadata.get("type"),
            "source_node_ids": [source.id for source in source_nodes],
        },
        "depends_on": source_refs,
        "fields": {
            "media_operation": result.metadata,
            "source_node_refs": source_refs,
        },
    }
    if result.kind == "image":
        input_json["render_state"] = "fresh"

    svc = NodeService(db)
    node = await svc.create_node(
        project_id,
        {
            "type": result.kind,
            "title": result.title,
            "status": "completed",
            "position_x": position["x"],
            "position_y": position["y"],
            "input_json": input_json,
            "output_json": output,
            "model_config_json": {
                "surface": "media_operation",
                "_ui_creator": "user",
                "created_by": "user",
            },
            "prompt": None,
            "error_message": None,
        },
    )
    project_media_history.register_node_outputs(project_id, node)
    return node


def _session_clear_state_patch(state: dict, *, cleared_at: str) -> tuple[dict, int]:
    """Build a model-context clear patch without touching canvas artifacts."""
    memory = state.get("memory") if isinstance(state.get("memory"), dict) else {}
    facts = memory.get("facts") if isinstance(memory.get("facts"), list) else []
    pinned_facts = [
        fact for fact in facts
        if isinstance(fact, dict) and fact.get("pinned")
    ]
    next_memory = dict(memory) if isinstance(memory, dict) else {}
    next_memory["facts"] = pinned_facts
    return (
        {
            "session": {},
            "guide_loaded": {},
            "_mentor_guides_loaded": {},
            "_skills_loaded": {},
            "_last_template_lookup": None,
            "_last_agent_review": None,
            ACTIVE_WORKFLOW_STATE_KEY: None,
            "workflow_runtime": {},
            "workflow_input_values": {},
            "memory": next_memory,
            "agent_token_usage": None,
            "context_cleared_at": cleared_at,
        },
        max(0, len(facts) - len(pinned_facts)),
    )


async def _archive_active_project_messages(db: AsyncSession, project_id: str) -> int:
    result = await db.exec(
        select(Message).where(
            Message.project_id == project_id,
            Message.archived == False,  # noqa: E712
        )
    )
    messages = list(result.all())
    for message in messages:
        message.archived = True
        db.add(message)
    return len(messages)


@router.post("")
async def create_project(
    req: CreateProjectRequest, db: AsyncSession = Depends(get_session)
):
    svc = ProjectService(db)
    project = await svc.create_project(
        title=req.title,
        description=req.description,
        genre=req.genre,
        format=req.format,
        episode_count=req.episode_count,
        duration_per_episode=req.duration_per_episode,
        budget_level=req.budget_level or "low",
    )
    return project.model_dump()


@router.get("")
async def list_projects(db: AsyncSession = Depends(get_session)):
    svc = ProjectService(db)
    projects = await svc.list_projects()
    return [p.model_dump() for p in projects]


@router.get("/{project_id}")
async def get_project(project_id: str, db: AsyncSession = Depends(get_session)):
    svc = ProjectService(db)
    project = await svc.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project.model_dump()


@router.patch("/{project_id}")
async def update_project(
    project_id: str,
    req: UpdateProjectRequest,
    db: AsyncSession = Depends(get_session),
):
    svc = ProjectService(db)
    patch = req.model_dump(exclude_none=True)
    project = await svc.update_project(
        project_id, patch
    )
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if "title" in patch:
        await svc.update_project_state(project_id, {"metadata.title": project.title})
        project = await svc.get_project(project_id) or project
    return project.model_dump()


@router.delete("/{project_id}")
async def delete_project(project_id: str, db: AsyncSession = Depends(get_session)):
    svc = ProjectService(db)
    ok = await svc.delete_project(project_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Project not found")
    return {"status": "deleted"}


@router.get("/{project_id}/state")
async def get_project_state(
    project_id: str, db: AsyncSession = Depends(get_session)
):
    svc = ProjectService(db)
    state = await svc.get_project_state(project_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return state


@router.post("/{project_id}/session/clear")
async def clear_project_session(
    project_id: str, db: AsyncSession = Depends(get_session)
) -> dict[str, object]:
    """Clear model-visible session context without touching canvas nodes/assets."""
    svc = ProjectService(db)
    state = await svc.get_project_state(project_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Project not found")

    cleared_at = datetime.utcnow().isoformat()
    patch, removed_memory_facts = _session_clear_state_patch(state, cleared_at=cleared_at)
    project = await svc.update_project_state(project_id, patch)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    archived_messages = await _archive_active_project_messages(db, project_id)
    try:
        from app.agent.task_graph import task_graph

        cleared_tasks = int(task_graph.clear_project(project_id))
    except Exception:
        cleared_tasks = 0
    await db.commit()
    return {
        "ok": True,
        "project_id": project_id,
        "cleared": [
            "messages",
            "session",
            "guide_loaded",
            "_mentor_guides_loaded",
            "_skills_loaded",
            "_last_template_lookup",
            "_last_agent_review",
            "active_workflow",
            "workflow_runtime",
            "workflow_input_values",
            "memory.facts",
            "task_graph",
            "agent_token_usage",
        ],
        "archived_messages": archived_messages,
        "cleared_tasks": cleared_tasks,
        "removed_memory_facts": removed_memory_facts,
        "context_cleared_at": cleared_at,
    }


@router.get("/{project_id}/messages")
async def list_messages(project_id: str, db: AsyncSession = Depends(get_session)):
    result = await db.exec(
        select(Message)
        .where(
            Message.project_id == project_id,
            Message.archived == False,  # noqa: E712
        )
        .order_by(Message.created_at)
    )
    return [m.model_dump() for m in result.all()]


@router.get("/{project_id}/nodes")
async def list_project_nodes(
    project_id: str, db: AsyncSession = Depends(get_session)
):
    await cleanup_interrupted_media_nodes(project_id=project_id)
    svc = NodeService(db)
    nodes = await svc.list_nodes(project_id)
    edges = await svc.list_canvas_edges(project_id, nodes=nodes)
    return {
        "nodes": [workflow_node_payload(n) for n in nodes],
        "edges": edges,
    }


@router.get("/{project_id}/workflow/templates")
async def list_project_workflow_templates(
    project_id: str,
    db: AsyncSession = Depends(get_session),
):
    svc = ProjectService(db)
    state = await svc.get_project_state(project_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Project not found")
    try:
        templates = canvas_workflow_templates.list_template_summaries()
    except canvas_workflow_templates.WorkflowTemplateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    active_workflow = _project_active_workflow_payload(project_id, state)
    workflow_id = _workflow_id_from_active_payload(active_workflow)
    active_runtime = _project_workflow_runtime_payload(state, workflow_id)
    active_runtimes = _project_workflow_runtime_payloads(state)
    runtime_workflow_id = workflow_id or str((active_runtime or {}).get("template_id") or "").strip()
    runtime_instance_id = str((active_runtime or {}).get("instance_id") or "").strip()
    return {
        "ok": True,
        "project_id": project_id,
        "templates": templates,
        "total": len(templates),
        "active_workflow": active_workflow,
        "active_workflow_runtime": active_runtime,
        "active_workflow_runtimes": active_runtimes,
        "workflow_input_values": workflow_tools.workflow_input_values_public_payload(
            state,
            workflow_id=runtime_workflow_id,
            instance_id=runtime_instance_id,
        ),
    }


@router.post("/{project_id}/workflow/templates")
async def save_project_workflow_template(
    project_id: str,
    req: ProjectWorkflowTemplateSaveRequest,
    db: AsyncSession = Depends(get_session),
):
    svc = ProjectService(db)
    state = await svc.get_project_state(project_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Project not found")
    workflow = req.workflow if isinstance(req.workflow, dict) else None
    if not workflow:
        raise HTTPException(status_code=400, detail="workflow is required")
    try:
        saved = workflow_template_store.save_user_template(
            workflow=workflow,
            template_id=req.template_id or "",
            name=req.name or "",
            description=req.description or "",
            category=req.category or "user",
            applies_to=req.applies_to or "",
            version=req.version or "",
            sample_inputs=req.inputs,
            source={
                "project_id": project_id,
                "source": "frontend_workflow_editor",
            },
            replace_existing=req.replace_existing,
        )
    except workflow_template_store.WorkflowTemplateStoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except WorkflowAuditError as exc:
        raise HTTPException(status_code=400, detail={"error": str(exc), "audit": exc.report}) from exc
    except canvas_workflow_templates.WorkflowTemplateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "ok": True,
        "project_id": project_id,
        **saved,
    }


@router.get("/{project_id}/workflow/templates/{template_id}/download")
async def download_project_workflow_template(
    project_id: str,
    template_id: str,
    version_id: str = Query(""),
    db: AsyncSession = Depends(get_session),
):
    svc = ProjectService(db)
    state = await svc.get_project_state(project_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Project not found")
    try:
        package = workflow_template_store.export_template_package(template_id, version_id)
    except workflow_template_store.WorkflowTemplateStoreError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "ok": True,
        "project_id": project_id,
        "template_id": package.get("template_id"),
        "version_id": package.get("version_id"),
        "filename": f"{package.get('template_id') or 'workflow_template'}.openreel-workflow-template.json",
        "package": package,
    }


@router.post("/{project_id}/workflow/templates/{template_id}/restore-builtin")
async def restore_project_builtin_workflow_template(
    project_id: str,
    template_id: str,
    db: AsyncSession = Depends(get_session),
):
    svc = ProjectService(db)
    state = await svc.get_project_state(project_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Project not found")
    try:
        builtin = canvas_workflow_templates.get_builtin_template(template_id)
        if not workflow_template_store.user_template_exists(template_id):
            raise HTTPException(status_code=409, detail="Template is already using the built-in version")
        deleted = workflow_template_store.delete_user_template(template_id)
        summary = next(
            item
            for item in canvas_workflow_templates.list_template_summaries()
            if str(item.get("id") or "") == str(builtin.get("id") or "")
        )
    except workflow_template_store.WorkflowTemplateStoreError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except canvas_workflow_templates.WorkflowTemplateError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "ok": True,
        "project_id": project_id,
        "template_id": builtin.get("id"),
        "restored_scope": "builtin",
        "summary": summary,
        "deleted_user_template": deleted,
    }


@router.put("/{project_id}/workflow/active")
async def set_project_active_workflow(
    project_id: str,
    req: ProjectWorkflowActiveRequest,
    db: AsyncSession = Depends(get_session),
):
    svc = ProjectService(db)
    state = await svc.get_project_state(project_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Project not found")
    if req.kind == "artifact":
        try:
            workflow_spec_artifacts.load_workflow_spec_artifact(project_id, req.artifact_ref or "")
        except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    active = _active_workflow_state_from_request(req)
    project = await svc.update_project_state(project_id, {ACTIVE_WORKFLOW_STATE_KEY: active})
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    refreshed_state = await svc.get_project_state(project_id) or {}
    active_workflow = _project_active_workflow_payload(project_id, refreshed_state)
    workflow_id = _workflow_id_from_active_payload(active_workflow)
    return {
        "ok": True,
        "project_id": project_id,
        "active_workflow": active_workflow,
        "active_workflow_runtime": _project_workflow_runtime_payload(refreshed_state, workflow_id),
        "active_workflow_runtimes": _project_workflow_runtime_payloads(refreshed_state),
    }


@router.post("/{project_id}/workflow/runtime/{instance_id}/pause")
async def pause_project_workflow_runtime(
    project_id: str,
    instance_id: str,
    req: ProjectWorkflowRuntimePauseRequest,
    db: AsyncSession = Depends(get_session),
):
    svc = ProjectService(db)
    state = await svc.get_project_state(project_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Project not found")
    result = await workflow_tools.workflow_runtime_request_pause(
        project_id,
        instance_id,
        template_id=req.template_id or "",
        reason=req.reason or "",
    )
    if result.get("ok") is False:
        raise HTTPException(status_code=400, detail=result)
    refreshed_state = await svc.get_project_state(project_id) or {}
    active_workflow = _project_active_workflow_payload(project_id, refreshed_state)
    workflow_id = _workflow_id_from_active_payload(active_workflow)
    return {
        **result,
        "active_workflow_runtime": _project_workflow_runtime_payload(refreshed_state, workflow_id),
        "active_workflow_runtimes": _project_workflow_runtime_payloads(refreshed_state),
    }


@router.delete("/{project_id}/workflow/runtime/{instance_id}")
async def delete_project_workflow_runtime(
    project_id: str,
    instance_id: str,
    db: AsyncSession = Depends(get_session),
):
    svc = ProjectService(db)
    state = await svc.get_project_state(project_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Project not found")
    result = await workflow_tools.workflow_runtime_delete_instance(project_id, instance_id)
    if result.get("ok") is False:
        raise HTTPException(status_code=400, detail=result)
    refreshed_state = await svc.get_project_state(project_id) or {}
    active_workflow = _project_active_workflow_payload(project_id, refreshed_state)
    workflow_id = _workflow_id_from_active_payload(active_workflow)
    return {
        **result,
        "active_workflow_runtime": _project_workflow_runtime_payload(refreshed_state, workflow_id),
        "active_workflow_runtimes": _project_workflow_runtime_payloads(refreshed_state),
    }


@router.post("/{project_id}/workflow/preview")
async def preview_project_workflow(
    project_id: str,
    req: ProjectWorkflowPreviewRequest,
):
    result = await workflow_tools.workflow_preview(
        project_id=project_id,
        template_id=req.template_id or "",
        workflow=req.workflow,
        artifact_ref=req.artifact_ref or "",
        instance_id=req.instance_id or "",
        inputs=req.inputs,
        context=req.context,
    )
    if result.get("ok") is False:
        raise HTTPException(status_code=400, detail=result)
    return result


@router.post("/{project_id}/workflow/materialize")
async def materialize_project_workflow(
    project_id: str,
    req: ProjectWorkflowMaterializeRequest,
):
    if req.workflow:
        result = await workflow_tools.workflow_materialize(
            project_id=project_id,
            workflow=req.workflow,
            title=req.title or "",
            inputs=req.inputs,
            context=req.context,
            origin_x=req.origin_x,
            origin_y=req.origin_y,
            spacing_x=req.spacing_x,
            spacing_y=req.spacing_y,
        )
    elif req.artifact_ref:
        result = await workflow_tools.workflow_materialize_artifact(
            project_id=project_id,
            artifact_ref=req.artifact_ref,
            title=req.title or "",
            inputs=req.inputs,
            context=req.context,
            origin_x=req.origin_x,
            origin_y=req.origin_y,
            spacing_x=req.spacing_x,
            spacing_y=req.spacing_y,
        )
    else:
        result = await workflow_tools.workflow_instantiate(
            project_id=project_id,
            template_id=req.template_id or "",
            title=req.title or "",
            inputs=req.inputs,
            origin_x=req.origin_x,
            origin_y=req.origin_y,
            spacing_x=req.spacing_x,
            spacing_y=req.spacing_y,
        )
    if result.get("ok") is False:
        raise HTTPException(status_code=400, detail=result)
    return result


@router.post("/{project_id}/workflow/run-step")
async def run_project_workflow_step(
    project_id: str,
    req: ProjectWorkflowRunStepRequest,
):
    result = await workflow_tools.workflow_run_step(
        project_id=project_id,
        step_id=req.step_id,
        template_id=req.template_id or "",
        workflow=req.workflow,
        artifact_ref=req.artifact_ref or "",
        title=req.title or "",
        inputs=req.inputs,
        context=req.context,
        ui_overrides=req.ui_overrides,
        instance_id=req.instance_id or "",
        origin_x=req.origin_x,
        origin_y=req.origin_y,
        spacing_x=req.spacing_x,
        spacing_y=req.spacing_y,
    )
    if result.get("ok") is False:
        raise HTTPException(status_code=400, detail=result)
    return result


@router.post("/{project_id}/workflow/run-next")
async def run_project_workflow_next_step(
    project_id: str,
    req: ProjectWorkflowRunNextRequest,
):
    result = await workflow_tools.workflow_run_next_step(
        project_id=project_id,
        template_id=req.template_id or "",
        workflow=req.workflow,
        artifact_ref=req.artifact_ref or "",
        title=req.title or "",
        inputs=req.inputs,
        context=req.context,
        ui_overrides=req.ui_overrides,
        instance_id=req.instance_id or "",
        origin_x=req.origin_x,
        origin_y=req.origin_y,
        spacing_x=req.spacing_x,
        spacing_y=req.spacing_y,
    )
    if result.get("ok") is False:
        raise HTTPException(status_code=400, detail=result)
    return result


@router.post("/{project_id}/workflow/run-all")
async def run_project_workflow_all_steps(
    project_id: str,
    req: ProjectWorkflowRunAllRequest,
):
    result = await workflow_tools.workflow_run_all_steps(
        project_id=project_id,
        template_id=req.template_id or "",
        workflow=req.workflow,
        artifact_ref=req.artifact_ref or "",
        title=req.title or "",
        inputs=req.inputs,
        context=req.context,
        ui_overrides=req.ui_overrides,
        instance_id=req.instance_id or "",
        origin_x=req.origin_x,
        origin_y=req.origin_y,
        spacing_x=req.spacing_x,
        spacing_y=req.spacing_y,
        max_steps=req.max_steps,
    )
    if result.get("ok") is False:
        raise HTTPException(status_code=400, detail=result)
    return result


@router.get("/{project_id}/media-history")
async def list_project_media_history(
    project_id: str,
    db: AsyncSession = Depends(get_session),
):
    return {"items": await _list_project_media_history_items(project_id, db)}


@router.post("/{project_id}/media-history/{item_id}/restore")
async def restore_project_media_history_item(
    project_id: str,
    item_id: str,
    req: ProjectMediaHistoryRestoreRequest,
    db: AsyncSession = Depends(get_session),
):
    item = await _find_project_media_history_item(project_id, item_id, db)
    if not item:
        raise HTTPException(status_code=404, detail="Media history item not found")
    kind = str(item.get("kind") or "").strip().lower()
    if kind not in {"image", "video", "audio"}:
        raise HTTPException(status_code=400, detail="Unsupported media history kind")
    rel_path = str(item.get("rel_path") or "")
    try:
        path = project_media_history.media_path_from_rel_path(project_id, rel_path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Media file not found")

    title = (req.title or str(item.get("title") or "")).strip() or {
        "image": "历史图片",
        "video": "历史视频",
        "audio": "历史音频",
    }[kind]
    prompt = str(item.get("prompt") or "").strip()
    async with node_display_id_allocation(project_id):
        node = WorkflowNode(
            id=str(uuid.uuid4()),
            project_id=project_id,
            display_id=await next_node_display_id(db, project_id),
            type=kind,
            title=title,
            status="completed",
            position_x=req.x,
            position_y=req.y,
            input_json=json.dumps({
                "surface": "media_history",
                "prompt": prompt,
                "source": {
                    "kind": "project_media_history",
                    "history_id": item.get("id"),
                    "rel_path": rel_path,
                },
            }, ensure_ascii=False),
            output_json=json.dumps(_media_output_for_history_item(item), ensure_ascii=False),
            model_config_json=json.dumps({"surface": "media_history", "_ui_creator": "user"}, ensure_ascii=False),
            prompt=prompt or None,
            error_message=None,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.add(node)
        await db.commit()
        await db.refresh(node)
    return {"ok": True, "node": await _node_detail_response(node, project_id, db)}


@router.delete("/{project_id}/media-history/{item_id}")
async def delete_project_media_history_item(
    project_id: str,
    item_id: str,
    db: AsyncSession = Depends(get_session),
):
    item = await _find_project_media_history_item(project_id, item_id, db)
    if not item:
        raise HTTPException(status_code=404, detail="Media history item not found")
    rel_path = str(item.get("rel_path") or "")
    try:
        path = project_media_history.media_path_from_rel_path(project_id, rel_path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    deleted = False
    if path.exists() and path.is_file():
        path.unlink()
        deleted = True
    project_media_history.remove_item(project_id, item_id)
    return {
        "ok": True,
        "id": item_id,
        "rel_path": rel_path,
        "deleted": deleted,
    }


@router.post("/{project_id}/nodes")
async def create_project_canvas_node(
    project_id: str,
    req: CanvasNodeCreateRequest,
    db: AsyncSession = Depends(get_session),
):
    node_type = req.type.strip().lower()
    if node_type not in {"text", "image", "video", "audio"}:
        raise HTTPException(status_code=400, detail="Node type must be text, image, video, or audio")

    title = req.title or {
        "text": "文本节点",
        "image": "图片节点",
        "video": "视频节点",
        "audio": "音频节点",
    }[node_type]
    svc = NodeService(db)
    node = await svc.create_node(
        project_id,
        {
            "type": node_type,
            "title": title,
            "status": "idle",
            "position_x": req.x,
            "position_y": req.y,
            "input_json": {
                "surface": "draft_canvas",
                "fields": {},
            },
            "model_config_json": {
                "surface": "draft_canvas",
                "_ui_creator": "user",
            },
        },
    )
    return node.model_dump()


@router.get("/{project_id}/nodes/{node_id}")
async def get_project_canvas_node_detail(
    project_id: str,
    node_id: str,
    db: AsyncSession = Depends(get_session),
):
    node = await db.get(WorkflowNode, node_id)
    if not node or node.project_id != project_id:
        raise HTTPException(status_code=404, detail="Node not found")
    return await _node_detail_response(node, project_id, db)


@router.patch("/{project_id}/nodes/{node_id}")
async def update_project_canvas_node_detail(
    project_id: str,
    node_id: str,
    req: CanvasNodeUpdateRequest,
    db: AsyncSession = Depends(get_session),
):
    node = await db.get(WorkflowNode, node_id)
    if not node or node.project_id != project_id:
        raise HTTPException(status_code=404, detail="Node not found")

    current_input = _parse_json_dict(node.input_json)
    old_input = _strip_ui_private(dict(current_input))
    had_dependency_fields = _has_dependency_fields(old_input)
    old_title = node.title or ""
    old_prompt = node.prompt or ""
    if req.input is not None:
        current_input.update(_strip_ui_private(dict(req.input)))

    fields_set = req.model_fields_set

    if "title" in fields_set and req.title is not None:
        title = req.title.strip()
        node.title = title or node.title
        if title:
            current_input["title"] = title

    if "prompt" in fields_set:
        prompt = (req.prompt or "").strip()
        node.prompt = prompt or None
        if prompt:
            current_input["prompt"] = prompt
        else:
            current_input.pop("prompt", None)

    if node.type == "image" and _image_render_inputs_changed(
        old_input,
        _strip_ui_private(dict(current_input)),
        old_prompt,
        node.prompt or "",
    ):
        current_input["render_state"] = "stale"

    next_input = _strip_ui_private(current_input)
    node.input_json = json.dumps(next_input, ensure_ascii=False)
    await refresh_node_reference_mentions(db, node)
    if node.type == "text" and "output" in fields_set:
        node.output_json = json.dumps(req.output, ensure_ascii=False) if req.output not in (None, "") else None
    node.updated_at = datetime.utcnow()
    db.add(node)
    await db.commit()
    await db.refresh(node)
    id_map = await _public_id_map(project_id, db)
    next_input_public = publicize_node_refs(_strip_ui_private(_parse_json_dict(node.input_json)), id_map)
    payload = _node_detail_payload(node, id_map)
    payload["changes"] = _node_update_changes(
        old_title=old_title,
        new_title=node.title or "",
        old_prompt=old_prompt,
        new_prompt=node.prompt or "",
        old_input=publicize_node_refs(old_input, id_map),
        new_input=next_input_public,
    )
    if had_dependency_fields or _has_dependency_fields(next_input):
        try:
            payload["edge_sync"] = await canvas_tools.sync_dependency_edges(project_id, node_id, next_input)
        except Exception as exc:
            payload["edge_sync_warning"] = str(exc)[:200]
    return payload


@router.post("/{project_id}/nodes/{node_id}/media")
async def upload_project_canvas_node_media(
    project_id: str,
    node_id: str,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_session),
):
    node = await db.get(WorkflowNode, node_id)
    if not node or node.project_id != project_id:
        raise HTTPException(status_code=404, detail="Node not found")
    if node.type not in {"image", "video"}:
        raise HTTPException(status_code=400, detail="Only image/video nodes support direct media upload")

    raw_name = Path(file.filename or "upload").name
    if not raw_name or raw_name in {".", ".."}:
        raise HTTPException(status_code=400, detail="Invalid filename")
    mime_type = file.content_type or mimetypes.guess_type(raw_name)[0]
    upload_kind = _classify_node_media_upload(raw_name, mime_type)
    if upload_kind != node.type:
        expected = "image" if node.type == "image" else "video"
        raise HTTPException(status_code=400, detail=f"Uploaded file must be a {expected}")

    try:
        root = project_media_history.project_root(project_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    media_dir = project_media_history.MEDIA_HISTORY_DIRS[node.type]
    target_dir = root / media_dir / "uploads"
    target_dir.mkdir(parents=True, exist_ok=True)
    filename = _safe_node_media_upload_filename(
        raw_name,
        node=node,
        kind=node.type,
        mime_type=mime_type,
    )
    target = (target_dir / filename).resolve()
    try:
        target.relative_to((root / media_dir).resolve())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Path outside media storage") from exc

    max_bytes = NODE_MEDIA_UPLOAD_MAX_BYTES[node.type]
    size = await _write_upload_file(target, file, max_bytes=max_bytes)
    rel_path = f"{media_dir}/uploads/{filename}"
    current_input = _parse_json_dict(node.input_json)
    current_output = _parse_json_value(node.output_json)
    uploaded_at = datetime.utcnow().isoformat()
    try:
        next_output = _build_uploaded_node_media_output(
            project_id=project_id,
            node=node,
            rel_path=rel_path,
            target_path=target,
            original_filename=raw_name,
            mime_type=mime_type,
            size=size,
            uploaded_at=uploaded_at,
            current_output=current_output,
            current_input=current_input,
        )
    except ValueError as exc:
        target.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    current_input["uploaded_output"] = {
        "kind": "user_upload",
        "rel_path": rel_path,
        "url": project_media_history.media_url(project_id, rel_path),
        "filename": raw_name,
        "mime_type": mime_type,
        "size": size,
        "uploaded_at": uploaded_at,
    }
    if node.type == "image":
        current_input["render_state"] = "fresh"

    node.input_json = json.dumps(_strip_ui_private(current_input), ensure_ascii=False)
    node.output_json = json.dumps(next_output, ensure_ascii=False)
    node.status = "completed"
    node.error_message = None
    node.updated_at = datetime.utcnow()
    db.add(node)
    await db.commit()
    await db.refresh(node)
    project_media_history.register_node_outputs(project_id, node)

    payload = await _node_detail_response(node, project_id, db)
    payload["uploaded_media"] = {
        "kind": node.type,
        "rel_path": rel_path,
        "url": project_media_history.media_url(project_id, rel_path),
        "filename": raw_name,
        "size": size,
        "mime_type": mime_type,
    }
    payload["changes"] = [{
        "field": "media_output",
        "label": "节点产物",
        "before": media_history.media_signature(current_output)[:800],
        "after": media_history.media_signature(next_output)[:800],
    }]
    return payload


@router.post("/{project_id}/nodes/{node_id}/history/switch")
async def switch_project_canvas_node_history(
    project_id: str,
    node_id: str,
    req: CanvasNodeHistorySwitchRequest,
    db: AsyncSession = Depends(get_session),
):
    node = await db.get(WorkflowNode, node_id)
    if not node or node.project_id != project_id:
        raise HTTPException(status_code=404, detail="Node not found")
    if node.type not in {"image", "video", "audio"}:
        raise HTTPException(status_code=400, detail="Only image/video/audio nodes have media history")

    current_output = _parse_json_value(node.output_json)
    current_input = _parse_json_dict(node.input_json)
    try:
        next_output, selected = media_history.switch_media_history_version(
            current_output,
            history_id=req.history_id,
            index=req.index,
            node_type=node.type,
            prompt=node.prompt or current_input.get("prompt"),
            input_data=current_input,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    before = media_history.media_signature(current_output)
    after = media_history.media_signature(next_output)
    selected_input = selected.get("input") if isinstance(selected.get("input"), dict) else None
    selected_prompt = media_history.prompt_from_state(
        selected.get("output"),
        selected_input,
        selected.get("prompt") if isinstance(selected.get("prompt"), str) else None,
    )
    next_input = _strip_ui_private(dict(selected_input or current_input))
    if selected_prompt:
        node.prompt = selected_prompt
        next_input["prompt"] = selected_prompt
    node.output_json = json.dumps(next_output, ensure_ascii=False)
    node.status = "completed"
    node.error_message = None
    if node.type == "image":
        next_input["render_state"] = "fresh"
    if next_input:
        node.input_json = json.dumps(_strip_ui_private(next_input), ensure_ascii=False)
    node.updated_at = datetime.utcnow()
    db.add(node)
    await db.commit()
    await db.refresh(node)

    payload = await _node_detail_response(node, project_id, db)
    payload["switched_history"] = {
        "id": selected.get("id"),
        "created_at": selected.get("created_at"),
    }
    if before != after:
        payload["changes"] = [{
            "field": "media_history",
            "label": "历史版本",
            "before": before[:800],
            "after": after[:800],
        }]
    return payload


@router.post("/{project_id}/media-operations")
async def run_project_media_operation(
    project_id: str,
    req: ProjectMediaOperationRequest,
    db: AsyncSession = Depends(get_session),
):
    async def load_node(node_id: str) -> WorkflowNode:
        node = await db.get(WorkflowNode, node_id)
        if not node or node.project_id != project_id:
            raise HTTPException(status_code=404, detail="Node not found")
        return node

    async def load_nodes(node_ids: list[str]) -> list[WorkflowNode]:
        unique_ids: list[str] = []
        for node_id in node_ids:
            text = str(node_id or "").strip()
            if text and text not in unique_ids:
                unique_ids.append(text)
        nodes: list[WorkflowNode] = []
        for node_id in unique_ids:
            nodes.append(await load_node(node_id))
        return nodes

    source_node: WorkflowNode | None = None
    source_nodes: list[WorkflowNode] = []
    try:
        if req.operation in {"video.export_frame", "video.split_tracks", "video.trim"}:
            if not req.source_node_id:
                raise HTTPException(status_code=400, detail="source_node_id is required")
            source_node = await load_node(req.source_node_id)
            source_nodes = [source_node]
        elif req.operation in {"video.concat", "audio.concat"}:
            source_nodes = await load_nodes(req.source_node_ids)
            if len(source_nodes) < 2:
                raise HTTPException(status_code=400, detail="source_node_ids must include at least two nodes")

        if req.operation == "video.export_frame":
            assert source_node is not None
            results = [
                await media_operations.export_video_frame(
                    project_id,
                    source_node,
                    mode=req.frame_mode,
                    time_seconds=req.time_seconds,
                    title=req.title,
                )
            ]
        elif req.operation == "video.split_tracks":
            assert source_node is not None
            results = await media_operations.split_video_tracks(project_id, source_node)
        elif req.operation == "video.trim":
            assert source_node is not None
            if req.range is None:
                raise HTTPException(status_code=400, detail="range is required")
            results = [
                await media_operations.trim_video(
                    project_id,
                    source_node,
                    start_seconds=req.range.start_seconds,
                    end_seconds=req.range.end_seconds,
                    title=req.title,
                )
            ]
        elif req.operation == "video.concat":
            results = [await media_operations.concat_video(project_id, source_nodes, title=req.title)]
        elif req.operation == "audio.concat":
            results = [await media_operations.concat_audio(project_id, source_nodes, title=req.title)]
        else:
            raise HTTPException(status_code=400, detail="Unsupported operation")
    except media_operations.MediaOperationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    base_position = (
        {"x": req.position.x, "y": req.position.y}
        if req.position
        else _media_operation_default_position(source_node or (source_nodes[0] if source_nodes else None))
    )
    existing_nodes = (await db.exec(
        select(WorkflowNode).where(WorkflowNode.project_id == project_id)
    )).all()
    occupied_positions = [
        {"x": float(node.position_x or 0.0), "y": float(node.position_y or 0.0)}
        for node in existing_nodes
    ]
    offset_step = 250.0
    center_offset = (len(results) - 1) / 2.0
    requested_positions = [
        {
            "x": base_position["x"],
            "y": base_position["y"] + (index - center_offset) * offset_step,
        }
        for index in range(len(results))
    ]
    same_column_y = [
        item["y"]
        for item in occupied_positions
        if abs(item["x"] - base_position["x"]) < 320.0
    ]
    if same_column_y and requested_positions:
        min_requested_y = min(item["y"] for item in requested_positions)
        group_shift = max(0.0, max(same_column_y) + 260.0 - min_requested_y)
        if group_shift:
            requested_positions = [
                {"x": item["x"], "y": item["y"] + group_shift}
                for item in requested_positions
            ]
    created_nodes: list[WorkflowNode] = []
    created_edges: list[WorkflowEdge] = []
    for index, result in enumerate(results):
        requested_position = requested_positions[index]
        position = _next_available_media_operation_position(requested_position, occupied_positions)
        node = await _create_media_operation_node(
            project_id,
            result,
            source_nodes=source_nodes,
            position=position,
            db=db,
        )
        created_nodes.append(node)
        for source in source_nodes:
            created_edges.append(
                await _create_dependency_edge(
                    project_id,
                    source,
                    node,
                    db,
                    label="媒体派生",
                )
            )

    id_map = await _public_id_map(project_id, db)
    return {
        "ok": True,
        "operation": req.operation,
        "nodes": [_node_detail_payload(node, id_map) for node in created_nodes],
        "edges": [edge.model_dump() for edge in created_edges],
    }


@router.post("/{project_id}/nodes/{node_id}/image/edit")
async def edit_project_canvas_node_image(
    project_id: str,
    node_id: str,
    req: CanvasNodeImageEditRequest,
    db: AsyncSession = Depends(get_session),
):
    result = await image_operations.edit_image_node(
        project_id=project_id,
        node_id=node_id,
        operations=req.operations,
        action=req.action,
        source_ref=req.source_ref,
        candidate_ref=req.candidate_ref,
    )
    if result.get("ok") is False:
        raise HTTPException(status_code=400, detail=result)
    if str(result.get("action") or "").lower() == "commit":
        node = await db.get(WorkflowNode, node_id)
        if node and node.project_id == project_id:
            result["node"] = await _node_detail_response(node, project_id, db)
    return result


@router.post("/{project_id}/nodes/{node_id}/image/edit/cleanup")
async def cleanup_project_canvas_node_image_edit(
    project_id: str,
    node_id: str,
):
    result = await image_operations.cleanup_image_edit_temps(project_id=project_id, node_id=node_id)
    if result.get("ok") is False:
        raise HTTPException(status_code=400, detail=result)
    return result


@router.post("/{project_id}/nodes/{node_id}/image/curve-preview")
async def preview_project_canvas_node_image_curve(
    project_id: str,
    node_id: str,
    req: CanvasNodeImageCurvePreviewRequest,
):
    result = await image_operations.preview_curve_image_node(
        project_id=project_id,
        node_id=node_id,
        source_ref=req.source_ref,
        color=req.color,
        detail=req.detail,
        line_strength=req.line_strength,
        base_visibility=req.base_visibility,
    )
    if result.get("ok") is False:
        raise HTTPException(status_code=400, detail=result)
    return result


@router.post("/{project_id}/panorama/captures")
async def create_project_panorama_capture(
    project_id: str,
    req: CanvasPanoramaCaptureRequest,
    db: AsyncSession = Depends(get_session),
):
    header, sep, encoded = req.data_url.partition(",")
    if sep != "," or not header.startswith("data:image/"):
        raise HTTPException(status_code=400, detail="data_url must be an image data URL")
    media_type = header[5:].split(";", 1)[0].lower()
    ext = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/webp": ".webp",
    }.get(media_type)
    if not ext:
        raise HTTPException(status_code=400, detail="Unsupported image data URL type")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=400, detail="Invalid image data URL")
    if not raw or len(raw) > 20 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Image data is empty or too large")

    source = None
    if req.source_node_id:
        source = await db.get(WorkflowNode, req.source_node_id)
        if not source or source.project_id != project_id:
            raise HTTPException(status_code=404, detail="Source node not found")

    capture_dir = settings.storage_path_resolved / project_id / "generated_images" / "panorama_captures"
    capture_dir.mkdir(parents=True, exist_ok=True)
    filename = f"panorama-capture-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}{ext}"
    target = capture_dir / filename
    target.write_bytes(raw)
    local_url = f"/api/media/{project_id}/panorama_captures/{filename}"

    title = (req.title or "").strip() or {
        "single": "全景截图",
        "four": "全景四视角",
        "eight": "全景八视角",
    }.get((req.mode or "").strip().lower(), "全景截图")
    input_data = {
        "surface": "draft_canvas",
        "title": title,
        "prompt": title,
        "fields": {
            "panorama_capture": True,
            "capture_mode": req.mode,
            **({"references": [{"ref": source.display_id if source and source.display_id is not None else req.source_node_id, "role": "source_panorama"}]} if req.source_node_id else {}),
        },
        "render_state": "fresh",
        **({"references": [{"ref": source.display_id if source and source.display_id is not None else req.source_node_id, "role": "source_panorama"}]} if req.source_node_id else {}),
    }
    output = {
        "ok": True,
        "type": "image",
        "operation": "panorama_capture",
        "status": "completed",
        "url": local_url,
        "local_url": local_url,
        "mode": req.mode,
        "panorama_capture": True,
    }
    svc = NodeService(db)
    node = await svc.create_node(
        project_id,
        {
            "type": "image",
            "title": title,
            "status": "completed",
            "position_x": req.x,
            "position_y": req.y,
            "input_json": input_data,
            "output_json": output,
            "prompt": title,
            "model_config_json": {
                "surface": "draft_canvas",
                "_ui_creator": "user",
            },
        },
    )
    if source:
        edge = WorkflowEdge(
            id=str(uuid.uuid4()),
            project_id=project_id,
            source_node_id=source.id,
            target_node_id=node.id,
            label="全景截图",
        )
        db.add(edge)
    await db.commit()
    await db.refresh(node)
    return {
        "ok": True,
        "local_url": local_url,
        "node": await _node_detail_response(node, project_id, db),
    }


async def _delete_project_canvas_nodes(
    project_id: str,
    requested_node_ids: list[str],
    db: AsyncSession,
) -> dict[str, Any]:
    resolved_ids: list[str] = []
    for raw_id in requested_node_ids:
        text = str(raw_id or "").strip()
        if not text:
            continue
        resolved = await resolve_internal_node_id(db, project_id, text)
        if resolved and resolved not in resolved_ids:
            resolved_ids.append(resolved)
    if not resolved_ids:
        raise HTTPException(status_code=404, detail="Node not found")

    node_result = await db.exec(
        select(WorkflowNode).where(
            WorkflowNode.project_id == project_id,
            WorkflowNode.id.in_(resolved_ids),
        )
    )
    nodes = list(node_result.all())
    if not nodes:
        raise HTTPException(status_code=404, detail="Node not found")

    found_ids = [node.id for node in nodes]
    if not found_ids:
        raise HTTPException(status_code=404, detail="Node not found")

    try:
        project_media_history.register_nodes_outputs(project_id, nodes)
    except Exception:
        # History registration is best-effort; node deletion must still work.
        pass

    edge_result = await db.exec(
        select(WorkflowEdge).where(
            WorkflowEdge.project_id == project_id,
            or_(
                WorkflowEdge.source_node_id.in_(found_ids),
                WorkflowEdge.target_node_id.in_(found_ids),
            ),
        )
    )
    edges = list(edge_result.all())
    for edge in edges:
        await db.delete(edge)

    remaining_result = await db.exec(
        select(WorkflowNode).where(
            WorkflowNode.project_id == project_id,
            ~WorkflowNode.id.in_(found_ids),
        )
    )
    remaining_nodes = list(remaining_result.all())
    cleaned_dependency_nodes = 0
    for target in remaining_nodes:
        changed = False
        for source in nodes:
            if _remove_edge_dependency(target, source):
                changed = True
        if changed:
            cleaned_dependency_nodes += 1
            db.add(target)

    asset_result = await db.exec(
        select(Asset).where(
            Asset.project_id == project_id,
            Asset.node_id.in_(found_ids),
        )
    )
    assets = list(asset_result.all())
    for asset in assets:
        await db.delete(asset)

    for node in nodes:
        await db.delete(node)
    await db.commit()
    return {
        "ok": True,
        "id": nodes[0].id if len(nodes) == 1 else None,
        "deleted_node_ids": [node.id for node in nodes],
        "deleted_nodes": len(nodes),
        "deleted_edges": len(edges),
        "deleted_asset_records": len(assets),
        "cleaned_dependency_nodes": cleaned_dependency_nodes,
    }


@router.post("/{project_id}/nodes/delete")
async def delete_project_canvas_nodes(
    project_id: str,
    req: CanvasNodesDeleteRequest,
    db: AsyncSession = Depends(get_session),
):
    return await _delete_project_canvas_nodes(project_id, req.node_ids, db)


@router.delete("/{project_id}/nodes/{node_id}")
async def delete_project_canvas_node(
    project_id: str,
    node_id: str,
    db: AsyncSession = Depends(get_session),
):
    return await _delete_project_canvas_nodes(project_id, [node_id], db)


@router.post("/{project_id}/canvas/restore-snapshot")
async def restore_project_canvas_snapshot(
    project_id: str,
    req: CanvasRestoreSnapshotRequest,
    db: AsyncSession = Depends(get_session),
):
    restored_nodes: list[str] = []
    restored_edges: list[str] = []
    now = datetime.utcnow()

    async with node_display_id_allocation(project_id):
        for item in req.nodes:
            node_type = item.type.strip().lower()
            if node_type not in {"text", "image", "video", "audio"}:
                continue
            position = item.position or {}
            model_config = {"surface": "draft_canvas", "_ui_creator": "user" if item.creator == "user" else "agent"}
            existing = await db.get(WorkflowNode, item.id)
            if existing and existing.project_id != project_id:
                continue
            node = existing or WorkflowNode(id=item.id, project_id=project_id, created_at=now)
            if node.display_id is None:
                node.display_id = await next_node_display_id(db, project_id)
            node.type = node_type
            node.title = item.title or {
                "text": "文本节点",
                "image": "图片节点",
                "video": "视频节点",
                "audio": "音频节点",
            }[node_type]
            node.status = item.status or "idle"
            node.position_x = float(position.get("x", 0.0) or 0.0)
            node.position_y = float(position.get("y", 0.0) or 0.0)
            node.input_json = _json_dumps_or_none(item.input)
            node.output_json = _json_dumps_or_none(item.output)
            node.model_config_json = _json_dumps_or_none(model_config)
            node.prompt = item.prompt
            node.error_message = item.error_message
            node.version = item.version or 1
            node.supersedes_id = item.supersedes_id
            node.updated_at = now
            db.add(node)
            restored_nodes.append(node.id)

        await db.flush()

        for item in req.edges:
            source_id = item.source_node_id or item.source
            target_id = item.target_node_id or item.target
            if not source_id or not target_id or source_id == target_id:
                continue
            source = await db.get(WorkflowNode, source_id)
            target = await db.get(WorkflowNode, target_id)
            if not source or not target or source.project_id != project_id or target.project_id != project_id:
                continue
            existing = None
            if item.id:
                existing = await db.get(WorkflowEdge, item.id)
                if existing and existing.project_id != project_id:
                    existing = None
            if not existing:
                existing = (await db.exec(
                    select(WorkflowEdge).where(
                        WorkflowEdge.project_id == project_id,
                        WorkflowEdge.source_node_id == source_id,
                        WorkflowEdge.target_node_id == target_id,
                    )
                )).first()
            edge = existing or WorkflowEdge(id=item.id or str(uuid.uuid4()), project_id=project_id, created_at=now)
            edge.source_node_id = source_id
            edge.target_node_id = target_id
            edge.label = item.label
            db.add(edge)
            if _add_edge_dependency(target, source):
                db.add(target)
            restored_edges.append(edge.id)

        await db.commit()
    return {"ok": True, "nodes": restored_nodes, "edges": restored_edges}


@router.patch("/{project_id}/nodes/{node_id}/position")
async def update_project_node_position(
    project_id: str,
    node_id: str,
    req: NodePositionRequest,
    db: AsyncSession = Depends(get_session),
):
    node = await db.get(WorkflowNode, node_id)
    if not node or node.project_id != project_id:
        raise HTTPException(status_code=404, detail="Node not found")
    node.position_x = req.x
    node.position_y = req.y
    node.updated_at = datetime.utcnow()
    db.add(node)
    await db.commit()
    await db.refresh(node)
    return {
        "ok": True,
        "id": node.id,
        "position": {"x": node.position_x, "y": node.position_y},
    }


@router.post("/{project_id}/edges")
async def create_project_edge(
    project_id: str,
    req: CanvasEdgeRequest,
    db: AsyncSession = Depends(get_session),
):
    if req.source_node_id == req.target_node_id:
        raise HTTPException(status_code=400, detail="Cannot connect a node to itself")
    source = await db.get(WorkflowNode, req.source_node_id)
    target = await db.get(WorkflowNode, req.target_node_id)
    if not source or not target or source.project_id != project_id or target.project_id != project_id:
        raise HTTPException(status_code=404, detail="Source or target node not found")

    existing = (await db.exec(
        select(WorkflowEdge).where(
            WorkflowEdge.project_id == project_id,
            WorkflowEdge.source_node_id == req.source_node_id,
            WorkflowEdge.target_node_id == req.target_node_id,
        )
    )).first()
    if existing:
        if _add_edge_dependency(target, source):
            db.add(target)
            await db.commit()
        return existing.model_dump()

    edge = WorkflowEdge(
        id=str(uuid.uuid4()),
        project_id=project_id,
        source_node_id=req.source_node_id,
        target_node_id=req.target_node_id,
        label=req.label,
    )
    db.add(edge)
    if _add_edge_dependency(target, source):
        db.add(target)
    await db.commit()
    await db.refresh(edge)
    return edge.model_dump()


@router.delete("/{project_id}/edges/{edge_id}")
async def delete_project_edge(
    project_id: str,
    edge_id: str,
    source_node_id: str | None = Query(default=None),
    target_node_id: str | None = Query(default=None),
    db: AsyncSession = Depends(get_session),
):
    edge = await db.get(WorkflowEdge, edge_id)
    source_id = ""
    target_id = ""
    deleted_edge_id: str | None = None
    deleted_edge_ids: list[str] = []

    if edge and edge.project_id == project_id:
        source_id = edge.source_node_id
        target_id = edge.target_node_id
        deleted_edge_id = edge.id
    elif edge:
        raise HTTPException(status_code=404, detail="Edge not found")
    else:
        source_id = (source_node_id or "").strip()
        target_id = (target_node_id or "").strip()
        if not source_id or not target_id:
            raise HTTPException(status_code=404, detail="Edge not found")
        existing = (await db.exec(
            select(WorkflowEdge).where(
                WorkflowEdge.project_id == project_id,
                WorkflowEdge.source_node_id == source_id,
                WorkflowEdge.target_node_id == target_id,
            )
        )).first()
        if existing:
            edge = existing
            deleted_edge_id = existing.id

    target = await db.get(WorkflowNode, target_id)
    if not target or target.project_id != project_id:
        raise HTTPException(status_code=404, detail="Target node not found")
    source = await db.get(WorkflowNode, source_id)
    if source and source.project_id != project_id:
        raise HTTPException(status_code=404, detail="Source node not found")

    dependency_removed = _remove_edge_dependency(target, source or source_id)
    if dependency_removed:
        db.add(target)
    edges_to_delete = list((await db.exec(
        select(WorkflowEdge).where(
            WorkflowEdge.project_id == project_id,
            WorkflowEdge.source_node_id == source_id,
            WorkflowEdge.target_node_id == target_id,
        )
    )).all())
    if edge is not None and edge not in edges_to_delete:
        edges_to_delete.append(edge)
    for item in edges_to_delete:
        deleted_edge_ids.append(item.id)
        await db.delete(item)
    await db.commit()
    return {
        "ok": True,
        "id": edge_id,
        "deleted_edge_id": deleted_edge_id,
        "deleted_edge_ids": deleted_edge_ids,
        "source_node_id": source_id,
        "target_node_id": target_id,
        "dependency_removed": dependency_removed,
    }


@router.get("/{project_id}/panel/layout")
async def get_project_panel_layout(project_id: str):
    try:
        return await panel_tools.panel_get_layout(project_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{project_id}/panel/layout")
async def set_project_panel_layout(project_id: str, req: PanelLayoutRequest):
    try:
        result = await panel_tools.panel_set_layout(project_id, mode=req.mode)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    return result
