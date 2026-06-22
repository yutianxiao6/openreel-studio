"""Blueprint materializer - deterministic text/image/video tree to canvas nodes.

The materializer must not reinterpret model-authored workflow structure. It walks
the tree, creates the same public node types, and preserves fields/references so
the Agent remains responsible for planning and prompt writing.
"""
from __future__ import annotations

import contextlib
import json
import logging
from typing import Any

from sqlalchemy import delete, select

from app.agent.blueprint_tree import read_blueprint, write_blueprint
from app.db.models import WorkflowEdge, WorkflowNode

async def materialize_story_node_from_active_blueprint(
    project_id: str,
    node_type: str,
    fields: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Semantic tree nodes are materialized at blueprint approval time."""
    return None


async def materialize_visual_prompt_node_from_active_blueprint(
    project_id: str,
    node_type: str,
    fields: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Semantic tree nodes are materialized at blueprint approval time."""
    return None

logger = logging.getLogger(__name__)

_MATERIALIZED_NODE_TYPES = {"text", "image", "video"}
_REPLACEMENT_DRAFT_KEY = "replacement_draft"


def _canvas_type(node: dict[str, Any]) -> str:
    node_type = str(node.get("type") or "")
    if node_type in _MATERIALIZED_NODE_TYPES:
        return node_type
    return ""


def _should_materialize(node: dict[str, Any]) -> bool:
    if not _canvas_type(node):
        return False
    if "materialize" in node:
        return bool(node.get("materialize"))
    return True


def _build_input(node: dict[str, Any], source_path: str = "") -> dict[str, Any]:
    node_type = str(node.get("type") or "")
    fields = node.get("fields") if isinstance(node.get("fields"), dict) else {}
    inp: dict[str, Any] = {
        "title": node.get("title", ""),
        "blueprint_node_id": node.get("id"),
        "blueprint_node_type": node_type,
    }
    if source_path:
        inp["blueprint_source_paths"] = [source_path]
        inp["source_blueprint_paths"] = [source_path]
    for key, value in fields.items():
        if value not in (None, "", [], {}):
            inp[key] = value
    for key in (
        "content",
        "description",
        "prompt",
        "negative_prompt",
        "resolution",
        "quality",
        "duration",
        "references",
        "depends_on",
        "episode_index",
        "segment_index",
        "episode_number",
        "segment_id",
        "shot_id",
        "source_path",
    ):
        value = node.get(key)
        if value not in (None, "", [], {}):
            inp.setdefault(key, value)
    if node_type == "text":
        inp.setdefault("content", node.get("content", ""))
    elif node_type == "image":
        inp.setdefault("description", node.get("description", ""))
    elif node_type == "video":
        inp.setdefault("description", node.get("description", ""))
    return inp


def _default_status(node: dict[str, Any], canvas_type: str) -> str:
    status = str(node.get("status") or "")
    if status in {"completed", "done"}:
        return "completed"
    if node.get("type") == "text":
        return "completed"
    return "idle"


async def materialize_blueprint(
    project_id: str,
    node_service: Any,
) -> dict[str, Any]:
    """Walk the tree and create canvas nodes.

    Returns {ok, created_count, nodes: [{blueprint_id, canvas_id, type}]}.
    """
    doc = read_blueprint(project_id)
    draft = doc.get(_REPLACEMENT_DRAFT_KEY) if isinstance(doc.get(_REPLACEMENT_DRAFT_KEY), dict) else None
    is_replacement = bool(
        isinstance(draft, dict)
        and draft.get("status") == "pending_review"
        and isinstance(draft.get("root"), dict)
    )
    source_doc = draft if is_replacement else doc
    root = source_doc["root"]

    created: list[dict[str, Any]] = []
    bp_to_canvas: dict[str, str] = {}
    created_canvas_ids: list[str] = []
    created_edges: set[tuple[str, str]] = set()
    deleted_old_count = 0

    async def _walk(node: dict[str, Any], parent_canvas_id: str | None, source_path: str) -> None:
        bp_id = node.get("id", "")
        if not _should_materialize(node):
            for index, child in enumerate(node.get("children") or []):
                if isinstance(child, dict):
                    await _walk(child, parent_canvas_id, f"{source_path}/children/{index}")
            return

        canvas_type = _canvas_type(node)
        inp = _build_input(node, source_path)

        canvas_node = await node_service.create_node(
            project_id=project_id,
            payload={
                "type": canvas_type,
                "title": node.get("title", ""),
                "status": _default_status(node, canvas_type),
                "input_json": inp,
            },
        )
        canvas_id = str(canvas_node.id)
        created_canvas_ids.append(canvas_id)
        bp_to_canvas[bp_id] = canvas_id
        created.append({
            "blueprint_id": bp_id,
            "canvas_id": canvas_id,
            "id": canvas_id,
            "type": canvas_type,
            "title": getattr(canvas_node, "title", None) or node.get("title", ""),
            "status": getattr(canvas_node, "status", None) or _default_status(node, canvas_type),
        })

        if parent_canvas_id:
            await _create_edge(node_service, project_id, parent_canvas_id, canvas_id, created_edges)

        for index, child in enumerate(node.get("children") or []):
            if isinstance(child, dict):
                await _walk(child, canvas_id, f"{source_path}/children/{index}")

    try:
        # Process root.children — all top-level siblings are parallel
        for index, child in enumerate(root.get("children") or []):
            if isinstance(child, dict):
                await _walk(child, None, f"/root/children/{index}")

        # Cross-branch references/dependencies: add blocking edges from upstream nodes to referrer
        for entry in created:
            bp_id = entry["blueprint_id"]
            target = _find_in_tree(root, bp_id)
            if not target:
                continue
            relation_ids = []
            for key in ("references", "depends_on"):
                value = target.get(key)
                if not isinstance(value, list):
                    fields = target.get("fields") if isinstance(target.get("fields"), dict) else {}
                    value = fields.get(key) if isinstance(fields.get(key), list) else []
                relation_ids.extend(value)
            for ref in _dedupe_relation_ids(relation_ids):
                if isinstance(ref, str) and ref.startswith("@"):
                    ref_bp_id = ref[1:]
                    if "/" in ref_bp_id:
                        continue
                    ref_canvas_id = bp_to_canvas.get(ref_bp_id)
                    referrer_canvas_id = bp_to_canvas.get(bp_id)
                    if ref_canvas_id and referrer_canvas_id and ref_canvas_id != referrer_canvas_id:
                        await _create_edge(
                            node_service,
                            project_id,
                            ref_canvas_id,
                            referrer_canvas_id,
                            created_edges,
                        )
    except Exception:
        await _cleanup_created_nodes(node_service, project_id, created_canvas_ids)
        raise

    if is_replacement:
        try:
            deleted_old_count = await _delete_existing_blueprint_nodes(
                node_service,
                project_id,
                exclude_node_ids=set(created_canvas_ids),
            )
        except Exception:
            await _cleanup_created_nodes(node_service, project_id, created_canvas_ids)
            raise

        doc["previous_blueprint"] = {
            "tree_version": doc.get("tree_version"),
            "status": doc.get("status"),
            "title": doc.get("title") or "",
            "summary": doc.get("summary") or "",
            "replaced_at": source_doc.get("updated_at") or "",
            "replace_reason": source_doc.get("replace_reason") or "",
            "replaces": source_doc.get("replaces") if isinstance(source_doc.get("replaces"), dict) else {},
        }
        doc["root"] = root
        doc["title"] = source_doc.get("title") or root.get("title") or doc.get("title")
        doc["summary"] = source_doc.get("summary") or root.get("content") or ""
        doc["source_request"] = source_doc.get("source_request") or doc.get("source_request") or ""
        doc.pop(_REPLACEMENT_DRAFT_KEY, None)

    doc["status"] = "materialized"
    tree_version = write_blueprint(project_id, doc)

    logger.info("materialize_blueprint: %s — %d nodes", project_id, len(created))
    return {
        "ok": True,
        "created_count": len(created),
        "nodes": created,
        "replacement": is_replacement,
        "deleted_old_count": deleted_old_count,
        "tree_version": tree_version,
        "title": doc.get("title") or "",
        "summary": doc.get("summary") or "",
    }


def _dedupe_relation_ids(values: list[Any]) -> list[Any]:
    seen: set[str] = set()
    result: list[Any] = []
    for value in values:
        key = str(value)
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def _find_in_tree(node: dict[str, Any], target_id: str) -> dict[str, Any] | None:
    if node.get("id") == target_id:
        return node
    for child in (node.get("children") or []):
        if isinstance(child, dict):
            found = _find_in_tree(child, target_id)
            if found is not None:
                return found
    return None


async def _create_edge(
    node_service: Any,
    project_id: str,
    from_node_id: str,
    to_node_id: str,
    seen: set[tuple[str, str]] | None = None,
) -> None:
    if not from_node_id or not to_node_id or from_node_id == to_node_id:
        return
    key = (from_node_id, to_node_id)
    if seen is not None:
        if key in seen:
            return
        seen.add(key)
    await node_service.create_edge(
        project_id=project_id,
        source_node_id=from_node_id,
        target_node_id=to_node_id,
    )


async def _cleanup_created_nodes(
    node_service: Any,
    project_id: str,
    node_ids: list[str],
) -> None:
    if not node_ids:
        return
    try:
        await node_service.db.execute(
            delete(WorkflowEdge).where(
                WorkflowEdge.project_id == project_id,
                (WorkflowEdge.source_node_id.in_(node_ids))
                | (WorkflowEdge.target_node_id.in_(node_ids)),
            )
        )
        await node_service.db.execute(
            delete(WorkflowNode).where(
                WorkflowNode.project_id == project_id,
                WorkflowNode.id.in_(node_ids),
            )
        )
        await node_service.db.commit()
    except Exception:
        logger.exception("materialize_blueprint cleanup failed for %s", project_id)
        with contextlib.suppress(Exception):
            await node_service.db.rollback()


def _is_blueprint_bound_node(node: Any) -> bool:
    input_json = getattr(node, "input_json", None)
    if isinstance(input_json, str):
        with contextlib.suppress(json.JSONDecodeError, TypeError):
            input_json = json.loads(input_json)
    return isinstance(input_json, dict) and bool(input_json.get("blueprint_node_id"))


async def _delete_existing_blueprint_nodes(
    node_service: Any,
    project_id: str,
    *,
    exclude_node_ids: set[str] | None = None,
) -> int:
    db = getattr(node_service, "db", None)
    if db is None:
        return 0
    excluded = {str(item) for item in (exclude_node_ids or set()) if item}
    rows = await db.execute(select(WorkflowNode).where(WorkflowNode.project_id == project_id))
    nodes = list(rows.scalars().all())
    node_ids = [
        str(getattr(node, "id", ""))
        for node in nodes
        if _is_blueprint_bound_node(node) and str(getattr(node, "id", "")) not in excluded
    ]
    node_ids = [node_id for node_id in node_ids if node_id]
    if not node_ids:
        return 0
    try:
        await db.execute(
            delete(WorkflowEdge).where(
                WorkflowEdge.project_id == project_id,
                (WorkflowEdge.source_node_id.in_(node_ids))
                | (WorkflowEdge.target_node_id.in_(node_ids)),
            )
        )
        await db.execute(
            delete(WorkflowNode).where(
                WorkflowNode.project_id == project_id,
                WorkflowNode.id.in_(node_ids),
            )
        )
        await db.commit()
        return len(node_ids)
    except Exception:
        logger.exception("materialize_blueprint replacement cleanup failed for %s", project_id)
        with contextlib.suppress(Exception):
            await db.rollback()
        raise
