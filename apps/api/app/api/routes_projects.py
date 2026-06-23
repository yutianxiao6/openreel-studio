"""Project CRUD endpoints."""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import or_
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db.models import Message, WorkflowEdge, WorkflowNode
from app.db.session import get_session
from app.mcp_tools import canvas_tools, panel_tools
from app.services import media_history
from app.services.node_service import NodeService, workflow_node_payload
from app.services.project_service import DEFAULT_EPISODE_COUNT, ProjectService

router = APIRouter()


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


class CanvasNodeHistorySwitchRequest(BaseModel):
    history_id: Optional[str] = None
    index: Optional[int] = None


class CanvasEdgeRequest(BaseModel):
    source_node_id: str
    target_node_id: str
    label: Optional[str] = None


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


def _node_detail_payload(node: WorkflowNode) -> dict[str, Any]:
    return {
        "id": node.id,
        "project_id": node.project_id,
        "type": node.type,
        "title": node.title,
        "status": node.status,
        "position": {"x": node.position_x, "y": node.position_y},
        "input": _strip_ui_private(_parse_json_dict(node.input_json)) or None,
        "output": _parse_json_value(node.output_json),
        "prompt": node.prompt,
        "render_state": _node_render_state(node),
        "error_message": node.error_message,
        "version": node.version,
        "supersedes_id": node.supersedes_id,
        "creator": _node_creator(node),
        "created_at": node.created_at.isoformat() if node.created_at else None,
        "updated_at": node.updated_at.isoformat() if node.updated_at else None,
    }


_CHANGE_LABELS = {
    "title": "标题",
    "prompt": "提示词",
    "content": "内容",
    "description": "描述",
    "aspect_ratio": "画幅",
    "resolution": "分辨率",
    "quality": "质量",
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


def _node_dependency_aliases(node_id: str) -> set[str]:
    return {node_id, f"node:{node_id}"}


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
    input_data = _parse_json_dict(target.input_json)
    aliases = _node_dependency_aliases(source_node_id)

    def add_to_container(container: dict[str, Any]) -> bool:
        changed = False
        deps = _dependency_values(container.get("depends_on"))
        if not any(dep in aliases for dep in deps):
            deps.append(f"node:{source_node_id}")
            container["depends_on"] = deps
            changed = True

        if isinstance(source, WorkflowNode):
            role = _manual_edge_reference_role(source, target)
            if role:
                refs = container.get("references")
                ref_items = refs if isinstance(refs, list) else ([refs] if refs else [])
                if not any(_reference_value(item) in aliases for item in ref_items):
                    ref_items.append({"ref": f"node:{source_node_id}", "role": role})
                    container["references"] = ref_items
                    changed = True
                if target.type in {"text", "image", "video"}:
                    reference_images = _dependency_values(container.get("reference_images"))
                    if not any(ref in aliases for ref in reference_images):
                        reference_images.append(f"node:{source_node_id}")
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


def _remove_edge_dependency(target: WorkflowNode, source_node_id: str) -> bool:
    input_data = _parse_json_dict(target.input_json)
    aliases = _node_dependency_aliases(source_node_id)

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


def _session_clear_state_patch(state: dict, *, cleared_at: str) -> tuple[dict, int]:
    """Build a prompt-context clear patch without touching project artifacts."""
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
    """Clear model-visible chat context without touching blueprint/canvas."""
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
            "memory.facts",
            "agent_token_usage",
        ],
        "archived_messages": archived_messages,
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
    svc = NodeService(db)
    nodes = await svc.list_nodes(project_id)
    edges = await svc.list_canvas_edges(project_id, nodes=nodes)
    return {
        "nodes": [workflow_node_payload(n) for n in nodes],
        "edges": edges,
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
    return _node_detail_payload(node)


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
    node.updated_at = datetime.utcnow()
    db.add(node)
    await db.commit()
    await db.refresh(node)
    payload = _node_detail_payload(node)
    payload["changes"] = _node_update_changes(
        old_title=old_title,
        new_title=node.title or "",
        old_prompt=old_prompt,
        new_prompt=node.prompt or "",
        old_input=old_input,
        new_input=_strip_ui_private(_parse_json_dict(node.input_json)),
    )
    if had_dependency_fields or _has_dependency_fields(next_input):
        try:
            payload["edge_sync"] = await canvas_tools.sync_dependency_edges(project_id, node_id, next_input)
        except Exception as exc:
            payload["edge_sync_warning"] = str(exc)[:200]
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

    payload = _node_detail_payload(node)
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


@router.delete("/{project_id}/nodes/{node_id}")
async def delete_project_canvas_node(
    project_id: str,
    node_id: str,
    db: AsyncSession = Depends(get_session),
):
    node = await db.get(WorkflowNode, node_id)
    if not node or node.project_id != project_id:
        raise HTTPException(status_code=404, detail="Node not found")

    edge_result = await db.exec(
        select(WorkflowEdge).where(
            WorkflowEdge.project_id == project_id,
            or_(
                WorkflowEdge.source_node_id == node_id,
                WorkflowEdge.target_node_id == node_id,
            ),
        )
    )
    edges = list(edge_result.all())
    for edge in edges:
        if edge.source_node_id == node_id:
            target = await db.get(WorkflowNode, edge.target_node_id)
            if target and target.project_id == project_id and _remove_edge_dependency(target, node_id):
                db.add(target)
        await db.delete(edge)
    await db.delete(node)
    await db.commit()
    return {"ok": True, "id": node_id, "deleted_edges": len(edges)}


@router.post("/{project_id}/canvas/restore-snapshot")
async def restore_project_canvas_snapshot(
    project_id: str,
    req: CanvasRestoreSnapshotRequest,
    db: AsyncSession = Depends(get_session),
):
    restored_nodes: list[str] = []
    restored_edges: list[str] = []
    now = datetime.utcnow()

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

    dependency_removed = _remove_edge_dependency(target, source_id)
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
