"""Canvas MCP Tools — workflow nodes and edges."""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlmodel import select

from app.config import settings
from app.db.models import Asset, WorkflowEdge, WorkflowNode
from app.db.session import session_scope
from app.services.node_ids import next_node_display_id


def _as_json_str(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _as_dict(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


_UI_PRIVATE_KEYS = {"_ui_creator", "created_by"}


def _agent_visible_dict(raw) -> dict | None:
    data = _as_dict(raw)
    if not data:
        return None
    return {key: value for key, value in data.items() if key not in _UI_PRIVATE_KEYS}


def _reference_value(item: Any) -> str:
    if isinstance(item, dict):
        for key in ("ref", "reference", "reference_input", "node_id", "nodeId", "source_node_id", "sourceNodeId", "id", "value"):
            value = item.get(key)
            if value is not None:
                text = str(value).strip()
                return (
                    f"node:{text}"
                    if key in {"node_id", "nodeId", "source_node_id", "sourceNodeId"} and text and not text.startswith("node:")
                    else text
                )
        return ""
    return str(item or "").strip()


def _dependency_node_ids(
    input_data: dict[str, Any],
    node_ids: set[str],
    node_id_aliases: dict[str, str] | None = None,
) -> list[str]:
    raw_items: list[Any] = []
    containers = [input_data]
    fields = input_data.get("fields")
    if isinstance(fields, dict):
        containers.append(fields)

    for container in containers:
        for key in ("depends_on", "references", "reference_images"):
            value = container.get(key)
            if isinstance(value, list):
                raw_items.extend(value)
            elif value:
                raw_items.append(value)

    deps: list[str] = []
    for raw in raw_items:
        text = _reference_value(raw)
        if text.startswith("@"):
            text = text[1:]
        if text.startswith("node:"):
            text = text[5:]
        if text.startswith("#"):
            text = text[1:]
        if (
            not text
            or text.startswith(("asset:", "upload:", "http://", "https://"))
            or "/" in text
        ):
            continue
        if text not in node_ids:
            text = (node_id_aliases or {}).get(text) or text
        if text not in node_ids:
            continue
        if text not in deps:
            deps.append(text)
    return deps


def _has_dependency_keys(input_data: dict[str, Any]) -> bool:
    for container in (input_data, input_data.get("fields") if isinstance(input_data.get("fields"), dict) else {}):
        if any(key in container for key in ("depends_on", "references", "reference_images")):
            return True
    return False


def _extract_surface(model_config_json: str | None, input_json: str | None = None) -> str:
    model_config = _as_dict(model_config_json)
    surface = model_config.get("surface") or model_config.get("_surface")
    if surface in {"project_panel", "draft_canvas"}:
        return surface
    # Compatibility for any early experimental writes that used input metadata.
    input_data = _as_dict(input_json)
    surface = input_data.get("surface") or input_data.get("_surface")
    if surface in {"project_panel", "draft_canvas"}:
        return surface
    return "draft_canvas"


async def create_node(
    project_id: str,
    node_type: str,
    title: str,
    position_x: float = 0,
    position_y: float = 0,
    input_data: dict | None = None,
    model_config: dict | None = None,
    prompt: str | None = None,
) -> dict:
    async with session_scope() as session:
        now = datetime.utcnow()
        model_config = dict(model_config or {})
        model_config.setdefault("_ui_creator", "agent")
        node = WorkflowNode(
            id=str(uuid.uuid4()),
            project_id=project_id,
            display_id=await next_node_display_id(session, project_id),
            type=node_type,
            title=title,
            status="idle",
            position_x=position_x,
            position_y=position_y,
            input_json=_as_json_str(input_data),
            model_config_json=_as_json_str(model_config),
            prompt=prompt,
            version=1,
            created_at=now,
            updated_at=now,
        )
        session.add(node)
        await session.commit()
        await session.refresh(node)
        return {
            "id": node.id,
            "display_id": node.display_id,
            "type": node.type,
            "title": node.title,
            "status": node.status,
            "position": {"x": node.position_x, "y": node.position_y},
            "surface": _extract_surface(node.model_config_json, node.input_json),
            "prompt": node.prompt,
        }


async def update_node(node_id: str, patch: dict | str) -> dict:
    if isinstance(patch, str):
        try:
            patch = json.loads(patch)
        except (json.JSONDecodeError, TypeError):
            return {"error": "patch must be a JSON object"}
    if not isinstance(patch, dict):
        return {"error": "patch must be a dict"}

    # 别名兼容:调用方常用 output_data / input_data / model_config 这些"自然"字段名,
    # 但 DB 列叫 *_json。统一映射,避免静默丢失产物(老 bug:output_data 写不进库)。
    aliases = {
        "output_data": "output_json",
        "input_data": "input_json",
        "model_config": "model_config_json",
    }
    patch = {aliases.get(k, k): v for k, v in patch.items()}

    async with session_scope() as session:
        node = await session.get(WorkflowNode, node_id)
        if not node:
            raise ValueError(f"Node {node_id} not found")
        json_fields = {"input_json", "output_json", "model_config_json"}
        for key, value in patch.items():
            if not hasattr(node, key):
                continue
            if key in json_fields:
                if key == "model_config_json":
                    existing_creator = _as_dict(node.model_config_json).get("_ui_creator")
                    next_model_config = _as_dict(value)
                    if existing_creator and "_ui_creator" not in next_model_config:
                        next_model_config["_ui_creator"] = existing_creator
                    if "_ui_creator" not in next_model_config:
                        next_model_config["_ui_creator"] = "agent"
                    value = next_model_config
                value = _as_json_str(value)
            setattr(node, key, value)
        node.updated_at = datetime.utcnow()
        session.add(node)
        await session.commit()
        await session.refresh(node)
        return {
            "id": node.id,
            "display_id": node.display_id,
            "status": node.status,
            "title": node.title,
            "prompt": node.prompt,
        }


async def connect_nodes(
    project_id: str,
    source_node_id: str,
    target_node_id: str,
    label: str | None = None,
) -> dict:
    async with session_scope() as session:
        existing = (await session.exec(
            select(WorkflowEdge).where(
                WorkflowEdge.project_id == project_id,
                WorkflowEdge.source_node_id == source_node_id,
                WorkflowEdge.target_node_id == target_node_id,
            )
        )).first()
        if existing is not None:
            return {
                "id": existing.id,
                "source": existing.source_node_id,
                "target": existing.target_node_id,
                "label": existing.label,
            }
        edge = WorkflowEdge(
            id=str(uuid.uuid4()),
            project_id=project_id,
            source_node_id=source_node_id,
            target_node_id=target_node_id,
            label=label,
            created_at=datetime.utcnow(),
        )
        session.add(edge)
        await session.commit()
        await session.refresh(edge)
        return {
            "id": edge.id,
            "source": edge.source_node_id,
            "target": edge.target_node_id,
            "label": edge.label,
        }


async def sync_dependency_edges(
    project_id: str,
    target_node_id: str,
    input_data: dict[str, Any] | None,
) -> dict[str, Any]:
    """Project node-authored dependencies into workflow_edges."""
    data = dict(input_data or {})
    if not project_id or not target_node_id or not _has_dependency_keys(data):
        return {"ok": True, "changed": False, "added_edges": [], "removed_edges": []}

    async with session_scope() as session:
        target = await session.get(WorkflowNode, target_node_id)
        if not target or target.project_id != project_id:
            return {"ok": False, "changed": False, "error": "target node not found"}

        node_rows = (await session.exec(
            select(WorkflowNode.id, WorkflowNode.display_id).where(WorkflowNode.project_id == project_id)
        )).all()
        node_ids = {str(node_id) for node_id, _display_id in node_rows}
        node_id_aliases: dict[str, str] = {}
        for row_node_id, display_id in node_rows:
            if display_id is None:
                continue
            text = str(display_id)
            node_id_aliases[text] = str(row_node_id)
            node_id_aliases[f"#{text}"] = str(row_node_id)
        desired_sources = [
            source_id for source_id in _dependency_node_ids(data, node_ids, node_id_aliases)
            if source_id != target_node_id
        ]
        desired_set = set(desired_sources)

        existing_rows = (await session.exec(
            select(WorkflowEdge).where(
                WorkflowEdge.project_id == project_id,
                WorkflowEdge.target_node_id == target_node_id,
            )
        )).all()
        existing_by_source: dict[str, WorkflowEdge] = {}

        added: list[dict[str, Any]] = []
        removed: list[dict[str, Any]] = []
        kept_sources: set[str] = set()

        for edge in existing_rows:
            if edge.source_node_id in desired_set and edge.source_node_id not in kept_sources:
                existing_by_source[edge.source_node_id] = edge
                kept_sources.add(edge.source_node_id)
                continue
            removed.append({
                "id": edge.id,
                "source_node_id": edge.source_node_id,
                "target_node_id": edge.target_node_id,
            })
            await session.delete(edge)

        for source_id in desired_sources:
            if source_id in existing_by_source:
                continue
            edge = WorkflowEdge(
                id=str(uuid.uuid4()),
                project_id=project_id,
                source_node_id=source_id,
                target_node_id=target_node_id,
                label=None,
                created_at=datetime.utcnow(),
            )
            session.add(edge)
            added.append({
                "id": edge.id,
                "source_node_id": edge.source_node_id,
                "target_node_id": edge.target_node_id,
            })

        if added or removed:
            await session.commit()
        return {
            "ok": True,
            "changed": bool(added or removed),
            "added_edges": added,
            "removed_edges": removed,
        }


_LINK_KEYS = (
    "episode_number",
    "segment_id",
    "segment_index",
    "shot_id",
    "shot_number",
    "scene_id",
    "character_name",
    "character_id",
    "tier",
    "parent_node_id",
)


def _extract_links(input_json: str | None) -> dict:
    """Pull out join keys from a node's input payload so the Agent can match
    nodes to characters / episodes / shots without reading every node's blob."""
    if not input_json:
        return {}
    try:
        data = json.loads(input_json)
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: data[k] for k in _LINK_KEYS if data.get(k) is not None}


def _summarize_output(output_json: str | None) -> str | None:
    """Cheap output preview for the LLM — first 160 chars of a string field
    or the type tag, no full payload."""
    if not output_json:
        return None
    try:
        data = json.loads(output_json)
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(data, dict):
        if data.get("type") == "fusion":
            stages = data.get("stages") or []
            stage_names = [
                s.get("name") for s in stages if isinstance(s, dict) and s.get("name")
            ]
            return f"fusion[{','.join(stage_names)}]" if stage_names else "fusion"
        for key in ("title", "name", "summary", "hook"):
            v = data.get(key)
            if isinstance(v, str) and v:
                return v[:160]
        if isinstance(data.get("type"), str):
            return f"type={data['type']}"
        keys = list(data.keys())[:5]
        return f"keys={keys}"
    if isinstance(data, list):
        return f"list[{len(data)}]"
    return None


def _render_state(node_type: str | None, input_json: str | None, output_json: str | None, status: str | None) -> str | None:
    if node_type != "image":
        return None
    input_data = _as_dict(input_json)
    state = input_data.get("render_state")
    if isinstance(state, str) and state.strip():
        return state.strip()
    if status == "completed" and output_json:
        return "fresh"
    return None


def _storage_project_roots(project_id: str) -> list[Path]:
    roots: list[Path] = []
    for raw in (
        getattr(settings, "STORAGE_DIR", "./storage"),
        getattr(settings, "STORAGE_PATH", "./storage"),
        Path(getattr(settings, "PROJECT_ROOT", ".")) / "storage",
        Path(getattr(settings, "PROJECT_ROOT", ".")) / "data" / "storage",
    ):
        try:
            root = Path(raw).expanduser().resolve() / project_id
        except (TypeError, ValueError):
            continue
        if root not in roots:
            roots.append(root)
    return roots


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _media_url_candidates(project_id: str, value: str) -> list[Path]:
    text = value.strip()
    candidates: list[Path] = []
    api_prefix = f"/api/media/{project_id}/"
    if text.startswith(api_prefix):
        filename = text[len(api_prefix):].lstrip("/")
        for root in _storage_project_roots(project_id):
            candidates.append((root / "generated_images" / filename).resolve())
        return candidates
    storage_prefix = f"/storage/{project_id}/"
    if text.startswith(storage_prefix):
        rel = text[len(f"/storage/{project_id}/"):].lstrip("/")
        for root in _storage_project_roots(project_id):
            candidates.append((root / rel).resolve())
    return candidates


def _path_candidates(project_id: str, value: Any) -> list[Path]:
    if not isinstance(value, str):
        return []
    text = value.strip()
    if not text or text.startswith(("http://", "https://", "data:")):
        return []
    candidates = _media_url_candidates(project_id, text)
    raw = Path(text).expanduser()
    if raw.is_absolute():
        candidates.append(raw.resolve())
    else:
        for root in _storage_project_roots(project_id):
            candidates.append((root / raw).resolve())
    return candidates


def _walk_output_paths(value: Any) -> list[str]:
    paths: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {
                "path",
                "local_path",
                "output_path",
                "image_path",
                "video_path",
                "file_path",
                "url",
                "local_url",
            } and isinstance(item, str):
                paths.append(item)
            elif isinstance(item, (dict, list)):
                paths.extend(_walk_output_paths(item))
    elif isinstance(value, list):
        for item in value:
            paths.extend(_walk_output_paths(item))
    return paths


def _asset_metadata(asset: Asset) -> dict[str, Any]:
    if not asset.metadata_json:
        return {}
    try:
        parsed = json.loads(asset.metadata_json)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _collect_node_owned_files(project_id: str, node: WorkflowNode, assets: list[Asset]) -> list[Path]:
    raw_values: list[Any] = []
    output = _as_dict(node.output_json)
    raw_values.extend(_walk_output_paths(output))
    for asset in assets:
        meta = _asset_metadata(asset)
        raw_values.extend([
            asset.path,
            asset.url,
            meta.get("local_path"),
            meta.get("local_url"),
            meta.get("url"),
        ])

    allowed_roots = _storage_project_roots(project_id)
    files: list[Path] = []
    seen: set[str] = set()
    for raw in raw_values:
        for candidate in _path_candidates(project_id, raw):
            if not any(_is_within(candidate, root) for root in allowed_roots):
                continue
            key = str(candidate)
            if key in seen:
                continue
            seen.add(key)
            files.append(candidate)
    return files


def _delete_files(paths: list[Path]) -> tuple[list[str], list[dict[str, str]]]:
    deleted: list[str] = []
    errors: list[dict[str, str]] = []
    for path in paths:
        try:
            if path.exists() and path.is_file():
                path.unlink()
                deleted.append(str(path))
        except Exception as exc:
            errors.append({"path": str(path), "error": str(exc)})
    return deleted, errors


async def list_nodes(project_id: str) -> list[dict]:
    async with session_scope() as session:
        result = await session.exec(
            select(WorkflowNode)
            .where(WorkflowNode.project_id == project_id)
            .order_by(WorkflowNode.created_at)
        )
        return [
            {
                "id": n.id,
                "display_id": n.display_id,
                "type": n.type,
                "title": n.title,
                "status": n.status,
                "position": {"x": n.position_x, "y": n.position_y},
                "version": n.version,
                "supersedes_id": n.supersedes_id,
                "prompt": n.prompt,
                "links": _extract_links(n.input_json),
                "surface": _extract_surface(n.model_config_json, n.input_json),
                "model_config": _agent_visible_dict(n.model_config_json),
                "render_state": _render_state(n.type, n.input_json, n.output_json, n.status),
                "output_summary": _summarize_output(n.output_json),
                "output": json.loads(n.output_json) if n.output_json else None,
                "error_message": n.error_message,
                "created_at": n.created_at.isoformat() if n.created_at else None,
                "updated_at": n.updated_at.isoformat() if n.updated_at else None,
            }
            for n in result.all()
        ]


async def get_node(node_id: str) -> dict:
    async with session_scope() as session:
        node = await session.get(WorkflowNode, node_id)
        if not node:
            return {"error": "Node not found"}
        return {
            "id": node.id,
            "display_id": node.display_id,
            "project_id": node.project_id,
            "type": node.type,
            "title": node.title,
            "status": node.status,
            "position": {"x": node.position_x, "y": node.position_y},
            "input": _agent_visible_dict(node.input_json),
            "output": json.loads(node.output_json) if node.output_json else None,
            "model_config": _agent_visible_dict(node.model_config_json),
            "surface": _extract_surface(node.model_config_json, node.input_json),
            "render_state": _render_state(node.type, node.input_json, node.output_json, node.status),
            "prompt": node.prompt,
            "error_message": node.error_message,
            "version": node.version,
            "supersedes_id": node.supersedes_id,
            "links": _extract_links(node.input_json),
            "created_at": node.created_at.isoformat() if node.created_at else None,
            "updated_at": node.updated_at.isoformat() if node.updated_at else None,
        }


async def delete_nodes(
    project_id: str,
    node_ids: list[str],
    *,
    delete_local_files: bool = True,
) -> dict:
    ids = [str(node_id).strip() for node_id in node_ids if str(node_id).strip()]
    ids = list(dict.fromkeys(ids))
    if not ids:
        return {"ok": True, "deleted_nodes": 0, "deleted_node_ids": [], "deleted_files": []}

    async with session_scope() as session:
        node_rows = (await session.exec(
            select(WorkflowNode).where(
                WorkflowNode.project_id == project_id,
                WorkflowNode.id.in_(ids),
            )
        )).all()
        if not node_rows:
            return {"error": "Node not found", "error_kind": "node_not_found"}
        found_ids = [node.id for node in node_rows]
        asset_rows = (await session.exec(
            select(Asset).where(
                Asset.project_id == project_id,
                Asset.node_id.in_(found_ids),
            )
        )).all()
        assets_by_node: dict[str, list[Asset]] = {}
        for asset in asset_rows:
            if asset.node_id:
                assets_by_node.setdefault(asset.node_id, []).append(asset)

        files_to_delete: list[Path] = []
        if delete_local_files:
            for node in node_rows:
                files_to_delete.extend(
                    _collect_node_owned_files(project_id, node, assets_by_node.get(node.id, []))
                )

        # remove edges referencing this node
        edges_stmt = select(WorkflowEdge).where(
            (WorkflowEdge.source_node_id.in_(found_ids))
            | (WorkflowEdge.target_node_id.in_(found_ids))
        )
        edge_rows = (await session.exec(edges_stmt)).all()
        for edge in edge_rows:
            await session.delete(edge)
        for asset in asset_rows:
            await session.delete(asset)
        for node in node_rows:
            await session.delete(node)
        await session.commit()

    deleted_files, file_errors = _delete_files(files_to_delete) if delete_local_files else ([], [])
    return {
        "ok": True,
        "id": found_ids[0] if len(found_ids) == 1 else None,
        "deleted_nodes": len(found_ids),
        "deleted_node_ids": found_ids,
        "deleted_edges": len(edge_rows),
        "deleted_asset_records": len(asset_rows),
        "deleted_files": deleted_files,
        "file_errors": file_errors,
    }


async def delete_node(node_id: str) -> dict:
    async with session_scope() as session:
        node = await session.get(WorkflowNode, node_id)
        if not node:
            return {"error": "Node not found", "error_kind": "node_not_found"}
        project_id = node.project_id
    return await delete_nodes(project_id, [node_id])


async def delete_canvas(
    project_id: str,
    scope: str = "selected",
    node_ids: list[str] | None = None,
) -> dict:
    normalized_scope = str(scope or "selected").strip().lower()
    if normalized_scope in {"all", "canvas", "clear_all"}:
        async with session_scope() as session:
            rows = (await session.exec(
                select(WorkflowNode.id).where(WorkflowNode.project_id == project_id)
            )).all()
        result = await delete_nodes(project_id, [str(node_id) for node_id in rows])
        result["scope"] = "all"
        return result

    ids = list(node_ids or [])
    if not ids:
        return {
            "ok": False,
            "error": "canvas.delete(scope='selected') requires node_ids",
            "error_kind": "missing_node_ids",
        }
    result = await delete_nodes(project_id, ids)
    result["scope"] = "selected"
    return result


async def list_edges(project_id: str) -> list[dict]:
    async with session_scope() as session:
        result = await session.exec(
            select(WorkflowEdge).where(WorkflowEdge.project_id == project_id)
        )
        return [
            {
                "id": e.id,
                "source": e.source_node_id,
                "target": e.target_node_id,
                "label": e.label,
            }
            for e in result.all()
        ]


async def layout_nodes(project_id: str) -> list[dict]:
    async with session_scope() as session:
        result = await session.exec(
            select(WorkflowNode)
            .where(WorkflowNode.project_id == project_id)
            .order_by(WorkflowNode.created_at)
        )
        nodes = list(result.all())
        updates = []
        for i, node in enumerate(nodes):
            node.position_x = 300
            node.position_y = 50 + i * 150
            session.add(node)
            updates.append(
                {"id": node.id, "position": {"x": node.position_x, "y": node.position_y}}
            )
        await session.commit()
        return updates


async def clear_all_nodes(project_id: str) -> dict:
    """Delete all nodes and edges for a project (reset canvas)."""
    return await delete_canvas(project_id=project_id, scope="all")


async def cleanup_test_nodes(project_id: str | None = None) -> dict:
    """Remove failed nodes that produced no output, plus orphaned edges.

    If project_id is None, scans all projects (use sparingly).
    Targets:
      - status=failed AND (output_json IS NULL OR output_json='') — never produced anything
      - edges referencing deleted nodes
    """
    async with session_scope() as session:
        node_query = select(WorkflowNode).where(WorkflowNode.status == "failed")
        if project_id:
            node_query = node_query.where(WorkflowNode.project_id == project_id)

        candidates = (await session.exec(node_query)).all()
        deleted_ids: list[str] = []
        for n in candidates:
            if n.output_json and n.output_json.strip() not in ("", "null", "{}"):
                continue
            deleted_ids.append(n.id)

        if not deleted_ids:
            return {"ok": True, "deleted_nodes": 0, "deleted_edges": 0}

        # Delete dependent edges first
        edge_query = select(WorkflowEdge).where(
            (WorkflowEdge.source_node_id.in_(deleted_ids))
            | (WorkflowEdge.target_node_id.in_(deleted_ids))
        )
        edges = (await session.exec(edge_query)).all()
        for e in edges:
            await session.delete(e)

        for n in candidates:
            if n.id in deleted_ids:
                await session.delete(n)

        await session.commit()
        return {
            "ok": True,
            "deleted_nodes": len(deleted_ids),
            "deleted_edges": len(edges),
            "ids": deleted_ids,
        }
