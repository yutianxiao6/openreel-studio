"""Deterministic structural repairs for semantic blueprint trees."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from app.agent.blueprint_tree_normalizer import (
    _collect_node_ids,
    _is_segment_node,
    _node_materializes,
    _normalize_references,
    _walk_nodes,
    _walk_nodes_with_parent,
)


def _auto_repair_flat_segment_media(root: dict[str, Any]) -> list[dict[str, str]]:
    """Move root-level segment media under the existing segment node when unambiguous."""
    children = root.get("children") if isinstance(root.get("children"), list) else []
    if not children:
        return []

    nodes = _walk_nodes(children)
    nodes_with_parent = _walk_nodes_with_parent(children)
    parent_by_id = {
        str(node.get("id") or ""): parent_id
        for node, parent_id in nodes_with_parent
        if node.get("id")
    }
    video_nodes = [
        node for node in nodes
        if node.get("type") == "video" and _node_materializes(node)
    ]
    root_video_nodes = [
        node for node in video_nodes
        if parent_by_id.get(str(node.get("id") or "")) == "root"
    ]
    if not root_video_nodes:
        return []

    segment_candidates = [node for node in nodes if _is_segment_node(node)]
    if len(segment_candidates) != 1:
        return []

    target = segment_candidates[0]
    target_id = str(target.get("id") or "")
    if not target_id:
        return []

    root_segment_media = [
        node for node in nodes
        if node.get("type") == "image"
        and _node_materializes(node)
        and parent_by_id.get(str(node.get("id") or "")) == "root"
        and re.search(
            r"scene|场景|storyboard|分镜|宫格|keyframe|首帧|尾帧|story_template|故事模板",
            f"{node.get('id') or ''}\n{node.get('title') or ''}",
            re.IGNORECASE,
        )
    ]
    move_ids = {
        str(node.get("id") or "")
        for node in [*root_segment_media, *root_video_nodes]
        if node.get("id")
    }
    if not move_ids:
        return []

    target_children = target.setdefault("children", [])
    if not isinstance(target_children, list):
        target_children = []
        target["children"] = target_children

    repairs: list[dict[str, str]] = []
    kept_children: list[dict[str, Any]] = []
    for child in children:
        if not isinstance(child, dict):
            kept_children.append(child)
            continue
        child_id = str(child.get("id") or "")
        if child_id in move_ids and child_id != target_id:
            target_children.append(child)
            repairs.append({"node_id": child_id, "parent_id": target_id})
            continue
        kept_children.append(child)
    root["children"] = kept_children
    return repairs


def _auto_repair_default_segment_container(root: dict[str, Any]) -> list[dict[str, str]]:
    """Create one lightweight segment container when root-level media needs grouping."""
    children = root.get("children") if isinstance(root.get("children"), list) else []
    if not children:
        return []
    nodes = _walk_nodes(children)
    if any(_is_segment_node(node) for node in nodes):
        return []
    nodes_with_parent = _walk_nodes_with_parent(children)
    parent_by_id = {
        str(node.get("id") or ""): parent_id
        for node, parent_id in nodes_with_parent
        if node.get("id")
    }
    root_video_nodes = [
        node for node in nodes
        if node.get("type") == "video"
        and _node_materializes(node)
        and parent_by_id.get(str(node.get("id") or "")) == "root"
    ]
    if not root_video_nodes:
        return []
    root_segment_media = [
        node for node in nodes
        if node.get("type") == "image"
        and _node_materializes(node)
        and parent_by_id.get(str(node.get("id") or "")) == "root"
        and re.search(
            r"scene|场景|storyboard|分镜|宫格|keyframe|首帧|尾帧|story_template|故事模板",
            f"{node.get('id') or ''}\n{node.get('title') or ''}",
            re.IGNORECASE,
        )
    ]
    if not root_segment_media:
        return []
    existing_ids = _collect_node_ids(children)
    segment_id = "segment_01"
    suffix = 2
    while segment_id in existing_ids:
        segment_id = f"segment_{suffix:02d}"
        suffix += 1
    now = datetime.now(timezone.utc).isoformat()
    segment = {
        "id": segment_id,
        "type": "text",
        "title": "15秒连续视频段",
        "content": "后端为保持蓝图层级清晰自动建立的单段容器；镜头节奏仍由分镜、prompt 和节点内容表达。",
        "status": "pending",
        "materialize": True,
        "fields": {"purpose": "segment_container", "auto_repaired": True},
        "children": [],
        "created_at": now,
        "updated_at": now,
    }
    move_ids = {
        str(node.get("id") or "")
        for node in [*root_segment_media, *root_video_nodes]
        if node.get("id")
    }
    repairs: list[dict[str, str]] = []
    kept_children: list[dict[str, Any]] = []
    inserted = False
    for child in children:
        if not isinstance(child, dict):
            kept_children.append(child)
            continue
        child_id = str(child.get("id") or "")
        if child_id in move_ids:
            segment["children"].append(child)
            repairs.append({"node_id": child_id, "parent_id": segment_id})
            if not inserted:
                kept_children.append(segment)
                inserted = True
            continue
        kept_children.append(child)
    if not inserted:
        kept_children.append(segment)
    root["children"] = kept_children
    return repairs


def _auto_repair_video_dependencies(root: dict[str, Any]) -> list[dict[str, Any]]:
    """Fill obvious video references when a single segment already contains the media chain."""
    children = root.get("children") if isinstance(root.get("children"), list) else []
    nodes = _walk_nodes(children)
    if not nodes:
        return []
    nodes_with_parent = _walk_nodes_with_parent(children)
    parent_by_id = {
        str(node.get("id") or ""): parent_id
        for node, parent_id in nodes_with_parent
        if node.get("id")
    }
    by_id = {
        str(node.get("id") or ""): node
        for node in nodes
        if node.get("id")
    }
    repairs: list[dict[str, Any]] = []

    def parent_chain(node_id: str) -> list[str]:
        chain: list[str] = []
        seen: set[str] = set()
        current = parent_by_id.get(node_id)
        while current and current != "root" and current not in seen:
            seen.add(current)
            chain.append(current)
            current = parent_by_id.get(current)
        return chain

    root_refs = [
        str(node.get("id") or "")
        for node, parent_id in nodes_with_parent
        if parent_id == "root"
        and node.get("id")
        and node.get("type") in {"text", "image"}
        and _node_materializes(node)
    ]

    for node, parent_id in nodes_with_parent:
        if node.get("type") != "video" or not _node_materializes(node):
            continue
        fields = node.get("fields") if isinstance(node.get("fields"), dict) else {}
        refs = _normalize_references(node.get("references") or fields.get("references"))
        deps = _normalize_references(node.get("depends_on") or fields.get("depends_on"))
        if refs or deps:
            continue
        video_id = str(node.get("id") or "")
        segment_ids = [
            ancestor_id
            for ancestor_id in parent_chain(video_id)
            if _is_segment_node(by_id.get(ancestor_id, {}))
        ]
        if parent_id != "root" and _is_segment_node(by_id.get(parent_id, {})):
            segment_ids.insert(0, parent_id)
        segment_id = next((item for item in segment_ids if item), "")
        if not segment_id:
            continue

        siblings = by_id.get(parent_id, {}).get("children") if parent_id != "root" else root.get("children")
        sibling_images = [
            str(sibling.get("id") or "")
            for sibling in (siblings or [])
            if isinstance(sibling, dict)
            and sibling is not node
            and sibling.get("id")
            and sibling.get("type") == "image"
            and _node_materializes(sibling)
        ]
        storyboard_deps = [
            item for item in sibling_images
            if re.search(r"storyboard|分镜|宫格|keyframe|首帧|尾帧|story_template|故事模板", item, re.IGNORECASE)
            or re.search(
                r"storyboard|分镜|宫格|keyframe|首帧|尾帧|story_template|故事模板",
                str((by_id.get(item) or {}).get("title") or ""),
                re.IGNORECASE,
            )
        ]
        dependency_ids = storyboard_deps or sibling_images
        reference_ids: list[str] = []
        for item in [*root_refs, segment_id, *sibling_images]:
            if item and item != video_id and item not in reference_ids:
                reference_ids.append(item)
        if not reference_ids and not dependency_ids:
            continue
        node["references"] = [f"@{item}" for item in reference_ids]
        if dependency_ids:
            node["depends_on"] = [f"@{item}" for item in dependency_ids]
        repairs.append({
            "node_id": video_id,
            "references": node.get("references") or [],
            "depends_on": node.get("depends_on") or [],
        })
    return repairs


def _auto_repair_video_production_paths(root: dict[str, Any]) -> list[dict[str, str]]:
    """Infer a missing production_path from available image dependencies."""
    children = root.get("children") if isinstance(root.get("children"), list) else []
    nodes = _walk_nodes(children)
    if not nodes:
        return []
    image_ids = {
        str(node.get("id") or "")
        for node in nodes
        if node.get("type") == "image" and _node_materializes(node) and node.get("id")
    }
    repairs: list[dict[str, str]] = []
    for node in nodes:
        if node.get("type") != "video" or not _node_materializes(node):
            continue
        fields = node.get("fields") if isinstance(node.get("fields"), dict) else {}
        if not isinstance(node.get("fields"), dict):
            node["fields"] = fields
        explicit = str(node.get("production_path") or fields.get("production_path") or fields.get("method") or fields.get("mode") or "").strip()
        if explicit:
            continue
        refs = _normalize_references(node.get("references") or fields.get("references"))
        deps = _normalize_references(node.get("depends_on") or fields.get("depends_on"))
        linked_ids = {str(ref).lstrip("@") for ref in [*refs, *deps]}
        path = "image_to_video" if image_ids and (linked_ids & image_ids or deps) else "text_to_video"
        node["production_path"] = path
        fields["production_path"] = path
        repairs.append({"node_id": str(node.get("id") or ""), "production_path": path})
    return repairs
