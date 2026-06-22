"""Blueprint tree data layer — read/write the JSON tree at blueprint.json.

The blueprint is a JSON tree stored at:
  data/projects/{project_id}/blueprint.json

Schema version 1.  Empty tree on first access.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

BLUEPRINT_FILENAME = "blueprint.json"
SCHEMA_VERSION = 1
TREE_VERSION_KEY = "tree_version"

# ── public API ────────────────────────────────────────────────────────────────


def blueprint_root(project_id: str) -> Path:
    """Absolute path to the project's blueprint.json file."""
    return Path(settings.PROJECT_ROOT) / "data" / "projects" / project_id / BLUEPRINT_FILENAME


def blueprint_exists(project_id: str) -> bool:
    return blueprint_root(project_id).exists()


def read_blueprint(project_id: str) -> dict[str, Any]:
    """Read the full blueprint tree.  Creates an empty tree if the file is
    missing or corrupt."""
    path = blueprint_root(project_id)
    if not path.exists():
        return _create_initial_tree(project_id)

    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("blueprint read failed for %s — recreating: %s", project_id, exc)
        return _create_initial_tree(project_id)

    if not isinstance(doc, dict):
        return _create_initial_tree(project_id)

    # Upgrade / repair missing fields
    if doc.get("version") != SCHEMA_VERSION:
        doc["version"] = SCHEMA_VERSION

    root: dict[str, Any] | None = doc.get("root") if isinstance(doc.get("root"), dict) else None
    if not root:
        doc["root"] = _empty_root_node()
    else:
        root.setdefault("id", "root")
        root.setdefault("type", "text")
        root.setdefault("title", "根节点")
        root.setdefault("content", "")
        root.setdefault("children", [])

    doc.setdefault("version", SCHEMA_VERSION)
    doc[TREE_VERSION_KEY] = _to_tree_version(doc.get(TREE_VERSION_KEY))
    doc.setdefault("skill", None)
    doc.setdefault("video_mode", None)
    doc.setdefault("title", None)
    doc.setdefault("status", "drafting")
    return doc


def summarize_blueprint_for_state(project_id: str) -> dict[str, Any] | None:
    """Return a compact semantic blueprint file summary for runtime state.

    This reads the blueprint file only when it already exists. It does not create
    a new file or promote the blueprint into DB state.
    """
    if not project_id or not blueprint_exists(project_id):
        return None
    doc = read_blueprint(project_id)
    if not blueprint_has_content(doc):
        return None
    root = doc.get("root") if isinstance(doc.get("root"), dict) else {}
    children = root.get("children") if isinstance(root.get("children"), list) else []
    nodes = _walk_tree_nodes(children)
    status = str(doc.get("status") or "drafting")
    summary: dict[str, Any] = {
        "schema_name": doc.get("schema_name") or "semantic_blueprint_tree",
        "status": status,
        "title": doc.get("title") or root.get("title") or "",
        "tree_version": doc.get(TREE_VERSION_KEY),
        "node_count": len(nodes),
        "root_child_count": len(children),
        "file_json": str(blueprint_root(project_id)),
        "needs_finalize": status == "drafting" and bool(children),
        "root_children": [
            _compact_node_ref(child)
            for child in children[:12]
            if isinstance(child, dict)
        ],
    }
    if doc.get("summary"):
        summary["summary"] = str(doc.get("summary") or "")[:360]
    if doc.get("source_request"):
        summary["source_request"] = str(doc.get("source_request") or "")[:240]
    fields = doc.get("fields") if isinstance(doc.get("fields"), dict) else {}
    if fields:
        summary["fields"] = {
            key: fields.get(key)
            for key in ("episode_count", "segment_seconds", "production_basis")
            if fields.get(key) not in (None, "", [], {})
        }
    return summary


def blueprint_has_content(doc: dict[str, Any]) -> bool:
    """Return True when a blueprint file contains user-visible project work.

    A read of an older empty project may have created a bare root-only
    blueprint.json. That file is storage residue, not a real draft.
    """
    if not isinstance(doc, dict):
        return False
    root = doc.get("root") if isinstance(doc.get("root"), dict) else {}
    children = root.get("children") if isinstance(root.get("children"), list) else []
    if children:
        return True
    status = str(doc.get("status") or "").strip()
    if status in {"pending_review", "active", "materialized"}:
        return True
    fields = doc.get("fields") if isinstance(doc.get("fields"), dict) else {}
    if any(value not in (None, "", [], {}) for value in fields.values()):
        return True
    for key in ("title", "summary", "source_request"):
        value = str(doc.get(key) or "").strip()
        if value and value != "根节点":
            return True
    root_title = str(root.get("title") or "").strip()
    root_content = str(root.get("content") or "").strip()
    return bool(root_content or (root_title and root_title != "根节点"))


def write_blueprint(project_id: str, doc: dict[str, Any]) -> int:
    """Atomically write the tree back to disk."""
    path = blueprint_root(project_id)
    path.parent.mkdir(parents=True, exist_ok=True)

    updated = dict(doc)
    previous = _to_tree_version(updated.get(TREE_VERSION_KEY))
    updated[TREE_VERSION_KEY] = previous + 1

    tmp = path.with_suffix(".json.tmp")
    payload = json.dumps(updated, ensure_ascii=False, indent=2)
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(payload)
    os.replace(tmp, path)
    return updated[TREE_VERSION_KEY]


def _walk_tree_nodes(children: list[Any]) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for child in children:
        if not isinstance(child, dict):
            continue
        nodes.append(child)
        grand = child.get("children") if isinstance(child.get("children"), list) else []
        nodes.extend(_walk_tree_nodes(grand))
    return nodes


def _compact_node_ref(node: dict[str, Any]) -> dict[str, Any]:
    children = node.get("children") if isinstance(node.get("children"), list) else []
    fields = node.get("fields") if isinstance(node.get("fields"), dict) else {}
    ref: dict[str, Any] = {
        "id": node.get("id"),
        "type": node.get("type"),
        "title": node.get("title"),
        "children": len(children),
    }
    for key in ("purpose", "production_path", "aspect_ratio", "duration_seconds"):
        value = node.get(key) or fields.get(key)
        if value not in (None, "", [], {}):
            ref[key] = value
    return {key: value for key, value in ref.items() if value not in (None, "", [], {})}


def _to_tree_version(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed >= 0 else 0


# ── tree helpers ──────────────────────────────────────────────────────────────


def find_node(root: dict[str, Any], node_id: str) -> dict[str, Any] | None:
    """Depth-first search for a node by id.  Returns the node dict or None."""
    if root.get("id") == node_id:
        return root
    for child in root.get("children") or []:
        if isinstance(child, dict):
            found = find_node(child, node_id)
            if found is not None:
                return found
    return None


def find_parent(root: dict[str, Any], node_id: str) -> tuple[dict[str, Any] | None, int]:
    """Return (parent_node, index_in_children) for *node_id*.
    Returns (None, -1) when the node is root or not found."""
    if root.get("id") == node_id:
        return None, -1  # root has no parent
    children: list[dict[str, Any]] = root.get("children") or []
    for idx, child in enumerate(children):
        if not isinstance(child, dict):
            continue
        if child.get("id") == node_id:
            return root, idx
        found_parent, found_idx = find_parent(child, node_id)
        if found_parent is not None:
            return found_parent, found_idx
    return None, -1


def add_child(parent: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    """Append *node* to parent.children. Fills in defaults. Returns the added node."""
    node.setdefault("status", "pending")
    node.setdefault("children", [])
    node.setdefault("created_at", _now_iso())
    node.setdefault("updated_at", _now_iso())
    if node.get("type") in ("image", "video"):
        node.setdefault("prompt", None)
    parent.setdefault("children", []).append(node)
    return node


def update_node(node: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Merge *patch* into *node*. Returns the node."""
    for key, value in patch.items():
        if value is None and key not in patch:
            continue
        node[key] = value
    node["updated_at"] = _now_iso()
    return node


def delete_node(root: dict[str, Any], node_id: str) -> bool:
    """Remove *node_id* and all its descendants from the tree."""
    parent, idx = find_parent(root, node_id)
    if parent is not None and idx >= 0:
        children: list[dict[str, Any]] = parent.get("children") or []
        if idx < len(children) and children[idx].get("id") == node_id:
            children.pop(idx)
            return True
    return False


def list_children(parent: dict[str, Any]) -> list[dict[str, Any]]:
    """Return a summary list of direct children (id, type, title, status only)."""
    return [
        {
            "id": c.get("id"),
            "type": c.get("type"),
            "title": c.get("title"),
            "status": c.get("status"),
        }
        for c in (parent.get("children") or [])
        if isinstance(c, dict)
    ]


def collect_references(node: dict[str, Any], root: dict[str, Any]) -> list[str]:
    """Resolve @node_id references in *node*.references to actual image URLs."""
    refs: list[str] = []
    for ref in (node.get("references") or []):
        if not isinstance(ref, str):
            continue
        if ref.startswith("@"):
            target_id = ref[1:]
            # skip self-reference and upload refs
            if "/" in target_id:
                # @upload/... — keep as-is for now
                refs.append(ref)
                continue
            target = find_node(root, target_id)
            if target is not None and target.get("url"):
                refs.append(str(target["url"]))
        else:
            refs.append(ref)
    return refs


# ── internal ──────────────────────────────────────────────────────────────────


def _now_iso() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _empty_root_node() -> dict[str, Any]:
    return {
        "id": "root",
        "type": "text",
        "title": "根节点",
        "content": "",
        "status": "pending",
        "children": [],
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }


def _create_initial_tree(project_id: str) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "version": SCHEMA_VERSION,
        TREE_VERSION_KEY: 0,
        "skill": None,
        "video_mode": None,
        "title": None,
        "status": "drafting",
        "root": _empty_root_node(),
    }
    write_blueprint(project_id, doc)
    return doc
