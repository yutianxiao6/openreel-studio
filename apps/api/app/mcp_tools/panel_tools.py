"""Panel layout helpers — project-level overview view.

Panel and canvas read from the same `workflow_nodes` table. The panel just
reshapes the flat node list into a nested tier hierarchy (global / episodes /
segments / shots). Agent does not call panel.* to keep it in sync; the web UI
uses project panel REST endpoints, and node data remains the source of truth.

Legacy `panel.get_layout` / `panel.set_layout` registry entries are
unregistered. Keep these functions as internal API helpers.
"""
from __future__ import annotations

import json
from typing import Any

from sqlmodel import select

from app.agent.blueprint_tree import blueprint_exists, read_blueprint
from app.agent.panel_layout import bucket_nodes, episode_order
from app.db.models import Project, WorkflowNode
from app.db.session import session_scope


PANEL_MODES = ("tier", "type", "phase", "status")


async def _get_state(project_id: str) -> dict[str, Any]:
    async with session_scope() as session:
        project = await session.get(Project, project_id)
        if not project:
            raise ValueError(f"Project {project_id} not found")
        return json.loads(project.state_json or "{}")


async def _save_state(project_id: str, state: dict[str, Any]) -> None:
    async with session_scope() as session:
        project = await session.get(Project, project_id)
        if not project:
            raise ValueError(f"Project {project_id} not found")
        project.state_json = json.dumps(state, ensure_ascii=False)
        session.add(project)
        await session.commit()


def _ensure_panel(state: dict[str, Any]) -> dict[str, Any]:
    panel = state.get("panel_layout")
    if not isinstance(panel, dict):
        panel = {"mode": "tier"}
    panel.setdefault("mode", "tier")
    state["panel_layout"] = panel
    return panel


def _node_dict(n: WorkflowNode) -> dict[str, Any]:
    input_data = _parse_json(n.input_json)
    output_data = _parse_json(n.output_json)
    prompt = (
        n.prompt
        or input_data.get("prompt")
        or output_data.get("prompt")
        or ""
    )
    return {
        "id": n.id,
        "project_id": n.project_id,
        "type": n.type,
        "title": n.title,
        "status": n.status,
        "version": n.version,
        "supersedes_id": n.supersedes_id,
        "input_json": n.input_json,
        "output_json": n.output_json,
        "model_config_json": n.model_config_json,
        "preview": _preview_from_output(output_data),
        "prompt": prompt,
        "blueprint_node_id": input_data.get("blueprint_node_id") or output_data.get("blueprint_node_id"),
        "blueprint_id": input_data.get("blueprint_id") or output_data.get("blueprint_id"),
        "blueprint_source_paths": input_data.get("blueprint_source_paths") or output_data.get("blueprint_source_paths"),
        "source_ids": input_data.get("source_ids") or output_data.get("source_ids"),
        "created_at": n.created_at.isoformat() if n.created_at else None,
    }


def _parse_json(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _preview_from_output(data: dict[str, Any]) -> dict[str, Any] | None:
    if not data:
        return None
    if data.get("type") == "fusion" and isinstance(data.get("stages"), list):
        return {
            "type": "fusion",
            "subject": data.get("subject"),
            "stages": data.get("stages"),
        }
    if data.get("type") == "storyboard" or (isinstance(data.get("shots"), list) and data.get("mode")):
        return {
            "type": "storyboard",
            "mode": data.get("mode"),
            "shot_count": data.get("shot_count"),
            "shots": data.get("shots"),
            "url": data.get("url"),
            "local_url": data.get("local_url"),
            "remote_url": data.get("remote_url"),
        }
    image = data.get("image") if isinstance(data.get("image"), dict) else None
    if image and (image.get("url") or image.get("local_url") or image.get("remote_url")):
        return {
            "type": "image",
            "url": image.get("url"),
            "local_url": image.get("local_url"),
            "remote_url": image.get("remote_url"),
        }
    if data.get("url") or data.get("local_url") or data.get("remote_url"):
        return {
            "type": "image",
            "url": data.get("url"),
            "local_url": data.get("local_url"),
            "remote_url": data.get("remote_url"),
        }
    for key in ("summary", "prompt", "content"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return {"type": "text", key: value.strip()}
    return None


def _surface_of_node(n: WorkflowNode) -> str:
    model_config = _parse_json(n.model_config_json)
    surface = model_config.get("surface") or model_config.get("_surface")
    if surface in {"project_panel", "draft_canvas"}:
        return str(surface)
    input_data = _parse_json(n.input_json)
    surface = input_data.get("surface") or input_data.get("_surface")
    if surface in {"project_panel", "draft_canvas"}:
        return str(surface)
    return "project_panel"


async def _load_nodes(project_id: str) -> list[dict[str, Any]]:
    async with session_scope() as session:
        result = await session.exec(
            select(WorkflowNode)
            .where(WorkflowNode.project_id == project_id)
            .order_by(WorkflowNode.created_at)
        )
        return [
            _node_dict(n)
            for n in result.all()
            if _surface_of_node(n) != "draft_canvas"
        ]


def _flat_bucket(nodes: list[dict[str, Any]], key_fn) -> dict[str, list[dict[str, Any]]]:
    grid: dict[str, list[dict[str, Any]]] = {}
    for n in nodes:
        k = key_fn(n) or "其他"
        grid.setdefault(k, []).append(_flat_summary(n))
    return grid


def _flat_summary(n: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": n.get("id"),
        "title": n.get("title"),
        "type": n.get("type"),
        "status": n.get("status"),
        "version": n.get("version", 1),
        "supersedes_id": n.get("supersedes_id"),
        "created_at": n.get("created_at"),
        "preview": n.get("preview"),
        "prompt": n.get("prompt"),
        "blueprint_node_id": n.get("blueprint_node_id"),
        "blueprint_id": n.get("blueprint_id"),
        "blueprint_source_paths": n.get("blueprint_source_paths"),
        "source_ids": n.get("source_ids"),
    }


def _build_view(project_id: str, nodes: list[dict[str, Any]], mode: str) -> dict[str, Any]:
    blueprint_tree = _build_blueprint_tree_view(project_id, nodes)
    if mode == "tier":
        grid = bucket_nodes(nodes)
        return {
            "mode": "tier",
            "grid": grid,
            "blueprint_tree": blueprint_tree,
            "episode_order": episode_order(grid),
            "node_count": len(nodes),
        }
    if mode == "type":
        return {"mode": "type", "grid": _flat_bucket(nodes, lambda n: n.get("type")), "blueprint_tree": blueprint_tree, "node_count": len(nodes)}
    if mode == "status":
        return {"mode": "status", "grid": _flat_bucket(nodes, lambda n: n.get("status")), "blueprint_tree": blueprint_tree, "node_count": len(nodes)}
    if mode == "phase":
        return {"mode": "phase", "grid": _flat_bucket(nodes, _phase_of), "blueprint_tree": blueprint_tree, "node_count": len(nodes)}
    grid = bucket_nodes(nodes)
    return {"mode": "tier", "grid": grid, "blueprint_tree": blueprint_tree, "episode_order": episode_order(grid), "node_count": len(nodes)}


def _as_ref_ids(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    refs: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if not text:
            continue
        if text.startswith("@"):
            text = text[1:]
        if "/" in text:
            continue
        if text and text not in refs:
            refs.append(text)
    return refs


def _tree_node_view(node: dict[str, Any], canvas_by_bp: dict[str, dict[str, Any]]) -> dict[str, Any]:
    fields = node.get("fields") if isinstance(node.get("fields"), dict) else {}
    node_id = str(node.get("id") or "")
    references = _as_ref_ids(node.get("references") or fields.get("references"))
    depends_on = _as_ref_ids(node.get("depends_on") or fields.get("depends_on"))
    return {
        "id": node_id,
        "type": node.get("type"),
        "title": node.get("title"),
        "status": node.get("status"),
        "materialize": bool(node.get("materialize")),
        "content": node.get("content") or fields.get("content"),
        "description": node.get("description") or fields.get("description"),
        "prompt": node.get("prompt") or fields.get("prompt"),
        "fields": {
            key: value
            for key, value in fields.items()
            if key in {
                "purpose",
                "segment",
                "duration",
                "duration_seconds",
                "aspect_ratio",
                "resolution",
                "quality",
                "production_path",
                "prompt_status",
                "prompt_template",
            }
            and value not in (None, "", [], {})
        },
        "references": references,
        "depends_on": depends_on,
        "canvas_node": canvas_by_bp.get(node_id),
        "children": [
            _tree_node_view(child, canvas_by_bp)
            for child in (node.get("children") or [])
            if isinstance(child, dict)
        ],
    }


def _build_blueprint_tree_view(project_id: str, nodes: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not project_id or not blueprint_exists(project_id):
        return None
    doc = read_blueprint(project_id)
    root = doc.get("root") if isinstance(doc.get("root"), dict) else None
    if not root or not isinstance(root.get("children"), list) or not root.get("children"):
        return None
    canvas_by_bp = {
        str(node.get("blueprint_node_id")): _flat_summary(node)
        for node in nodes
        if node.get("blueprint_node_id")
    }
    root_view = _tree_node_view(root, canvas_by_bp)
    by_id: dict[str, dict[str, str]] = {}

    def walk(node: dict[str, Any]) -> None:
        node_id = str(node.get("id") or "")
        if node_id:
            by_id[node_id] = {
                "id": node_id,
                "title": str(node.get("title") or node_id),
                "type": str(node.get("type") or ""),
            }
        for child in node.get("children") or []:
            if isinstance(child, dict):
                walk(child)

    walk(root_view)
    return {
        "title": doc.get("title") or root.get("title"),
        "summary": doc.get("summary") or root.get("content"),
        "status": doc.get("status"),
        "tree_version": doc.get("tree_version"),
        "root": root_view,
        "by_id": by_id,
    }


_PHASE_MAP = {
    "project_setting": "setup", "outline": "story", "outline_generation": "story",
    "character": "character", "character_generation": "character",
    "character_image_prompt": "character", "character_reference_image": "character",
    "character_relationship": "character",
    "episode_script": "script", "script_generation": "script",
    "episode_review": "review", "script_review": "review",
    "episode_segment_plan": "segment", "segment": "segment",
    "scene": "scene", "scene_image": "scene", "scene_image_prompt": "scene",
    "panorama": "scene", "panorama_view": "scene",
    "shot_list": "shot", "storyboard_grid": "shot", "storyboard_generation": "shot", "shot": "shot",
    "shot_image_prompt": "image", "shot_reference_image": "image",
    "image_prompt_generation": "image", "image_generation": "image",
    "shot_first_frame": "image", "shot_last_frame": "image",
    "shot_video_prompt": "video", "shot_video_clip": "video",
    "video_prompt_generation": "video", "video_generation": "video",
    "episode_export": "export", "project_export": "export", "export": "export",
}


def _phase_of(n: dict[str, Any]) -> str:
    return _PHASE_MAP.get(n.get("type") or "", "其他")


async def panel_get_layout(project_id: str) -> dict[str, Any]:
    state = await _get_state(project_id)
    panel = _ensure_panel(state)
    nodes = await _load_nodes(project_id)
    return {"ok": True, **_build_view(project_id, nodes, panel.get("mode", "tier"))}


async def panel_set_layout(project_id: str, mode: str = "tier") -> dict[str, Any]:
    if mode not in PANEL_MODES:
        return {"error": f"mode 必须是 {list(PANEL_MODES)} 之一"}
    state = await _get_state(project_id)
    panel = _ensure_panel(state)
    panel["mode"] = mode
    await _save_state(project_id, state)
    nodes = await _load_nodes(project_id)
    return {"ok": True, **_build_view(project_id, nodes, mode)}
